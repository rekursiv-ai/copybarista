"""Regex-group replacement templates, mirroring Copybara's ``core.replace``.

A ``replace`` transform is two templates plus a mapping of interpolation names
to regular expressions. Literal text in a template is matched verbatim;
``${name}`` interpolations match the named group's regex on the ``before`` side
and re-emit the captured text on the ``after`` side. The same machinery runs in
both directions: reversing a transform swaps ``before`` and ``after``, so the
regex anchoring carries over symmetrically (Copybara ``Replace.reverse()``).

This is what makes a non-injective literal rewrite (e.g. a long internal
namespace collapsed to a short public package name) safely reversible: the
boundary that identifies a real module token -- not an identifier substring or
a dotfile -- is declared once in the group regex and applies to forward and
reverse alike.
"""

from __future__ import annotations

from dataclasses import dataclass

import re

from copybarista.errors import ConfigError


_INTERPOLATION = re.compile(r"\$\{(?P<name>[A-Za-z_][A-Za-z0-9_]*)\}")


@dataclass(frozen=True, slots=True, kw_only=True)
class ReplaceTemplate:
    """A compiled ``before`` pattern paired with its ``after`` renderer.

    Attributes:
      pattern: Compiled regex matching the ``before`` template.
      after_tokens: Parsed ``after`` template tokens, rendered per match.

    """

    pattern: re.Pattern[str]
    after_tokens: tuple[_Token, ...]

    def apply(self, text: str) -> str:
        """Return ``text`` with every ``before`` match rendered as ``after``."""
        return self.pattern.sub(self._render, text)

    def count(self, text: str) -> int:
        """Return how many non-overlapping ``before`` matches occur in ``text``."""
        return sum(1 for _ in self.pattern.finditer(text))

    def _render(self, match: re.Match[str]) -> str:
        """Render the ``after`` template for one ``before`` match."""
        return "".join(
            match.group(token.value) if token.is_group else token.value
            for token in self.after_tokens
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class _Token:
    """One literal or interpolation segment of a template."""

    value: str
    is_group: bool


def compile_replace(
    *, before: str, after: str, regex_groups: tuple[tuple[str, str], ...]
) -> ReplaceTemplate:
    """Compile a replacement template pair, mirroring Copybara semantics.

    Args:
      before: Template matched against source text. Literal segments match
        verbatim; ``${name}`` interpolations match the named group's regex.
      after: Template rendered for each match. ``${name}`` interpolations
        re-emit the captured group text.
      regex_groups: Ordered ``(name, pattern)`` pairs binding interpolation
        names to regular expressions.

    Returns:
      template: Compiled template usable in either direction.

    Raises:
      ConfigError: If a group regex is invalid or a template references an
        undefined or unused interpolation name.

    """
    groups = dict(regex_groups)
    if len(groups) != len(regex_groups):
        raise ConfigError("replace regex_groups names must be unique")
    before_tokens = _parse(before)
    after_tokens = _parse(after)
    before_names = {t.value for t in before_tokens if t.is_group}
    after_names = {t.value for t in after_tokens if t.is_group}
    undefined = (before_names | after_names) - set(groups)
    if undefined:
        raise ConfigError(
            "replace references undefined regex_groups: " + ", ".join(sorted(undefined))
        )
    if not after_names <= before_names:
        raise ConfigError(
            "replace after interpolates groups absent from before: "
            + ", ".join(sorted(after_names - before_names))
        )
    unused = set(groups) - before_names
    if unused:
        raise ConfigError(
            "replace regex_groups never matched by before: " + ", ".join(sorted(unused))
        )
    pattern = _build_pattern(tokens=before_tokens, groups=groups)
    return ReplaceTemplate(pattern=pattern, after_tokens=after_tokens)


def _build_pattern(
    *, tokens: tuple[_Token, ...], groups: dict[str, str]
) -> re.Pattern[str]:
    """Compile a ``before`` token sequence into a single regex."""
    parts: list[str] = []
    for token in tokens:
        if token.is_group:
            parts.append(f"(?P<{token.value}>{groups[token.value]})")
        else:
            parts.append(re.escape(token.value))
    try:
        return re.compile("".join(parts))
    except re.error as err:
        raise ConfigError(
            f"replace regex_groups produce an invalid pattern: {err}"
        ) from err


def _parse(template: str) -> tuple[_Token, ...]:
    """Split a template into literal and interpolation tokens."""
    tokens: list[_Token] = []
    cursor = 0
    for match in _INTERPOLATION.finditer(template):
        if match.start() > cursor:
            tokens.append(
                _Token(value=template[cursor : match.start()], is_group=False)
            )
        tokens.append(_Token(value=match.group("name"), is_group=True))
        cursor = match.end()
    if cursor < len(template):
        tokens.append(_Token(value=template[cursor:], is_group=False))
    return tuple(tokens)
