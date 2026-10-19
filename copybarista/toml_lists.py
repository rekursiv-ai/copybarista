"""Append to and remove from a TOML list as written, comments included.

A list is edited in its own layout. A one-line list stays one line; a list with
one entry per line keeps its indent and every comment line, and an appended
entry lands after the last line, below any closing comment such as ``# NEVER
ADD MORE!``. Removing an entry also removes the comment lines directly above it,
unless the entry after it still shares them (``# torch`` above two torch pins).

Any other layout (two entries on a line, an entry spanning lines) is rejected
rather than guessed at.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

import tomllib

from treekle import from_plain


class ListError(ValueError):
    """A value is not a list, or not one this module can edit as written."""


@dataclass(frozen=True, slots=True, kw_only=True)
class _Line:
    """One line inside a one-entry-per-line list."""

    indent: str
    value: str  # Empty for a comment or blank line.
    comma: bool
    rest: str  # Spacing, a trailing comment, and the line break.

    def text(self) -> str:
        return f"{self.indent}{self.value}{',' if self.comma else ''}{self.rest}"


@dataclass(frozen=True, slots=True, kw_only=True)
class ListValue:
    """A ``key = [...]`` entry, split into what an edit keeps and what it moves.

    Attributes:
      head: The text up to and including ``[`` (and its line break, multi-line).
      lines: A multi-line list's lines, or None for a one-line list.
      items: A one-line list's entries as written.
      pad: The spaces just inside a one-line list's brackets.
      tail: The text from ``]`` (and its indent, multi-line) to the end.

    """

    head: str
    lines: tuple[_Line, ...] | None
    items: tuple[str, ...]
    pad: str
    tail: str

    @classmethod
    def parse(cls, text: str) -> ListValue:
        """Split the ``key = [...]`` entry ``text``.

        Args:
          text: The whole entry, key included.

        Returns:
          value: The entry split into what an edit keeps and what it moves.

        Raises:
          ListError: The value is not a list, or holds two entries on a line or
            one entry over several lines.

        """
        open_at = _skip_spaces(text, _after_equals(text))
        if text[open_at] != "[":
            raise ListError(f"not a list: {text.strip()!r}")
        line_end = text.find("\n", open_at)
        line_end = len(text) if line_end < 0 else line_end
        first = text[open_at + 1 : line_end].strip()
        if first and not first.startswith("#"):
            return cls._one_line(text, open_at=open_at)
        return cls._multi_line(text, body=line_end + 1)

    @classmethod
    def _one_line(cls, text: str, *, open_at: int) -> ListValue:
        close, items = open_at + 1, list[str]()
        start = close
        while text[close] != "]":
            end = _value_end(text, close)
            if end < 0:
                raise ListError(f"an entry spans lines: {text.strip()!r}")
            close = end
            if text[close] == ",":
                items.append(text[start:close].strip())
                close += 1
                start = close
            elif text[close] == "\n":
                raise ListError(f"a one-line list spans lines: {text.strip()!r}")
        if text[start:close].strip():
            items.append(text[start:close].strip())
        inside = text[open_at + 1 : close]
        pad = inside[: len(inside) - len(inside.lstrip(" "))]
        return cls(
            head=text[: open_at + 1],
            lines=None,
            items=tuple(items),
            pad=pad,
            tail=text[close:],
        )

    @classmethod
    def _multi_line(cls, text: str, *, body: int) -> ListValue:
        lines: list[_Line] = []
        pos = body
        while True:
            end = text.find("\n", pos)
            end = len(text) if end < 0 else end + 1
            line = text[pos:end]
            stripped = line.strip()
            indent = line[: len(line) - len(line.lstrip(" \t"))]
            if stripped.startswith("]"):
                return cls(
                    head=text[:body],
                    lines=tuple(lines),
                    items=(),
                    pad="",
                    tail=text[pos:],
                )
            if not stripped or stripped.startswith("#"):
                lines.append(
                    _Line(
                        indent=indent,
                        value="",
                        comma=False,
                        rest=line[len(indent) :],
                    ),
                )
            else:
                lines.append(_entry_line(line, indent=indent))
            pos = end

    def values(self) -> list[str]:
        """Return each entry as written."""
        if self.lines is None:
            return list(self.items)
        return [line.value for line in self.lines if line.value]

    def text(self) -> str:
        """Return the entry as written."""
        if self.lines is None:
            pad = self.pad if self.items else ""
            return f"{self.head}{pad}{', '.join(self.items)}{pad}{self.tail}"
        return f"{self.head}{''.join(line.text() for line in self.lines)}{self.tail}"

    def appended(self, extra: ListValue) -> ListValue:
        """Return this list with ``extra``'s entries after its last line.

        Args:
          extra: The entries to append, with the comment lines above each.

        Returns:
          value: The list in its own layout, ``extra`` appended.

        """
        if self.lines is None:
            # The blank inside an empty ``[ ]`` is not padding around entries.
            pad = self.pad if self.items else ""
            return replace(self, items=(*self.items, *extra.values()), pad=pad)
        indent = next(
            (line.indent for line in self.lines if line.value or line.rest.strip()),
            "    ",
        )
        lines = list(self.lines)
        last = max((i for i, line in enumerate(lines) if line.value), default=None)
        if last is not None:
            lines[last] = replace(lines[last], comma=True)
        added = (
            [_Line(indent=indent, value=v, comma=True, rest="\n") for v in extra.items]
            if extra.lines is None
            else [
                replace(line, indent=indent, comma=bool(line.value))
                for line in extra.lines
                if line.value or line.rest.strip()
            ]
        )
        return replace(self, lines=(*lines, *added))

    def without(self, removed: list[str]) -> ListValue:
        """Return this list without ``removed``, matched by content.

        Args:
          removed: The entries to remove, as written.

        Returns:
          value: The list without them and the comment lines only they used.

        Raises:
          ListError: The list lacks one of ``removed``.

        """
        result = self
        for entry in removed:
            result = result._without_one(entry)
        return result

    def _without_one(self, entry: str) -> ListValue:
        wanted = _content(entry)
        index = next(
            (i for i, value in enumerate(self.values()) if _content(value) == wanted),
            None,
        )
        if index is None:
            raise ListError(f"the list lacks {entry!r}")
        if self.lines is None:
            return replace(self, items=self.items[:index] + self.items[index + 1 :])
        lines = list(self.lines)
        at = [i for i, line in enumerate(lines) if line.value][index]
        start = at
        # A comment above an entry is the entry's alone unless an entry follows it.
        following = lines[at + 1] if at + 1 < len(lines) else None
        if following is None or not following.value:
            while (
                start and not lines[start - 1].value and lines[start - 1].rest.strip()
            ):
                start -= 1
        del lines[start : at + 1]
        return replace(self, lines=tuple(lines))


def _entry_line(line: str, *, indent: str) -> _Line:
    """Split one entry line into its value, comma, and the rest."""
    start = len(indent)
    end = _value_end(line, start)
    if end < 0:
        raise ListError(f"an entry spans lines: {line.strip()!r}")
    value = line[start:end].rstrip()
    after = line[start + len(value) :]
    comma = after.lstrip(" \t").startswith(",")
    rest = after.lstrip(" \t")[1:] if comma else after
    if rest.strip() and not rest.lstrip().startswith("#"):
        raise ListError(f"two entries share a line: {line.strip()!r}")
    return _Line(indent=indent, value=value, comma=comma, rest=rest)


def _after_equals(text: str) -> int:
    """Return the index just past the ``=`` that ends the entry's key."""
    pos = 0
    while text[pos] != "=":
        pos = _string_end(text, pos) if text[pos] in "\"'" else pos + 1
    return pos + 1


def _skip_spaces(text: str, pos: int) -> int:
    while text[pos] in " \t":
        pos += 1
    return pos


# Returns -1 when a bracket the value opens is still open at the line break.
def _value_end(text: str, pos: int) -> int:
    """Return where the value at ``pos`` ends: at a comma, ``]``, ``#``, or line break."""
    depth = 0
    while pos < len(text):
        char = text[pos]
        if char in "\"'":
            if text.startswith(char * 3, pos):
                return -1
            pos = _string_end(text, pos)
            continue
        if char in "[{":
            depth += 1
        elif char in "]}" and depth:
            depth -= 1
        elif char == "\n" and depth:
            return -1
        elif not depth and char in ",]#\n":
            return pos
        pos += 1
    return pos


def _string_end(text: str, pos: int) -> int:
    """Return the index just past the one-line string opening at ``pos``."""
    quote = text[pos]
    pos += 1
    while text[pos] != quote:
        pos += 2 if quote == '"' and text[pos] == "\\" else 1
    return pos + 1


def _content(value: str) -> object:
    return from_plain(tomllib.loads(f"v = {value}"), dict[str, object])["v"]
