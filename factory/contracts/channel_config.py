"""ChannelConfig: everything channel-specific, as validated data (never executable logic)."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from factory.contracts.enums import ChannelState
from factory.contracts.errors import ConfigError
from factory.contracts.ids import CHANNEL_ID_PATTERN, IDENT_PATTERN, is_valid_channel_id

SUPPORTED_CONFIG_VERSIONS = ("1.0",)
ENV_VAR_PATTERN = r"^[A-Z][A-Z0-9_]{2,63}$"
NARRATOR_ROLE = "narrator"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Dna(_Strict):
    """Content DNA handed to the Director as creative guidance."""

    tone: str = Field(min_length=1)
    themes: list[str] = Field(default_factory=list)
    creative_constraints: list[str] = Field(default_factory=list)
    forbidden: list[str] = Field(default_factory=list)
    director_notes: str | None = None


class VisualStyle(_Strict):
    style: str = Field(min_length=1)
    image_prompt_suffix: str | None = None


class VoiceSpec(_Strict):
    """A voice role -> provider voice mapping. Provider names stay opaque strings here."""

    voice: str = Field(min_length=1)
    rate: str | None = None
    pitch: str | None = None


class DurationRange(_Strict):
    min_s: float = Field(gt=0)
    max_s: float = Field(gt=0)

    @model_validator(mode="after")
    def _ordered(self) -> "DurationRange":
        if self.min_s > self.max_s:
            raise ValueError("min_s must be <= max_s")
        return self


class CtaPolicy(_Strict):
    style: Literal["none", "soft", "explicit"] = "none"
    default_text: str | None = None


class PublishingDefaults(_Strict):
    category_id: str | None = None
    default_tags: list[str] = Field(default_factory=list)
    contains_synthetic_media: bool = True
    made_for_kids: bool = False


class ChannelConfig(_Strict):
    config_version: Literal["1.0"]
    channel: str = Field(pattern=CHANNEL_ID_PATTERN)
    display_name: str = Field(min_length=1)
    lifecycle_state: ChannelState
    niche: str = Field(min_length=1)
    experiment_prefix: str = Field(pattern=r"^[A-Z]{2,6}$")
    dna: Dna
    visual: VisualStyle
    allowed_formats: list[str] = Field(min_length=1)
    default_format: str
    target_duration_s: DurationRange
    voices: dict[str, VoiceSpec]
    cta: CtaPolicy = Field(default_factory=CtaPolicy)
    publishing: PublishingDefaults = Field(default_factory=PublishingDefaults)
    # Logical secret name -> ENVIRONMENT VARIABLE NAME. Values are never stored here.
    secret_env: dict[str, str] = Field(default_factory=dict)

    @field_validator("allowed_formats")
    @classmethod
    def _formats_unique_idents(cls, v: list[str]) -> list[str]:
        import re

        for item in v:
            if not re.match(IDENT_PATTERN, item):
                raise ValueError(f"invalid format name: {item!r}")
        if len(set(v)) != len(v):
            raise ValueError("allowed_formats contains duplicates")
        return v

    @field_validator("voices")
    @classmethod
    def _voice_roles(cls, v: dict[str, VoiceSpec]) -> dict[str, VoiceSpec]:
        import re

        for role in v:
            if not re.match(IDENT_PATTERN, role):
                raise ValueError(f"invalid voice role: {role!r}")
        if NARRATOR_ROLE not in v:
            raise ValueError(f"voices must define the {NARRATOR_ROLE!r} role")
        return v

    @field_validator("secret_env")
    @classmethod
    def _secret_env_names_only(cls, v: dict[str, str]) -> dict[str, str]:
        import re

        for logical, env_name in v.items():
            if not re.match(ENV_VAR_PATTERN, env_name):
                raise ValueError(
                    f"secret_env[{logical!r}] must be an environment variable NAME "
                    "(UPPER_SNAKE_CASE), never a secret value"
                )
        return v

    @model_validator(mode="after")
    def _default_format_allowed(self) -> "ChannelConfig":
        if self.default_format not in self.allowed_formats:
            raise ValueError("default_format must be one of allowed_formats")
        return self


def load_channel_config(path: Path) -> ChannelConfig:
    """Load and validate one config file. All failures surface as ConfigError."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read channel config {path}: {exc}") from exc
    try:
        raw = yaml.safe_load(text)  # safe_load: config is data, never code
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: top level must be a mapping")
    version = raw.get("config_version")
    if version not in SUPPORTED_CONFIG_VERSIONS:
        raise ConfigError(
            f"{path}: unsupported config_version {version!r}; supported: {SUPPORTED_CONFIG_VERSIONS}"
        )
    try:
        return ChannelConfig.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(f"{path}: invalid channel config:\n{exc}") from exc


def load_channel(channels_dir: Path, channel: str) -> ChannelConfig:
    """Load channels/<channel>/config.yaml and check the directory matches the declared id."""
    if not is_valid_channel_id(channel):
        raise ConfigError(f"invalid channel id: {channel!r}")
    config = load_channel_config(Path(channels_dir) / channel / "config.yaml")
    if config.channel != channel:
        raise ConfigError(
            f"channels/{channel}/config.yaml declares channel {config.channel!r}; must match directory"
        )
    return config


def list_channels(channels_dir: Path) -> list[str]:
    root = Path(channels_dir)
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if (p / "config.yaml").is_file())
