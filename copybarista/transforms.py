"""Text transforms for staged export trees.

Transforms mutate the staging directory after file selection and before the
destination publisher runs. Each transform reports whether it changed content,
how many edits it made, and which staged files were affected so manifests can
describe the export without inspecting destination state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

import itertools
import os
import re
import shutil
import stat
import sys

from treekle import ReadError, json_loads

from copybarista.commands import CommandRunner
from copybarista.config import Transform
from copybarista.errors import CopybaristaError, ExportError, TransformError
from copybarista.globs import GlobSet, Globstar
from copybarista.manifest import (
    TransformFileReport,
    TransformReport,
)
from copybarista.template import ReplaceTemplate, replace_template


if TYPE_CHECKING:
    from collections.abc import Iterable


class _FileMapping(Protocol):
    """Source and destination paths for a staged file."""

    @property
    def source(self) -> str:
        """Return the original source path."""
        ...

    @property
    def destination(self) -> str:
        """Return the staged destination path."""
        ...


def apply_transforms(
    root: Path,
    transforms: tuple[Transform, ...],
    files: tuple[_FileMapping, ...] = (),
    *,
    globstar: Globstar = "one_or_more",
) -> tuple[TransformReport, ...]:
    """Apply transforms in config order.

    Args:
      root: Export staging root.
      transforms: Transforms to apply.
      files: Exported file mapping before transforms.
      globstar: Workflow ``**`` semantics for transform path globs.

    Returns:
      reports: Per-transform execution reports.

    """
    sources_by_destination = {entry.destination: entry.source for entry in files}
    reports: list[TransformReport] = []
    # One walk of the staged tree serves every text transform; only a ``move``
    # changes which paths exist, so the listing is refreshed after one.
    listing = _list_staged_files(root)
    # A run of adjacent ``replace`` transforms reads and writes each file once
    # rather than once per transform: each rewrites one file's text alone, so
    # applying the run file by file is applying it transform by transform.
    for replaces, run in itertools.groupby(transforms, key=_is_replace):
        if replaces:
            batch = tuple(run)
            results = _replace_all(
                root=root,
                transforms=batch,
                sources_by_destination=sources_by_destination,
                globstar=globstar,
                listing=listing,
            )
            reports.extend(map(_report, batch, results))
            continue
        for transform in run:
            reports.append(
                apply_transform(
                    root=root,
                    transform=transform,
                    sources_by_destination=sources_by_destination,
                    globstar=globstar,
                    listing=listing,  # pragma: no mutate -- an empty listing is walked again.
                ),
            )
            if transform.type == "move":
                listing = _list_staged_files(root)
    return tuple(reports)


def apply_transform(
    root: Path,
    *,
    transform: Transform,
    sources_by_destination: dict[str, str],
    globstar: Globstar = "one_or_more",
    listing: tuple[str, ...] = (),
) -> TransformReport:
    """Apply one configured transform and return its report.

    Args:
      root: Export staging root.
      transform: Transform config entry.
      sources_by_destination: Source paths keyed by staged destination path.
      globstar: Workflow ``**`` semantics for the transform path glob.
      listing: Root-relative POSIX paths of the staged files, as returned by a
        prior walk; empty means walk ``root`` now.

    Returns:
      report: Transform execution report.

    """
    if not listing:
        listing = _list_staged_files(root)
    if transform.type == "replace":
        (result,) = _replace_all(
            root=root,
            transforms=(transform,),
            sources_by_destination=sources_by_destination,
            globstar=globstar,
            listing=listing,
        )
    elif transform.type == "move":
        result = _move(
            root=root,
            transform=transform,
            sources_by_destination=sources_by_destination,
        )
    elif transform.type == "strip_block":
        result = _strip_block(
            root=root,
            transform=transform,
            sources_by_destination=sources_by_destination,
            globstar=globstar,
            listing=listing,
        )
    elif transform.type == "internal_lines":
        result = _internal_lines(
            root=root,
            transform=transform,
            sources_by_destination=sources_by_destination,
            globstar=globstar,
            listing=listing,
        )
    elif transform.type == "uncomment":
        result = _uncomment(
            root=root,
            transform=transform,
            sources_by_destination=sources_by_destination,
            globstar=globstar,
            listing=listing,
        )
    else:
        result = _format(
            root=root,
            transform=transform,
            sources_by_destination=sources_by_destination,
        )
    return _report(transform, result)


@dataclass(frozen=True, slots=True, kw_only=True)
class _TransformResult:
    """Internal transform outcome before adding config identity fields."""

    changed: int
    count: int
    files: tuple[TransformFileReport, ...]


def uncomment_source_text(text: str, transform: Transform) -> tuple[str, int]:
    """Uncomment single-line markers and block-delimited regions.

    Public so the importer can reverse an ``uncomment`` by locating each source
    block's exported form; a second copy of this walk would drift from it.

    Args:
      text: Source code as a string.
      transform: Transform configuration with start/end markers.

    Returns:
      uncommented_text: Source with marker lines and comments removed.
      count: Number of marker regions processed.

    """
    # ``split`` leaves a final newline as an empty last line, which no marker
    # matches, so ``join`` restores the text's ending as it was. Popping that
    # line and re-adding a newline would turn an empty text into one.
    lines = text.split("\n")
    result: list[str] = []
    i = 0
    count = 0
    while i < len(lines):
        if transform.end and transform.start in lines[i]:
            end_idx = None
            for j in range(i + 1, len(lines)):
                if transform.end in lines[j]:
                    end_idx = j
                    break
            if end_idx is None:
                raise TransformError(
                    f"Transformation '{transform.id}' did not find end marker",
                )
            result.extend(_uncomment_line(line) for line in lines[i + 1 : end_idx])
            i = end_idx + 1
            count += 1
        elif line_has_marker_token(lines[i], transform.start):
            # Inline marker (no block ``end``): uncomment the code before the
            # marker and drop the marker itself. ``line_has_marker_token`` rejects
            # a marker immediately followed by ``:`` so the inline
            # ``# copybarista:external`` never claims a block ``:external:start``/
            # ``:end`` line (whose prefix it shares); those belong to the paired
            # block ``uncomment`` transform above.
            uncommented = lines[i].split(transform.start)[0].rstrip()
            result.append(_uncomment_line(uncommented))
            count += 1
            i += 1
        else:
            result.append(lines[i])
            i += 1
    return "\n".join(result), count


def line_has_marker_token(line: str, marker: str) -> bool:
    """Return whether ``line`` carries the inline ``marker`` as a whole token.

    A bare substring test would misfire when one marker is a prefix of another --
    the per-line ``# copybarista:internal`` / ``# copybarista:external`` markers
    are prefixes of the block markers ``# copybarista:internal:start`` / ``:end``
    (and the ``:external`` equivalents). Matching the marker only when it is NOT
    immediately followed by ``:`` keeps the line marker from claiming a block
    marker's line (which belongs to a separate ``strip_block`` / block
    ``uncomment`` transform). On export the two never collide because the block
    transform runs first; on reverse-import they can, so this disambiguation is
    load-bearing.

    Args:
      line: Text line to search.
      marker: Marker string to find as a whole token (not followed by ':').

    Returns:
      has_marker: True if marker appears not immediately followed by ':'.

    """
    index = line.find(marker)
    while index != -1:
        after = index + len(marker)
        if after >= len(line) or line[after] != ":":
            return True
        index = line.find(marker, index + 1)
    return False


def strip_source_text(text: str, transform: Transform) -> str:
    """Return the exported form of one text for a strip transform.

    The single source of truth for how ``strip_block`` and ``internal_lines``
    remove content on export, factored out of the file-walking transforms so the
    reverse-import path can derive removed regions from the *actual* export
    result rather than re-deriving marker semantics by hand. Keeping one
    definition means inclusive/exclusive/else and future tweaks stay consistent
    across export and import automatically.

    Args:
      text: Source code as a string.
      transform: Transform configuration with strip markers and type.

    Returns:
      stripped_text: Source with marked regions removed (exported form).

    """
    return strip_source_regions(text, transform)[0]


def strip_source_regions(
    text: str,
    transform: Transform,
) -> tuple[str, tuple[tuple[int, str], ...]]:
    """Return the exported text and the source-only regions it removed.

    Companion to ``strip_source_text`` that also reports each removed region as
    ``(offset_in_stripped, removed_text)``: the offset the region occupied in the
    *stripped* (exported) form and the exact bytes deleted. The reverse-import
    path re-inserts these verbatim, so deriving them from the SAME traversal the
    export uses keeps them exact for every block shape -- there is no second,
    drift-prone re-derivation of marker semantics.

    A transform that rewrites rather than deletes (``strip_block`` with an
    ``else`` branch uncomments and keeps the else lines) has no verbatim removed
    region to report and cannot be reversed by re-insertion; callers that need
    regions must reject it first. This returns no regions for the ``else`` case
    rather than fabricate one.

    Args:
      text: Source code as a string.
      transform: Transform configuration with strip markers and type.

    Returns:
      exported_text: Source with marked regions removed.
      removed_regions: List of (offset_in_stripped, removed_bytes) for each
        deleted region, empty when transform uses else/rewrite logic.

    """
    if transform.type == "strip_block":
        if transform.else_marker:
            return _strip_blocks_with_else(text, transform)[0], ()
        return _strip_blocks_regions(text, transform)
    if transform.type == "internal_lines":
        marker = transform.start
        kept: list[str] = []
        regions: list[tuple[int, str]] = []
        offset = 0
        for line in text.splitlines(keepends=True):
            if line_has_marker_token(line, marker):
                regions.append((offset, line))
            else:
                kept.append(line)
                offset += len(line)
        return "".join(kept), tuple(regions)
    raise TransformError(
        f"strip_source_text does not support transform type {transform.type!r}",
    )


@dataclass(slots=True, kw_only=True)
class _Replacement:
    """One ``replace`` transform and its tally over a shared pass of the files."""

    transform: Transform
    template: ReplaceTemplate | None
    matcher: GlobSet
    matched: bool = False
    skipped_symlink: bool = False
    count: int = 0
    files: list[TransformFileReport] = field(default_factory=list)

    def substitute(self, text: str) -> tuple[str, int]:
        """Rewrite ``text`` by this transform.

        Args:
          text: The file's text as the earlier transforms left it.

        Returns:
          updated: The rewritten text.
          replacements: How many occurrences were replaced.

        """
        if self.template is not None:
            return self.template.apply_counted(text)
        before, after = self.transform.before, self.transform.after
        return text.replace(before, after), text.count(before)

    def result(self) -> _TransformResult:
        """Return the tally as a report.

        Returns:
          result: What this transform changed.

        Raises:
          TransformError: The transform is required and changed nothing.

        """
        if not self.files and self.transform.required:
            if self.skipped_symlink and not self.matched:
                reason = "only matched symlinks"
            elif self.skipped_symlink or self.matched:
                reason = "found files, but no replacement text"
            else:
                reason = "matched no files"
            raise TransformError(
                f"Transformation '{self.transform.id}' made no changes: {reason}",
            )
        return _TransformResult(
            changed=len(self.files),
            count=self.count,
            files=tuple(self.files),
        )


def biome_command(root: Path) -> list[str]:
    """Return the Biome command that formats as ``root/biome.json`` is written for.

    Args:
      root: Tree whose ``biome.json`` holds the formatter settings.

    Returns:
      argv: ``npx`` running the Biome release named by the settings' ``$schema``.

    Raises:
      TransformError: ``root`` has no ``biome.json`` naming a Biome release.

    """
    # The schema URL is the version the settings were written for (``biome migrate``
    # rewrites it), so the export formats exactly as a checkout using them does.
    path = root / "biome.json"
    try:
        settings = json_loads(path.read_bytes(), dict[str, object])
    except (OSError, ReadError):
        settings = dict[str, object]()
    schema = settings.get("$schema")
    version = re.fullmatch(
        r"https://biomejs\.dev/schemas/([0-9][\w.-]*)/schema\.json",
        schema if isinstance(schema, str) else "",
    )
    if version is None:
        raise TransformError(
            f"biome_format needs {path} whose $schema names its Biome release",
        )
    return [shutil.which("npx") or "npx", "--yes", f"@biomejs/biome@{version[1]}"]


def _format_commands(root: Path, transform: Transform) -> list[list[str]]:
    """Return the formatter runs one ``ruff_format`` or ``biome_format`` makes."""
    if transform.type == "biome_format":
        return [
            [
                *biome_command(root),
                "format",
                "--write",
                "--no-errors-on-unmatched",
                transform.path,
            ],
        ]
    ruff = [sys.executable, "-m", "ruff"]
    # Formatting exposes compact multiline brackets; COM812 then explodes them.
    return [
        [*ruff, "format", "--no-cache", transform.path],
        [
            *ruff,
            "check",
            "--fix",
            "--extend-select",
            "COM812",
            "--exit-zero",
            "--no-cache",
            transform.path,
        ],
        [*ruff, "format", "--no-cache", transform.path],
    ]


def _format(
    root: Path,
    transform: Transform,
    sources_by_destination: dict[str, str],
) -> _TransformResult:
    """Run a formatter inside the staged tree and report the files it rewrote."""
    target = root / transform.path
    if not target.exists():
        if transform.required:
            raise TransformError(f"Transformation '{transform.id}' matched no files")
        return _TransformResult(changed=0, count=0, files=())
    commands = _format_commands(root, transform)
    before = _snapshot_regular_files(root=root, target=target)
    newest_tick = _newest_tick_contents(root=root, snapshot=before)
    runner = CommandRunner()
    try:
        for argv in commands:
            runner.run(argv, cwd=root)
    except ExportError as err:
        raise TransformError(
            f"Transformation '{transform.id}' {transform.type} failed: {err}",
        ) from err
    after = _snapshot_regular_files(root=root, target=target)
    changed_paths = tuple(
        path
        for path in _sorted_by_segments(after)
        if before.get(path) != after[path]
        or (path in newest_tick and (root / path).read_bytes() != newest_tick[path])
    )
    deleted_paths = tuple(
        path for path in _sorted_by_segments(before) if path not in after
    )
    files = tuple(
        _file_report(
            root=root,
            path=root / path,
            count=1,
            sources_by_destination=sources_by_destination,
        )
        for path in (*changed_paths, *deleted_paths)
    )
    return _TransformResult(changed=len(files), count=len(files), files=files)


def _strip_blocks(text: str, transform: Transform) -> tuple[str, int]:
    """Remove every marker-delimited block from text."""
    if not transform.start or not transform.end:
        raise TransformError(
            f"Transformation '{transform.id}' markers must be non-empty",
        )
    if transform.else_marker:
        return _strip_blocks_with_else(text, transform)
    updated = text
    search_from = 0  # pragma: no mutate -- str.find reads a None start as 0.
    count = 0
    while True:
        start_idx = updated.find(transform.start, search_from)
        if start_idx < 0:
            return updated, count
        first_end_idx = updated.find(transform.end, search_from)
        if first_end_idx >= 0 and first_end_idx < start_idx:
            raise TransformError(
                f"Transformation '{transform.id}' found end marker before start marker",
            )
        end_idx = updated.find(transform.end, start_idx + len(transform.start))
        if end_idx == -1:
            raise TransformError(
                f"Transformation '{transform.id}' did not find end marker",
            )
        next_start_idx = updated.find(transform.start, start_idx + len(transform.start))
        if next_start_idx != -1 and next_start_idx < end_idx:
            raise TransformError(
                f"Transformation '{transform.id}' found nested start marker",
            )
        if transform.inclusive:
            end_idx += len(transform.end)
            # Cut from the marker's OWN line start: ``find`` returns the marker
            # offset, so an indented block left its leading whitespace behind as
            # a stub line. ``ruff_format`` then deletes that stub, and reversal
            # could never reproduce the public text from the stubbed form.
            # pragma: no mutate start -- str.rfind reads a None start as 0.
            line_start = updated.rfind("\n", 0, start_idx) + 1
            # pragma: no mutate end
            if not updated[line_start:start_idx].strip():
                start_idx = line_start
            updated = _collapse_removed_block_gap(
                updated[:start_idx],
                updated[end_idx:],
            )
            search_from = start_idx
        else:
            updated = updated[:start_idx] + updated[end_idx:]
            search_from = start_idx + len(transform.end)
        count += 1


def _strip_blocks_with_else(text: str, transform: Transform) -> tuple[str, int]:
    """Replace an internal/public conditional block with its else branch."""
    # ``split`` leaves a final newline as an empty last line, which no marker
    # matches, so ``join`` restores the text's ending as it was. Popping that
    # line and re-adding a newline would turn an empty text into one.
    lines = text.split("\n")
    result: list[str] = []
    i = 0
    count = 0
    while i < len(lines):
        if transform.start in lines[i]:
            else_idx = None
            end_idx = None
            for j in range(i + 1, len(lines)):
                if transform.else_marker in lines[j]:
                    else_idx = j
                elif transform.end in lines[j]:
                    end_idx = j
                    break
            if end_idx is None:
                raise TransformError(
                    f"Transformation '{transform.id}' did not find end marker",
                )
            if else_idx is None:
                raise TransformError(
                    f"Transformation '{transform.id}' found block without "
                    f"else marker '{transform.else_marker}'",
                )
            for line in lines[else_idx + 1 : end_idx]:
                stripped = line.lstrip()
                indent = line[: len(line) - len(stripped)]
                if stripped.startswith("# "):
                    result.append(indent + stripped[2:])
                elif stripped.startswith("#"):
                    result.append(indent + stripped[1:])
                else:
                    result.append(line)
            i = end_idx + 1
            count += 1
        else:
            result.append(lines[i])
            i += 1
    return "\n".join(result), count


def _file_report(
    root: Path,
    path: Path,
    count: int,
    sources_by_destination: dict[str, str],
) -> TransformFileReport:
    """Build the per-file transform report for a staged path."""
    destination = path.relative_to(root).as_posix()
    return TransformFileReport(
        source=sources_by_destination.get(destination, destination),
        destination=destination,
        count=count,
    )


# ``os.walk`` and a string slice, not ``rglob`` + ``Path.relative_to``: the pathlib
# spelling builds a ``Path`` per entry and then reparses it, and exporting priml called
# ``relative_to`` 108,615 times across 17 patterns, 2.6s of a 7.5s export. Walked once
# per ``apply_transforms`` (0.015s over 4,785 files) rather than once per transform
# (0.32s for the 17 priml text transforms); a ``move`` is the only transform that
# changes the set, and the caller re-walks after one.
def _list_staged_files(root: Path) -> tuple[str, ...]:
    """Return every staged non-directory entry as a root-relative POSIX path."""
    prefix = len(str(root)) + 1
    rel_paths: list[str] = []
    for directory, _, names in os.walk(root):
        rel_paths.extend(
            os.path.join(directory, name)[prefix:].replace(os.sep, "/")  # noqa: PTH118 -- str path is the point.
            for name in names
        )
    return tuple(_sorted_by_segments(rel_paths))


# ``Path`` compares by path segments, so ``a/b`` sorts before ``a-b``; a plain string
# sort puts ``-`` before ``/`` and would reorder manifests and reports.
def _sorted_by_segments(rel_paths: Iterable[str]) -> list[str]:
    """Sort relative POSIX paths the way ``sorted()`` orders ``Path`` objects."""
    return sorted(rel_paths, key=_path_segments)


def _path_segments(rel_path: str) -> list[str]:
    return rel_path.split("/")


def _matching_files(
    root: Path,
    *,
    listing: tuple[str, ...],
    pattern: str,
    globstar: Globstar,
) -> tuple[Path, ...]:
    """Return staged files from ``listing`` matched by one supported path glob."""
    matcher = GlobSet(include=(pattern,), globstar=globstar)
    return tuple(root / rel for rel in listing if matcher.matches(rel))


def _read_text(path: Path) -> str:
    """Read UTF-8 text and turn decode failures into transform errors."""
    try:
        # pragma: no mutate start -- this host's locale is UTF-8 too.
        return path.read_text(encoding="utf-8")
        # pragma: no mutate end
    except UnicodeDecodeError as err:
        raise TransformError(f"Cannot decode UTF-8 file for transform: {path}") from err


# Not the bytes: reading every staged file twice cost 0.36s of a priml export. Size and
# mtime miss only a same-size rewrite within the snapshot's own timestamp tick: ruff's
# quote normalization keeps the size, and on a filesystem stamping whole seconds a file
# the preceding transform wrote milliseconds earlier keeps its mtime through ruff's
# rewrite. ``_newest_tick_contents`` keeps the bytes of just the files in that tick.
#
# ``os.walk`` + ``os.lstat`` on strings rather than ``rglob`` + ``Path.lstat``: the two
# snapshots around ruff cost 0.25s of a priml export as pathlib, 0.05s as strings.
def _snapshot_regular_files(root: Path, target: Path) -> dict[str, tuple[int, int]]:
    """Return ``(size, mtime_ns)`` per staged regular file, keyed by relative path."""
    prefix = len(str(root)) + 1
    if target.is_file():
        candidates = [str(target)]
    else:
        candidates = [
            os.path.join(directory, name)  # noqa: PTH118 -- str path is the point.
            for directory, _, names in os.walk(target)
            for name in names
        ]
    snapshot: dict[str, tuple[int, int]] = {}
    for full in candidates:
        status = os.lstat(full)
        if stat.S_ISREG(status.st_mode):
            rel = full[prefix:].replace(os.sep, "/")
            snapshot[rel] = (status.st_size, status.st_mtime_ns)
    return snapshot


# Every write after the snapshot lands in its newest tick or a later one, so a file
# stamped earlier cannot be rewritten without its mtime moving. Measuring the tick from
# the newest mtime rather than the clock needs no assumption about its length.
def _newest_tick_contents(
    root: Path,
    snapshot: dict[str, tuple[int, int]],
) -> dict[str, bytes]:
    """Return the bytes of the snapshot's files that share its newest mtime."""
    newest = max((mtime_ns for _, mtime_ns in snapshot.values()), default=None)
    return {
        path: (root / path).read_bytes()
        for path, (_, mtime_ns) in snapshot.items()
        if mtime_ns == newest
    }


def _is_replace(transform: Transform) -> bool:
    return transform.type == "replace"


def _report(transform: Transform, result: _TransformResult) -> TransformReport:
    """Return ``result`` under ``transform``'s configured identity."""
    return TransformReport(
        id=transform.id,
        type=transform.type,
        path=transform.path,
        changed=result.changed,
        count=result.count,
        files=result.files,
    )


# Mirrors ``_strip_blocks`` exactly (same marker walk, same inclusive gap collapse) so
# the removed spans are the ground truth for reverse re-insertion. ``updated`` is edited
# in place and re-searched from the cut point, so ``start_idx`` is already an offset in
# the partially-stripped text -- i.e. the final stripped-form offset of that region.
def _strip_blocks_regions(
    text: str,
    transform: Transform,
) -> tuple[str, tuple[tuple[int, str], ...]]:
    """Strip marker-delimited blocks, reporting each removed region."""
    if not transform.start or not transform.end:
        raise TransformError(
            f"Transformation '{transform.id}' markers must be non-empty",
        )
    updated = text
    search_from = 0  # pragma: no mutate -- str.find reads a None start as 0.
    regions: list[tuple[int, str]] = []
    while True:
        start_idx = updated.find(transform.start, search_from)
        if start_idx < 0:
            return updated, tuple(regions)
        first_end_idx = updated.find(transform.end, search_from)
        if 0 <= first_end_idx < start_idx:
            raise TransformError(
                f"Transformation '{transform.id}' found end marker before start marker",
            )
        end_idx = updated.find(transform.end, start_idx + len(transform.start))
        if end_idx == -1:
            raise TransformError(
                f"Transformation '{transform.id}' did not find end marker",
            )
        next_start_idx = updated.find(transform.start, start_idx + len(transform.start))
        if next_start_idx != -1 and next_start_idx < end_idx:
            raise TransformError(
                f"Transformation '{transform.id}' found nested start marker",
            )
        if transform.inclusive:
            end_idx += len(transform.end)
            # Mirror ``_strip_blocks``: the cut starts at the marker's own line
            # start so an indented block leaves no whitespace stub.
            # pragma: no mutate start -- str.rfind reads a None start as 0.
            line_start = updated.rfind("\n", 0, start_idx) + 1
            # pragma: no mutate end
            if not updated[line_start:start_idx].strip():
                start_idx = line_start
            removed = updated[start_idx:end_idx]
            after = updated[end_idx:]
            # Match _collapse_removed_block_gap: when kept text before the block
            # ends in a newline and the text after begins with newline(s), those
            # leading newlines are collapsed into the cut, so they are part of
            # the removed region (``after.lstrip("\n")``).
            if updated[:start_idx].endswith("\n") and after.startswith("\n"):
                stripped_after = after.lstrip("\n")
                removed += after[: len(after) - len(stripped_after)]
                after = stripped_after
            updated = updated[:start_idx] + after
            search_from = start_idx
        else:
            removed = updated[start_idx:end_idx]
            updated = updated[:start_idx] + updated[end_idx:]
            search_from = start_idx + len(transform.end)
        regions.append((start_idx, removed))


def _collapse_removed_block_gap(before: str, after: str) -> str:
    """Avoid creating extra blank lines around an inclusive stripped block."""
    if before.endswith("\n") and after.startswith("\n"):
        return before + after.lstrip("\n")
    return before + after


def _replacement(transform: Transform, globstar: Globstar) -> _Replacement:
    """Compile one ``replace`` transform's rule and path glob."""
    if not transform.before:
        raise TransformError(
            f"Transformation '{transform.id}' before must be non-empty",
        )
    return _Replacement(
        transform=transform,
        template=replace_template(
            before=transform.before,
            after=transform.after,
            regex_groups=transform.regex_groups,
            module=transform.module,
        ),
        matcher=GlobSet(include=(transform.path,), globstar=globstar),
    )


# Each file's text passes through every transform whose glob matches it, in config
# order, so each transform sees what the earlier ones made of it. A failure is the one
# applying them one at a time would raise first: a rule that cannot compile stops the
# run there, after the rules before it have been applied and checked.
def _replace_all(
    root: Path,
    *,
    transforms: tuple[Transform, ...],
    sources_by_destination: dict[str, str],
    globstar: Globstar,
    listing: tuple[str, ...],
) -> list[_TransformResult]:
    """Apply adjacent ``replace`` transforms in order, one read and write per file."""
    rules: list[_Replacement] = []
    failure: CopybaristaError | None = None
    for transform in transforms:
        try:
            rules.append(_replacement(transform, globstar))
        except CopybaristaError as error:
            failure = error
            break
    for rel in listing:
        matching = [rule for rule in rules if rule.matcher.matches(rel)]
        if not matching:
            continue
        path = root / rel
        if path.is_symlink():
            for rule in matching:
                rule.skipped_symlink = True
            continue
        original = text = _read_text(path)
        for rule in matching:
            rule.matched = True
            updated, replacements = rule.substitute(text)
            if replacements == 0 or updated == text:
                continue
            text = updated
            rule.count += replacements
            rule.files.append(
                _file_report(
                    root=root,
                    path=path,
                    count=replacements,
                    sources_by_destination=sources_by_destination,
                ),
            )
        if text != original:
            # pragma: no mutate start -- this host's locale is UTF-8 too.
            path.write_text(text, encoding="utf-8")
            # pragma: no mutate end
    results = [rule.result() for rule in rules]
    if failure is not None:
        raise failure
    return results


def _strip_block(
    root: Path,
    transform: Transform,
    sources_by_destination: dict[str, str],
    globstar: Globstar,
    listing: tuple[str, ...],
) -> _TransformResult:
    """Remove marker-delimited blocks from matched files."""
    paths = _matching_files(
        root=root,
        listing=listing,
        pattern=transform.path,
        globstar=globstar,
    )
    changed = 0
    total_count = 0
    files: list[TransformFileReport] = []
    for path in paths:
        if path.is_symlink():
            continue
        original = _read_text(path)
        updated, count = _strip_blocks(original, transform)
        if updated == original:
            continue
        # pragma: no mutate start -- this host's locale is UTF-8 too.
        path.write_text(updated, encoding="utf-8")
        # pragma: no mutate end
        changed += 1
        total_count += count
        files.append(
            _file_report(
                root=root,
                path=path,
                count=count,
                sources_by_destination=sources_by_destination,
            ),
        )
    if changed == 0 and transform.required:
        if paths:
            raise TransformError(
                f"Transformation '{transform.id}' did not find start marker",
            )
        raise TransformError(f"Transformation '{transform.id}' matched no files")
    return _TransformResult(changed=changed, count=total_count, files=tuple(files))


def _internal_lines(
    root: Path,
    transform: Transform,
    sources_by_destination: dict[str, str],
    globstar: Globstar,
    listing: tuple[str, ...],
) -> _TransformResult:
    """Remove every line containing the start marker from matched files."""
    paths = _matching_files(
        root=root,
        listing=listing,
        pattern=transform.path,
        globstar=globstar,
    )
    changed = 0
    total_count = 0
    files: list[TransformFileReport] = []
    for path in paths:
        if path.is_symlink():
            continue
        original = _read_text(path)
        lines = original.splitlines(keepends=True)
        kept = [
            line for line in lines if not line_has_marker_token(line, transform.start)
        ]
        count = len(lines) - len(kept)
        if count == 0:
            continue
        # pragma: no mutate start -- this host's locale is UTF-8 too.
        path.write_text("".join(kept), encoding="utf-8")
        # pragma: no mutate end
        changed += 1
        total_count += count
        files.append(
            _file_report(
                root=root,
                path=path,
                count=count,
                sources_by_destination=sources_by_destination,
            ),
        )
    if changed == 0 and transform.required:
        if paths:
            raise TransformError(f"Transformation '{transform.id}' did not find marker")
        raise TransformError(f"Transformation '{transform.id}' matched no files")
    return _TransformResult(changed=changed, count=total_count, files=tuple(files))


def _uncomment(
    root: Path,
    transform: Transform,
    sources_by_destination: dict[str, str],
    globstar: Globstar,
    listing: tuple[str, ...],
) -> _TransformResult:
    """Uncomment lines marked for external export."""
    paths = _matching_files(
        root=root,
        listing=listing,
        pattern=transform.path,
        globstar=globstar,
    )
    changed = 0
    total_count = 0
    files: list[TransformFileReport] = []
    for path in paths:
        if path.is_symlink():
            continue
        original = _read_text(path)
        updated, count = uncomment_source_text(original, transform)
        if updated == original:
            continue
        # pragma: no mutate start -- this host's locale is UTF-8 too.
        path.write_text(updated, encoding="utf-8")
        # pragma: no mutate end
        changed += 1
        total_count += count
        files.append(
            _file_report(
                root=root,
                path=path,
                count=count,
                sources_by_destination=sources_by_destination,
            ),
        )
    if changed == 0 and transform.required:
        if paths:
            raise TransformError(f"Transformation '{transform.id}' did not find marker")
        raise TransformError(f"Transformation '{transform.id}' matched no files")
    return _TransformResult(changed=changed, count=total_count, files=tuple(files))


def _uncomment_line(line: str) -> str:
    """Strip a leading comment prefix from a line, preserving indentation."""
    stripped = line.lstrip()
    indent = line[: len(line) - len(stripped)]
    if stripped.startswith("# "):
        return indent + stripped[2:]
    if stripped.startswith("#"):
        return indent + stripped[1:]
    return line


def _move(
    root: Path,
    transform: Transform,
    sources_by_destination: dict[str, str],
) -> _TransformResult:
    """Move or rename a file or directory within the staging tree."""
    source = root / transform.path
    dest = root / transform.destination
    if not source.exists():
        if transform.required:
            raise TransformError(f"Transformation '{transform.id}' matched no files")
        return _TransformResult(changed=0, count=0, files=())
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), dest)
    if dest.is_dir():
        moved = [p for p in dest.rglob("*") if p.is_file()]
    else:
        moved = [dest]
    count = len(moved)
    files: list[TransformFileReport] = []
    for path in sorted(moved):
        new_path = path.relative_to(root)
        new_rel = new_path.as_posix()
        suffix = new_path.relative_to(transform.destination)
        old_rel = (Path(transform.path) / suffix).as_posix()
        files.append(
            TransformFileReport(
                source=sources_by_destination.get(old_rel, old_rel),
                destination=new_rel,
                count=1,
            ),
        )
    return _TransformResult(changed=count, count=count, files=tuple(files))
