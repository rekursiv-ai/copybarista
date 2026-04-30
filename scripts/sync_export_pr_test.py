"""Tests for source-to-public sync helpers."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts import sync_export_pr
from scripts.sync_export_pr import (
    _commit_author,
    _gh_pr_exists,
    _public_pr_text,
    _replace_tree,
    export_branch_name,
    export_pr_body,
)


def test_export_pr_body_contains_review_context():
    body = export_pr_body(
        description="Publish the latest sync changes.",
        branch="copybarista/export/main",
        source_sha="abcdef1234567890",
        workflow="Copybarista Export",
        file_count=58,
    )

    assert "Publish the latest sync changes." in body
    assert "- Export branch: `copybarista/export/main`" in body
    assert "- Source commit: `abcdef1234567890`" in body
    assert "- Exported files: `58`" in body
    assert "- Workflow: `Copybarista Export`" in body
    assert "Do not push manual commits to this generated branch." in body


def test_public_pr_text_rejects_private_source_names():
    private_name = "Lo" + "op"

    with pytest.raises(SystemExit) as error:
        _public_pr_text(
            value=f"Publish changes from {private_name}",
            name="--pr-title",
            forbidden_text=(private_name,),
        )

    assert error.value.code == 2


def test_public_pr_text_accepts_reviewable_summary():
    assert (
        _public_pr_text(
            value="Prepare public repository updates",
            name="--pr-title",
            forbidden_text=("private-source",),
        )
        == "Prepare public repository updates"
    )


def test_export_branch_name_uses_source_branch():
    assert (
        export_branch_name(explicit="", source_branch="main", source_sha="abcdef")
        == "copybarista/export/main"
    )


def test_export_branch_name_allows_explicit_branch():
    assert (
        export_branch_name(
            explicit="copybarista/export/custom",
            source_branch="main",
            source_sha="abcdef",
        )
        == "copybarista/export/custom"
    )


def test_commit_author_uses_sync_identity():
    assert (
        _commit_author("copybarista", "copybarista@rekursiv.ai")
        == "copybarista <copybarista@rekursiv.ai>"
    )


def test_gh_pr_exists_only_counts_open_prs(monkeypatch: pytest.MonkeyPatch):
    def fake_run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        assert argv[0:4] == ["gh", "pr", "list", "--repo"]
        assert "--state" in argv
        assert "open" in argv
        return subprocess.CompletedProcess(argv, 0, stdout="[]")

    monkeypatch.setattr(sync_export_pr, "_run", fake_run)

    assert not _gh_pr_exists(
        branch="copybarista/export/main",
        repo="rekursiv-ai/copybarista",
        cwd=Path.cwd(),
    )


def test_replace_tree_preserves_git_and_removes_stale_files(tmp_path: Path):
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    (source / ".github/workflows").mkdir(parents=True)
    (source / ".github/workflows/ci.yml").write_text(
        "name: CI\n",
        encoding="utf-8",
    )
    (source / "pkg").mkdir(parents=True)
    (source / "pkg/module.py").write_text("new\n", encoding="utf-8")
    (destination / ".git").mkdir(parents=True)
    (destination / ".git/HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (destination / ".github/workflows").mkdir(parents=True)
    (destination / ".github/workflows/import.yml").write_text(
        "name: Import\n",
        encoding="utf-8",
    )
    (destination / "stale.txt").write_text("old\n", encoding="utf-8")
    (destination / "pkg").mkdir()
    (destination / "pkg/old.py").write_text("old\n", encoding="utf-8")

    _replace_tree(source=source, destination=destination)

    assert (destination / ".git/HEAD").read_text(encoding="utf-8")
    assert (destination / ".github/workflows/import.yml").read_text(encoding="utf-8")
    assert (destination / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert not (destination / "stale.txt").exists()
    assert not (destination / "pkg/old.py").exists()
    assert (destination / "pkg/module.py").read_text(encoding="utf-8") == "new\n"
