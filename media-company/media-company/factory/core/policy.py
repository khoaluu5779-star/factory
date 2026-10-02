"""Mode/policy layer: the ONLY place that decides what is allowed.

effective permission = f(global mode, channel lifecycle state). Nothing else in the codebase
inspects modes; stages ask nothing and are simply not invoked when not permitted.

Decision table (reason: publishing is irreversible, so it needs both a global AUTO and a
channel that has left the IDEA/TEST phases):

    global SHUTDOWN                          -> no production, no publishing
    channel IDEA / PAUSE / KILL              -> no production, no publishing
    global TEST                              -> produce, never publish
    global AUTO + channel TEST               -> produce, never publish (channel not yet promoted)
    global AUTO + EVALUATING/CONTINUE/SCALE  -> produce and publish
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping

from factory.contracts.authorization import PublishAuthorization, issue_publish_authorization
from factory.contracts.enums import ChannelState, GlobalMode

MODE_ENV_VAR = "FACTORY_MODE"

_NO_PRODUCTION = frozenset({ChannelState.IDEA, ChannelState.PAUSE, ChannelState.KILL})
_PUBLISH_ALLOWED = frozenset({ChannelState.EVALUATING, ChannelState.CONTINUE, ChannelState.SCALE})


@dataclass(frozen=True)
class PolicyDecision:
    may_produce: bool
    may_publish: bool
    reason: str


def load_global_mode(environ: Mapping[str, str] | None = None) -> GlobalMode:
    """FACTORY_MODE -> GlobalMode. Missing or invalid values fail SAFE to SHUTDOWN."""
    env = os.environ if environ is None else environ
    raw = env.get(MODE_ENV_VAR, "").strip().upper()
    try:
        return GlobalMode(raw)
    except ValueError:
        return GlobalMode.SHUTDOWN


class Policy:
    def __init__(self, global_mode: GlobalMode) -> None:
        self.global_mode = global_mode

    def decide(self, channel_state: ChannelState) -> PolicyDecision:
        if self.global_mode is GlobalMode.SHUTDOWN:
            return PolicyDecision(False, False, "global mode is SHUTDOWN")
        if channel_state in _NO_PRODUCTION:
            return PolicyDecision(False, False, f"channel state is {channel_state}")
        if self.global_mode is GlobalMode.TEST:
            return PolicyDecision(True, False, "global mode is TEST: publishing disabled")
        if channel_state in _PUBLISH_ALLOWED:
            return PolicyDecision(True, True, "AUTO mode and channel cleared for publishing")
        return PolicyDecision(True, False, f"channel state {channel_state} may not publish yet")

    def authorize_publish(
        self, job_id: str, channel: str, channel_state: ChannelState
    ) -> PublishAuthorization | None:
        """Returns a sealed authorization, or None when publishing is not permitted."""
        if not self.decide(channel_state).may_publish:
            return None
        return issue_publish_authorization(job_id, channel)
