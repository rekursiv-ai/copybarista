"""Tests for Copybarista TOML configuration."""

from __future__ import annotations

from pathlib import Path

import pytest

from copybarista.config import (
    load_config,
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
    assert config.files.destination_prefix == ""
    assert config.files.destination_prefix_exclude == ()
    assert [transform.type for transform in config.transforms] == [
        "replace",
        "strip_block",
    ]


def test_loads_destination_prefix_config(tmp_path: Path):
    config_path = tmp_path / "copy.barista.toml"
    config_path.write_text(
        """
        [workflow]
        name = "demo"
        mode = "squash"
        source_root = "project"

        [files]
        include = ["**"]
        destination_prefix = "pkg"
        destination_prefix_exclude = ["README.md", ".github/**"]
        """,
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.files.destination_prefix == "pkg"
    assert config.files.destination_prefix_exclude == ("README.md", ".github/**")


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
