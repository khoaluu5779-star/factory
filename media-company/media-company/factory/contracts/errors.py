"""Exception hierarchy. Every Factory error derives from FactoryError."""

from __future__ import annotations


class FactoryError(Exception):
    """Base class for all Factory errors."""


class ConfigError(FactoryError):
    """A channel config is missing, unreadable or invalid."""


class UnsupportedSchemaVersion(FactoryError, ValueError):
    """A document declares a schema version this build does not understand."""


class StateError(FactoryError):
    """Persistent state is missing, corrupt or of an unsupported version."""


class InvalidTransition(FactoryError):
    """A state-machine transition that is not allowed."""


class PolicyViolation(FactoryError):
    """An action was attempted that the mode/policy layer does not permit."""


class WorkspaceError(FactoryError):
    """Unsafe or invalid workspace access."""


class StageError(FactoryError):
    """Base class for errors raised by stages. `retryable` drives the retry model."""

    retryable: bool = True


class RetryableStageError(StageError):
    """Transient failure (network, provider hiccup). Worth retrying."""

    retryable = True


class PermanentStageError(StageError):
    """Failure that retrying cannot fix (bad input, failed quality gate)."""

    retryable = False


class MissingInputError(PermanentStageError):
    """A stage needs an upstream output that does not exist."""


class ContractViolation(StageError):
    """A stage produced output that breaks a contract. Retryable: regeneration may fix it."""

    retryable = True
