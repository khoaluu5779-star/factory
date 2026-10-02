"""Job model: the single persisted record that follows a Job ID through every stage."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from factory.contracts.enums import ChannelState, GlobalMode, JobStatus, StageStatus
from factory.contracts.errors import StateError
from factory.contracts.ids import CHANNEL_ID_PATTERN, JOB_ID_PATTERN

STATE_SCHEMA_VERSION = "1.0"
SUPPORTED_STATE_VERSIONS = (STATE_SCHEMA_VERSION,)


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ArtifactRef(_Model):
    """A temporary workspace file produced by a stage, verifiable by size and hash."""

    path: str  # relative to the job workspace
    size_bytes: int = Field(ge=0)
    sha256: str


class CostEntry(_Model):
    """Cost metadata. amount_usd is 0.0 for free tiers; units keeps usage visible anyway."""

    provider: str  # "gemini", "pollinations", "edge_tts", "github_actions", ...
    amount_usd: float = Field(default=0.0, ge=0)
    units: float | None = None
    unit_label: str | None = None


class FailureInfo(_Model):
    stage: str
    attempt: int
    error_type: str
    message: str
    retryable: bool
    occurred_at: datetime
    detail: str | None = None  # truncated, redacted traceback


class StageRecord(_Model):
    name: str
    status: StageStatus = StageStatus.PENDING
    attempts: int = 0
    max_attempts: int = 0
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_s: float | None = None
    artifacts: list[ArtifactRef] = Field(default_factory=list)
    documents: list[str] = Field(default_factory=list)  # names saved in the StateStore
    costs: list[CostEntry] = Field(default_factory=list)
    metrics: dict[str, Any] = Field(default_factory=dict)
    failures: list[FailureInfo] = Field(default_factory=list)  # full history, never trimmed
    note: str | None = None  # e.g. why SKIPPED / invalidated


class ExperimentMeta(_Model):
    experiment_id: str
    channel: str
    format: str
    hook_type: str
    story_format: str
    duration_target_s: float
    visual_style: str
    result: str | None = None  # filled in later by analytics


class Job(_Model):
    state_schema_version: str = STATE_SCHEMA_VERSION
    job_id: str = Field(pattern=JOB_ID_PATTERN)
    channel: str = Field(pattern=CHANNEL_ID_PATTERN)
    status: JobStatus = JobStatus.PENDING
    status_reason: str | None = None
    failed_stage: str | None = None
    created_at: datetime
    updated_at: datetime
    global_mode: GlobalMode  # mode at creation/last (re)start, for the audit trail
    channel_state: ChannelState
    experiment: ExperimentMeta | None = None
    stages: list[StageRecord]

    def stage(self, name: str) -> StageRecord:
        for rec in self.stages:
            if rec.name == name:
                return rec
        raise StateError(f"job {self.job_id} has no stage {name!r}")

    def total_cost_usd(self) -> float:
        return sum(c.amount_usd for rec in self.stages for c in rec.costs)

    def cost_by_provider(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for rec in self.stages:
            for c in rec.costs:
                out[c.provider] = out.get(c.provider, 0.0) + c.amount_usd
        return out

    def all_failures(self) -> list[FailureInfo]:
        return [f for rec in self.stages for f in rec.failures]


class PublicationInfo(_Model):
    """Contents of the 'publication' document a publish stage must return."""

    video_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    published_at: datetime
    url: str | None = None


class VideoRecord(_Model):
    """Links a published video back to the job and experiment that produced it."""

    state_schema_version: str = STATE_SCHEMA_VERSION
    video_id: str
    channel: str
    job_id: str
    experiment_id: str | None
    format: str | None
    published_at: datetime
    status: str = "published"


def check_state_version(version: str, what: str) -> None:
    if version not in SUPPORTED_STATE_VERSIONS:
        raise StateError(
            f"{what} has unsupported state_schema_version {version!r}; supported: {SUPPORTED_STATE_VERSIONS}"
        )


def new_job(
    *,
    job_id: str,
    channel: str,
    stage_names: list[str],
    global_mode: GlobalMode,
    channel_state: ChannelState,
    now: datetime,
) -> Job:
    return Job(
        job_id=job_id,
        channel=channel,
        created_at=now,
        updated_at=now,
        global_mode=global_mode,
        channel_state=channel_state,
        stages=[StageRecord(name=n) for n in stage_names],
    )
