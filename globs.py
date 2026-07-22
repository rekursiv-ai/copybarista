"""Glob matching for export file selection.

The matcher accepts `*`, `**`, `?`, brace alternation, character classes, and
backslash-escaped literal characters. This Java-style subset keeps native TOML
and supported `copy.bara.sky` workflows on one matching engine. Unsupported or
unsafe path forms are rejected during config validation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Literal

import re

from copybarista.errors import GlobError


Globstar = Literal["zero_or_more", "one_or_more"]
"""Number of path segments matched by ``**`` between separators.

``one_or_more`` mirrors Java ``PathMatcher`` and Copybara: ``**/foo`` requires
at least one directory ahead of ``foo``. ``zero_or_more`` mirrors bash globstar,
gitignore, and Python ``glob.glob(..., recursive=True)``: ``**/foo`` also
matches a root-level ``foo``.
"""


@dataclass(frozen=True, slots=True, kw_only=True)
class GlobSet:
    """Include/exclude matcher over normalized POSIX relative paths.

    Paths are included when they match at least one include pattern and no
    exclude pattern. Compiled regexes are cached on the instance because file
    selection checks many paths against the same pattern set. Syntax policy is
    carried by the instance so tests and future config loaders can tighten or
    relax specific parser rules without module-level state.

    Attributes:
      include: Patterns that select paths.
      exclude: Patterns that remove paths after inclusion.
      globstar: Whether ``**`` between separators matches zero or one or more
        segments. Defaults to ``one_or_more`` for Copybara parity.
      min_brace_choices: Minimum choices required in `{a,b}` alternation.

    """

    include: tuple[str, ...]
    exclude: tuple[str, ...] = ()
    globstar: Globstar = "one_or_more"
    min_brace_choices: int = 2
    _include_regex: tuple[re.Pattern[str], ...] = field(init=False, repr=False)
    _exclude_regex: tuple[re.Pattern[str], ...] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        """Compile patterns once so repeated path checks stay cheap."""
        object.__setattr__(
            self,
            "_include_regex",
            _compile_all(
                self.include,
                globstar=self.globstar,
                min_brace_choices=self.min_brace_choices,
            ),
        )
        object.__setattr__(
            self,
            "_exclude_regex",
            _compile_all(
                self.exclude,
                globstar=self.globstar,
                min_brace_choices=self.min_brace_choices,
            ),
        )

    def matches(self, path: str) -> bool:
        """Return whether a path is included and not excluded."""
        normalized = path.replace("\\", "/").strip("/")
        return any(r.fullmatch(normalized) for r in self._include_regex) and not any(
            r.fullmatch(normalized) for r in self._exclude_regex
        )


def _compile_all(
    patterns: tuple[str, ...], *, globstar: Globstar, min_brace_choices: int
) -> tuple[re.Pattern[str], ...]:
    """Compile supported glob patterns to full-match regexes."""
    return tuple(
        re.compile(
            _glob_to_regex(
                pattern, globstar=globstar, min_brace_choices=min_brace_choices
            )
        )
        for pattern in patterns
    )


def validate_pattern(pattern: str) -> str:
    """Validate one supported glob pattern.

    Rejecting unsupported patterns at config load time is safer than treating
    them as literals and exporting the wrong file set.
    """
    if not pattern:
        raise GlobError("Glob pattern must not be empty")
    path = PurePosixPath(pattern)
    if path.is_absolute() or ".." in path.parts:
        raise GlobError(f"Glob pattern must be relative without '..': {pattern}")
    _check_balanced(pattern=pattern, left="[", right="]")
    _check_balanced(pattern=pattern, left="{", right="}")
    return pattern.strip("/")


def _check_balanced(*, pattern: str, left: str, right: str) -> None:
    """Reject unbalanced bracket or brace syntax."""
    depth = 0
    escaped = False
    for char in pattern:
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == left:
            depth += 1
        elif char == right:
            depth -= 1
        if depth < 0:
            raise GlobError(f"Unbalanced glob syntax in pattern: {pattern}")
    if depth != 0:
        raise GlobError(f"Unbalanced glob syntax in pattern: {pattern}")


def _glob_to_regex(
    pattern: str,
    *,
    globstar: Globstar = "one_or_more",
    min_brace_choices: int = 2,
) -> str:
    """Translate the supported glob subset to a Python regex.

    The returned regex intentionally has no anchors because callers use
    `fullmatch`, which keeps the translation focused on path component rules.
    """
    pattern = validate_pattern(pattern)
    globstar_slash = ".+/" if globstar == "one_or_more" else "(?:.+/)?"
    parts: list[str] = []
    idx = 0
    while idx < len(pattern):
        char = pattern[idx]
        if char == "*":
            if idx + 1 < len(pattern) and pattern[idx + 1] == "*":
                if idx + 2 < len(pattern) and pattern[idx + 2] == "/":
                    parts.append(globstar_slash)
                    idx += 3
                else:
                    parts.append(".*")
                    idx += 2
            else:
                parts.append("[^/]*")
                idx += 1
        elif char == "?":
            parts.append("[^/]")
            idx += 1
        elif char == "[":
            class_regex, idx = _char_class_regex(pattern=pattern, start=idx)
            parts.append(class_regex)
        elif char == "{":
            brace_regex, idx = _brace_regex(
                pattern=pattern,
                start=idx,
                globstar=globstar,
                min_brace_choices=min_brace_choices,
            )
            parts.append(brace_regex)
        elif char == "\\" and idx + 1 < len(pattern):
            parts.append(re.escape(pattern[idx + 1]))
            idx += 2
        else:
            parts.append(re.escape(char))
            idx += 1
    return "".join(parts)


def _char_class_regex(*, pattern: str, start: int) -> tuple[str, int]:
    """Translate one supported character class."""
    end = pattern.find("]", start + 1)
    if end < 0:
        raise GlobError(f"Unbalanced glob syntax in pattern: {pattern}")
    raw = pattern[start + 1 : end]
    if not raw:
        raise GlobError(f"Empty character class in glob pattern: {pattern}")
    negated = raw.startswith("!")
    content = raw[1:] if negated else raw
    if not content or "/" in content:
        raise GlobError(f"Invalid character class in glob pattern: {pattern}")
    prefix = "^" if negated else ""
    return f"[{prefix}{_escape_char_class(content)}]", end + 1


def _escape_char_class(content: str) -> str:
    """Escape character-class content while preserving simple ranges."""
    escaped: list[str] = []
    for idx, char in enumerate(content):
        if char == "-" and 0 < idx < len(content) - 1:
            escaped.append("-")
        else:
            escaped.append(re.escape(char))
    return "".join(escaped)


def _brace_regex(
    *, pattern: str, start: int, globstar: Globstar, min_brace_choices: int
) -> tuple[str, int]:
    """Translate one supported brace alternation."""
    end = pattern.find("}", start + 1)
    if end < 0:
        raise GlobError(f"Unbalanced glob syntax in pattern: {pattern}")
    choices = pattern[start + 1 : end].split(",")
    if len(choices) < min_brace_choices or any(choice == "" for choice in choices):
        raise GlobError(f"Invalid brace alternation in glob pattern: {pattern}")
    return (
        "(?:"
        + "|".join(
            _glob_to_regex(
                choice, globstar=globstar, min_brace_choices=min_brace_choices
            )
            for choice in choices
        )
        + ")",
        end + 1,
    )
