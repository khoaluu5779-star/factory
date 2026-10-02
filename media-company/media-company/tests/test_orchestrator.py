"""Integration tests for the orchestrator using fake stages only (no network, no credentials)."""

from __future__ import annotations

import shutil

import pytest

from factory.contracts.channel_config import ChannelConfig
from factory.contracts.enums import GlobalMode, JobStatus, RunOutcome, StageName, StageStatus
from factory.contracts.errors import ContractViolation, PermanentStageError, PolicyViolation, StateError
from factory.contracts.job import CostEntry
from factory.core.events import EventType
from factory.core.orchestrator import RunOptions
from factory.core.redact import SecretRedactor
from factory.core.retry import RetryConfig, RetryPolicy
from factory.core.workspace import JobWorkspace
from factory.stages.fakes import FakeDirectorStage, FakePublishStage, FakeStage
from tests.conftest import build_env, make_stages

RUN = "9001"
DIRECT = StageName.DIRECTOR.value


def statuses(job) -> dict[str, StageStatus]:
    return {r.name: r.status for r in job.stages}


# ---- happy path / final states -------------------------------------------------
def test_successful_job_publishes_and_reaches_final_state(tmp_path, publishing_channel):
    env = build_env(tmp_path, mode=GlobalMode.AUTO)
    result = env.orch.start(publishing_channel, RUN)

    assert result.outcome is RunOutcome.COMPLETED
    job = result.job
    assert job.status is JobStatus.COMPLETED
    assert set(statuses(job).values()) == {StageStatus.SUCCESS}
    assert env.stages["publish"].calls == 1
    assert env.store.load_job(job.job_id) == job  # persisted state matches


def test_job_id_uses_actions_run_id_and_propagates_everywhere(tmp_path, publishing_channel):
    env = build_env(tmp_path, mode=GlobalMode.AUTO)
    job = env.orch.start(publishing_channel, RUN).job
    assert job.job_id.startswith("JOB-20260927-drama-") and job.job_id.endswith(f"-{RUN}")

    assert all(e.job_id == job.job_id for e in env.sink.events if e.type is not EventType.JOB_BLOCKED)
    plan = env.store.load_document(job.job_id, "director_plan")
    assert plan["job_id"] == job.job_id
    video = env.store.load_video(env.store.load_document(job.job_id, "publication")["video_id"])
    assert video.job_id == job.job_id and video.channel == "drama"
    # the (already cleaned-up) workspace path was derived from the same id
    assert JobWorkspace(env.workspace_root, job.job_id).root.name == job.job_id


def test_experiment_metadata_links_video_to_experiment(tmp_path, publishing_channel):
    env = build_env(tmp_path, mode=GlobalMode.AUTO)
    job = env.orch.start(publishing_channel, RUN).job
    assert job.experiment.experiment_id == "DR-0001"
    assert job.experiment.channel == "drama" and job.experiment.format == "short"
    assert job.experiment.result is None
    video_id = env.store.load_document(job.job_id, "publication")["video_id"]
    video = env.store.load_video(video_id)
    assert video.experiment_id == "DR-0001" and video.status == "published"


def test_costs_are_tracked_per_job(tmp_path, channel):
    stages = make_stages(images=FakeStage("images", files={"images/a.png": b"x"},
                                          costs=[CostEntry(provider="pollinations", units=4, unit_label="images")]))
    env = build_env(tmp_path, mode=GlobalMode.TEST, stages=stages)
    job = env.orch.start(channel, RUN).job
    assert job.total_cost_usd() == 0.0
    assert set(job.cost_by_provider()) == {"pollinations", "gemini"}


# ---- modes: TEST never publishes, SHUTDOWN never produces --------------------------
def test_test_mode_never_publishes(tmp_path, publishing_channel):
    env = build_env(tmp_path, mode=GlobalMode.TEST)
    result = env.orch.start(publishing_channel, RUN)  # channel WOULD be allowed to publish
    assert result.outcome is RunOutcome.COMPLETED
    assert env.stages["publish"].calls == 0
    assert result.job.stage("publish").status is StageStatus.SKIPPED
    assert "TEST" in result.job.stage("publish").note
    assert env.store.load_document(result.job.job_id, "publication") is None
    assert env.stages["render"].calls == 1  # everything before publish still ran


def test_auto_mode_with_unpromoted_channel_does_not_publish(tmp_path, channel):
    env = build_env(tmp_path, mode=GlobalMode.AUTO)  # drama is lifecycle TEST
    result = env.orch.start(channel, RUN)
    assert result.outcome is RunOutcome.COMPLETED
    assert env.stages["publish"].calls == 0


def test_shutdown_prevents_all_production(tmp_path, channel):
    env = build_env(tmp_path, mode=GlobalMode.SHUTDOWN)
    result = env.orch.start(channel, RUN)
    assert result.outcome is RunOutcome.BLOCKED and result.job is None
    assert all(s.calls == 0 for s in env.stages.values())
    assert env.store.list_jobs() == []
    assert not env.workspace_root.exists()
    assert env.sink.types() == [EventType.JOB_BLOCKED]


@pytest.mark.parametrize("state", ["PAUSE", "KILL", "IDEA"])
def test_paused_or_killed_channel_produces_nothing(tmp_path, channel, state):
    env = build_env(tmp_path, mode=GlobalMode.AUTO)
    result = env.orch.start(channel.model_copy(update={"lifecycle_state": state}), RUN)
    assert result.outcome is RunOutcome.BLOCKED
    assert all(s.calls == 0 for s in env.stages.values())


def test_orchestrator_refuses_publish_stage_without_authorization(tmp_path, publishing_channel):
    env = build_env(tmp_path, mode=GlobalMode.AUTO)
    job = env.orch.start(publishing_channel, RUN).job
    publish = FakePublishStage()
    rec = job.stage("publish")
    with pytest.raises(PolicyViolation):
        env.orch._run_stage(job, publishing_channel, publish, rec, JobWorkspace(env.workspace_root, job.job_id), None)
    assert publish.calls == 0


# ---- retries ---------------------------------------------------------------------
def test_only_the_failed_stage_is_retried(tmp_path, channel):
    stages = make_stages(tts=FakeStage("tts", files={"audio/a.wav": b"w"}, fail_times=2))
    env = build_env(tmp_path, mode=GlobalMode.TEST, stages=stages)
    result = env.orch.start(channel, RUN)

    assert result.outcome is RunOutcome.COMPLETED
    calls = {name: s.calls for name, s in env.stages.items()}
    assert calls["tts"] == 3
    assert calls["research"] == calls["director"] == calls["images"] == calls["render"] == calls["qc"] == 1
    tts = result.job.stage("tts")
    assert tts.attempts == 3 and len(tts.failures) == 2
    assert env.delays == [1.0, 2.0]  # deterministic backoff, injected sleep


def test_retry_events_report_attempt_counts(tmp_path, channel):
    env = build_env(tmp_path, mode=GlobalMode.TEST,
                    stages=make_stages(tts=FakeStage("tts", fail_times=1)))
    env.orch.start(channel, RUN)
    failed = [e for e in env.sink.events if e.type is EventType.STAGE_FAILED]
    retry = [e for e in env.sink.events if e.type is EventType.STAGE_RETRY]
    assert (failed[0].stage, failed[0].attempt, failed[0].max_attempts) == ("tts", 1, 3)
    assert len(retry) == 1 and retry[0].job_id == failed[0].job_id


def test_retries_are_bounded_and_failure_info_is_preserved(tmp_path, channel):
    stages = make_stages(tts=FakeStage("tts", fail_times=99))
    env = build_env(tmp_path, mode=GlobalMode.TEST, stages=stages,
                    options=RunOptions(cleanup_on_failure=False))
    result = env.orch.start(channel, RUN)

    assert result.outcome is RunOutcome.FAILED
    assert env.stages["tts"].calls == 3  # exactly max_attempts, then stop
    assert env.stages["render"].calls == 0 and env.stages["publish"].calls == 0
    job = result.job
    assert job.status is JobStatus.FAILED and job.failed_stage == "tts"
    assert "tts" in job.status_reason
    f = job.stage("tts").failures
    assert [x.attempt for x in f] == [1, 2, 3]
    assert f[0].error_type == "RetryableStageError" and "simulated transient failure" in f[0].message
    assert f[0].retryable and f[0].detail and "Traceback" in f[0].detail
    assert statuses(job)["render"] is StageStatus.PENDING


def test_permanent_errors_are_not_retried(tmp_path, channel):
    stages = make_stages(qc=FakeStage("qc", fail_times=1, error=lambda: PermanentStageError("QC score too low")))
    env = build_env(tmp_path, mode=GlobalMode.TEST, stages=stages)
    result = env.orch.start(channel, RUN)
    assert result.outcome is RunOutcome.FAILED
    assert env.stages["qc"].calls == 1
    assert result.job.stage("qc").failures[0].retryable is False
    assert env.stages["publish"].calls == 0


def test_per_stage_retry_override(tmp_path, channel):
    retry = RetryConfig(default=RetryPolicy(max_attempts=3, initial_delay_s=0),
                        per_stage={"images": RetryPolicy(max_attempts=1)})
    env = build_env(tmp_path, mode=GlobalMode.TEST, retry=retry,
                    stages=make_stages(images=FakeStage("images", fail_times=1)))
    assert env.orch.start(channel, RUN).outcome is RunOutcome.FAILED


def test_unknown_stage_in_retry_config_is_rejected(tmp_path):
    with pytest.raises(ValueError):
        build_env(tmp_path, retry=RetryConfig(per_stage={"typo": RetryPolicy()}))


def test_invalid_director_plan_is_a_retryable_contract_violation(tmp_path, channel):
    class SloppyDirector(FakeDirectorStage):
        def build_plan(self, ctx):
            plan = super().build_plan(ctx)
            if self.calls == 1:
                plan["job_id"] = "JOB-20260101-drama-1"  # wrong job
            return plan

    env = build_env(tmp_path, mode=GlobalMode.TEST, stages=make_stages(director=SloppyDirector()))
    result = env.orch.start(channel, RUN)
    assert result.outcome is RunOutcome.COMPLETED
    first = result.job.stage(DIRECT).failures[0]
    assert first.error_type == ContractViolation.__name__ and "plan.job_id" in first.message
    assert env.stages["research"].calls == 1


def test_stage_claiming_missing_artifact_fails_cleanly(tmp_path, channel):
    class Liar(FakeStage):
        def run(self, ctx):
            self.calls += 1
            from factory.contracts.stage import StageResult
            return StageResult(artifacts=["render/never_written.mp4"])

    env = build_env(tmp_path, mode=GlobalMode.TEST, stages=make_stages(render=Liar("render")),
                    retry=RetryConfig(default=RetryPolicy(max_attempts=2, initial_delay_s=0)))
    result = env.orch.start(channel, RUN)
    assert result.outcome is RunOutcome.FAILED
    assert result.job.stage("render").failures[0].error_type == "ContractViolation"


def test_secrets_are_redacted_from_stored_failures(tmp_path, channel):
    secret = "AIzaSy-super-secret-key"
    stages = make_stages(tts=FakeStage("tts", fail_times=9,
                                       error=lambda: RuntimeError(f"401 for https://api?key={secret}")))
    env = build_env(tmp_path, mode=GlobalMode.TEST, stages=stages, redactor=SecretRedactor([secret]))
    job = env.orch.start(channel, RUN).job
    persisted = (env.state_root / "jobs" / job.job_id / "job.json").read_text()
    assert secret not in persisted and "[REDACTED]" in persisted
    assert all(secret not in e.model_dump_json() for e in env.sink.events)


# ---- kill safety and cross-run recovery ----------------------------------------------
def test_killed_run_resumes_without_repeating_completed_stages(tmp_path, channel):
    crash = make_stages(tts=FakeStage("tts", fail_times=1, error=lambda: KeyboardInterrupt()))
    env1 = build_env(tmp_path, mode=GlobalMode.TEST, stages=crash)
    with pytest.raises(KeyboardInterrupt):
        env1.orch.start(channel, RUN)

    saved = env1.store.list_jobs("drama")[0]
    assert saved.status is JobStatus.RUNNING and saved.stage("tts").status is StageStatus.RUNNING
    assert saved.stage("tts").attempts == 1  # persisted before the stage ran

    env2 = build_env(tmp_path, mode=GlobalMode.TEST)  # new process, fresh stage objects
    result = env2.orch.run_channel(channel, "1")  # run id is irrelevant: the unfinished job is resumed
    assert result.outcome is RunOutcome.COMPLETED and result.job.job_id == saved.job_id
    calls = {n: s.calls for n, s in env2.stages.items()}
    assert calls["research"] == calls["director"] == calls["images"] == 0  # not repeated
    assert calls["tts"] == 1 and calls["render"] == 1
    tts = result.job.stage("tts")
    assert tts.attempts == 2 and tts.failures[0].error_type == "Interrupted"
    assert EventType.JOB_RESUMED in env2.sink.types()


def test_repeated_kills_cannot_loop_forever(tmp_path, channel):
    retry = RetryConfig(default=RetryPolicy(max_attempts=2, initial_delay_s=0))
    for _ in range(2):
        env = build_env(tmp_path, mode=GlobalMode.TEST, retry=retry,
                        stages=make_stages(tts=FakeStage("tts", fail_times=9, error=lambda: KeyboardInterrupt())))
        with pytest.raises(KeyboardInterrupt):
            env.orch.run_channel(channel, RUN)
    env = build_env(tmp_path, mode=GlobalMode.TEST, retry=retry)
    result = env.orch.run_channel(channel, RUN)
    assert result.outcome is RunOutcome.FAILED
    assert env.stages["tts"].calls == 0  # budget already consumed by the interrupted attempts


def test_resume_after_lost_media_restarts_at_earliest_missing_stage(tmp_path, channel):
    # Run 1: render fails permanently; workspace is kept so we can simulate a lost runner.
    stages = make_stages(render=FakeStage("render", fail_times=1, error=lambda: PermanentStageError("boom")))
    env1 = build_env(tmp_path, mode=GlobalMode.TEST, stages=stages, options=RunOptions(cleanup_on_failure=False))
    failed = env1.orch.start(channel, RUN)
    assert failed.outcome is RunOutcome.FAILED
    ws = JobWorkspace(env1.workspace_root, failed.job.job_id)
    assert ws.exists()

    shutil.rmtree(env1.workspace_root)  # the runner is gone; only the StateStore survives

    env2 = build_env(tmp_path, mode=GlobalMode.TEST)
    result = env2.orch.resume(failed.job.job_id, channel)
    assert result.outcome is RunOutcome.COMPLETED
    calls = {n: s.calls for n, s in env2.stages.items()}
    # research/director outputs live in the StateStore -> untouched; media stages are redone
    assert calls["research"] == 0 and calls["director"] == 0
    assert calls["images"] == calls["tts"] == calls["render"] == calls["qc"] == 1
    invalidated = [e.stage for e in env2.sink.events if e.type is EventType.STAGE_INVALIDATED]
    assert invalidated == ["images", "tts", "render"]  # earliest missing stage + everything after it
    # failure history from run 1 is preserved even though the stage was re-run
    assert result.job.stage("render").failures[0].message == "boom"


def test_resume_with_intact_media_reruns_only_the_failed_stage(tmp_path, channel):
    stages = make_stages(render=FakeStage("render", files={"render/final.mp4": b"m"}, fail_times=1,
                                          error=lambda: PermanentStageError("boom")))
    env1 = build_env(tmp_path, mode=GlobalMode.TEST, stages=stages, options=RunOptions(cleanup_on_failure=False))
    failed = env1.orch.start(channel, RUN)

    env2 = build_env(tmp_path, mode=GlobalMode.TEST)
    result = env2.orch.resume(failed.job.job_id, channel)
    assert result.outcome is RunOutcome.COMPLETED
    calls = {n: s.calls for n, s in env2.stages.items()}
    assert calls["images"] == calls["tts"] == 0 and calls["render"] == 1
    assert result.job.stage("render").attempts == 1  # fresh budget after explicit resume


def test_tampered_artifact_is_detected_on_resume(tmp_path, channel):
    stages = make_stages(qc=FakeStage("qc", fail_times=1, error=lambda: PermanentStageError("x")))
    env1 = build_env(tmp_path, mode=GlobalMode.TEST, stages=stages, options=RunOptions(cleanup_on_failure=False))
    failed = env1.orch.start(channel, RUN)
    ws = JobWorkspace(env1.workspace_root, failed.job.job_id)
    ws.path("audio/scene_01.wav").write_bytes(b"corrupted")

    env2 = build_env(tmp_path, mode=GlobalMode.TEST)
    env2.orch.resume(failed.job.job_id, channel)
    assert env2.stages["images"].calls == 0
    assert env2.stages["tts"].calls == 1 and env2.stages["render"].calls == 1


def test_missing_stored_document_invalidates_that_stage_onward(tmp_path, channel):
    stages = make_stages(qc=FakeStage("qc", fail_times=1, error=lambda: PermanentStageError("x")))
    env1 = build_env(tmp_path, mode=GlobalMode.TEST, stages=stages, options=RunOptions(cleanup_on_failure=False))
    failed = env1.orch.start(channel, RUN)
    (env1.state_root / "jobs" / failed.job.job_id / "docs" / "director_plan.json").unlink()

    env2 = build_env(tmp_path, mode=GlobalMode.TEST)
    env2.orch.resume(failed.job.job_id, channel)
    assert env2.stages["research"].calls == 0
    assert env2.stages["director"].calls == 1  # earliest stage with missing output


def test_same_run_id_is_idempotent(tmp_path, channel):
    env = build_env(tmp_path, mode=GlobalMode.TEST)
    first = env.orch.start(channel, RUN)
    again = env.orch.start(channel, RUN)
    assert again.outcome is RunOutcome.COMPLETED and again.job.job_id == first.job.job_id
    assert env.stages["research"].calls == 1


def test_resume_is_blocked_by_policy_and_leaves_job_untouched(tmp_path, channel):
    env1 = build_env(tmp_path, mode=GlobalMode.TEST,
                     stages=make_stages(tts=FakeStage("tts", fail_times=1, error=lambda: KeyboardInterrupt())))
    with pytest.raises(KeyboardInterrupt):
        env1.orch.start(channel, RUN)
    job_id = env1.store.list_jobs()[0].job_id
    before = env1.store.load_job(job_id)

    env2 = build_env(tmp_path, mode=GlobalMode.SHUTDOWN)
    result = env2.orch.resume(job_id, channel)
    assert result.outcome is RunOutcome.BLOCKED
    assert env2.store.load_job(job_id) == before
    assert all(s.calls == 0 for s in env2.stages.values())


def test_resume_unknown_or_foreign_job(tmp_path, channel):
    env = build_env(tmp_path, mode=GlobalMode.TEST)
    with pytest.raises(StateError):
        env.orch.resume("JOB-20260101-drama-1", channel)
    job = env.orch.start(channel, RUN).job
    other = channel.model_copy(update={"channel": "finance"})
    with pytest.raises(StateError):
        env.orch.resume(job.job_id, other)


def test_skipped_publish_is_reevaluated_when_mode_changes(tmp_path, publishing_channel):
    # A job left RUNNING right after publish was skipped under TEST resumes under AUTO and publishes.
    stages = make_stages(publish=FakePublishStage())
    env1 = build_env(tmp_path, mode=GlobalMode.TEST, stages=stages,
                     options=RunOptions(cleanup_on_success=False))
    first = env1.orch.start(publishing_channel, RUN)
    job = env1.store.load_job(first.job.job_id)
    job.status = JobStatus.RUNNING  # simulate a kill just before the job was marked COMPLETED
    env1.store.save_job(job)

    env2 = build_env(tmp_path, mode=GlobalMode.AUTO)
    result = env2.orch.resume(job.job_id, publishing_channel)
    assert result.outcome is RunOutcome.COMPLETED
    assert env2.stages["publish"].calls == 1
    assert result.job.stage("publish").status is StageStatus.SUCCESS


def test_cancel_job(tmp_path, channel):
    env1 = build_env(tmp_path, mode=GlobalMode.TEST,
                     stages=make_stages(tts=FakeStage("tts", fail_times=1, error=lambda: KeyboardInterrupt())))
    with pytest.raises(KeyboardInterrupt):
        env1.orch.start(channel, RUN)
    job_id = env1.store.list_jobs()[0].job_id
    job = env1.orch.cancel_job(job_id, "channel killed")
    assert job.status is JobStatus.CANCELLED and job.status_reason == "channel killed"
    assert statuses(job)["tts"] is StageStatus.CANCELLED
    assert statuses(job)["images"] is StageStatus.SUCCESS  # finished work is not rewritten
    assert not JobWorkspace(env1.workspace_root, job_id).exists()
    assert env1.orch.resume(job_id, channel).outcome is RunOutcome.CANCELLED


# ---- cleanup ---------------------------------------------------------------------------
def test_workspace_is_deleted_after_success_and_after_terminal_failure(tmp_path, channel):
    env = build_env(tmp_path, mode=GlobalMode.TEST)
    ok = env.orch.start(channel, "1")
    assert not JobWorkspace(env.workspace_root, ok.job.job_id).exists()
    assert EventType.CLEANUP in env.sink.types()

    env2 = build_env(tmp_path / "second", mode=GlobalMode.TEST,
                     stages=make_stages(render=FakeStage("render", files={"render/x.mp4": b"m"}, fail_times=99)))
    bad = env2.orch.start(channel, "2")
    assert bad.outcome is RunOutcome.FAILED
    assert not JobWorkspace(env2.workspace_root, bad.job.job_id).exists()


def test_cleanup_never_touches_other_jobs_or_state(tmp_path, channel):
    env = build_env(tmp_path, mode=GlobalMode.TEST)
    neighbour = JobWorkspace(env.workspace_root, "JOB-20260927-finance-77").create()
    (neighbour.root / "precious.mp4").write_bytes(b"do not delete")
    (env.workspace_root / "README.txt").write_text("unrelated")

    job = env.orch.start(channel, RUN).job
    assert (neighbour.root / "precious.mp4").read_bytes() == b"do not delete"
    assert (env.workspace_root / "README.txt").exists()
    assert env.store.load_job(job.job_id) is not None  # durable state is not "temporary media"


def test_cleanup_failure_does_not_change_job_outcome(tmp_path, channel, monkeypatch):
    env = build_env(tmp_path, mode=GlobalMode.TEST)

    def explode(self):
        raise OSError("disk on fire")

    monkeypatch.setattr(JobWorkspace, "cleanup", explode)
    result = env.orch.start(channel, RUN)
    assert result.outcome is RunOutcome.COMPLETED
    assert any(e.type is EventType.CLEANUP and e.data.get("ok") is False for e in env.sink.events)


def test_keep_media_option(tmp_path, channel):
    env = build_env(tmp_path, mode=GlobalMode.TEST, options=RunOptions(cleanup_on_success=False))
    job = env.orch.start(channel, RUN).job
    assert (JobWorkspace(env.workspace_root, job.job_id).root / "render" / "final.mp4").exists()


def test_no_generated_media_ends_up_outside_the_workspace(tmp_path, channel):
    env = build_env(tmp_path, mode=GlobalMode.TEST, options=RunOptions(cleanup_on_success=False))
    env.orch.start(channel, RUN)
    media = [p for p in tmp_path.rglob("*") if p.suffix in {".mp4", ".wav", ".png"}]
    assert media and all(env.workspace_root in p.parents for p in media)


# ---- scheduling entry point ------------------------------------------------------------
def test_run_channel_starts_new_job_when_nothing_is_pending(tmp_path, channel):
    env = build_env(tmp_path, mode=GlobalMode.TEST)
    a = env.orch.run_channel(channel, "10")
    b = env.orch.run_channel(channel, "11")  # first job is COMPLETED, so a new one starts
    assert a.job.job_id.endswith("-10") and b.job.job_id.endswith("-11")


def test_run_channel_resumes_unfinished_job_instead_of_starting_another(tmp_path, channel):
    env1 = build_env(tmp_path, mode=GlobalMode.TEST,
                     stages=make_stages(tts=FakeStage("tts", fail_times=1, error=lambda: KeyboardInterrupt())))
    with pytest.raises(KeyboardInterrupt):
        env1.orch.run_channel(channel, "20")
    env2 = build_env(tmp_path, mode=GlobalMode.TEST)
    result = env2.orch.run_channel(channel, "21")
    assert result.job.job_id.endswith("-20")
    assert len(env2.store.list_jobs("drama")) == 1


def test_duplicate_stage_names_rejected(tmp_path):
    with pytest.raises(ValueError):
        build_env(tmp_path, stages={"a": FakeStage("x"), "b": FakeStage("x")})
