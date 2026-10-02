from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from factory.contracts.channel_config import ChannelConfig, load_channel
from factory.contracts.enums import ChannelState, GlobalMode, StageName
from factory.core.events import InMemorySink
from factory.core.orchestrator import Orchestrator, RunOptions
from factory.core.policy import Policy
from factory.core.redact import SecretRedactor
from factory.core.retry import RetryConfig, RetryPolicy
from factory.stages.fakes import FakeDirectorStage, FakePublishStage, FakeStage
from factory.state.file_store import FileStateStore

REPO_ROOT = Path(__file__).resolve().parent.parent


class FakeClock:
    """Deterministic clock that advances one second per reading."""

    def __init__(self) -> None:
        self.now = datetime(2026, 9, 27, 12, 0, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


def make_stages(**overrides) -> dict[str, object]:
    """The default 7-stage fake pipeline keyed by stage name; overrides replace stages."""
    stages: dict[str, object] = {
        StageName.RESEARCH.value: FakeStage("research", documents={"research": {"topic": "x"}}),
        StageName.DIRECTOR.value: FakeDirectorStage(),
        StageName.IMAGES.value: FakeStage("images", files={"images/scene_01.png": b"img"}),
        StageName.TTS.value: FakeStage("tts", files={"audio/scene_01.wav": b"wav"}),
        StageName.RENDER.value: FakeStage("render", files={"render/final.mp4": b"mp4"}),
        StageName.QC.value: FakeStage("qc", documents={"qc_report": {"verdict": "PASS"}}),
        StageName.PUBLISH.value: FakePublishStage(),
    }
    stages.update(overrides)
    return stages


@dataclass
class Env:
    orch: Orchestrator
    store: FileStateStore
    sink: InMemorySink
    stages: dict
    workspace_root: Path
    state_root: Path
    delays: list[float] = field(default_factory=list)


def build_env(
    tmp_path: Path,
    *,
    mode: GlobalMode = GlobalMode.AUTO,
    stages: dict | None = None,
    retry: RetryConfig | None = None,
    options: RunOptions | None = None,
    redactor: SecretRedactor | None = None,
) -> Env:
    stages = stages if stages is not None else make_stages()
    store = FileStateStore(tmp_path / "state")
    sink = InMemorySink()
    delays: list[float] = []
    orch = Orchestrator(
        pipeline=list(stages.values()),
        store=store,
        workspace_root=tmp_path / "workspace",
        policy=Policy(mode),
        events=sink,
        retry=retry or RetryConfig(default=RetryPolicy(max_attempts=3, initial_delay_s=1)),
        options=options,
        redactor=redactor or SecretRedactor(),
        clock=FakeClock(),
        sleep=delays.append,
    )
    return Env(orch, store, sink, stages, tmp_path / "workspace", tmp_path / "state", delays)


@pytest.fixture
def channel() -> ChannelConfig:
    return load_channel(REPO_ROOT / "channels", "drama")


@pytest.fixture
def publishing_channel(channel: ChannelConfig) -> ChannelConfig:
    """Drama promoted to a lifecycle state that is allowed to publish."""
    return channel.model_copy(update={"lifecycle_state": ChannelState.EVALUATING})
