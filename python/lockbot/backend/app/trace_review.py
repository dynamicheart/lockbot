"""Opt-in private reviewer for Agent evaluation snapshots and anchored feedback."""

import hashlib
import json
import os
import re
import sqlite3
import stat
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, model_validator

from lockbot.backend.app.auth.dependencies import require_super_admin
from lockbot.backend.app.auth.models import User

PRIVATE_HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
MAX_DIAGNOSTIC_BYTES = 16 * 1024 * 1024
MAX_DATASET_INDEX_BYTES = 1024 * 1024
MAX_DATASET_METADATA_BYTES = 64 * 1024
MAX_DATASET_LIST_BYTES = 4 * 1024 * 1024
MAX_DATASETS = 4096
SNAPSHOT_LIMITS = {"raw.json": 32 * 1024 * 1024, "latest.json": 1024 * 1024}
DATASET_PATTERN = re.compile(r"^[a-f0-9]{64}$")


class PrivateReviewRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def handle(request):
            try:
                return await original(request)
            except HTTPException as error:
                error.headers = {**(error.headers or {}), **PRIVATE_HEADERS}
                raise
            except RequestValidationError as error:
                response = await request_validation_exception_handler(request, error)
                response.headers.update(PRIVATE_HEADERS)
                return response

        return handle


class ReviewComment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str = Field(min_length=1, max_length=256)
    scope: Literal["case", "run"] = "run"
    subject: Literal["general", "scenario", "prompt", "expected", "grading", "interface"] = "general"
    run_index: int | None = Field(default=None, ge=0, le=10000)
    run_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    raw: bool = True
    dataset_id: str | None = Field(default=None, max_length=64, pattern=r"^[a-f0-9]{64}$")
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
    router = APIRouter(include_in_schema=False, route_class=PrivateReviewRoute)
    store_lock = threading.Lock()

    @contextmanager
    def dataset_catalog():
        root = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            folder = os.open("datasets", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
        finally:
            os.close(root)
        try:
            if os.fstat(folder).st_mode & 0o777 != 0o700:
                raise ValueError("Private dataset directory required")
            yield folder
        finally:
            os.close(folder)

    def private_file(folder, name, limit, *, size=None, digest=None, read=True):
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=folder)
        with os.fdopen(descriptor, "rb") as source:
            details = os.fstat(source.fileno())
            if (
                not stat.S_ISREG(details.st_mode)
                or details.st_mode & 0o777 != 0o600
                or details.st_size > limit
                or (size is not None and details.st_size != size)
            ):
                raise ValueError("Invalid dataset file")
            if not read:
                return None
            data = source.read(limit + 1)
        if (
            len(data) > limit
            or (size is not None and len(data) != size)
            or (digest is not None and hashlib.sha256(data).hexdigest() != digest)
        ):
            raise ValueError("Dataset integrity check failed")
        return data

    def dataset_registry(folder):
        try:
            data = json.loads(private_file(folder, "index.json", MAX_DATASET_INDEX_BYTES))
        except FileNotFoundError:
            return []  # A staged, unregistered directory is never published.
        entries = data.get("datasets") if isinstance(data, dict) else None
        if (
            type(data.get("schema_version")) is not int
            or data["schema_version"] != 1
            or not isinstance(entries, list)
            or len(entries) > MAX_DATASETS
            or any(not isinstance(item, str) or not DATASET_PATTERN.fullmatch(item) for item in entries)
            or len(set(entries)) != len(entries)
        ):
            raise ValueError("Invalid dataset registry")
        return entries

    @contextmanager
    def dataset_files(folder, dataset_id):
        descriptor = os.open(dataset_id, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=folder)
        try:
            if os.fstat(descriptor).st_mode & 0o777 != 0o700:
                raise ValueError("Private dataset directory required")
            metadata = json.loads(private_file(descriptor, "metadata.json", MAX_DATASET_METADATA_BYTES))
            if (
                not isinstance(metadata, dict)
                or type(metadata.get("schema_version")) is not int
                or metadata["schema_version"] != 1
                or metadata.get("dataset_id") != dataset_id
                or metadata.get("raw_sha256") != dataset_id
                or not isinstance(metadata.get("latest_sha256"), str)
                or not DATASET_PATTERN.fullmatch(metadata["latest_sha256"])
            ):
                raise ValueError("Invalid dataset metadata")
            for name, limit in SNAPSHOT_LIMITS.items():
                field = "raw" if name == "raw.json" else "latest"
                size = metadata.get(field + "_size_bytes")
                if type(size) is not int or not 2 <= size <= limit:
                    raise ValueError("Invalid dataset size")
                private_file(descriptor, name, limit, size=size, read=False)
            yield descriptor, metadata
        finally:
            os.close(descriptor)

    def read_snapshot(raw, dataset_id=None):
        if dataset_id is not None:
            if not DATASET_PATTERN.fullmatch(dataset_id):
                raise HTTPException(404, "Dataset is unavailable", headers=PRIVATE_HEADERS)
            try:
                with dataset_catalog() as folder:
                    if dataset_id not in dataset_registry(folder):
                        raise HTTPException(404, "Dataset is unavailable", headers=PRIVATE_HEADERS)
                    try:
                        with dataset_files(folder, dataset_id) as (files, metadata):
                            name, field = ("raw.json", "raw") if raw else ("latest.json", "latest")
                            return private_file(
                                files,
                                name,
                                SNAPSHOT_LIMITS[name],
                                size=metadata[field + "_size_bytes"],
                                digest=metadata[field + "_sha256"],
                            )
                    except FileNotFoundError:
                        raise ValueError("Published dataset is incomplete") from None
            except FileNotFoundError:
                raise HTTPException(404, "Dataset is unavailable", headers=PRIVATE_HEADERS) from None
            except (OSError, ValueError, TypeError, AttributeError, RecursionError):
                raise HTTPException(503, "Dataset is unavailable", headers=PRIVATE_HEADERS) from None
        filename, max_bytes = ("raw.json", 32 * 1024 * 1024) if raw else ("latest.json", 1024 * 1024)
        try:
            with (directory / filename).open("rb") as source:
                data = source.read(max_bytes + 1)
        except FileNotFoundError:
            raise HTTPException(status_code=404, headers=PRIVATE_HEADERS) from None
        if len(data) > max_bytes:
            raise HTTPException(status_code=503, headers=PRIVATE_HEADERS)
        return data

    def digest_trial(snapshot, case_id, trial, raw, run_index, dataset_id=None):
        content = {
            "raw": raw,
            "version": snapshot.get("version"),
            "case_id": case_id,
            "run_index": run_index,
            "trial": trial,
        }
        if dataset_id is not None:
            content["dataset_id"] = dataset_id
        return hashlib.sha256(json.dumps(content, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def run_identity(case_id, run_index, raw, dataset_id=None):
        try:
            snapshot = json.loads(read_snapshot(raw, dataset_id))
            trial = snapshot["cases"][case_id][run_index]
        except (ValueError, KeyError, IndexError, TypeError, RecursionError):
            raise HTTPException(404, "Trace run is unavailable", headers=PRIVATE_HEADERS) from None
        # Include visibility and package provenance: a comment must never drift to a
        # different run just because its model name or array position stayed the same.
        return digest_trial(snapshot, case_id, trial, raw, run_index, dataset_id), trial

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
    def snapshot(raw: bool = False, dataset_id: str | None = None):
        try:
            data = json.loads(read_snapshot(raw, dataset_id))
            data["review_runs"] = {
                case_id: [
                    digest_trial(data, case_id, trial, raw, index, dataset_id) for index, trial in enumerate(trials)
                ]
                for case_id, trials in data["cases"].items()
            }
        except (ValueError, KeyError, TypeError, RecursionError):
            raise HTTPException(503, "Snapshot is unavailable", headers=PRIVATE_HEADERS) from None
        if dataset_id is not None:
            data["dataset_id"] = dataset_id
        return JSONResponse(data, headers=PRIVATE_HEADERS)

    @router.get("/api/dev/trace-review/datasets", dependencies=[Depends(require_super_admin)])
    def datasets():
        try:
            with dataset_catalog() as folder:
                entries = []
                total_bytes = 0
                for dataset_id in dataset_registry(folder):
                    try:
                        with dataset_files(folder, dataset_id) as (_, metadata):
                            total_bytes += len(json.dumps(metadata, ensure_ascii=False).encode())
                            if total_bytes > MAX_DATASET_LIST_BYTES:
                                raise ValueError("Dataset index response is too large")
                            entries.append(metadata)
                    except FileNotFoundError:
                        raise ValueError("Published dataset is incomplete") from None
            return JSONResponse({"datasets": entries}, headers=PRIVATE_HEADERS)
        except FileNotFoundError:
            return JSONResponse({"datasets": []}, headers=PRIVATE_HEADERS)
        except (OSError, ValueError, TypeError, AttributeError, RecursionError):
            raise HTTPException(503, "Dataset index is unavailable", headers=PRIVATE_HEADERS) from None

    @router.get("/api/dev/trace-review/evidence/{digest}", dependencies=[Depends(require_super_admin)])
    def diagnostic(digest: str):
        if not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise HTTPException(404, "Evidence is unavailable", headers=PRIVATE_HEADERS)
        try:
            # Directory and file descriptors prevent path substitution or symlink
            # escapes. Read bounded bytes before responding so the digest covers
            # exactly the body sent, even if an operator replaces a file later.
            folder = os.open(directory / "evidence", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                if os.fstat(folder).st_mode & 0o777 != 0o700:
                    raise HTTPException(503, "Evidence is unavailable", headers=PRIVATE_HEADERS)
                descriptor = os.open(digest, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=folder)
            finally:
                os.close(folder)
            with os.fdopen(descriptor, "rb") as source:
                details = os.fstat(source.fileno())
                if (
                    not stat.S_ISREG(details.st_mode)
                    or details.st_mode & 0o777 != 0o600
                    or details.st_size > MAX_DIAGNOSTIC_BYTES
                ):
                    raise HTTPException(503, "Evidence is unavailable", headers=PRIVATE_HEADERS)
                content = source.read(MAX_DIAGNOSTIC_BYTES + 1)
        except FileNotFoundError:
            raise HTTPException(404, "Evidence is unavailable", headers=PRIVATE_HEADERS) from None
        except OSError:
            raise HTTPException(503, "Evidence is unavailable", headers=PRIVATE_HEADERS) from None
        if len(content) > MAX_DIAGNOSTIC_BYTES or hashlib.sha256(content).hexdigest() != digest:
            raise HTTPException(503, "Evidence integrity check failed", headers=PRIVATE_HEADERS)
        return Response(
            content,
            media_type="application/octet-stream",
            headers={
                **PRIVATE_HEADERS,
                "Content-Disposition": f'attachment; filename="trace-evidence-{digest}.bin"',
                "X-Content-SHA256": digest,
            },
        )

    @router.get("/api/dev/trace-review/comments", dependencies=[Depends(require_super_admin)])
    def comments(
        response: Response,
        case_id: str | None = None,
        run_index: int | None = None,
        raw: bool = True,
        dataset_id: str | None = None,
    ):
        response.headers.update(PRIVATE_HEADERS)
        if run_index is not None and not 0 <= run_index <= 10000:
            raise HTTPException(422, "Invalid run index", headers=PRIVATE_HEADERS)
        if dataset_id is not None and not (case_id is not None and run_index is not None):
            read_snapshot(raw, dataset_id)  # Even a comment-only selector must be published and valid.
        run_id = (
            run_identity(case_id, run_index, raw, dataset_id)[0]
            if case_id is not None and run_index is not None
            else None
        )
        with open_store() as db:
            rows = comment_rows(db)
        return {
            "run_id": run_id,
            "comments": [
                row
                for row in rows
                if (case_id is None or row["case_id"] == case_id)
                and (dataset_id is None or global_feedback(row) or row.get("dataset_id") == dataset_id)
            ],
        }

    @router.post("/api/dev/trace-review/comments", status_code=201)
    def add_comment(body: ReviewComment, response: Response, user: User = Depends(require_super_admin)):
        response.headers.update(PRIVATE_HEADERS)
        if body.scope == "case":
            try:
                snapshot = json.loads(read_snapshot(body.raw, body.dataset_id))
            except (ValueError, TypeError, RecursionError):
                raise HTTPException(503, "Snapshot is unavailable", headers=PRIVATE_HEADERS) from None
            if body.case_id not in snapshot.get("cases", {}):
                raise HTTPException(404, "Case is unavailable", headers=PRIVATE_HEADERS)
            model = None
        else:
            current, trial = run_identity(body.case_id, body.run_index, body.raw, body.dataset_id)
            if body.run_id != current:
                raise HTTPException(409, "Trace changed; reload before commenting", headers=PRIVATE_HEADERS)
            validate_anchor(body.anchor, trial)
            model = trial.get("model")
        if not body.body.strip():
            raise HTTPException(422, "Write a comment", headers=PRIVATE_HEADERS)
        now = datetime.now(timezone.utc).isoformat()
        fields = body.model_dump()
        if body.dataset_id is None or global_feedback(fields):
            fields.pop("dataset_id")
        record = {
            **fields,
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
        comment_id: str,
        body: ReviewCommentUpdate,
        response: Response,
        user: User = Depends(require_super_admin),
        dataset_id: str | None = None,
    ):
        response.headers.update(PRIVATE_HEADERS)
        with open_store() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload FROM comments WHERE id = ?", (comment_id,)).fetchone()
            if row is None:
                raise HTTPException(404, headers=PRIVATE_HEADERS)
            record = json.loads(row[0])
            if dataset_id is not None:
                read_snapshot(record.get("raw", True), dataset_id)
                if not global_feedback(record) and record.get("dataset_id") != dataset_id:
                    raise HTTPException(404, "Comment is unavailable", headers=PRIVATE_HEADERS)
            record.update(
                status=body.status, updated_at=datetime.now(timezone.utc).isoformat(), updated_by=user.username
            )
            db.execute("UPDATE comments SET payload = ? WHERE id = ?", (json.dumps(record), comment_id))
        return record

    return router


def global_feedback(record):
    return record.get("scope") == "case" or str(record.get("anchor", "")).startswith("ui:")
