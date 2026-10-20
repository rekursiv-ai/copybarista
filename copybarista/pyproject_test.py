from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

import textwrap
import tomllib

from treekle import from_plain

import pytest

from copybarista import pyproject
from copybarista.config import load_config
from copybarista.delta_derive import derive
from copybarista.delta_render import DeltaError, render
from copybarista.pyproject import (
    DeltaConflictError,
    PyprojectSource,
    pyproject_inputs,
    rendered_pyproject,
    same_render,
)


if TYPE_CHECKING:
    from pathlib import Path


def test_render_appends_entries_with_their_comments():
    base = _doc(
        """
        [t]
        a = [
            "x",
            # NEVER ADD MORE!
        ]
        """,
    )
    delta = _doc(
        """
        [t]
        "+a" = [
            # Extra.
            "y",  # Why y.
        ]
        """,
    )
    text = str(render(base, delta))
    assert _plain(str(text)) == {"t": {"a": ["x", "y"]}}
    assert text.index("# NEVER ADD MORE!") < text.index("# Extra.")
    assert "# Why y." in text


@pytest.mark.parametrize(
    ("base", "expected"),
    [
        ('a = ["x"]\n', 'a = ["x", "y", "z"]\n'),
        ("a = []\n", 'a = ["y", "z"]\n'),
        ('a = [\n    "x",\n]\n', 'a = [\n    "x",\n    "y",\n    "z",\n]\n'),
        ('a = [\n    "x"\n]\n', 'a = [\n    "x",\n    "y",\n    "z",\n]\n'),
    ],
)
def test_render_appends_in_the_list_own_layout(base: str, expected: str):
    out = str(render(_doc(base), _doc('"+a" = ["y", "z"]\n')))
    assert out == expected


def test_render_deletes_a_table_the_delta_emptied():
    base = _doc("[a]\nx = 1\n\n[a.b]\ny = 2\n\n[c]\nz = 3\n")
    delta = _doc('[a]\n"!x" = true\n\n[a.b]\n"!y" = true\n')
    assert str(render(base, delta)) == "[c]\nz = 3\n"


@pytest.mark.parametrize(
    ("base", "delta", "expected"),
    [
        ("[a]\nx = 1\n\n[b]\ny = 1\n", '"!a" = true\n', "[b]\ny = 1\n"),
        (
            "# Top.\n\n[a]\nx = 1\n\n[b]\ny = 1\n",
            '"!a" = true\n',
            "# Top.\n\n[b]\ny = 1\n",
        ),
        ("# Top.\n\n[a]\nx = 1\n", "# (clear)\n\n[a]\nx = 2\n", "[a]\nx = 2\n"),
        ("[a]\nx = 1\n\n# End.\n", "[a]\nx = 1\n# (clear)\n", "[a]\nx = 1\n"),
    ],
)
def test_render_keeps_blank_lines_only_between_things(
    base: str,
    delta: str,
    expected: str,
):
    assert str(render(_doc(base), _doc(delta))) == expected


def test_render_keeps_a_list_the_delta_emptied():
    out = render(_doc('a = ["x"]\n'), _doc('"-a" = ["x"]\n'))
    assert _plain(out) == {"a": []}


def test_render_adds_a_key_above_the_table_trailing_comments():
    base = _doc("[t]\na = 1\n# About the next table.\n\n[u]\nb = 2\n")
    out = str(render(base, _doc("[t]\n# Lead of c.\nc = 3\n")))
    assert out == (
        "[t]\na = 1\n# Lead of c.\nc = 3\n# About the next table.\n\n[u]\nb = 2\n"
    )


@pytest.mark.parametrize(
    ("base", "target"),
    [
        ("[t]\na = 1\nc = 3\n", "[t]\na = 1\nb = 2\nc = 3\n"),
        ("[t]\na = 1\nc = 3\n", "[t]\nb = 2\na = 1\nc = 3\n"),
        ("[t]\na = 1\nc = 3\n", "[t]\na = 1\nb = 2\nx = 0\nc = 3\n"),
        (
            "[t]\na = 1  # Why a.\nc = 3\n",
            "[t]\na = 1  # Why a.\n# About b.\nb = 2\nc = 3\n",
        ),
        ("[t]\na = 1\nc = 3\n", "[t]\na = 1\nc = 3\nd = 4\n"),
    ],
)
def test_derive_adds_a_key_where_the_target_has_it(base: str, target: str):
    """A key added mid-table lands there, without restating any key it follows."""
    delta = str(derive(_doc(base), _doc(target)))
    assert "a = 1" not in delta
    assert str(render(_doc(base), _doc(delta))) == target


def test_render_replacing_a_key_keeps_the_comment_above_the_next_header():
    base = _doc("[t]\na = 1\n# About u.\n\n[u]\nb = 2\n")
    out = str(render(base, _doc("[t]\n# New lead.\na = 9\n")))
    assert out == "[t]\n# New lead.\na = 9\n# About u.\n\n[u]\nb = 2\n"


@pytest.mark.parametrize(
    "target",
    [
        "[t]\na = 1\nb = false  # Why b.\n",
        "[t]\na = 1\n\n# About c.\n[c]\nz = 3\n",
        "[t]\na = 1\n\n# About idx.\n[[t.idx]]\nn = 1\n",
    ],
)
def test_derive_carries_every_comment(target: str):
    base = _doc("[t]\na = 1\n")
    out = str(
        render(base, str(str(derive(base, _doc(target))))),
    )
    assert _plain(str(out)) == _plain(_doc(target))
    assert out == target


def test_render_deleting_a_key_keeps_comments_a_blank_line_separates():
    base = _doc("[t]\na = 1\n# Keep me.\n\n# About x.\nx = [1]\n\n[u]\nb = 2\n")
    text = str(render(base, _doc('[t]\n"!x" = true\n')))
    assert "# Keep me." in text
    assert "About x" not in text


def test_render_adds_a_key_above_trailing_comments_after_a_deleted_key():
    base = _doc("[t]\na = 1\nx = 2\n# About u.\n\n[u]\nb = 2\n")
    out = str(render(base, _doc('[t]\n"!x" = true\nc = 3\n')))
    assert out == "[t]\na = 1\nc = 3\n# About u.\n\n[u]\nb = 2\n"


@pytest.mark.parametrize(
    "target",
    [
        'a = [\n    "x",\n    "y",\n]\n',
        'a = [\n    "x",\n    # About y.\n    "y",\n]\n',
        'a = [\n    "x",\n    # Other.\n    "y",\n]\n',
        'a = [\n    "x",\n]\n',
        "a = 1  # Why.\n",
    ],
)
def test_derive_carries_comments_inside_values(target: str):
    base = _doc('a = [\n    "x",\n    # About y.\n    "y",\n    "z",\n]\n')
    _assert_round_trip(base, target)


@pytest.mark.parametrize(
    "target",
    [
        '[project]\nname = "p"\nv = 1\n\n[g]\nk = 1\n\n[project.urls]\nH = "h"\n',
        '[project]\nname = "p"\n# Why v.\nv = 2\n\n[g]\nk = 1\n\n[project.urls]\nH = "h"\n',
    ],
)
def test_derive_carries_comments_in_an_out_of_order_table(target: str):
    base = _doc(
        '[project]\nname = "p"\n# About v.\nv = 1\n\n[g]\nk = 1\n\n[project.urls]\nH = "h"\n',
    )
    _assert_round_trip(base, target)


@pytest.mark.parametrize(
    "target",
    [
        "[t]\na = 1\n\n# About u.\n[u]\nb = 2\n",
        "[t]\na = 1\n\n# Other.\n[u]\nb = 2\n",
    ],
)
def test_derive_carries_a_header_comment_of_a_shared_table(target: str):
    _assert_round_trip(_doc("[t]\na = 1\n\n[u]\nb = 2\n"), target)


@pytest.mark.parametrize(
    "target",
    [
        "# Top.\n\n[t]\na = 2\n",
        "# Changed.\n\n[t]\na = 2\n# End.\n",
        "[t]\na = 2\n",
    ],
)
def test_derive_carries_the_header_and_footer_comments(target: str):
    _assert_round_trip(_doc("# Top.\n\n[t]\na = 1\n\n# End.\n"), target)


def test_derive_carries_a_lead_into_a_super_table():
    base = _doc("[tool.b]\nx = 1\n")
    target = "[tool.a]\ny = 2\n\n# About b.\n[tool.b]\nx = 1\n"
    _assert_round_trip(base, target)
    out = str(
        render(base, str(str(derive(base, _doc(target))))),
    )
    assert "[tool]" not in out


@pytest.mark.parametrize(
    "target",
    [
        '[t]\na = [1]\n"-a" = [1]\n',
        '[t]\na = 1\n"!a" = true\n',
        '[t]\n"+b" = ["x"]\n',
        '[t]\n"=c" = 1\n',
    ],
)
def test_derive_escapes_a_key_spelled_like_an_operator(target: str):
    _assert_round_trip(_doc("[t]\na = [1]\n"), target)


@pytest.mark.parametrize(
    "target",
    [
        'a = ["x", "y", "x"]\n',
        'a = ["x", "x", "y"]\n',
        'a = ["y"]\n',
    ],
)
def test_derive_counts_repeated_entries(target: str):
    _assert_round_trip(_doc('a = ["x", "y"]\n'), target)


@pytest.mark.parametrize(
    ("target", "delta"),
    [
        (
            "[[i]]\nn = 1\n\n[[i]]\nn = 3\n",
            {"-i": [{"n": 2}], "+i": [{"n": 3}]},
        ),
        ("[[i]]\nn = 2\n\n[[i]]\nn = 1\n", {"i": [{"n": 2}, {"n": 1}]}),
        ("[[i]]\nn = 1\n\n[[i]]\nn = 1\n", {"i": [{"n": 1}, {"n": 1}]}),
        ("[[i]]\nn = 1\n", {"-i": [{"n": 2}]}),
    ],
)
def test_derive_edits_an_array_of_tables(target: str, delta: dict[str, object]):
    base = _doc("[[i]]\nn = 1\n\n[[i]]\nn = 2\n")
    derived = derive(base, _doc(target))
    assert _plain(derived) == delta
    _assert_round_trip(base, target)


@pytest.mark.parametrize(
    "target",
    [
        "# About u.\n[u]\nb = 2\n\n[[i]]\nn = 3\n",
        "[t]\na = 1\n\n# About u.\n[t.u]\nb = 2\n",
        "[a]\nx = 1\n\n# About t.\n[tool.t]\ny = 1\n\n[[i]]\nn = 2\n\n[[i]]\nn = 3\n",
    ],
)
def test_derive_and_render_leave_their_inputs_untouched(target: str):
    base = _doc("[[i]]\nn = 1\n")
    target_doc = _doc(target)
    before = (str(base), str(target_doc))
    delta = derive(base, target_doc)
    delta_text = str(delta)
    render(base, delta)
    assert (str(base), str(target_doc)) == before
    assert str(delta) == delta_text


@pytest.mark.parametrize(
    "target",
    [
        "[t]\na = 1\n\n# About u.\n[u]\nb = 2\n",
        "[t]\na = 1\n# About u.\n[u]\nb = 2\n",
    ],
)
def test_derive_adds_a_table_with_its_own_comment_once(target: str):
    out = str(
        render(
            _doc("[t]\na = 1\n"),
            str(str(derive(_doc("[t]\na = 1\n"), _doc(target)))),
        ),
    )
    assert out.count("# About u.") == 1
    assert out.index("# About u.") < out.index("[u]")
    assert "\n\n\n" not in out


@pytest.mark.parametrize(
    "target",
    [
        "[a]\nx = 1\n\n# New.\n[tool.b]\ny = 1\n",
        "[a]\nx = 1\n\n# New.\n[tool.b]\ny = 1\n\n# About c.\n[tool.c]\nz = 1\n",
        "[a]\nx = 1\n\n# New.\n[tool.b]\ny = 1\n\n[tool.c]\nz = 1\n",
    ],
)
def test_derive_replaces_a_header_comment_under_a_super_table(target: str):
    base = _doc("[a]\nx = 1\n\n# About b.\n[tool.b]\ny = 1\n")
    _assert_round_trip(base, target)
    out = str(
        render(base, str(str(derive(base, _doc(target))))),
    )
    assert "\n\n\n" not in out


@pytest.mark.parametrize(
    ("base", "target"),
    [
        # A new table copied whole must not take the next header's comment along.
        ("[u]\nb = 1\n", "[t]\na = 1\n\n# About u.\n[u]\nb = 1\n"),
        (
            "[[i]]\nn = 1\n\n[u]\nb = 1\n",
            "[[i]]\nn = 1\n\n[[i]]\nn = 2\n\n# About u.\n[u]\nb = 1\n",
        ),
        (
            "[[i]]\nn = 1\n\n[[i]]\nn = 2\n\n[u]\nb = 1\n",
            "[[i]]\nn = 2\n\n[[i]]\nn = 1\n\n# About u.\n[u]\nb = 1\n",
        ),
        # A new first header under a super table the delta already writes into.
        ("[tool.c]\nz = 1\n", "# About b.\n[tool.b]\ny = 1\n"),
        ("[tool.c]\nz = 1\n", "\n# About b.\n[tool.b]\ny = 1\n"),
    ],
)
def test_derive_gives_each_header_comment_exactly_one_owner(base: str, target: str):
    _assert_round_trip(_doc(base), target)
    delta = str(str(derive(_doc(base), _doc(target))))
    out = str(render(_doc(base), delta))
    for header in ("[u]", "[tool.b]"):
        if header in target:
            comment = target[: target.index(header)].rstrip("\n").rsplit("\n", 1)[-1]
            assert out[: out.index(header)].rstrip("\n").endswith(comment)


def test_derive_adds_a_header_under_a_super_table_exactly():
    base = _doc("[tool.a]\nx = 1\n")
    target = "[tool.a]\nx = 1\n\n# About b.\n[tool.b]\ny = 1\n"
    delta = str(str(derive(base, _doc(target))))
    assert str(render(base, delta)) == target


def test_derive_clears_a_header_comment_under_a_super_table():
    base = _doc("[a]\nx = 1\n\n# About b.\n[tool.b]\ny = 1\n")
    _assert_round_trip(base, "[a]\nx = 1\n\n[tool.b]\ny = 2\n")


def test_rendered_pyproject_and_inputs_follow_the_config(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text("a = 1\n")
    (tmp_path / "shared.delta.toml").write_text("")
    delta = _delta(tmp_path, body="a = 2\n", extends=("shared.delta.toml",))
    config = tmp_path / "copy.barista.toml"
    head = (
        '[workflow]\nname = "d"\nmode = "squash"\nsource_root = "pkg"\n'
        '\n[files]\ninclude = ["**"]\n'
    )
    config.write_text(head)
    assert rendered_pyproject(config, source_ref=tmp_path) == ""
    assert pyproject_inputs(config, source_ref=tmp_path) == ()
    relative = delta.relative_to(tmp_path).as_posix()
    config.write_text(f'{head}\n[files.pyproject]\ndelta = "{relative}"\n')
    assert load_config(config).files.pyproject == relative
    assert rendered_pyproject(config, source_ref=tmp_path) == "a = 2\n"
    assert pyproject_inputs(config, source_ref=tmp_path) == (
        relative,
        "pyproject.toml",
        "shared.delta.toml",
    )


def test_render_rejects_removing_from_an_absent_list():
    with pytest.raises(DeltaError, match="'b'"):
        render(_doc("a = 1\n"), _doc('"-b" = ["x"]\n'))


@pytest.mark.parametrize(
    ("base", "delta", "expected"),
    [
        ("# Only.\n", "# New.\n", "# New.\n"),
        ("# Only.\n", "# (clear)\n", ""),
        ("# Only.\n", "#\n", "#\n"),
        ("[t]\na = 1\n", '[t]\n"+b" = [1]\n', "[t]\na = 1\nb = [1]\n"),
    ],
)
def test_render_handles_documents_without_keys(base: str, delta: str, expected: str):
    assert str(render(_doc(base), _doc(delta))) == expected


@pytest.mark.parametrize(
    ("base", "expected"),
    [
        ("a = [ ]\n", 'a = ["x"]\n'),
        ('a = [ "w" ]\n', 'a = [ "w", "x" ]\n'),
    ],
)
def test_render_appends_to_a_spaced_one_line_list(base: str, expected: str):
    assert str(render(_doc(base), _doc('"+a" = ["x"]\n'))) == expected


def test_render_edits_both_parts_of_a_top_level_out_of_order_table():
    base = _doc("[p]\nn = 1\n\n[g]\nk = 1\n\n# About u.\n[p.u]\nh = 1\n")
    delta = _doc('#\n\n[p]\n# About n.\nn = 2\n\n[p.u]\n"!h" = true\ni = 3\n')
    out = render(base, delta)
    assert _plain(out) == {"p": {"n": 2, "u": {"i": 3}}, "g": {"k": 1}}
    assert "# About n.\nn = 2" in str(out)


@pytest.mark.parametrize(
    ("base", "target"),
    [
        ("a = 1\n", "# C.\na.b = 1\n"),
        ("a.b = 1\n", "# C.\na.b = 2\n"),
        ("[t]\na.b = 1\n", "[t]\n# C.\na.b = 1\nz = 2\n"),
        ("[tool.a.b]\nc = 1\n", "[tool]\nx.y = 1\n\n[tool.a]\nb = 1\n"),
    ],
)
def test_derive_carries_comments_above_dotted_keys(base: str, target: str):
    _assert_round_trip(_doc(base), target)


@pytest.mark.parametrize("target", ["[a]\nb = 2\n", "a.b = 2\n"])
def test_derive_writes_no_clear_where_neither_side_has_a_comment(target: str):
    assert "#" not in str(derive(_doc("a = 1\n"), _doc(target)))
    _assert_round_trip(_doc("z = 0\n# About a.\na = 1\n"), f"z = 0\n{target}")


@pytest.mark.parametrize(
    ("base", "delta"),
    [
        ("[a]\nx = 1\n\n# About b.\n[b]\ny = 1\n", '"!a" = true\n'),
        (
            "[[i]]\nn = 1\n\n[[i]]\nn = 2\n\n# About b.\n[b]\ny = 1\n",
            '"-i" = [{n = 2}]\n',
        ),
        (
            "[t]\na = 1\n\n[t.u]\nc = 1\n\n# About b.\n[b]\ny = 1\n",
            '[t]\n"!u" = true\n',
        ),
    ],
)
def test_render_removing_a_table_keeps_the_next_header_comment(base: str, delta: str):
    assert "# About b.\n[b]" in str(render(_doc(base), _doc(delta)))


def test_derive_carries_a_preamble_edit_beside_a_top_level_deletion():
    base = _doc("# Top.\n\n[a]\nx = 1\n\n[b]\ny = 1\n")
    _assert_round_trip(base, "# New top.\n\n[b]\ny = 1\n")


def test_derive_carries_an_added_empty_table():
    _assert_round_trip(_doc("[a]\nx = 1\n"), "[a]\nx = 1\n\n[e]\n")


@pytest.mark.parametrize(
    "target",
    ["[t]\n#\na = 2\n", "[t]\n#\na = 1\n", "#\n\n[t]\na = 1\n"],
)
def test_derive_keeps_a_bare_hash_comment_line(target: str):
    _assert_round_trip(_doc("[t]\na = 1\n"), target)


_OWNED_BASE = """\
# Preamble.

# About a.
a = 1  # Trail a.

# About t.
[t]
# About x.
x = 1
w = 0
# Tail of t.

# About u.
[u]
y = 2

# About i.
[[i]]
n = 1

# About i2.
[[i]]
n = 2

# About tool.c.
[tool.c]
z = 3

# Footer.
"""

# Each delta, and the comments of ``_OWNED_BASE`` it owns: a key owns its lead and
# trailing remark, a table also everything inside it. Every other comment must
# survive the edit, wherever it sits.
_OWNED_EDITS: dict[str, tuple[str, frozenset[str]]] = {
    "delete-a": ('"!a" = true\n', frozenset({"# About a.", "# Trail a."})),
    "delete-t": (
        '"!t" = true\n',
        frozenset({"# About t.", "# About x.", "# Tail of t."}),
    ),
    "delete-t.x": ('[t]\n"!x" = true\n', frozenset({"# About x."})),
    "delete-t.w": ('[t]\n"!w" = true\n', frozenset()),
    "delete-u": ('"!u" = true\n', frozenset({"# About u."})),
    "delete-i": ('"!i" = true\n', frozenset({"# About i.", "# About i2."})),
    "remove-i1": ('"-i" = [{n = 1}]\n', frozenset({"# About i."})),
    "remove-i2": ('"-i" = [{n = 2}]\n', frozenset({"# About i2."})),
    "delete-tool": ('"!tool" = true\n', frozenset({"# About tool.c."})),
    "replace-a": ("a = 5\n", frozenset({"# Trail a."})),
    "replace-t.x": ("[t]\nx = 5\n", frozenset()),
    "add-t.v": ("[t]\nv = 1\n", frozenset()),
    "add-v": ("[v]\nk = 1\n", frozenset()),
    "add-tool.d": ("[tool.d]\nk = 1\n", frozenset()),
}


@pytest.mark.parametrize("name", list(_OWNED_EDITS))
def test_render_removes_only_the_comments_an_edit_owns(name: str):
    delta, owned = _OWNED_EDITS[name]
    out = str(render(_doc(_OWNED_BASE), _doc(delta)))
    expected = [line for line in _comment_texts(_OWNED_BASE) if line not in owned]
    assert sorted(_comment_texts(out)) == sorted(expected)
    assert out.rstrip("\n").endswith("# Footer.")
    assert out.startswith("# Preamble.\n")


@pytest.mark.parametrize("name", list(_OWNED_EDITS))
def test_derive_reproduces_every_owned_edit(name: str):
    target = str(render(_doc(_OWNED_BASE), _doc(_OWNED_EDITS[name][0])))
    _assert_round_trip(_doc(_OWNED_BASE), target)


@pytest.mark.parametrize(
    "delta",
    [
        "[t]\nb = 2\n",
        "[t.u]\nc = 3\n",
        '[t]\n"+l" = [1]\n',
        "[t]\na = 9\n",
    ],
)
def test_render_never_indents_a_key_under_a_commented_header(delta: str):
    base = _doc("# Top note.\n\n# About t, with spaces.\n[t]\na = 1\n")
    out = str(render(base, _doc(delta)))
    assert not [line for line in out.splitlines() if line.startswith(" ")]


@pytest.mark.parametrize(
    "delta",
    ["[p.s]\nc = 1\n", "[p]\nm = 2\n", "[p]\nn = 2\n\n[p.s]\nc = 1\n"],
)
def test_render_keeps_the_preamble_out_of_a_split_first_table(delta: str):
    base = _doc(
        "# Never edit this.\n# Sync it with uv.\n\n[p]\nn = 1\n\n[g]\nk = 1\n\n[p.u]\nh = 1\n",
    )
    out = str(render(base, _doc(delta)))
    assert out.startswith("# Never edit this.\n# Sync it with uv.\n\n[p]\n")
    assert not [line for line in out.splitlines() if line.startswith(" ")]


@pytest.mark.parametrize(
    "delta",
    ['"!b" = true\n', "b = {x = 2}\n", "[b]\nx = 2\n", '[b]\n"!x" = true\nz = 1\n'],
)
def test_render_keeps_a_section_comment_the_blank_lines_set_apart(delta: str):
    base = _doc("[a]\nk = 1\n\n[b]\nx = 1\n\n# About the rest.\n\n[t.h]\ny = 1\n")
    out = str(render(base, _doc(delta)))
    assert "\n# About the rest.\n\n[t.h]" in out


def test_derive_keeps_the_preamble_above_a_split_table_once():
    base = _doc(
        "# Top.\n\n[project]\nx = 1\n\n[other]\nz = 1\n\n[project.urls]\nu = 1\n",
    )
    target = "# Top.\n# More.\n\n[project]\nx = 1\n\n[other]\nz = 1\n\n[project.urls]\nu = 1\n"
    delta = str(derive(base, _doc(target)))
    assert delta == "# Top.\n# More.\n\n"
    assert str(render(base, _doc(delta))) == target


def test_derive_adds_a_table_without_restating_the_commented_rest():
    base = _doc("[[i]]\n# About.\nx = 1\n")
    target = "[[i]]\n# About.\nx = 1\n\n[[i]]\nx = 2\n"
    delta = str(derive(base, _doc(target)))
    assert _plain(str(delta)) == {"+i": [{"x": 2}]}
    assert str(render(base, _doc(delta))) == target


def test_derive_replaces_reordered_tables_without_restating_their_lead():
    base = _doc("# About i.\n[[i]]\nx = 1\n\n[[i]]\nx = 2\n")
    target = "# About i.\n[[i]]\nx = 2\n\n[[i]]\nx = 1\n"
    delta = str(derive(base, _doc(target)))
    assert "# About i." not in delta
    assert str(render(base, _doc(delta))) == target


def test_derive_clears_a_footer_below_tables_it_extends():
    _assert_round_trip(_doc("[[i]]\nx = 1\n\n# F.\n"), "[[i]]\nx = 1\n\n[[i]]\nx = 2\n")


@pytest.mark.parametrize(
    ("base", "target"),
    [
        ("[t.w]\nx = 1\n", "[t.w]\nx = 1\n\n[t.w.h.c]\np = 1\n"),
        (
            "[a]\nk = 1\n\n# Shared.\n\n[t.w]\nx = 1\n",
            "[a]\nk = 2\n\n# Shared.\n\n# Own.\n[t.w]\nx = 1\n",
        ),
    ],
)
def test_derive_then_render_keeps_the_target_byte_for_byte(base: str, target: str):
    delta = str(derive(_doc(base), _doc(target)))
    assert str(render(_doc(base), _doc(delta))) == target


@pytest.mark.parametrize(
    ("base", "target"),
    [
        ("a = 1\n", "a = 1  # Why.\n"),
        ("a = 1  # Old.\n", "a = 1  # New.\n"),
        ("a = 1  # Old.\n", "a = 1\n"),
        ('[t]\nn = "x"\n', '[t]\nn = "x"  # Why.\n'),
        ("a = [1]\n", "a = [1]  # Why.\n"),
        ("a = {x = 1}\n", "a = {x = 1}  # Why.\n"),
        ("[a]\nx = 1\n", "[a]  # Why.\nx = 1\n"),
        ("[a]  # Old.\nx = 1\n", "[a]  # New.\nx = 1\n"),
        ("[a]  # Old.\nx = 1\n", "[a]\nx = 1\n"),
    ],
)
def test_derive_carries_a_trailing_comment_edit(base: str, target: str):
    delta = str(derive(_doc(base), _doc(target)))
    assert str(render(_doc(base), _doc(delta))) == target


def test_derive_keeps_a_table_the_target_empties_in_place():
    base = "[a]\nx = 1\ny = 2\n\n[b]\nz = 3\n"
    target = "[a]\n\n[b]\nz = 3\n"
    delta = str(derive(_doc(base), _doc(target)))
    assert str(render(_doc(base), _doc(delta))) == target


def test_render_replaces_a_table_whole_when_the_delta_also_deletes_it():
    base = _doc("# About a.\n[a]\nx = 1\n\n[b]\nz = 3\n")
    delta = _doc('"!a" = true\n\n[a]\nk = 9\n')
    out = str(render(base, delta))
    assert out == "# About a.\n[a]\nk = 9\n\n[b]\nz = 3\n"


@pytest.mark.parametrize(
    ("delta", "message"),
    [
        ('[t]\n"!b" = true\n', r"deletes 't\.b',"),
        ('[t]\n"-b" = [1]\n', r"removes from 't\.b',"),
    ],
)
def test_render_names_the_missing_key_by_its_path(delta: str, message: str):
    with pytest.raises(DeltaError, match=message):
        render(_doc("[t]\na = 1\n"), _doc(delta))


def test_render_is_stable_across_layers():
    base = _doc("# Top note.\n# More.\n\n[t]\na = 1\n\n# About u.\n[u]\nb = 2\n")
    once = render(base, _doc("[t]\na = 2\n"))
    twice = render(once, _doc("[u]\nb = 3\n"))
    assert str(twice) == (
        "# Top note.\n# More.\n\n[t]\na = 2\n\n# About u.\n[u]\nb = 3\n"
    )


def test_render_appends_to_a_list_with_no_closing_line():
    out = render("a = []", _doc('"+a" = ["x", "y"]\n'))
    assert str(out) == 'a = ["x", "y"]'


def test_fold_rejects_a_public_settings_table(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text("a = 1\n")
    source = PyprojectSource.load(_delta(tmp_path, body=""), source_ref=tmp_path)
    with pytest.raises(DeltaError, match=r"\[pyproject\]"):
        source.fold('a = 1\n\n[pyproject]\nbase = "other.toml"\n')


def test_source_load_reports_malformed_toml_as_a_delta_error(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text("a = 1\n")
    delta = _delta(tmp_path, body="b = [\n")
    with pytest.raises(DeltaError, match=r"pyproject\.delta\.toml"):
        PyprojectSource.load(delta, source_ref=tmp_path)


def test_source_load_reports_a_non_utf8_file_as_a_delta_error(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_bytes(b'a = "\xff"\n')
    with pytest.raises(DeltaError, match=r"pyproject\.toml"):
        PyprojectSource.load(_delta(tmp_path, body=""), source_ref=tmp_path)


def test_fold_reports_malformed_public_toml_as_a_delta_error(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text("a = 1\n")
    source = PyprojectSource.load(_delta(tmp_path, body=""), source_ref=tmp_path)
    with pytest.raises(DeltaError, match="public"):
        source.fold("a = [\n")


def test_render_appends_to_a_missing_list():
    out = render(_doc("[t]\n"), _doc('[t]\n"+a" = ["x"]\n'))
    assert _plain(out) == {"t": {"a": ["x"]}}


def test_render_removes_entries_and_the_comment_only_they_used():
    base = _doc(
        """
        deps = [
            # torch
            "torch",
            "torchao",
            # jax
            "jax",
            # NEVER ADD MORE!
        ]
        """,
    )
    text = str(render(base, _doc('"-deps" = ["jax", "torch"]\n')))
    assert _plain(str(text)) == {"deps": ["torchao"]}
    assert "# torch" in text
    assert "# jax" not in text
    assert "# NEVER ADD MORE!" in text


@pytest.mark.parametrize(
    ("base", "expected"),
    [
        (
            'a = [\n    # Lead.\n    "x",\n    # Other.\n    "y",\n]\n',
            'a = [\n    # Other.\n    "y",\n]\n',
        ),
        (
            'a = [\n    # Group.\n    "x",\n    "y",\n]\n',
            'a = [\n    # Group.\n    "y",\n]\n',
        ),
        ('a = [\n    "x",\n    "y",\n]\n', 'a = [\n    "y",\n]\n'),
        ('a = ["x", "y"]\n', 'a = ["y"]\n'),
    ],
)
def test_render_removes_the_first_entry_and_its_lead(base: str, expected: str):
    assert str(render(_doc(base), _doc('"-a" = ["x"]\n'))) == expected


def test_render_star_removes_keys_tables_and_their_leading_comments():
    base = _doc(
        """
        [tool]
        keep = 1
        # About drop.
        drop = 2

        [tool.houselint]
        select = "ALL"
        """,
    )
    delta = _doc('[tool]\n"!drop" = true\n"!houselint" = true\n')
    text = str(render(base, delta))
    assert _plain(str(text)) == {"tool": {"keep": 1}}
    assert "About drop" not in text


def test_render_replaces_values_and_merges_tables():
    base = _doc(
        """
        [tool.ruff]
        line-length = 88
        exclude = ["a", "b"]

        [tool.ruff.lint]
        select = ["ALL"]
        """,
    )
    delta = _doc(
        """
        [tool.ruff]
        exclude = ["c"]

        [tool.ruff.lint]
        fixable = ["F"]

        [tool.ty]
        all = "warn"
        """,
    )
    assert _plain(render(base, delta)) == {
        "tool": {
            "ruff": {
                "line-length": 88,
                "exclude": ["c"],
                "lint": {"select": ["ALL"], "fixable": ["F"]},
            },
            "ty": {"all": "warn"},
        },
    }


def test_render_replaces_a_value_together_with_its_leading_comment():
    base = _doc(
        """
        [t]
        # Private reason naming the monorepo.
        regex = "a"
        # Kept.
        other = 1
        """,
    )
    delta = _doc('[t]\n# Public reason.\nregex = "b"\n')
    text = str(render(base, delta))
    assert _plain(str(text)) == {"t": {"regex": "b", "other": 1}}
    assert "Private reason" not in text
    assert text.index("# Public reason.") < text.index('regex = "b"')
    assert "# Kept." in text


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("# Old.\n", "# New.\n"),
        ("# Old.\n", ""),
        ("", "# New.\n"),
    ],
)
def test_derive_carries_comment_only_edits(before: str, after: str):
    base = _doc(f"[t]\nk = 0\n{before}a = 1\nb = 2\n")
    target = _doc(f"[t]\nk = 0\n{after}a = 1\nb = 2\n")
    delta = str(str(derive(base, target)))
    text = str(render(base, delta))
    assert _plain(str(text)) == _plain(target)
    assert ("# Old." in text) == ("Old" in after)
    assert ("# New." in text) == ("New" in after)


def test_render_edits_an_out_of_order_table():
    base = _doc(
        """
        [project]
        name = "loop"

        [groups]
        g = 1

        [project.urls]
        Home = "h"
        """,
    )
    delta = _doc('[project]\nname = "pkg"\n"!urls" = true\n')
    assert _plain(render(base, delta)) == {
        "project": {"name": "pkg"},
        "groups": {"g": 1},
    }


def test_render_appends_and_removes_tables_in_an_array():
    base = _doc(
        """
        [[tool.uv.index]]
        name = "cpu"

        [[tool.uv.index]]
        url = "https://pypi.together.ai/"
        """,
    )
    delta = _doc(
        """
        [tool.uv]
        "-index" = [{url = "https://pypi.together.ai/"}]
        "+index" = [{name = "cu128"}]
        """,
    )
    assert _plain(render(base, delta)) == {
        "tool": {"uv": {"index": [{"name": "cpu"}, {"name": "cu128"}]}},
    }


def test_render_ignores_the_settings_table():
    delta = _doc('[pyproject]\nbase = "../pyproject.toml"\n')
    assert _plain(render(_doc("a = 1\n"), delta)) == {"a": 1}


def test_render_rejects_removing_an_absent_entry():
    with pytest.raises(DeltaError, match="'z'"):
        render(_doc('a = ["x"]\n'), _doc('"-a" = ["z"]\n'))


def test_render_rejects_removing_an_absent_key():
    with pytest.raises(DeltaError, match="'b'"):
        render(_doc("a = 1\n"), _doc('"!b" = true\n'))


@pytest.mark.parametrize(
    "target",
    [
        'a = ["x", "y", "z"]\nb = 2\n[t]\nk = 1\n',
        'a = ["y"]\nb = 2\n[t]\nk = 1\n',
        'a = ["x", "y", "w"]\nb = 2\n[t]\nk = 1\n',
        'a = ["z", "y", "x"]\nb = 3\n[t]\nk = 1\nnew = [1]\n',
        'a = ["q"]\n[t]\nk = 1\n',
        'a = ["x", "y", "z"]\nb = 2\n[t]\nk = 1\n[t.sub]\nv = "s"\n',
        'a = ["x", "y", "z"]\nb = 2\n[[t.i]]\nn = 1\n[[t.i]]\nn = 2\n',
        'a = "x"\nb = [2]\n',
        'a = ["w", "x", "y", "z"]\nb = 2\n[t]\nk = 1\n',
        'a = ["x", "w", "z"]\nb = 2\n[t]\nk = 1\n',
    ],
)
def test_derive_reproduces_the_target(target: str):
    base = _doc('a = ["x", "y", "z"]\nb = 2\n[t]\nk = 1\n[[t.i]]\nn = 1\n')
    target_doc = str(target)
    delta = str(str(derive(base, target_doc)))
    assert _plain(render(base, delta)) == _plain(target_doc)


def test_derive_of_an_identical_document_is_empty():
    base = _doc('a = ["x"]\n[t]\nk = 1\n')
    assert _plain(derive(base, _doc('a = ["x"]\n[t]\nk = 1\n'))) == {}


def test_derive_uses_list_operators_and_keeps_comments():
    base = _doc('a = [\n    "x",\n    "y",\n]\n')
    target = _doc('a = [\n    "x",\n    # Extra.\n    "z",  # Why z.\n]\n')
    text = str(derive(base, target))
    assert _plain(str(text)) == {"-a": ["y"], "+a": ["z"]}
    assert "# Extra." in text
    assert "# Why z." in text


def test_source_renders_base_extends_and_renames(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text(
        'paths = ["src/stubs"]\ndrop = 1\nkeep = 2\n',
    )
    (tmp_path / "shared.delta.toml").write_text(
        '"!drop" = true\n\n[pyproject.rename]\n"src/stubs" = "typings"\n',
    )
    delta = _delta(tmp_path, body="keep = 3\n", extends=("shared.delta.toml",))
    source = PyprojectSource.load(delta, source_ref=tmp_path)
    assert _plain(str(source.render())) == {"paths": ["typings"], "keep": 3}


def test_source_layer_operations_use_its_renamed_spelling(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text('paths = ["src/stubs", "x"]\n')
    (tmp_path / "shared.delta.toml").write_text(
        '"-paths" = ["typings"]\n\n[pyproject.rename]\n"src/stubs" = "typings"\n',
    )
    delta = _delta(tmp_path, body="", extends=("shared.delta.toml",))
    source = PyprojectSource.load(delta, source_ref=tmp_path)
    assert _plain(str(source.render())) == {"paths": ["x"]}


def test_source_renders_utf8_with_one_trailing_newline(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text('a = "é"\n\n\n')
    source = PyprojectSource.load(_delta(tmp_path, body=""), source_ref=tmp_path)
    assert source.render() == 'a = "é"\n'


@pytest.mark.parametrize(
    "edit",
    [
        ("b = 1", "b = 2"),
        ('    "x",\n', '    "x",\n    "z",\n'),
        ("k = 9", "k = 8"),
        ('    "x",\n', ""),
    ],
)
def test_fold_reproduces_an_edited_render(tmp_path: Path, edit: tuple[str, str]):
    (tmp_path / "pyproject.toml").write_text(
        'a = [\n    "x",\n]\n# About b.\nb = 1\n[t]\nk = 0\n',
    )
    delta = _delta(tmp_path, body="[t]\nk = 9\n")
    source = PyprojectSource.load(delta, source_ref=tmp_path)
    public = source.render().replace(*edit)
    assert public != source.render()
    delta.write_text(source.fold(public))
    again = PyprojectSource.load(delta, source_ref=tmp_path).render()
    assert _plain(str(again)) == _plain(str(public))
    assert _plain(str(delta.read_text()))["pyproject"] == {
        "base": "pyproject.toml",
    }


@pytest.mark.parametrize(
    ("base", "public"),
    [
        ('[t]\n"a.b" = 1\n', '[t]\n"a.b" = 2\n'),
        ("[t]\na = 1\n", "[t]\na = 1\nb = 2\n"),
    ],
)
def test_fold_reproduces_the_public_file_byte_for_byte(
    tmp_path: Path,
    base: str,
    public: str,
):
    (tmp_path / "pyproject.toml").write_text(base)
    delta = _delta(tmp_path, body="")
    delta.write_text(PyprojectSource.load(delta, source_ref=tmp_path).fold(public))
    assert PyprojectSource.load(delta, source_ref=tmp_path).render() == public


def test_fold_rejects_a_delta_whose_render_loses_a_value(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """The delta is all that is stored, so a fold must never ship a lost value."""
    (tmp_path / "pyproject.toml").write_text("[t]\na = 1\n")
    source = PyprojectSource.load(_delta(tmp_path, body=""), source_ref=tmp_path)
    exact = render

    def lossy(base: str, delta: str) -> str:
        return exact(base, delta).replace("b = 2\n", "")

    monkeypatch.setattr(pyproject, "render", lossy)  # Fold's own render call.
    with pytest.raises(DeltaError, match=r"line 3: public 'b = 2', render ''"):
        source.fold("[t]\na = 1\nb = 2\n")


@pytest.mark.parametrize(
    ("base", "public"),
    [
        (
            "[t]\n# About a.\na = 1\nb = 1\n",
            "# New top.\n\n[t]\n# About a.\na = 2\nb = 1\n",
        ),
        ("[t]\n# About a.\na = 1\nb = 1\n", "[t]\n# Edited.\na = 2\nb = 1\n"),
        ("[t]\n# About a.\na = 1\nb = 1\n", "[t]\n# About a.\na = 2  # Why.\nb = 1\n"),
        ("[t]\n# About a.\na = 1\nb = 1\n", "[t]  # Why.\n# About a.\na = 2\nb = 1\n"),
        ("[t]\n# About a.\na = 1\nb = 1\n", "[t]\na = 2\nb = 1\n"),
        ("[t]\n# About a.\na = 1\nb = 1\n", "[t]\n\n# About a.\na = 2\nb = 1\n"),
        # Moved: the comment now explains another key.
        ("[t]\n# About a.\na = 1\nb = 1\n", "[t]\na = 1\n# About a.\nb = 1\n"),
        ("[t]\n# A.\na = 1\n# B.\nb = 1\n", "[t]\n# B.\na = 1\n# A.\nb = 1\n"),
        ("[t]\n# About a.\na = 1\nb = 1\n", "[t]\n# About a.\nn = 0\na = 1\nb = 1\n"),
        (
            "d = [\n    # D.\n    1,\n    2,\n]\n",
            "d = [\n    1,\n    # D.\n    2,\n]\n",
        ),
        # Left behind by a delete, the comment now explains the next key.
        ("[t]\n# About a.\na = 1\nb = 1\n", "[t]\n# About a.\nb = 1\n"),
        # A value edit that drops or rewrites the comment beside the value.
        ("[t]\na = 1  # Why.\n", "[t]\na = 2\n"),
        ("[t]\na = 1  # Why.\n", "[t]\na = 2  # Else.\n"),
        ('d = [\n    # T.\n    "t>=1",\n]\n', 'd = [\n    "t>=2",\n]\n'),
        ("[t]\na = 1\n", "[t]\na = 1\nb = 2  # Why.\n"),
        # Layout alone: a blank line or an indent no value edit needs.
        ("[t]\na = 1\nb = 1\n", "[t]\na = 1\n\nb = 1\n"),
        ("[t]\na = 1\n\n[u]\nb = 1\n", "[t]\na = 1\n[u]\nb = 1\n"),
        ("[t]\na = 1\nb = 1\n", "[t]\n  a = 1\nb = 1\n"),
    ],
)
def test_fold_rejects_a_public_comment_or_layout_edit(
    tmp_path: Path,
    base: str,
    public: str,
):
    """Comments come from the monorepo; a public edit to one is sent back there."""
    (tmp_path / "pyproject.toml").write_text(base)
    source = PyprojectSource.load(_delta(tmp_path, body=""), source_ref=tmp_path)
    with pytest.raises(DeltaError, match="no value edit explains"):
        source.fold(public)


@pytest.mark.parametrize(
    ("base", "public"),
    [
        ("[t]\n# About a.\na = 1\nb = 1\n", "[t]\n# About a.\na = 2\nb = 1\n"),
        ("[t]\n# About a.\na = 1\nb = 1\n", "[t]\n# About a.\na = 1\nb = 1\nc = 3\n"),
        # The comment beside a value stays with it.
        ("[t]\na = 1  # Why.\nb = 1\n", "[t]\na = 2  # Why.\nb = 1\n"),
        (
            'd = [\n    # T.\n    "t>=1",\n    "x",\n]\n',
            'd = [\n    # T.\n    "t>=2",\n    "x",\n]\n',
        ),
        (
            'd = [\n    "a",\n    # C.\n    "c",\n]\n',
            'd = [\n    "a",\n    # C.\n    "c",\n    "b",\n]\n',
        ),
        # A deleted key takes the comment lines it owns.
        ("[t]\n# About a.\na = 1\nb = 1\n", "[t]\nb = 1\n"),
        ("[t]\na = 1  # Why.\nb = 1\n", "[t]\nb = 1\n"),
        ("[t]\nx = 0\n\n# About a.\na = 1\nb = 1\n", "[t]\nx = 0\n\nb = 1\n"),
        (
            'd = [\n    # D.\n    "x",\n    # E.\n    "y",\n]\n',
            'd = [\n    # E.\n    "y",\n]\n',
        ),
        # A table comes and goes with the blank line that sets it apart.
        ("[t]\na = 1\n", "[t]\na = 1\n\n[u]\nb = 1\n"),
        ("[t]\na = 1\n\n# About u.\n[u]\nb = 1\n", "[t]\na = 1\n"),
        ("[t]\na = 1\n\n[u]\nb = 1\n\n[v]\nc = 1\n", "[t]\na = 1\n\n[v]\nc = 1\n"),
        # The list's other comments stay where an entry comes or goes.
        (
            'd = [\n    "a",\n    # B.\n    "b",\n    "c",\n    # End.\n]\n',
            'd = [\n    "a",\n    # B.\n    "b",\n    # End.\n]\n',
        ),
        (
            'd = [\n    # A.\n    "a",\n    # End.\n]\n',
            'd = [\n    # A.\n    "a",\n    # End.\n    "z",\n]\n',
        ),
        (
            'd = [\n    "a",  # Why.\n    # End.\n]\n',
            "d = [\n    # End.\n]\n",
        ),
        # An emptied table keeps its header and the lines above it.
        (
            "[t]\na = 1\n\n[u]\nb = 1\n\n[v]\nc = 1\n",
            "[t]\na = 1\n\n[u]\n\n[v]\nc = 1\n",
        ),
        ("[t]\na = 1\n\n# U.\n[u]\nb = 1\n", "[t]\na = 1\n\n# U.\n[u]\n"),
    ],
)
def test_fold_accepts_a_value_edit_beside_the_base_comments(
    tmp_path: Path,
    base: str,
    public: str,
):
    (tmp_path / "pyproject.toml").write_text(base)
    delta = _delta(tmp_path, body="")
    delta.write_text(PyprojectSource.load(delta, source_ref=tmp_path).fold(public))
    assert PyprojectSource.load(delta, source_ref=tmp_path).render() == public


def test_fold_accepts_a_value_edit_beside_the_delta_comments(tmp_path: Path):
    """The shipped file's comments include the delta's own, and those stay too."""
    (tmp_path / "pyproject.toml").write_text("[t]\na = 1\nb = 1\n")
    delta = _delta(tmp_path, body="[t]\n# Why 2.\na = 2\n")
    source = PyprojectSource.load(delta, source_ref=tmp_path)
    public = source.render().replace("b = 1", "b = 3")
    assert replace(source, delta=source.fold(public)).render() == public


def test_fold_accepts_a_value_edit_spelled_by_hand(tmp_path: Path):
    """Spacing within a line is spelling: the fold keeps the value, not the spaces."""
    (tmp_path / "pyproject.toml").write_text("[t]\n# About a.\na = 1\nb = 1\n")
    source = PyprojectSource.load(_delta(tmp_path, body=""), source_ref=tmp_path)
    public = "[t]\n# About a.\na   =   2\nb = 1\n"
    assert same_render(replace(source, delta=source.fold(public)).render(), public)


def test_fold_names_a_line_that_differs_only_in_indentation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    (tmp_path / "pyproject.toml").write_text("[t]\na = 1\n")
    source = PyprojectSource.load(_delta(tmp_path, body=""), source_ref=tmp_path)
    exact = render

    def indented(base: str, delta: str) -> str:
        return exact(base, delta).replace("b = 2\n", "  b = 2\n")

    monkeypatch.setattr(pyproject, "derive", _derive_b)
    monkeypatch.setattr(pyproject, "render", indented)
    with pytest.raises(DeltaError, match=r"line 3: public 'b = 2', render '  b = 2'"):
        source.fold("[t]\na = 1\nb = 2\n")


@pytest.mark.parametrize(
    ("current", "head", "expected"),
    [
        ("# New.\nb = 1\nc = 1\n", "b = 3\nc = 1\n", "# New.\nb = 3\nc = 1\n"),
        ("b = 1\nn = 0\nc = 1\n", "b = 3\nc = 1\n", "b = 3\nn = 0\nc = 1\n"),
        ("b = 1\nc = 2\n", "b = 3\nc = 1\n", "b = 3\nc = 2\n"),
        ("b = 3\nc = 1\n", "b = 3\nc = 1\n", "b = 3\nc = 1\n"),
        (
            "b = 1\nc = 1\n",
            "b = 1\nc = 1\n\n[t]\nk = 1\n",
            "b = 1\nc = 1\n\n[t]\nk = 1\n",
        ),
    ],
)
def test_merge_replays_the_public_edit_past_source_drift(
    tmp_path: Path,
    current: str,
    head: str,
    expected: str,
):
    (tmp_path / "pyproject.toml").write_text(current)
    source = PyprojectSource.load(_delta(tmp_path, body=""), source_ref=tmp_path)
    delta = source.merge(base="b = 1\nc = 1\n", head=head)
    assert replace(source, delta=delta).render() == expected


@pytest.mark.parametrize(
    ("current", "head", "match"),
    [
        (
            "b = 4\nc = 1\n",
            "b = 3\nc = 1\n",
            (
                r"^The source changed 'b' since the public base, and the public edit "
                r"changes it too$"
            ),
        ),
        ("c = 1\n", "b = 3\nc = 1\n", r"changed 'b'"),
        ("[t]\nk = 2\n", "[t]\nk = 3\n", r"changed 't\.k'"),
        ("b = 1\nc = 1\nd = 1\n", "b = 1\nc = 1\nd = 2\n", r"changed 'd'"),
        ("b = 1\nc = 2\n", "b = 1\n", r"changed 'c'"),
    ],
)
def test_merge_reports_a_value_both_sides_changed(
    tmp_path: Path,
    current: str,
    head: str,
    match: str,
):
    (tmp_path / "pyproject.toml").write_text(current)
    source = PyprojectSource.load(_delta(tmp_path, body=""), source_ref=tmp_path)
    base = "[t]\nk = 1\n" if current.startswith("[t]") else "b = 1\nc = 1\n"
    with pytest.raises(DeltaConflictError, match=match):
        source.merge(base=base, head=head)


def test_merge_of_an_edit_the_source_already_made_keeps_the_delta(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text("c = 1\n")
    source = PyprojectSource.load(_delta(tmp_path, body=""), source_ref=tmp_path)
    assert source.merge(base="b = 1\nc = 1\n", head="c = 1\n") == source.delta


def test_merge_reports_an_edit_that_no_longer_applies(tmp_path: Path):
    """A key both sides deleted agrees in value, but the delete has nothing to take."""
    (tmp_path / "pyproject.toml").write_text("c = 1\n")
    source = PyprojectSource.load(_delta(tmp_path, body=""), source_ref=tmp_path)
    with pytest.raises(DeltaConflictError, match="no longer applies"):
        source.merge(base="b = 1\nc = 1\n", head="c = 2\n")


def test_merge_refuses_a_comment_edit_whatever_the_drift(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text("# New.\nb = 1\nc = 1\n")
    source = PyprojectSource.load(_delta(tmp_path, body=""), source_ref=tmp_path)
    with pytest.raises(DeltaError, match="comments are maintained") as err:
        source.merge(base="b = 1\nc = 1\n", head="b = 1\n# Mine.\nc = 1\n")
    assert not isinstance(err.value, DeltaConflictError)


def test_fold_of_an_unchanged_render_keeps_only_the_delta(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text("a = 1\nb = 2\n")
    delta = _delta(tmp_path, body="b = 3\n")
    source = PyprojectSource.load(delta, source_ref=tmp_path)
    assert _plain(source.fold(source.render())) == {
        "b": 3,
        "pyproject": {"base": "pyproject.toml"},
    }


@pytest.mark.parametrize(
    ("base", "delta"),
    [
        ('a = "s"\n', '"+a" = ["x"]\n'),
        ('a = "s"\n', '"-a" = ["s"]\n'),
        ("[a]\nx = 1\n", '"+a" = [1]\n'),
        ("a = [1]\n", '"+a" = 2\n'),
        ("a = [1]\n", '"-a" = 1\n'),
        ("[[a]]\nn = 1\n", '"+a" = [1]\n'),
    ],
)
def test_render_rejects_a_list_operation_on_a_non_list(base: str, delta: str):
    with pytest.raises(DeltaError, match="'a'"):
        render(_doc(base), _doc(delta))


def test_source_load_reports_a_rename_that_breaks_toml_as_a_delta_error(
    tmp_path: Path,
):
    (tmp_path / "pyproject.toml").write_text('a = "x"\n')
    delta = _delta(tmp_path, body="")
    delta.write_text(delta.read_text() + '\n[pyproject.rename]\n"x" = "\\""\n')
    with pytest.raises(DeltaError, match="rename"):
        PyprojectSource.load(delta, source_ref=tmp_path)


def test_render_appends_tables_to_an_array_with_their_comments():
    base = _doc('[[g]]\nname = "a"\n')
    delta = _doc('# Why b.\n[["+g"]]\n# Inner.\nname = "b"  # Trailing.\n')
    out = str(render(base, delta))
    assert _plain(str(out)) == {"g": [{"name": "a"}, {"name": "b"}]}
    assert _comment_texts(out) == ["# Why b.", "# Inner.", "# Trailing."]
    assert out.index("# Why b.") < out.rindex("[[g]]")


@pytest.mark.parametrize(
    "target",
    [
        '[[g]]\nname = "a"\n\n# Why b.\n[[g]]\nname = "b"  # Trailing.\n',
        '[[g]]\nname = "a"\n\n[[g]]\n# Inner.\nname = "b"\n',
        '[[g]]\n# Inner.\nname = "a"\n',
        '[[g]]\nname = "a"  # Trailing.\n',
    ],
)
def test_derive_carries_comments_inside_an_array_of_tables(target: str):
    _assert_round_trip(_doc('[[g]]\nname = "a"\n'), target)


@pytest.mark.parametrize(
    "target",
    ["b = 2\n# end\n", "# Top.\n\nb = 2\n", "# Top.\n\nb = 2\n# end\n"],
)
def test_derive_keeps_a_preamble_or_footer_on_a_plain_document(target: str):
    """A document with no header has only a preamble and a footer to tell apart."""
    base = _doc("b = 2\n")
    delta = str(str(derive(base, _doc(target))))
    assert str(render(base, delta)) == target


def test_render_reads_a_comment_only_delta_as_the_comment_it_spells():
    """A hand-written delta of comments alone stays where its closing line puts it.

    ``derive`` closes a preamble with a blank line and writes a footer bare, so a
    bare run replaces the footer; a closed run, the preamble. Either way it lands
    where ``derive`` would have written it, never somewhere a human did not mean.
    """
    base = _doc("b = 2\n# end\n")
    assert str(render(base, _doc("# Fin.\n"))) == "b = 2\n# Fin.\n"
    assert str(render(base, _doc("# Top.\n\n"))) == "# Top.\n\nb = 2\n# end\n"


@pytest.mark.parametrize(
    ("left", "right", "same"),
    [
        ("b = 2\n", "b   =   2\n", True),
        ('u = "x#y"\n', 'u   =   "x#y"\n', True),
        ("x = nan\n", "x = nan\n", True),
        ("x = 1.0\n", "x = 1.00\n", True),
        ("[a]\nx = 1\n[b]\ny = 1\n", "[a]\nx = 1\n\n[b]\ny = 1\n", False),
        ("[a]\nx = 1\n\n[b]\ny = 1\n", "[a]\nx = 1\n\n [b]\ny = 1\n", False),
        (
            "a = [\n    1,\n    # N.\n]\nb = 1\n",
            "a = [\n    1,\n]\n# N.\nb = 1\n",
            False,
        ),
        ("a = [\n    1,\n]  # N.\nb = 1\n", "a = [\n    1,\n]\n# N.\nb = 1\n", False),
        ("b = 2\n", "b = 3\n", False),
        ("b = 2\n", "# Why.\nb = 2\n", False),
        ("b = 2\n", "b = 2  # Why.\n", False),
        ("b = 2\n", "b = 2\n# End.\n", False),
        ("# N.\na = 1\nb = 2\nc = 1\n", "a = 1\nb = 2\n# N.\nc = 1\n", False),
        ("a = 1\nb = 2\n", "b = 2\na = 1\n", False),
        ("a = [\n  1,\n  # N.\n  2,\n]\n", "a = [\n  # N.\n  1,\n  2,\n]\n", False),
        ("[t]\na = 1\n\n[u]\nb = 1\n", "[u]\nb = 1\n\n[t]\na = 1\n", False),
        ("[t]  # N.\na = 1\n", "[t]\n# N.\na = 1\n", False),
        ("a = [1, 2]\n", "a = [\n    1,\n    2,\n]\n", True),
    ],
)
def test_same_render_ignores_spelling_but_not_comments(
    left: str,
    right: str,
    same: bool,
):
    assert same_render(left, right) is same


def test_same_render_reports_malformed_toml_as_a_delta_error():
    with pytest.raises(DeltaError, match="Cannot parse"):
        same_render("a = [\n", "a = 1\n")


def _derive_b(shared: str, public: str) -> str:
    del shared, public
    return "[t]\nb = 2\n"


def _doc(text: str) -> str:
    return textwrap.dedent(text)


def _plain(text: str) -> dict[str, object]:
    return from_plain(tomllib.loads(text), dict[str, object])


def _comment_texts(text: str) -> list[str]:
    """Return each comment in ``text`` from its ``#``, in order."""
    return [
        line[line.index("#") :].strip() for line in text.splitlines() if "#" in line
    ]


def _assert_round_trip(base: str, target: str) -> None:
    """Assert ``derive`` then ``render`` reproduces ``target`` byte for byte."""
    assert render(base, derive(base, _doc(target))) == _doc(target)


def _delta(tmp_path: Path, *, body: str, extends: tuple[str, ...] = ()) -> Path:
    """Write ``pkg/pyproject.delta.toml`` against ``tmp_path/pyproject.toml``."""
    path = tmp_path / "pkg" / "pyproject.delta.toml"
    path.parent.mkdir(exist_ok=True)
    settings = 'base = "pyproject.toml"\n'
    if extends:
        settings += f"extends = {list(extends)!r}\n".replace("'", '"')
    # Top-level delta keys precede the settings table so they stay top-level.
    path.write_text(f"{body}\n[pyproject]\n{settings}")
    return path


if __name__ == "__main__":
    from copybarista.lib.testing.runner import test_main

    test_main(__file__)
