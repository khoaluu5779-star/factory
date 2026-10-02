"""Structured events. Sinks decide what to show; Discord will be one filtering sink."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, Field


class EventType(StrEnum):
    JOB_STARTED = "JOB_STARTED"
    JOB_RESUMED = "JOB_RESUMED"
    JOB_COMPLETED = "JOB_COMPLETED"
    JOB_FAILED = "JOB_FAILED"
    JOB_CANCELLED = "JOB_CANCELLED"
    JOB_BLOCKED = "JOB_BLOCKED"
    STAGE_STARTED = "STAGE_STARTED"
    STAGE_SUCCEEDED = "STAGE_SUCCEEDED"
    STAGE_FAILED = "STAGE_FAILED"
    STAGE_RETRY = "STAGE_RETRY"
    STAGE_SKIPPED = "STAGE_SKIPPED"
    STAGE_INVALIDATED = "STAGE_INVALIDATED"
    CLEANUP = "CLEANUP"


class Event(BaseModel):
    ts: datetime
    type: EventType
    channel: str
    job_id: str | None = None
    stage: str | None = None
    attempt: int | None = None
    max_attempts: int | None = None
    message: str = ""
    data: dict[str, Any] = Field(default_factory=dict)


class EventSink(Protocol):
    def emit(self, event: Event) -> None: ...


class InMemorySink:
    def __init__(self) -> None:
        self.events: list[Event] = []

    def emit(self, event: Event) -> None:
        self.events.append(event)

    def types(self) -> list[EventType]:
        return [e.type for e in self.events]


class LoggingSink:
    """One JSON line per event through the standard logging module."""

    def __init__(self, logger: logging.Logger | None = None) -> None:
        self._log = logger or logging.getLogger("factory.events")

    def emit(self, event: Event) -> None:
        self._log.info(event.model_dump_json(exclude_none=True))


class MultiSink:
    """Fans out to several sinks; one failing sink never breaks the pipeline."""

    def __init__(self, *sinks: EventSink) -> None:
        self._sinks = sinks

    def emit(self, event: Event) -> None:
        for sink in self._sinks:
            try:
                sink.emit(event)
            except Exception:  # noqa: BLE001 - observability must not take the factory down
                logging.getLogger("factory.events").exception("event sink failed")


def to_json_line(event: Event) -> str:
    return json.dumps(event.model_dump(mode="json", exclude_none=True), sort_keys=True)
