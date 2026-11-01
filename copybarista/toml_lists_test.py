from __future__ import annotations

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


if __name__ == "__main__":
    from copybarista.lib.testing.runner import test_main

    test_main(__file__)
