"""Stage contract: what a pipeline stage receives, returns and must declare."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from factory.contracts.authorization import PublishAuthorization
from factory.contracts.channel_config import ChannelConfig
from factory.contracts.director_plan import DirectorPlan, parse_director_plan
from factory.contracts.enums import DIRECTOR_PLAN_DOC
from factory.contracts.errors import MissingInputError
from factory.contracts.job import CostEntry
from factory.core.workspace import JobWorkspace


class StageResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifacts: list[str] = Field(default_factory=list)  # workspace-relative file paths
    documents: dict[str, dict[str, Any]] = Field(default_factory=dict)  # small durable JSON
    costs: list[CostEntry] = Field(default_factory=list)
    metrics: dict[str, Any] = Field(default_factory=dict)


@dataclass(frozen=True)
class StageContext:
    job_id: str
    channel: ChannelConfig
    workspace: JobWorkspace
    attempt: int
    max_attempts: int
    publish_authorization: PublishAuthorization | None
    _load_document: Callable[[str], dict[str, Any] | None] = field(repr=False)

    def document(self, name: str) -> dict[str, Any]:
        """A durable output of an earlier stage. Missing input is a permanent error."""
        data = self._load_document(name)
        if data is None:
            raise MissingInputError(f"required upstream document {name!r} is not available")
        return data

    def director_plan(self) -> DirectorPlan:
        return parse_director_plan(self.document(DIRECTOR_PLAN_DOC))


@runtime_checkable
class Stage(Protocol):
    name: str
    requires_publish_authorization: bool

    def run(self, ctx: StageContext) -> StageResult: ...
