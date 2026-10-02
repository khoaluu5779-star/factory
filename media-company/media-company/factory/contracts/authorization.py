"""PublishAuthorization: proof that the policy layer approved publishing for one job.

Only `factory.core.policy` may call `issue_publish_authorization`; a repository test
enforces that. Constructing the object directly raises, so a stage cannot fabricate one.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from factory.contracts.errors import PolicyViolation

_SEAL = object()


@dataclass(frozen=True)
class PublishAuthorization:
    job_id: str
    channel: str
    _seal: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._seal is not _SEAL:
            raise PolicyViolation("PublishAuthorization can only be issued by the policy layer")


def issue_publish_authorization(job_id: str, channel: str) -> PublishAuthorization:
    """POLICY LAYER ONLY."""
    return PublishAuthorization(job_id=job_id, channel=channel, _seal=_SEAL)
