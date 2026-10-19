"""Render a package's ``pyproject.toml`` from a base file plus a delta.

The delta (``pyproject.delta.toml``) states only how the package differs from
the base, usually the source repository's root ``pyproject.toml``. Every
operation edits the base's lines in place; a line no operation names is kept as
written, so the render reads like a hand edit of the base.

Operations, by key spelling:

- ``key = value``: replace the value in place, or add the key at the end of its
  table. A ``[table]`` merges key by key into the base's table.
- ``"!key" = true``: delete the key or table, with the comment lines it owns.
  Beside ``key`` itself in the same delta, it replaces the table whole.
- ``"+key" = [...]``: append entries to a list, in the list's own layout;
  ``[["+key"]]`` appends a table to an array of tables.
- ``"-key" = [...]``: remove entries from a list by content; ``[["-key"]]``
  removes the first matching table, or inline table, from it.
- ``[[key]]`` with no operation states the array of tables whole: the base's
  elements go, the delta's take their place.
- ``"^anchor".key = value``: add ``key`` right after ``anchor`` in its table;
  ``"^".key`` adds it first.
- ``"=key"``: a key whose own name starts with one of ``+-!=^``.

Comments ride along. Each comment and blank line belongs to the key or table
below it, or closes the table above it when a blank line follows (see
``toml_lines``):

- The comment lines above a delta key or ``[header]`` replace the base's; the
  blank lines opening the base's lead stay, so the delta's own blank lines are
  only layout. With no comment, the base's lead stays. A new key or table takes
  the delta's lead as written; a new table with none gets one blank line.
- A ``# (clear)`` line as the last line above a delta key or ``[header]`` states
  the lead exactly: the lines before it, blank ones included, or none. Last, so
  it never opens the delta, where a comment and a blank line read as preamble.
  Above a ``[header]`` that follows another operation, the first blank line only
  separates the two and is not part of the lead.
- A replaced value takes the delta's trailing comment, or none. A ``[header]``
  keeps its trailing comment unless the delta's carries one; ``# (clear)`` there
  removes it.
- Comments directly under a delta table's last key, closed by a blank line,
  replace that table's closing comments; ending them with ``# (clear)`` states
  them exactly, a lone one removes them.
- The delta's opening comment block, closed by a blank line, replaces the file's
  preamble; a lone ``# (clear)`` there removes it. Its closing comments replace
  the footer. A delta of comments alone is a preamble when a blank line ends it,
  else a footer.

Its ``[pyproject]`` table holds settings: ``base`` (the base file), ``extends``
(shared deltas applied first, in order), and ``rename`` (text substitutions
applied to the document a layer receives, before its own operations, which are
therefore written in the renamed spelling). Paths are relative to the source
checkout root.

Export renders the file (``PyprojectSource.render``); import folds a public edit
back into the delta (``PyprojectSource.fold``), so only the delta is stored.

Comments are maintained in the source repository alone. A public edit may change
values; one that edits, adds, or removes a comment or blank line is rejected, so
its author proposes it in the source repository. A fold is checked by rendering
it: one that does not reproduce the public file is rejected rather than stored.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import itertools
import tomllib

from treekle import from_plain

from copybarista.config import load_config
from copybarista.delta_derive import derive
from copybarista.delta_render import SETTINGS, DeltaError, render
from copybarista.toml_layout import LayoutError, layout
from copybarista.toml_lines import Document, LinesError, parse


if TYPE_CHECKING:
    from pathlib import Path


@dataclass(frozen=True, slots=True, kw_only=True)
class PyprojectSource:
    """A delta and the shared document its extends chain renders it onto.

    Attributes:
      delta: The package's ``pyproject.delta.toml`` text.
      shared: The base with every ``extends`` layer and the delta's ``rename``
        applied: what the delta's operations edit.

    """

    delta: str
    shared: str

    @classmethod
    def load(cls, delta_path: Path, *, source_ref: Path) -> PyprojectSource:
        """Read ``delta_path``, its ``extends`` chain, and its base.

        Args:
          delta_path: The package's ``pyproject.delta.toml``.
          source_ref: Source checkout root the settings' paths are relative to.

        Returns:
          source: The delta and the shared document it applies to.

        Raises:
          DeltaError: A file is not UTF-8 TOML, or a layer edits what its base
            lacks.

        """
        delta = _read(delta_path)
        settings = _settings(delta, source=str(delta_path))
        document = _read(source_ref / from_plain(settings.get("base"), str))
        for name in from_plain(settings.get("extends"), list[str], default=[]):
            layer = _read(source_ref / name)
            document = render(_renamed(document, layer=layer, source=name), layer)
        shared = _renamed(document, layer=delta, source=str(delta_path))
        return cls(delta=delta, shared=shared)

    def render(self) -> str:
        """Return the package's ``pyproject.toml`` text."""
        return _one_newline(render(self.shared, self.delta))

    def fold(self, public: str) -> str:
        """Return the delta text that renders ``public``, settings kept.

        Args:
          public: An edited ``pyproject.toml``, such as a public contribution.

        Returns:
          delta: The new ``pyproject.delta.toml`` text.

        Raises:
          DeltaError: ``public`` is not TOML, carries the settings table, edits a
            comment or blank line, or holds a value edit the delta cannot express.

        """
        edited = _parsed(public, source="the public pyproject.toml")
        # Settings name files in the source checkout; a public edit may not
        # repoint them.
        if any(block.path[:1] == (SETTINGS,) for block in edited.blocks):
            raise DeltaError(
                f"The public pyproject.toml may not carry a [{SETTINGS}] table",
            )
        # Comments come from the monorepo alone. Folding a public comment edit would
        # need comment operations the delta lacks; dropping it would ship a merged
        # edit that the next export silently reverts. Rejecting it sends the
        # contributor to the monorepo instead.
        if not _same_comments(public, shipped=self.render()):
            raise DeltaError(
                "The public pyproject.toml edits a comment or blank line; comments "
                "are maintained in the source repository, so propose that change "
                "there.",
            )
        text = _with_settings(derive(self.shared, public), settings=self.delta)
        rendered = _one_newline(render(self.shared, text))
        if not same_render(rendered, public):
            raise DeltaError(
                "The folded delta does not reproduce the public pyproject.toml; "
                f"first difference at {_first_difference(rendered, public)}",
            )
        return text


def same_render(left: str, right: str) -> bool:
    """Return whether ``left`` and ``right`` differ only in spelling.

    Args:
      left: A ``pyproject.toml`` text.
      right: Another.

    Returns:
      same: Whether content, every comment in place, and the blank lines and
        indentation between lines agree; only spacing within a line, quoting,
        and line breaks inside a value may differ.

    Raises:
      DeltaError: Either text is not TOML.

    """
    try:
        return _content(left) == _content(right) and layout(left) == layout(right)
    except (tomllib.TOMLDecodeError, LayoutError) as err:
        raise DeltaError(f"Cannot parse a pyproject.toml: {err}") from err


def rendered_pyproject(config_path: Path, *, source_ref: Path) -> str:
    """Return the ``pyproject.toml`` a package's export ships.

    Args:
      config_path: The package's ``copy.barista.toml``.
      source_ref: Source checkout root.

    Returns:
      text: The rendered file, or ``""`` when the config renders none.

    """
    delta = load_config(config_path).files.pyproject
    if not delta:
        return ""
    return PyprojectSource.load(source_ref / delta, source_ref=source_ref).render()


def pyproject_inputs(config_path: Path, *, source_ref: Path) -> tuple[str, ...]:
    """Return the checkout-relative files a package's ``pyproject.toml`` renders from.

    Args:
      config_path: The package's ``copy.barista.toml``.
      source_ref: Source checkout root.

    Returns:
      paths: The delta, its base, and each ``extends`` layer; empty when the
        config renders none.

    """
    delta = load_config(config_path).files.pyproject
    if not delta:
        return ()
    settings = _settings(_read(source_ref / delta), source=delta)
    extends = from_plain(settings.get("extends"), list[str], default=[])
    return (delta, from_plain(settings.get("base"), str), *extends)


def _read(path: Path) -> str:
    """Read UTF-8 ``path``, reporting a decode error as a ``DeltaError``."""
    try:
        return path.read_bytes().decode()
    except UnicodeDecodeError as err:
        raise DeltaError(f"Cannot parse {path}: {err}") from err


def _settings(delta: str, *, source: str) -> dict[str, object]:
    """Return the delta's ``[pyproject]`` settings."""
    try:
        plain = tomllib.loads(delta)
    except tomllib.TOMLDecodeError as err:
        raise DeltaError(f"Cannot parse {source}: {err}") from err
    return from_plain(plain.get(SETTINGS), dict[str, object], default={})


def _renamed(document: str, *, layer: str, source: str) -> str:
    """Apply ``layer``'s ``[pyproject.rename]`` substitutions to ``document``."""
    rename = from_plain(
        _settings(layer, source=source).get("rename"),
        dict[str, str],
        default={},
    )
    for old, new in rename.items():
        document = document.replace(old, new)
    _parsed(document, source=f"the document after [{SETTINGS}.rename] {rename}")
    return document


# The settings follow the operations, as every delta file is written; the footer
# stays last.
def _with_settings(delta: str, *, settings: str) -> str:
    """Return ``delta`` with the ``[pyproject]`` tables of ``settings`` added."""
    own = _parsed(settings, source="the delta")
    tables = "".join(b.text() for b in own.blocks if b.path[:1] == (SETTINGS,))
    tables = tables.lstrip("\n")
    folded = _parsed(delta, source="the folded delta")
    body = "".join((*folded.preamble, *(b.text() for b in folded.blocks)))
    joiner = "\n" if body.strip() else ""
    return _one_newline(f"{body}{joiner}{tables}{''.join(folded.footer)}")


# Compared as a sequence, apart from the keys: adding or deleting a key changes none
# of it, while editing, adding, deleting, or moving a comment or blank line does. A
# key deleted along with the comment above it changes it, so that delete is refused.
def _same_comments(public: str, *, shipped: str) -> bool:
    """Return whether both files hold the same comments, blank lines, and indents."""
    return _comment_lines(public) == _comment_lines(shipped)


# Both texts were parsed by ``fold`` already, so neither raises here.
def _comment_lines(text: str) -> list[tuple[str, object]]:
    """Return ``text``'s comment, remark, blank, and indent events, in order."""
    kinds = {"comment", "remark", "blank", "indent"}
    return [(kind, what) for kind, what in layout(text) if kind in kinds]


def _one_newline(text: str) -> str:
    """End ``text`` with exactly one newline, as the end-of-file fixer requires."""
    return text.rstrip("\n") + "\n"


# NaN equals nothing, itself included, so a file holding one would never match its
# own copy.
def _content(text: str) -> dict[str, object]:
    """Return ``text``'s TOML content, every NaN read as the string ``"nan"``."""
    return from_plain(
        tomllib.loads(
            text,
            parse_float=lambda spelled: "nan" if "nan" in spelled else float(spelled),
        ),
        dict[str, object],
    )


# Spacing within a line never fails the check, so it never names the line either.
def _first_difference(rendered: str, public: str) -> str:
    """Return ``line N: public '...', render '...'`` for the first line that differs."""
    pairs = itertools.zip_longest(public.splitlines(), rendered.splitlines())
    number, (want, got) = next(
        (n, pair)
        for n, pair in enumerate(pairs, start=1)
        if "".join((pair[0] or "").split()) != "".join((pair[1] or "").split())
    )
    return f"line {number}: public {want or ''!r}, render {got or ''!r}"


def _parsed(text: str, *, source: str) -> Document:
    """Split ``text`` into lines, reporting a syntax error in ``source``."""
    try:
        return parse(text)
    except LinesError as err:
        raise DeltaError(f"Cannot parse {source}: {err}") from err
