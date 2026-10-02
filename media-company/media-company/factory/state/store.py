"""StateStore: the persistence boundary.

Implementations must be safe against partial writes. Anything that survives a runner
(jobs, small stage documents, video records) goes through this interface, so a private or
external store can replace the local one without touching the Factory Core.
"""

from __future__ import annotations

from typing import Any, Protocol, Sequence

from factory.contracts.enums import JobStatus
from factory.contracts.job import Job, VideoRecord


class StateStore(Protocol):
    def save_job(self, job: Job) -> None: ...

    def load_job(self, job_id: str) -> Job | None: ...

    def list_jobs(
        self, channel: str | None = None, statuses: Sequence[JobStatus] | None = None
    ) -> list[Job]:
        """Newest first (by created_at)."""
        ...

    def save_document(self, job_id: str, name: str, data: dict[str, Any]) -> None: ...

    def load_document(self, job_id: str, name: str) -> dict[str, Any] | None: ...

    def save_video(self, record: VideoRecord) -> None: ...

    def load_video(self, video_id: str) -> VideoRecord | None: ...
