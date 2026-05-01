"""Workflow staging for repository exports.

The runner resolves the configured source root, copies the selected files into
an isolated staging directory, applies text transforms, and returns both the
staged tree and manifest data. Destination publishers consume this output
without re-reading the source checkout.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import shutil
import time

from copybarista.config import WorkflowConfig
from copybarista.errors import ExportError
from copybarista.globs import GlobSet
from copybarista.manifest import ManifestEntry, TransformReport, file_entry
from copybarista.transforms import apply_transforms


class PhaseRecorder(Protocol):
    """Receives elapsed seconds for benchmarkable workflow phases."""

    def __call__(self, phase: str, elapsed_sec: float) -> None:
        """Record one named phase."""
        ...


@dataclass(frozen=True, slots=True, kw_only=True)
class StagedTree:
    """A transformed tree plus manifest metadata for destination publishing."""

    root: Path
    files: tuple[ManifestEntry, ...]
    transforms: tuple[TransformReport, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class WorkflowRunner:
    """Prepare the transformed staged tree for one workflow run."""

    config: WorkflowConfig
    source_ref: Path

    def stage(
        self, staging: Path, record_phase: PhaseRecorder | None = None
    ) -> StagedTree:
        """Copy selected source files to staging and apply transforms.

        Args:
          staging: Empty directory that will receive the transformed tree.
          record_phase: Optional benchmark hook for internal phase timings.

        Returns:
          staged_tree: Transformed staged tree and manifest data.

        Raises:
          ExportError: If source selection or staging fails.

        """
        source_ref = self.source_ref.resolve()
        source_root = (self.source_ref / self.config.source_root).resolve()
        if not source_root.is_relative_to(source_ref):
            raise ExportError(f"Source root escapes source checkout: {source_root}")
        if not source_root.is_dir():
            raise ExportError(f"Source root does not exist: {source_root}")
        staging.mkdir(parents=True, exist_ok=True)
        entries = _copy_selected(
            source_root=source_root,
            staging=staging,
            matcher=GlobSet(
                include=self.config.files.include,
                exclude=self.config.files.exclude,
            ),
            source_prefix=self.config.source_root,
            record_phase=record_phase,
        )
        transform_started = time.perf_counter()
        reports = apply_transforms(
            root=staging, transforms=self.config.transforms, files=entries
        )
        _record_phase(
            record_phase, "transforms", time.perf_counter() - transform_started
        )
        manifest_started = time.perf_counter()
        files = tuple(
            file_entry(
                source=entry.source,
                destination=entry.destination,
                path=staging / entry.destination,
            )
            for entry in entries
        )
        _record_phase(
            record_phase, "final_manifest", time.perf_counter() - manifest_started
        )
        return StagedTree(
            root=staging,
            files=files,
            transforms=reports,
        )


def _copy_selected(
    source_root: Path,
    staging: Path,
    matcher: GlobSet,
    source_prefix: str,
    record_phase: PhaseRecorder | None = None,
) -> tuple[ManifestEntry, ...]:
    """Copy matching source files into staging and build initial entries."""
    started = time.perf_counter()
    copy_sec = 0.0
    manifest_sec = 0.0
    entries: list[ManifestEntry] = []
    for path in sorted(source_root.rglob("*")):
        rel = path.relative_to(source_root).as_posix()
        if not matcher.matches(rel):
            continue
        if path.is_symlink():
            _validate_symlink(path=path, source_root=source_root)
        if not path.is_file() and not path.is_symlink():
            continue
        dest = staging / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        copy_started = time.perf_counter()
        shutil.copy2(path, dest, follow_symlinks=False)
        copy_sec += time.perf_counter() - copy_started
        manifest_started = time.perf_counter()
        entries.append(
            file_entry(
                source=_source_path(source_prefix=source_prefix, rel=rel),
                destination=rel,
                path=dest,
            )
        )
        manifest_sec += time.perf_counter() - manifest_started
    total_sec = time.perf_counter() - started
    _record_phase(record_phase, "select", max(total_sec - copy_sec - manifest_sec, 0))
    _record_phase(record_phase, "copy", copy_sec)
    _record_phase(record_phase, "initial_manifest", manifest_sec)
    return tuple(entries)


def _source_path(*, source_prefix: str, rel: str) -> str:
    """Return the source path recorded in manifests."""
    if not source_prefix:
        return rel
    return f"{source_prefix}/{rel}"


def _validate_symlink(path: Path, source_root: Path) -> None:
    """Reject symlinks that escape the selected source root."""
    target = path.resolve()
    if not target.is_relative_to(source_root):
        raise ExportError(f"Symlink points outside source root: {path}")


def _record_phase(
    record_phase: PhaseRecorder | None, phase: str, elapsed_sec: float
) -> None:
    """Record an optional benchmark phase without coupling staging to scripts."""
    if record_phase is not None:
        record_phase(phase, elapsed_sec)
