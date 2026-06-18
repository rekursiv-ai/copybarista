"""Text transforms for staged export trees.

Transforms mutate the staging directory after file selection and before the
destination publisher runs. Each transform reports whether it changed content,
how many edits it made, and which staged files were affected so manifests can
describe the export without inspecting destination state.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import shutil
import sys

from copybarista.commands import CommandRunner
from copybarista.config import Transform
from copybarista.errors import ExportError, TransformError
from copybarista.globs import GlobSet, Globstar
from copybarista.manifest import (
    TransformFileReport,
    TransformReport,
)
from copybarista.template import compile_replace


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
    return tuple(
        apply_transform(
            root=root,
            transform=transform,
            sources_by_destination=sources_by_destination,
            globstar=globstar,
        )
        for transform in transforms
    )


def apply_transform(
    root: Path,
    *,
    transform: Transform,
    sources_by_destination: dict[str, str],
    globstar: Globstar = "one_or_more",
) -> TransformReport:
    """Apply one configured transform and return its report.

    Args:
      root: Export staging root.
      transform: Transform config entry.
      sources_by_destination: Source paths keyed by staged destination path.
      globstar: Workflow ``**`` semantics for the transform path glob.

    Returns:
      report: Transform execution report.

    """
    if transform.type == "replace":
        result = _replace(
            root=root,
            transform=transform,
            sources_by_destination=sources_by_destination,
            globstar=globstar,
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
        )
    elif transform.type == "internal_lines":
        result = _internal_lines(
            root=root,
            transform=transform,
            sources_by_destination=sources_by_destination,
            globstar=globstar,
        )
    elif transform.type == "uncomment":
        result = _uncomment(
            root=root,
            transform=transform,
            sources_by_destination=sources_by_destination,
            globstar=globstar,
        )
    else:
        result = _ruff_format(
            root=root,
            transform=transform,
            sources_by_destination=sources_by_destination,
        )
    return TransformReport(
        id=transform.id,
        type=transform.type,
        path=transform.path,
        changed=result.changed,
        count=result.count,
        files=result.files,
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class _TransformResult:
    """Internal transform outcome before adding config identity fields."""

    changed: int
    count: int
    files: tuple[TransformFileReport, ...]


def _replace(
    root: Path,
    transform: Transform,
    sources_by_destination: dict[str, str],
    globstar: Globstar,
) -> _TransformResult:
    """Apply a literal or regex-group replacement and return its change report."""
    if not transform.before:
        raise TransformError(
            f"Transformation '{transform.id}' before must be non-empty"
        )
    template = (
        compile_replace(
            before=transform.before,
            after=transform.after,
            regex_groups=transform.regex_groups,
        )
        if transform.regex_groups
        else None
    )
    paths = _matching_files(root=root, pattern=transform.path, globstar=globstar)
    matched_files = 0
    skipped_symlinks = 0
    changed = 0
    count = 0
    files: list[TransformFileReport] = []
    for path in paths:
        if path.is_symlink():
            skipped_symlinks += 1
            continue
        matched_files += 1
        original = _read_text(path)
        if template is not None:
            replacements = template.count(original)
            updated = template.apply(original)
        else:
            replacements = original.count(transform.before)
            updated = original.replace(transform.before, transform.after)
        if replacements == 0 or updated == original:
            continue
        path.write_text(updated, encoding="utf-8")
        changed += 1
        count += replacements
        files.append(
            _file_report(
                root=root,
                path=path,
                count=replacements,
                sources_by_destination=sources_by_destination,
            )
        )
    if changed == 0 and transform.required:
        if skipped_symlinks and matched_files == 0:
            reason = "only matched symlinks"
        elif paths:
            reason = "found files, but no replacement text"
        else:
            reason = "matched no files"
        raise TransformError(
            f"Transformation '{transform.id}' made no changes: {reason}"
        )
    return _TransformResult(changed=changed, count=count, files=tuple(files))


def _strip_block(
    root: Path,
    transform: Transform,
    sources_by_destination: dict[str, str],
    globstar: Globstar,
) -> _TransformResult:
    """Remove marker-delimited blocks from matched files."""
    paths = _matching_files(root=root, pattern=transform.path, globstar=globstar)
    changed = 0
    total_count = 0
    files: list[TransformFileReport] = []
    for path in paths:
        if path.is_symlink():
            continue
        original = _read_text(path)
        updated, count = _strip_blocks(original, transform)
        if count == 0 or updated == original:
            continue
        path.write_text(updated, encoding="utf-8")
        changed += 1
        total_count += count
        files.append(
            _file_report(
                root=root,
                path=path,
                count=count,
                sources_by_destination=sources_by_destination,
            )
        )
    if changed == 0 and transform.required:
        if paths:
            raise TransformError(
                f"Transformation '{transform.id}' did not find start marker"
            )
        raise TransformError(f"Transformation '{transform.id}' matched no files")
    return _TransformResult(changed=changed, count=total_count, files=tuple(files))


def _internal_lines(
    root: Path,
    transform: Transform,
    sources_by_destination: dict[str, str],
    globstar: Globstar,
) -> _TransformResult:
    """Remove every line containing the start marker from matched files."""
    paths = _matching_files(root=root, pattern=transform.path, globstar=globstar)
    changed = 0
    total_count = 0
    files: list[TransformFileReport] = []
    for path in paths:
        if path.is_symlink():
            continue
        original = _read_text(path)
        lines = original.splitlines(keepends=True)
        kept = [line for line in lines if transform.start not in line]
        count = len(lines) - len(kept)
        if count == 0:
            continue
        path.write_text("".join(kept), encoding="utf-8")
        changed += 1
        total_count += count
        files.append(
            _file_report(
                root=root,
                path=path,
                count=count,
                sources_by_destination=sources_by_destination,
            )
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
) -> _TransformResult:
    """Uncomment lines marked for external export."""
    paths = _matching_files(root=root, pattern=transform.path, globstar=globstar)
    changed = 0
    total_count = 0
    files: list[TransformFileReport] = []
    for path in paths:
        if path.is_symlink():
            continue
        original = _read_text(path)
        updated, count = _uncomment_text(original, transform)
        if count == 0 or updated == original:
            continue
        path.write_text(updated, encoding="utf-8")
        changed += 1
        total_count += count
        files.append(
            _file_report(
                root=root,
                path=path,
                count=count,
                sources_by_destination=sources_by_destination,
            )
        )
    if changed == 0 and transform.required:
        if paths:
            raise TransformError(f"Transformation '{transform.id}' did not find marker")
        raise TransformError(f"Transformation '{transform.id}' matched no files")
    return _TransformResult(changed=changed, count=total_count, files=tuple(files))


def _uncomment_text(text: str, transform: Transform) -> tuple[str, int]:
    """Uncomment single-line markers and block-delimited regions."""
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
        trailing_newline = True
    else:
        trailing_newline = False
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
                    f"Transformation '{transform.id}' did not find end marker"
                )
            result.extend(_uncomment_line(line) for line in lines[i + 1 : end_idx])
            i = end_idx + 1
            count += 1
        elif transform.start in lines[i]:
            uncommented = lines[i].split(transform.start)[0].rstrip()
            result.append(_uncomment_line(uncommented))
            count += 1
            i += 1
        else:
            result.append(lines[i])
            i += 1
    final = "\n".join(result)
    if trailing_newline:
        final += "\n"
    return final, count


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
    root: Path, transform: Transform, sources_by_destination: dict[str, str]
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
            )
        )
    return _TransformResult(changed=count, count=count, files=tuple(files))


def _ruff_format(
    root: Path, transform: Transform, sources_by_destination: dict[str, str]
) -> _TransformResult:
    """Run Ruff fixes and formatting inside the staged tree."""
    target = root / transform.path
    if not target.exists():
        if transform.required:
            raise TransformError(f"Transformation '{transform.id}' matched no files")
        return _TransformResult(changed=0, count=0, files=())
    before = _snapshot_regular_files(root=root, target=target)
    runner = CommandRunner()
    try:
        runner.run(
            [
                sys.executable,
                "-m",
                "ruff",
                "check",
                "--fix",
                "--exit-zero",
                "--no-cache",
                transform.path,
            ],
            cwd=root,
        )
        runner.run(
            [sys.executable, "-m", "ruff", "format", "--no-cache", transform.path],
            cwd=root,
        )
    except ExportError as err:
        raise TransformError(
            f"Transformation '{transform.id}' ruff_format failed: {err}"
        ) from err
    after = _snapshot_regular_files(root=root, target=target)
    changed_paths = tuple(
        path for path in sorted(after) if before.get(path) != after[path]
    )
    deleted_paths = tuple(path for path in sorted(before) if path not in after)
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
            f"Transformation '{transform.id}' markers must be non-empty"
        )
    if transform.else_marker:
        return _strip_blocks_with_else(text, transform)
    updated = text
    search_from = 0
    count = 0
    while True:
        start_idx = updated.find(transform.start, search_from)
        if start_idx < 0:
            return updated, count
        first_end_idx = updated.find(transform.end, search_from)
        if first_end_idx >= 0 and first_end_idx < start_idx:
            raise TransformError(
                f"Transformation '{transform.id}' found end marker before start marker"
            )
        end_idx = updated.find(transform.end, start_idx + len(transform.start))
        if end_idx < 0:
            raise TransformError(
                f"Transformation '{transform.id}' did not find end marker"
            )
        next_start_idx = updated.find(transform.start, start_idx + len(transform.start))
        if 0 <= next_start_idx < end_idx:
            raise TransformError(
                f"Transformation '{transform.id}' found nested start marker"
            )
        if transform.inclusive:
            end_idx += len(transform.end)
            updated = _collapse_removed_block_gap(
                updated[:start_idx], updated[end_idx:]
            )
            search_from = start_idx
        else:
            updated = updated[:start_idx] + updated[end_idx:]
            search_from = start_idx + len(transform.end)
        count += 1


def _strip_blocks_with_else(text: str, transform: Transform) -> tuple[str, int]:
    """Replace an internal/public conditional block with its else branch."""
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
        trailing_newline = True
    else:
        trailing_newline = False
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
                    f"Transformation '{transform.id}' did not find end marker"
                )
            if else_idx is None:
                raise TransformError(
                    f"Transformation '{transform.id}' found block without "
                    f"else marker '{transform.else_marker}'"
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
    final = "\n".join(result)
    if trailing_newline:
        final += "\n"
    return final, count


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


def _matching_files(root: Path, pattern: str, globstar: Globstar) -> tuple[Path, ...]:
    """Return staged files matched by one supported path glob."""
    matcher = GlobSet(include=(pattern,), globstar=globstar)
    return tuple(
        path
        for path in sorted(root.rglob("*"))
        if path.is_file() and matcher.matches(path.relative_to(root).as_posix())
    )


def _read_text(path: Path) -> str:
    """Read UTF-8 text and turn decode failures into transform errors."""
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as err:
        raise TransformError(f"Cannot decode UTF-8 file for transform: {path}") from err


def _snapshot_regular_files(root: Path, target: Path) -> dict[Path, bytes]:
    """Return staged file bytes keyed by root-relative path."""
    paths = (target,) if target.is_file() else tuple(sorted(target.rglob("*")))
    return {
        path.relative_to(root): path.read_bytes()
        for path in paths
        if path.is_file() and not path.is_symlink()
    }


def _collapse_removed_block_gap(before: str, after: str) -> str:
    """Avoid creating extra blank lines around an inclusive stripped block."""
    if before.endswith("\n") and after.startswith("\n"):
        return before + after.lstrip("\n")
    return before + after
