"""Tests for the development-artifact cleaner."""

from __future__ import annotations

from typing import TYPE_CHECKING

import sys

import pytest

from copybarista.scripts import clean


if TYPE_CHECKING:
    from pathlib import Path


def test_clean_removes_each_known_artifact_but_the_venv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _project(tmp_path, monkeypatch)
    (root / ".coverage").write_text("data\n", encoding="utf-8")
    (root / "build" / "lib").mkdir(parents=True)
    (root / ".venv").mkdir()
    (root / "README.md").write_text("keep\n", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["clean.py"])

    assert clean.main() == 0

    assert sorted(path.name for path in root.iterdir()) == [
        ".venv",
        "README.md",
        "scripts",
    ]


def test_clean_with_venv_removes_the_venv_too(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _project(tmp_path, monkeypatch)
    (root / ".venv" / "bin").mkdir(parents=True)
    monkeypatch.setattr(sys, "argv", ["clean.py", "--venv"])

    assert clean.main() == 0

    assert not (root / ".venv").exists()


def test_dry_run_prints_each_artifact_and_removes_none(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = _project(tmp_path, monkeypatch)
    (root / ".coverage").write_text("data\n", encoding="utf-8")
    (root / "dist").mkdir()
    monkeypatch.setattr(sys, "argv", ["clean.py", "--dry-run"])

    assert clean.main() == 0

    assert capsys.readouterr().out.splitlines() == [
        str(root / ".coverage"),
        str(root / "dist"),
    ]
    assert (root / ".coverage").is_file()
    assert (root / "dist").is_dir()


@pytest.mark.parametrize("pointee", ["keep", "../outside"])
def test_clean_unlinks_a_symlinked_artifact_and_keeps_what_it_points_to(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    pointee: str,
) -> None:
    root = _project(tmp_path, monkeypatch)
    (root / pointee).mkdir()
    (root / pointee / "data").write_text("keep\n", encoding="utf-8")
    (root / "dist").symlink_to(pointee)
    monkeypatch.setattr(sys, "argv", ["clean.py"])

    assert clean.main() == 0

    assert not (root / "dist").is_symlink()
    assert (root / pointee / "data").is_file()


def test_clean_refuses_an_artifact_whose_directory_leaves_the_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _project(tmp_path, monkeypatch)
    outside = tmp_path / "outside"
    (outside / "__pycache__").mkdir(parents=True)
    (root / "copybarista").symlink_to(outside)
    monkeypatch.setattr(sys, "argv", ["clean.py"])

    with pytest.raises(ValueError, match="outside project"):
        clean.main()

    assert (outside / "__pycache__").is_dir()


@pytest.mark.parametrize("target", ["..", "dist/.."])
def test_clean_refuses_a_target_that_climbs_out_of_its_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
) -> None:
    """Only the directory is resolved, so ``..`` must be refused as written."""
    _project(tmp_path, monkeypatch)

    with pytest.raises(ValueError, match="outside project"):
        clean._safe_project_path(target)


def _project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the cleaner at a fresh project root under ``tmp_path`` and return it."""
    root = tmp_path.resolve() / "project"
    (root / "scripts").mkdir(parents=True)
    monkeypatch.setattr(clean, "_CWD", root / "scripts")
    return root


if __name__ == "__main__":
    from copybarista.lib.testing.runner import test_main

    test_main(__file__)
