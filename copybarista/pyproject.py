"""Render a package's ``pyproject.toml`` from a base file plus a delta.

The delta (``pyproject.delta.toml``) states only how the package differs from
the base, usually the source repository's root ``pyproject.toml``:

- ``"+key" = [...]`` appends entries to a list, comments included.
- ``"-key" = [...]`` removes entries from a list.
- ``"!key" = true`` deletes the key or table outright.
- ``key = value`` replaces a value; a table merges key by key.
- ``"=key" = value`` sets a key whose own name starts with ``+``, ``-``, ``!`` or
  ``=`` (a negated ruff glob such as ``"!**/keep/**"``).

Comments ride along. A replaced value takes the delta's comment lines above
it, or keeps the base's when the delta has none; a ``# (clear)`` line there
clears them. The same holds for a table header and the file's first and last
lines.

Its ``[pyproject]`` table holds settings: ``base`` (the base file),
``extends`` (shared deltas applied first, in order), and ``rename`` (text
substitutions such as ``src/stubs`` to ``stubs``). A layer's rename
applies to the document it receives, before its own operations, so those are
written in the renamed spelling.
Paths are relative to the source checkout root.

Export renders the file (``render``); import folds a public edit back into the
delta (``derive``), so only the delta is stored.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, cast

import copy
import itertools

from tomlkit.container import Container, OutOfOrderTableProxy
from tomlkit.exceptions import TOMLKitError
from tomlkit.items import (
    AoT,
    Array,
    Comment,
    InlineTable,
    Item,
    Key,
    Null,
    Table,
    Trivia,
    Whitespace,
    _ArrayItemGroup,
)
from treekle import from_plain

import tomlkit

from copybarista.config import load_config


if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path


SETTINGS: Final = "pyproject"

type _Node = Container | Table | OutOfOrderTableProxy


class DeltaError(ValueError):
    """A delta, its base, or a public edit cannot be rendered or folded."""


def render(base: tomlkit.TOMLDocument, delta: _Node) -> tomlkit.TOMLDocument:
    """Return a copy of ``base`` with ``delta``'s operations applied.

    Args:
      base: The document to start from; left unmodified.
      delta: ``+key``, ``-key``, ``!key``, and ``key = value`` operations. Its
        top-level ``[pyproject]`` settings table is skipped.

    Returns:
      document: The rendered document, comments and layout preserved.

    Raises:
      DeltaError: The delta removes an entry or key ``base`` lacks.

    """
    document = _normalized(base)
    delta = _normalized(delta)
    # Over a base with keys, comments a delta holds with no operation and no closing
    # blank line can only be the footer (``derive`` closes a preamble with one); over
    # a key-less base everything is preamble.
    footer_only = (
        _has_key(document)
        and not _has_key(delta)
        and _comment_run_end(delta.body) == len(delta.body)
    )
    preamble = None if footer_only else _wanted(_delta_preamble(delta))
    # The footer ends whatever body renders last, which an edit may delete or follow
    # with a new table; held apart, it goes back at the end.
    footer = _wanted(
        _run((delta, 0, len(delta.body))) if footer_only else _run(_footer_site(delta)),
    )
    if footer is None:
        footer = _run(_footer_site(document))
    _set_run(_footer_site(document), lines=())
    before = copy.deepcopy(document)
    _apply(document, delta=delta, path="")
    _prune(document, delta=delta, before=before)
    if preamble is not None:
        _set_run(_preamble_site(document), lines=preamble)
    _set_run(_footer_site(document), lines=footer)
    _strip_leading_blanks(document)
    return document


# Blank lines separate what is above from what is below, so the file's first line is
# never one: an edit that deletes or clears the top hands its separation to nothing.
# The preamble carries the blank line that closes it, so a first header under it
# needs none of its own.
def _strip_leading_blanks(document: tomlkit.TOMLDocument) -> None:
    """Drop the blank lines that would open ``document`` or double the preamble's."""
    body = document.body
    start = 0
    while start < len(body) and isinstance(body[start][1], Whitespace | Null):
        start += 1
    del body[:start]
    _reindex(document)
    head = list(itertools.takewhile(lambda entry: entry[0] is None, body))
    above = [v for _, v in head if not isinstance(v, Null)]
    first = body[len(head)][1] if len(head) < len(body) else None
    if above and not isinstance(above[-1], Whitespace):
        return  # A preamble with no closing blank line keeps the header's.
    if (header := _first_rendered_header(first)) is not None:
        header.trivia.indent = header.trivia.indent.lstrip("\n")


def _first_rendered_header(item: object) -> Table | None:
    """Return the header line ``item`` renders first, descending header-less tables."""
    if (header := _header_of(item)) is not None:
        return header
    if isinstance(item, Table):
        return _first_rendered_header(
            next((v for k, v in item.value.body if k is not None), None),
        )
    return None


# A delta's own first lines, before a blank line, are the file's preamble: ``derive``
# always writes that blank line, so even a delta that opens with a plain operation
# (``"!a" = true``) keeps its preamble apart from that operation's lead.
def _delta_preamble(delta: Container) -> tuple[str, ...]:
    """Return the comment lines atop ``delta`` that a blank line ends, and drop them."""
    body = delta.body
    end = _comment_run_end(body)
    if end and end < len(body) and isinstance(body[end][1], Whitespace):
        lines = _run(
            (delta, 0, end + 1),
        )  # The blank line separates it from what follows.
        del body[: end + 1]
        _reindex(delta)
        return lines
    return _run(_preamble_site(delta))


def _has_key(document: Container) -> bool:
    return any(key is not None for key, _ in document.body)


def _comment_run_end(body: list[tuple[Key | None, Item]]) -> int:
    """Return the index after the unkeyed comments that open ``body``."""
    end = 0
    while (
        end < len(body) and body[end][0] is None and isinstance(body[end][1], Comment)
    ):
        end += 1
    return end


def derive(
    base: tomlkit.TOMLDocument,
    target: tomlkit.TOMLDocument,
) -> tomlkit.TOMLDocument:
    """Return the delta that renders ``target`` from ``base``.

    Lists become ``-key`` and ``+key`` entry operations when the entries both keep
    stay in order, tables are compared key by key, and every other change
    replaces the value. Added entries and values carry their comments.

    Args:
      base: The document the delta applies to.
      target: The document the delta must produce.

    Returns:
      delta: Operations satisfying ``render(base, delta) == target``.

    """
    delta = tomlkit.document()
    base, target = _normalized(base), _normalized(target)
    _derive_into(delta, base=base, target=target)
    _derive_run(delta, base=base, target=target, site=_footer_site)
    _derive_run(delta, base=base, target=target, site=_preamble_site)
    return delta


def same_render(left: str, right: str) -> bool:
    """Return whether ``left`` and ``right`` differ only in spelling.

    Args:
      left: A ``pyproject.toml`` text.
      right: Another.

    Returns:
      same: Whether a delta from either to the other would be empty, so content
        and every comment, in place, agree.

    Raises:
      DeltaError: Either text is not TOML.

    """
    first = _parse(left, source="a pyproject.toml")
    second = _parse(right, source="a pyproject.toml")
    # Text, not truthiness: a delta changing only the preamble or footer has no key.
    return not tomlkit.dumps(derive(first, second)).strip() and not (
        tomlkit.dumps(derive(second, first)).strip()
    )


def _derive_run(
    delta: tomlkit.TOMLDocument,
    *,
    base: Container,
    target: Container,
    site: _Site,
) -> None:
    """Write ``target``'s run at ``site`` into ``delta`` when it differs from ``base``'s."""
    lines = _run(site(target))
    if _comments(lines) == _comments(_run(site(base))):
        return
    if not _comments(lines):
        lines = (_CLEAR,)
    if site is _preamble_site and lines[-1].strip():
        lines = (*lines, "\n")  # A blank line keeps it off the first header's lead.
    _set_run(site(delta), lines=lines)


@dataclass(frozen=True, slots=True, kw_only=True)
class PyprojectSource:
    """A delta and every file its ``pyproject.toml`` is rendered from."""

    delta: tomlkit.TOMLDocument
    settings: Table
    shared: tomlkit.TOMLDocument

    @classmethod
    def load(cls, delta_path: Path, *, source_ref: Path) -> PyprojectSource:
        """Read ``delta_path``, its ``extends`` chain, and its base.

        Args:
          delta_path: The package's ``pyproject.delta.toml``.
          source_ref: Source checkout root the settings' paths are relative to.

        Returns:
          source: The delta and the shared base it applies to.

        """
        delta = _parse_file(delta_path)
        found = _get(delta, key=SETTINGS)
        settings = found if isinstance(found, Table) else tomlkit.table()
        plain = from_plain(settings.unwrap(), dict[str, object])
        document = _parse_file(source_ref / from_plain(plain.get("base"), str))
        # Each layer's ``rename`` rewrites the document it receives, so its own
        # operations are written in the renamed spelling.
        layers = [
            _parse_file(source_ref / name)
            for name in from_plain(plain.get("extends"), list[str], default=[])
        ]
        for layer in layers:
            document = render(_renamed(document, layer=layer), layer)
        document = _renamed(document, layer=delta)
        return cls(delta=delta, settings=settings, shared=document)

    def render(self) -> str:
        """Return the package's ``pyproject.toml`` text."""
        return _one_newline(tomlkit.dumps(render(self.shared, self.delta)))

    def fold(self, public: str) -> str:
        """Return the delta text that renders ``public``, settings kept.

        Args:
          public: An edited ``pyproject.toml``, such as a public contribution.

        Returns:
          delta: The new ``pyproject.delta.toml`` text.

        """
        edited = _parse(public, source="the public pyproject.toml")
        # Settings name files in the source checkout; a public edit may not
        # repoint them.
        if SETTINGS in edited:
            raise DeltaError(
                f"The public pyproject.toml may not carry a [{SETTINGS}] table",
            )
        folded = derive(self.shared, edited)
        # The footer must stay last, after the settings table.
        footer = _run(_footer_site(folded))
        _set_run(_footer_site(folded), lines=())
        folded.append(SETTINGS, copy.deepcopy(self.settings))
        _set_run(_footer_site(folded), lines=footer)
        return _one_newline(tomlkit.dumps(folded))


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
    found = _get(_parse_file(source_ref / delta), key=SETTINGS)
    plain = from_plain(_plain(found), dict[str, object], default={})
    extends = from_plain(plain.get("extends"), list[str], default=[])
    return (delta, from_plain(plain.get("base"), str), *extends)


def _parse_file(path: Path) -> tomlkit.TOMLDocument:
    """Parse the TOML file at ``path``, reporting a syntax error as a ``DeltaError``."""
    try:
        text = path.read_bytes().decode()
    except UnicodeDecodeError as err:
        raise DeltaError(f"Cannot parse {path}: {err}") from err
    return _parse(text, source=str(path))


def _parse(text: str, *, source: str) -> tomlkit.TOMLDocument:
    """Parse ``text``, reporting a syntax error in ``source`` as a ``DeltaError``."""
    try:
        return tomlkit.parse(text)
    except TOMLKitError as err:
        raise DeltaError(f"Cannot parse {source}: {err}") from err


def _one_newline(text: str) -> str:
    """End ``text`` with exactly one newline, as the end-of-file fixer requires."""
    return text.rstrip("\n") + "\n"


def _apply(target: _Node, *, delta: _Node, path: str) -> None:
    """Apply each operation in ``delta`` to ``target``, in delta order."""
    for raw_key, operand in _items(delta):
        if not path and raw_key == SETTINGS:
            continue
        key, op = _split(raw_key)
        where = f"{path}.{key}" if path else key
        # ``"!key"`` beside ``key`` replaces the value whole instead of merging into it.
        whole = op == "=" and f"!{key}" in delta
        if op == "!":
            if key not in target:
                raise DeltaError(f"delta deletes {where!r}, which the base lacks")
            if _literal(key) not in delta:
                _drop_key(target, key=key)
        elif op == "-":
            _remove(target, key=key, operand=operand, where=where)
        elif op == "+":
            _append(target, key=key, operand=operand, where=where)
        elif (
            not whole
            and isinstance(operand, Table)
            and _is_table(child := _get(target, key=key))
        ):
            # A merged table keeps its header comment unless the delta gives one.
            if (lead := _wanted(_lead(delta, key=raw_key))) is not None:
                _set_lead(target, key=key, lead=lead)
            _apply(cast(_Node, child), delta=operand, path=where)
        else:
            # A replacement keeps the comment above the key unless the delta gives one.
            lead = _wanted(_lead(delta, key=raw_key))
            if key in target:
                kept = _lead(target, key=key) if lead is None else lead
                _set_lead(target, key=key, lead=())
                _assign(target, key=key, value=_detached(operand))
            else:
                kept = lead or ()
                _insert(target, key=key, value=_detached(operand))
            _set_lead(target, key=key, lead=kept)
            _set_remark(_get(target, key=key), like=operand)


# ``tomlkit`` keeps the old value's trailing comment when the new one has none, and for
# a table that comment ends its ``[header]`` line.
def _set_remark(item: object, *, like: object) -> None:
    """Give ``item`` the trailing comment ``like`` has, or none."""
    header = _header_of(item)
    if header is not None:
        source = _header_of(like)
        header.trivia.comment_ws = "" if source is None else source.trivia.comment_ws
        header.trivia.comment = "" if source is None else source.trivia.comment
    elif isinstance(item, Item) and isinstance(like, Item) and not _is_table(item):
        item.trivia.comment_ws = like.trivia.comment_ws
        item.trivia.comment = like.trivia.comment


# Only tables the delta emptied: an empty table the base carries, one the delta adds
# empty, or one it replaces whole (``"!key"`` beside ``key``), stays.
def _prune(target: _Node, *, delta: _Node, before: _Node) -> None:
    """Delete each table the delta emptied, deepest first."""
    for raw_key, operand in _items(delta):
        key, op = _split(raw_key)
        if op != "=" or not isinstance(operand, Table) or f"!{key}" in delta:
            continue
        child = _get(target, key=key)
        was = _get(before, key=key)
        if not _is_table(child) or not _is_table(was):
            continue
        _prune(cast(_Node, child), delta=operand, before=cast(_Node, was))
        if not _plain(child) and _plain(was):
            _drop_key(target, key=key)


def _remove(target: _Node, *, key: str, operand: object, where: str) -> None:
    """Remove the listed entries from ``key``'s list."""
    if key not in target:
        raise DeltaError(f"delta removes from {where!r}, which the base lacks")
    current = _get(target, key=key)
    if not isinstance(current, Array | AoT):
        raise DeltaError(f"delta removes entries from {where!r}, which is not a list")
    if not isinstance(operand, Array | AoT):
        raise DeltaError(f"delta removes a non-list from {where!r}")
    for entry in [_plain(value) for value in _elements(operand)]:
        values = [_plain(value) for value in _elements(current)]
        if entry not in values:
            raise DeltaError(
                f"delta removes {entry!r} from {where}, which lacks it",
            )
        if isinstance(current, Array):
            _drop_entry(current, index=values.index(entry))
        else:
            del current[values.index(entry)]


def _append(target: _Node, *, key: str, operand: object, where: str) -> None:
    """Append ``operand``'s entries, with their comments, to ``key``'s list."""
    if not isinstance(operand, Array | AoT):
        raise DeltaError(f"delta appends a non-list to {where!r}")
    current = _get(target, key=key)
    if current is None:
        _assign(target, key=key, value=copy.deepcopy(operand))
    elif isinstance(current, AoT):
        for table in _tables(operand, where=where):
            current.insert(len(current), table)
    elif isinstance(current, Array) and isinstance(operand, Array):
        _extend(current, extra=operand)
    else:
        raise DeltaError(f"delta appends to {where!r}, which is not a list")


def _tables(operand: Array | AoT, *, where: str) -> list[Table]:
    """Return copies of ``operand``'s tables, ``[[header]]`` comments included."""
    if isinstance(operand, AoT):
        return [copy.deepcopy(table) for table in operand.body]
    tables: list[Table] = []
    # An inline ``[{...}]`` operand is an Array of inline tables.
    for entry in _elements(operand):
        if not isinstance(entry, InlineTable):
            raise DeltaError(f"delta appends a non-table to the tables of {where!r}")
        table = tomlkit.table()
        for name, value in _items(entry.value):
            table[name] = value
        tables.append(table)
    return tables


# `tomlkit` keeps an array's comments only in its private line groups; its public
# ``append`` and ``del`` drop or orphan them, so these edit the groups directly.
def _extend(current: Array, *, extra: Array) -> None:
    """Append ``extra``'s lines after ``current``'s last line, keeping comments."""
    groups = _groups(current)
    closing = groups.pop() if groups and groups[-1].is_whitespace() else None
    indent = next(
        (g.indent for g in groups if g.indent is not None and "\n" in g.indent.s),
        None,
    )
    if indent is None:
        # A single-line list stays single-line; its public ``append`` owns the
        # separators, and an inline list carries no comments to lose. The blank
        # inside an empty ``[ ]`` would trail the first entry.
        if closing is not None and groups:
            groups.append(closing)
        # ``Array`` subclasses a bare ``list``, so its own methods are untyped.
        items = cast(list[object], current)
        for value in _elements(extra):
            items.append(value)
        return
    values = [g for g in groups if _holds_value(g)]
    if values and values[-1].comma is None:
        values[-1].comma = Whitespace(",")
    # A comment ending a value's line rides in that value's group, so moving or
    # dropping the group carries it.
    for group in copy.deepcopy(_groups(extra)):
        if group.is_whitespace():
            continue
        if group.value is not None and _holds_value(group):
            group.comma = Whitespace(",")
            _list_append(current, group.value)
        if _holds_value(group) or _leads(group):
            group.indent = copy.deepcopy(indent)
        groups.append(group)
    if closing is not None:
        groups.append(closing)
    current._reindex()  # noqa: SLF001 -- see above.


def _drop_entry(current: Array, *, index: int) -> None:
    """Delete entry ``index``, its trailing comment, and a lead only it uses."""
    groups = _groups(current)
    position = current._index_map[index]  # noqa: SLF001 -- see above.
    end = position + 1
    start = position
    while start and _leads(groups[start - 1]):
        start -= 1
    following = groups[end:]
    after = next((g for g in following if _holds_value(g) or _leads(g)), None)
    if after is not None and _holds_value(after):
        start = position
    # In a one-line list the next entry inherits the opening position.
    if start == 0 and end < len(groups) and groups[0].indent is None:
        groups[end].indent = None
    del groups[start:end]
    _list_delete(current, index=index)
    current._reindex()  # noqa: SLF001 -- see above.


def _groups(array: Array) -> list[_ArrayItemGroup]:
    """Return ``array``'s line groups: indent, value, comma, and comment."""
    return array._value  # noqa: SLF001 -- see above.


def _list_append(array: Array, value: Item) -> None:
    """Append to ``array``'s value list without touching its line groups."""
    list[Item].append(array, value)


def _list_delete(array: Array, *, index: int) -> None:
    """Delete from ``array``'s value list without touching its line groups."""
    list[Item].__delitem__(array, index)


def _elements(current: Array | AoT) -> list[object]:
    return list(current)


def _holds_value(group: _ArrayItemGroup) -> bool:
    return group.value is not None and not isinstance(group.value, Null)


def _leads(group: _ArrayItemGroup) -> bool:
    """Return whether ``group`` is a comment on a line of its own."""
    return (
        not _holds_value(group)
        and group.comment is not None
        and group.indent is not None
        and "\n" in group.indent.as_string()
    )


def _drop_key(target: _Node, *, key: str) -> None:
    """Delete ``key`` and the comment lines directly above it."""
    _set_lead(target, key=key, lead=(), attached=True)
    del target[key]


# A body's trailing comments and blank lines belong to no later key: after
# ``_normalized`` the next header carries its own lead. A plain key added at the end
# still goes ahead of them, so they stay last.
def _insert(target: _Node, *, key: str, value: object) -> None:
    """Add ``key`` to ``target`` ahead of the comments and blank lines ending it."""
    _assign(target, key=key, value=value)
    found = _position(target, key=key)
    if found is None or isinstance(value, Table | AoT):
        return
    container, index = found
    body = container.body
    start = _run_start(body, end=index, kinds=_RUN)
    if start < index:
        body.insert(start, body.pop(index))
        _reindex(container)


# ``Null`` is the placeholder a deleted key leaves behind.
_RUN: Final = (Comment, Whitespace, Null)


def _run_start(
    body: list[tuple[Key | None, Item]],
    *,
    end: int,
    kinds: tuple[type, ...],
) -> int:
    """Return where the run of unkeyed ``kinds`` entries ending at ``end`` starts."""
    start = end
    while (
        start > 0
        and body[start - 1][0] is None
        and isinstance(body[start - 1][1], kinds)
    ):
        start -= 1
    return start


def _reindex(container: Container) -> None:
    """Rebuild ``container``'s key map after its body was spliced."""
    container._map.clear()  # noqa: SLF001 -- splicing the body shifts every index.
    for i, (k, _) in enumerate(container.body):
        if k is not None:
            previous = container._map.get(k)  # noqa: SLF001 -- see above.
            container._map[k] = i if previous is None else (*_indices(previous), i)  # noqa: SLF001 -- see above.


# After ``_normalized``, a header's lead is its own indent. A plain key's lead is the
# run of comment and blank lines directly above it in its body, back to the key before
# it, so a separator comment between two keys survives a round trip.
def _lead(node: _Node, *, key: str) -> tuple[str, ...]:
    """Return the lines directly above ``key``: its comments and blank lines."""
    if (header := _header_of(_get(node, key=key))) is not None:
        return tuple(header.trivia.indent.splitlines(keepends=True))
    site = _key_lead_site(node, key=key, attached=False)
    if site is None:
        return ()
    body, start, end = site
    return tuple(
        item.as_string() for _, item in body[start:end] if not isinstance(item, Null)
    )


def _set_lead(
    node: _Node,
    *,
    key: str,
    lead: tuple[str, ...],
    attached: bool = False,
) -> None:
    """Replace the lines directly above ``key`` with ``lead``."""
    if (header := _header_of(_get(node, key=key))) is not None:
        # A lead that opens with no blank line keeps the header's separation.
        keep = (
            "" if lead and not lead[0].strip() else _blank_prefix(header.trivia.indent)
        )
        header.trivia.indent = keep + "".join(_line(line).as_string() for line in lead)
        return
    site = _key_lead_site(node, key=key, attached=attached)
    # Every caller sets the lead of a key it has just placed in ``node``.
    assert isinstance(site, tuple)
    body, start, end = site
    body[start:end] = [(None, _line(line)) for line in lead]
    for owner in _owners(node):
        _reindex(owner)


def _header_of(item: object) -> Table | None:
    """Return the table whose ``[header]`` line ``item`` renders first, or None."""
    if isinstance(item, AoT):
        return item.body[0] if item.body else None
    if isinstance(item, OutOfOrderTableProxy):
        # A table split around others renders its first part's header first.
        item = item._tables[0]  # noqa: SLF001 -- no public view of the parts.
    if isinstance(item, Table) and not item.is_super_table():
        return item
    return None


# A deleted key's ``attached`` lead stops at a blank line: a comment above one belongs
# to what precedes it.
def _key_lead_site(
    node: _Node,
    *,
    key: str,
    attached: bool,
) -> tuple[list[tuple[Key | None, Item]], int, int] | None:
    """Return ``(body, start, end)``: the comment and blank lines above plain ``key``."""
    found = _position(node, key=key)
    if found is None:
        return None
    container, index = found
    body = container.body
    kinds = Comment if attached else Comment | Whitespace | Null
    start = index
    while (
        start > 0
        and body[start - 1][0] is None
        and isinstance(body[start - 1][1], kinds)
    ):
        start -= 1
    return body, start, index


# `tomlkit` stores the comments above ``[header]`` at the END of whatever renders just
# before it -- the previous table's body, or its last descendant's -- so deleting that
# table would delete them, and a key added to it would land between them and their
# header. Moved into the header's own indent, which tomlkit renders directly above
# it, every comment has one owner: a header's lead travels with its header.
def _normalized(document: _Node) -> tomlkit.TOMLDocument:
    """Return a copy of ``document`` with each header's lead in its own indent."""
    copied = cast(tomlkit.TOMLDocument, copy.deepcopy(document))
    leads: list[_Lead] = []
    # The preamble is the file's, not the first header's: the walk starts below it.
    preamble = _preamble_end(copied.body)
    _find_leads(
        copied.body,
        cursor=_Cursor(body=copied.body, end=preamble, floor=preamble),
        leads=leads,
    )
    # Found first, then moved last to first, so no move shifts a slice still to move.
    for header, body, start, end in reversed(leads):
        header.trivia.indent = (
            "".join(item.as_string() for _, item in body[start:end])
            + header.trivia.indent
        )
        del body[start:end]
    for owner in _owners(copied):
        _reindex(owner)
    return copied


type _Lead = tuple[Table, list[tuple[Key | None, Item]], int, int]


@dataclass(slots=True, kw_only=True)
class _Cursor:
    """Where rendering has reached: the end of ``body[:end]``, never below ``floor``."""

    body: list[tuple[Key | None, Item]]
    end: int
    floor: int = 0


# The preamble is the comment block atop the file that a blank line closes. Without
# that blank line, the comments sit directly on the first key and are its lead.
def _preamble_end(body: list[tuple[Key | None, Item]]) -> int:
    """Return the index just past the preamble atop ``body`` and its blank lines."""
    end = 0
    while (
        end < len(body) and body[end][0] is None and isinstance(body[end][1], Comment)
    ):
        end += 1
    if not end or end == len(body) or not isinstance(body[end][1], Whitespace):
        return 0
    while (
        end < len(body)
        and body[end][0] is None
        and isinstance(body[end][1], Whitespace)
    ):
        end += 1
    return end


# Walked in render order, the text just before a ``[header]`` line ends ``cursor``.
# A header-less table (``[tool]`` holding only ``[tool.a]``) renders no line, so its
# first header takes the lines above the header-less table's own key.
def _find_leads(
    body: list[tuple[Key | None, Item]],
    *,
    cursor: _Cursor,
    leads: list[_Lead],
) -> None:
    """Append ``(header, body, start, end)`` for each header's lead under ``body``."""
    for index, (key, value) in enumerate(body):
        if key is None:
            continue
        if cursor.body is body:
            # Rendering reached this key; the unkeyed lines before it end the cursor.
            cursor.end = index
        if not isinstance(value, Table | AoT):
            cursor.body, cursor.end = body, len(body)
            continue
        for table in value.body if isinstance(value, AoT) else [value]:
            if table.is_aot_element() or not table.is_super_table():
                start = max(_lead_start(cursor.body, end=cursor.end), cursor.floor)
                leads.append((table, cursor.body, start, cursor.end))
                cursor.body, cursor.end = table.value.body, len(table.value.body)
                cursor.floor = 0
            _find_leads(table.value.body, cursor=cursor, leads=leads)


# Only comments directly under what renders before, with a blank line after them,
# belong to it (``# Tail of t.`` stays in ``[t]``). Everything else in the gap, a
# section comment set apart by blank lines included, introduces the header, so it
# survives deleting or replacing the table before it.
def _lead_start(body: list[tuple[Key | None, Item]], *, end: int) -> int:
    """Return where the lead ending at ``body[end]`` starts."""
    start = _run_start(body, end=end, kinds=_RUN)
    tail = start
    while tail < end and isinstance(body[tail][1], Comment):
        tail += 1
    return tail if tail < end else start


# Its own lead is set separately, by ``_set_lead``; the blank line that separates it
# from what precedes is layout, so it stays.
def _detached(value: object) -> object:
    """Return a copy of ``value`` without the comments above its first header."""
    value = copy.deepcopy(value)
    if (header := _header_of(value)) is not None:
        header.trivia.indent = _blank_prefix(header.trivia.indent)
    return value


def _blank_prefix(indent: str) -> str:
    """Return the blank lines that open ``indent``."""
    lines = indent.splitlines(keepends=True)
    return "".join(itertools.takewhile(lambda line: not line.strip(), lines))


def _owners(node: _Node) -> list[Container]:
    """Return every container under ``node``, whose key maps a splice may shift."""
    containers: list[Container] = []
    for container in _fragments(node):
        containers.append(container)
        for _, item in container.body:
            if isinstance(item, Table):
                containers.extend(_owners(item))
            elif isinstance(item, AoT):
                for table in item.body:
                    containers.extend(_owners(table))
    return containers


def _indices(previous: int | tuple[int, ...]) -> tuple[int, ...]:
    return previous if isinstance(previous, tuple) else (previous,)


def _position(node: _Node, *, key: str) -> tuple[Container, int] | None:
    """Return the container holding ``key`` under ``node`` and its index, or None."""
    for container in _fragments(node):
        index = next(
            (
                i
                for i, (k, _) in enumerate(container.body)
                if k is not None and k.key == key
            ),
            None,
        )
        if index is not None:
            return container, index
    return None


def _fragments(node: _Node) -> list[Container]:
    """Return ``node``'s containers: one, or each part of an out-of-order table."""
    if isinstance(node, OutOfOrderTableProxy):
        return [table.value for table in node._tables]  # noqa: SLF001 -- no public view of the parts.
    return [_container(node)]


def _container(node: Container | Table) -> Container:
    return node if isinstance(node, Container) else node.value


# ``Table.__setitem__`` copies the spaces in the table's own indent onto each new key.
# After ``_normalized`` that indent holds the header's lead comments, whose inner
# spaces would then indent the key. Its ``Container`` adds the key as written.
#
# After ``_normalized`` the next header's own lead holds the blank line between two
# tables, so the separation tomlkit adds on placing a table would stack onto it: the
# blank line above a header-less table, and the one replacing a table appends after it.
def _assign(node: _Node, *, key: str, value: object) -> None:
    """Set ``node[key]`` to ``value`` without re-indenting it."""
    length = len(value.value.body) if isinstance(value, Table) else 0
    if isinstance(node, Table):
        node.value[key] = value
        dict[str, object].__setitem__(node, key, node.value.item(key))
    else:
        node[key] = value
    placed = _get(node, key=key)
    if not isinstance(placed, Table):
        return
    if placed.is_super_table():
        placed.trivia.indent = ""
    body = placed.value.body
    if placed is value and len(body) > length:
        del body[length:]
        _reindex(placed.value)


# The item itself, not ``node[key]``: indexing unwraps a boolean to a bare
# ``bool``, dropping the comment that trails it.
def _get(node: _Node, *, key: str) -> object:
    """Return ``node``'s item for ``key``, or None when ``node`` lacks ``key``."""
    if key not in node:
        return None
    if isinstance(node, OutOfOrderTableProxy):
        found = _position(node, key=key)
        value = cast(object, node[key])
        if found is None or _is_table(value):
            return value
        container, index = found
        return container.body[index][1]
    return cast(object, node.item(key))


def _items(node: _Node) -> Iterator[tuple[str, object]]:
    """Yield ``(key, value)`` for every key of ``node``."""
    for key in [str(name) for name in node]:
        yield key, _get(node, key=key)


def _split(raw_key: str) -> tuple[str, str]:
    """Split ``"+name"`` into ``("name", "+")``; a plain key has ``"="``."""
    for op in _OPS:
        if raw_key.startswith(op):
            return raw_key.removeprefix(op), op
    return raw_key, "="


# A ruff per-file-ignores glob may itself start with ``!`` (negation), so a real
# key spelled like an operation carries a leading ``=`` to mean "set this key".
_OPS: Final = ("+", "-", "!", "=")


def _literal(key: str) -> str:
    """Return the delta spelling that sets ``key`` itself."""
    return f"={key}" if key.startswith(_OPS) else key


def _is_table(value: object) -> bool:
    return isinstance(value, Table | OutOfOrderTableProxy)


def _plain(value: object) -> object:
    return (
        value.unwrap()
        if isinstance(value, Item | Container | OutOfOrderTableProxy)
        else value
    )


def _derive_into(delta: Container | Table, *, base: _Node, target: _Node) -> None:
    """Write into ``delta`` the operations that turn ``base`` into ``target``."""
    for key in [key for key in base if key not in target]:
        _assign(delta, key=f"!{key}", value=True)
    for key, value in _items(target):
        old = _get(base, key=key)
        lead = _lead(target, key=key)
        name = _literal(key)
        # Blank lines are layout, not content: only a comment change re-emits.
        had = _comments(_lead(base, key=key))
        moved = _comments(lead) != had
        replaced = key not in base or (not _is_table(value) and moved)
        if replaced or _is_table(old) != _is_table(value):
            _replace(delta, name=name, value=value, lead=lead, cleared=bool(had))
        elif _is_table(old) and _is_table(value) and _merge_loses(old, value):
            _assign(delta, key=f"!{key}", value=True)
            _replace(delta, name=name, value=value, lead=lead, cleared=bool(had))
        elif _is_table(old) and _is_table(value):
            # Recursed even when the values match: a comment may still differ.
            sub = tomlkit.table()
            _derive_into(sub, base=cast(_Node, old), target=cast(_Node, value))
            if sub or moved:
                _assign(delta, key=name, value=sub)
            if moved:
                _set_lead(delta, key=name, lead=lead if _comments(lead) else (_CLEAR,))
        elif _plain(old) == _plain(value) and _remarks(old) == _remarks(value):
            continue
        elif isinstance(old, Array) and isinstance(value, Array):
            _derive_list(delta, key=key, old=old, new=value)
        elif isinstance(old, AoT) and isinstance(value, AoT):
            _derive_tables(delta, key=key, old=old, new=value)
        else:
            _assign(delta, key=name, value=_detached(value))


# A merge cannot say "keep this table, empty" (render deletes a table its delta
# empties) or carry the comment ending its ``[header]`` line, so either replaces the
# table whole.
def _merge_loses(old: object, new: object) -> bool:
    """Return whether merging key by key fails to turn table ``old`` into ``new``."""
    if not _plain(new) and _plain(old):
        return True
    return _header_remark(old) != _header_remark(new)


def _header_remark(item: object) -> str:
    header = _header_of(item)
    return "" if header is None else header.trivia.comment


def _replace(
    delta: Container | Table,
    *,
    name: str,
    value: object,
    lead: tuple[str, ...],
    cleared: bool,
) -> None:
    """Write ``value`` whole under ``name``, with ``lead`` (or a clear) above it."""
    _assign(delta, key=name, value=_detached(value))
    _set_lead(
        delta,
        key=name,
        lead=(_CLEAR,) if cleared and not _comments(lead) else lead,
    )


def _comments(lead: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(line for line in lead if line.strip())


# Where a delta gives no comment, the base's stays; this line instead clears it. A bare
# ``#`` cannot be the marker: real comment blocks use it as a paragraph break.
_CLEAR: Final = "# (clear)"


def _wanted(lines: tuple[str, ...]) -> tuple[str, ...] | None:
    """Return the lines a delta asks for: None keeps the base's, ``()`` clears it."""
    comments = _comments(lines)
    if not comments:
        return None
    return () if tuple(line.strip() for line in comments) == (_CLEAR,) else lines


type _Run = tuple[Container, int, int]
type _Site = Callable[[Container], _Run]


# After ``_normalized`` the preamble is all that is left of the document body above
# its first key: a header took its own lead, and a plain first key owns the lines above
# it (so a document opening with one has no preamble).
def _preamble_site(document: Container) -> _Run:
    """Return the lines atop ``document`` that no key owns."""
    body = document.body
    first = next((i for i, (k, _) in enumerate(body) if k is not None), len(body))
    if first < len(body) and not isinstance(body[first][1], Table | AoT):
        return document, 0, 0
    return document, 0, first


def _footer_site(document: Container) -> _Run:
    """Return the comment and blank lines that end ``document``'s last body."""
    container = document
    while True:
        last = next((v for k, v in reversed(container.body) if k is not None), None)
        if isinstance(last, AoT) and last.body:
            last = last.body[-1]
        if not isinstance(last, Table):
            break
        container = last.value
    end = len(container.body)
    # A document with no key at all is all preamble (``_preamble_site``).
    start = (
        end
        if container is document and last is None
        else _run_start(
            container.body,
            end=end,
            kinds=_RUN,
        )
    )
    return container, start, end


def _run(site: _Run) -> tuple[str, ...]:
    container, start, end = site
    return tuple(
        item.as_string()
        for _, item in container.body[start:end]
        if not isinstance(item, Null)
    )


def _set_run(site: _Run, *, lines: tuple[str, ...]) -> None:
    container, start, end = site
    container.body[start:end] = [(None, _line(line)) for line in lines]
    _reindex(container)


def _line(line: str) -> Item:
    """Return ``line`` as a blank-line or comment-line body entry."""
    if not line.strip():
        return Whitespace(line)
    return Comment(Trivia(comment_ws="", comment=line.rstrip("\n"), trail="\n"))


def _derive_list(delta: Container | Table, *, key: str, old: Array, new: Array) -> None:
    """Write ``-key``/``+key`` for ``old`` to ``new``, or a replacement if reordered."""
    before = [_plain(v) for v in _elements(old)]
    after = [_plain(v) for v in _elements(new)]
    if not _entry_ops_fit(before, after):
        _assign(delta, key=_literal(key), value=copy.deepcopy(new))
        return
    ops = tomlkit.table()
    if removed := [v for v in before if v not in after]:
        ops[f"-{key}"] = removed
    if any(v not in before for v in after):
        ops[f"+{key}"] = _added_lines(new, keep=before, base=old)
    if not _ops_keep_remarks(ops, key=key, old=old, new=new):
        _assign(delta, key=_literal(key), value=copy.deepcopy(new))
        return
    for name, operand in _items(ops):
        _assign(delta, key=name, value=operand)


# Entry operations move comments only with their entries; any other comment edit
# needs the whole list.
def _ops_keep_remarks(
    ops: Table,
    *,
    key: str,
    old: Array | AoT,
    new: Array | AoT,
) -> bool:
    """Return whether ``ops`` turn ``old`` into a list with ``new``'s comments."""
    trial = tomlkit.table()
    trial[key] = copy.deepcopy(old)
    # The operations come from ``old`` itself, so none fails and ``path``, which only
    # names the key in an error, is never read.
    _apply(trial, delta=ops, path=key)  # pragma: no mutate
    return _remarks(_get(trial, key=key)) == _remarks(new)


# ``-key`` and ``+key`` match entries by value, so a list holding one twice cannot
# say which copy goes; and ``+key`` appends, so it reproduces ``after`` only when
# every added entry follows every kept one. Otherwise the list is replaced whole.
def _entry_ops_fit(before: list[object], after: list[object]) -> bool:
    """Return whether ``-key``/``+key`` turn ``before`` into ``after``."""
    if _has_repeats(before) or _has_repeats(after):
        return False
    kept = [v for v in before if v in after]
    return bool(kept) and after[: len(kept)] == kept


def _has_repeats(values: list[object]) -> bool:
    return any(v in values[:i] for i, v in enumerate(values))


# ``as_string`` leaves out the comment ending the item's own line (``a = 1  # Why.``,
# ``[t]  # Why.``), which tomlkit keeps in ``trivia``; without it a public edit to that
# comment alone would derive no operation and be lost.
def _remarks(value: object) -> list[str]:
    """Return the comments inside and ending ``value``, sorted."""
    if not isinstance(value, Item):
        return []
    owners = value.body if isinstance(value, AoT) else [value]
    text = value.as_string() + "".join(f"\n{o.trivia.comment}" for o in owners)
    return sorted(
        line[line.index("#") :].strip() for line in text.splitlines() if "#" in line
    )


def _added_lines(new: Array, *, keep: list[object], base: Array) -> Array:
    """Return ``new``'s entries absent from ``keep``, each with its own comments."""
    known = {_comment(g) for g in _groups(base)} - {""}
    groups = _groups(new)
    chosen: list[int] = []
    for i, group in enumerate(groups):
        if not _holds_value(group) or _plain(group.value) in keep:
            continue
        lead = i
        while lead > 0 and _leads(groups[lead - 1]):
            lead -= 1
        chosen.extend(j for j in range(lead, i) if _comment(groups[j]) not in known)
        chosen.append(i)
    lines = tomlkit.array()
    _groups(lines).extend(copy.deepcopy(groups[i]) for i in dict.fromkeys(chosen))
    for group in _groups(lines):
        if group.value is not None and _holds_value(group):
            _list_append(lines, group.value)
    _groups(lines).append(_ArrayItemGroup(indent=Whitespace("\n")))
    lines._reindex()  # noqa: SLF001 -- see `_extend`.
    return lines


def _comment(group: _ArrayItemGroup) -> str:
    return "" if group.comment is None else group.comment.as_string().strip()


def _derive_tables(delta: Container | Table, *, key: str, old: AoT, new: AoT) -> None:
    """Write ``-key``/``+key`` for an array of tables, or a replacement if reordered."""
    before: list[object] = [t.unwrap() for t in old.body]
    after: list[object] = [t.unwrap() for t in new.body]
    if not _entry_ops_fit(before, after):
        _assign(delta, key=_literal(key), value=_detached(new))
        return
    ops = tomlkit.table()
    if removed := [t for t in before if t not in after]:
        ops[f"-{key}"] = removed
    if added := [t for t in new.body if t.unwrap() not in before]:
        aot = tomlkit.aot()
        for table in added:
            aot.insert(len(aot), copy.copy(table))
        ops[f"+{key}"] = aot
    if not _ops_keep_remarks(ops, key=key, old=old, new=new):
        _assign(delta, key=_literal(key), value=_detached(new))
        return
    for name, operand in _items(ops):
        _assign(delta, key=name, value=operand)


def _renamed(document: tomlkit.TOMLDocument, *, layer: _Node) -> tomlkit.TOMLDocument:
    """Apply ``layer``'s ``[pyproject.rename]`` substitutions to ``document``."""
    own = from_plain(_plain(_get(layer, key=SETTINGS)), dict[str, object], default={})
    rename = from_plain(own.get("rename"), dict[str, str], default={})
    if not rename:
        return document
    text = tomlkit.dumps(document)
    for old, new in rename.items():
        text = text.replace(old, new)
    return _parse(text, source=f"the document after [{SETTINGS}.rename] {rename}")
