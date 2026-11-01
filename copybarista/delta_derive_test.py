from __future__ import annotations

import pytest

from copybarista.delta_derive import _renders, derive, derive_values
from copybarista.delta_render import render


_BASE = """\
# Preamble.

# About a.
a = 1  # Trail a.

# About t.
[t]  # Why t.
# About x.
x = 1
w = [
    "p",
    # About q.
    "q",
    # NEVER ADD MORE!
]
# Tail of t.

# About u.
[u]
y = 2

# About i.
[[i]]
n = 1

[[i]]
n = 2

[tool.c]
z = 3

# Footer.
"""


def _lines(text: str) -> list[str]:
    return text.splitlines(keepends=True)


def _edits() -> list[tuple[str, str]]:
    """Return ``(name, target)`` for one edit of each kind at each line of _BASE."""
    lines = _lines(_BASE)
    found: list[tuple[str, str]] = []
    for i, line in enumerate(lines):
        head, tail = lines[:i], lines[i + 1 :]
        stripped = line.strip()
        if stripped.startswith("["):
            found.append(
                (f"{i}-header-lead", "".join([*head, "# New.\n", line, *tail])),
            )
            found.append(
                (
                    f"{i}-header-remark",
                    "".join([*head, f"{line.rstrip()}  # New.\n", *tail]),
                ),
            )
        elif "=" in stripped and not stripped.startswith("#"):
            found.append((f"{i}-key-lead", "".join([*head, "# New.\n", line, *tail])))
            if not stripped.endswith("["):
                found.append((f"{i}-key-add", "".join([*head, line, "k = 0\n", *tail])))
                found.append((f"{i}-key-delete", "".join([*head, *tail])))
                value = line.replace("1", "9").replace("2", "8").replace("3", "7")
                found.append((f"{i}-value", "".join([*head, value, *tail])))
        elif stripped.startswith('"'):
            found.append((f"{i}-entry-delete", "".join([*head, *tail])))
            found.append(
                (f"{i}-entry-lead", "".join([*head, "    # New.\n", line, *tail])),
            )
        elif stripped.startswith("#"):
            edited = line.replace("#", "# New", 1)
            found.append((f"{i}-comment-edit", "".join([*head, edited, *tail])))
            found.append((f"{i}-comment-delete", "".join([*head, *tail])))
        elif not stripped:
            found.append((f"{i}-blank-delete", "".join([*head, *tail])))
    return found


_EDITS = _edits()


@pytest.mark.parametrize(("name", "target"), _EDITS, ids=[name for name, _ in _EDITS])
def test_derive_then_render_reproduces_each_edit(name: str, target: str):
    del name
    delta = derive(_BASE, target)
    assert render(_BASE, delta) == target


@pytest.mark.parametrize(
    ("base", "target"),
    [
        pytest.param("a = 1\nb = 2\nc = 3\n", "c = 3\na = 1\nb = 2\n", id="top-level"),
        pytest.param(
            "[x]\n# About a.\na = 1\nb = 2\nc = 3\n",
            "[x]\nc = 3\n# About a.\na = 1\nb = 2\n",
            id="in-a-table",
        ),
        pytest.param(
            "[x]\na = 1\nb = 2\nc = 3\n",
            "[x]\na = 1\nc = 4\nb = 2\n",
            id="edited",
        ),
    ],
)
def test_derive_then_render_reproduces_a_reorder(base: str, target: str):
    assert render(base, derive(base, target)) == target


@pytest.mark.parametrize(
    ("base", "target"),
    [
        pytest.param('["-t"]\nk = 1\n', '["-t"]\nk = 2\n', id="edited-table"),
        pytest.param("a = 1\n", 'a = 1\n\n["!t"]\nk = 1\n', id="new-table"),
        pytest.param('["-t"]\nk = 1\n', '["-t"]  # Why.\nk = 1\n', id="header-remark"),
        pytest.param("a = 1\n", 'a = 1\n\n[["-i"]]\nn = 1\n', id="new-array"),
        pytest.param("b = 1\n", '"-a" = 2\nb = 1\n', id="key-placed-first"),
        pytest.param('"-a" = 1\n', '"-a" = 2\n', id="edited-key"),
    ],
)
def test_derive_escapes_a_name_spelled_like_an_operation(base: str, target: str):
    assert render(base, derive(base, target)) == target


@pytest.mark.parametrize(
    ("base", "target"),
    [
        pytest.param("a = 1\n", "a = 1\n\n# Footer.\n", id="footer-only"),
        pytest.param(
            "# Top.\n\na = 1\n",
            "# New top.\n\na = 1\n\n# Footer.\n",
            id="preamble-and-footer",
        ),
        pytest.param(
            "[[i]]\nn = 1\n",
            "[[i]]\nn = 1\n\n# About two.\n[[i]]\nn = 2\n",
            id="commented-element",
        ),
    ],
)
def test_derive_then_render_reproduces_a_comment_only_change(base: str, target: str):
    assert render(base, derive(base, target)) == target


def test_renders_is_false_for_a_delta_the_base_cannot_take():
    assert not _renders("a = 1\n", '"!b" = true\n', target="a = 1\n")
    assert _renders("a = 1\n", "a = 2\n", target="a = 2\n")


def test_derive_of_the_base_itself_is_empty():
    assert derive(_BASE, _BASE) == ""


@pytest.mark.parametrize(
    ("target", "delta"),
    [
        ("a = 1\nb = 2\nc = 3\n", '"^a".b = 2\n'),
        ("a = 1\nb = 2\nx = 0\nc = 3\n", '"^a".b = 2\n"^b".x = 0\n'),
        ("b = 2\na = 1\nc = 3\n", '"^".b = 2\n'),
        ("a = 1\nc = 3\nb = 2\n", "b = 2\n"),
        ("a = 1\nc = 9\n", "c = 9\n"),
        ("a = 1\n", '"!c" = true\n'),
    ],
)
def test_derive_writes_one_operation_per_change(target: str, delta: str):
    assert derive("a = 1\nc = 3\n", target) == delta


@pytest.mark.parametrize(
    ("target", "delta"),
    [
        ('w = ["x", "y", "z"]\n', '"+w" = ["z"]\n'),
        ('w = ["y"]\n', '"-w" = ["x"]\n'),
        ('w = ["y", "x"]\n', 'w = ["y", "x"]\n'),
        ('w = ["x", "x", "y"]\n', 'w = ["x", "x", "y"]\n'),
    ],
)
def test_derive_edits_a_list_by_entry_where_it_can(target: str, delta: str):
    assert derive('w = ["x", "y"]\n', target) == delta


@pytest.mark.parametrize(
    ("base", "target", "delta"),
    [
        # A deleted subtable is named under its parent's header, never redeclared.
        ("[t]\nk = 1\n\n[[t.i]]\nn = 1\n", "[t]\nk = 2\n", '[t]\n"!i" = true\nk = 2\n'),
        # An array edit is written as list operations where they reproduce it.
        ("[[i]]\nn = 1\n\n[[i]]\nn = 2\n", "[[i]]\nn = 1\n", '"-i" = [{n = 2}]\n'),
        # A stated new table gives only its comments when render's lead fits.
        ("a = 1\n", "a = 1\n\n# About t.\n[t]\nb = 2\n", "# About t.\n[t]\nb = 2\n"),
        # A restated array leaves its first element's lead to the base.
        (
            "# About i.\n[[i]]\nn = 1\n\n[[i]]\nn = 2\n",
            "# About i.\n[[i]]\n# Inner.\nn = 1\n\n[[i]]\nn = 2\n",
            "[[i]]\n# Inner.\nn = 1\n\n[[i]]\nn = 2\n",
        ),
        # An element with a comment of its own stays when a plain one is added.
        (
            "[[i]]\nn = 1\n\n# Two.\n[[i]]\nn = 2\n",
            "[[i]]\nn = 1\n\n# Two.\n[[i]]\nn = 2\n\n[[i]]\nn = 3\n",
            '"+i" = [{n = 3}]\n',
        ),
        # A new array has no base element whose lead it could take.
        ("a = 1\n", "a = 1\n\n[[j]]\nn = 1\n", "[[j]]\nn = 1\n"),
        # Only the restated array's own first element lends it a lead.
        (
            "[[h]]\nm = 1\n\n# About i.\n[[i]]\nn = 1\n\n[[i]]\nn = 2\n",
            "[[h]]\nm = 1\n\n# About i.\n[[i]]\nn = 2\n\n[[i]]\nn = 1\n",
            "[[i]]\nn = 2\n\n[[i]]\nn = 1\n",
        ),
        # A repeat under its own comment is a repeat, so the array is restated.
        (
            "[[i]]\nn = 1\n\n[[i]]\nn = 2\n",
            "[[i]]\nn = 1\n\n# Again.\n[[i]]\nn = 1\n",
            "[[i]]\nn = 1\n\n# Again.\n[[i]]\nn = 1\n",
        ),
        # A repeat under another lead is still a repeat, never a ``+`` of itself.
        (
            "[[i]]\nn = 1\n\n[[i]]\nn = 2\n",
            "[[i]]\nn = 1\n\n[[i]]\nn = 1\n",
            "[[i]]\nn = 1\n\n[[i]]\nn = 1\n",
        ),
        # A part ending in a comment keeps every character of it.
        ("a = 1\n", "a = 1\n\n[t]\nb = 2  # X\n", "[t]\nb = 2  # X\n"),
        # Trailing spaces in a part are the target's, kept as written.
        (
            "[t]\na = 1\n\n[u]\nb = 1\n",
            "[t]\na = 2  \n\n[u]\nb = 1\n",
            "[t]\na = 2  \n",
        ),
        # A new key follows a dotted, quoted key: the anchor is spelled whole.
        (
            '[t]\n"a.b".c = 1\nz = 2\n',
            '[t]\n"a.b".c = 1\nk = 1\nz = 2\n',
            '[t]\n"^\\"a.b\\".c".k = 1\n',
        ),
        # A subtable emptied under a headed parent is deleted under that header.
        (
            "[t]\na = 1\n\n[t.u]\nb = 1\n\n[v]\nc = 1\n",
            "[t]\na = 1\n\n[t.u]\n\n[v]\nc = 1\n",
            '[t]\n"!u" = true\n\n\n# (clear)\n[t.u]\n',
        ),
    ],
)
def test_derive_writes_the_shortest_delta_that_renders(
    base: str,
    target: str,
    delta: str,
):
    assert derive(base, target) == delta
    assert render(base, delta) == target


def test_derive_writes_only_the_header_a_change_lies_under():
    base = "[t]\na = 1\nb = 2\n\n[u]\nc = 3\n"
    assert derive(base, base.replace("b = 2", "b = 9")) == "[t]\nb = 9\n"


def test_derive_keeps_the_preamble_apart_from_the_first_operation():
    assert derive("# Top.\n\na = 1\n", "# New.\n\na = 2\n") == "# New.\n\na = 2\n"


@pytest.mark.parametrize(
    ("base", "target"),
    [
        ("# A.\na = 1\n", "# B.\na = 1\n"),
        ("a = 1  # A.\n", "a = 1\n"),
        ('w = [\n    # A.\n    "x",\n]\n', 'w = [\n    "x",\n]\n'),
        # A table the base already holds empty keeps its comments to the base.
        ("# About t.\n[t]  # Why.\n\n[t.u]\nb = 1\n", "[t]\n\n[t.u]\nb = 1\n"),
    ],
)
def test_derive_values_of_a_comment_edit_is_empty(base: str, target: str):
    assert derive_values(base, target) == ""


@pytest.mark.parametrize(
    ("base", "target"),
    [
        ('a = "x"  # Why.\nb = 1\n', "a = 'x'  # Why.\nb = 1\n"),
        ("a = 1  # A.\nb = 2  # B.\n", "b = 2  # B.\na = 1  # A.\n"),
        (
            "[t]\n# Why a.\na = 1\n# Why b.\nb = 2\nc = 3\n",
            "[t]\n# Why b.\nb = 2\n# Why a.\na = 1\nc = 3\n",
        ),
        # Beside them, a new key or table renders as ``derive`` writes one.
        ("a = 1  # A.\n", "a = 1  # A.\nb = 2\n"),
        ("a = 1  # A.\n", "a = 1  # A.\n\n[t]\nk = 1\n"),
    ],
    ids=[
        "requoted",
        "moved",
        "moved under its comment lines",
        "a key added",
        "a table added",
    ],
)
def test_derive_values_writes_a_value_it_respells_or_moves_with_its_comments(
    base: str,
    target: str,
):
    """Only a value left as it stood is no edit; one written keeps its comments."""
    assert render(base, derive_values(base, target)) == target


@pytest.mark.parametrize(
    ("base", "target"),
    [
        ("a = 1  # Why.\nb = 1\n", "a = true  # Why.\nb = 1\n"),
        ("a = 1  # Why.\nb = 1\n", "a = 1.0  # Why.\nb = 1\n"),
        ("w = [\n    # A.\n    1,\n]\n", "w = [\n    # A.\n    true,\n]\n"),
    ],
)
def test_derive_values_keeps_the_comments_beside_a_value_of_a_new_type(
    base: str,
    target: str,
):
    """``1`` to ``true`` or ``1.0`` is a value edit, though Python's ``==`` disagrees."""
    assert render(base, derive_values(base, target)) == target


@pytest.mark.parametrize(
    ("base", "target", "rendered"),
    [
        pytest.param(
            "w = [\n    # A.\n    1,\n    # B.\n    2,\n]\n",
            "w = [1]\n",
            "w = [\n    # A.\n    1,\n]\n",
            id="removed",
        ),
        pytest.param(
            "w = [\n    # A.\n    1,\n]\n",
            "w = [1, 2]\n",
            "w = [\n    # A.\n    1,\n    2,\n]\n",
            id="appended",
        ),
    ],
)
def test_derive_values_keeps_each_kept_entry_s_comments_as_a_list_changes_length(
    base: str,
    target: str,
    rendered: str,
):
    """A removed entry takes the comments only it used; a kept one keeps its own."""
    assert render(base, derive_values(base, target)) == rendered


@pytest.mark.parametrize(
    ("base", "target", "rendered"),
    [
        pytest.param(
            "a = 1\nb = 1\n",
            "a = 2\nb = 1\n",
            "a = 2\nb = 1\n",
            id="no-remark",
        ),
        # A remark beside a multi-line value has no one line to sit on in a new one.
        pytest.param(
            'a = """x\ny"""  # Why.\n',
            'a = "z"\n',
            'a = "z"\n',
            id="multi-line",
        ),
        # A one-line list keeps its remark through a by-entry edit.
        pytest.param(
            "w = [1, 2]  # Why.\n",
            "w = [1, 3]\n",
            "w = [1, 3]  # Why.\n",
            id="one-line-list",
        ),
        # Repeated or reordered entries cannot say whose comments are whose.
        pytest.param(
            'w = [\n    # A.\n    "x",\n    "x",\n]\n',
            'w = ["x"]\n',
            'w = ["x"]\n',
            id="repeated",
        ),
        pytest.param(
            'w = [\n    # A.\n    "x",\n    "y",\n]\n',
            'w = ["y", "z", "x"]\n',
            'w = ["y", "z", "x"]\n',
            id="reordered",
        ),
    ],
)
def test_derive_values_drops_comments_it_cannot_place(
    base: str,
    target: str,
    rendered: str,
):
    assert render(base, derive_values(base, target)) == rendered


if __name__ == "__main__":
    from copybarista.lib.testing.runner import test_main

    test_main(__file__)
