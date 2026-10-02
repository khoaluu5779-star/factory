"""Explicit transition tables for stages and jobs. All status changes go through here."""

from __future__ import annotations

from factory.contracts.enums import JobStatus, StageStatus
from factory.contracts.errors import InvalidTransition
from factory.contracts.job import Job, StageRecord

S = StageStatus
J = JobStatus

STAGE_TRANSITIONS: dict[StageStatus, frozenset[StageStatus]] = {
    S.PENDING: frozenset({S.RUNNING, S.SKIPPED, S.CANCELLED}),
    S.RUNNING: frozenset({S.SUCCESS, S.FAILED, S.CANCELLED}),
    S.FAILED: frozenset({S.RUNNING, S.PENDING, S.CANCELLED}),  # RUNNING = retry, PENDING = reset
    S.SUCCESS: frozenset({S.PENDING}),  # only by invalidation (outputs went missing)
    S.SKIPPED: frozenset({S.PENDING}),
    S.CANCELLED: frozenset(),
}

JOB_TRANSITIONS: dict[JobStatus, frozenset[JobStatus]] = {
    J.PENDING: frozenset({J.RUNNING, J.CANCELLED}),
    J.RUNNING: frozenset({J.COMPLETED, J.FAILED, J.CANCELLED}),
    J.FAILED: frozenset({J.RUNNING, J.CANCELLED}),  # RUNNING only via an explicit resume
    J.COMPLETED: frozenset(),
    J.CANCELLED: frozenset(),
}

TERMINAL_JOB_STATUSES = frozenset({J.COMPLETED, J.CANCELLED})


def transition_stage(record: StageRecord, new: StageStatus) -> None:
    if new not in STAGE_TRANSITIONS[record.status]:
        raise InvalidTransition(f"stage {record.name}: {record.status} -> {new} is not allowed")
    record.status = new


def transition_job(job: Job, new: JobStatus) -> None:
    if new not in JOB_TRANSITIONS[job.status]:
        raise InvalidTransition(f"job {job.job_id}: {job.status} -> {new} is not allowed")
    job.status = new
