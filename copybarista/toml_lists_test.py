from __future__ import annotations

import tomllib

from treekle import PlainTree, from_plain

import pytest

from copybarista.toml_lists import ListError, ListValue


def _list(text: str) -> ListValue:
    return ListValue.parse(text)


@pytest.mark.parametrize(
    ("text", "values"),
    [
        ('a = ["x", "y"]\n', ['"x"', '"y"']),
        ("a = []\n", []),
        ("a = [ ]\n", []),
        ('a = [{x = [1, 2]}, "y,z"]\n', ["{x = [1, 2]}", '"y,z"']),
        ('a = [\n    # Lead.\n    "x",  # Why.\n    "y"\n]\n', ['"x"', '"y"']),
    ],
)
def test_values_reads_each_entry_as_written(text: str, values: list[str]):
    assert _list(text).values() == values


@pytest.mark.parametrize(
    ("base", "extra", "expected"),
    [
        ('a = ["x"]\n', '["y", "z"]', 'a = ["x", "y", "z"]\n'),
        ("a = []\n", '["y", "z"]', 'a = ["y", "z"]\n'),
        ("a = [ ]\n", '["x"]', 'a = ["x"]\n'),
        ('a = [ "w" ]\n', '["x"]', 'a = [ "w", "x" ]\n'),
        ('a = ["x",]\n', '["y"]', 'a = ["x", "y"]\n'),
        (
            'a = [\n    "x",\n]\n',
            '["y", "z"]',
            'a = [\n    "x",\n    "y",\n    "z",\n]\n',
        ),
        (
            'a = [\n    "x"\n]\n',
            '["y", "z"]',
            'a = [\n    "x",\n    "y",\n    "z",\n]\n',
        ),
        (
            'a = [\n    "x",\n    # NEVER ADD MORE!\n]\n',
            '[\n  # Extra.\n  "y",  # Why y.\n]',
            'a = [\n    "x",\n    # NEVER ADD MORE!\n    # Extra.\n    "y",  # Why y.\n]\n',
        ),
        ('a = ["x"]\n', '[\n    # Extra.\n    "y",\n]', 'a = ["x", "y"]\n'),
        (
            'a = [\n    "x",  # Why x.\n]\n',
            '["y"]',
            'a = [\n    "x",  # Why x.\n    "y",\n]\n',
        ),
        (
            'a = [\n    "x"  # Why x.\n]\n',
            '["y"]',
            'a = [\n    "x",  # Why x.\n    "y",\n]\n',
        ),
    ],
)
def test_appended_keeps_the_list_own_layout(base: str, extra: str, expected: str):
    assert _list(base).appended(ListValue.parse(f"v = {extra}\n")).text() == expected


@pytest.mark.parametrize(
    ("base", "removed", "expected"),
    [
        ('a = ["x", "y"]\n', ['"x"'], 'a = ["y"]\n'),
        (
            'a = [\n    # Lead.\n    "x",\n    # Other.\n    "y",\n]\n',
            ['"x"'],
            'a = [\n    # Other.\n    "y",\n]\n',
        ),
        (
            'a = [\n    # Group.\n    "x",\n    "y",\n]\n',
            ['"x"'],
            'a = [\n    # Group.\n    "y",\n]\n',
        ),
        (
            (
                'd = [\n    # torch\n    "torch",\n    "torchao",\n    # jax\n'
                '    "jax",\n    # NEVER ADD MORE!\n]\n'
            ),
            ['"jax"', '"torch"'],
            'd = [\n    # torch\n    "torchao",\n    # NEVER ADD MORE!\n]\n',
        ),
        (
            (
                'g = [\n    "p",\n    # Extra.\n    {include-group = "s"},\n'
                '    {include-group = "n"},\n]\n'
            ),
            ['{ include-group = "s" }', '{include-group="n"}'],
            'g = [\n    "p",\n]\n',
        ),
    ],
)
def test_without_drops_entries_and_the_comments_only_they_used(
    base: str,
    removed: list[str],
    expected: str,
):
    assert _list(base).without(removed).text() == expected


@pytest.mark.parametrize(
    ("base", "removed", "expected"),
    [
        ("a = [true, 1.0, 1]\n", ["1"], "a = [true, 1.0]\n"),
        ("a = [1, 1.0]\n", ["1.0"], "a = [1]\n"),
        ("a = [1, nan]\n", ["nan"], "a = [1]\n"),
    ],
)
def test_without_matches_an_entry_by_its_type_too(
    base: str,
    removed: list[str],
    expected: str,
):
    """TOML's ``true``, ``1`` and ``1.0`` differ, and NaN is an entry like any other."""
    assert _list(base).without(removed).text() == expected


def test_without_rejects_an_entry_the_list_lacks():
    with pytest.raises(ListError, match="'\"z\"'"):
        _list('a = ["x"]\n').without(['"z"'])


@pytest.mark.parametrize(
    "text",
    [
        'a = ["x",\n    "y"]\n',
        'a = [\n    "x", "y",\n]\n',
        "a = [\n    {x = [\n        1,\n    ]},\n]\n",
    ],
)
def test_editing_a_layout_without_one_entry_per_line_is_rejected(text: str):
    with pytest.raises(ListError):
        _list(text).appended(ListValue.parse('v = ["z"]\n'))


def test_parse_rejects_a_value_that_is_not_a_list():
    with pytest.raises(ListError):
        ListValue.parse('a = "x"\n')


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ('a = ["""x"""]\n', "an entry spans lines"),
        ("a = [1", "a list does not close"),
    ],
)
def test_parse_rejects_a_one_line_list_it_cannot_split(text: str, message: str):
    with pytest.raises(ListError, match=message):
        ListValue.parse(text)


@pytest.mark.parametrize(
    "text",
    [
        '"a=b"=["x"]\n',
        "'a=b'=['x']\n",
        '""=["x"]\n',
        '"a\\"=b"=["x"]\n',
        'X=["x"]\n',
        'ab=["x"]\n',
        'a_b-1.child=["x"]\n',
        'a\t=\t["x"]\n',
        'a=["", "x\\"y", \'literal\\path\']\n',
        'a=[{x={y=[1,2]}, z="tail"}, 3]\n',
        'a = [\n\t"x",\t# Why.\n\t"y"\n]',
        'a = [ # Opening comment.\n  "x",\n]\n',
        'a = [\n  # Lead.\n\n  "x",\n\n]\n',
    ],
)
def test_parse_text_preserves_quotes_nested_values_and_comments(text: str):
    rendered = ListValue.parse(text).text()

    assert rendered == text
    assert from_plain(tomllib.loads(rendered), dict[str, PlainTree]) == from_plain(
        tomllib.loads(text),
        dict[str, PlainTree],
    )


@pytest.mark.parametrize(
    "entry",
    [
        '{X={child=[1,2]}, other="tail"}',
        "{child=[{X=1}, {Y=2}], last=3}",
        "[[1,2], [3,4]]",
    ],
)
def test_nested_values_remain_whole_during_removal(entry: str):
    value = ListValue.parse(f'a=[{entry}, "keep"]\n')

    assert value.values() == [entry, '"keep"']
    assert value.without([entry]).text() == 'a=["keep"]\n'


@pytest.mark.parametrize(
    ("base", "extra", "expected"),
    [
        ("a = [\n]\n", '["x"]', 'a = [\n    "x",\n]\n'),
        ("a = [\n\n]\n", '["x"]', 'a = [\n\n    "x",\n]\n'),
        (
            "a = [\n  # Keep.\n]\n",
            '["x"]',
            'a = [\n  # Keep.\n  "x",\n]\n',
        ),
        (
            'a = [\n\t"x"\n]\n',
            '[\n "y"\n]',
            'a = [\n\t"x",\n\t"y",\n]\n',
        ),
        (
            'a = [\n  "x"  , # Keep.\n]\n',
            '["y"]',
            'a = [\n  "x", # Keep.\n  "y",\n]\n',
        ),
    ],
)
def test_appended_handles_empty_lists_and_nondefault_indentation(
    base: str,
    extra: str,
    expected: str,
):
    assert (
        ListValue.parse(base).appended(ListValue.parse(f"v={extra}")).text() == expected
    )


@pytest.mark.parametrize(
    ("base", "removed", "expected"),
    [
        ('a = [ "x" ]\n', ['"x"'], "a = []\n"),
        ('a = [\n  "x",\n]\n', ['"x"'], "a = [\n]\n"),
        (
            'a = [\n  "x",\n\n  # Only y.\n  "y",\n]\n',
            ['"y"'],
            'a = [\n  "x",\n\n]\n',
        ),
        ('a=["x", "x", "y"]\n', ['"x"'], 'a=["x", "y"]\n'),
    ],
)
def test_without_preserves_unrelated_entries_and_empty_layouts(
    base: str,
    removed: list[str],
    expected: str,
):
    assert ListValue.parse(base).without(removed).text() == expected


@pytest.mark.parametrize(
    "text",
    [
        'a = [\n """multi""",\n]\n',
        'a = [\n "x" , "y",\n]\n',
        "a = [\n {x = [\n  1,\n ]},\n]\n",
        "a=[[\n1,\n]]\n",
        "a=[{x=[\n1,\n]}]\n",
    ],
)
def test_parse_rejects_unsupported_multiline_entries(text: str):
    assert "a" in from_plain(tomllib.loads(text), dict[str, PlainTree])
    with pytest.raises(ListError):
        ListValue.parse(text)


def test_terminal_escaped_quote_keeps_the_string_token_whole():
    entry = r'"x\""'
    value = ListValue.parse(f'a=[{entry}, "keep"]\n')

    assert value.values() == [entry, '"keep"']
    assert value.without([entry]).text() == 'a=["keep"]\n'


def test_empty_one_line_list_without_terminal_newline_can_be_edited():
    value = ListValue.parse("a=[]")

    assert value.text() == "a=[]"
    assert value.values() == []
    assert value.appended(ListValue.parse('v=["x"]')).text() == 'a=["x"]'


if __name__ == "__main__":
    from copybarista.lib.testing.runner import test_main

    test_main(__file__)
