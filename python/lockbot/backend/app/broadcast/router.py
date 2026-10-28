from collections import Counter
from datetime import datetime, timedelta
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.orm import Session

from lockbot.backend.app.audit.service import write_audit_log
from lockbot.backend.app.auth.dependencies import require_super_admin
from lockbot.backend.app.auth.models import User
from lockbot.backend.app.broadcast.models import BroadcastJob, BroadcastTarget
from lockbot.backend.app.broadcast.service import recent_targets
from lockbot.backend.app.database import get_db
from lockbot.backend.app.rate_limit import limiter

router = APIRouter(prefix="/api/admin/broadcasts", tags=["settings"])


class PreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str = Field(min_length=1, max_length=1800)
    recent_days: int = Field(default=7, ge=1, le=30)
    mention_all: bool = False

    @field_validator("message")
    @classmethod
    def nonempty(cls, value):
        if not value.strip():
            raise ValueError("Message must not be empty")
        return value.strip()


def summary(db, job, *, offset=0, limit=100):
    targets = db.query(BroadcastTarget).filter_by(job_id=job.id)
    counts = Counter(s for (s,) in targets.with_entities(BroadcastTarget.status).all())
    return dict(
        id=job.id,
        message=job.message,
        mention_all=job.mention_all,
        recent_days=job.recent_days,
        status=job.status,
        created_at=job.created_at,
        total=sum(counts.values()),
        counts=dict(counts),
        targets=[
            dict(
                id=t.id,
                bot_id=t.bot_id,
                bot_name=t.bot_name,
                group_id=t.group_id,
                status=t.status,
                result_code=t.result_code,
            )
            for t in targets.order_by(BroadcastTarget.id).offset(offset).limit(limit)
        ],
        offset=offset,
        limit=limit,
    )


@router.post("/preview", status_code=201)
@limiter.limit("10/minute")
def preview(
    request: Request, body: PreviewRequest, user: User = Depends(require_super_admin), db: Session = Depends(get_db)
):
    targets = recent_targets(db, body.recent_days)
    if len(targets) > 1000:
        raise HTTPException(422, "Too many groups; choose a shorter recent activity window")
    job = BroadcastJob(id=str(uuid4()), creator_id=user.id, **body.model_dump())
    db.add(job)
    db.flush()
    db.add_all([BroadcastTarget(job_id=job.id, **target) for target in targets])
    db.commit()
    return summary(db, job)


@router.get("")
def history(user: User = Depends(require_super_admin), db: Session = Depends(get_db)):
    jobs = (
        db.query(BroadcastJob)
        .filter(BroadcastJob.status != "draft")
        .order_by(BroadcastJob.created_at.desc())
        .limit(20)
        .all()
    )
    return [summary(db, job, limit=0) for job in jobs]


@router.get("/{job_id}")
def status(job_id: str, offset: int = 0, user: User = Depends(require_super_admin), db: Session = Depends(get_db)):
    if offset < 0:
        raise HTTPException(422, "Invalid offset")
    job = db.get(BroadcastJob, job_id)
    if job is None:
        raise HTTPException(404, "Broadcast not found")
    return summary(db, job, offset=offset)


@router.post("/{job_id}/send", status_code=202)
def submit(job_id: str, request: Request, user: User = Depends(require_super_admin), db: Session = Depends(get_db)):
    job = db.get(BroadcastJob, job_id)
    if job is None or job.creator_id != user.id:
        raise HTTPException(404, "Broadcast not found")
    if job.status != "draft":
        return summary(db, job)  # idempotent: the same preview can only be submitted once
    if job.created_at < datetime.utcnow() - timedelta(minutes=10):
        raise HTTPException(409, "Preview expired; preview again before sending")
    if not db.query(BroadcastTarget).filter_by(job_id=job_id).count():
        raise HTTPException(409, "No recent eligible groups")
    # A single UPDATE makes concurrent confirmation safe.
    changed = (
        db.query(BroadcastJob)
        .filter_by(id=job_id, status="draft")
        .update({"status": "queued", "submitted_at": datetime.utcnow()})
    )
    if changed:
        write_audit_log(
            db,
            user,
            "site.broadcast",
            detail={"broadcast_id": job_id, "mention_all": job.mention_all, "recent_days": job.recent_days},
            ip=request.client.host if request.client else None,
        )
    db.commit()
    db.refresh(job)
    return summary(db, job)
