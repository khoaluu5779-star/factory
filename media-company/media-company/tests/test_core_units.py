"""Small units: ids, state machine, retry policy, policy, redaction, workspace, store."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from factory.contracts.authorization import PublishAuthorization
from factory.contracts.enums import ChannelState, GlobalMode, JobStatus, StageStatus
from factory.contracts.errors import InvalidTransition, PolicyViolation, StateError, WorkspaceError
from factory.contracts.ids import make_job_id, parse_job_id, resolve_run_id
from factory.contracts.job import Job, VideoRecord, new_job
from factory.core.policy import Policy, load_global_mode
from factory.core.redact import MASK, SecretRedactor
from factory.core.retry import RetryPolicy
from factory.core.state_machine import (
    JOB_TRANSITIONS,
    STAGE_TRANSITIONS,
    transition_job,
    transition_stage,
)
from factory.core.workspace import JobWorkspace
from factory.state.file_store import FileStateStore
from tests.conftest import REPO_ROOT

NOW = datetime(2026, 9, 27, 23, 59, tzinfo=timezone.utc)
JOB_ID = "JOB-20260927-drama-42"


# ---- ids -------------------------------------------------------------------
def test_job_id_format_and_roundtrip():
    jid = make_job_id("drama", 9876543210, NOW)
    assert jid == "JOB-20260927-drama-9876543210"
    assert parse_job_id(jid) == ("20260927", "drama", "9876543210")


@pytest.mark.parametrize("channel, run", [("Drama", "1"), ("dra-ma", "1"), ("drama", "abc"), ("drama", "")])
def test_job_id_rejects_bad_inputs(channel, run):
    with pytest.raises(ValueError):
        make_job_id(channel, run, NOW)


def test_run_id_from_env_or_timestamp():
    assert resolve_run_id({"GITHUB_RUN_ID": "555"}, NOW) == "555"
    assert resolve_run_id({}, NOW).isdigit()
    with pytest.raises(ValueError):
        resolve_run_id({"GITHUB_RUN_ID": "x1"}, NOW)


# ---- state machine ---------------------------------------------------------
def _job() -> Job:
    return new_job(
        job_id=JOB_ID, channel="drama", stage_names=["a", "b"],
        global_mode=GlobalMode.TEST, channel_state=ChannelState.TEST, now=NOW,
    )


def test_every_stage_status_has_a_table_entry():
    assert set(STAGE_TRANSITIONS) == set(StageStatus)
    assert set(JOB_TRANSITIONS) == set(JobStatus)


def test_stage_happy_path_and_retry_path():
    rec = _job().stages[0]
    for step in (StageStatus.RUNNING, StageStatus.FAILED, StageStatus.RUNNING, StageStatus.SUCCESS):
        transition_stage(rec, step)
    assert rec.status is StageStatus.SUCCESS


@pytest.mark.parametrize(
    "start, target",
    [
        (StageStatus.PENDING, StageStatus.SUCCESS),
        (StageStatus.PENDING, StageStatus.FAILED),
        (StageStatus.SUCCESS, StageStatus.RUNNING),
        (StageStatus.SUCCESS, StageStatus.FAILED),
        (StageStatus.CANCELLED, StageStatus.PENDING),
        (StageStatus.SKIPPED, StageStatus.RUNNING),
    ],
)
def test_illegal_stage_transitions_raise(start, target):
    rec = _job().stages[0]
    rec.status = start
    with pytest.raises(InvalidTransition):
        transition_stage(rec, target)
    assert rec.status is start


def test_job_transitions():
    job = _job()
    transition_job(job, JobStatus.RUNNING)
    transition_job(job, JobStatus.COMPLETED)
    for target in JobStatus:
        with pytest.raises(InvalidTransition):
            transition_job(job, target)  # COMPLETED is terminal


def test_failed_job_can_only_resume_or_cancel():
    job = _job()
    job.status = JobStatus.FAILED
    with pytest.raises(InvalidTransition):
        transition_job(job, JobStatus.COMPLETED)
    transition_job(job, JobStatus.RUNNING)


# ---- retry -----------------------------------------------------------------
def test_retry_backoff_is_capped_and_deterministic():
    p = RetryPolicy(max_attempts=5, initial_delay_s=2, backoff_factor=2, max_delay_s=10)
    assert [p.delay_after(n) for n in (1, 2, 3, 4)] == [2, 4, 8, 10]


@pytest.mark.parametrize("n", [0, 11])
def test_retry_attempts_are_bounded(n):
    with pytest.raises(ValidationError):
        RetryPolicy(max_attempts=n)


# ---- policy ----------------------------------------------------------------
@pytest.mark.parametrize(
    "mode, state, produce, publish",
    [
        (GlobalMode.SHUTDOWN, ChannelState.SCALE, False, False),
        (GlobalMode.TEST, ChannelState.SCALE, True, False),
        (GlobalMode.TEST, ChannelState.EVALUATING, True, False),
        (GlobalMode.AUTO, ChannelState.TEST, True, False),
        (GlobalMode.AUTO, ChannelState.EVALUATING, True, True),
        (GlobalMode.AUTO, ChannelState.CONTINUE, True, True),
        (GlobalMode.AUTO, ChannelState.SCALE, True, True),
        (GlobalMode.AUTO, ChannelState.IDEA, False, False),
        (GlobalMode.AUTO, ChannelState.PAUSE, False, False),
        (GlobalMode.AUTO, ChannelState.KILL, False, False),
    ],
)
def test_policy_matrix(mode, state, produce, publish):
    d = Policy(mode).decide(state)
    assert (d.may_produce, d.may_publish) == (produce, publish)
    assert (Policy(mode).authorize_publish(JOB_ID, "drama", state) is not None) is publish


def test_every_mode_and_state_combination_is_defined():
    for mode in GlobalMode:
        for state in ChannelState:
            Policy(mode).decide(state)  # must not raise


@pytest.mark.parametrize("raw, expected", [("auto", GlobalMode.AUTO), (" TEST ", GlobalMode.TEST), ("SHUTDOWN", GlobalMode.SHUTDOWN)])
def test_mode_parsing(raw, expected):
    assert load_global_mode({"FACTORY_MODE": raw}) is expected


@pytest.mark.parametrize("env", [{}, {"FACTORY_MODE": ""}, {"FACTORY_MODE": "yolo"}])
def test_missing_or_invalid_mode_fails_safe(env):
    assert load_global_mode(env) is GlobalMode.SHUTDOWN


def test_publish_authorization_cannot_be_forged():
    with pytest.raises(PolicyViolation):
        PublishAuthorization(job_id=JOB_ID, channel="drama")
    with pytest.raises(PolicyViolation):
        PublishAuthorization(job_id=JOB_ID, channel="drama", _seal=object())


def test_only_the_policy_module_issues_authorizations():
    offenders = []
    for path in (REPO_ROOT / "factory").rglob("*.py"):
        rel = path.relative_to(REPO_ROOT).as_posix()
        if rel in {"factory/contracts/authorization.py", "factory/core/policy.py"}:
            continue
        if "issue_publish_authorization" in path.read_text():
            offenders.append(rel)
    assert offenders == []


# ---- redaction -------------------------------------------------------------
def test_redactor_masks_secret_env_values_only():
    env = {"YT_REFRESH_TOKEN_DRAMA": "1//super-secret-token", "HOME": "/home/user", "SHORT_KEY": "abc"}
    red = SecretRedactor.from_env(env)
    text = "failed calling https://x?key=1//super-secret-token at /home/user abc"
    assert red.redact(text) == f"failed calling https://x?key={MASK} at /home/user abc"


# ---- workspace -------------------------------------------------------------
def test_workspace_rejects_unsafe_paths(tmp_path: Path):
    ws = JobWorkspace(tmp_path, JOB_ID).create()
    for bad in ("../escape.txt", "/etc/passwd", "a/../../b", ""):
        with pytest.raises(WorkspaceError):
            ws.path(bad)
    with pytest.raises(WorkspaceError):
        JobWorkspace(tmp_path, "../../etc")


def test_workspace_manifest_detects_missing_and_changed_files(tmp_path: Path):
    ws = JobWorkspace(tmp_path, JOB_ID).create()
    f = ws.path("images/a.png")
    f.parent.mkdir()
    f.write_bytes(b"one")
    ref = ws.describe("images/a.png")
    assert ws.verify(ref)
    f.write_bytes(b"two")
    assert not ws.verify(ref)
    f.unlink()
    assert not ws.verify(ref)


def test_cleanup_removes_only_its_own_job(tmp_path: Path):
    mine = JobWorkspace(tmp_path, JOB_ID).create()
    other = JobWorkspace(tmp_path, "JOB-20260927-drama-43").create()
    (mine.root / "x.bin").write_bytes(b"1")
    (other.root / "keep.bin").write_bytes(b"2")
    unrelated = tmp_path / "notes.txt"
    unrelated.write_text("keep")
    assert mine.cleanup() is True
    assert not mine.root.exists()
    assert (other.root / "keep.bin").exists() and unrelated.exists()
    assert mine.cleanup() is False  # idempotent


def test_cleanup_refuses_symlinked_workspace(tmp_path: Path):
    victim = tmp_path / "precious"
    victim.mkdir()
    (victim / "data.txt").write_text("keep")
    ws = JobWorkspace(tmp_path, JOB_ID)
    ws.root.parent.mkdir(parents=True)
    ws.root.symlink_to(victim, target_is_directory=True)
    with pytest.raises(WorkspaceError):
        ws.cleanup()
    assert (victim / "data.txt").exists()


# ---- state store -----------------------------------------------------------
def test_store_roundtrip_and_listing(tmp_path: Path):
    store = FileStateStore(tmp_path)
    a = _job()
    b = new_job(job_id="JOB-20260928-drama-43", channel="drama", stage_names=["a"],
                global_mode=GlobalMode.AUTO, channel_state=ChannelState.SCALE,
                now=datetime(2026, 9, 28, tzinfo=timezone.utc))
    c = new_job(job_id="JOB-20260928-other-44", channel="other", stage_names=["a"],
                global_mode=GlobalMode.AUTO, channel_state=ChannelState.SCALE, now=NOW)
    for j in (a, b, c):
        store.save_job(j)
    assert store.load_job(JOB_ID) == a
    assert store.load_job("JOB-20260101-drama-1") is None
    assert [j.job_id for j in store.list_jobs("drama")] == [b.job_id, a.job_id]  # newest first
    b.status = JobStatus.RUNNING
    store.save_job(b)
    assert [j.job_id for j in store.list_jobs("drama", [JobStatus.RUNNING])] == [b.job_id]
    assert not list(tmp_path.rglob(".tmp-*"))  # atomic writes leave no temp files


def test_store_documents_and_path_safety(tmp_path: Path):
    store = FileStateStore(tmp_path)
    store.save_document(JOB_ID, "research", {"a": 1})
    assert store.load_document(JOB_ID, "research") == {"a": 1}
    assert store.load_document(JOB_ID, "nope") is None
    for bad in ("../x", "A b", "", "a/b"):
        with pytest.raises(StateError):
            store.save_document(JOB_ID, bad, {})
    with pytest.raises(StateError):
        store.save_document("../../evil", "research", {})


def test_store_rejects_unsupported_version_and_corruption(tmp_path: Path):
    store = FileStateStore(tmp_path)
    store.save_job(_job())
    path = tmp_path / "jobs" / JOB_ID / "job.json"
    raw = json.loads(path.read_text())
    raw["state_schema_version"] = "9.9"
    path.write_text(json.dumps(raw))
    with pytest.raises(StateError, match="unsupported"):
        store.load_job(JOB_ID)
    path.write_text("{not json")
    with pytest.raises(StateError, match="corrupt"):
        store.load_job(JOB_ID)


def test_store_video_records(tmp_path: Path):
    store = FileStateStore(tmp_path)
    rec = VideoRecord(video_id="abc123", channel="drama", job_id=JOB_ID, experiment_id="DR-0001",
                      format="short", published_at=NOW)
    store.save_video(rec)
    assert store.load_video("abc123") == rec
    with pytest.raises(StateError):
        store.load_video("../x")
