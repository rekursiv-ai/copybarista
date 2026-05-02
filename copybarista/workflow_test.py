"""Tests for workflow staging edge cases."""

from __future__ import annotations

from pathlib import Path

import pytest

from copybarista.config import (
    FileCopy,
    FileSelection,
    FolderDestination,
    GitDestination,
    Transform,
    WorkflowConfig,
)
from copybarista.errors import ExportError
from copybarista.workflow import WorkflowRunner


def _config(
    source_root: str = "project",
    copies: tuple[FileCopy, ...] = (),
    transforms: tuple[Transform, ...] = (),
) -> WorkflowConfig:
    return WorkflowConfig(
        name="demo",
        mode="squash",
        source_root=source_root,
        files=FileSelection(include=("**",), exclude=(), copy=copies),
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
        tmp_path / "stage"
    )

    assert staged.files == ()
    assert staged.transforms == ()


def test_workflow_runner_keeps_root_sources_relative(tmp_path: Path):
    source_ref = tmp_path / "repo"
    source_ref.mkdir()
    (source_ref / "README.md").write_text("hello\n", encoding="utf-8")

    staged = WorkflowRunner(
        config=_config(source_root=""), source_ref=source_ref
    ).stage(tmp_path / "stage")

    assert [(entry.source, entry.destination) for entry in staged.files] == [
        ("README.md", "README.md")
    ]


def test_workflow_runner_rejects_symlink_outside_source_root(tmp_path: Path):
    source_ref = tmp_path / "repo"
    project = source_ref / "project"
    project.mkdir(parents=True)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret\n", encoding="utf-8")
    (project / "leak.txt").symlink_to(outside)

    with pytest.raises(ExportError, match="Symlink points outside"):
        WorkflowRunner(config=_config(), source_ref=source_ref).stage(
            tmp_path / "stage"
        )


def test_workflow_runner_rejects_source_root_symlink_escape(tmp_path: Path):
    source_ref = tmp_path / "repo"
    outside = tmp_path / "outside"
    outside.mkdir()
    source_ref.mkdir()
    (source_ref / "project").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ExportError, match="escapes source checkout"):
        WorkflowRunner(config=_config(), source_ref=source_ref).stage(
            tmp_path / "stage"
        )


def test_workflow_runner_preserves_internal_symlink(tmp_path: Path):
    source_ref = tmp_path / "repo"
    project = source_ref / "project"
    project.mkdir(parents=True)
    (project / "target.txt").write_text("hello\n", encoding="utf-8")
    (project / "link.txt").symlink_to(project / "target.txt")

    staged = WorkflowRunner(config=_config(), source_ref=source_ref).stage(
        tmp_path / "stage"
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
            )
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
        "test\n", encoding="utf-8"
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
            )
        ),
        source_ref=source_ref,
    ).stage(tmp_path / "stage")

    assert (staged.root / "project/lib/web/search.py").is_file()
    assert not (staged.root / "project/lib/web/search_test.py").exists()
    assert ("shared/lib/web/search.py", "project/lib/web/search.py") in (
        (entry.source, entry.destination) for entry in staged.files
    )


def test_workflow_runner_rejects_extra_copy_collision(tmp_path: Path):
    source_ref = tmp_path / "repo"
    (source_ref / "project").mkdir(parents=True)
    (source_ref / "project" / "app.py").write_text("app\n", encoding="utf-8")
    (source_ref / "shared/lib").mkdir(parents=True)
    (source_ref / "shared/lib/app.py").write_text("lib\n", encoding="utf-8")

    with pytest.raises(ExportError, match="already exists"):
        WorkflowRunner(
            config=_config(
                copies=(FileCopy(source="shared/lib/app.py", destination="app.py"),)
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
                )
            ),
            source_ref=source_ref,
        ).stage(tmp_path / "stage")


def test_workflow_runner_manifest_tracks_moved_directory(tmp_path: Path):
    source_ref = tmp_path / "repo"
    (source_ref / "project/_stubs/pkg").mkdir(parents=True)
    (source_ref / "project/_stubs/pkg/__init__.py").write_text(
        "value = 1\n", encoding="utf-8"
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
            )
        ),
        source_ref=source_ref,
    ).stage(tmp_path / "stage")

    assert not (staged.root / "_stubs/pkg/__init__.py").exists()
    assert (staged.root / "pkg/__init__.py").is_file()
    assert [(entry.source, entry.destination) for entry in staged.files] == [
        ("project/_stubs/pkg/__init__.py", "pkg/__init__.py")
    ]
