"""Leak checks for transformed export trees.

The export pipeline already controls which files enter the public tree and
which transforms run. Leak checks are the final, read-only guard over that
transformed tree: they catch source-only paths, monorepo import names, private
markers, and similar release mistakes before any destination is mutated.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

import os

from copybarista.config import reverse_file_moves, reverse_move_transforms
from copybarista.errors import LeakCheckError
from copybarista.globs import GlobSet, Globstar
from copybarista.lib.files.grep import GrepError, Query, grep


if TYPE_CHECKING:
    from copybarista.config import (
        FileSelection,
        ForbiddenPathRule,
        ForbiddenTextRule,
        LeakCheck,
        Transform,
        WorkflowConfig,
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class LeakViolation:
    """One leak-check policy violation."""

    rule_id: str

    path: str

    line: int = 0

    message: str = ""

    def format(self) -> str:
        """Return a CI-safe violation message without echoing matched text.

        Returns:
          result: The str.

        """
        location = f"{self.path}:{self.line}" if self.line else self.path
        if self.message:
            return f"{self.rule_id}: {location}: {self.message}"
        return f"{self.rule_id}: {location}: forbidden export content"


def check_leaks(
    *,
    root: Path,
    policy: LeakCheck,
    files: FileSelection,
    transforms: tuple[Transform, ...] = (),
    globstar: Globstar = "one_or_more",
) -> tuple[LeakViolation, ...]:
    """Return leak-check violations for a transformed tree.

    Every ``files.exclude`` pattern is also a forbidden path: an exported file
    whose source-root path (``move`` transforms and ``files.moves`` undone) is
    excluded was put back by a ``[[files.copy]]`` or ``[[files.write]]``.

    Args:
      root: Exported tree root to scan.
      policy: Leak-check rules from workflow config.
      files: Workflow file selection whose excludes are forbidden.
      transforms: Workflow transforms; ``move`` entries are undone first.
      globstar: Workflow ``**`` semantics for rule path globs.

    Returns:
      violations: Policy violations in deterministic order.

    Raises:
      LeakCheckError: If `root` is not a directory and policy has rules.

    """
    if not policy.forbidden_path and not policy.forbidden_text and not files.exclude:
        return ()
    if not root.is_dir():
        raise LeakCheckError(f"Leak check root does not exist: {root}")
    listing = _list_tree(root)
    return (
        *_excluded_path_violations(
            files=files,
            transforms=transforms,
            rel_paths=listing.regular_files,
            globstar=globstar,
        ),
        *_forbidden_path_violations(
            rules=policy.forbidden_path,
            rel_paths=listing.paths,
            globstar=globstar,
        ),
        *_forbidden_text_violations(
            root=root,
            rules=policy.forbidden_text,
            rel_files=listing.regular_files,
            globstar=globstar,
        ),
    )


def enforce_leak_check(
    *,
    root: Path,
    config: WorkflowConfig,
) -> None:
    """Raise when a transformed tree violates the workflow's leak-check policy.

    Args:
      root: Transformed export tree.
      config: Workflow whose leak policy, excludes, and moves apply.

    """
    violations = check_leaks(
        root=root,
        policy=config.leak_check,
        files=config.files,
        transforms=config.transforms,
        globstar=config.globstar,
    )
    if violations:
        lines = "\n".join(violation.format() for violation in violations)
        raise LeakCheckError(f"Leak check failed:\n{lines}")


@dataclass(frozen=True, slots=True, kw_only=True)
class _TreeListing:
    """Root-relative POSIX paths below an export root, listed once for all rules."""

    paths: tuple[str, ...]

    regular_files: tuple[str, ...]


# ``os.scandir`` on strings, not ``rglob`` + ``lstat``: the directory entries already
# carry the file type, so no per-path stat is needed. Listing the 4,785-file priml
# export dropped from 0.41s to 0.02s. Sorting by path segment keeps the order
# ``sorted(Path)`` produced, so violation order is unchanged.
def _list_tree(root: Path) -> _TreeListing:
    """Walk `root` once in deterministic order and split out the regular files."""
    prefix = len(str(root)) + 1
    entries: list[tuple[str, bool]] = []
    pending = [str(root)]
    while pending:
        with os.scandir(pending.pop()) as scan:
            for entry in scan:
                rel = entry.path[prefix:].replace(os.sep, "/")
                # pragma: no mutate start -- follow_symlinks=None is falsy too.
                is_file = entry.is_file(follow_symlinks=False)
                is_dir = entry.is_dir(follow_symlinks=False)
                # pragma: no mutate end
                entries.append((rel, is_file))
                if is_dir:
                    pending.append(entry.path)
    entries.sort(key=_entry_segments)
    return _TreeListing(
        paths=tuple(rel for rel, _ in entries),
        regular_files=tuple(rel for rel, is_regular in entries if is_regular),
    )


def _entry_segments(entry: tuple[str, bool]) -> list[str]:
    return entry[0].split("/")


# Excludes are source-root-relative, so only a path a ``files.moves`` entry placed is in
# their space. An unmoved path is a ``[[files.copy]]`` destination (``.export/uv.lock``
# at the root, ``typings/<pkg>``), and judging it would forbid the copy itself. With no
# moves the export root IS the source root, so every path is judged.
def _excluded_path_violations(
    *,
    files: FileSelection,
    transforms: tuple[Transform, ...],
    rel_paths: tuple[str, ...],
    globstar: Globstar,
) -> tuple[LeakViolation, ...]:
    """Return exported files whose source-root path ``files.exclude`` matches."""
    if not files.exclude:
        return ()
    excluded = GlobSet(include=files.exclude, globstar=globstar)
    violations: list[LeakViolation] = []
    for rel in rel_paths:
        staged = reverse_move_transforms(public_path=rel, transforms=transforms)
        source, moved = reverse_file_moves(staged, files.moves)
        if (moved or not files.moves) and excluded.matches(source):
            violations.append(
                LeakViolation(
                    rule_id="excluded-path",
                    path=rel,
                    message="files.exclude path was exported",
                ),
            )
    return tuple(violations)


def _forbidden_path_violations(
    *,
    rules: tuple[ForbiddenPathRule, ...],
    rel_paths: tuple[str, ...],
    globstar: Globstar,
) -> tuple[LeakViolation, ...]:
    """Return forbidden-path violations."""
    violations: list[LeakViolation] = []
    for rule in rules:
        matcher = GlobSet(include=rule.paths, globstar=globstar)
        violations.extend(
            LeakViolation(
                rule_id=rule.id,
                path=rel,
                message=rule.message or "forbidden path was exported",
            )
            for rel in rel_paths
            if matcher.matches(rel)
        )
    return tuple(violations)


# Each rule names its files by ``GlobSet``, so they are handed over by name and searched
# whatever ripgrep's own ignore rules say. ``text`` searches a file with a NUL byte too:
# a leak in one is still a leak.
def _forbidden_text_violations(
    *,
    root: Path,
    rules: tuple[ForbiddenTextRule, ...],
    rel_files: tuple[str, ...],
    globstar: Globstar,
) -> tuple[LeakViolation, ...]:
    """Return forbidden-text violations, one ripgrep search per rule, all at once."""
    # Every rule is its own ``rg`` process, so they overlap instead of queueing.
    # pragma: no mutate start -- the pool size changes speed, never the result.
    with ThreadPoolExecutor(max_workers=max(1, len(rules))) as pool:
        # pragma: no mutate end
        search = partial(
            _rule_violations,
            root=root,
            rel_files=rel_files,
            globstar=globstar,
        )
        found = list(pool.map(search, rules))
    return tuple(violation for violations in found for violation in violations)


def _rule_violations(
    rule: ForbiddenTextRule,
    /,
    *,
    root: Path,
    rel_files: tuple[str, ...],
    globstar: Globstar,
) -> list[LeakViolation]:
    """Return one rule's violations: the first matching line of each file."""
    matcher = GlobSet(include=rule.paths, exclude=rule.exclude, globstar=globstar)
    prefix = f"{root}/"
    names = {f"{prefix}{rel}": rel for rel in rel_files if matcher.matches(rel)}
    # Multiline, so a rule written with ``\s`` can match across lines as
    # Python ``re.search`` over the whole text did.
    query = Query(
        pattern=rule.pattern,
        output_mode="content",
        multiline=True,
        text=True,
    )
    try:
        rows = grep([Path(name) for name in names], query)
    except GrepError as error:
        raise LeakCheckError(
            f"Leak check rule {rule.id!r} could not run: {error}",
        ) from error
    first: dict[str, int] = {}
    for row in rows:
        name, line = _located(row, names=names)
        first.setdefault(name, line)
    return [
        LeakViolation(
            rule_id=rule.id,
            path=name,
            line=line,
            message=rule.message or "forbidden text matched",
        )
        for name, line in first.items()
    ]


# A path may itself hold ``:`` followed by digits, so the row is cut at the first
# colon whose prefix is a searched file, never at a pattern.
def _located(row: str, *, names: dict[str, str]) -> tuple[str, int]:
    """Split a ``path:line:text`` row into its root-relative path and line."""
    name = row.partition(":")[0]
    rest = row[len(name) + 1 :]
    while name not in names:
        head, _, rest = rest.partition(":")
        name = f"{name}:{head}"
    return names[name], int(rest.partition(":")[0])
