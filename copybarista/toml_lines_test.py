from __future__ import annotations

from pathlib import Path
from typing import Final

import pytest

from copybarista.toml_lines import Block, Entry, LinesError, parse


_CWD: Final = Path(__file__).resolve().parent

# The nearest checkout root: the monorepo's, or the export's one level up.
_ROOT: Final = next(p for p in _CWD.parents if (p / "pyproject.toml").is_file())


def test_parse_splits_a_file_into_preamble_blocks_and_footer():
    doc = parse(
        "# Top.\n\na = 1\n\n# About t.\n[t]  # Why t.\nb = [\n  1,\n]\n# End.\n",
    )
    assert doc.preamble == ("# Top.\n", "\n")
    assert doc.blocks == (
        Block(
            lead=(),
            header="",
            path=(),
            array=False,
            entries=(Entry(lead=(), key=("a",), text="a = 1\n"),),
            tail=(),
        ),
        Block(
            lead=("\n", "# About t.\n"),
            header="[t]  # Why t.\n",
            path=("t",),
            array=False,
            entries=(Entry(lead=(), key=("b",), text="b = [\n  1,\n]\n"),),
            tail=(),
        ),
    )
    assert doc.footer == ("# End.\n",)


def test_parse_gives_a_comment_to_the_entry_below_it():
    doc = parse("[t]\na = 1\n# About b.\nb = 2\n")
    assert doc.blocks[1].entries[1] == Entry(
        lead=("# About b.\n",),
        key=("b",),
        text="b = 2\n",
    )


@pytest.mark.parametrize(
    ("gap", "lead"),
    [("# Tail of t.\n\n", "\n"), ("# Tail of t.\n\n# About u.\n", "\n# About u.\n")],
)
def test_parse_keeps_a_comment_a_blank_line_ends_with_the_block_above(
    gap: str,
    lead: str,
):
    doc = parse(f"[t]\na = 1\n{gap}[u]\nb = 1\n")
    assert doc.blocks[1].tail == ("# Tail of t.\n",)
    assert "".join(doc.blocks[2].lead) == lead
    root = parse(f"a = 1\n{gap}[u]\nb = 1\n")
    assert root.blocks[0].tail == ("# Tail of t.\n",)


@pytest.mark.parametrize(
    ("after", "tail", "footer"),
    [
        ("# End.\n", (), ("# End.\n",)),
        ("# Tail.\n\n# End.\n", ("# Tail.\n",), ("\n", "# End.\n")),
        ("# Tail.\n\n", ("# Tail.\n",), ("\n",)),
        ("", (), ()),
    ],
)
def test_parse_splits_the_end_of_the_file_as_a_header_would(
    after: str,
    tail: tuple[str, ...],
    footer: tuple[str, ...],
):
    doc = parse(f"[t]\na = 1\n{after}")
    assert (doc.blocks[-1].tail, doc.footer) == (tail, footer)


@pytest.mark.parametrize(
    "gap",
    ["\n# About u.\n", "# About u.\n", "\n# About u.\n\n", "\n"],
)
def test_parse_gives_any_other_comment_to_the_header_below(gap: str):
    doc = parse(f"[t]\na = 1\n{gap}[u]\nb = 1\n")
    assert doc.blocks[1].tail == ()
    assert "".join(doc.blocks[2].lead) == gap


@pytest.mark.parametrize(
    ("text", "preamble"),
    [
        ("# Top.\n\n[t]\n", ("# Top.\n", "\n")),
        ("# Top.\n\n\n[t]\n", ("# Top.\n", "\n")),
        ("# Top.\n[t]\n", ()),
        ("# Only.\n", ("# Only.\n",)),
        ("a = 1\n", ()),
    ],
)
def test_parse_reads_the_preamble_only_where_a_blank_line_closes_it(
    text: str,
    preamble: tuple[str, ...],
):
    assert parse(text).preamble == preamble


def test_parse_ends_a_last_statement_without_a_line_break_with_the_text():
    doc = parse("a = 1\nb = []")
    assert [e.text for e in doc.blocks[0].entries] == ["a = 1\n", "b = []"]
    assert doc.footer == ()


def test_parse_reads_dotted_and_quoted_keys():
    doc = parse('[t]\n"a.b" = 1\nc.d = 2\n')
    assert [entry.key for entry in doc.blocks[1].entries] == [("a.b",), ("c", "d")]


def test_parse_reads_array_tables_as_separate_blocks():
    doc = parse("[[i]]\nn = 1\n\n[[i]]\nn = 2\n")
    assert [block.path for block in doc.blocks[1:]] == [("i",), ("i",)]
    assert [block.array for block in doc.blocks[1:]] == [True, True]


def test_parse_keeps_a_multiline_string_in_one_entry():
    doc = parse('a = """\n# not a comment\n[not.a.header]\n"""\nb = 1\n')
    assert [entry.key for entry in doc.blocks[0].entries] == [("a",), ("b",)]


@pytest.mark.parametrize(
    "text",
    [
        "a = [\n  1,  # One.\n  2,\n]  # End.\nb = 1\n",
        "a = {x = 1}  # Why.\n",
        "a = '''\n[x]\n'''\n",
        "\n\n[t]\n\n\na = 1\n\n",
        "[t]\r\na = 1\r\n",
        "a = 1",
        "a = 1\nb = []",
        "[t]\na = 1\n# Tail one.\n# Tail two.\n\n[u]\n",
    ],
)
def test_parse_round_trips_every_layout(text: str):
    assert parse(text).text() == text


@pytest.mark.parametrize("text", ["a = [1\n", "[t\n"])
def test_parse_rejects_malformed_toml(text: str):
    with pytest.raises(LinesError):
        parse(text)


# The tree, not git: mutation testing runs the suite in a copy with no repository.
def _real_files() -> list[Path]:
    deltas = sorted(_ROOT.glob("loop/**/.export/pyproject.delta.toml"))
    return [_ROOT / "pyproject.toml", *deltas, *sorted(_ROOT.glob("ops/**/*.toml"))]


def _parent_name(path: Path) -> str:
    return path.parent.name


@pytest.mark.parametrize("path", _real_files(), ids=_parent_name)
def test_parse_round_trips_every_real_file_byte_for_byte(path: Path):
    text = path.read_text(encoding="utf-8")
    assert parse(text).text() == text


if __name__ == "__main__":
    from copybarista.lib.testing.runner import test_main

    test_main(__file__)
