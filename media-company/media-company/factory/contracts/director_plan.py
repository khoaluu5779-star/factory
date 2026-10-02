"""DirectorPlan: the versioned contract between the Gemini Director and the Factory.

Design notes
- Characters are described once, centrally. Scenes reference them by id and carry only a
  short, scene-relevant image prompt.
- Voice roles are abstract ("narrator", "protagonist"); ChannelConfig.voices maps roles to
  provider voices, so the plan never names a TTS voice.
- Strict (extra="forbid"): an unexpected key is a contract violation, not silently ignored.
"""

from __future__ import annotations

from typing import Any, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from factory.contracts.channel_config import ChannelConfig
from factory.contracts.errors import ContractViolation, UnsupportedSchemaVersion
from factory.contracts.ids import CHANNEL_ID_PATTERN, IDENT_PATTERN, JOB_ID_PATTERN

SchemaVersion = Literal["1.0"]
SUPPORTED_SCHEMA_VERSIONS: tuple[str, ...] = get_args(SchemaVersion)
MAX_IMAGE_PROMPT_CHARS = 600


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Concept(_Strict):
    logline: str = Field(min_length=1)
    story_format: str = Field(min_length=1)
    narrative_structure: list[str] = Field(min_length=1)


class Hook(_Strict):
    text: str = Field(min_length=1)
    hook_type: str = Field(min_length=1)


class Character(_Strict):
    id: str = Field(pattern=IDENT_PATTERN)
    name: str = Field(min_length=1)
    description: str = Field(min_length=1)  # the one detailed "bible" entry
    voice_role: str = Field(pattern=IDENT_PATTERN)


class Line(_Strict):
    kind: Literal["narration", "dialogue"]
    voice_role: str = Field(pattern=IDENT_PATTERN)
    text: str = Field(min_length=1)


class Scene(_Strict):
    index: int = Field(ge=1)
    visual_intent: str = Field(min_length=1)
    image_prompt: str = Field(min_length=1, max_length=MAX_IMAGE_PROMPT_CHARS)
    character_ids: list[str] = Field(default_factory=list)
    lines: list[Line] = Field(min_length=1)
    duration_hint_s: float | None = Field(default=None, gt=0)
    sound_intent: str | None = None
    vfx_intent: str | None = None


class Ending(_Strict):
    type: str = Field(min_length=1)  # e.g. "twist", "cliffhanger", "resolution"
    summary: str | None = None


class Cta(_Strict):
    type: Literal["none", "soft", "explicit"] = "none"
    text: str | None = None


class PublishingMetadata(_Strict):
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    category_id: str | None = None
    contains_synthetic_media: bool = True
    made_for_kids: bool = False


class ExperimentPlan(_Strict):
    experiment_id: str = Field(min_length=1)
    format: str = Field(min_length=1)
    hook_type: str = Field(min_length=1)
    story_format: str = Field(min_length=1)
    duration_target_s: float = Field(gt=0)
    visual_style: str = Field(min_length=1)


class DirectorPlan(_Strict):
    schema_version: SchemaVersion
    job_id: str = Field(pattern=JOB_ID_PATTERN)
    channel: str = Field(pattern=CHANNEL_ID_PATTERN)
    title: str = Field(min_length=1, max_length=100)
    format: str = Field(min_length=1)
    target_duration_s: float = Field(gt=0)
    concept: Concept
    hook: Hook
    characters: list[Character] = Field(default_factory=list)
    scenes: list[Scene] = Field(min_length=1)
    ending: Ending
    cta: Cta = Field(default_factory=Cta)
    publishing: PublishingMetadata = Field(default_factory=PublishingMetadata)
    experiment: ExperimentPlan

    @model_validator(mode="after")
    def _internal_consistency(self) -> "DirectorPlan":
        ids = [c.id for c in self.characters]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate character ids")
        known = set(ids)
        expected = list(range(1, len(self.scenes) + 1))
        if [s.index for s in self.scenes] != expected:
            raise ValueError("scene indexes must be contiguous, ordered and start at 1")
        for scene in self.scenes:
            missing = [c for c in scene.character_ids if c not in known]
            if missing:
                raise ValueError(f"scene {scene.index} references unknown characters: {missing}")
        if self.experiment.format != self.format:
            raise ValueError("experiment.format must equal format")
        if self.experiment.duration_target_s != self.target_duration_s:
            raise ValueError("experiment.duration_target_s must equal target_duration_s")
        return self

    def voice_roles_used(self) -> set[str]:
        roles = {c.voice_role for c in self.characters}
        for scene in self.scenes:
            roles.update(line.voice_role for line in scene.lines)
        return roles


def parse_director_plan(data: Any) -> DirectorPlan:
    """Validate a raw document. Checks the schema version FIRST for a clear error."""
    if not isinstance(data, dict):
        raise ContractViolation("DirectorPlan must be a JSON object")
    version = data.get("schema_version")
    if version not in SUPPORTED_SCHEMA_VERSIONS:
        raise UnsupportedSchemaVersion(
            f"unsupported DirectorPlan schema_version {version!r}; supported: {SUPPORTED_SCHEMA_VERSIONS}"
        )
    try:
        return DirectorPlan.model_validate(data)
    except ValidationError as exc:
        raise ContractViolation(f"invalid DirectorPlan: {exc}") from exc


def check_plan_against_channel(plan: DirectorPlan, channel: ChannelConfig) -> None:
    """Cross-contract checks that need both the plan and the channel config."""
    problems: list[str] = []
    if plan.channel != channel.channel:
        problems.append(f"plan.channel {plan.channel!r} != channel {channel.channel!r}")
    if plan.format not in channel.allowed_formats:
        problems.append(f"format {plan.format!r} not in allowed_formats {channel.allowed_formats}")
    span = channel.target_duration_s
    if not (span.min_s <= plan.target_duration_s <= span.max_s):
        problems.append(
            f"target_duration_s {plan.target_duration_s} outside [{span.min_s}, {span.max_s}]"
        )
    unknown = plan.voice_roles_used() - set(channel.voices)
    if unknown:
        problems.append(f"voice roles not defined by the channel: {sorted(unknown)}")
    if not plan.experiment.experiment_id.startswith(channel.experiment_prefix + "-"):
        problems.append(
            f"experiment_id {plan.experiment.experiment_id!r} must start with "
            f"{channel.experiment_prefix + '-'!r}"
        )
    if problems:
        raise ContractViolation("DirectorPlan violates channel config: " + "; ".join(problems))


def director_plan_json_schema() -> dict[str, Any]:
    """The JSON Schema published for the Director prompt and for external validation."""
    schema = DirectorPlan.model_json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["title"] = "DirectorPlan"
    return schema
