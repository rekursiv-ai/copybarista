"""Leak checks for transformed export trees.

The export pipeline already controls which files enter the public tree and
which transforms run. Leak checks are the final, read-only guard over that
transformed tree: they catch source-only paths, monorepo import names, private
markers, and similar release mistakes before any destination is mutated.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import os
import re

from copybarista.config import (
    ForbiddenPathRule,
    ForbiddenTextRule,
    LeakCheck,
)
from copybarista.errors import LeakCheckError
from copybarista.globs import GlobSet, Globstar


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
    *, root: Path, policy: LeakCheck, globstar: Globstar = "one_or_more"
) -> tuple[LeakViolation, ...]:
    """Return leak-check violations for a transformed tree.

    Args:
      root: Exported tree root to scan.
      policy: Leak-check rules from workflow config.
      globstar: Workflow ``**`` semantics for rule path globs.

    Returns:
      violations: Policy violations in deterministic order.

    Raises:
      LeakCheckError: If `root` is not a directory and policy has rules.

    """
    if not policy.forbidden_path and not policy.forbidden_text:
        return ()
    if not root.is_dir():
        raise LeakCheckError(f"Leak check root does not exist: {root}")
    listing = _list_tree(root)
    return (
        *_forbidden_path_violations(
            rules=policy.forbidden_path, rel_paths=listing.paths, globstar=globstar
        ),
        *_forbidden_text_violations(
            root=root,
            rules=policy.forbidden_text,
            rel_files=listing.regular_files,
            globstar=globstar,
        ),
    )


def enforce_leak_check(
    *, root: Path, policy: LeakCheck, globstar: Globstar = "one_or_more"
) -> None:
    """Raise when a transformed tree violates leak-check policy.

    Args:
      root: Root.
      policy: Policy.
      globstar: Globstar.

    """
    violations = check_leaks(root=root, policy=policy, globstar=globstar)
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
                entries.append((rel, entry.is_file(follow_symlinks=False)))
                if entry.is_dir(follow_symlinks=False):
                    pending.append(entry.path)
    entries.sort(key=_entry_segments)
    return _TreeListing(
        paths=tuple(rel for rel, _ in entries),
        regular_files=tuple(rel for rel, is_regular in entries if is_regular),
    )


def _entry_segments(entry: tuple[str, bool]) -> list[str]:
    return entry[0].split("/")


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


def _forbidden_text_violations(
    *,
    root: Path,
    rules: tuple[ForbiddenTextRule, ...],
    rel_files: tuple[str, ...],
    globstar: Globstar,
) -> tuple[LeakViolation, ...]:
    """Return forbidden-text violations, reading each matched file once."""
    texts: dict[str, str] = {}
    violations: list[LeakViolation] = []
    for rule in rules:
        matcher = GlobSet(include=rule.paths, exclude=rule.exclude, globstar=globstar)
        pattern = re.compile(rule.pattern, flags=re.MULTILINE)
        for rel in rel_files:
            if not matcher.matches(rel):
                continue
            if rel not in texts:
                texts[rel] = (root / rel).read_text(encoding="utf-8", errors="replace")
            match = pattern.search(texts[rel])
            if match is None:
                continue
            violations.append(
                LeakViolation(
                    rule_id=rule.id,
                    path=rel,
                    line=texts[rel].count("\n", 0, match.start()) + 1,
                    message=rule.message or "forbidden text matched",
                )
            )
    return tuple(violations)
