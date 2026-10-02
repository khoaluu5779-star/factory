"""Fake stages for tests and smoke runs. They touch no network and cost nothing.

NOT production code: real Gemini/Pollinations/Edge TTS/FFmpeg/YouTube stages will implement the
same `Stage` protocol and replace these in the pipeline builder.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

from factory.contracts.channel_config import NARRATOR_ROLE
from factory.contracts.enums import DIRECTOR_PLAN_DOC, PUBLICATION_DOC, StageName
from factory.contracts.errors import PermanentStageError, RetryableStageError
from factory.contracts.job import CostEntry
from factory.contracts.stage import Stage, StageContext, StageResult


class FakeStage:
    """Configurable stage. `fail_times` transient failures happen before it succeeds."""

    requires_publish_authorization = False

    def __init__(
        self,
        name: str,
        *,
        files: dict[str, bytes] | None = None,
        documents: dict[str, dict] | None = None,
        costs: list[CostEntry] | None = None,
        fail_times: int = 0,
        error: Callable[[], Exception] | None = None,
    ) -> None:
        self.name = name
        self.files = files or {}
        self.documents = documents or {}
        self.costs = costs or []
        self.fail_times = fail_times
        self.error = error or (lambda: RetryableStageError(f"{name}: simulated transient failure"))
        self.calls = 0

    def run(self, ctx: StageContext) -> StageResult:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise self.error()
        for rel, content in self.files.items():
            target = ctx.workspace.path(rel)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        return StageResult(
            artifacts=list(self.files),
            documents={k: dict(v) for k, v in self.documents.items()},
            costs=list(self.costs),
        )


class FakeDirectorStage(FakeStage):
    """Builds a valid DirectorPlan for the running job from the channel config."""

    def __init__(self, **kw) -> None:
        super().__init__(StageName.DIRECTOR.value, **kw)

    def build_plan(self, ctx: StageContext) -> dict:
        ch = ctx.channel
        duration = ch.target_duration_s.min_s
        return {
            "schema_version": "1.0",
            "job_id": ctx.job_id,
            "channel": ch.channel,
            "title": f"{ch.display_name} test story",
            "format": ch.default_format,
            "target_duration_s": duration,
            "concept": {
                "logline": "A placeholder story used by the fake Director.",
                "story_format": "twist",
                "narrative_structure": ["hook", "conflict", "reversal"],
            },
            "hook": {"text": "Nobody expected the letter.", "hook_type": "curiosity_gap"},
            "characters": [],
            "scenes": [
                {
                    "index": 1,
                    "visual_intent": "a sealed letter on a table",
                    "image_prompt": "sealed letter on a wooden table, dim light",
                    "lines": [{"kind": "narration", "voice_role": NARRATOR_ROLE, "text": "It arrived at dawn."}],
                }
            ],
            "ending": {"type": "twist"},
            "experiment": {
                "experiment_id": f"{ch.experiment_prefix}-0001",
                "format": ch.default_format,
                "hook_type": "curiosity_gap",
                "story_format": "twist",
                "duration_target_s": duration,
                "visual_style": ch.visual.style,
            },
        }

    def run(self, ctx: StageContext) -> StageResult:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise self.error()
        return StageResult(
            documents={DIRECTOR_PLAN_DOC: self.build_plan(ctx)},
            costs=[CostEntry(provider="gemini", amount_usd=0.0, units=1, unit_label="request")],
        )


class FakePublishStage(FakeStage):
    """Would 'publish'. Counts calls so tests can prove it never runs when not authorized."""

    requires_publish_authorization = True

    def __init__(self) -> None:
        super().__init__(StageName.PUBLISH.value)

    def run(self, ctx: StageContext) -> StageResult:
        self.calls += 1
        if ctx.publish_authorization is None:  # defense in depth; the orchestrator already guards
            raise PermanentStageError("publish attempted without authorization")
        return StageResult(
            documents={
                PUBLICATION_DOC: {
                    "video_id": f"fake-{ctx.job_id[-12:]}",
                    "published_at": datetime.now(timezone.utc).isoformat(),
                }
            }
        )


def build_fake_pipeline() -> list[Stage]:
    """The default seven-stage pipeline wired with fakes."""
    return [
        FakeStage(StageName.RESEARCH.value, documents={"research": {"topic": "placeholder"}}),
        FakeDirectorStage(),
        FakeStage(StageName.IMAGES.value, files={"images/scene_01.png": b"fake-image"}),
        FakeStage(StageName.TTS.value, files={"audio/scene_01.wav": b"fake-audio"}),
        FakeStage(StageName.RENDER.value, files={"render/final.mp4": b"fake-video"}),
        FakeStage(StageName.QC.value, documents={"qc_report": {"verdict": "PASS"}}),
        FakePublishStage(),
    ]
