"""Tests for public-to-source sync helpers."""

from __future__ import annotations

from pathlib import Path

import subprocess

import pytest

from scripts import sync_import_change
from scripts.sync_import_change import (
    ImportBaseError,
    ImportRequest,
    _commit_author,
    _gh_pr_exists,
    _run_import_change,
    _string_bool,
    _validate_target,
    import_branch_name,
    import_change_pr_body,
    import_commit_subject_prefix,
    last_synced_public_sha,
)


def _git_repo_with_commits(*, root: Path, subjects: list[str]) -> None:
    """Initialize a Git repo at root and commit each subject in order."""
    _git(root, "init", "-q", "-b", "main", str(root))
    for key, value in (
        ("user.email", "test@example.com"),
        ("user.name", "Test"),
        ("commit.gpgsign", "false"),
    ):
        _git(root, "config", key, value)
    for i, subject in enumerate(subjects):
        (root / "f.txt").write_text(f"{i}\n", encoding="utf-8")
        _git(root, "add", "f.txt")
        _git(root, "commit", "-q", "-m", subject)


def _git(root: Path, *args: str) -> None:
    """Run a Git command in root for test fixture setup."""
    argv = ["git"] if args[0] == "init" else ["git", "-C", str(root)]
    subprocess.run([*argv, *args], check=True)  # noqa: S603 -- fixed argv, test-only


def _import_request(*, target_dir: Path) -> ImportRequest:
    """Build an ImportRequest with placeholder fields.

    For tests that only care about target_dir / project routing.
    """
    return ImportRequest(
        public_base=target_dir / "public-base",
        public_head=target_dir / "public-head",
        target_dir=target_dir,
        target_repo="rekursiv-ai/source",
        project_path=Path("package"),
        base_branch="main",
        public_repo="rekursiv-ai/public",
        public_sha="abcdef123456",
        public_base_ref="base",
        public_head_ref="head",
        branch="copybarista/import/sha-abcdef123456",
        sync_label="Package",
        sync_user_name="copybarista",
        sync_user_email="copybarista@example.com",
        report=target_dir / "report.json",
        open_pr=False,
        open_pr_only=False,
        runner_temp=target_dir,
        type_check_targets=(".",),
        refresh_public_lockfile=False,
    )


def test_main_accepts_generic_project_validation_args(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[ImportRequest] = []

    def fake_run_import_sync(request: ImportRequest) -> None:
        captured.append(request)

    monkeypatch.setattr(sync_import_change, "run_import_sync", fake_run_import_sync)

    sync_import_change.run(
        [
            "--project-path",
            "packages/configgle",
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
    assert captured[0].type_check_targets == ("configgle", "tests")
    assert not captured[0].refresh_public_lockfile


def test_main_resolves_filesystem_inputs_to_absolute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Copybarista subprocesses run with cwd=target_dir, so relative path inputs
    # must be made absolute or they double (target/target/...).
    captured: list[ImportRequest] = []

    def fake_run_import_sync(request: ImportRequest) -> None:
        captured.append(request)

    monkeypatch.setattr(sync_import_change, "run_import_sync", fake_run_import_sync)

    sync_import_change.run(
        [
            "--project-path",
            "pkg",
            "--target-dir",
            "target",
            "--public-base",
            "pb",
            "--public-head",
            "ph",
            "--report",
            "r.json",
            "--runner-temp",
            "rt",
            "--public-base-ref",
            "base",
            "--public-head-ref",
            "head",
        ]
    )

    req = captured[0]
    for value in (
        req.target_dir,
        req.public_base,
        req.public_head,
        req.report,
        req.runner_temp,
    ):
        assert value.is_absolute(), value


def test_main_accepts_refresh_public_lockfile_arg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[ImportRequest] = []

    def fake_run_import_sync(request: ImportRequest) -> None:
        captured.append(request)

    monkeypatch.setattr(sync_import_change, "run_import_sync", fake_run_import_sync)

    sync_import_change.run(
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
        requirements=tmp_path / "copybarista-requirements.txt",
    )

    assert len(calls) == 1


def test_run_import_sync_imports_then_validates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    target = tmp_path / "target"

    def fake_run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0)

    def fake_export_requirements(*, target_dir: Path, runner_temp: Path) -> Path:
        del target_dir  # signature must match for monkeypatch; unused here
        return runner_temp / "copybarista-requirements.txt"

    def fake_import_change(
        *, request: ImportRequest, project: Path, requirements: Path
    ) -> None:
        calls.append(
            ["import", str(project), str(request.target_dir), str(requirements)]
        )

    # Signature must match _validate_target for monkeypatch; some args unused here.
    def fake_validate_target(
        *,
        request: ImportRequest,
        project: Path,
        type_check_targets: tuple[str, ...],
        runner_temp: Path,
        requirements: Path,
    ) -> None:
        del request  # unused here; present to match the patched signature
        calls.append(
            [
                "validate",
                str(project),
                str(runner_temp),
                str(requirements),
                *type_check_targets,
            ]
        )

    monkeypatch.setattr(sync_import_change, "_run", fake_run)
    monkeypatch.setattr(
        sync_import_change, "_export_copybarista_requirements", fake_export_requirements
    )
    monkeypatch.setattr(sync_import_change, "_run_import_change", fake_import_change)
    monkeypatch.setattr(sync_import_change, "_validate_target", fake_validate_target)

    sync_import_change.run_import_sync(
        ImportRequest(
            public_base=tmp_path / "public-base",
            public_head=tmp_path / "public-head",
            target_dir=target,
            target_repo="rekursiv-ai/source",
            project_path=Path("package"),
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

    # Requirements are exported (stubbed), then import, then validate -- both
    # receive the same pinned requirements path.
    reqs = str(tmp_path / "copybarista-requirements.txt")
    assert calls[0] == ["import", str(target / "package"), str(target), reqs]
    assert calls[1] == ["validate", str(target / "package"), str(tmp_path), reqs, "."]


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


def test_validate_target_runs_checks_against_exported_tree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    tree = Path("/sentinel/validation-tree")

    def fake_run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0)

    # Signature must match _export_public_tree for monkeypatch; args unused.
    def fake_export(
        *,
        request: ImportRequest,
        project: Path,
        runner_temp: Path,
        requirements: Path,
    ) -> Path:
        del request, project, runner_temp, requirements
        return tree

    def fake_basedpyright(*, tree: Path, targets: tuple[str, ...]) -> None:
        calls.append(["basedpyright", str(tree), *targets])

    monkeypatch.setattr(sync_import_change, "_run", fake_run)
    monkeypatch.setattr(sync_import_change, "_export_public_tree", fake_export)
    monkeypatch.setattr(sync_import_change, "_run_basedpyright", fake_basedpyright)

    _validate_target(
        request=_import_request(target_dir=Path("/repo/target")),
        project=Path("/repo/pkg"),
        type_check_targets=("configgle",),
        runner_temp=Path("/sentinel/runner"),
        requirements=Path("/sentinel/copybarista-requirements.txt"),
    )

    # The exported public tree is synced once (with the package installed), then
    # every check runs against it via --project/--no-sync.
    assert [
        "uv",
        "--quiet",
        "--project",
        str(tree),
        "sync",
        "--frozen",
        "--all-groups",
    ] in calls
    assert [
        "uv",
        "--quiet",
        "--project",
        str(tree),
        "run",
        "--no-sync",
        "ty",
        "check",
    ] in calls

    uv_runs = [c for c in calls if c[:1] == ["uv"] and "run" in c]
    assert uv_runs
    assert all("--no-sync" in c for c in uv_runs)
    assert all(
        "--project" in c and c[c.index("--project") + 1] == str(tree) for c in uv_runs
    )


def test_export_public_tree_runs_copybarista_export(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(sync_import_change, "_run", fake_run)

    target = tmp_path / "target"
    project = target / "package"
    runner_temp = tmp_path / "runner"
    runner_temp.mkdir()

    requirements = tmp_path / "copybarista-requirements.txt"
    tree = sync_import_change._export_public_tree(
        request=_import_request(target_dir=target),
        project=project,
        runner_temp=runner_temp,
        requirements=requirements,
    )

    assert tree == runner_temp / "copybarista-validation-tree"
    export = calls[0]
    # Dependency-free copybarista export of the post-import tree to a folder,
    # using the pinned requirements file.
    assert "--no-project" in export
    assert str(requirements) in export
    assert export[-6:] == [
        "export",
        str(project / "copy.barista.toml"),
        str(target),
        "--folder-dir",
        str(tree),
        "--force",
    ]


def test_export_copybarista_requirements_exports_group_from_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(
            argv, 0, stdout="pyyaml==6.0.3\nruff==0.15.17\n"
        )

    monkeypatch.setattr(sync_import_change, "_run", fake_run)

    target = tmp_path / "target"
    runner_temp = tmp_path / "runner"
    runner_temp.mkdir()

    requirements = sync_import_change._export_copybarista_requirements(
        target_dir=target, runner_temp=runner_temp
    )

    assert requirements == (runner_temp / "copybarista-requirements.txt").resolve()
    assert requirements.read_text() == "pyyaml==6.0.3\nruff==0.15.17\n"
    # Pins come from the lock via the copybarista group, not a maintained file.
    assert calls[0] == [
        "uv",
        "--quiet",
        "export",
        "--only-group",
        "copybarista",
        "--no-hashes",
        "--no-emit-project",
        "--format",
        "requirements.txt",
    ]


def test_import_commit_subject_round_trips_through_baseline_walk(
    tmp_path: Path,
) -> None:
    # The SHA written into the import commit subject must be the exact SHA the
    # next run's baseline walk recovers. This is the contract OA-1 broke at the
    # workflow level (recording the run SHA instead of the imported head SHA).
    sha = "3" * 40
    _git_repo_with_commits(
        root=tmp_path,
        subjects=[import_commit_subject_prefix("Sagent") + sha],
    )

    assert (
        last_synced_public_sha(
            target_dir=tmp_path, sync_label="Sagent", base_branch="main"
        )
        == sha
    )


def test_last_synced_public_sha_returns_newest_imported_sha(tmp_path: Path) -> None:
    older = "a" * 40
    newer = "b" * 40
    _git_repo_with_commits(
        root=tmp_path,
        subjects=[
            f"Import Sagent public changes {older}",
            "Add cross-experiment metric query support",
            f"Import Sagent public changes {newer}",
            "Later unrelated work",
        ],
    )

    assert (
        last_synced_public_sha(
            target_dir=tmp_path, sync_label="Sagent", base_branch="main"
        )
        == newer
    )


def test_last_synced_public_sha_scopes_to_sync_label(tmp_path: Path) -> None:
    # A different label's imports must not be mistaken for this label's baseline.
    sagent = "c" * 40
    _git_repo_with_commits(
        root=tmp_path,
        subjects=[
            f"Import Sagent public changes {sagent}",
            f"Import Configgle public changes {'d' * 40}",
        ],
    )

    assert (
        last_synced_public_sha(
            target_dir=tmp_path, sync_label="Sagent", base_branch="main"
        )
        == sagent
    )


def test_last_synced_public_sha_raises_without_prior_import(tmp_path: Path) -> None:
    _git_repo_with_commits(root=tmp_path, subjects=["Initial commit"])

    with pytest.raises(ImportBaseError, match="No landed 'Sagent' import commit"):
        last_synced_public_sha(
            target_dir=tmp_path, sync_label="Sagent", base_branch="main"
        )


def test_last_synced_public_sha_returns_fallback_without_prior_import(
    tmp_path: Path,
) -> None:
    # First import: no landed history yet, so the caller-supplied baseline
    # (the pushed commit's parent) stands in for the merge ancestor.
    parent = "f" * 40
    _git_repo_with_commits(root=tmp_path, subjects=["Initial commit"])

    assert (
        last_synced_public_sha(
            target_dir=tmp_path,
            sync_label="Sagent",
            base_branch="main",
            fallback=parent,
        )
        == parent
    )


def test_last_synced_public_sha_matches_label_literally(tmp_path: Path) -> None:
    # A label with regex metacharacters must match by literal text, not as a
    # git --grep regex that could match unintended subjects.
    sha = "1" * 40
    _git_repo_with_commits(
        root=tmp_path,
        subjects=[f"Import C++.NET public changes {sha}"],
    )

    assert (
        last_synced_public_sha(
            target_dir=tmp_path, sync_label="C++.NET", base_branch="main"
        )
        == sha
    )


def test_last_synced_public_sha_ignores_incidental_sha_in_subject(
    tmp_path: Path,
) -> None:
    # A 40-hex token elsewhere in an unrelated subject must not be mistaken for
    # a landed import; only the import-template position counts.
    incidental = "9" * 40
    landed = "2" * 40
    _git_repo_with_commits(
        root=tmp_path,
        subjects=[
            f"Import Sagent public changes {landed}",
            f"Revert commit {incidental} for reasons",
        ],
    )

    assert (
        last_synced_public_sha(
            target_dir=tmp_path, sync_label="Sagent", base_branch="main"
        )
        == landed
    )


def test_main_print_synced_base_prints_and_skips_import(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    sha = "e" * 40
    _git_repo_with_commits(
        root=tmp_path, subjects=[f"Import Sagent public changes {sha}"]
    )

    def fail_run_import_sync(_: ImportRequest) -> None:
        raise AssertionError("import must not run in print-synced-base mode")

    monkeypatch.setattr(sync_import_change, "run_import_sync", fail_run_import_sync)

    sync_import_change.run(
        [
            "--print-synced-base",
            "--target-dir",
            str(tmp_path),
            "--sync-label",
            "Sagent",
            "--base-branch",
            "main",
        ]
    )

    assert capsys.readouterr().out.strip() == sha


def test_main_print_synced_base_emits_fallback_without_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    parent = "7" * 40
    _git_repo_with_commits(root=tmp_path, subjects=["Initial commit"])

    def fail_run_import_sync(_: ImportRequest) -> None:
        raise AssertionError("import must not run in print-synced-base mode")

    monkeypatch.setattr(sync_import_change, "run_import_sync", fail_run_import_sync)

    sync_import_change.run(
        [
            "--print-synced-base",
            "--target-dir",
            str(tmp_path),
            "--sync-label",
            "Sagent",
            "--base-branch",
            "main",
            "--fallback-sha",
            parent,
        ]
    )

    assert capsys.readouterr().out.strip() == parent


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
