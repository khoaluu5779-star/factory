"""Closed vocabularies used across the Factory (no magic strings)."""

from __future__ import annotations

from enum import StrEnum


class StageStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    CANCELLED = "CANCELLED"


class JobStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"  # every stage SUCCESS or SKIPPED (a TEST job ends here too)
    FAILED = "FAILED"  # a stage exhausted its retry budget or failed permanently
    CANCELLED = "CANCELLED"  # explicitly cancelled; terminal


class GlobalMode(StrEnum):
    SHUTDOWN = "SHUTDOWN"
    TEST = "TEST"
    AUTO = "AUTO"


class ChannelState(StrEnum):
    """Channel lifecycle. Declared in the channel config; changing it needs no code change."""

    IDEA = "IDEA"
    TEST = "TEST"
    EVALUATING = "EVALUATING"
    CONTINUE = "CONTINUE"
    SCALE = "SCALE"
    PAUSE = "PAUSE"
    KILL = "KILL"


class StageName(StrEnum):
    """Canonical stage names of the default pipeline. Stage names elsewhere are plain str."""

    RESEARCH = "research"
    DIRECTOR = "director"
    IMAGES = "images"
    TTS = "tts"
    RENDER = "render"
    QC = "qc"
    PUBLISH = "publish"


class RunOutcome(StrEnum):
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    BLOCKED = "BLOCKED"  # policy refused to start or resume; no work was done


# Names of small JSON documents that the core itself understands.
DIRECTOR_PLAN_DOC = "director_plan"
PUBLICATION_DOC = "publication"
