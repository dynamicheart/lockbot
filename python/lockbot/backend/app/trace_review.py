"""Opt-in private reviewer for Agent evaluation snapshots and anchored feedback."""

import hashlib
import json
import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, model_validator

from lockbot.backend.app.auth.dependencies import require_super_admin
from lockbot.backend.app.auth.models import User

PRIVATE_HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}


class ReviewComment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str = Field(min_length=1, max_length=256)
    scope: Literal["case", "run"] = "run"
    subject: Literal["general", "scenario", "prompt", "expected", "grading", "interface"] = "general"
    run_index: int | None = Field(default=None, ge=0, le=10000)
    run_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    raw: bool = True
    anchor: str = Field(
        max_length=160,
        pattern=r"^(case|run|ui:[a-z-]+|message:\d+:\d+:(text|label)|tool:\d+:\d+:\d+:(name|label|input|output|error))$",
    )
    quote: str = Field(default="", max_length=4000)
    body: str = Field(min_length=1, max_length=4000)

    @model_validator(mode="after")
    def scoped_anchor(self):
        if self.scope == "case":
            if self.run_index is not None or self.run_id is not None or self.anchor != "case":
                raise ValueError("Case feedback is independent of model runs")
        elif self.run_index is None or self.run_id is None or self.anchor == "case":
            raise ValueError("Run feedback requires the original evidence identity")
        return self


class ReviewCommentUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["open", "resolved"]


def trace_review_router(directory: Path) -> APIRouter:
    router = APIRouter(include_in_schema=False)
    store_lock = threading.Lock()

    def read_snapshot(raw):
        filename, max_bytes = ("raw.json", 32 * 1024 * 1024) if raw else ("latest.json", 1024 * 1024)
        try:
            with (directory / filename).open("rb") as source:
                data = source.read(max_bytes + 1)
        except FileNotFoundError:
            raise HTTPException(status_code=404, headers=PRIVATE_HEADERS) from None
        if len(data) > max_bytes:
            raise HTTPException(status_code=503, headers=PRIVATE_HEADERS)
        return data

    def digest_trial(snapshot, case_id, trial, raw, run_index):
        content = {
            "raw": raw,
            "version": snapshot.get("version"),
            "case_id": case_id,
            "run_index": run_index,
            "trial": trial,
        }
        return hashlib.sha256(json.dumps(content, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def run_identity(case_id, run_index, raw):
        try:
            snapshot = json.loads(read_snapshot(raw))
            trial = snapshot["cases"][case_id][run_index]
        except (ValueError, KeyError, IndexError, TypeError):
            raise HTTPException(404, "Trace run is unavailable", headers=PRIVATE_HEADERS) from None
        # Include visibility and package provenance: a comment must never drift to a
        # different run just because its model name or array position stayed the same.
        return digest_trial(snapshot, case_id, trial, raw, run_index), trial

    @contextmanager
    def open_store():
        path = directory / "review-comments.sqlite3"
        if path.is_symlink():
            raise HTTPException(503, "Comment store is unavailable", headers=PRIVATE_HEADERS)
        with store_lock:
            try:
                descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                pass
            else:
                os.close(descriptor)
            db = sqlite3.connect(path, timeout=5)
            db.execute("CREATE TABLE IF NOT EXISTS comments (id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
            db.commit()
        try:
            with db:
                yield db
        finally:
            db.close()

    def comment_rows(db):
        return [json.loads(row[0]) for row in db.execute("SELECT payload FROM comments ORDER BY rowid")]

    def validate_anchor(anchor, trial):
        if anchor == "run":
            return
        parts = anchor.split(":")
        if parts[0] == "ui":
            if parts[1] in {"case-title", "run-title", "counts", "chat-tab", "event-tab", "case-nav", "payload-tab"}:
                return
        else:
            try:
                entry = trial["sessions"][int(parts[1])][int(parts[2])]
                if parts[0] == "message" and entry["role"] in {"user", "assistant", "human", "missing"}:
                    return
                if parts[0] == "tool" and entry["role"] == "tools":
                    entry["steps"][int(parts[3])]
                    return
            except (IndexError, KeyError, ValueError, TypeError):
                pass
        raise HTTPException(422, "Comment anchor is unavailable", headers=PRIVATE_HEADERS)

    @router.get("/dev/trace-review")
    def viewer():
        page = directory / "viewer.html"
        if not page.is_file():
            raise HTTPException(status_code=404)
        return FileResponse(page, media_type="text/html", headers=PRIVATE_HEADERS)

    @router.get("/api/dev/trace-review/snapshot", dependencies=[Depends(require_super_admin)])
    def snapshot(raw: bool = False):
        try:
            data = json.loads(read_snapshot(raw))
            data["review_runs"] = {
                case_id: [digest_trial(data, case_id, trial, raw, index) for index, trial in enumerate(trials)]
                for case_id, trials in data["cases"].items()
            }
        except (ValueError, KeyError, TypeError):
            raise HTTPException(503, "Snapshot is unavailable", headers=PRIVATE_HEADERS) from None
        return JSONResponse(data, headers=PRIVATE_HEADERS)

    @router.get("/api/dev/trace-review/comments", dependencies=[Depends(require_super_admin)])
    def comments(response: Response, case_id: str | None = None, run_index: int | None = None, raw: bool = True):
        response.headers.update(PRIVATE_HEADERS)
        if run_index is not None and not 0 <= run_index <= 10000:
            raise HTTPException(422, "Invalid run index", headers=PRIVATE_HEADERS)
        run_id = run_identity(case_id, run_index, raw)[0] if case_id is not None and run_index is not None else None
        with open_store() as db:
            rows = comment_rows(db)
        return {"run_id": run_id, "comments": [row for row in rows if case_id is None or row["case_id"] == case_id]}

    @router.post("/api/dev/trace-review/comments", status_code=201)
    def add_comment(body: ReviewComment, response: Response, user: User = Depends(require_super_admin)):
        response.headers.update(PRIVATE_HEADERS)
        if body.scope == "case":
            snapshot = json.loads(read_snapshot(body.raw))
            if body.case_id not in snapshot.get("cases", {}):
                raise HTTPException(404, "Case is unavailable", headers=PRIVATE_HEADERS)
            model = None
        else:
            current, trial = run_identity(body.case_id, body.run_index, body.raw)
            if body.run_id != current:
                raise HTTPException(409, "Trace changed; reload before commenting", headers=PRIVATE_HEADERS)
            validate_anchor(body.anchor, trial)
            model = trial.get("model")
        if not body.body.strip():
            raise HTTPException(422, "Write a comment", headers=PRIVATE_HEADERS)
        now = datetime.now(timezone.utc).isoformat()
        record = {
            **body.model_dump(),
            "id": uuid.uuid4().hex,
            "body": body.body.strip(),
            "author": user.username,
            "model": model,
            "status": "open",
            "created_at": now,
            "updated_at": now,
        }
        with open_store() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT COUNT(*) FROM comments").fetchone()[0] >= 1000:
                raise HTTPException(
                    409, "Comment store is full; ask the administrator to archive it", headers=PRIVATE_HEADERS
                )
            db.execute("INSERT INTO comments (id, payload) VALUES (?, ?)", (record["id"], json.dumps(record)))
        return record

    @router.patch("/api/dev/trace-review/comments/{comment_id}")
    def update_comment(
        comment_id: str, body: ReviewCommentUpdate, response: Response, user: User = Depends(require_super_admin)
    ):
        response.headers.update(PRIVATE_HEADERS)
        with open_store() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload FROM comments WHERE id = ?", (comment_id,)).fetchone()
            if row is None:
                raise HTTPException(404, headers=PRIVATE_HEADERS)
            record = json.loads(row[0])
            record.update(
                status=body.status, updated_at=datetime.now(timezone.utc).isoformat(), updated_by=user.username
            )
            db.execute("UPDATE comments SET payload = ? WHERE id = ?", (json.dumps(record), comment_id))
        return record

    return router
