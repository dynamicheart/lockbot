"""Administrator-only execution DTOs; no credentials or raw identity proofs."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Phase = Literal["planned", "freezing", "checkpointed", "switched", "activating", "aborting", "completed", "aborted"]


class PlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_application_id: int = Field(gt=0)
    target_chat_id: str = Field(pattern=r"^oc_[A-Za-z0-9_-]{1,125}$")
    operation_id: str = Field(pattern=r"^[a-f0-9]{32}$")


class ChallengeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reviewed_target_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    publication_evidence_ref: str = Field(pattern=r"^[a-f0-9]{32}$")
    publication_reviewed: Literal[True]


class PhaseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_phase: Phase
