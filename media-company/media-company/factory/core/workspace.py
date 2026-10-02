"""Temporary per-job workspace. Generated media lives here and nowhere else."""

from __future__ import annotations

import hashlib
import re
import shutil
from pathlib import Path

from factory.contracts.errors import WorkspaceError
from factory.contracts.ids import JOB_ID_PATTERN
from factory.contracts.job import ArtifactRef

_JOB_RE = re.compile(JOB_ID_PATTERN)
_CHUNK = 1024 * 1024


class JobWorkspace:
    """<root>/jobs/<job_id>/... All paths are confined to this job's directory."""

    def __init__(self, root: Path, job_id: str) -> None:
        if not _JOB_RE.match(job_id):
            raise WorkspaceError(f"invalid job id for workspace: {job_id!r}")
        self.job_id = job_id
        self._jobs_root = Path(root).resolve() / "jobs"
        self.root = self._jobs_root / job_id

    def create(self) -> "JobWorkspace":
        self.root.mkdir(parents=True, exist_ok=True)
        return self

    def exists(self) -> bool:
        return self.root.is_dir()

    def path(self, relative: str) -> Path:
        """Resolve a workspace-relative path; reject absolute paths, '..' and symlink escapes."""
        rel = Path(relative)
        if rel.is_absolute() or ".." in rel.parts or not rel.parts:
            raise WorkspaceError(f"unsafe workspace path: {relative!r}")
        full = (self.root / rel).resolve()
        if not full.is_relative_to(self.root.resolve()):
            raise WorkspaceError(f"path escapes workspace: {relative!r}")
        return full

    def subdir(self, name: str) -> Path:
        d = self.path(name)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def describe(self, relative: str) -> ArtifactRef:
        """Build a manifest entry for an existing file."""
        p = self.path(relative)
        if not p.is_file():
            raise WorkspaceError(f"artifact missing: {relative!r}")
        h = hashlib.sha256()
        with p.open("rb") as fh:
            while chunk := fh.read(_CHUNK):
                h.update(chunk)
        return ArtifactRef(path=Path(relative).as_posix(), size_bytes=p.stat().st_size, sha256=h.hexdigest())

    def verify(self, ref: ArtifactRef) -> bool:
        """True only if the file still exists and matches the recorded size and hash."""
        try:
            current = self.describe(ref.path)
        except WorkspaceError:
            return False
        return current.size_bytes == ref.size_bytes and current.sha256 == ref.sha256

    def cleanup(self) -> bool:
        """Delete THIS job's directory only. Returns True if something was removed."""
        target = self.root
        if not target.exists() and not target.is_symlink():
            return False
        if target.is_symlink():
            raise WorkspaceError(f"refusing to delete symlinked workspace: {target}")
        resolved = target.resolve()
        if resolved.parent != self._jobs_root.resolve() or resolved.name != self.job_id:
            raise WorkspaceError(f"refusing to delete unexpected path: {resolved}")
        shutil.rmtree(resolved)
        return True
