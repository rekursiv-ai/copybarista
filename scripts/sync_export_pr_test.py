"""Tests for source-to-public sync helpers."""

from __future__ import annotations

from pathlib import Path

import subprocess

import pytest

from scripts import sync_export_pr
from scripts.sync_export_pr import (
    ExportRequest,
    _commit_author,
    _gh_pr_exists,
    _public_pr_text,
    _replace_tree,
    export_branch_name,
    export_pr_body,
    export_pr_text,
)


def test_main_accepts_generic_project_validation_args(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[ExportRequest] = []

    def fake_run_export_sync(request: ExportRequest) -> None:
        captured.append(request)

    monkeypatch.setattr(sync_export_pr, "run_export_sync", fake_run_export_sync)

    sync_export_pr.main(
        [
            "--project-path",
            "loop/lib/configgle",
            "--release-check-script",
            "scripts/check_release_tree.py",
            "--type-check-target",
            "configgle",
            "--type-check-target",
            "tests",
            "--smoke-import",
            "configgle",
        ]
    )

    assert captured[0].project_path == Path("loop/lib/configgle")
    assert captured[0].release_check_script == Path("scripts/check_release_tree.py")
    assert captured[0].type_check_targets == ("configgle", "tests")
    assert captured[0].smoke_import == "configgle"


def test_export_pr_body_contains_review_context():
    body = export_pr_body(
        description="Publish the latest sync changes.",
        branch="copybarista/export/main",
        sync_label="Copybarista",
    )

    assert "Publish the latest sync changes." in body
    assert "Copybarista export branch: `copybarista/export/main`" in body
    assert "Source commit" not in body
    assert "Exported files" not in body
    assert "Workflow:" not in body
    assert "Do not push manual commits to this generated branch." in body


def test_export_pr_body_accepts_custom_sync_label():
    body = export_pr_body(
        description="Publish the latest sync changes.",
        branch="configgle/export/main",
        sync_label="Configgle",
    )

    assert "Configgle export branch: `configgle/export/main`" in body


def test_export_pr_text_uses_defaults_without_commit_message_opt_in():
    text = export_pr_text(
        title="",
        body="",
        source_message="Improve README image\n\nRestore the exported mascot asset.",
        use_source_message=False,
        forbidden_text=("private-source",),
    )

    assert text.title == "Update public export"
    assert text.body == "Updates the generated public repository export."


def test_export_pr_text_can_use_commit_title_and_description():
    text = export_pr_text(
        title="",
        body="",
        source_message="Improve README image\n\nRestore the exported mascot asset.",
        use_source_message=True,
        forbidden_text=("private-source",),
    )

    assert text.title == "Improve README image"
    assert text.body == "Restore the exported mascot asset."


def test_export_pr_text_accepts_manual_title_and_body():
    text = export_pr_text(
        title="Public sync update",
        body="Prepare release docs.",
        source_message="Private implementation detail",
        use_source_message=True,
        forbidden_text=("private",),
    )

    assert text.title == "Public sync update"
    assert text.body == "Prepare release docs."


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
        export_branch_name(
            explicit="",
            source_branch="main",
            source_sha="abcdef",
            prefix="copybarista/export/",
        )
        == "copybarista/export/main"
    )


def test_export_branch_name_allows_explicit_branch():
    assert (
        export_branch_name(
            explicit="copybarista/export/custom",
            source_branch="main",
            source_sha="abcdef",
            prefix="copybarista/export/",
        )
        == "copybarista/export/custom"
    )


def test_export_branch_name_rejects_non_generated_explicit_branch():
    with pytest.raises(SystemExit) as error:
        export_branch_name(
            explicit="main",
            source_branch="main",
            source_sha="abcdef",
            prefix="copybarista/export/",
        )

    assert error.value.code == 2


def test_export_branch_name_rejects_malformed_explicit_branch():
    with pytest.raises(SystemExit) as error:
        export_branch_name(
            explicit="copybarista/export/../main",
            source_branch="main",
            source_sha="abcdef",
            prefix="copybarista/export/",
        )

    assert error.value.code == 2


def test_commit_author_uses_sync_identity():
    assert (
        _commit_author("copybarista", "copybarista@example.com")
        == "copybarista <copybarista@example.com>"
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


def test_gh_pr_exists_retries_transient_github_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def fake_run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        if calls == 1:
            return subprocess.CompletedProcess(
                argv,
                1,
                stdout="",
                stderr="HTTP 504: try resubmitting your request",
            )
        return subprocess.CompletedProcess(argv, 0, stdout="[]", stderr="")

    def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(sync_export_pr, "_run", fake_run)
    monkeypatch.setattr(sync_export_pr.time, "sleep", no_sleep)

    assert not _gh_pr_exists(
        branch="copybarista/export/main",
        repo="rekursiv-ai/copybarista",
        cwd=Path.cwd(),
    )
    assert calls == 2


def test_gh_pr_exists_fails_loudly_after_retry_limit(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls = 0

    def fake_run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        return subprocess.CompletedProcess(
            argv,
            1,
            stdout="",
            stderr="HTTP 504: try resubmitting your request",
        )

    def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(sync_export_pr, "_run", fake_run)
    monkeypatch.setattr(sync_export_pr.time, "sleep", no_sleep)

    with pytest.raises(SystemExit) as error:
        _gh_pr_exists(
            branch="copybarista/export/main",
            repo="rekursiv-ai/copybarista",
            cwd=Path.cwd(),
        )

    assert error.value.code == 1
    assert calls == sync_export_pr.GITHUB_RETRY_ATTEMPTS
    assert "HTTP 504" in capsys.readouterr().err


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
