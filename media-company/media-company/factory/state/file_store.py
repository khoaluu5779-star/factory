"""Local JSON-file StateStore for development and tests.

NOT durable on GitHub-hosted runners (the disk vanishes with the runner). Cross-run
recovery in production needs a private/external StateStore implementation.

Layout:  <root>/jobs/<job_id>/job.json
         <root>/jobs/<job_id>/docs/<name>.json
         <root>/videos/<video_id>.json
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Sequence

from pydantic import ValidationError

from factory.contracts.enums import JobStatus
from factory.contracts.errors import StateError
from factory.contracts.ids import IDENT_PATTERN, JOB_ID_PATTERN
from factory.contracts.job import Job, VideoRecord, check_state_version

_JOB_RE = re.compile(JOB_ID_PATTERN)
_DOC_RE = re.compile(IDENT_PATTERN)
_VIDEO_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _atomic_write(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)  # atomic on POSIX: readers see the old or the new file, never half
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class FileStateStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    # -- jobs -----------------------------------------------------------------
    def _job_dir(self, job_id: str) -> Path:
        if not _JOB_RE.match(job_id):
            raise StateError(f"invalid job id: {job_id!r}")
        return self.root / "jobs" / job_id

    def save_job(self, job: Job) -> None:
        _atomic_write(self._job_dir(job.job_id) / "job.json", job.model_dump_json(indent=2))

    def load_job(self, job_id: str) -> Job | None:
        path = self._job_dir(job_id) / "job.json"
        if not path.is_file():
            return None
        return self._read_job(path)

    @staticmethod
    def _read_job(path: Path) -> Job:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            check_state_version(str(raw.get("state_schema_version")), f"job file {path.name}")
            return Job.model_validate(raw)
        except (OSError, json.JSONDecodeError, ValidationError) as exc:
            raise StateError(f"corrupt job file {path}: {exc}") from exc

    def list_jobs(
        self, channel: str | None = None, statuses: Sequence[JobStatus] | None = None
    ) -> list[Job]:
        jobs_dir = self.root / "jobs"
        if not jobs_dir.is_dir():
            return []
        found: list[Job] = []
        for path in jobs_dir.glob("*/job.json"):
            job = self._read_job(path)
            if channel is not None and job.channel != channel:
                continue
            if statuses is not None and job.status not in statuses:
                continue
            found.append(job)
        found.sort(key=lambda j: (j.created_at, j.job_id), reverse=True)
        return found

    # -- documents ------------------------------------------------------------
    def _doc_path(self, job_id: str, name: str) -> Path:
        if not _DOC_RE.match(name):
            raise StateError(f"invalid document name: {name!r}")
        return self._job_dir(job_id) / "docs" / f"{name}.json"

    def save_document(self, job_id: str, name: str, data: dict[str, Any]) -> None:
        _atomic_write(self._doc_path(job_id, name), json.dumps(data, indent=2, sort_keys=True))

    def load_document(self, job_id: str, name: str) -> dict[str, Any] | None:
        path = self._doc_path(job_id, name)
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise StateError(f"corrupt document {path}: {exc}") from exc
        if not isinstance(data, dict):
            raise StateError(f"document {path} is not a JSON object")
        return data

    # -- videos ---------------------------------------------------------------
    def _video_path(self, video_id: str) -> Path:
        if not _VIDEO_RE.match(video_id):
            raise StateError(f"invalid video id: {video_id!r}")
        return self.root / "videos" / f"{video_id}.json"

    def save_video(self, record: VideoRecord) -> None:
        _atomic_write(self._video_path(record.video_id), record.model_dump_json(indent=2))

    def load_video(self, video_id: str) -> VideoRecord | None:
        path = self._video_path(video_id)
        if not path.is_file():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            check_state_version(str(raw.get("state_schema_version")), f"video file {path.name}")
            return VideoRecord.model_validate(raw)
        except (OSError, json.JSONDecodeError, ValidationError) as exc:
            raise StateError(f"corrupt video file {path}: {exc}") from exc
