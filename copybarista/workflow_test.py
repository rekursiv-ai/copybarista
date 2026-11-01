"""Tests for workflow staging edge cases."""

from __future__ import annotations

from dataclasses import replace
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

import re

import pytest

from copybarista.config import (
    FileCopy,
    FileMove,
    FileSelection,
    FileWrite,
    FolderDestination,
    GitDestination,
    Transform,
    WorkflowConfig,
    parse_config,
)
from copybarista.errors import ExportError
from copybarista.workflow import MoveSequence, WorkflowRunner


if TYPE_CHECKING:
    from pathlib import Path


def _config(
    source_root: str = "project",
    copies: tuple[FileCopy, ...] = (),
    writes: tuple[FileWrite, ...] = (),
    transforms: tuple[Transform, ...] = (),
) -> WorkflowConfig:
    return WorkflowConfig(
        name="demo",
        mode="squash",
        source_root=source_root,
        files=FileSelection(include=("**",), exclude=(), copy=copies, write=writes),
        transforms=transforms,
        folder=FolderDestination(),
        git=GitDestination(),
    )


def test_workflow_runner_reports_missing_source_root(tmp_path: Path):
    with pytest.raises(ExportError, match="Source root"):
        WorkflowRunner(config=_config(), source_ref=tmp_path).stage(tmp_path / "stage")


def test_workflow_runner_allows_empty_selection(tmp_path: Path):
    source_ref = tmp_path / "repo"
    (source_ref / "project").mkdir(parents=True)

    staged = WorkflowRunner(config=_config(), source_ref=source_ref).stage(
        tmp_path / "stage",
    )

    assert staged.files == ()
    assert staged.transforms == ()


def test_workflow_runner_keeps_root_sources_relative(tmp_path: Path):
    source_ref = tmp_path / "repo"
    source_ref.mkdir()
    (source_ref / "README.md").write_text("hello\n", encoding="utf-8")

    staged = WorkflowRunner(
        config=_config(source_root=""),
        source_ref=source_ref,
    ).stage(tmp_path / "stage")

    assert [(entry.source, entry.destination) for entry in staged.files] == [
        ("README.md", "README.md"),
    ]


def test_workflow_runner_can_transform_read_only_source_snapshot(tmp_path: Path):
    source_ref = tmp_path / "repo"
    project = source_ref / "project"
    project.mkdir(parents=True)
    source = project / "module.py"
    source.write_text("from private import value\n", encoding="utf-8")
    source.chmod(0o444)

    staged = WorkflowRunner(
        config=_config(
            transforms=(
                Transform(
                    id="replace-import",
                    type="replace",
                    path="module.py",
                    before="from private import",
                    after="from public import",
                ),
            ),
        ),
        source_ref=source_ref,
    ).stage(tmp_path / "stage")

    assert (staged.root / "module.py").read_text(encoding="utf-8") == (
        "from public import value\n"
    )


def test_workflow_runner_rejects_symlink_outside_source_root(tmp_path: Path):
    source_ref = tmp_path / "repo"
    project = source_ref / "project"
    project.mkdir(parents=True)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret\n", encoding="utf-8")
    (project / "leak.txt").symlink_to(outside)

    with pytest.raises(ExportError, match="Symlink points outside"):
        WorkflowRunner(config=_config(), source_ref=source_ref).stage(
            tmp_path / "stage",
        )


def test_default_python_excludes_skip_out_of_root_venv_symlink(tmp_path: Path):
    # A local `uv sync` leaves .venv/bin/python as a symlink to an interpreter
    # outside the source root. Opting into the python excludes filters it before
    # the symlink guard runs, so the export does not abort.
    source_ref = tmp_path / "repo"
    project = source_ref / "project"
    (project / ".venv" / "bin").mkdir(parents=True)
    (project / "module.py").write_text("x = 1\n", encoding="utf-8")
    outside = tmp_path / "python"
    outside.write_text("#!/bin/sh\n", encoding="utf-8")
    (project / ".venv" / "bin" / "python").symlink_to(outside)
    config = WorkflowConfig(
        name="demo",
        mode="squash",
        source_root="project",
        files=FileSelection(
            include=("**",),
            exclude=(),
            use_default_python_excludes=True,
        ),
        transforms=(),
        folder=FolderDestination(),
        git=GitDestination(),
    )

    staged = WorkflowRunner(config=config, source_ref=source_ref).stage(
        tmp_path / "stage",
    )

    destinations = {entry.destination for entry in staged.files}
    assert "module.py" in destinations
    assert not any(".venv" in dest for dest in destinations)


def test_venv_symlink_rejected_when_default_excludes_disabled(tmp_path: Path):
    # With the python excludes off (the default), an out-of-root .venv symlink is
    # not filtered and the export aborts at the symlink guard.
    source_ref = tmp_path / "repo"
    project = source_ref / "project"
    (project / ".venv" / "bin").mkdir(parents=True)
    outside = tmp_path / "python"
    outside.write_text("#!/bin/sh\n", encoding="utf-8")
    (project / ".venv" / "bin" / "python").symlink_to(outside)

    with pytest.raises(ExportError, match="Symlink points outside"):
        WorkflowRunner(config=_config(), source_ref=source_ref).stage(
            tmp_path / "stage",
        )


def test_workflow_runner_rejects_source_root_symlink_escape(tmp_path: Path):
    source_ref = tmp_path / "repo"
    outside = tmp_path / "outside"
    outside.mkdir()
    source_ref.mkdir()
    (source_ref / "project").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ExportError, match="escapes source checkout"):
        WorkflowRunner(config=_config(), source_ref=source_ref).stage(
            tmp_path / "stage",
        )


def test_workflow_runner_preserves_internal_symlink(tmp_path: Path):
    source_ref = tmp_path / "repo"
    project = source_ref / "project"
    project.mkdir(parents=True)
    (project / "target.txt").write_text("hello\n", encoding="utf-8")
    (project / "link.txt").symlink_to(project / "target.txt")

    staged = WorkflowRunner(config=_config(), source_ref=source_ref).stage(
        tmp_path / "stage",
    )

    assert (staged.root / "link.txt").is_symlink()
    assert [entry.destination for entry in staged.files] == ["link.txt", "target.txt"]


def test_workflow_runner_copies_extra_file_from_repo_root(tmp_path: Path):
    source_ref = tmp_path / "repo"
    (source_ref / "project").mkdir(parents=True)
    (source_ref / "project" / "app.py").write_text("app\n", encoding="utf-8")
    (source_ref / "shared/lib").mkdir(parents=True)
    (source_ref / "shared/lib/json.py").write_text("lib\n", encoding="utf-8")

    staged = WorkflowRunner(
        config=_config(
            copies=(
                FileCopy(
                    source="shared/lib/json.py",
                    destination="project/lib/json.py",
                ),
            ),
        ),
        source_ref=source_ref,
    ).stage(tmp_path / "stage")

    assert (staged.root / "project/lib/json.py").read_text(encoding="utf-8") == "lib\n"
    assert [(entry.source, entry.destination) for entry in staged.files] == [
        ("project/app.py", "app.py"),
        ("shared/lib/json.py", "project/lib/json.py"),
    ]


def test_workflow_runner_copies_extra_directory_with_filters(tmp_path: Path):
    source_ref = tmp_path / "repo"
    (source_ref / "project").mkdir(parents=True)
    (source_ref / "project" / "app.py").write_text("app\n", encoding="utf-8")
    (source_ref / "shared/lib/web").mkdir(parents=True)
    (source_ref / "shared/lib/web/search.py").write_text("search\n", encoding="utf-8")
    (source_ref / "shared/lib/web/search_test.py").write_text(
        "test\n",
        encoding="utf-8",
    )

    staged = WorkflowRunner(
        config=_config(
            copies=(
                FileCopy(
                    source="shared/lib/web",
                    destination="project/lib/web",
                    include=("*.py",),
                    exclude=("*_test.py",),
                ),
            ),
        ),
        source_ref=source_ref,
    ).stage(tmp_path / "stage")

    assert (staged.root / "project/lib/web/search.py").is_file()
    assert not (staged.root / "project/lib/web/search_test.py").exists()
    assert ("shared/lib/web/search.py", "project/lib/web/search.py") in (
        (entry.source, entry.destination) for entry in staged.files
    )


def test_workflow_runner_writes_generated_file(tmp_path: Path):
    source_ref = tmp_path / "repo"
    (source_ref / "project").mkdir(parents=True)

    staged = WorkflowRunner(
        config=_config(
            writes=(
                FileWrite(
                    path="project/lib/web/__init__.py",
                    content='"""Web helpers."""\n',
                ),
            ),
        ),
        source_ref=source_ref,
    ).stage(tmp_path / "stage")

    assert (staged.root / "project/lib/web/__init__.py").read_text(
        encoding="utf-8",
    ) == '"""Web helpers."""\n'
    assert [(entry.source, entry.destination) for entry in staged.files] == [
        ("<generated:project/lib/web/__init__.py>", "project/lib/web/__init__.py"),
    ]


def test_workflow_runner_reports_a_malformed_delta(tmp_path: Path):
    source_ref = tmp_path / "repo"
    (source_ref / "project").mkdir(parents=True)
    (source_ref / "pyproject.toml").write_text("a = 1\n", encoding="utf-8")
    (source_ref / "project/pyproject.delta.toml").write_text(
        'b = [\n[pyproject]\nbase = "pyproject.toml"\n',
        encoding="utf-8",
    )
    config = replace(
        _config(),
        files=replace(_config().files, pyproject="project/pyproject.delta.toml"),
    )

    with pytest.raises(ExportError, match=r"Cannot render pyproject\.toml"):
        WorkflowRunner(config=config, source_ref=source_ref).stage(tmp_path / "stage")


def test_workflow_runner_renders_pyproject_from_its_delta(tmp_path: Path):
    source_ref = tmp_path / "repo"
    (source_ref / "project").mkdir(parents=True)
    (source_ref / "pyproject.toml").write_text('a = ["x"]\nb = 1\n', encoding="utf-8")
    (source_ref / "project/pyproject.delta.toml").write_text(
        '"+a" = ["y"]\nb = 2\n\n[pyproject]\nbase = "pyproject.toml"\n',
        encoding="utf-8",
    )

    staged = WorkflowRunner(
        config=replace(
            _config(),
            files=replace(
                _config().files,
                exclude=("pyproject.delta.toml",),
                pyproject="project/pyproject.delta.toml",
            ),
        ),
        source_ref=source_ref,
    ).stage(tmp_path / "stage")

    assert (staged.root / "pyproject.toml").read_text(encoding="utf-8") == (
        'a = ["x", "y"]\nb = 2\n'
    )
    assert ("project/pyproject.delta.toml", "pyproject.toml") in (
        (entry.source, entry.destination) for entry in staged.files
    )


@pytest.mark.parametrize("copy_source", ["project/.export", "./project/.export"])
@pytest.mark.parametrize("exclude", [(), (".export/**",)])
@pytest.mark.parametrize(
    "spelling",
    [
        "project/.export/pyproject.delta.toml",
        "./project/.export/pyproject.delta.toml",
        "project//.export/pyproject.delta.toml",
        "project/./.export/pyproject.delta.toml",
    ],
)
def test_workflow_runner_never_ships_the_delta(
    tmp_path: Path,
    exclude: tuple[str, ...],
    spelling: str,
    copy_source: str,
):
    """The delta is an input to the render: no selection or copy may ship it."""
    source_ref = tmp_path / "repo"
    (source_ref / "project/.export").mkdir(parents=True)
    (source_ref / "pyproject.toml").write_text("a = 1\n", encoding="utf-8")
    (source_ref / "project/.export/pyproject.delta.toml").write_text(
        'a = 2\n\n[pyproject]\nbase = "pyproject.toml"\n',
        encoding="utf-8",
    )
    (source_ref / "project/.export/SECURITY.md").write_text("s\n", encoding="utf-8")
    config = _config(
        copies=(FileCopy(source="project/.export", destination="."),),
    )
    config = replace(
        config,
        files=replace(
            config.files,
            exclude=exclude,
        ),
    )
    raw: dict[str, object] = {
        "workflow": {"source_root": "project"},
        "files": {
            "include": ["**"],
            "exclude": list(exclude),
            "copy": [{"source": copy_source, "destination": "."}],
            "pyproject": {"delta": spelling},
        },
    }
    config = replace(config, files=parse_config(raw).files)

    staged = WorkflowRunner(config=config, source_ref=source_ref).stage(
        tmp_path / "stage",
    )

    shipped = {PurePosixPath(entry.destination).as_posix() for entry in staged.files}
    assert {"pyproject.toml", "SECURITY.md"} <= shipped
    assert not any(path.endswith("pyproject.delta.toml") for path in shipped)
    assert not list(staged.root.rglob("pyproject.delta.toml"))


def test_workflow_runner_rejects_extra_copy_collision(tmp_path: Path):
    source_ref = tmp_path / "repo"
    (source_ref / "project").mkdir(parents=True)
    (source_ref / "project" / "app.py").write_text("app\n", encoding="utf-8")
    (source_ref / "shared/lib").mkdir(parents=True)
    (source_ref / "shared/lib/app.py").write_text("lib\n", encoding="utf-8")

    with pytest.raises(ExportError, match="already exists"):
        WorkflowRunner(
            config=_config(
                copies=(FileCopy(source="shared/lib/app.py", destination="app.py"),),
            ),
            source_ref=source_ref,
        ).stage(tmp_path / "stage")


def test_workflow_runner_rejects_missing_extra_copy_source(tmp_path: Path):
    source_ref = tmp_path / "repo"
    (source_ref / "project").mkdir(parents=True)

    with pytest.raises(ExportError, match="does not exist"):
        WorkflowRunner(
            config=_config(
                copies=(
                    FileCopy(
                        source="shared/lib/missing.py",
                        destination="project/lib/missing.py",
                    ),
                ),
            ),
            source_ref=source_ref,
        ).stage(tmp_path / "stage")


def test_workflow_runner_rejects_generated_file_collision(tmp_path: Path):
    source_ref = tmp_path / "repo"
    (source_ref / "project").mkdir(parents=True)
    (source_ref / "project" / "app.py").write_text("app\n", encoding="utf-8")

    with pytest.raises(ExportError, match="already exists"):
        WorkflowRunner(
            config=_config(writes=(FileWrite(path="app.py", content="generated\n"),)),
            source_ref=source_ref,
        ).stage(tmp_path / "stage")


def test_workflow_runner_manifest_tracks_moved_directory(tmp_path: Path):
    source_ref = tmp_path / "repo"
    (source_ref / "project/_stubs/pkg").mkdir(parents=True)
    (source_ref / "project/_stubs/pkg/__init__.py").write_text(
        "value = 1\n",
        encoding="utf-8",
    )

    staged = WorkflowRunner(
        config=_config(
            transforms=(
                Transform(
                    id="move-stub",
                    type="move",
                    path="_stubs/pkg",
                    destination="pkg",
                ),
            ),
        ),
        source_ref=source_ref,
    ).stage(tmp_path / "stage")

    assert not (staged.root / "_stubs/pkg/__init__.py").exists()
    assert (staged.root / "pkg/__init__.py").is_file()
    assert [(entry.source, entry.destination) for entry in staged.files] == [
        ("project/_stubs/pkg/__init__.py", "pkg/__init__.py"),
    ]


@pytest.mark.parametrize(
    ("rel", "destination"),
    [
        ("README.md", "docs/README.md"),
        ("pkg/a.py", "lib/pkg/a.py"),
        ("pkgs/a.py", "pkgs/a.py"),
        ("other.py", "other.py"),
    ],
)
def test_move_sequence_relocates_an_exact_path_or_the_tree_under_it(
    rel: str,
    destination: str,
):
    moves = MoveSequence(
        moves=(
            FileMove(path="README.md", destination="docs/README.md"),
            FileMove(path="pkg", destination="lib/pkg"),
        ),
    )

    assert moves.destination_path(rel) == destination


def test_workflow_runner_rejects_a_directory_copy_over_a_selected_file(tmp_path: Path):
    source_ref = tmp_path / "repo"
    (source_ref / "project/sub").mkdir(parents=True)
    (source_ref / "project/sub/README.md").write_text("project\n", encoding="utf-8")
    (source_ref / "shared").mkdir()
    (source_ref / "shared/README.md").write_text("shared\n", encoding="utf-8")

    with pytest.raises(
        ExportError,
        match=re.escape(
            "Export destination already exists: sub/README.md from shared/README.md",
        ),
    ):
        WorkflowRunner(
            config=_config(copies=(FileCopy(source="shared", destination="sub"),)),
            source_ref=source_ref,
        ).stage(tmp_path / "stage")


def test_workflow_runner_rejects_a_copy_source_linked_outside_the_checkout(
    tmp_path: Path,
):
    source_ref = tmp_path / "repo"
    (source_ref / "project").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "x.py").write_text("x = 1\n", encoding="utf-8")
    (source_ref / "shared").symlink_to(outside)

    with pytest.raises(
        ExportError,
        match="Copied source escapes source checkout: shared",
    ):
        WorkflowRunner(
            config=_config(copies=(FileCopy(source="shared", destination="lib"),)),
            source_ref=source_ref,
        ).stage(tmp_path / "stage")


def test_workflow_runner_skips_a_file_copy_its_own_filter_excludes(tmp_path: Path):
    source_ref = tmp_path / "repo"
    (source_ref / "project").mkdir(parents=True)
    (source_ref / "project/app.py").write_text("app\n", encoding="utf-8")
    (source_ref / "shared").mkdir()
    (source_ref / "shared/notes.txt").write_text("notes\n", encoding="utf-8")

    staged = WorkflowRunner(
        config=_config(
            copies=(
                FileCopy(
                    source="shared/notes.txt",
                    destination="notes.txt",
                    exclude=("*.txt",),
                ),
            ),
        ),
        source_ref=source_ref,
    ).stage(tmp_path / "stage")

    assert not (staged.root / "notes.txt").exists()
    assert [entry.destination for entry in staged.files] == ["app.py"]


def test_workflow_runner_rejects_a_selected_pyproject_beside_its_delta(
    tmp_path: Path,
):
    source_ref = tmp_path / "repo"
    (source_ref / "project").mkdir(parents=True)
    (source_ref / "pyproject.toml").write_text("b = 1\n", encoding="utf-8")
    (source_ref / "project/pyproject.toml").write_text("b = 3\n", encoding="utf-8")
    (source_ref / "project/pyproject.delta.toml").write_text(
        'b = 2\n\n[pyproject]\nbase = "pyproject.toml"\n',
        encoding="utf-8",
    )

    with pytest.raises(
        ExportError,
        match=re.escape(
            "Export destination already exists: pyproject.toml from "
            "project/pyproject.delta.toml",
        ),
    ):
        WorkflowRunner(
            config=replace(
                _config(),
                files=replace(
                    _config().files,
                    exclude=("pyproject.delta.toml",),
                    pyproject="project/pyproject.delta.toml",
                ),
            ),
            source_ref=source_ref,
        ).stage(tmp_path / "stage")


def test_workflow_runner_reports_how_long_each_phase_took(tmp_path: Path):
    source_ref = tmp_path / "repo"
    (source_ref / "project").mkdir(parents=True)
    (source_ref / "project/app.py").write_text("app\n", encoding="utf-8")
    phases: list[tuple[str, float]] = []

    def record(phase: str, elapsed_sec: float) -> None:
        phases.append((phase, elapsed_sec))

    WorkflowRunner(config=_config(), source_ref=source_ref).stage(
        tmp_path / "stage",
        record_phase=record,
    )

    assert {phase for phase, _ in phases} >= {
        "select",
        "copy",
        "transforms",
        "final_manifest",
    }
    assert all(elapsed_sec >= 0 for _, elapsed_sec in phases)


if __name__ == "__main__":
    from copybarista.lib.testing.runner import test_main

    test_main(__file__)
