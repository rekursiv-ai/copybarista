"""Read a TOML file as the lines each key and table owns.

A ``pyproject.toml`` is edited by people, so every comment and blank line in it
says something about the key or table it sits beside. This model hands each one
to exactly one owner, so an edit that moves, replaces, or deletes the owner
takes its lines along and leaves every other line where it was:

- The **preamble** is the comment block atop the file and the one blank line
  that closes it.
- A **block** is one table: its ``[header]`` line with its **lead**, the comment
  and blank lines directly above it; then its **entries**; then its **tail**,
  comments directly under its last entry that a blank line separates from the
  next header. The first block is the root: no header, the keys above the first
  one, and a tail as any other block's.
- An **entry** is one ``key = value``, every line of a multi-line value included,
  with its lead: the comment and blank lines directly above it inside the block.
- The **footer** is what follows the last statement, less the last block's tail:
  the end of the file splits the lines after the last statement as a header
  would.

Text is kept exactly as written, so ``parse(text).text() == text``.
``tomllib`` validates the file; this module only splits it.
"""

from __future__ import annotations

from dataclasses import dataclass

import functools
import itertools
import tomllib

from treekle import from_plain


type KeyPath = tuple[str, ...]


class LinesError(ValueError):
    """The text is not TOML."""


@dataclass(frozen=True, slots=True, kw_only=True)
class Entry:
    """One ``key = value`` and the comment and blank lines directly above it."""

    lead: tuple[str, ...]
    key: KeyPath
    text: str


@dataclass(frozen=True, slots=True, kw_only=True)
class Block:
    """One table: its header line, the lines above it, and its entries.

    Attributes:
      lead: Comment and blank lines directly above the header.
      header: The ``[header]`` line as written; empty for the root block.
      path: The table's key path; ``()`` for the root block.
      array: Whether the header is ``[[...]]``.
      entries: The table's keys, in file order.
      tail: Comments directly under the last entry that a blank line ends.

    """

    lead: tuple[str, ...]
    header: str
    path: KeyPath
    array: bool
    entries: tuple[Entry, ...]
    tail: tuple[str, ...]

    def text(self) -> str:
        """Return the block as written."""
        body = "".join(f"{''.join(entry.lead)}{entry.text}" for entry in self.entries)
        return f"{''.join(self.lead)}{self.header}{body}{''.join(self.tail)}"


@dataclass(frozen=True, slots=True, kw_only=True)
class Document:
    """A TOML file as its preamble, blocks, and footer."""

    preamble: tuple[str, ...]
    blocks: tuple[Block, ...]
    footer: tuple[str, ...]

    def text(self) -> str:
        """Return the file as written."""
        body = "".join(block.text() for block in self.blocks)
        return f"{''.join(self.preamble)}{body}{''.join(self.footer)}"


# One fold parses the same base and delta texts many times over, and a Document is
# immutable, so a text is parsed once. An error is not cached and raises again.
@functools.lru_cache(maxsize=256)
def parse(text: str) -> Document:
    """Split ``text`` into the lines each key and table owns.

    Args:
      text: A TOML document.

    Returns:
      document: The preamble, the root block then one block per header, and the
        footer.

    Raises:
      LinesError: ``text`` is not TOML.

    """
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError as err:
        raise LinesError(str(err)) from err
    lines = text.splitlines(keepends=True)
    statements = _Scanner(text=text).statements()
    first = statements[0].start if statements else len(lines)
    preamble = _preamble_end(lines[:first], whole=not statements)
    blocks = [Block(lead=(), header="", path=(), array=False, entries=(), tail=())]
    entries: list[Entry] = []
    cursor = preamble
    for statement in statements:
        gap = tuple(lines[cursor : statement.start])
        if statement.header:
            previous = blocks.pop()
            ended = previous.header or entries
            tail, lead = _split_gap(gap) if ended else ((), gap)
            blocks.append(_finished(previous, entries=entries, tail=tail))
            entries = []
            blocks.append(
                Block(
                    lead=lead,
                    header=lines[statement.start],
                    path=statement.key,
                    array=statement.array,
                    entries=(),
                    tail=(),
                ),
            )
        else:
            text_lines = lines[statement.start : statement.stop]
            entries.append(Entry(lead=gap, key=statement.key, text="".join(text_lines)))
        cursor = statement.stop
    tail, footer = _split_gap(tuple(lines[cursor:])) if statements else ((), ())
    blocks.append(_finished(blocks.pop(), entries=entries, tail=tail))
    return Document(
        preamble=tuple(lines[:preamble]),
        blocks=tuple(blocks),
        footer=footer if statements else tuple(lines[cursor:]),
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class _Statement:
    """One header or ``key = value``: the lines ``[start, stop)`` it spans."""

    start: int
    stop: int
    key: KeyPath
    header: bool
    array: bool


def split_entry(text: str) -> tuple[str, str]:
    """Split ``key = value`` text at its ``=``: the key as written, then the rest."""
    pos = 0
    while text[pos] != "=":
        pos = _string_end(text, pos) if text[pos] in "\"'" else pos + 1
    return text[:pos], text[pos + 1 :]


def split_header(line: str) -> tuple[str, str]:
    """Split a ``[header]`` line after its closing bracket.

    Args:
      line: The header line as written.

    Returns:
      parts: The brackets and key, then the rest of the line.

    """
    pos = line.index("[")
    array = line.startswith("[[", pos)
    pos += 2 if array else 1
    while line[pos] != "]":
        pos = _string_end(line, pos) if line[pos] in "\"'" else pos + 1
    end = pos + (2 if array else 1)
    return line[:end], line[end:]


def spell(keys: KeyPath) -> str:
    """Return ``keys`` as a dotted TOML key, quoting each part that needs it.

    A part starting with ``-`` is a bare key to TOML, but quoted here: a delta
    reads it as an operation either way, and quoted it reads as one to people.

    Args:
      keys: The key path.

    Returns:
      key: The dotted key, as a delta writes it.

    """
    return ".".join(
        key if _is_bare(key) and not key.startswith("-") else _quoted(key)
        for key in keys
    )


def split_key(spelled: str) -> KeyPath:
    """Return the key path a dotted TOML key spells, the inverse of ``spell``.

    Args:
      spelled: A dotted key, each part bare or quoted.

    Returns:
      keys: The key path.

    """
    return _Scanner(text=f"{spelled}=").keys()


def remark(rest: str) -> str:
    """Return the comment in the text after a value or header, or ``""``."""
    stripped = rest.strip()
    return stripped if stripped.startswith("#") else ""


def _is_bare(key: str) -> bool:
    return bool(key) and all(
        char.isascii() and (char.isalnum() or char in "_-") for char in key
    )


def _quoted(key: str) -> str:
    escaped = key.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _string_end(text: str, pos: int) -> int:
    """Return the index just past the one-line string opening at ``pos``."""
    quote = text[pos]
    pos += 1
    while text[pos] != quote:
        pos += 2 if quote == '"' and text[pos] == "\\" else 1
    return pos + 1


def _finished(block: Block, *, entries: list[Entry], tail: tuple[str, ...]) -> Block:
    return Block(
        lead=block.lead,
        header=block.header,
        path=block.path,
        array=block.array,
        entries=tuple(entries),
        tail=tail,
    )


# One blank line closes the preamble; any more open the first statement's lead, so a
# delta can state that lead exactly.
def _preamble_end(lines: list[str], *, whole: bool) -> int:
    """Return the index past the comment block and the blank line closing it."""
    if whole:
        return len(lines)
    comments = len(list(itertools.takewhile(_is_comment, lines)))
    if not comments or comments == len(lines) or lines[comments].strip():
        return 0
    return comments + 1


# Only comments directly under the last entry, with a blank line after them, belong
# to the block above (``# Tail of t.``). A comment a blank line sets apart from that
# entry, or one touching the header, introduces the header below, so it survives
# deleting or replacing the table above.
def _split_gap(gap: tuple[str, ...]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Split the lines between two tables into the tail above and the lead below."""
    comments = len(list(itertools.takewhile(_is_comment, gap)))
    if comments and comments < len(gap):
        return gap[:comments], gap[comments:]
    return (), gap


def _is_comment(line: str) -> bool:
    return line.lstrip().startswith("#")


# Only text ``tomllib`` accepted reaches the scanner, so it reads positions and
# never validates.
@dataclass(slots=True, kw_only=True)
class _Scanner:
    """Find the lines each statement spans, skipping strings and values whole."""

    text: str
    pos: int = 0
    line: int = 0

    def statements(self) -> list[_Statement]:
        found: list[_Statement] = []
        while True:
            self.gap()
            if self.pos >= len(self.text):
                return found
            start = self.line
            if self.text[self.pos] == "[":
                array = self.text.startswith("[[", self.pos)
                self.advance(2 if array else 1)
                key = self.keys()
                header = True
            else:
                array = False
                key = self.keys()
                self.advance()  # The ``=``.
                self.value()
                header = False
            self.end_of_line()
            # A last line with no line break still ends where the text does.
            unterminated = not self.text.endswith("\n") and self.pos == len(self.text)
            found.append(
                _Statement(
                    start=start,
                    stop=self.line + unterminated,
                    key=key,
                    header=header,
                    array=array,
                ),
            )

    def advance(self, count: int = 1) -> None:
        for _ in range(count):
            self.line += self.text[self.pos] == "\n"
            self.pos += 1

    def gap(self) -> None:
        """Skip blank and comment lines."""
        while self.pos < len(self.text):
            if self.text[self.pos] in " \t\r\n":
                self.advance()
            elif self.text[self.pos] == "#":
                self.end_of_line()
            else:
                return

    def end_of_line(self) -> None:
        """Skip to the start of the next line, a closing ``]`` or comment included."""
        while self.pos < len(self.text) and self.text[self.pos] != "\n":
            self.advance()
        if self.pos < len(self.text):
            self.advance()

    def spaces(self) -> None:
        while self.text[self.pos] in " \t":
            self.advance()

    def keys(self) -> KeyPath:
        keys: list[str] = []
        while True:
            self.spaces()
            start = self.pos
            if self.text[start] in "\"'":
                self.string()
                spelled = self.text[start : self.pos]
                keys.append(
                    from_plain(tomllib.loads(f"k = {spelled}"), dict[str, str])["k"],
                )
            else:
                while self.text[self.pos] not in " \t.=]":
                    self.advance()
                keys.append(self.text[start : self.pos])
            self.spaces()
            if self.text[self.pos] != ".":
                return tuple(keys)
            self.advance()

    def string(self) -> None:
        quote = self.text[self.pos]
        escapes = quote == '"'
        if self.text.startswith(quote * 3, self.pos):
            self.advance(3)
            while not self.text.startswith(quote * 3, self.pos):
                self.advance(2 if escapes and self.text[self.pos] == "\\" else 1)
            # Up to two quotes before the closing three belong to the string.
            run = 3
            while run < 5 and self.text.startswith(quote, self.pos + run):
                run += 1
            self.advance(run)
            return
        self.advance()
        while self.text[self.pos] != quote:
            self.advance(2 if escapes and self.text[self.pos] == "\\" else 1)
        self.advance()

    def value(self) -> None:
        """Skip one value, every line of a list or inline table included."""
        depth = 0
        while self.pos < len(self.text):
            char = self.text[self.pos]
            if char in "\"'":
                self.string()
                continue
            if char == "#" or (char == "\n" and not depth):
                if not depth:
                    return
                while self.text[self.pos] != "\n":
                    self.advance()
                continue
            depth += (char in "[{") - (char in "]}")
            self.advance()
            if not depth and char in "]}":
                return
