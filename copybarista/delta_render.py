"""Apply a ``pyproject.delta.toml`` to its base, line by line.

``copybarista.pyproject`` documents the delta format; this module
implements it. Every operation edits the base's lines in place, as ``toml_lines``
splits them, so a line no operation names is kept as written.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Final

import itertools
import tomllib

from copybarista.toml_lines import (
    Block,
    Document,
    Entry,
    KeyPath,
    LinesError,
    parse,
    remark,
    spell,
    spelled_key,
    split_entry,
    split_header,
    split_key,
    value_key,
)
from copybarista.toml_lists import ListError, ListValue


SETTINGS: Final = "pyproject"

# Where a delta gives no comment, the base's stays; this line instead clears it. A
# bare ``#`` cannot be the marker: real comment blocks use it as a paragraph break.
_CLEAR: Final = "# (clear)"

# Operation prefixes. A ruff per-file-ignores glob may itself start with ``!``, so
# a real key spelled like an operation carries a leading ``=``.
_OPS: Final = ("+", "-", "!", "=", "^")


class DeltaError(ValueError):
    """A delta, its base, or a public edit cannot be rendered or folded."""


def render(base: str, delta: str) -> str:
    """Return ``base`` with ``delta``'s operations applied.

    Args:
      base: The document to start from.
      delta: The operations; its top-level ``[pyproject]`` settings are skipped.

    Returns:
      text: The rendered document, every line no operation names as written.

    Raises:
      DeltaError: Either text is not TOML, or the delta edits what ``base``
        lacks.

    """
    work = _Work.of(_parsed(base, source="the base"))
    ops = _separated(_parsed(delta, source="the delta"))
    _apply_runs(work, delta=ops)
    whole = _whole_replacements(ops)
    _replace_arrays(work, delta=ops)
    named = [
        _literal_path(block.path)
        for block in ops.blocks
        if block.header and not block.array and block.path[:1] != (SETTINGS,)
    ]
    for block in ops.blocks:
        if block.path[:1] == (SETTINGS,):
            continue
        if block.header and not block.array:
            at = named.index(_literal_path(block.path))
            exact = bool(block.lead) and block.lead[-1].strip() == _CLEAR
            order = _Order(
                before=tuple(named[:at]),
                after=tuple(named[at + 1 :]),
                exact=exact,
            )
            _apply_block(work, block=block, whole=whole, order=order)
        elif block.header:
            _apply_block(work, block=block, whole=whole, order=_Order())
        else:
            for entry in block.entries:
                _apply_entry(work, at=(), entry=entry, whole=whole)
    work.prune()
    work.exact_top = _states_top(work, delta=ops)
    return work.text()


def _separated(delta: Document) -> Document:
    """Return ``delta`` with the separating blank line taken off each exact table lead."""
    blocks = list(delta.blocks)
    first = next((i for i, b in enumerate(blocks) if b.header or b.entries), None)
    for index, block in enumerate(blocks):
        lead = block.lead
        exact = lead and lead[-1].strip() == _CLEAR
        if block.header and index != first and exact and not lead[0].strip():
            blocks[index] = replace(block, lead=lead[1:])
    return replace(delta, blocks=tuple(blocks))


def _states_top(work: _Work, *, delta: Document) -> bool:
    """Return whether the delta stated, exactly, the lead of what now opens the body."""
    # The root table has no header, so its first key opens the body; with no table or
    # key left, nothing does, and no lead below can match.
    top = next(
        (
            t.path if t.header else t.entries[0].key
            for t in work.tables
            if t.header or t.entries
        ),
        None,
    )
    for block in delta.blocks:
        leads = (
            [(block.path, block.lead)]
            if block.header
            else [(_split_op(e.key)[1], e.lead) for e in block.entries]
        )
        for at, lead in leads:
            if at == top and lead and lead[-1].strip() == _CLEAR:
                return True
    return False


@dataclass(frozen=True, slots=True, kw_only=True)
class _Order:
    """The tables the delta names around the one being applied, and whether it is placed."""

    before: tuple[KeyPath, ...] = ()
    after: tuple[KeyPath, ...] = ()
    exact: bool = False


@dataclass(slots=True, kw_only=True)
class _Table:
    """A block being edited."""

    lead: list[str]
    header: str
    path: KeyPath
    array: bool
    entries: list[Entry]
    tail: list[str]
    # Whether this render added the table; an added table is never pruned.
    added: bool = False

    @classmethod
    def of(cls, block: Block) -> _Table:
        return cls(
            lead=list(block.lead),
            header=block.header,
            path=block.path,
            array=block.array,
            entries=list(block.entries),
            tail=list(block.tail),
        )

    def text(self) -> str:
        return Block(
            lead=tuple(self.lead),
            header=self.header,
            path=self.path,
            array=self.array,
            entries=tuple(self.entries),
            tail=tuple(self.tail),
        ).text()

    def index(self, key: KeyPath) -> int | None:
        return next((i for i, e in enumerate(self.entries) if e.key == key), None)


@dataclass(slots=True, kw_only=True)
class _Work:
    """The document being rendered."""

    preamble: list[str]
    tables: list[_Table]
    footer: list[str]
    # Tables a delete emptied, pruned at the end unless something refilled them.
    emptied: set[KeyPath] = field(default_factory=set[KeyPath])
    # Whether the delta stated the first lines of the body exactly.
    exact_top: bool = False

    @classmethod
    def of(cls, document: Document) -> _Work:
        return cls(
            preamble=list(document.preamble),
            tables=[_Table.of(block) for block in document.blocks],
            footer=list(document.footer),
        )

    def text(self) -> str:
        """Return the rendered file.

        Neither the file nor the body under a preamble opens with a blank line
        an edit left behind: deleting or clearing what was on top hands its
        separation to nothing, and the preamble carries the blank line that closes
        it. A lead the delta states exactly is the exception, as it is the delta's
        own choice.

        Returns:
          text: The preamble, every table, and the footer.

        """
        body = "".join(table.text() for table in self.tables)
        if not self.exact_top:
            body = body.lstrip("\r\n")
        return "".join((*self.preamble, body, *self.footer))

    def table(self, path: KeyPath) -> _Table | None:
        """Return the table headed ``[path]``, or the root for ``()``."""
        return next(
            (t for t in self.tables if t.path == path and not t.array),
            None,
        )

    def entry(self, path: KeyPath) -> tuple[_Table, int] | None:
        """Return the table holding the key at ``path`` and its index there.

        Args:
          path: The key's full path.

        Returns:
          found: The table and the key's index in it, or None when absent.

        """
        for table in self.tables:
            if table.array or table.path != path[: len(table.path)]:
                continue
            index = table.index(path[len(table.path) :])
            if index is not None:
                return table, index
        return None

    def is_table(self, path: KeyPath) -> bool:
        """Return whether ``path`` names a table: headed, implied, or dotted.

        Args:
          path: The table's key path.

        Returns:
          table: Whether any header or dotted key reaches past ``path``.

        """
        return any(t.path[: len(path)] == path for t in self.tables if t.path) or any(
            len(table.path) + len(entry.key) > len(path)
            and (*table.path, *entry.key)[: len(path)] == path
            for table in self.tables
            if not table.array
            for entry in table.entries
        )

    def is_dotted(self, path: KeyPath) -> bool:
        """Return whether a dotted key, not a header, defines the table at ``path``.

        Args:
          path: The table's key path.

        Returns:
          dotted: Whether a key's own dotted parts reach past a header to ``path``.

        """
        return any(
            len(table.path) < len(path) < len(table.path) + len(entry.key)
            and (*table.path, *entry.key)[: len(path)] == path
            for table in self.tables
            if not table.array
            for entry in table.entries
        )

    def exists(self, path: KeyPath) -> bool:
        return self.entry(path) is not None or self.is_table(path)

    def delete(self, path: KeyPath) -> None:
        """Delete the key or table at ``path`` with the lines it owns.

        Args:
          path: The key's or table's full path.

        """
        if (found := self.entry(path)) is not None:
            table, index = found
            _drop_entry(table, index=index)
            self.emptied.add(table.path)
            return
        self.tables = [
            t for t in self.tables if t.path[: len(path)] != path or not t.path
        ]
        for table in self.tables:
            if table.array:
                continue
            for index in reversed(range(len(table.entries))):
                full = (*table.path, *table.entries[index].key)
                if full[: len(path)] == path:
                    _drop_entry(table, index=index)
        self.emptied.add(path[:-1])

    def insert_table(self, table: _Table) -> None:
        """Insert ``table`` after the run of tables nearest it in path, else last.

        The nearest is the table sharing the longest path prefix with it; the run
        is that table and the subtables that directly follow it.

        Args:
          table: The table to insert.

        """
        self.tables.insert(self.nearest_end(table), table)

    # A table whose delta lead is exact sits where the delta lists it: where the nearest
    # run would put it past a table the delta names after it, or ahead of one it names
    # before it, that order wins. Any other new table takes the nearest run, as
    # hand-written deltas expect.
    def place(self, table: _Table, *, index: int, order: _Order) -> None:
        """Insert ``table`` at ``index``, kept between the tables ``order`` names.

        Args:
          table: The table to insert.
          index: Where the nearest run, or a replaced table, would put it.
          order: The tables the delta names around it.

        """
        if order.exact:
            after = [self.table(path) for path in order.after]
            ahead = [self.tables.index(t) for t in after if t is not None]
            if ahead and index > min(ahead):
                index = min(ahead)
            before = [self.table(path) for path in order.before]
            behind = [self.tables.index(t) + 1 for t in before if t is not None]
            if behind and index < max(behind):
                index = max(behind)
        self.tables.insert(index, table)

    def nearest_end(self, table: _Table) -> int:
        """Return the index past the run of tables nearest ``table`` in path.

        Args:
          table: The table being placed.

        Returns:
          index: Past that run, or the end when no table shares a prefix.

        """
        shared = [
            (_shared_depth(t.path, table.path), i)
            for i, t in enumerate(self.tables)
            if t.path
        ]
        depth, index = max(shared, default=(0, 0), key=lambda pair: (pair[0], -pair[1]))
        if not depth:
            return len(self.tables)
        prefix = self.tables[index].path[:depth]
        end = index + 1
        while end < len(self.tables) and self.tables[end].path[:depth] == prefix:
            end += 1
        return end

    # A table a delete leaves with no key and no subtable goes, header and lead with
    # it; a delta keeps one empty by restating it whole (``"!t"`` beside ``[t]``).
    def prune(self) -> None:
        """Delete each table a delete left empty, deepest first."""
        while True:
            empty = [
                t
                for t in self.tables
                if t.path in self.emptied
                and t.path
                and not t.array
                and not t.added
                and not t.entries
                and not any(
                    o.path[: len(t.path)] == t.path and o is not t for o in self.tables
                )
            ]
            if not empty:
                return
            for table in empty:
                self.tables.remove(table)
                self.emptied.add(table.path[:-1])


# The deleted entry's lead splits at its last blank line: the comments below it were
# the entry's and go; what is above, a separated section comment and the blank lines
# setting the entry apart, stays. The blank lines that opened the next entry's lead
# separated it from the deleted one, so the kept part replaces them.
def _drop_entry(table: _Table, *, index: int) -> None:
    """Delete an entry and the comments directly above it; keep what a blank sets apart."""
    entry = table.entries.pop(index)
    lead = list(entry.lead)
    while lead and _is_comment(lead[-1]):
        lead.pop()
    if index < len(table.entries):
        following = table.entries[index]
        rest = list(itertools.dropwhile(lambda line: not line.strip(), following.lead))
        kept = lead or _blank_prefix(list(following.lead))
        table.entries[index] = replace(following, lead=(*kept, *rest))
        return
    while lead and not lead[-1].strip():
        lead.pop()
    table.tail[:0] = lead


def _apply_runs(work: _Work, *, delta: Document) -> None:
    """Replace the preamble and footer where the delta gives them."""
    if not any(block.header or block.entries for block in delta.blocks):
        lines = list(delta.preamble)
        if not any(table.header or table.entries for table in work.tables):
            # A base of comments alone is all preamble, so the delta's replace it.
            _set_run(work.preamble, lines=lines)
        elif lines and not lines[-1].strip():
            _set_preamble(work.preamble, lines=lines)
        elif (split := _preamble_split(lines)) is not None:
            _set_preamble(work.preamble, lines=lines[:split])
            _set_run(work.footer, lines=lines[split + 1 :])
        elif lines:
            _set_run(work.footer, lines=lines)
        return
    _set_preamble(work.preamble, lines=list(delta.preamble))
    footer = list(delta.footer)
    if footer and not footer[0].strip():
        # The blank line that sets the footer apart from the last operation.
        footer = footer[1:]
    _set_run(work.footer, lines=footer)


# ``derive`` writes a preamble, the blank line closing it, one blank line, then the
# footer; the footer keeps any blank lines of its own after those.
def _preamble_split(lines: list[str]) -> int | None:
    """Return the end of a comment-only delta's preamble when a footer follows it."""
    comments = len(list(itertools.takewhile(_is_comment, lines)))
    end = comments + 1
    if not comments or end + 1 >= len(lines):
        return None
    if lines[comments].strip() or lines[end].strip():
        return None
    return end


def _set_preamble(run: list[str], *, lines: list[str]) -> None:
    """Replace the preamble with the delta's comments; a lone clear marker removes it."""
    comments = [line for line in lines if line.strip()]
    if comments == [f"{_CLEAR}\n"] or [c.strip() for c in comments] == [_CLEAR]:
        run[:] = []
    elif comments:
        run[:] = lines


# A ``# (clear)`` line ending the delta's states the run exactly as the lines before it;
# a lone one, none. Otherwise its lines replace the run as written.
def _set_run(run: list[str], *, lines: list[str]) -> None:
    """Replace a preamble, footer, or tail with the delta's, when it gives one."""
    if (wanted := _wanted(lines, base=())) is not None:
        run[:] = wanted


# None keeps it.
#
# Before a closing ``# (clear)`` line the lead is stated exactly. Otherwise the delta's
# comment lines follow the blank lines that open ``base``.
def _wanted(
    delta: list[str] | tuple[str, ...],
    *,
    base: list[str] | tuple[str, ...],
) -> list[str] | None:
    """Return the lead the delta's lines ``delta`` ask for over ``base``."""
    if delta and delta[-1].strip() == _CLEAR:
        return list(delta[:-1])
    if not any(_is_comment(line) for line in delta):
        return None
    comments = list(itertools.dropwhile(lambda line: not line.strip(), delta))
    return [*_blank_prefix(list(base)), *comments]


def _is_comment(line: str) -> bool:
    return line.lstrip().startswith("#")


def _whole_replacements(delta: Document) -> set[KeyPath]:
    """Return each path the delta both deletes and sets: it replaces those whole."""
    deleted: set[KeyPath] = set()
    named: set[KeyPath] = set()
    for block in delta.blocks:
        at = _literal_path(block.path) if block.header else ()
        if block.header and not block.array:
            named.add(at)
        for entry in block.entries:
            op, key = _split_op(entry.key)
            full = (*at, *key)
            (deleted if op == "!" else named).add(full)
    return deleted & named


def _literal_path(path: KeyPath) -> KeyPath:
    """Return ``path`` with each ``=`` escape removed."""
    return tuple(key.removeprefix("=") if key.startswith("=") else key for key in path)


def _split_op(key: KeyPath) -> tuple[str, KeyPath]:
    """Split the operation off a delta key: ``("!", ("a",))`` for ``"!a"``."""
    if key[0].startswith("^"):
        return "^", key
    last = key[-1]
    op = next((op for op in _OPS if last.startswith(op)), "")
    return op, (*key[:-1], last[1:] if op else last)


# A ``[[key]]`` without an operation states every element: the base's elements go,
# the first one's lead and place kept, and the delta's elements are added in order.
def _replace_arrays(work: _Work, *, delta: Document) -> None:
    """Remove each array of tables the delta states whole, leaving a marker in place."""
    stated = {
        _literal_path(block.path)
        for block in delta.blocks
        if block.array and not _split_op(block.path)[0]
    }
    for path in stated:
        elements = [i for i, t in enumerate(work.tables) if t.array and t.path == path]
        if not elements:
            continue
        first = work.tables[elements[0]]
        owned = {
            i
            for start in elements
            for i in range(start, len(work.tables))
            if i == start
            or (
                work.tables[i].path[: len(path)] == path
                and not work.tables[i].array
                and all(j not in elements for j in range(start + 1, i + 1))
            )
        }
        marker = _Table(
            lead=list(first.lead),
            header="",
            path=path,
            array=True,
            entries=[],
            tail=[],
            added=True,
        )
        work.tables = [
            marker if i == elements[0] else t
            for i, t in enumerate(work.tables)
            if i == elements[0] or i not in owned
        ]


def _shared_depth(left: KeyPath, right: KeyPath) -> int:
    """Return how many leading parts ``left`` and ``right`` share."""
    return len(
        list(
            itertools.takewhile(
                lambda n: left[n] == right[n],
                range(min(len(left), len(right))),
            ),
        ),
    )


def _apply_block(
    work: _Work,
    *,
    block: Block,
    whole: set[KeyPath],
    order: _Order,
) -> None:
    """Apply a delta ``[header]`` and the operations under it."""
    op, path = _split_op(block.path)
    if block.array and op in {"+", "-"}:
        _apply_array(work, block=block, op=op, path=path)
        return
    # ``=`` only escapes a name spelled like an operation: the element is added.
    if block.array and op in {"", "="}:
        _add_element(work, block=block, path=_literal_path(path))
        return
    path = _literal_path(path)
    base = work.table(path)
    if path in whole:
        position = _replace_position(work, path=path)
        lead = list(base.lead) if base is not None else ["\n"]
        work.delete(path)
        table = _new_table(block, path=path, lead=lead)
        work.place(table, index=position, order=order)
        _fill(work, table=table, block=block, whole=whole)
        return
    # A table only its subtables' headers imply takes a header of its own when the
    # delta gives it a key; one a dotted key defines cannot.
    implied = work.is_table(path) and not any(
        _split_op(entry.key)[0] in {"", "^"} for entry in block.entries
    )
    if base is None and not work.is_dotted(path) and not implied:
        table = _new_table(block, path=path, lead=["\n"])
        work.place(table, index=work.nearest_end(table), order=order)
        _fill(work, table=table, block=block, whole=whole)
        return
    if base is not None:
        _set_lead(base.lead, delta=block.lead)
        base.header = _merged_header(base.header, delta=block.header)
    for entry in block.entries:
        _apply_entry(work, at=path, entry=entry, whole=whole)
    # Last: a deleted last key hands its comments to the tail the delta states.
    if base is not None:
        _set_run(base.tail, lines=list(block.tail))


def _add_element(work: _Work, *, block: Block, path: KeyPath) -> None:
    """Add a stated element of a replaced array: into its marker, else after the last."""
    marker = next(
        (t for t in work.tables if t.array and t.path == path and not t.header),
        None,
    )
    element = _new_table(
        block,
        path=path,
        lead=["\n"] if marker is None else marker.lead,
    )
    element.entries.extend(block.entries)
    if marker is not None:
        work.tables[work.tables.index(marker)] = element
        return
    _insert_element(work, element=element, path=path)


def _append_table(work: _Work, *, path: KeyPath, value: str, where: str) -> None:
    """Append inline table ``value`` to the array of tables at ``path`` as a ``[[path]]``."""
    spelled = value.strip()
    if not spelled.startswith("{"):
        raise DeltaError(f"delta appends a non-table to {where!r}")
    inner = spelled[1:-1].strip()
    pairs = ListValue.parse(f"v = [{inner}]\n").values() if inner else []
    entries = tuple(
        entry for pair in pairs for entry in parse(f"{pair}\n").blocks[0].entries
    )
    element = _Table(
        lead=["\n"],
        header=f"[[{spell(path)}]]\n",
        path=path,
        array=True,
        entries=list(entries),
        tail=[],
        added=True,
    )
    _insert_element(work, element=element, path=path)


def _insert_element(work: _Work, *, element: _Table, path: KeyPath) -> None:
    """Insert ``element`` after the last table under ``path``, else where it fits."""
    last = max(
        (
            i
            for i, t in enumerate(work.tables)
            if t.path[: len(path)] == path and t.path
        ),
        default=None,
    )
    if last is None:
        work.insert_table(element)
    else:
        work.tables.insert(last + 1, element)


def _replace_position(work: _Work, *, path: KeyPath) -> int:
    """Return where a table replacing the one at ``path`` goes, once that is deleted."""
    first = next(
        (
            i
            for i, t in enumerate(work.tables)
            if t.path and t.path[: len(path)] == path
        ),
        len(work.tables),
    )
    deleted = sum(
        1 for t in work.tables[:first] if t.path and t.path[: len(path)] == path
    )
    return first - deleted


def _new_table(block: Block, *, path: KeyPath, lead: list[str]) -> _Table:
    """Return an empty table headed like ``block``, its lead given or the delta's."""
    brackets = "[[" if block.array else "["
    closing = "]]" if block.array else "]"
    _, rest = split_header(block.header)
    header = f"{brackets}{spell(path)}{closing}{'' if _CLEAR in rest else rest}"
    if _CLEAR in rest:
        header += "\n"
    if (wanted := _wanted(block.lead, base=lead)) is not None:
        lead = wanted
    return _Table(
        lead=lead,
        header=header,
        path=path,
        array=block.array,
        entries=[],
        tail=list(block.tail),
        added=True,
    )


def _blank_prefix(lines: list[str]) -> list[str]:
    return list(itertools.takewhile(lambda line: not line.strip(), lines))


def _fill(work: _Work, *, table: _Table, block: Block, whole: set[KeyPath]) -> None:
    for entry in block.entries:
        _apply_entry(work, at=table.path, entry=entry, whole=whole)


def _set_lead(lead: list[str], *, delta: tuple[str, ...]) -> None:
    """Replace ``lead`` with the delta's, when it gives one."""
    if (wanted := _wanted(delta, base=lead)) is not None:
        lead[:] = wanted


def _merged_header(base: str, *, delta: str) -> str:
    """Return ``base`` with ``delta``'s trailing comment, when it has one."""
    base_brackets, base_rest = split_header(base)
    _, rest = split_header(delta)
    if not remark(rest):
        return base
    if remark(rest) == _CLEAR:
        return f"{base_brackets}{base_rest[len(base_rest.rstrip('\r\n')) :]}"
    return f"{base_brackets}{rest}"


def _apply_array(work: _Work, *, block: Block, op: str, path: KeyPath) -> None:
    """Append a table to, or remove one from, the array of tables at ``path``."""
    where = ".".join(path)
    if op == "+":
        table = _new_table(block, path=path, lead=["\n"])
        table.array = True
        table.header = f"[[{spell(path)}]]{split_header(block.header)[1]}"
        table.entries = list(block.entries)
        work.insert_table(table)
        return
    wanted = _content("".join(entry.text for entry in block.entries))
    elements = [t for t in work.tables if t.array and t.path == path]
    match = next((t for t in elements if _content(_body(t)) == wanted), None)
    if match is not None:
        start = work.tables.index(match)
        end = start + 1
        while (
            end < len(work.tables)
            and work.tables[end].path[: len(path)] == path
            and not (work.tables[end].array and work.tables[end].path == path)
        ):
            end += 1
        del work.tables[start:end]
        return
    inline = "{" + ", ".join(entry.text.strip() for entry in block.entries) + "}"
    _remove(work, path=path, values=[inline], where=where)


def _body(table: _Table) -> str:
    return "".join(entry.text for entry in table.entries)


def _content(text: str) -> object:
    # Only a parsed document's own entries reach here, so they parse.
    return value_key(tomllib.loads(text))


def _apply_entry(
    work: _Work,
    *,
    at: KeyPath,
    entry: Entry,
    whole: set[KeyPath],
) -> None:
    """Apply one delta ``key = value`` under the table at ``at``."""
    op, key = _split_op(entry.key)
    anchor: KeyPath | None = None
    if op == "^":
        anchor = split_key(key[0][1:]) if key[0] != "^" else ()
        # The anchored key's last part may carry an ``=`` escape of its own.
        op, key = _split_op(key[1:])
    # ``_split_op`` already took the operation, or the ``=`` escape, off the last part.
    key = (*_literal_path(key[:-1]), key[-1])
    path = (*at, *key)
    where = ".".join(path)
    if op == "!":
        if not work.exists(path):
            raise DeltaError(f"delta deletes {where!r}, which the base lacks")
        if path not in whole:
            work.delete(path)
        return
    if op == "-":
        _remove(
            work,
            path=path,
            values=_operand(entry, where=where, verb="removes a"),
            where=where,
        )
        _set_entry_lead(work, path=path, delta=entry.lead)
        return
    if op == "+":
        _append(work, path=path, entry=entry, where=where)
        return
    _set(work, at=at, key=key, entry=entry, anchor=anchor, whole=path in whole)


def _set_entry_lead(work: _Work, *, path: KeyPath, delta: tuple[str, ...]) -> None:
    """Replace the lead of the entry at ``path`` with the delta's, when it gives one."""
    if (found := work.entry(path)) is None:
        return
    table, index = found
    current = table.entries[index]
    if (lead := _wanted(delta, base=current.lead)) is not None:
        table.entries[index] = replace(current, lead=tuple(lead))


def _operand(entry: Entry, *, where: str, verb: str) -> list[str]:
    try:
        return ListValue.parse(entry.text).values()
    except ListError as err:
        raise DeltaError(f"delta {verb} non-list from {where!r}") from err


def _remove(work: _Work, *, path: KeyPath, values: list[str], where: str) -> None:
    found = work.entry(path)
    if found is None:
        if any(t.array and t.path == path for t in work.tables):
            for value in values:
                _remove_table(work, path=path, value=value, where=where)
            return
        raise DeltaError(f"delta removes from {where!r}, which the base lacks")
    table, index = found
    current = table.entries[index]
    try:
        edited = ListValue.parse(current.text)
    except ListError as err:
        raise DeltaError(
            f"delta removes entries from {where!r}, which is not a list",
        ) from err
    for value in values:
        try:
            edited = edited.without([value])
        except ListError as err:
            raise DeltaError(
                f"delta removes {value} from {where!r}, which lacks it",
            ) from err
    table.entries[index] = replace(current, text=edited.text())


def _remove_table(work: _Work, *, path: KeyPath, value: str, where: str) -> None:
    wanted = spelled_key(value)
    match = next(
        (
            t
            for t in work.tables
            if t.array and t.path == path and _content(_body(t)) == wanted
        ),
        None,
    )
    if match is None:
        raise DeltaError(f"delta removes {value!r} from {where}, which lacks it")
    work.tables.remove(match)


def _append(work: _Work, *, path: KeyPath, entry: Entry, where: str) -> None:
    try:
        extra = ListValue.parse(entry.text)
    except ListError as err:
        raise DeltaError(f"delta appends a non-list to {where!r}") from err
    found = work.entry(path)
    if found is None:
        if any(t.array and t.path == path for t in work.tables):
            for value in extra.values():
                _append_table(work, path=path, value=value, where=where)
            return
        if work.is_table(path):
            raise DeltaError(f"delta appends to {where!r}, which is not a list")
        _set(work, at=path[:-1], key=path[-1:], entry=entry, anchor=None, whole=False)
        return
    table, index = found
    current = table.entries[index]
    try:
        merged = ListValue.parse(current.text).appended(extra)
    except ListError as err:
        raise DeltaError(f"delta appends to {where!r}, which is not a list") from err
    lead = _wanted(entry.lead, base=current.lead)
    table.entries[index] = Entry(
        lead=current.lead if lead is None else tuple(lead),
        key=current.key,
        text=merged.text(),
    )


def _set(
    work: _Work,
    *,
    at: KeyPath,
    key: KeyPath,
    entry: Entry,
    anchor: KeyPath | None,
    whole: bool,
) -> None:
    """Replace the value at ``at + key`` in place, or add it."""
    path = (*at, *key)
    _, rest = split_entry(entry.text)
    if (found := work.entry(path)) is not None:
        table, index = found
        current = table.entries[index]
        base_key, _ = split_entry(current.text)
        lead = _wanted(entry.lead, base=current.lead)
        table.entries[index] = Entry(
            lead=current.lead if lead is None else tuple(lead),
            key=current.key,
            text=f"{base_key}={rest}",
        )
        return
    # A new key has no base lead, so the delta's blank lines above it stand.
    lead = _wanted(entry.lead, base=entry.lead)
    if work.is_table(path):
        work.delete(path)
        del whole
    table = work.table(at)
    if table is None:
        table = _Table(
            lead=["\n"],
            header=f"[{spell(at)}]\n",
            path=at,
            array=False,
            entries=[],
            tail=[],
            added=True,
        )
        work.insert_table(table)
    added = Entry(lead=tuple(lead or ()), key=key, text=f"{spell(key)} ={rest}")
    if anchor is None:
        table.entries.append(added)
        return
    if not anchor:
        table.entries.insert(0, added)
        return
    index = table.index(anchor)
    if index is None:
        where, after = ".".join(path), ".".join((*at, *anchor))
        raise DeltaError(
            f"delta places {where!r} after {after!r}, which the base lacks",
        )
    table.entries.insert(index + 1, added)


def _parsed(text: str, *, source: str) -> Document:
    try:
        return parse(text)
    except LinesError as err:
        raise DeltaError(f"Cannot parse {source}: {err}") from err
