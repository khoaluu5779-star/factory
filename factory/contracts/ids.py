"""Identifier formats. Job IDs are derived from the GitHub Actions run ID."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Mapping

CHANNEL_ID_PATTERN = r"^[a-z][a-z0-9_]{0,31}$"
JOB_ID_PATTERN = r"^JOB-\d{8}-[a-z][a-z0-9_]{0,31}-\d+$"
IDENT_PATTERN = r"^[a-z][a-z0-9_]{0,63}$"  # stage names, document names, voice roles, ...

_CHANNEL_RE = re.compile(CHANNEL_ID_PATTERN)
_JOB_RE = re.compile(r"^JOB-(\d{8})-([a-z][a-z0-9_]{0,31})-(\d+)$")


def is_valid_channel_id(value: str) -> bool:
    return bool(_CHANNEL_RE.match(value))


def make_job_id(channel: str, run_id: str | int, when: datetime) -> str:
    """JOB-YYYYMMDD-<channel>-<actions_run_id>. The date is taken in UTC."""
    if not is_valid_channel_id(channel):
        raise ValueError(f"invalid channel id: {channel!r}")
    run = str(run_id)
    if not run.isdigit():
        raise ValueError(f"run id must be numeric, got {run!r}")
    stamp = when.astimezone(timezone.utc).strftime("%Y%m%d")
    return f"JOB-{stamp}-{channel}-{run}"


def parse_job_id(job_id: str) -> tuple[str, str, str]:
    """Return (yyyymmdd, channel, run_id) or raise ValueError."""
    m = _JOB_RE.match(job_id)
    if not m:
        raise ValueError(f"invalid job id: {job_id!r}")
    return m.group(1), m.group(2), m.group(3)


def resolve_run_id(environ: Mapping[str, str], now: datetime) -> str:
    """GITHUB_RUN_ID on Actions; a millisecond timestamp for local runs."""
    run = environ.get("GITHUB_RUN_ID", "").strip()
    if run:
        if not run.isdigit():
            raise ValueError("GITHUB_RUN_ID must be numeric")
        return run
    return str(int(now.timestamp() * 1000))
