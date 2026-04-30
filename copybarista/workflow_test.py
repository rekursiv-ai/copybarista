"""Tests for workflow staging edge cases."""

from __future__ import annotations

from pathlib import Path

import pytest

from copybarista.config import (
    FileSelection,
    FolderDestination,
    GitDestination,
    WorkflowConfig,
)
from copybarista.errors import ExportError
from copybarista.workflow import WorkflowRunner


def _config(source_root: str = "project") -> WorkflowConfig:
    return WorkflowConfig(
        name="demo",
        mode="squash",
        source_root=source_root,
        files=FileSelection(include=("**",), exclude=()),
        transforms=(),
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
