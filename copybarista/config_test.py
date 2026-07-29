"""Tests for Copybarista TOML configuration."""

from __future__ import annotations

from pathlib import Path

import tomllib

import pytest

from copybarista.config import (
    DEFAULT_PYTHON_EXCLUDES,
    load_config,
    parse_config,
    workflow_to_toml,
)
from copybarista.errors import ConfigError


def test_loads_sample_style_config(tmp_path: Path):
    config_path = tmp_path / "copy.barista.toml"
    config_path.write_text(
        """
        [workflow]
        name = "demo"
        mode = "squash"
        source_root = "project"

        [destination.folder]
        path = "/tmp/copybarista-demo"

        [files]
        include = ["**"]
        exclude = ["build/**", "**/__pycache__/**"]

        [[transform]]
        type = "replace"
        path = "demo_test.py"
        before = "from internal.demo import"
        after = "from demo import"

        [[transform]]
        type = "strip_block"
        path = "README.md"
        start = "<!-- copybarista:strip:start -->"
        end = "<!-- copybarista:strip:end -->"
        """,
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.name == "demo"
    assert config.mode == "squash"
    assert config.source_root == "project"
    assert config.files.include == ("**",)
    assert config.files.exclude == ("build/**", "**/__pycache__/**")
    assert config.files.moves == ()
    assert [transform.type for transform in config.transforms] == [
        "replace",
        "strip_block",
    ]


def test_loads_moves_config(tmp_path: Path):
    config_path = tmp_path / "copy.barista.toml"
    config_path.write_text(
        """
        [workflow]
        name = "demo"
        mode = "squash"
        source_root = "project"

        [files]
        include = ["**"]

        [[files.moves]]
        path = ""
        destination = "pkg"

        [[files.moves]]
        path = "pkg/README.md"
        destination = "README.md"

        [[files.moves]]
        path = "pkg/.github"
        destination = ".github"
        """,
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert [(move.path, move.destination) for move in config.files.moves] == [
        ("", "pkg"),
        ("pkg/README.md", "README.md"),
        ("pkg/.github", ".github"),
    ]
    # Writer round-trip: the ordered moves survive re-serialization verbatim.
    serialized = workflow_to_toml(config)
    assert "[[files.moves]]" in serialized
    assert 'destination = "pkg"' in serialized
    assert 'path = "pkg/README.md"' in serialized
    reparsed = parse_config(tomllib.loads(serialized))
    assert reparsed.files.moves == config.files.moves


def test_rejects_move_with_empty_destination_and_nonempty_path():
    """A non-whole-tree move with empty destination flattens irreversibly.

    Forward placement strips the ``path`` prefix, but the import reverse cannot
    re-prefix an ambiguous root path, so such a move is not invertible. The
    parser must reject it, matching the ``move`` transform's non-empty
    destination requirement.
    """
    raw = tomllib.loads(
        """
        [workflow]
        name = "demo"
        mode = "squash"
        source_root = "project"

        [files]
        include = ["**"]

        [[files.moves]]
        path = "sub"
        destination = ""
        """
    )
    with pytest.raises(ConfigError, match="destination"):
        parse_config(raw)


def test_rejects_move_with_empty_path_and_empty_destination():
    """A move that neither prefixes nor relocates is a no-op config error."""
    raw = tomllib.loads(
        """
        [workflow]
        name = "demo"
        mode = "squash"
        source_root = "project"

        [files]
        include = ["**"]

        [[files.moves]]
        path = ""
        destination = ""
        """
    )
    with pytest.raises(ConfigError, match="destination"):
        parse_config(raw)


def test_loads_file_copy_config(tmp_path: Path):
    config_path = tmp_path / "copy.barista.toml"
    config_path.write_text(
        """
        [workflow]
        name = "demo"
        mode = "squash"
        source_root = "project"

        [files]
        include = ["**"]

        [[files.copy]]
        source = "shared/json.py"
        destination = "demo/lib/json.py"
        include = ["*.py"]
        exclude = ["*_test.py"]
        """,
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert len(config.files.copy) == 1
    assert config.files.copy[0].source == "shared/json.py"
    assert config.files.copy[0].destination == "demo/lib/json.py"
    assert config.files.copy[0].include == ("*.py",)
    assert config.files.copy[0].exclude == ("*_test.py",)
    serialized = workflow_to_toml(config)
    assert "[[files.copy]]" in serialized
    assert 'source = "shared/json.py"' in serialized
    assert 'include = ["*.py"]' in serialized
    assert 'exclude = ["*_test.py"]' in serialized


def test_loads_file_write_config(tmp_path: Path):
    config_path = tmp_path / "copy.barista.toml"
    config_path.write_text(
        """
        [workflow]
        name = "demo"
        mode = "squash"
        source_root = "project"

        [files]
        include = ["**"]

        [[files.write]]
        path = "demo/lib/web/__init__.py"
        content = "\\\"\\\"\\\"Web helpers.\\\"\\\"\\\"\\n"
        """,
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert len(config.files.write) == 1
    assert config.files.write[0].path == "demo/lib/web/__init__.py"
    assert config.files.write[0].content == '"""Web helpers."""\n'
    serialized = workflow_to_toml(config)
    assert "[[files.write]]" in serialized
    assert 'path = "demo/lib/web/__init__.py"' in serialized
    assert 'content = "\\"\\"\\"Web helpers.\\"\\"\\"\\n"' in serialized


def test_default_python_excludes_off_by_default(tmp_path: Path):
    config_path = tmp_path / "copy.barista.toml"
    config_path.write_text(
        """
        [workflow]
        name = "demo"
        mode = "squash"
        source_root = "project"

        [destination.folder]
        path = "/tmp/copybarista-demo"

        [files]
        include = ["**"]
        exclude = ["build/**"]

        [[files.copy]]
        source = "shared/pkg"
        destination = "demo/pkg"
        """,
        encoding="utf-8",
    )

    config = load_config(config_path)

    # The default set is opt-in: absent the flag, nothing is prepended.
    assert config.files.use_default_python_excludes is False
    assert config.files.effective_exclude() == ("build/**",)
    assert config.files.copy[0].use_default_python_excludes is False
    assert config.files.copy[0].effective_exclude() == ()


def test_default_python_excludes_cover_build_artifacts():
    # Build outputs and coverage artifacts are Python-generated and belong in the
    # single default set, not duplicated in a second sync-only list.
    for pattern in ("build/**", "dist/**", "htmlcov/**", ".coverage"):
        assert pattern in DEFAULT_PYTHON_EXCLUDES


def test_use_default_python_excludes_opt_in(tmp_path: Path):
    config_path = tmp_path / "copy.barista.toml"
    config_path.write_text(
        """
        [workflow]
        name = "demo"
        mode = "squash"
        source_root = "project"

        [destination.folder]
        path = "/tmp/copybarista-demo"

        [files]
        include = ["**"]
        exclude = ["build/**"]
        use_default_python_excludes = true

        [[files.copy]]
        source = "shared/pkg"
        destination = "demo/pkg"
        use_default_python_excludes = true
        """,
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.files.use_default_python_excludes is True
    # Opting in prepends the defaults while preserving the declared exclude.
    effective = config.files.effective_exclude()
    assert ".venv/**" in effective
    assert "**/.venv/**" in effective
    assert "**/__pycache__/**" in effective
    assert "build/**" in effective
    # The raw config field stays exactly what the TOML declared.
    assert config.files.exclude == ("build/**",)
    assert config.files.copy[0].use_default_python_excludes is True
    assert ".venv/**" in config.files.copy[0].effective_exclude()
    # Round-trip preserves the opt-in on both the selection and the copy.
    serialized = workflow_to_toml(config)
    assert serialized.count("use_default_python_excludes = true") == 2


def test_loads_leak_check_policy(tmp_path: Path):
    config_path = tmp_path / "copy.barista.toml"
    config_path.write_text(
        """
        [workflow]
        name = "demo"
        mode = "squash"
        source_root = "project"

        [files]
        include = ["**"]

        [leak_check]

        [[leak_check.forbidden_path]]
        id = "private-paths"
        paths = ["private/**", "copy.barista.toml"]
        message = "source-only path"

        [[leak_check.forbidden_text]]
        id = "loop-imports"
        pattern = "\\\\binternal_pkg\\\\."
        paths = ["**/*.py"]
        exclude = ["tests/**"]
        """,
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.leak_check.forbidden_path[0].id == "private-paths"
    assert config.leak_check.forbidden_path[0].paths == (
        "private/**",
        "copy.barista.toml",
    )
    assert config.leak_check.forbidden_text[0].pattern == r"\binternal_pkg\."
    assert config.leak_check.forbidden_text[0].exclude == ("tests/**",)
    serialized = workflow_to_toml(config)
    assert "[[leak_check.forbidden_path]]" in serialized
    assert "[[leak_check.forbidden_text]]" in serialized


def test_rejects_invalid_leak_check_regex(tmp_path: Path):
    config_path = tmp_path / "copy.barista.toml"
    config_path.write_text(
        """
        [workflow]
        name = "demo"
        mode = "squash"
        source_root = "project"

        [files]
        include = ["**"]

        [[leak_check.forbidden_text]]
        pattern = "["
        """,
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="forbidden_text regex"):
        load_config(config_path)


def test_rejects_file_copy_escape(tmp_path: Path):
    config_path = tmp_path / "copy.barista.toml"
    config_path.write_text(
        """
        [workflow]
        name = "demo"
        mode = "squash"
        source_root = "project"

        [files]
        include = ["**"]

        [[files.copy]]
        source = "../shared/json.py"
        destination = "demo/lib/json.py"
        """,
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match=r"files\.copy\.source"):
        load_config(config_path)


def test_rejects_file_write_escape(tmp_path: Path):
    config_path = tmp_path / "copy.barista.toml"
    config_path.write_text(
        """
        [workflow]
        name = "demo"
        mode = "squash"
        source_root = "project"

        [files]
        include = ["**"]

        [[files.write]]
        path = "../demo/__init__.py"
        content = ""
        """,
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match=r"files\.write\.path"):
        load_config(config_path)


def test_loads_ruff_format_transform(tmp_path: Path):
    config_path = tmp_path / "copy.barista.toml"
    config_path.write_text(
        """
        [workflow]
        name = "demo"
        mode = "squash"
        source_root = "project"

        [files]
        include = ["**"]

        [[transform]]
        type = "ruff_format"
        path = "."
        """,
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.transforms[0].type == "ruff_format"
    assert config.transforms[0].path == "."


def test_rejects_missing_workflow(tmp_path: Path):
    config_path = tmp_path / "copy.barista.toml"
    config_path.write_text(
        """
        [files]
        include = ["**"]
        """,
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="workflow"):
        load_config(config_path)


def test_rejects_unknown_top_level_key(tmp_path: Path):
    config_path = tmp_path / "copy.barista.toml"
    config_path.write_text(
        """
        [workflow]
        name = "demo"
        mode = "squash"
        source_root = "project"

        [files]
        include = ["**"]

        [authoring]
        mapping = "legacy-style-but-unsupported"
        """,
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="authoring"):
        load_config(config_path)


def test_rejects_unknown_workflow_key(tmp_path: Path):
    config_path = tmp_path / "copy.barista.toml"
    config_path.write_text(
        """
        [workflow]
        name = "demo"
        mode = "squash"
        source_root = "project"
        last_rev_state = "unsupported"

        [files]
        include = ["**"]
        """,
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="last_rev_state"):
        load_config(config_path)


def test_workflow_defaults_globstar_to_one_or_more(tmp_path: Path):
    config_path = tmp_path / "copy.barista.toml"
    config_path.write_text(
        """
        [workflow]
        name = "demo"
        mode = "squash"
        source_root = "project"

        [files]
        include = ["**"]
        """,
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.globstar == "one_or_more"


def test_workflow_can_opt_into_zero_or_more_globstar(tmp_path: Path):
    config_path = tmp_path / "copy.barista.toml"
    config_path.write_text(
        """
        [workflow]
        name = "demo"
        mode = "squash"
        source_root = "project"
        globstar = "zero_or_more"

        [files]
        include = ["**"]
        """,
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.globstar == "zero_or_more"


def test_rejects_unknown_globstar_value(tmp_path: Path):
    config_path = tmp_path / "copy.barista.toml"
    config_path.write_text(
        """
        [workflow]
        name = "demo"
        mode = "squash"
        source_root = "project"
        globstar = "infinite"

        [files]
        include = ["**"]
        """,
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="globstar"):
        load_config(config_path)


def test_rejects_unsupported_mode(tmp_path: Path):
    config_path = tmp_path / "copy.barista.toml"
    config_path.write_text(
        """
        [workflow]
        name = "demo"
        mode = "iterative"
        source_root = "project"

        [files]
        include = ["**"]
        """,
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="mode"):
        load_config(config_path)


def test_rejects_unsupported_replace_options(tmp_path: Path):
    config_path = tmp_path / "copy.barista.toml"
    config_path.write_text(
        """
        [workflow]
        name = "demo"
        mode = "squash"
        source_root = "project"

        [files]
        include = ["**"]

        [[transform]]
        type = "replace"
        path = "demo.py"
        before = "old"
        after = "new"
        first_only = true
        """,
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="first_only"):
        load_config(config_path)


def test_rejects_unknown_git_key(tmp_path: Path):
    config_path = tmp_path / "copy.barista.toml"
    config_path.write_text(
        """
        [workflow]
        name = "demo"
        mode = "squash"
        source_root = "project"

        [files]
        include = ["**"]

        [destination.git]
        url = "file:///tmp/demo.git"
        fetch = "unsupported"
        """,
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="fetch"):
        load_config(config_path)


def test_accepts_java_style_file_glob_syntax(tmp_path: Path):
    config_path = tmp_path / "copy.barista.toml"
    config_path.write_text(
        """
        [workflow]
        name = "demo"
        mode = "squash"
        source_root = "project"

        [files]
        include = ["src/{main,test}.py"]
        """,
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.files.include == ("src/{main,test}.py",)


def test_rejects_transform_path_traversal(tmp_path: Path):
    config_path = tmp_path / "copy.barista.toml"
    config_path.write_text(
        """
        [workflow]
        name = "demo"
        mode = "squash"
        source_root = "project"

        [files]
        include = ["**"]

        [[transform]]
        type = "replace"
        path = "../outside.py"
        before = "old"
        after = "new"
        """,
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="path"):
        load_config(config_path)


def test_rejects_empty_replace_before(tmp_path: Path):
    config_path = tmp_path / "copy.barista.toml"
    config_path.write_text(
        """
        [workflow]
        name = "demo"
        mode = "squash"
        source_root = "project"

        [files]
        include = ["**"]

        [[transform]]
        type = "replace"
        path = "demo.py"
        before = ""
        after = "new"
        """,
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="non-empty"):
        load_config(config_path)


def test_parses_explicit_replace_reversal(tmp_path: Path):
    config_path = tmp_path / "copy.barista.toml"
    config_path.write_text(
        """
        [workflow]
        name = "demo"
        mode = "squash"
        source_root = "project"

        [files]
        include = ["**"]

        [[transform]]
        type = "replace"
        path = "pkg/*.py"
        before = "internal"
        after = "public"
        reverse_before = "public import"
        reverse_after = "internal import"
        """,
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.transforms[0].reverse_before == "public import"
    assert config.transforms[0].reverse_after == "internal import"


def _regex_groups_config(body: str) -> str:
    """Wrap a single replace transform body in a minimal workflow config."""
    return (
        '[workflow]\nname = "demo"\nmode = "squash"\nsource_root = "project"\n\n'
        '[files]\ninclude = ["**"]\n\n'
        '[[transform]]\ntype = "replace"\npath = "pkg/*.py"\n' + body
    )


def test_parses_and_round_trips_regex_groups(tmp_path: Path):
    config_path = tmp_path / "copy.barista.toml"
    config_path.write_text(
        _regex_groups_config(
            'before = "internal.pkg.${s}"\n'
            'after = "pkg.${s}"\n'
            'regex_groups = { s = "[A-Za-z_]" }\n'
            "required = false\n"
        ),
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.transforms[0].regex_groups == (("s", "[A-Za-z_]"),)
    # Serialization preserves the binding so config round-trips.
    assert 'regex_groups = { s = "[A-Za-z_]" }' in workflow_to_toml(config)


def test_rejects_invalid_regex_groups_pattern(tmp_path: Path):
    config_path = tmp_path / "copy.barista.toml"
    config_path.write_text(
        _regex_groups_config(
            'before = "internal.pkg.${s}"\n'
            'after = "pkg.${s}"\n'
            'regex_groups = { s = "[unterminated" }\n'
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="not valid regex"):
        load_config(config_path)


def test_rejects_non_string_regex_groups_pattern(tmp_path: Path):
    config_path = tmp_path / "copy.barista.toml"
    config_path.write_text(
        _regex_groups_config(
            'before = "internal.pkg.${s}"\nafter = "pkg.${s}"\nregex_groups = { s = 7 }\n'
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="must be a string"):
        load_config(config_path)


def test_rejects_regex_groups_with_explicit_reversal(tmp_path: Path):
    config_path = tmp_path / "copy.barista.toml"
    config_path.write_text(
        _regex_groups_config(
            'before = "internal.pkg.${s}"\n'
            'after = "pkg.${s}"\n'
            'regex_groups = { s = "[A-Za-z_]" }\n'
            'reverse_before = "pkg"\n'
            'reverse_after = "internal.pkg"\n'
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="mutually exclusive"):
        load_config(config_path)


def test_rejects_unknown_transform_type(tmp_path: Path):
    config_path = tmp_path / "copy.barista.toml"
    config_path.write_text(
        """
        [workflow]
        name = "demo"
        mode = "squash"
        source_root = "project"

        [files]
        include = ["**"]

        [[transform]]
        type = "regex_groups"
        path = "demo.py"
        before = "old"
        after = "new"
        """,
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="transform"):
        load_config(config_path)


def test_rejects_empty_strip_block_markers(tmp_path: Path):
    config_path = tmp_path / "copy.barista.toml"
    config_path.write_text(
        """
        [workflow]
        name = "demo"
        mode = "squash"
        source_root = "project"

        [files]
        include = ["**"]

        [[transform]]
        type = "strip_block"
        path = "README.md"
        start = ""
        end = "<!-- copybarista:strip:end -->"
        """,
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="non-empty"):
        load_config(config_path)


def test_parses_move_transform(tmp_path: Path):
    config_path = tmp_path / "copy.barista.toml"
    config_path.write_text(
        """
        [workflow]
        name = "demo"
        mode = "squash"
        source_root = "project"

        [files]
        include = ["**"]

        [[transform]]
        type = "move"
        path = "old/readme.md"
        destination = "new/readme.md"
        """,
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.transforms[0].type == "move"
    assert config.transforms[0].path == "old/readme.md"
    assert config.transforms[0].destination == "new/readme.md"


def test_rejects_move_glob_path(tmp_path: Path):
    config_path = tmp_path / "copy.barista.toml"
    config_path.write_text(
        """
        [workflow]
        name = "demo"
        mode = "squash"
        source_root = "project"

        [files]
        include = ["**"]

        [[transform]]
        type = "move"
        path = "*.md"
        destination = "docs/readme.md"
        """,
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="exact file"):
        load_config(config_path)


def test_rejects_move_empty_destination(tmp_path: Path):
    config_path = tmp_path / "copy.barista.toml"
    config_path.write_text(
        """
        [workflow]
        name = "demo"
        mode = "squash"
        source_root = "project"

        [files]
        include = ["**"]

        [[transform]]
        type = "move"
        path = "old/readme.md"
        destination = ""
        """,
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="non-empty"):
        load_config(config_path)
