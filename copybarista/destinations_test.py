"""Tests for destination publishing and safety checks."""

from __future__ import annotations

from pathlib import Path

import pytest

from copybarista.destinations import write_folder_destination
from copybarista.errors import ExportError
from copybarista.workflow import StagedTree


def _staged_tree(root: Path) -> StagedTree:
    root.mkdir()
    (root / "README.md").write_text("hello\n", encoding="utf-8")
    return StagedTree(root=root, files=(), transforms=())


def test_folder_destination_reports_created_and_updated(tmp_path: Path):
    source_ref = tmp_path / "repo"
    source_root = source_ref / "project"
    source_root.mkdir(parents=True)
    destination = tmp_path / "out"

    created = write_folder_destination(
        _staged_tree(tmp_path / "stage1"),
        destination=destination,
        source_ref=source_ref,
        source_root=source_root,
        replace_existing=True,
    )
    (destination / "stale.txt").write_text("stale\n", encoding="utf-8")
    updated = write_folder_destination(
        _staged_tree(tmp_path / "stage2"),
        destination=destination,
        source_ref=source_ref,
        source_root=source_root,
        replace_existing=True,
    )

    assert created.status == "created"
    assert created.ref == destination.as_posix()
    assert updated.status == "updated"
    assert sorted(path.name for path in destination.iterdir()) == ["README.md"]


def test_folder_destination_can_consume_staging(tmp_path: Path):
    source_ref = tmp_path / "repo"
    source_root = source_ref / "project"
    source_root.mkdir(parents=True)
    staging = tmp_path / "stage"
    destination = tmp_path / "out"

    result = write_folder_destination(
        _staged_tree(staging),
        destination=destination,
        source_ref=source_ref,
        source_root=source_root,
        replace_existing=True,
        consume_staging=True,
    )

    assert result.status == "created"
    assert (destination / "README.md").read_text(encoding="utf-8") == "hello\n"
    assert not staging.exists()


def test_folder_destination_rejects_source_root(tmp_path: Path):
    source_ref = tmp_path / "repo"
    source_root = source_ref / "project"
    source_root.mkdir(parents=True)

    with pytest.raises(ExportError, match="dangerous"):
        write_folder_destination(
            _staged_tree(tmp_path / "stage"),
            destination=source_root,
            source_ref=source_ref,
            source_root=source_root,
        )


def test_folder_destination_rejects_paths_inside_source_root(tmp_path: Path):
    source_ref = tmp_path / "repo"
    source_root = source_ref / "project"
    source_root.mkdir(parents=True)

    with pytest.raises(ExportError, match="inside source root"):
        write_folder_destination(
            _staged_tree(tmp_path / "stage"),
            destination=source_root / "out",
            source_ref=source_ref,
            source_root=source_root,
        )


def test_folder_destination_rejects_paths_inside_source_checkout(tmp_path: Path):
    source_ref = tmp_path / "repo"
    source_root = source_ref / "project"
    source_root.mkdir(parents=True)

    with pytest.raises(ExportError, match="inside source checkout"):
        write_folder_destination(
            _staged_tree(tmp_path / "stage"),
            destination=source_ref / "out",
            source_ref=source_ref,
            source_root=source_root,
        )


def test_folder_destination_rejects_destination_symlink(tmp_path: Path):
    source_ref = tmp_path / "repo"
    source_root = source_ref / "project"
    source_root.mkdir(parents=True)
    target = tmp_path / "target"
    target.mkdir()
    destination = tmp_path / "out"
    destination.symlink_to(target, target_is_directory=True)

    with pytest.raises(ExportError, match="symlink destination"):
        write_folder_destination(
            _staged_tree(tmp_path / "stage"),
            destination=destination,
            source_ref=source_ref,
            source_root=source_root,
            replace_existing=True,
        )

    assert target.is_dir()


def test_folder_destination_rejects_staged_symlink_escape(tmp_path: Path):
    source_ref = tmp_path / "repo"
    source_root = source_ref / "project"
    source_root.mkdir(parents=True)
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "escape").symlink_to("../outside.txt")

    with pytest.raises(ExportError, match="outside staged tree"):
        write_folder_destination(
            StagedTree(root=stage, files=(), transforms=()),
            destination=tmp_path / "out",
            source_ref=source_ref,
            source_root=source_root,
            replace_existing=True,
        )


def test_folder_destination_rejects_paths_inside_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    home = tmp_path / "home"
    home.mkdir()
    source_ref = tmp_path / "repo"
    source_root = source_ref / "project"
    source_root.mkdir(parents=True)
    destination = home / "out"
    destination.mkdir()
    monkeypatch.setenv("HOME", home.as_posix())

    with pytest.raises(ExportError, match="home directory"):
        write_folder_destination(
            _staged_tree(tmp_path / "stage"),
            destination=destination,
            source_ref=source_ref,
            source_root=source_root,
            replace_existing=True,
        )
