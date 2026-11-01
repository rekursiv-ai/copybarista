from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from copybarista.toml_layout import LayoutError, code_lines, layout, uncommented


if TYPE_CHECKING:
    from collections.abc import Callable


def test_layout_lists_keys_headers_and_comments_in_order():
    text = '# Top.\n\n[t]  # Why t.\n# About a.\na = 1\nb.c = "x"  # Why c.\n'
    assert layout(text) == (
        ("comment", "# Top."),
        ("blank", ""),
        ("header", ("t",)),
        ("remark", "# Why t."),
        ("comment", "# About a."),
        ("key", ("t", "a")),
        ("key", ("t", "b", "c")),
        ("remark", "# Why c."),
    )


def test_layout_numbers_array_tables_and_their_subtables():
    text = "[[i]]\nn = 1\n\n[[i]]\nn = 2\n\n[i.s]\nk = 1\n"
    assert layout(text) == (
        ("header", ("i", 0)),
        ("key", ("i", 0, "n")),
        ("blank", ""),
        ("header", ("i", 1)),
        ("key", ("i", 1, "n")),
        ("blank", ""),
        ("header", ("i", 1, "s")),
        ("key", ("i", 1, "s", "k")),
    )


def test_layout_places_comments_between_list_entries():
    text = 'a = [\n    "x",  # Why x.\n    # About y.\n    "y",\n]\n'
    assert layout(text) == (
        ("key", ("a",)),
        ("entry", ("a", 0)),
        ("remark", "# Why x."),
        ("comment", "# About y."),
        ("entry", ("a", 1)),
        ("close", ("a",)),
    )


def test_layout_reads_inline_tables_and_nested_lists():
    assert layout("a = [{x = [1, 2]}, {y = 'z', w = 1}]\n") == (
        ("key", ("a",)),
        ("entry", ("a", 0)),
        ("key", ("a", 0, "x")),
        ("entry", ("a", 0, "x", 0)),
        ("entry", ("a", 0, "x", 1)),
        ("close", ("a", 0, "x")),
        ("close", ("a", 0)),
        ("entry", ("a", 1)),
        ("key", ("a", 1, "y")),
        ("key", ("a", 1, "w")),
        ("close", ("a", 1)),
        ("close", ("a",)),
    )


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("a = [\n    1,\n    # N.\n]\nb = 1\n", "a = [\n    1,\n]\n# N.\nb = 1\n"),
        ("a = [\n    1,\n]  # N.\nb = 1\n", "a = [\n    1,\n]\n# N.\nb = 1\n"),
        ("a = {x = 1}  # N.\nb = 1\n", "a = {x = 1}\n# N.\nb = 1\n"),
    ],
)
def test_layout_tells_a_comment_inside_a_value_from_one_after_it(
    left: str,
    right: str,
):
    assert layout(left) != layout(right)


def test_layout_counts_blank_lines_between_lines_but_not_inside_values():
    assert layout("a = 1\n\nb = 1\n") == (
        ("key", ("a",)),
        ("blank", ""),
        ("key", ("b",)),
    )
    assert layout("a = [\n\n    1,\n]\n") == layout("a = [1]\n")


def test_layout_counts_the_indent_of_a_line():
    assert layout("[t]\n a = 1\n") == (
        ("header", ("t",)),
        ("indent", " "),
        ("key", ("t", "a")),
    )
    assert layout(" [t]\n")[0] == ("indent", " ")


@pytest.mark.parametrize(
    "text",
    ['a = """x""""\n', 'a = """x"""""\n', "a = '''x''''\n"],
)
def test_layout_reads_quotes_that_end_a_multiline_string(text: str):
    assert layout(f"{text}b = 1\n") == (("key", ("a",)), ("key", ("b",)))


@pytest.mark.parametrize(
    "text",
    [
        'a = "#not"\n',
        "a = '#not'\n",
        'a = """\n# not\n"""\n',
        "a = '''\n# not\n'''\n",
        'a = "q\\"#not"\n',
        '"#k" = 1\n',
    ],
)
def test_layout_ignores_a_hash_inside_a_string(text: str):
    assert [kind for kind, _ in layout(text)] == ["key"]


def test_layout_unquotes_keys():
    assert layout('"a.b" = 1\n\'c\'.d = 2\n["x y"."z"]\n') == (
        ("key", ("a.b",)),
        ("key", ("c", "d")),
        ("header", ("x y", "z")),
    )


def test_layout_ignores_spacing_within_a_line_and_line_breaks_within_a_value():
    assert layout("a=1\nb   =   [1,2]\n") == layout("a = 1\nb = [\n  1,\n  2,\n]\n")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("a = 1  # Why.\n", [((), "a=1", "# Why.")]),
        ('a = "x # y"\n', [((), 'a="x#y"', "")]),
        (
            "a = [\n    # Lead.\n\n    1,  # One.\n]\n",
            [((), "a=[", ""), (("# Lead.", ""), "1,", "# One."), ((), "]", "")],
        ),
        (
            'a = """\n# not\n\n"""  # Why.\n',
            [((), 'a="""', ""), ((), "#not", ""), ((), "", ""), ((), '"""', "# Why.")],
        ),
        ("a = 1\n# End.\n\n", [((), "a=1", ""), (("# End.", ""), "", "")]),
        (
            'a = """\nx\n"""\n\nb = """\n# y\n"""\n',
            [
                ((), 'a="""', ""),
                ((), "x", ""),
                ((), '"""', ""),
                (("",), 'b="""', ""),
                ((), "#y", ""),
                ((), '"""', ""),
            ],
        ),
        ("a = 1\r\n# B.\r\nb = 2\r\n", [((), "a=1", ""), (("# B.",), "b=2", "")]),
    ],
)
def test_code_lines_hand_each_comment_to_the_code_below_it(
    text: str,
    expected: list[tuple[tuple[str, ...], str, str]],
):
    assert [(c.lead, c.code, c.remark) for c in code_lines(text)] == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("# Top.\n\n[t]  # Why.\n# About a.\na = 1  # Trail.\n", "[t]\na = 1\n"),
        ('a = "x#y"  # Real.\n', 'a = "x#y"\n'),
        (
            "a = [\n    1,  # One.\n    # Lead.\n    2,\n]  # End.\n",
            "a = [\n    1,\n    2,\n]\n",
        ),
        ("[t]\n\n\na = 1\n\n# End.\n", "[t]\na = 1\n"),
        ('a = """\n# not\n\n"""\n', 'a = """\n# not\n\n"""\n'),
        ("a = 1\r\n# B.\r\nb = 2", "a = 1\r\nb = 2"),
    ],
)
def test_uncommented_drops_comments_and_blank_lines(text: str, expected: str):
    assert uncommented(text) == expected


@pytest.mark.parametrize("read", [layout, code_lines, uncommented])
@pytest.mark.parametrize("text", ["a = [1\n", 'a = "x\n', "[t\n", "= 1\n"])
def test_layout_rejects_malformed_toml(text: str, read: Callable[[str], object]):
    with pytest.raises(LayoutError):
        read(text)


if __name__ == "__main__":
    from copybarista.lib.testing.runner import test_main

    test_main(__file__)
