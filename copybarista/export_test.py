"""Tests for local folder export behavior."""

from __future__ import annotations

from pathlib import Path

import os
import stat

import pytest

from copybarista.config import load_config
from copybarista.errors import ExportError, LeakCheckError
from copybarista.export import export_folder


def test_folder_export_filters_transforms_and_cleans_stale_files(tmp_path: Path):
    source_ref = tmp_path / "repo"
    project = source_ref / "project"
    project.mkdir(parents=True)
    (project / "README.md").write_text(
        "visible\n"
        "<!-- copybarista:strip:start -->\n"
        "internal\n"
        "<!-- copybarista:strip:end -->\n",
        encoding="utf-8",
    )
    (project / "module_test.py").write_text("from private import thing\n", "utf-8")
    (project / "ignored.pyc").write_bytes(b"cache")
    script = project / "script.sh"
    script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)

    destination = tmp_path / "out"
    (destination / "stale_dir").mkdir(parents=True)
    (destination / "stale_dir" / "stale.txt").write_text("stale", encoding="utf-8")

    config_path = tmp_path / "copy.barista.toml"
    config_path.write_text(
        f"""
        [workflow]
        name = "project"
        mode = "squash"
        source_root = "project"

        [destination.folder]
        path = "{destination.as_posix()}"

        [files]
        include = ["**"]
        exclude = ["*.pyc"]

        [[transform]]
        type = "replace"
        path = "module_test.py"
        before = "from private import"
        after = "from public import"

        [[transform]]
        type = "strip_block"
        path = "README.md"
        start = "<!-- copybarista:strip:start -->"
        end = "<!-- copybarista:strip:end -->"
        """,
        encoding="utf-8",
    )

    manifest = export_folder(
        load_config(config_path),
        source_ref=source_ref,
        destination=destination,
        force=True,
    )

    assert sorted(
        path.relative_to(destination).as_posix()
        for path in destination.rglob("*")
        if path.is_file()
    ) == ["README.md", "module_test.py", "script.sh"]
    assert (destination / "README.md").read_text(encoding="utf-8") == "visible\n"
    assert (destination / "module_test.py").read_text(encoding="utf-8") == (
        "from public import thing\n"
    )
    assert os.access(destination / "script.sh", os.X_OK)
    assert [entry.destination for entry in manifest.files] == [
        "README.md",
        "module_test.py",
        "script.sh",
    ]
    assert [
        (transform.id, transform.changed, transform.count)
        for transform in manifest.transforms
    ] == [
        ("1:replace:module_test.py", 1, 1),
        ("2:strip_block:README.md", 1, 1),
    ]
    assert [
        (file.source, file.destination, file.count)
        for transform in manifest.transforms
        for file in transform.files
    ] == [
        ("project/module_test.py", "module_test.py", 1),
        ("project/README.md", "README.md", 1),
    ]


def test_folder_export_manifest_is_deterministic_across_runs(tmp_path: Path):
    source_ref = tmp_path / "repo"
    project = source_ref / "project"
    project.mkdir(parents=True)
    (project / "b.txt").write_text("b\n", encoding="utf-8")
    (project / "a.txt").write_text("a\n", encoding="utf-8")
    config_path = tmp_path / "copy.barista.toml"
    config_path.write_text(
        """
        [workflow]
        name = "project"
        mode = "squash"
        source_root = "project"

        [files]
        include = ["**"]
        """,
        encoding="utf-8",
    )
    config = load_config(config_path)

    first = export_folder(
        config,
        source_ref=source_ref,
        destination=tmp_path / "out1",
        force=True,
    )
    second = export_folder(
        config,
        source_ref=source_ref,
        destination=tmp_path / "out2",
        force=True,
    )

    assert first.to_json() == second.to_json()


def test_folder_export_can_prefix_package_files(tmp_path: Path):
    source_ref = tmp_path / "repo"
    project = source_ref / "project"
    project.mkdir(parents=True)
    (project / "pkg").mkdir()
    (project / "__init__.py").write_text(
        "from internal.demo import api\n", encoding="utf-8"
    )
    (project / "pkg" / "module.py").write_text(
        "from internal.demo import api\n", encoding="utf-8"
    )
    (project / "README.md").write_text("readme\n", encoding="utf-8")
    (project / ".gitignore").write_text(".venv/\n", encoding="utf-8")
    config_path = tmp_path / "copy.barista.toml"
    config_path.write_text(
        """
        [workflow]
        name = "project"
        mode = "squash"
        source_root = "project"

        [files]
        include = ["**"]
        destination_prefix = "demo"
        destination_prefix_exclude = [".gitignore", "README.md"]

        [[transform]]
        type = "replace"
        path = "demo/{*.py,**/*.py}"
        before = "internal.demo"
        after = "demo"
        """,
        encoding="utf-8",
    )

    manifest = export_folder(
        load_config(config_path),
        source_ref=source_ref,
        destination=tmp_path / "out",
        force=True,
    )

    assert [entry.destination for entry in manifest.files] == [
        ".gitignore",
        "README.md",
        "demo/__init__.py",
        "demo/pkg/module.py",
    ]
    assert (tmp_path / "out" / ".gitignore").read_text(encoding="utf-8") == ".venv/\n"
    assert (tmp_path / "out" / "README.md").read_text(encoding="utf-8") == "readme\n"
    assert (tmp_path / "out" / "demo" / "__init__.py").read_text(
        encoding="utf-8"
    ) == "from demo import api\n"


def test_folder_export_can_run_ruff_format_transform(tmp_path: Path):
    source_ref = tmp_path / "repo"
    project = source_ref / "project"
    project.mkdir(parents=True)
    (project / "pyproject.toml").write_text(
        """
        [tool.ruff]
        fix = true

        [tool.ruff.lint]
        select = ["I"]
        """,
        encoding="utf-8",
    )
    (project / "module.py").write_text("import sys\nimport os\n", encoding="utf-8")
    config_path = tmp_path / "copy.barista.toml"
    config_path.write_text(
        """
        [workflow]
        name = "project"
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

    manifest = export_folder(
        load_config(config_path),
        source_ref=source_ref,
        destination=tmp_path / "out",
        force=True,
    )

    assert (tmp_path / "out" / "module.py").read_text(encoding="utf-8") == (
        "import os\nimport sys\n"
    )
    assert manifest.transforms[0].type == "ruff_format"
    assert manifest.transforms[0].changed == 1


def test_folder_export_ruff_format_allows_unfixable_lint_after_fixes(tmp_path: Path):
    source_ref = tmp_path / "repo"
    project = source_ref / "project"
    project.mkdir(parents=True)
    (project / "pyproject.toml").write_text(
        """
        [tool.ruff]
        fix = true

        [tool.ruff.lint]
        select = ["F401", "I"]
        """,
        encoding="utf-8",
    )
    (project / "module.py").write_text("import sys\nimport os\n", encoding="utf-8")
    config_path = tmp_path / "copy.barista.toml"
    config_path.write_text(
        """
        [workflow]
        name = "project"
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

    manifest = export_folder(
        load_config(config_path),
        source_ref=source_ref,
        destination=tmp_path / "out",
        force=True,
    )

    assert (tmp_path / "out" / "module.py").read_text(encoding="utf-8") == ""
    assert manifest.transforms[0].changed == 1


def test_folder_export_runs_leak_checks_after_transforms(tmp_path: Path):
    source_ref = tmp_path / "repo"
    project = source_ref / "project"
    project.mkdir(parents=True)
    (project / "module.py").write_text(
        "from internal_pkg.demo import api\n", encoding="utf-8"
    )
    config_path = tmp_path / "copy.barista.toml"
    config_path.write_text(
        """
        [workflow]
        name = "project"
        mode = "squash"
        source_root = "project"

        [files]
        include = ["**"]

        [[transform]]
        type = "replace"
        path = "module.py"
        before = "internal_pkg.demo"
        after = "demo"

        [[leak_check.forbidden_text]]
        id = "loop-imports"
        pattern = "\\\\binternal_pkg\\\\."
        paths = ["*.py", "**/*.py"]
        """,
        encoding="utf-8",
    )

    export_folder(
        load_config(config_path),
        source_ref=source_ref,
        destination=tmp_path / "out",
        force=True,
    )

    assert (tmp_path / "out" / "module.py").read_text(encoding="utf-8") == (
        "from demo import api\n"
    )


def test_folder_export_fails_on_leak_check_violation(tmp_path: Path):
    source_ref = tmp_path / "repo"
    project = source_ref / "project"
    project.mkdir(parents=True)
    (project / "module.py").write_text(
        "from internal_pkg.demo import api\n", encoding="utf-8"
    )
    config_path = tmp_path / "copy.barista.toml"
    config_path.write_text(
        """
        [workflow]
        name = "project"
        mode = "squash"
        source_root = "project"

        [files]
        include = ["**"]

        [[leak_check.forbidden_text]]
        id = "loop-imports"
        pattern = "\\\\binternal_pkg\\\\."
        paths = ["*.py", "**/*.py"]
        """,
        encoding="utf-8",
    )

    with pytest.raises(LeakCheckError, match="Leak check failed"):
        export_folder(
            load_config(config_path),
            source_ref=source_ref,
            destination=tmp_path / "out",
            force=True,
        )


def test_folder_export_ruff_format_allows_noop(tmp_path: Path):
    source_ref = tmp_path / "repo"
    project = source_ref / "project"
    project.mkdir(parents=True)
    (project / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    config_path = tmp_path / "copy.barista.toml"
    config_path.write_text(
        """
        [workflow]
        name = "project"
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

    manifest = export_folder(
        load_config(config_path),
        source_ref=source_ref,
        destination=tmp_path / "out",
        force=True,
    )

    assert manifest.transforms[0].changed == 0
    assert manifest.transforms[0].files == ()


def test_folder_export_requires_force_for_existing_destination(tmp_path: Path):
    source_ref = tmp_path / "repo"
    project = source_ref / "project"
    project.mkdir(parents=True)
    (project / "README.md").write_text("visible\n", encoding="utf-8")
    destination = tmp_path / "out"
    destination.mkdir()

    config_path = tmp_path / "copy.barista.toml"
    config_path.write_text(
        f"""
        [workflow]
        name = "project"
        mode = "squash"
        source_root = "project"

        [destination.folder]
        path = "{destination.as_posix()}"

        [files]
        include = ["**"]
        """,
        encoding="utf-8",
    )

    with pytest.raises(ExportError, match="--force"):
        export_folder(
            load_config(config_path),
            source_ref=source_ref,
            destination=destination,
        )
