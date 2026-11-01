from __future__ import annotations

import pytest

from copybarista.delta_render import DeltaError, render


def _render(base: str, delta: str) -> str:
    return render(base, delta)


@pytest.mark.parametrize(
    ("base", "delta", "expected"),
    [
        # A replaced value keeps its place and the comment above it.
        (
            "[t]\n# About a.\na = 1\nb = 2\n",
            "[t]\na = 9\n",
            "[t]\n# About a.\na = 9\nb = 2\n",
        ),
        # A delta's comment replaces the base's.
        ("[t]\n# Old.\na = 1\n", "[t]\n# New.\na = 9\n", "[t]\n# New.\na = 9\n"),
        # ``# (clear)`` removes it.
        ("[t]\n# Old.\na = 1\n", "[t]\n# (clear)\na = 9\n", "[t]\na = 9\n"),
        # A trailing comment comes from the delta.
        ("[t]\na = 1  # Old.\n", "[t]\na = 9  # New.\n", "[t]\na = 9  # New.\n"),
        ("[t]\na = 1  # Old.\n", "[t]\na = 9\n", "[t]\na = 9\n"),
        # A deleted key takes its comment; a separated comment stays.
        (
            "[t]\na = 1\n# About b.\nb = 2\nc = 3\n",
            '[t]\n"!b" = true\n',
            "[t]\na = 1\nc = 3\n",
        ),
        (
            "[t]\na = 1\n# Section.\n\n# About b.\nb = 2\nc = 3\n",
            '[t]\n"!b" = true\n',
            "[t]\na = 1\n# Section.\n\nc = 3\n",
        ),
        # A new key goes last in its table, ahead of the table's tail.
        (
            "[t]\na = 1\n# Tail.\n\n[u]\n",
            "[t]\nb = 2\n",
            "[t]\na = 1\nb = 2\n# Tail.\n\n[u]\n",
        ),
        # ``"^a"`` places a new key after ``a``.
        ("[t]\na = 1\nc = 3\n", '[t]\n"^a".b = 2\n', "[t]\na = 1\nb = 2\nc = 3\n"),
        ("[t]\na = 1\nc = 3\n", '[t]\n"^".b = 2\n', "[t]\nb = 2\na = 1\nc = 3\n"),
        # A deleted table takes its lead and tail, never the next header's lead.
        (
            "[a]\nx = 1\n# Tail.\n\n# About b.\n[b]\ny = 1\n",
            '"!a" = true\n',
            "# About b.\n[b]\ny = 1\n",
        ),
        # A new table goes after the last table under its parent.
        (
            "[t]\na = 1\n\n[u]\nb = 1\n",
            "[t.s]\nc = 1\n",
            "[t]\na = 1\n\n[t.s]\nc = 1\n\n[u]\nb = 1\n",
        ),
        (
            "[a]\nx = 1\n",
            "# About b.\n[b]\ny = 1\n",
            "[a]\nx = 1\n\n# About b.\n[b]\ny = 1\n",
        ),
        # A new subtable follows its nearest table and that table's subtables.
        (
            "[p]\nx = 1\n\n[g]\n\n[p.u]\nh = 1\n",
            "[p.s]\nc = 1\n",
            "[p]\nx = 1\n\n[p.s]\nc = 1\n\n[g]\n\n[p.u]\nh = 1\n",
        ),
        (
            "[t.a]\nx = 1\n\n[u]\n",
            "[t.a.b.c]\nk = 1\n",
            "[t.a]\nx = 1\n\n[t.a.b.c]\nk = 1\n\n[u]\n",
        ),
        # A deleted key leaves one blank line between its neighbours, never two.
        (
            "t = [\n    1,\n]\n\nx = 1\n\n# Next.\n",
            '"!x" = true\n',
            "t = [\n    1,\n]\n\n# Next.\n",
        ),
        (
            "[t]\na = 1\n\n# About x.\nx = 1\n\nb = 1\n",
            '[t]\n"!x" = true\n',
            "[t]\na = 1\n\nb = 1\n",
        ),
        (
            "[t]\na = 1\n\n\nx = 1\n\nb = 1\n",
            '[t]\n"!x" = true\n',
            "[t]\na = 1\n\n\nb = 1\n",
        ),
        # A list operation carries the comment above it.
        (
            '[t]\na = ["x"]\n',
            '[t]\n# Why.\n"+a" = ["y"]\n',
            '[t]\n# Why.\na = ["x", "y"]\n',
        ),
        # ``[[key]]`` with no operation replaces the array of tables whole.
        (
            "[[i]]\nn = 1\n\n[[i]]\nn = 2\n\n[u]\n",
            "# Only.\n[[i]]\nn = 3\n\n[[i]]\nn = 4\n",
            "# Only.\n[[i]]\nn = 3\n\n[[i]]\nn = 4\n\n[u]\n",
        ),
        # A header comment is a header edit.
        ("[t]\na = 1\n", "[t]  # Why.\n", "[t]  # Why.\na = 1\n"),
        ("[t]  # Old.\na = 1\n", "[t]\n", "[t]  # Old.\na = 1\n"),
        ("[t]  # Old.\na = 1\n", "[t]  # (clear)\n", "[t]\na = 1\n"),
        ("[a]\n\n# Old.\n[t]\na = 1\n", "# New.\n[t]\n", "[a]\n\n# New.\n[t]\na = 1\n"),
        # Lists.
        ('a = [\n    "x",\n]\n', '"+a" = ["y"]\n', 'a = [\n    "x",\n    "y",\n]\n'),
        ('a = ["x", "y"]\n', '"-a" = ["x"]\n', 'a = ["y"]\n'),
        # Arrays of tables.
        ("[[i]]\nn = 1\n\n[[i]]\nn = 2\n", '[["-i"]]\nn = 2\n', "[[i]]\nn = 1\n"),
        (
            "[[i]]\nn = 1\n",
            '# About 2.\n[["+i"]]\nn = 2\n',
            "[[i]]\nn = 1\n\n# About 2.\n[[i]]\nn = 2\n",
        ),
        # Preamble and footer.
        (
            "# Top.\n\n[t]\na = 1\n",
            "# New top.\n\n[t]\na = 2\n",
            "# New top.\n\n[t]\na = 2\n",
        ),
        ("# Top.\n\n[t]\na = 1\n", "# (clear)\n\n", "[t]\na = 1\n"),
        # Before a closing ``# (clear)`` a lead is exact, blank lines included.
        ("[a]\nx = 1\n\n[t]\n", "[a]\n# (clear)\n[t]\n", "[a]\nx = 1\n[t]\n"),
        ("[a]\nx = 1\n[t]\n", "[a]\n\n\n\n# (clear)\n[t]\n", "[a]\nx = 1\n\n\n[t]\n"),
        ("[a]\nx = 1\n\n[t]\n", "[a]\n\n# (clear)\n[t]\n", "[a]\nx = 1\n[t]\n"),
        ("[t]\na = 1\n\nb = 1\n", "[t]\n# (clear)\nb = 1\n", "[t]\na = 1\nb = 1\n"),
        (
            "[t]\n# Old.\nb = 1\n",
            "[t]\n\n# New.\n# (clear)\nb = 1\n",
            "[t]\n\n# New.\nb = 1\n",
        ),
        ("[t]\na = 1\n# End.\n", "[t]\na = 1\n# (clear)\n", "[t]\na = 1\n"),
        # ``"!t"`` beside ``t`` replaces the table whole.
        (
            "# About a.\n[a]\nx = 1\n\n[b]\n",
            '"!a" = true\n\n[a]\nk = 9\n',
            "# About a.\n[a]\nk = 9\n\n[b]\n",
        ),
        # A key deleted and set again is restated whole, in place, as a table is;
        # one set again after an anchor moves there.
        (
            "# About a.\na = 1\nb = 2\n",
            '"!a" = true\na = 3\n',
            "# About a.\na = 3\nb = 2\n",
        ),
        ("[t]\na = 1\nb = 2\n", '[t]\n"!b" = true\n"^".b = 2\n', "[t]\nb = 2\na = 1\n"),
        # ``=`` escapes a key spelled like an operation.
        ("[t]\n", '[t]\n"=!x" = 1\n', '[t]\n"!x" = 1\n'),
        ("[t]\n", '[t]\n"==c" = 1\n', '[t]\n"=c" = 1\n'),
        # A last line with no line break is still a statement.
        ("a = []", '"+a" = ["x"]\n', 'a = ["x"]'),
        # A list operation carries the comment above it on removal too.
        (
            "[t]\n# Old.\na = [1, 2]\n",
            '[t]\n# New.\n"-a" = [2]\n',
            "[t]\n# New.\na = [1]\n",
        ),
        # ``+``/``-`` on an array of tables add and remove ``[[...]]`` elements.
        ("[[i]]\nn = 1\n", '"+i" = [{n = 2}]\n', "[[i]]\nn = 1\n\n[[i]]\nn = 2\n"),
        # A removal takes the value it names: ``true``, ``1`` and ``1.0`` are three.
        ("a = [true, 1.0, 1]\n", '"-a" = [1]\n', "a = [true, 1.0]\n"),
        (
            "[[i]]\nn = true\n\n[[i]]\nn = 1\n",
            '"-i" = [{n = 1}]\n',
            "[[i]]\nn = true\n",
        ),
        ("[[i]]\nn = 1.0\n\n[[i]]\nn = 1\n", '[["-i"]]\nn = 1\n', "[[i]]\nn = 1.0\n"),
        # A comment-only base is all preamble; the delta's comments replace it.
        ("# Only.\n", "# New.\n", "# New.\n"),
        ("# Only.\n", "# (clear)\n", ""),
        # A comment-only delta can carry a preamble and a footer at once.
        ("b = 2\n", "# Top.\n\n\n# end\n", "# Top.\n\nb = 2\n# end\n"),
        # A footer's separating blank line is not part of it.
        ("[t]\na = 1\n\n# End.\n", "[t]\na = 1\n\n# (clear)\n", "[t]\na = 1\n"),
        # A table only subtables imply takes a header of its own.
        (
            "[tool.a.b]\nc = 1\n",
            "[tool]\nx.y = 1\n",
            "[tool.a.b]\nc = 1\n\n[tool]\nx.y = 1\n",
        ),
        (
            "[tool.b]\nx = 1\n",
            "# About.\n[tool]\ny = 2\n",
            "[tool.b]\nx = 1\n\n# About.\n[tool]\ny = 2\n",
        ),
        # A new table whose lead is exact goes ahead of one the delta lists after it.
        (
            "[u]\nb = 1\n",
            "# (clear)\n[t]\na = 1\n\n\n# About u.\n# (clear)\n[u]\n",
            "[t]\na = 1\n\n# About u.\n[u]\nb = 1\n",
        ),
    ],
)
def test_render_applies_each_operation_in_place(base: str, delta: str, expected: str):
    assert _render(base, delta) == expected


@pytest.mark.parametrize(
    ("delta", "message"),
    [
        ('[t]\n"!b" = true\n', r"deletes 't\.b', which the base lacks"),
        ('[t]\n"-b" = [1]\n', r"removes from 't\.b', which the base lacks"),
        ('[t]\n"^z".b = 1\n', r"places 't\.b' after 't\.z', which the base lacks"),
        ('"+a" = 1\n', r"appends a non-list to 'a'"),
        ('"-a" = [9]\n', r"removes 9 from 'a', which lacks it"),
    ],
)
def test_render_names_what_the_base_lacks(delta: str, message: str):
    with pytest.raises(DeltaError, match=message):
        _render("a = [1]\n[t]\na = 1\n", delta)


@pytest.mark.parametrize(
    ("base", "delta", "expected"),
    [
        # The delta's own ``[pyproject]`` settings are not an operation.
        ("a = 1\n", 'a = 2\n\n[pyproject]\nbase = "x"\n', "a = 2\n"),
        # Deleting a dotted key's parent takes each key under it, arrays aside.
        (
            "a.b = 1\nd = 3\n\n[[i]]\nn = 1\n",
            '"!a" = true\n',
            "d = 3\n\n[[i]]\nn = 1\n",
        ),
        ("a.b = 1\na.c = 2\nd = 3\n", '"!a" = true\n', "d = 3\n"),
        # A table emptied of its last key goes with it.
        ("[[i]]\nn = 1\n\n[t]\na = 1\n", '[t]\n"!a" = true\n', "[[i]]\nn = 1\n"),
        # A new table joins the run of tables nearest it in path.
        (
            "[tool.a]\nx = 1\n\n[tool.a.b]\ny = 1\n\n[other]\nz = 1\n",
            "[tool.a.c]\nw = 1\n",
            "[tool.a]\nx = 1\n\n[tool.a.b]\ny = 1\n\n[tool.a.c]\nw = 1\n\n[other]\nz = 1\n",
        ),
        # An exact lead keeps a new table after one the delta names before it.
        (
            "[x.a]\nk = 1\n\n[b]\ny = 1\n",
            "# (clear)\n[b]\n\n# (clear)\n[x.n]\nk = 2\n",
            "[x.a]\nk = 1\n[b]\ny = 1\n[x.n]\nk = 2\n",
        ),
        # A comment-only delta with no blank line closing a preamble is all footer.
        ("a = 1\n", "# Foot.\n", "a = 1\n# Foot.\n"),
        (
            "a = 1\n",
            "# Top.\n\n# Mid.\n# Foot.\n",
            "a = 1\n# Top.\n\n# Mid.\n# Foot.\n",
        ),
        # A stated array of tables the base lacks is added.
        ("a = 1\n", "[[i]]\nn = 1\n", "a = 1\n\n[[i]]\nn = 1\n"),
        ("a = 1\n", "[t]  # (clear)\nk = 1\n", "a = 1\n\n[t]\nk = 1\n"),
        # ``[["-i"]]`` removes the element holding exactly those keys, subtables too.
        (
            "[[i]]\nn = 1\n[i.sub]\nx = 1\n\n[[i]]\nn = 2\n\n[j]\nm = 1\n",
            '[["-i"]]\nn = 1\n',
            "[[i]]\nn = 2\n\n[j]\nm = 1\n",
        ),
        # Appending to a key the base lacks sets it, under a table made for it.
        ("b = 1\n", '"+a" = ["x"]\n', 'b = 1\na = ["x"]\n'),
        ("a = 1\n", 't."+k" = ["x"]\n', 'a = 1\n\n[t]\nk = ["x"]\n'),
        # Setting a key over a table replaces the table.
        ("[a]\nk = 1\n\n[b]\n", "a = 1\n", "a = 1\n\n[b]\n"),
    ],
)
def test_render_places_what_the_base_lacks(base: str, delta: str, expected: str):
    assert _render(base, delta) == expected


@pytest.mark.parametrize(
    ("base", "delta", "expected"),
    [
        ("[t]\nk = 1\n", "\n\n# (clear)\n[t]\nk = 2\n", "\n\n[t]\nk = 2\n"),
        ("[t]\nk = 1\n", "\n\n[t]\nk = 2\n", "[t]\nk = 2\n"),
        ("a = 1\n\n[t]\n", "\n# (clear)\na = 2\n", "\na = 2\n\n[t]\n"),
        ("a = 1\nb = 2\n", '"!a" = true\n\n# (clear)\nb = 3\n', "\nb = 3\n"),
        ("a = 1\n\nb = 2\n", '"!a" = true\n', "b = 2\n"),
        ("a = 1\nb = 2\n", "\n# (clear)\nb = 3\n", "a = 1\n\nb = 3\n"),
        # An exact lead lower down leaves the new top's opening blank line to go.
        (
            "a = 1\n\nb = 2\nc = 3\n",
            '"!a" = true\n\n# (clear)\nc = 4\n',
            "b = 2\n\nc = 4\n",
        ),
        (
            "[t]\nk = 1\n\n[u]\nj = 1\n",
            "\n# (clear)\n[u]\nj = 2\n",
            "[t]\nk = 1\n\n[u]\nj = 2\n",
        ),
        ('"-a" = 1\n', '\n# (clear)\n"=-a" = 2\n', '\n"-a" = 2\n'),
        ("[t]\nk = 1\n", "\n# (clear)\n[t]\n", "\n[t]\nk = 1\n"),
    ],
)
def test_render_opens_with_blank_lines_only_where_the_delta_states_them(
    base: str,
    delta: str,
    expected: str,
):
    """A lead stated exactly is the delta's own; any other opening blank line goes."""
    assert _render(base, delta) == expected


@pytest.mark.parametrize(
    ("base", "delta", "message"),
    [
        ("[[i]]\nn = 1\n", '"+i" = ["x"]\n', "appends a non-table to 'i'"),
        (
            "[[i]]\nn = 1\n",
            '[["-i"]]\nn = 3\n',
            r"removes '\{n = 3\}' from i, which lacks it",
        ),
        ("a = [1]\n", '"-a" = 1\n', "removes a non-list from 'a'"),
        ("a = 1\n", '"-a" = [1]\n', "removes entries from 'a', which is not a list"),
        ("[a]\nk = 1\n", '"+a" = ["x"]\n', "appends to 'a', which is not a list"),
        ("a = 1\n", '"+a" = ["x"]\n', "appends to 'a', which is not a list"),
        ("a = [1\n", "a = 2\n", "Cannot parse the base"),
    ],
)
def test_render_refuses_an_operation_the_value_cannot_take(
    base: str,
    delta: str,
    message: str,
):
    with pytest.raises(DeltaError, match=message):
        _render(base, delta)


if __name__ == "__main__":
    from copybarista.lib.testing.runner import test_main

    test_main(__file__)
