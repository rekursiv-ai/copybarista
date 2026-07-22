"""Leak checks for transformed export trees.

The export pipeline already controls which files enter the public tree and
which transforms run. Leak checks are the final, read-only guard over that
transformed tree: they catch source-only paths, monorepo import names, private
markers, and similar release mistakes before any destination is mutated.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

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
        """Return a CI-safe violation message without echoing matched text."""
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
    return (
        *_forbidden_path_violations(
            root=root, rules=policy.forbidden_path, globstar=globstar
        ),
        *_forbidden_text_violations(
            root=root, rules=policy.forbidden_text, globstar=globstar
        ),
    )


def enforce_leak_check(
    *, root: Path, policy: LeakCheck, globstar: Globstar = "one_or_more"
) -> None:
    """Raise when a transformed tree violates leak-check policy."""
    violations = check_leaks(root=root, policy=policy, globstar=globstar)
    if violations:
        lines = "\n".join(violation.format() for violation in violations)
        raise LeakCheckError(f"Leak check failed:\n{lines}")


def _forbidden_path_violations(
    *, root: Path, rules: tuple[ForbiddenPathRule, ...], globstar: Globstar
) -> tuple[LeakViolation, ...]:
    """Return forbidden-path violations."""
    rel_paths = _relative_paths(root)
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
    *, root: Path, rules: tuple[ForbiddenTextRule, ...], globstar: Globstar
) -> tuple[LeakViolation, ...]:
    """Return forbidden-text violations."""
    rel_paths = _relative_paths(root)
    violations: list[LeakViolation] = []
    for rule in rules:
        matcher = GlobSet(include=rule.paths, exclude=rule.exclude, globstar=globstar)
        pattern = re.compile(rule.pattern, flags=re.MULTILINE)
        for rel in rel_paths:
            path = root / rel
            if path.is_symlink() or not path.is_file() or not matcher.matches(rel):
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            match = pattern.search(text)
            if match is None:
                continue
            violations.append(
                LeakViolation(
                    rule_id=rule.id,
                    path=rel,
                    line=text.count("\n", 0, match.start()) + 1,
                    message=rule.message or "forbidden text matched",
                )
            )
    return tuple(violations)


def _relative_paths(root: Path) -> tuple[str, ...]:
    """Return all paths below `root` in deterministic POSIX form."""
    return tuple(path.relative_to(root).as_posix() for path in sorted(root.rglob("*")))
