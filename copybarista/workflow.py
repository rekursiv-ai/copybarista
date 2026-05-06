"""Workflow staging for repository exports.

The runner resolves the configured source root, copies the selected files into
an isolated staging directory, applies text transforms, and returns both the
staged tree and manifest data. Destination publishers consume this output
without re-reading the source checkout.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol

import shutil
import time

from copybarista.config import FileWrite, Transform, WorkflowConfig
from copybarista.errors import ExportError
from copybarista.globs import GlobSet
from copybarista.leak_check import enforce_leak_check
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
        entries = [
            *_copy_selected(
                source_root=source_root,
                staging=staging,
                matcher=GlobSet(
                    include=self.config.files.include,
                    exclude=self.config.files.exclude,
                    globstar=self.config.globstar,
                ),
                prefixer=DestinationPrefixer.from_config(self.config),
                source_prefix=self.config.source_root,
                record_phase=record_phase,
            ),
        ]
        for file_copy in self.config.files.copy:
            entries.extend(
                _copy_additional(
                    source_ref=source_ref,
                    staging=staging,
                    source=file_copy.source,
                    destination=file_copy.destination,
                    matcher=GlobSet(
                        include=file_copy.include,
                        exclude=file_copy.exclude,
                        globstar=self.config.globstar,
                    ),
                    record_phase=record_phase,
                )
            )
        entries.extend(
            _write_generated(staging=staging, file_write=file_write)
            for file_write in self.config.files.write
        )
        entries_tuple = tuple(entries)
        transform_started = time.perf_counter()
        reports = apply_transforms(
            root=staging,
            transforms=self.config.transforms,
            files=entries_tuple,
            globstar=self.config.globstar,
        )
        entries_tuple = _apply_transform_destinations(
            entries_tuple,
            config=self.config,
        )
        _record_phase(
            record_phase, "transforms", time.perf_counter() - transform_started
        )
        leak_started = time.perf_counter()
        enforce_leak_check(
            root=staging,
            policy=self.config.leak_check,
            globstar=self.config.globstar,
        )
        _record_phase(record_phase, "leak_check", time.perf_counter() - leak_started)
        manifest_started = time.perf_counter()
        files = tuple(
            file_entry(
                source=entry.source,
                destination=entry.destination,
                path=staging / entry.destination,
            )
            for entry in entries_tuple
        )
        _record_phase(
            record_phase, "final_manifest", time.perf_counter() - manifest_started
        )
        return StagedTree(
            root=staging,
            files=files,
            transforms=reports,
        )


def _apply_transform_destinations(
    entries: tuple[ManifestEntry, ...], *, config: WorkflowConfig
) -> tuple[ManifestEntry, ...]:
    """Update manifest destinations for transforms that relocate staged files."""
    for transform in config.transforms:
        if transform.type != "move":
            continue
        entries = tuple(_apply_move_destination(entry, transform) for entry in entries)
    return entries


def _apply_move_destination(
    entry: ManifestEntry, transform: Transform
) -> ManifestEntry:
    """Return `entry` with its destination rewritten by one move transform."""
    if entry.destination == transform.path:
        return replace(entry, destination=transform.destination)
    prefix = f"{transform.path}/"
    if entry.destination.startswith(prefix):
        suffix = entry.destination.removeprefix(transform.path)
        return replace(entry, destination=f"{transform.destination}{suffix}")
    return entry


@dataclass(frozen=True, slots=True, kw_only=True)
class DestinationPrefixer:
    """Map source-root paths to exported destination paths."""

    prefix: str
    exclude: GlobSet | None = None

    @classmethod
    def from_config(cls, config: WorkflowConfig) -> DestinationPrefixer:
        """Build a destination prefixer from workflow config."""
        exclude = (
            GlobSet(
                include=config.files.destination_prefix_exclude,
                globstar=config.globstar,
            )
            if config.files.destination_prefix_exclude
            else None
        )
        return cls(prefix=config.files.destination_prefix, exclude=exclude)

    def destination_path(self, rel: str) -> str:
        """Return the exported destination path for a source-relative path."""
        if not self.prefix or (self.exclude is not None and self.exclude.matches(rel)):
            return rel
        return f"{self.prefix}/{rel}"


def _copy_selected(
    source_root: Path,
    staging: Path,
    matcher: GlobSet,
    prefixer: DestinationPrefixer,
    source_prefix: str,
    record_phase: PhaseRecorder | None = None,
    phase_prefix: str = "",
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
        destination = prefixer.destination_path(rel)
        dest = staging / destination
        if dest.exists() or dest.is_symlink():
            source = _source_path(source_prefix=source_prefix, rel=rel)
            raise ExportError(
                f"Export destination already exists: {destination} from {source}"
            )
        dest.parent.mkdir(parents=True, exist_ok=True)
        copy_started = time.perf_counter()
        shutil.copy2(path, dest, follow_symlinks=False)
        copy_sec += time.perf_counter() - copy_started
        manifest_started = time.perf_counter()
        entries.append(
            file_entry(
                source=_source_path(source_prefix=source_prefix, rel=rel),
                destination=destination,
                path=dest,
            )
        )
        manifest_sec += time.perf_counter() - manifest_started
    total_sec = time.perf_counter() - started
    _record_phase(
        record_phase,
        f"{phase_prefix}select",
        max(total_sec - copy_sec - manifest_sec, 0),
    )
    _record_phase(record_phase, f"{phase_prefix}copy", copy_sec)
    _record_phase(record_phase, f"{phase_prefix}initial_manifest", manifest_sec)
    return tuple(entries)


def _copy_additional(
    *,
    source_ref: Path,
    staging: Path,
    source: str,
    destination: str,
    matcher: GlobSet,
    record_phase: PhaseRecorder | None = None,
) -> tuple[ManifestEntry, ...]:
    """Copy one additional repo-relative source into the staged tree."""
    source_path = (source_ref / source).resolve()
    if not source_path.is_relative_to(source_ref):
        raise ExportError(f"Copied source escapes source checkout: {source}")
    if not source_path.exists():
        raise ExportError(f"Copied source does not exist: {source}")
    if source_path.is_file() or source_path.is_symlink():
        if matcher.matches(source_path.name):
            _validate_symlink(path=source_path, source_root=source_path.parent)
            return (
                _copy_file(
                    source_ref=source_ref,
                    source_path=source_path,
                    staging=staging,
                    destination=destination,
                ),
            )
        return ()
    return _copy_selected(
        source_root=source_path,
        staging=staging,
        matcher=matcher,
        prefixer=DestinationPrefixer(prefix=destination),
        source_prefix=source,
        record_phase=record_phase,
        phase_prefix=f"copy:{destination}.",
    )


def _source_path(*, source_prefix: str, rel: str) -> str:
    """Return the source path recorded in manifests."""
    if not source_prefix:
        return rel
    return f"{source_prefix}/{rel}"


def _copy_file(
    *,
    source_ref: Path,
    source_path: Path,
    staging: Path,
    destination: str,
) -> ManifestEntry:
    """Copy one source file into staging and return its manifest entry."""
    dest = staging / destination
    if dest.exists() or dest.is_symlink():
        source = source_path.relative_to(source_ref).as_posix()
        raise ExportError(
            f"Export destination already exists: {destination} from {source}"
        )
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_path, dest, follow_symlinks=False)
    return file_entry(
        source=source_path.relative_to(source_ref).as_posix(),
        destination=destination,
        path=dest,
    )


def _write_generated(staging: Path, file_write: FileWrite) -> ManifestEntry:
    """Write one generated file into staging and return its manifest entry."""
    dest = staging / file_write.path
    if dest.exists() or dest.is_symlink():
        raise ExportError(f"Export destination already exists: {file_write.path}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(file_write.content, encoding="utf-8")
    return file_entry(
        source=f"<generated:{file_write.path}>",
        destination=file_write.path,
        path=dest,
    )


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
