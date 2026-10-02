"""Orchestrator: runs a pipeline of stages for one job with state, retries and policy.

Key behaviours
- Job ID is created once (from the Actions run ID) and stamped on every event and record.
- State is persisted BEFORE a stage runs (attempt counted) and after it finishes, so a kill
  at any point is recoverable and cannot cause an unbounded retry loop.
- Retries happen inside the failed stage only; completed stages are never re-run unless
  their outputs are gone (media files missing/changed, or stored documents missing), in which
  case the earliest such stage and everything after it is reset.
- The policy layer alone decides whether to produce or publish; stages that require publish
  authorization are simply not invoked without one.
"""

from __future__ import annotations

import re
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Sequence

from pydantic import ValidationError

from factory.contracts.channel_config import ChannelConfig
from factory.contracts.director_plan import check_plan_against_channel, parse_director_plan
from factory.contracts.enums import (
    DIRECTOR_PLAN_DOC,
    PUBLICATION_DOC,
    JobStatus,
    RunOutcome,
    StageStatus,
)
from factory.contracts.errors import (
    ContractViolation,
    PolicyViolation,
    StageError,
    StateError,
    WorkspaceError,
)
from factory.contracts.ids import IDENT_PATTERN, make_job_id
from factory.contracts.job import (
    ExperimentMeta,
    FailureInfo,
    Job,
    PublicationInfo,
    StageRecord,
    VideoRecord,
    new_job,
)
from factory.contracts.stage import Stage, StageContext, StageResult
from factory.core.events import Event, EventSink, EventType, InMemorySink
from factory.core.policy import Policy
from factory.core.redact import SecretRedactor
from factory.core.retry import RetryConfig
from factory.core.state_machine import TERMINAL_JOB_STATUSES, transition_job, transition_stage
from factory.core.workspace import JobWorkspace
from factory.state.store import StateStore

_DOC_NAME_RE = re.compile(IDENT_PATTERN)
_MAX_MESSAGE = 2000
_MAX_DETAIL = 4000


@dataclass(frozen=True)
class RunOptions:
    cleanup_on_success: bool = True
    cleanup_on_failure: bool = True  # only reached for terminal failures (budget exhausted)


@dataclass(frozen=True)
class RunResult:
    outcome: RunOutcome
    job: Job | None
    reason: str | None = None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Orchestrator:
    def __init__(
        self,
        *,
        pipeline: Sequence[Stage],
        store: StateStore,
        workspace_root: Path,
        policy: Policy,
        events: EventSink | None = None,
        retry: RetryConfig | None = None,
        options: RunOptions | None = None,
        redactor: SecretRedactor | None = None,
        clock: Callable[[], datetime] = _utcnow,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        names = [s.name for s in pipeline]
        if not names:
            raise ValueError("pipeline must contain at least one stage")
        if len(set(names)) != len(names):
            raise ValueError(f"duplicate stage names in pipeline: {names}")
        self.pipeline = list(pipeline)
        self.stage_names = names
        self.store = store
        self.workspace_root = Path(workspace_root)
        self.policy = policy
        self.events: EventSink = events or InMemorySink()
        self.retry = retry or RetryConfig()
        unknown = set(self.retry.per_stage) - set(names)
        if unknown:
            raise ValueError(f"retry config references unknown stages: {sorted(unknown)}")
        self.options = options or RunOptions()
        self.redactor = redactor or SecretRedactor.from_env()
        self._clock = clock
        self._sleep = sleep

    # ------------------------------------------------------------------ public API
    def start(self, channel: ChannelConfig, run_id: str | int) -> RunResult:
        """Create the job for this run (or resume it, if the same run ID was seen before)."""
        decision = self.policy.decide(channel.lifecycle_state)
        if not decision.may_produce:
            self._emit(EventType.JOB_BLOCKED, channel.channel, message=decision.reason)
            return RunResult(RunOutcome.BLOCKED, None, decision.reason)

        job_id = make_job_id(channel.channel, run_id, self._clock())
        if self.store.load_job(job_id) is not None:
            return self.resume(job_id, channel)

        job = new_job(
            job_id=job_id,
            channel=channel.channel,
            stage_names=self.stage_names,
            global_mode=self.policy.global_mode,
            channel_state=channel.lifecycle_state,
            now=self._clock(),
        )
        self._save(job)
        return self._execute(job, channel, resumed=False)

    def resume(self, job_id: str, channel: ChannelConfig) -> RunResult:
        job = self.store.load_job(job_id)
        if job is None:
            raise StateError(f"cannot resume unknown job {job_id}")
        if job.channel != channel.channel:
            raise StateError(f"job {job_id} belongs to channel {job.channel!r}, not {channel.channel!r}")
        if job.status in TERMINAL_JOB_STATUSES:
            outcome = RunOutcome.COMPLETED if job.status is JobStatus.COMPLETED else RunOutcome.CANCELLED
            return RunResult(outcome, job, f"job already {job.status}")

        decision = self.policy.decide(channel.lifecycle_state)
        if not decision.may_produce:
            self._emit(EventType.JOB_BLOCKED, job.channel, job_id=job.job_id, message=decision.reason)
            return RunResult(RunOutcome.BLOCKED, job, decision.reason)

        ws = JobWorkspace(self.workspace_root, job.job_id)
        self._recover(job, ws)
        job.global_mode = self.policy.global_mode
        job.channel_state = channel.lifecycle_state
        return self._execute(job, channel, resumed=True)

    def run_channel(self, channel: ChannelConfig, run_id: str | int) -> RunResult:
        """Scheduled entry point: continue an unfinished job for the channel, else start one."""
        pending = self.store.list_jobs(channel.channel, [JobStatus.PENDING, JobStatus.RUNNING])
        if pending:
            return self.resume(pending[0].job_id, channel)
        return self.start(channel, run_id)

    def cancel_job(self, job_id: str, reason: str) -> Job:
        job = self.store.load_job(job_id)
        if job is None:
            raise StateError(f"cannot cancel unknown job {job_id}")
        if job.status in TERMINAL_JOB_STATUSES:
            return job
        for rec in job.stages:
            if rec.status in (StageStatus.PENDING, StageStatus.RUNNING, StageStatus.FAILED):
                transition_stage(rec, StageStatus.CANCELLED)
        transition_job(job, JobStatus.CANCELLED)
        job.status_reason = self.redactor.redact(reason)
        self._save(job)
        self._emit(EventType.JOB_CANCELLED, job.channel, job_id=job.job_id, message=job.status_reason)
        self._cleanup(job, JobWorkspace(self.workspace_root, job.job_id))
        return job

    # ------------------------------------------------------------------ recovery
    def _recover(self, job: Job, ws: JobWorkspace) -> None:
        """Make a persisted job safe to continue after a crash, a failure or lost media."""
        now = self._clock()
        # 1. A stage still RUNNING means the process died mid-stage: count it as a failed attempt.
        for rec in job.stages:
            if rec.status is StageStatus.RUNNING:
                rec.failures.append(
                    FailureInfo(
                        stage=rec.name,
                        attempt=rec.attempts,
                        error_type="Interrupted",
                        message="run ended while the stage was running",
                        retryable=True,
                        occurred_at=now,
                    )
                )
                rec.finished_at = now
                transition_stage(rec, StageStatus.FAILED)

        # 2. Policy-skipped stages are re-evaluated on every run.
        for rec in job.stages:
            if rec.status is StageStatus.SKIPPED:
                transition_stage(rec, StageStatus.PENDING)
                rec.note = None

        # 3. Earliest SUCCESS stage whose outputs are gone invalidates itself and all after it.
        first_bad = next(
            (
                i
                for i, rec in enumerate(job.stages)
                if rec.status is StageStatus.SUCCESS and not self._outputs_available(job, rec, ws)
            ),
            None,
        )
        if first_bad is not None:
            for rec in job.stages[first_bad:]:
                if rec.status is not StageStatus.PENDING:
                    self._invalidate(job, rec, f"outputs of stage {job.stages[first_bad].name!r} unavailable")

        # 4. An explicit resume of a FAILED job grants the failed stage a fresh attempt budget.
        if job.status is JobStatus.FAILED:
            for rec in job.stages:
                if rec.status is StageStatus.FAILED:
                    rec.attempts = 0

    def _outputs_available(self, job: Job, rec: StageRecord, ws: JobWorkspace) -> bool:
        if not all(ws.verify(ref) for ref in rec.artifacts):
            return False
        return all(self.store.load_document(job.job_id, d) is not None for d in rec.documents)

    def _invalidate(self, job: Job, rec: StageRecord, why: str) -> None:
        transition_stage(rec, StageStatus.PENDING)  # failure history and costs are preserved
        rec.attempts = 0
        rec.artifacts = []
        rec.documents = []
        rec.metrics = {}
        rec.started_at = rec.finished_at = rec.duration_s = None
        rec.note = f"invalidated: {why}"
        self._emit(EventType.STAGE_INVALIDATED, job.channel, job_id=job.job_id, stage=rec.name, message=why)

    # ------------------------------------------------------------------ execution
    def _execute(self, job: Job, channel: ChannelConfig, *, resumed: bool) -> RunResult:
        ws = JobWorkspace(self.workspace_root, job.job_id).create()
        if job.status is not JobStatus.RUNNING:
            transition_job(job, JobStatus.RUNNING)
        job.status_reason = None
        job.failed_stage = None
        self._save(job)
        self._emit(
            EventType.JOB_RESUMED if resumed else EventType.JOB_STARTED,
            job.channel,
            job_id=job.job_id,
            message=f"mode={self.policy.global_mode} channel_state={channel.lifecycle_state}",
        )

        failed = False
        for stage in self.pipeline:
            rec = job.stage(stage.name)
            if rec.status in (StageStatus.SUCCESS, StageStatus.SKIPPED):
                continue

            auth = None
            if stage.requires_publish_authorization:
                auth = self.policy.authorize_publish(job.job_id, job.channel, channel.lifecycle_state)
                if auth is None:
                    self._skip(job, rec, self.policy.decide(channel.lifecycle_state).reason)
                    continue

            if not self._run_stage(job, channel, stage, rec, ws, auth):
                failed = True
                break

        if failed:
            transition_job(job, JobStatus.FAILED)
            self._save(job)
            self._emit(
                EventType.JOB_FAILED,
                job.channel,
                job_id=job.job_id,
                stage=job.failed_stage,
                message=job.status_reason or "",
            )
            if self.options.cleanup_on_failure:
                self._cleanup(job, ws)
            return RunResult(RunOutcome.FAILED, job, job.status_reason)

        transition_job(job, JobStatus.COMPLETED)
        self._save(job)
        self._emit(
            EventType.JOB_COMPLETED,
            job.channel,
            job_id=job.job_id,
            message="completed",
            data={
                "cost_usd": job.total_cost_usd(),
                "experiment_id": job.experiment.experiment_id if job.experiment else None,
                "published": any(PUBLICATION_DOC in r.documents for r in job.stages),
                "stage_durations_s": {r.name: r.duration_s for r in job.stages},
            },
        )
        if self.options.cleanup_on_success:
            self._cleanup(job, ws)
        return RunResult(RunOutcome.COMPLETED, job)

    def _skip(self, job: Job, rec: StageRecord, why: str) -> None:
        if rec.status is StageStatus.FAILED:
            transition_stage(rec, StageStatus.PENDING)
        transition_stage(rec, StageStatus.SKIPPED)
        rec.note = why
        self._save(job)
        self._emit(EventType.STAGE_SKIPPED, job.channel, job_id=job.job_id, stage=rec.name, message=why)

    def _run_stage(
        self,
        job: Job,
        channel: ChannelConfig,
        stage: Stage,
        rec: StageRecord,
        ws: JobWorkspace,
        auth,
    ) -> bool:
        if stage.requires_publish_authorization and auth is None:
            raise PolicyViolation(f"stage {stage.name!r} requires publish authorization")

        policy = self.retry.for_stage(stage.name)
        rec.max_attempts = policy.max_attempts

        while True:
            if rec.attempts >= rec.max_attempts:
                job.failed_stage = rec.name
                job.status_reason = f"retry budget exhausted for stage {rec.name!r}"
                return False

            transition_stage(rec, StageStatus.RUNNING)
            rec.attempts += 1
            rec.started_at = self._clock()
            rec.finished_at = None
            self._save(job)  # persisted BEFORE running: a kill now still counts this attempt
            self._emit(
                EventType.STAGE_STARTED,
                job.channel,
                job_id=job.job_id,
                stage=rec.name,
                attempt=rec.attempts,
                max_attempts=rec.max_attempts,
            )

            ctx = StageContext(
                job_id=job.job_id,
                channel=channel,
                workspace=ws,
                attempt=rec.attempts,
                max_attempts=rec.max_attempts,
                publish_authorization=auth,
                _load_document=lambda name, _jid=job.job_id: self.store.load_document(_jid, name),
            )
            try:
                result = stage.run(ctx)
                self._commit(job, channel, rec, result, ws)
            except PolicyViolation:
                raise  # a policy bug must be loud, not retried
            except Exception as exc:  # noqa: BLE001 - every stage failure goes through the retry model
                retryable = exc.retryable if isinstance(exc, StageError) else True
                failure = self._failure(rec, exc, retryable)
                rec.failures.append(failure)
                rec.finished_at = self._clock()
                transition_stage(rec, StageStatus.FAILED)
                self._save(job)
                self._emit(
                    EventType.STAGE_FAILED,
                    job.channel,
                    job_id=job.job_id,
                    stage=rec.name,
                    attempt=rec.attempts,
                    max_attempts=rec.max_attempts,
                    message=failure.message,
                    data={"error_type": failure.error_type, "retryable": retryable},
                )
                if not retryable or rec.attempts >= rec.max_attempts:
                    job.failed_stage = rec.name
                    job.status_reason = f"stage {rec.name!r} failed: {failure.error_type}: {failure.message}"
                    return False
                delay = policy.delay_after(rec.attempts)
                self._emit(
                    EventType.STAGE_RETRY,
                    job.channel,
                    job_id=job.job_id,
                    stage=rec.name,
                    attempt=rec.attempts,
                    max_attempts=rec.max_attempts,
                    message=f"retrying stage {rec.name} in {delay:.1f}s",
                    data={"delay_s": delay},
                )
                self._sleep(delay)
                continue

            self._save(job)
            self._emit(
                EventType.STAGE_SUCCEEDED,
                job.channel,
                job_id=job.job_id,
                stage=rec.name,
                attempt=rec.attempts,
                max_attempts=rec.max_attempts,
                data={"duration_s": rec.duration_s},
            )
            return True

    def _commit(
        self, job: Job, channel: ChannelConfig, rec: StageRecord, result: StageResult, ws: JobWorkspace
    ) -> None:
        """Validate everything first; only then persist documents and mutate the record."""
        try:
            refs = [ws.describe(p) for p in result.artifacts]
        except WorkspaceError as exc:
            raise ContractViolation(f"stage {rec.name!r} returned an unusable artifact: {exc}") from exc

        for name in result.documents:
            if not _DOC_NAME_RE.match(name):
                raise ContractViolation(f"invalid document name {name!r} from stage {rec.name!r}")

        experiment: ExperimentMeta | None = None
        if DIRECTOR_PLAN_DOC in result.documents:
            plan = parse_director_plan(result.documents[DIRECTOR_PLAN_DOC])
            if plan.job_id != job.job_id:
                raise ContractViolation(f"plan.job_id {plan.job_id!r} != job {job.job_id!r}")
            check_plan_against_channel(plan, channel)
            exp = plan.experiment
            experiment = ExperimentMeta(
                experiment_id=exp.experiment_id,
                channel=plan.channel,
                format=exp.format,
                hook_type=exp.hook_type,
                story_format=exp.story_format,
                duration_target_s=exp.duration_target_s,
                visual_style=exp.visual_style,
            )

        publication: PublicationInfo | None = None
        if PUBLICATION_DOC in result.documents:
            try:
                publication = PublicationInfo.model_validate(result.documents[PUBLICATION_DOC])
            except ValidationError as exc:
                raise ContractViolation(f"invalid publication document: {exc}") from exc

        effective_experiment = experiment or job.experiment
        video: VideoRecord | None = None
        if publication is not None:
            video = VideoRecord(
                video_id=publication.video_id,
                channel=job.channel,
                job_id=job.job_id,
                experiment_id=effective_experiment.experiment_id if effective_experiment else None,
                format=effective_experiment.format if effective_experiment else None,
                published_at=publication.published_at,
            )

        # Side effects first (idempotent writes), record mutation last, so a failure above
        # leaves the stage RUNNING -> FAILED cleanly. The caller persists the job afterwards.
        for name, data in result.documents.items():
            self.store.save_document(job.job_id, name, data)
        if video is not None:
            self.store.save_video(video)

        now = self._clock()
        transition_stage(rec, StageStatus.SUCCESS)
        rec.finished_at = now
        rec.duration_s = (now - rec.started_at).total_seconds() if rec.started_at else None
        rec.artifacts = refs
        rec.documents = list(result.documents)
        rec.costs.extend(result.costs)
        rec.metrics = dict(result.metrics)
        if experiment is not None:
            job.experiment = experiment

    # ------------------------------------------------------------------ helpers
    def _failure(self, rec: StageRecord, exc: Exception, retryable: bool) -> FailureInfo:
        return FailureInfo(
            stage=rec.name,
            attempt=rec.attempts,
            error_type=type(exc).__name__,
            message=self.redactor.redact(str(exc))[:_MAX_MESSAGE],
            retryable=retryable,
            occurred_at=self._clock(),
            detail=self.redactor.redact("".join(traceback.format_exception(exc)))[-_MAX_DETAIL:],
        )

    def _cleanup(self, job: Job, ws: JobWorkspace) -> None:
        """Remove this job's temporary media. Never raises: cleanup must not change a job's fate."""
        try:
            removed = ws.cleanup()
            self._emit(
                EventType.CLEANUP, job.channel, job_id=job.job_id, message="workspace removed" if removed else "nothing to remove"
            )
        except Exception as exc:  # noqa: BLE001
            self._emit(
                EventType.CLEANUP,
                job.channel,
                job_id=job.job_id,
                message=f"cleanup failed: {self.redactor.redact(str(exc))}",
                data={"ok": False},
            )

    def _save(self, job: Job) -> None:
        job.updated_at = self._clock()
        self.store.save_job(job)

    def _emit(
        self,
        type_: EventType,
        channel: str,
        *,
        job_id: str | None = None,
        stage: str | None = None,
        attempt: int | None = None,
        max_attempts: int | None = None,
        message: str = "",
        data: dict | None = None,
    ) -> None:
        self.events.emit(
            Event(
                ts=self._clock(),
                type=type_,
                channel=channel,
                job_id=job_id,
                stage=stage,
                attempt=attempt,
                max_attempts=max_attempts,
                message=message,
                data=data or {},
            )
        )
