"""Tests for public-to-source sync helpers."""

from __future__ import annotations

from pathlib import Path

import subprocess

import pytest

from scripts import sync_import_change
from scripts.sync_import_change import (
    ImportRequest,
    _commit_author,
    _gh_pr_exists,
    _run_import_change,
    _string_bool,
    _validate_target,
    import_branch_name,
    import_change_pr_body,
)


def test_main_accepts_generic_project_validation_args(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[ImportRequest] = []

    def fake_run_import_sync(request: ImportRequest) -> None:
        captured.append(request)

    monkeypatch.setattr(sync_import_change, "run_import_sync", fake_run_import_sync)

    sync_import_change.main(
        [
            "--project-path",
            "packages/configgle",
            "--copybarista-project-path",
            "tools/copybarista",
            "--public-base-ref",
            "base",
            "--public-head-ref",
            "head",
            "--type-check-target",
            "configgle",
            "--type-check-target",
            "tests",
        ]
    )

    assert captured[0].project_path == Path("packages/configgle")
    assert captured[0].copybarista_project_path == Path("tools/copybarista")
    assert captured[0].type_check_targets == ("configgle", "tests")
    assert not captured[0].refresh_public_lockfile


def test_main_accepts_refresh_public_lockfile_arg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[ImportRequest] = []

    def fake_run_import_sync(request: ImportRequest) -> None:
        captured.append(request)

    monkeypatch.setattr(sync_import_change, "run_import_sync", fake_run_import_sync)

    sync_import_change.main(
        [
            "--project-path",
            "packages/configgle",
            "--public-base-ref",
            "base",
            "--public-head-ref",
            "head",
            "--refresh-public-lockfile",
        ]
    )

    assert captured[0].refresh_public_lockfile


def test_import_change_ignores_generated_public_lockfile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner_temp = tmp_path / "runner"
    runner_temp.mkdir()
    public_base = tmp_path / "public-base"
    public_head = tmp_path / "public-head"
    target = tmp_path / "target"
    project = target / "package"
    for root in (public_base, public_head, project, target / "tools" / "copybarista"):
        root.mkdir(parents=True)
    (public_base / "module.py").write_text("base\n", encoding="utf-8")
    (public_base / "uv.lock").write_text("base lock\n", encoding="utf-8")
    (public_head / "module.py").write_text("head\n", encoding="utf-8")
    (public_head / "uv.lock").write_text("head lock\n", encoding="utf-8")
    (project / "copy.barista.toml").write_text("[workflow]\n", encoding="utf-8")
    calls: list[list[str]] = []

    def fake_run(
        argv: list[str],
        **_: object,
    ) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        sanitized_base = Path(argv[argv.index("--public-base") + 1])
        sanitized_head = Path(argv[argv.index("--public-head") + 1])
        assert sanitized_base != public_base
        assert sanitized_head != public_head
        assert (sanitized_base / "module.py").read_text(encoding="utf-8") == "base\n"
        assert (sanitized_head / "module.py").read_text(encoding="utf-8") == "head\n"
        assert not (sanitized_base / "uv.lock").exists()
        assert not (sanitized_head / "uv.lock").exists()
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(sync_import_change, "_run", fake_run)

    _run_import_change(
        request=ImportRequest(
            public_base=public_base,
            public_head=public_head,
            target_dir=target,
            target_repo="rekursiv-ai/source",
            project_path=Path("package"),
            copybarista_project_path=Path("tools/copybarista"),
            base_branch="main",
            public_repo="rekursiv-ai/public",
            public_sha="abcdef123456",
            public_base_ref="base",
            public_head_ref="head",
            branch="copybarista/import/sha-abcdef123456",
            sync_label="Package",
            sync_user_name="copybarista",
            sync_user_email="copybarista@example.com",
            report=tmp_path / "report.json",
            open_pr=False,
            open_pr_only=False,
            runner_temp=runner_temp,
            type_check_targets=(".",),
            refresh_public_lockfile=True,
        ),
        project=project,
    )

    assert len(calls) == 1


def test_run_import_sync_prepares_copybarista_tool_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    target = tmp_path / "target"

    def fake_run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0)

    def fake_import_change(*, request: ImportRequest, project: Path) -> None:
        calls.append(["import", str(project), str(request.target_dir)])

    def fake_validate_target(
        *,
        project: Path,
        type_check_targets: tuple[str, ...],
    ) -> None:
        calls.append(["validate", str(project), *type_check_targets])

    monkeypatch.setattr(sync_import_change, "_run", fake_run)
    monkeypatch.setattr(sync_import_change, "_run_import_change", fake_import_change)
    monkeypatch.setattr(sync_import_change, "_validate_target", fake_validate_target)

    sync_import_change.run_import_sync(
        ImportRequest(
            public_base=tmp_path / "public-base",
            public_head=tmp_path / "public-head",
            target_dir=target,
            target_repo="rekursiv-ai/source",
            project_path=Path("package"),
            copybarista_project_path=Path("tools/copybarista"),
            base_branch="main",
            public_repo="rekursiv-ai/public",
            public_sha="abcdef123456",
            public_base_ref="base",
            public_head_ref="head",
            branch="copybarista/import/sha-abcdef123456",
            sync_label="Package",
            sync_user_name="copybarista",
            sync_user_email="copybarista@example.com",
            report=tmp_path / "report.json",
            open_pr=False,
            open_pr_only=False,
            runner_temp=tmp_path,
            type_check_targets=(".",),
            refresh_public_lockfile=False,
        )
    )

    assert calls[0] == [
        "uv",
        "--quiet",
        "--project",
        str(target / "tools" / "copybarista"),
        "sync",
        "--all-groups",
    ]
    assert calls[1] == [
        "uv",
        "--quiet",
        "--project",
        str(target / "package"),
        "sync",
        "--all-groups",
    ]


def test_import_change_pr_body_contains_review_context():
    body = import_change_pr_body(
        public_repo="rekursiv-ai/copybarista",
        public_sha="abcdef1234567890",
        public_base_ref="base",
        public_head_ref="head",
        source_base_ref="source",
        sync_label="Copybarista",
    )

    assert "Imports Copybarista public repository changes into the source" in body
    assert "- Public repository: `rekursiv-ai/copybarista`" in body
    assert "- Public SHA: `abcdef1234567890`" in body
    assert "- Public base: `base`" in body
    assert "- Public head: `head`" in body
    assert "- Source base: `source`" in body
    assert "`copybarista import-change`" in body
    assert "Regenerate this PR before merging if source `main` changes." in body


def test_import_change_pr_body_accepts_custom_sync_label():
    body = import_change_pr_body(
        public_repo="rekursiv-ai/configgle",
        public_sha="abcdef1234567890",
        public_base_ref="base",
        public_head_ref="head",
        source_base_ref="source",
        sync_label="Configgle",
    )

    assert "Imports Configgle public repository changes into the source" in body


def test_string_bool_accepts_action_boolean_values():
    assert _string_bool("true")
    assert _string_bool("1")
    assert _string_bool("yes")
    assert not _string_bool("false")
    assert not _string_bool("")


def test_import_branch_name_uses_public_sha():
    assert (
        import_branch_name(
            explicit="",
            public_sha="abcdef1234567890",
            prefix="copybarista/import/",
        )
        == "copybarista/import/sha-abcdef123456"
    )


def test_import_branch_name_allows_explicit_branch():
    assert (
        import_branch_name(
            explicit="copybarista/import/custom",
            public_sha="abcdef1234567890",
            prefix="copybarista/import/",
        )
        == "copybarista/import/custom"
    )


def test_import_branch_name_rejects_non_generated_explicit_branch():
    with pytest.raises(SystemExit) as error:
        import_branch_name(
            explicit="main",
            public_sha="abcdef1234567890",
            prefix="copybarista/import/",
        )

    assert error.value.code == 2


def test_import_branch_name_rejects_malformed_explicit_branch():
    with pytest.raises(SystemExit) as error:
        import_branch_name(
            explicit="copybarista/import/../main",
            public_sha="abcdef1234567890",
            prefix="copybarista/import/",
        )

    assert error.value.code == 2


def test_commit_author_uses_sync_identity():
    assert (
        _commit_author("copybarista", "copybarista@example.com")
        == "copybarista <copybarista@example.com>"
    )


def test_gh_pr_exists_only_counts_open_prs(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        assert argv[0:4] == ["gh", "pr", "list", "--repo"]
        assert "--state" in argv
        assert "open" in argv
        return subprocess.CompletedProcess(argv, 0, stdout="[]")

    monkeypatch.setattr(sync_import_change, "_run", fake_run)

    assert not _gh_pr_exists(
        branch="copybarista/import/sha-abcdef123456",
        repo="rekursiv-ai/source",
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

    monkeypatch.setattr(sync_import_change, "_run", fake_run)
    monkeypatch.setattr("time.sleep", no_sleep)

    assert not _gh_pr_exists(
        branch="copybarista/import/sha-abcdef123456",
        repo="rekursiv-ai/source",
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

    monkeypatch.setattr(sync_import_change, "_run", fake_run)
    monkeypatch.setattr("time.sleep", no_sleep)

    with pytest.raises(SystemExit) as error:
        _gh_pr_exists(
            branch="copybarista/import/sha-abcdef123456",
            repo="rekursiv-ai/source",
            cwd=Path.cwd(),
        )

    assert error.value.code == 1
    assert calls == sync_import_change.GITHUB_RETRY_ATTEMPTS
    assert "HTTP 504" in capsys.readouterr().err


def test_validate_target_runs_ty_check(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0)

    def fake_basedpyright(*, project: Path, targets: tuple[str, ...]) -> None:
        calls.append(["basedpyright", str(project), *targets])

    monkeypatch.setattr(sync_import_change, "_run", fake_run)
    monkeypatch.setattr(sync_import_change, "_run_basedpyright", fake_basedpyright)

    _validate_target(project=Path("/repo/pkg"), type_check_targets=("configgle",))

    assert [
        "uv",
        "--quiet",
        "--project",
        "/repo/pkg",
        "run",
        "ty",
        "check",
    ] in calls
