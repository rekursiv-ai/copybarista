"""Derive the delta that renders a target ``pyproject.toml`` from its base.

``derive(base, target)`` is the inverse of ``delta_render.render``: it writes the
fewest operations, each carrying only the comment lines it changes, such that
``render(base, derive(base, target)) == target``. Both files are split by
``toml_lines``, so each comment and blank line is compared where it sits, as part
of the key or table that owns it.

The order of operations follows the target. A table the target moved, or entries
it reordered, are stated whole: the delta syntax places a new key or table, it
does not move an old one. ``PyprojectSource.fold`` re-renders every derivation
and rejects one that does not reproduce its target.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Final

import itertools

from copybarista.delta_render import DeltaError, render
from copybarista.toml_layout import layout, uncommented
from copybarista.toml_lines import (
    Block,
    Document,
    Entry,
    KeyPath,
    parse,
    remark,
    spell,
    spelled_key,
    split_entry,
    split_header,
)
from copybarista.toml_lists import ListError, ListValue


if TYPE_CHECKING:
    from collections.abc import Sequence


_CLEAR: Final = "# (clear)\n"
_OPS: Final = ("+", "-", "!", "=", "^")


def derive(base: str, target: str) -> str:
    """Return the delta text that renders ``target`` from ``base``.

    Args:
      base: The document the delta applies to.
      target: The document the delta must produce.

    Returns:
      delta: The operations as delta text; empty when the two agree.

    """
    old, new = parse(base), parse(target)
    restated = _restated_arrays(old.blocks, new.blocks)
    listed = {
        p for p in restated if _array_ops(old.blocks, new.blocks, path=p) is not None
    }
    # The shortest spelling first: arrays as ``-``/``+`` lists, new tables on the
    # default lead. Each is kept only if it renders the target; the last states
    # every changed array and lead whole.
    tries = ((listed, False), (set[KeyPath](), False))
    for chosen, exact in tries:
        delta = _derived(old, new, restated=restated, listed=chosen, exact=exact)
        if _renders(base, delta, target=target):
            return delta
    return _derived(old, new, restated=restated, listed=set(), exact=True)


def derive_values(base: str, target: str) -> str:
    """Return the operations turning ``base``'s content into ``target``'s, comments aside.

    The operations carry no comment or blank line, so rendering them onto
    ``base`` shows where its own comments and blank lines land once only the
    values change: a deleted key takes the lines it owns, a new table gets the
    blank line render gives one.

    Args:
      base: The document the operations apply to.
      target: The document whose content they reproduce.

    Returns:
      delta: The value operations as delta text; empty when the contents agree.

    """
    # Both sides take the base's comments, so an entry left as it stood is no edit,
    # and one respelled, moved or changed is written with them. Comparing the
    # commented target with an uncommented base made every commented entry an edit,
    # and a merge restated it over the source's own change.
    commented = parse(base)
    old = _with_base_comments(commented, parse(uncommented(base)))
    new = _with_base_comments(commented, parse(uncommented(target)))
    restated = _restated_arrays(old.blocks, new.blocks)
    # pragma: no mutate start -- exact=None is falsy too.
    return _derived(old, new, restated=restated, listed=set(), exact=False)
    # pragma: no mutate end


# A table emptied of every key is stated whole, so it takes the lines above its header
# and the remark ending it from the base; any other table keeps its own in place. A
# replaced value keeps what sat beside it: the remark ending its line, and in a list
# replaced entry for entry, the comment lines inside it.
def _with_base_comments(base: Document, target: Document) -> Document:
    """Return ``target`` with each kept key's, and each emptied table's, base comments."""
    tables = {
        block.path: block
        for block in base.blocks
        if not block.array and block.path and block.entries
    }
    entries = {
        (block.path, entry.key): entry
        for block in base.blocks
        if not block.array
        for entry in block.entries
    }
    blocks = tuple(
        block
        if block.array
        else replace(
            block,
            lead=block.lead if emptied is None else emptied.lead,
            header=block.header if emptied is None else emptied.header,
            entries=tuple(
                _commented(entries.get((block.path, entry.key)), entry)
                for entry in block.entries
            ),
        )
        for block in target.blocks
        for emptied in (None if block.entries else tables.get(block.path),)
    )
    return replace(target, blocks=blocks)


def _with_remark(base: Entry, entry: Entry) -> Entry:
    """Return one-line ``entry`` ending in ``base``'s remark, when ``base`` has one."""
    base_rest, value = split_entry(base.text)[1], split_entry(entry.text)[1]
    if "\n" in base_rest.rstrip("\n") or "\n" in value.rstrip("\n"):
        return entry
    notes = [str(what) for kind, what in layout(base.text) if kind == "remark"]
    if not notes:
        return entry
    key = split_entry(entry.text)[0]
    return replace(entry, text=f"{key}= {value.strip()}  {notes[-1]}\n")


def _commented(base: Entry | None, entry: Entry) -> Entry:
    """Return ``entry``'s new value written into ``base``'s comments, where it fits."""
    if base is None:
        return entry
    # The comment lines above it travel with it, so an entry written elsewhere keeps
    # them; ``uncommented`` took them from ``entry``.
    entry = replace(entry, lead=base.lead)
    try:
        before, after = ListValue.parse(base.text), ListValue.parse(entry.text)
    except ListError:
        return _with_remark(base, entry)
    if before.lines is None:
        return entry
    if len(before.values()) != len(after.values()):
        return _with_list_comments(before, after, entry=entry)
    lines = list(before.lines)
    values = iter(after.values())
    for index, line in enumerate(lines):
        if line.value:
            lines[index] = replace(line, value=next(values))
    return replace(entry, text=replace(before, lines=tuple(lines)).text())


# Entries removed or appended, the rest kept in order, edit the base list as ``-key``
# and ``+key`` do: a removed entry takes the comments only it used; the others stay.
def _with_list_comments(before: ListValue, after: ListValue, *, entry: Entry) -> Entry:
    """Return ``entry`` as ``before`` with the removed and appended entries applied."""
    old = [spelled_key(v) for v in before.values()]
    new = [spelled_key(v) for v in after.values()]
    kept = [v for v in old if v in new]
    if _repeats(old) or _repeats(new) or new[: len(kept)] != kept:
        return entry
    removed = [t for t, v in zip(before.values(), old, strict=True) if v not in new]
    added = [t for t, v in zip(after.values(), new, strict=True) if v not in old]
    edited = before.without(removed)
    if added:
        edited = edited.appended(ListValue.parse(f"v = [{', '.join(added)}]\n"))
    return replace(entry, text=edited.text())


def _renders(base: str, delta: str, *, target: str) -> bool:
    try:
        return render(base, delta) == target
    except DeltaError:
        return False


def _derived(
    old: Document,
    new: Document,
    *,
    restated: set[KeyPath],
    listed: set[KeyPath],
    exact: bool,
) -> str:
    """Return the delta text, writing each array in ``listed`` as list operations."""
    body = _blocks(
        old.blocks,
        new.blocks,
        restated=restated,
        listed=listed,
        exact=exact,
    )
    preamble = _preamble(old.preamble, new.preamble)
    footer = _run(old.footer, new.footer)
    if not body and not preamble:
        return "".join(footer)
    if footer and not body:
        # A comment-only delta reads as a preamble when a blank line closes it.
        body = "\n"
    elif footer or body.endswith(_CLEAR):
        # Closes the last table, so its tail stays a tail and the footer a footer.
        body += "\n"
    if body.startswith("\n") and new.preamble and not preamble:
        # Without a preamble in the delta, a blank line atop it reads as nothing:
        # an exact lead opening the body needs the preamble stated above it.
        preamble = _preamble((), new.preamble)
    return "".join((*preamble, body, *footer))


# The parser reads a preamble as comments closed by one blank line, so the delta's
# preamble is always that shape: its comments, or a lone clear marker to remove one.
def _preamble(old: tuple[str, ...], new: tuple[str, ...]) -> tuple[str, ...]:
    """Return the delta lines that turn preamble ``old`` into ``new``."""
    if old == new:
        return ()
    comments = tuple(line for line in new if line.strip())
    return (*(comments or (_CLEAR,)), "\n")


# A run of comment lines alone is written as is; any other is stated exactly, closed by
# the clear marker.
def _run(old: tuple[str, ...], new: tuple[str, ...]) -> tuple[str, ...]:
    """Return the lines that turn run ``old`` into ``new``: none when they agree."""
    if old == new:
        return ()
    if new and all(line.strip() for line in new):
        return new
    return (*new, _CLEAR)


# Kept tables are edited in place. A table the target moved among the others is deleted
# and stated again; render places a new table after its nearest relative, so the
# derivation is checked by rendering it, never assumed.
def _blocks(
    old: tuple[Block, ...],
    new: tuple[Block, ...],
    *,
    restated: set[KeyPath],
    listed: set[KeyPath],
    exact: bool,
) -> str:
    """Return the delta text, writing each array in ``listed`` as list operations."""
    stated = restated - listed
    old_ids = [i for i in _ids(old) if not _array_id(i, restated)]
    new_ids = [i for i in _ids(new) if not _array_id(i, restated)]
    gone = [p for p in stated if not any(b.path == p and b.array for b in new)]
    moved = _moved(old_ids, new_ids)
    deleted = [i[0] for i in old_ids if i not in new_ids and i[0]]
    deleted += [i[0] for i in moved] + gone
    # Each operation on a child goes under its parent's header where the delta has
    # one, so no dotted key there redeclares a table a header declares.
    headed = {b.path for b in new[1:] if not b.array}
    # Render prunes a table its deletes empty; one the target keeps empty is
    # deleted, under its parent's header, then stated whole.
    emptied = {
        ident
        for block, ident in zip(new[1:], _ids(new)[1:], strict=True)
        if ident in old_ids
        and ident not in moved
        and not _array_id(ident, stated)
        and _emptied(old[_ids(old).index(ident)], block, new=new)
    }
    extras: dict[KeyPath, list[str]] = {}
    for path in _roots([*deleted, *(i[0] for i in emptied)]):
        at = _owner(path, headed=headed)
        extras.setdefault(at, []).append(f"{_op_key('!', path[len(at) :])} = true\n")
    for path in sorted(listed):
        at = _owner(path, headed=headed)
        extras.setdefault(at, []).extend(_array_ops(old, new, path=path, at=at) or [])
    parts: list[str] = []
    for block, ident in zip(new[1:], _ids(new)[1:], strict=True):
        if ident[0] in gone or (ident[0] in listed and _array_id(ident, listed)):
            continue
        if _array_id(ident, stated) or ident not in old_ids or ident in moved:
            own = [] if block.array else extras.pop(block.path, [])
            lead = _first_lead(old, block, stated=stated)
            parts.append(_stated(block, base=lead, exact=exact, extra=own))
        elif ident in emptied:
            parts.append(_stated(block, base=None, exact=True, extra=[]))
        else:
            own = [] if block.array else extras.pop(block.path, [])
            parts.append(_block_ops(old[_ids(old).index(ident)], block, extra=own))
    root = _table_ops(old[0], new[0]) if old and new else ""
    heads = [
        f"[{spell(path)}]\n{''.join(lines)}" for path, lines in extras.items() if path
    ]
    first = "".join(extras.get((), ()))
    return _joined([first, root, *heads, *parts])


def _owner(path: KeyPath, *, headed: set[KeyPath]) -> KeyPath:
    """Return the deepest headed table above ``path``, or ``()`` for the root."""
    return next(
        (path[:n] for n in range(len(path) - 1, 0, -1) if path[:n] in headed),
        (),
    )


def _roots(paths: list[KeyPath]) -> list[KeyPath]:
    """Return each path not already under another of ``paths``, once, in order."""
    roots = [p for p in paths if not any(p[: len(q)] == q and p != q for q in paths)]
    return list(dict.fromkeys(roots))


def _first_lead(
    old: tuple[Block, ...],
    block: Block,
    *,
    stated: set[KeyPath],
) -> tuple[str, ...] | None:
    """Return the lead a restated array's first element takes from the base, or None."""
    if block.path not in stated:
        return None
    first = next((b for b in old if b.array and b.path == block.path), None)
    return None if first is None else first.lead


def _array_ops(
    old: tuple[Block, ...],
    new: tuple[Block, ...],
    *,
    path: KeyPath,
    at: KeyPath = (),
) -> list[str] | None:
    """Return ``-path``/``+path`` lines for an array of tables, or None if they cannot."""
    before = [b for b in old if b.path[: len(path)] == path and b.path]
    after = [b for b in new if b.path[: len(path)] == path and b.path]
    if not after or not all(b.path == path and b.array for b in (*before, *after)):
        return None
    # Elements compare by what they hold, not the lines above them, so a repeat
    # under another lead is still a repeat; only the ones a list names need an
    # inline spelling.
    old_texts = [_held(b) for b in before]
    new_texts = [_held(b) for b in after]
    if _repeats(old_texts) or _repeats(new_texts):
        return None
    kept = [t for t in old_texts if t in new_texts]
    if not kept or new_texts[: len(kept)] != kept:
        return None
    removed = [_inline(b) for b in before if _held(b) not in new_texts]
    added = [b for b in after if _held(b) not in old_texts]
    spelled = [_inline(b) for b in added]
    if any(b.lead != ("\n",) or b.tail for b in added):
        return None
    if None in removed or None in spelled:
        return None
    key = path[len(at) :]
    lines: list[str] = []
    if removed:
        lines.append(f"{_op_key('-', key)} = [{', '.join(map(str, removed))}]\n")
    if added:
        lines.append(f"{_op_key('+', key)} = [{', '.join(map(str, spelled))}]\n")
    return lines


def _held(block: Block) -> str:
    return replace(block, lead=()).text()


def _inline(block: Block) -> str | None:
    """Return a plain element as an inline table, or None if it holds a comment."""
    if block.header != f"[[{spell(block.path)}]]\n":
        return None
    if any(
        e.lead or "\n" in e.text.rstrip("\n") or "#" in e.text for e in block.entries
    ):
        return None
    return "{" + ", ".join(e.text.strip() for e in block.entries) + "}"


def _emptied(old: Block, block: Block, *, new: tuple[Block, ...]) -> bool:
    """Return whether the target keeps table ``old`` with every key deleted."""
    nested = any(
        b.path[: len(block.path)] == block.path and b is not block for b in new
    )
    return bool(old.entries) and not block.entries and not nested and not block.array


def _moved(
    old_ids: list[tuple[KeyPath, int]],
    new_ids: list[tuple[KeyPath, int]],
) -> set[tuple[KeyPath, int]]:
    """Return the kept tables out of the base's order, fewest first."""
    kept = [i for i in new_ids if i in old_ids and i[0]]
    rank = {ident: index for index, ident in enumerate(old_ids)}
    order = [rank[i] for i in kept]
    keep = _longest_increasing(order)
    return {kept[index] for index in range(len(kept)) if index not in keep}


def _longest_increasing(values: list[int]) -> set[int]:
    """Return the indices of one longest strictly increasing subsequence."""
    best: list[list[int]] = []
    none: list[int] = []
    for index, value in enumerate(values):
        chain = max(
            (c for c in best if values[c[-1]] < value),
            key=len,
            default=none,
        )
        best.append([*chain, index])
    return set(max(best, key=len, default=none))


# Render drops the blank line that opens an exact table lead after another operation,
# so every part after the first takes one, its own lead's lines following it.
def _joined(parts: list[str]) -> str:
    """Join delta parts, each ending in one newline, one blank line apart."""
    kept = [part.rstrip("\n") + "\n" for part in parts if part.strip()]
    return "\n".join(kept)
    # The parser drops blank lines opening a file, so the first part's exact lead
    # must not open with them; derive states any it needs above the preamble.


def _ids(blocks: tuple[Block, ...]) -> list[tuple[KeyPath, int]]:
    """Return each block's identity: its path, and its index among array elements."""
    seen: dict[KeyPath, int] = {}
    ids: list[tuple[KeyPath, int]] = []
    for block in blocks:
        if block.array:
            seen[block.path] = seen.get(block.path, -1) + 1
            ids.append((block.path, seen[block.path]))
        else:
            ids.append((block.path, -1))
    return ids


def _restated_arrays(old: tuple[Block, ...], new: tuple[Block, ...]) -> set[KeyPath]:
    """Return each array of tables whose elements changed: those are stated whole."""
    paths = {block.path for block in (*old, *new) if block.array}
    return {path for path in paths if _array_text(old, path) != _array_text(new, path)}


def _array_text(blocks: tuple[Block, ...], path: KeyPath) -> list[str]:
    return [b.text() for b in blocks if b.path[: len(path)] == path and b.path]


def _array_id(ident: tuple[KeyPath, int], restated: set[KeyPath]) -> bool:
    return any(ident[0][: len(path)] == path for path in restated)


# An exact lead is closed with the clear marker, so it renders as the target has it; a
# plain one gives only its comments, over the blank line render gives a new table.
def _stated(
    block: Block,
    *,
    base: tuple[str, ...] | None,
    exact: bool,
    extra: list[str],
) -> str:
    """Return ``block`` written whole."""
    if block.lead == base:
        lead: tuple[str, ...] = ()
    elif exact:
        lead = (*block.lead, _CLEAR)
    else:
        lead = tuple(line for line in block.lead if line.strip())
    body = "".join(f"{''.join(e.lead)}{e.text}" for e in block.entries)
    return "".join((*lead, _delta_header(block), *extra, body, *block.tail))


def _block_ops(old: Block, new: Block, *, extra: list[str]) -> str:
    """Return the delta block turning table ``old`` into ``new``, or ``""``."""
    spelled = _delta_header(new)
    lead = _lead(old.lead, new.lead)
    header = _header(old.header, spelled)
    body = "".join(extra) + _table_ops(old, new)
    if lead is None and header is None and not body:
        return ""
    brackets = split_header(spelled)[0]
    return "".join((*(lead or ()), header or f"{brackets}\n", body))


def _delta_header(block: Block) -> str:
    """Return ``block``'s header as a delta writes it, a name read as an op escaped."""
    if not block.path[-1].startswith(_OPS):
        return block.header
    rest = split_header(block.header)[1]
    if block.array:
        return f"[[{_escaped(block.path)}]]{rest}"
    return f"[{_escaped(block.path)}]{rest}"


def _header(old: str, new: str) -> str | None:
    """Return a header line carrying the trailing-comment change, or None for none."""
    old_remark = remark(split_header(old)[1])
    new_remark = remark(split_header(new)[1])
    if old_remark == new_remark:
        return None
    brackets, rest = split_header(new)
    return f"{brackets}{rest}" if new_remark else f"{brackets}  {_CLEAR}"


# Render keeps the base's opening blank lines under a delta's comment lines, so only a
# lead whose comments alone changed is written plainly; any other change is stated
# exactly, after a clear marker.
def _lead(old: tuple[str, ...], new: tuple[str, ...]) -> tuple[str, ...] | None:
    """Return the delta lines that turn lead ``old`` into ``new``, or None."""
    if old == new:
        return None
    old_rest = old[len(_opening_blanks(old)) :]
    new_rest = new[len(_opening_blanks(new)) :]
    plain = (
        _opening_blanks(old) == _opening_blanks(new)
        and new_rest
        and all(line.strip() for line in new_rest)
        and all(line.strip() for line in old_rest)
    )
    return new_rest if plain else (*new, _CLEAR)


def _opening_blanks(lead: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(itertools.takewhile(lambda line: not line.strip(), lead))


def _table_ops(old: Block, new: Block) -> str:
    """Return the entry operations turning ``old``'s entries into ``new``'s."""
    lines: list[str] = []
    old_keys = [entry.key for entry in old.entries]
    new_keys = [entry.key for entry in new.entries]
    lines.extend(
        f"{_op_key('!', entry.key)} = true\n"
        for entry in old.entries
        if entry.key not in new_keys
    )
    kept = [key for key in old_keys if key in new_keys]
    moved: list[KeyPath] = []
    if kept != [key for key in new_keys if key in old_keys]:
        moved = kept
        lines.extend(f"{_op_key('!', key)} = true\n" for key in kept)
        old_keys = [key for key in old_keys if key not in kept]
    placed = list(old_keys)
    for index, entry in enumerate(new.entries):
        if entry.key in old_keys:
            lines.extend(_entry_ops(old.entries[old_keys.index(entry.key)], entry))
            continue
        # Anchor on the key placed just before, added ones included, so a run of
        # new keys lands in the target's order.
        anchor = _anchor(new_keys[:index], placed)
        placed.append(entry.key)
        lines.extend(
            _added(
                entry,
                anchor=anchor,
                # A moved key set again plainly is restated whole, in place: render
                # moves it only to an anchor.
                last=entry.key not in moved
                and not any(key in old_keys for key in new_keys[index + 1 :]),
            ),
        )
    if tail := _tail(old.tail, new.tail):
        # A blank line under them is what makes these lines the table's tail.
        lines.extend((*tail, "\n"))
    return "".join(lines)


def _anchor(before: list[KeyPath], old_keys: list[KeyPath]) -> KeyPath:
    """Return the base key a new entry follows; ``()`` for the start of the table."""
    kept = [key for key in before if key in old_keys]
    return kept[-1] if kept else ()


def _tail(old: tuple[str, ...], new: tuple[str, ...]) -> tuple[str, ...]:
    """Return a table's new closing comments, closed by the clear marker."""
    if old == new:
        return ()
    return (*new, _CLEAR)


def _entry_ops(old: Entry, new: Entry) -> list[str]:
    """Return the operations turning entry ``old`` into ``new``."""
    lead = _lead(old.lead, new.lead)
    old_rest, new_rest = split_entry(old.text)[1], split_entry(new.text)[1]
    if _squeezed(old_rest) == _squeezed(new_rest):
        return [*lead, _literal(new)] if lead is not None else []
    if (ops := _list_ops(old, new)) is not None:
        return [*(lead or ()), *ops]
    return [*(lead or ()), _literal(new)]


def _squeezed(text: str) -> str:
    return "".join(text.split())


def _literal(entry: Entry) -> str:
    """Return ``entry`` as a set operation, its key escaped if it reads as one."""
    if not entry.key[-1].startswith(_OPS):
        return entry.text
    rest = split_entry(entry.text)[1]
    return f"{spell((*entry.key[:-1], '=' + entry.key[-1]))} ={rest}"


def _escaped(key: KeyPath) -> str:
    """Return ``key`` spelled for a delta, its last part escaped if it reads as an op."""
    if not key[-1].startswith(_OPS):
        return spell(key)
    return spell((*key[:-1], "=" + key[-1]))


def _op_key(op: str, key: KeyPath) -> str:
    return spell((*key[:-1], op + key[-1]))


def _list_ops(old: Entry, new: Entry) -> list[str] | None:
    """Return ``-key``/``+key`` lines when they reproduce ``new``, else None."""
    try:
        before = ListValue.parse(old.text)
        after = ListValue.parse(new.text)
    except ListError:
        return None
    old_values = [spelled_key(v) for v in before.values()]
    new_values = [spelled_key(v) for v in after.values()]
    if _repeats(old_values) or _repeats(new_values):
        return None
    kept = [v for v in old_values if v in new_values]
    if not kept or new_values[: len(kept)] != kept:
        return None
    removed = [
        t
        for t, v in zip(before.values(), old_values, strict=True)
        if v not in new_values
    ]
    added = [
        t
        for t, v in zip(after.values(), new_values, strict=True)
        if v not in old_values
    ]
    expected = before.without(removed) if removed else before
    if added:
        expected = expected.appended(_added_lines(after, keep=old_values))
    if (
        expected.text() != new.text
        and split_entry(expected.text())[1] != split_entry(new.text)[1]
    ):
        return None
    lines: list[str] = []
    if removed:
        lines.append(f"{_op_key('-', new.key)} = [{', '.join(removed)}]\n")
    if added:
        added_list = _added_lines(after, keep=old_values)
        lines.append(
            f"{_op_key('+', new.key)} = {split_entry(added_list.text())[1].lstrip()}",
        )
    return lines


def _added_lines(after: ListValue, *, keep: list[object]) -> ListValue:
    """Return a list of ``after``'s new entries with the comment lines above each."""
    if after.lines is None:
        new = [v for v in after.values() if spelled_key(v) not in keep]
        return ListValue.parse(f"v = [{', '.join(new)}]\n")
    chosen: list[str] = []
    pending: list[str] = []
    for line in after.lines:
        if not line.value:
            pending.append(line.text())
            continue
        if spelled_key(line.value) not in keep:
            chosen.extend((*pending, line.text()))
        pending = []
    chosen.extend(pending)
    return ListValue.parse(f"v = [\n{''.join(chosen)}]\n")


def _repeats(values: Sequence[object]) -> bool:
    return any(v in values[:i] for i, v in enumerate(values))


def _added(entry: Entry, *, anchor: KeyPath, last: bool) -> list[str]:
    """Return the operation adding ``entry``: plain when last, else anchored."""
    lead = list(entry.lead) if any(line.strip() for line in entry.lead) else []
    if last:
        return [*lead, _literal(entry)]
    rest = split_entry(entry.text)[1]
    # The anchor is one quoted part holding the anchor's own spelling.
    place = spell((f"^{spell(anchor)}",)) if len(anchor) else '"^"'
    return [*lead, f"{place}.{_escaped(entry.key)} ={rest}"]
