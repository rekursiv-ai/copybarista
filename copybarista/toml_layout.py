"""Read where every key, table, list entry, and comment of a TOML file sits.

``tomllib`` reads a file's content and drops its comments; ``layout`` reads the
rest. Together they say whether two spellings of a file are the same file: equal
content, every comment in the same place relative to the same keys, and the same
blank lines and indentation between lines. Spaces within a line, quoting, and line
breaks inside a value do not count.

Each event is ``(kind, what)``:

- ``("header", path)``: a ``[table]`` or ``[[array]]`` line.
- ``("key", path)``: a ``key = value``, inline-table keys included.
- ``("entry", path)``: a list entry, its index the last part of ``path``.
- ``("close", path)``: the ``]`` or ``}`` ending the list or inline table at
  ``path``, so a comment before it and one after it differ.
- ``("comment", text)``: a comment on a line of its own.
- ``("remark", text)``: a comment ending a line that holds something else.
- ``("blank", "")``: an empty line between two others, outside any value.
- ``("indent", text)``: the spaces opening a line outside any value.

A path names a key from the document root; an ``[[array]]`` element is its
index, so ``[i.s]`` under the second ``[[i]]`` is ``("i", 1, "s")``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import tomllib

from treekle import from_plain


type KeyPath = tuple[str | int, ...]
type Event = tuple[str, KeyPath | str]


class LayoutError(ValueError):
    """The text is not TOML."""


def layout(text: str) -> tuple[Event, ...]:
    """Return the events of ``text`` in file order.

    Args:
      text: A TOML document.

    Returns:
      events: ``(kind, path)`` per header, key, and entry, and ``(kind, text)``
        per comment.

    Raises:
      LayoutError: ``text`` is not TOML.

    """
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError as err:
        raise LayoutError(str(err)) from err
    scanner = _Scanner(text=text)
    scanner.document()
    return tuple(scanner.events)


_BARE: frozenset[str] = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-",
)


# Only valid TOML reaches the scanner, so it reads positions and never validates.
@dataclass(slots=True, kw_only=True)
class _Scanner:
    text: str
    pos: int = 0
    # Whether the current line already holds something, so a comment is a remark.
    busy: bool = False
    # Lists and inline tables open around ``pos``; their lines are one value.
    depth: int = 0
    table: KeyPath = ()
    events: list[Event] = field(default_factory=list[Event])
    counts: dict[KeyPath, int] = field(default_factory=dict[KeyPath, int])

    def document(self) -> None:
        self.line_start()
        while self.pos < len(self.text):
            self.gap()
            if self.pos >= len(self.text):
                return
            if self.text[self.pos] == "[":
                self.header()
            else:
                self.pair(prefix=self.table)

    def gap(self) -> None:
        """Skip spaces, line breaks, and comments, emitting each comment."""
        while self.pos < len(self.text):
            char = self.text[self.pos]
            if char in " \t\r":
                self.pos += 1
            elif char == "\n":
                self.pos += 1
                self.busy = False
                self.line_start()
            elif char == "#":
                end = self.text.find("\n", self.pos)
                end = len(self.text) if end < 0 else end
                comment = self.text[self.pos : end].rstrip("\r")
                self.events.append(("remark" if self.busy else "comment", comment))
                self.pos = end
            else:
                return

    def line_start(self) -> None:
        """Emit a blank line, or the indent of the line starting at ``pos``."""
        if self.depth:
            return
        end = self.pos
        while end < len(self.text) and self.text[end] in " \t":
            end += 1
        if end == len(self.text):
            return
        if self.text[end] in "\r\n":
            self.events.append(("blank", ""))
        elif end > self.pos:
            self.events.append(("indent", self.text[self.pos : end]))
        self.pos = end

    def emit(self, kind: str, path: KeyPath) -> None:
        self.events.append((kind, path))
        self.busy = True

    def header(self) -> None:
        array = self.text.startswith("[[", self.pos)
        self.pos += 2 if array else 1
        keys = self.keys()
        self.pos += 2 if array else 1
        parts: list[str | int] = []
        for level, key in enumerate(keys, start=1):
            parts.append(key)
            path: KeyPath = tuple(parts)
            if array and level == len(keys):
                self.counts[path] = self.counts.get(path, -1) + 1
            # Under an array of tables, a key names its newest element.
            if path in self.counts:
                parts.append(self.counts[path])
        self.table = tuple(parts)
        self.emit("header", self.table)

    def pair(self, *, prefix: KeyPath) -> None:
        path = (*prefix, *self.keys())
        self.pos += 1  # The ``=``.
        self.spaces()
        self.emit("key", path)
        self.value(path)

    def keys(self) -> list[str]:
        keys: list[str] = []
        while True:
            self.spaces()
            keys.append(self.key())
            self.spaces()
            if self.text[self.pos] != ".":
                return keys
            self.pos += 1

    def key(self) -> str:
        start = self.pos
        if self.text[start] in "\"'":
            self.string()
            quoted = from_plain(
                tomllib.loads(f"k = {self.text[start : self.pos]}"),
                dict[str, str],
            )
            return quoted["k"]
        while self.text[self.pos] in _BARE:
            self.pos += 1
        return self.text[start : self.pos]

    def spaces(self) -> None:
        while self.text[self.pos] in " \t":
            self.pos += 1

    def value(self, path: KeyPath) -> None:
        char = self.text[self.pos]
        if char in "\"'":
            self.string()
        elif char == "[":
            self.array(path)
        elif char == "{":
            self.inline(path)
        else:
            while self.pos < len(self.text) and self.text[self.pos] not in ",]}#\r\n":
                self.pos += 1

    def string(self) -> None:
        quote = self.text[self.pos]
        if self.text.startswith(quote * 3, self.pos):
            self.pos += 3
            while not self.text.startswith(quote * 3, self.pos):
                self.pos += 2 if quote == '"' and self.text[self.pos] == "\\" else 1
            # Up to two quotes before the closing three belong to the string.
            run = 3
            while run < 5 and self.text.startswith(quote, self.pos + run):
                run += 1
            self.pos += run
            return
        self.pos += 1
        while self.text[self.pos] != quote:
            self.pos += 2 if quote == '"' and self.text[self.pos] == "\\" else 1
        self.pos += 1

    def array(self, path: KeyPath) -> None:
        self.pos += 1
        self.depth += 1
        index = 0
        while True:
            self.gap()
            if self.text[self.pos] == "]":
                self.close(path)
                return
            entry = (*path, index)
            self.emit("entry", entry)
            self.value(entry)
            index += 1
            self.gap()
            if self.text[self.pos] == ",":
                self.pos += 1

    def inline(self, path: KeyPath) -> None:
        self.pos += 1
        self.depth += 1
        while True:
            self.spaces()
            if self.text[self.pos] == "}":
                self.close(path)
                return
            self.pair(prefix=path)
            self.spaces()
            if self.text[self.pos] == ",":
                self.pos += 1

    def close(self, path: KeyPath) -> None:
        self.pos += 1
        self.depth -= 1
        self.emit("close", path)
