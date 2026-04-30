"""Tests for public release-tree policy checks."""

from __future__ import annotations

from pathlib import Path

from scripts.check_release_tree import check_tree


def _write_required_tree(root: Path) -> None:
    for path in (
        root / ".github/workflows/ci.yml",
        root / ".github/workflows/sync-to-source.yml",
        root / "LICENSE",
        root / "README.md",
        root / "copybarista/__init__.py",
        root / "pyproject.toml",
        root / "scripts/__init__.py",
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("ok\n", encoding="utf-8")


def test_check_tree_accepts_required_release_shape(tmp_path: Path):
    _write_required_tree(tmp_path)

    assert check_tree(root=tmp_path) == ()


def test_check_tree_rejects_private_generated_and_vcs_paths(tmp_path: Path):
    _write_required_tree(tmp_path)
    for path in (
        tmp_path / "private/SPEC.md",
        tmp_path / "site/index.html",
        tmp_path / "copy.bara.sky",
        tmp_path / "copy.barista.toml",
        tmp_path / ".github/workflows/pages.yml",
        tmp_path / ".pytest_cache/v/cache/nodeids",
        tmp_path / "copybarista/__pycache__/config.pyc",
        tmp_path / "copybarista.egg-info/PKG-INFO",
        tmp_path / "pkg/.git/HEAD",
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("bad\n", encoding="utf-8")

    errors = check_tree(root=tmp_path)

    assert any("Private implementation files" in error for error in errors)
    assert any("Generated directory" in error for error in errors)
    assert any("Python bytecode" in error for error in errors)
    assert any("Build metadata" in error for error in errors)
    assert any("VCS metadata" in error for error in errors)
    assert any("Source-only release file" in error for error in errors)


def test_check_tree_rejects_private_sync_readme_markers(tmp_path: Path):
    _write_required_tree(tmp_path)
    (tmp_path / "README.md").write_text(
        "<!-- copybarista:private-sync:start -->\n"
        "Private notes.\n"
        "<!-- copybarista:private-sync:end -->\n",
        encoding="utf-8",
    )

    errors = check_tree(root=tmp_path)

    assert any("Private sync README block" in error for error in errors)


def test_check_tree_allows_root_git_for_checked_out_public_repo(tmp_path: Path):
    _write_required_tree(tmp_path)
    (tmp_path / ".git/HEAD").parent.mkdir()
    (tmp_path / ".git/HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")

    assert any("VCS metadata" in error for error in check_tree(root=tmp_path))
    assert check_tree(root=tmp_path, allow_root_git=True) == ()


def test_check_tree_reports_missing_required_paths(tmp_path: Path):
    errors = check_tree(root=tmp_path)

    assert any(".github/workflows/ci.yml" in error for error in errors)
    assert any("pyproject.toml" in error for error in errors)
