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
import stat
import time

from copybarista.config import (
    FileMove,
    FileWrite,
    Transform,
    WorkflowConfig,
)
from copybarista.errors import ExportError
from copybarista.globs import GlobSet
from copybarista.leak_check import enforce_leak_check
from copybarista.manifest import (
    ManifestEntry,
    TransformReport,
    file_entry,
)
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
class _StagedFile:
    """Source and destination pair for files copied into staging."""

    source: str
    destination: str


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
                    exclude=self.config.files.effective_exclude(),
                    globstar=self.config.globstar,
                ),
                prefixer=MoveSequence(moves=self.config.files.moves),
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
                        exclude=file_copy.effective_exclude(),
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
    entries: tuple[_StagedFile, ...], *, config: WorkflowConfig
) -> tuple[_StagedFile, ...]:
    """Update manifest destinations for transforms that relocate staged files."""
    for transform in config.transforms:
        if transform.type != "move":
            continue
        entries = tuple(_apply_move_destination(entry, transform) for entry in entries)
    return entries


def _apply_move_destination(entry: _StagedFile, transform: Transform) -> _StagedFile:
    """Return `entry` with its destination rewritten by one move transform."""
    return replace(
        entry,
        destination=_relocate_path(
            entry.destination, source=transform.path, destination=transform.destination
        ),
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class MoveSequence:
    """Map source-root paths to exported destination paths via ordered moves.

    Applies each ``FileMove`` in order to a source-root-relative path, exactly as
    Copybara applies a sequence of ``core.move`` transforms: a whole-tree move
    (``path = ""``) relocates every path under a destination prefix, and a later
    per-subtree move relocates a matching subtree again (typically a back-move to
    the public root). ``import_request._reverse_file_moves`` inverts this exactly
    for injective move sequences -- the shape the config parser (non-empty
    destinations) and the export-time destination-collision guard admit. A
    non-injective sequence (two moves to one destination) cannot round-trip, but
    such a config fails export before it can ship.
    """

    moves: tuple[FileMove, ...]

    def destination_path(self, rel: str) -> str:
        """Return the exported destination path for a source-relative path."""
        for move in self.moves:
            rel = _relocate_path(rel, source=move.path, destination=move.destination)
        return rel


def _relocate_path(path: str, *, source: str, destination: str) -> str:
    """Return ``path`` with a ``source`` prefix rewritten to ``destination``.

    An empty ``source`` matches the whole tree, so every path gains the
    ``destination`` prefix. A non-empty ``source`` matches its exact value or any
    path under ``source/``, rewriting that prefix to ``destination``; a path
    matching neither is returned unchanged. This is the single forward relocation
    rule shared by the ``files.moves`` sequence and ``move`` transforms; its
    inverse is ``import_request._reverse_relocation``.
    """
    if not source:
        return f"{destination}/{path}"
    if path == source:
        return destination
    prefix = f"{source}/"
    if path.startswith(prefix):
        return f"{destination}/{path.removeprefix(prefix)}"
    return path


def _copy_selected(
    source_root: Path,
    *,
    staging: Path,
    matcher: GlobSet,
    prefixer: MoveSequence,
    source_prefix: str,
    record_phase: PhaseRecorder | None = None,
    phase_prefix: str = "",
) -> tuple[_StagedFile, ...]:
    """Copy matching source files into staging and build initial entries."""
    started = time.perf_counter()
    copy_sec = 0.0
    entries: list[_StagedFile] = []
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
        _copy_to_staging(path, dest)
        copy_sec += time.perf_counter() - copy_started
        entries.append(
            _StagedFile(
                source=_source_path(source_prefix=source_prefix, rel=rel),
                destination=destination,
            )
        )
    total_sec = time.perf_counter() - started
    _record_phase(
        record_phase,
        f"{phase_prefix}select",
        max(total_sec - copy_sec, 0),
    )
    _record_phase(record_phase, f"{phase_prefix}copy", copy_sec)
    return tuple(entries)


def _copy_additional(
    *,
    source_ref: Path,
    staging: Path,
    source: str,
    destination: str,
    matcher: GlobSet,
    record_phase: PhaseRecorder | None = None,
) -> tuple[_StagedFile, ...]:
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
        prefixer=MoveSequence(moves=(FileMove(path="", destination=destination),)),
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
) -> _StagedFile:
    """Copy one source file into staging and return its file mapping."""
    dest = staging / destination
    if dest.exists() or dest.is_symlink():
        source = source_path.relative_to(source_ref).as_posix()
        raise ExportError(
            f"Export destination already exists: {destination} from {source}"
        )
    dest.parent.mkdir(parents=True, exist_ok=True)
    _copy_to_staging(source_path, dest)
    return _StagedFile(
        source=source_path.relative_to(source_ref).as_posix(),
        destination=destination,
    )


def _write_generated(staging: Path, file_write: FileWrite) -> _StagedFile:
    """Write one generated file into staging and return its file mapping."""
    dest = staging / file_write.path
    if dest.exists() or dest.is_symlink():
        raise ExportError(f"Export destination already exists: {file_write.path}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(file_write.content, encoding="utf-8")
    return _StagedFile(
        source=f"<generated:{file_write.path}>",
        destination=file_write.path,
    )


def _copy_to_staging(source: Path, destination: Path) -> None:
    """Copy source metadata while keeping the private staging file mutable."""
    shutil.copy2(source, destination, follow_symlinks=False)
    if not destination.is_symlink():
        destination.chmod(destination.stat().st_mode | stat.S_IWUSR)


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
