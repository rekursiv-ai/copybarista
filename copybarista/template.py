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


@dataclass(frozen=True, slots=True, kw_only=True)
class _Token:
    """One literal or interpolation segment of a template."""

    value: str
    is_group: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class ReplaceTemplate:
    """A compiled ``before`` pattern paired with its ``after`` renderer.

    Attributes:
      pattern: Compiled regex matching the ``before`` template.
      after_tokens: Parsed ``after`` template tokens, rendered per match.

    """

    pattern: re.Pattern[str]

    after_tokens: tuple[_Token, ...]

    # ``(sentinel_group, token_count)`` runs of ``after_tokens``: when set, only the
    # run whose sentinel group participated in the match is rendered. This is how
    # one compiled pattern carries two alternatives (``compile_module_replace``)
    # while ``apply``/``count`` stay a single ``re`` pass.
    alternatives: tuple[tuple[str, int], ...] = ()

    def apply(self, text: str) -> str:
        """Return ``text`` with every ``before`` match rendered as ``after``.

        Args:
          text: Text.

        Returns:
          result: The str.

        """
        return self.pattern.sub(self.render, text)

    def count(self, text: str) -> int:
        """Return how many non-overlapping ``before`` matches occur in ``text``.

        Args:
          text: Text.

        Returns:
          result: The int.

        """
        return sum(1 for _ in self.pattern.finditer(text))

    def render(self, match: re.Match[str]) -> str:
        """Render the ``after`` template for one ``before`` match.

        Args:
          match: A match of ``pattern``.

        Returns:
          text: The replacement for that match.

        """
        tokens = self.after_tokens
        start = 0
        for sentinel, count in self.alternatives:
            if match.group(sentinel) is not None:
                tokens = self.after_tokens[start : start + count]
                break
            start += count
        return "".join(
            match.group(token.value) if token.is_group else token.value
            for token in tokens
        )


def compile_replace(
    *,
    before: str,
    after: str,
    regex_groups: tuple[tuple[str, str], ...],
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
    _check_group_patterns(regex_groups)
    before_tokens = _parse(before)
    after_tokens = _parse(after)
    before_names = {t.value for t in before_tokens if t.is_group}
    after_names = {t.value for t in after_tokens if t.is_group}
    undefined = (before_names | after_names) - set(groups)
    if undefined:
        raise ConfigError(
            "replace references undefined regex_groups: "
            + ", ".join(sorted(undefined)),
        )
    if after_names - before_names:
        raise ConfigError(
            "replace after interpolates groups absent from before: "
            + ", ".join(sorted(after_names - before_names)),
        )
    unused = set(groups) - before_names
    if unused:
        raise ConfigError(
            "replace regex_groups never matched by before: "
            + ", ".join(sorted(unused)),
        )
    pattern = _build_pattern(tokens=before_tokens, groups=groups)
    return ReplaceTemplate(pattern=pattern, after_tokens=after_tokens)


# Python spells one module two ways: the dotted token (``import a.b.c``,
# ``from a.b.c import x``, ``lazy_import("a.b.c")``, ``a.b.c.attr``) and the
# ``from a.b import c`` form. A literal rule on the dotted token misses the second,
# so every shipping config carried a hand-written twin per leaf -- and the leaf nobody
# twinned shipped a monorepo import. Deriving both from ONE dotted path removes the
# per-leaf maintenance: ``parent`` and ``leaf`` are split here and each alternative
# is anchored so an identifier that merely starts with the leaf (``userdirs_fixture``)
# or ends with the parent (``my_a.b``) is left alone.
_MODULE_PATH = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+")


def compile_module_replace(*, before: str, after: str) -> ReplaceTemplate:
    """Compile a dotted-module rename that covers every import spelling.

    Args:
      before: Dotted source module path (``a.b.c``).
      after: Dotted public module path (``x.c``).

    Returns:
      template: Compiled template; ``apply`` rewrites the dotted token wherever
        it stands as a whole name and the ``from a.b import c`` form, including
        ``c`` inside a comma list or a parenthesised block.

    Raises:
      ConfigError: If either side is not a dotted module path.

    """
    for label, value in (("before", before), ("after", after)):
        if _MODULE_PATH.fullmatch(value) is None:
            raise ConfigError(
                f"module replace {label} must be a dotted module path: {value!r}",
            )
    before_parent, _, before_leaf = before.rpartition(".")
    after_parent, _, after_leaf = after.rpartition(".")
    # Two alternatives share one compiled pattern so ``re.sub`` walks the text once.
    # ``names`` absorbs everything between ``import`` and the leaf inside a comma
    # list or an open paren, so the leaf is matched as a whole word wherever it
    # sits in the list; a leaf that is a prefix of a longer name is not it.
    dotted = rf"(?P<dl>(?<![A-Za-z0-9_.])){re.escape(before)}(?P<dt>(?![A-Za-z0-9_]))"
    from_form = (
        rf"(?P<fl>(?<![A-Za-z0-9_.]))from[ \t]+{re.escape(before_parent)}"
        rf"(?P<fg1>[ \t]+)import(?P<fg2>[ \t]+)"
        rf"(?P<fn>\([^)]*?\b|(?:[A-Za-z_][A-Za-z0-9_]*[ \t]*,[ \t]*)*)"
        rf"{re.escape(before_leaf)}(?P<ft>(?![A-Za-z0-9_]))"
    )
    return ReplaceTemplate(
        pattern=re.compile(f"{dotted}|{from_form}"),
        after_tokens=(
            _Token(value="dl", is_group=True),
            _Token(value=after, is_group=False),
            _Token(value="dt", is_group=True),
            _Token(value="fl", is_group=True),
            _Token(value=f"from {after_parent}", is_group=False),
            _Token(value="fg1", is_group=True),
            _Token(value="import", is_group=False),
            _Token(value="fg2", is_group=True),
            _Token(value="fn", is_group=True),
            _Token(value=after_leaf, is_group=False),
            _Token(value="ft", is_group=True),
        ),
        alternatives=(("dl", 3), ("fl", 8)),
    )


def replace_template(
    *,
    before: str,
    after: str,
    regex_groups: tuple[tuple[str, str], ...],
    module: bool,
) -> ReplaceTemplate | None:
    """Return the compiled template for a ``replace``, or ``None`` for a literal.

    One dispatch shared by export, reverse import, and PR-text rewriting, so a
    new replace flavour is honoured everywhere or nowhere.

    Args:
      before: Transform ``before`` text.
      after: Transform ``after`` text.
      regex_groups: Transform ``regex_groups`` bindings.
      module: Transform ``module`` flag.

    Returns:
      template: A compiled template, or ``None`` when ``str.replace`` is exact.

    """
    if module:
        return compile_module_replace(before=before, after=after)
    if regex_groups:
        return compile_replace(before=before, after=after, regex_groups=regex_groups)
    return None


def literal_segments(template: str, *, separator: str) -> str:
    """Return a template's literal text with interpolations replaced.

    Each ``${name}`` is replaced by ``separator`` so callers that reason about
    the literal skeleton -- e.g. recovering marker lines from a
    ``regex_groups`` replacement -- share this module's interpolation grammar
    instead of restating it. A second copy of the pattern would silently stop
    agreeing the moment the grammar here changed.

    Args:
      template: Template string with ${name} interpolations.
      separator: String to replace each interpolation with.

    Returns:
      skeleton: Literal segments joined by separator (no interpolation groups).

    """
    return separator.join(
        token.value for token in _parse(template) if not token.is_group
    )


def _parse(template: str) -> tuple[_Token, ...]:
    """Split a template into literal and interpolation tokens."""
    tokens: list[_Token] = []
    cursor = 0
    for match in re.finditer(r"\$\{(?P<name>[A-Za-z_][A-Za-z0-9_]*)\}", template):
        if match.start() > cursor:
            tokens.append(
                _Token(value=template[cursor : match.start()], is_group=False),
            )
        tokens.append(_Token(value=match.group("name"), is_group=True))
        cursor = match.end()
    if cursor < len(template):
        tokens.append(_Token(value=template[cursor:], is_group=False))
    return tuple(tokens)


# Compiling only the ASSEMBLED pattern is not sufficient: the groups are concatenated,
# so a malformed one can be re-balanced by whatever follows it. ``[unclosed`` stays an
# open character class that swallows text until a later group closes it, and ``(grp`` is
# closed by a later ``)`` -- both compile as a pair while each raises alone, yielding a
# regex that means nothing like what was written. Copybara validates per group and
# refuses to load the config, so a masked group here admits a ``.sky`` that cannot run
# there.
def _check_group_patterns(regex_groups: tuple[tuple[str, str], ...]) -> None:
    """Reject a group whose regex is invalid on its own."""
    for name, pattern in regex_groups:
        try:
            re.compile(pattern)
        except re.error as err:
            raise ConfigError(
                f"replace regex_groups.{name} is not valid regex: {pattern}",
            ) from err


def _build_pattern(
    *,
    tokens: tuple[_Token, ...],
    groups: dict[str, str],
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
            f"replace regex_groups produce an invalid pattern: {err}",
        ) from err
