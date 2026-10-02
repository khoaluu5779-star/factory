"""Bounded, deterministic retry policy. Retries are per stage, never per pipeline."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class RetryPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    max_attempts: int = Field(default=3, ge=1, le=10)  # hard upper bound: no infinite loops
    initial_delay_s: float = Field(default=2.0, ge=0)
    backoff_factor: float = Field(default=2.0, ge=1)
    max_delay_s: float = Field(default=60.0, ge=0)

    def delay_after(self, failed_attempt: int) -> float:
        """Seconds to wait after the given (1-based) failed attempt."""
        return min(self.max_delay_s, self.initial_delay_s * self.backoff_factor ** (failed_attempt - 1))


class RetryConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    default: RetryPolicy = Field(default_factory=RetryPolicy)
    per_stage: dict[str, RetryPolicy] = Field(default_factory=dict)

    def for_stage(self, name: str) -> RetryPolicy:
        return self.per_stage.get(name, self.default)
