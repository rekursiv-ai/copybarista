"""Tests for public-to-source sync helpers."""

from __future__ import annotations

from pathlib import Path

import subprocess

import pytest

from copybarista.scripts import sync_import_change
from copybarista.scripts.sync_import_change import (
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
        validation_commands=(),
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
            "--validation-command",
            "uv sync --all-groups",
            "--validation-command",
            "uv run pytest",
        ]
    )

    assert captured[0].project_path == Path("packages/configgle")
    assert captured[0].validation_commands == ("uv sync --all-groups", "uv run pytest")
    assert not captured[0].refresh_public_lockfile


def test_export_copybarista_requirements_uses_frozen(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The copybarista-group export must not re-resolve the monorepo lock.

    Run in the monorepo root without ``--frozen``, ``uv export`` is free to
    re-resolve the whole ``loop`` project whenever the runner's checked-out
    ``uv.lock`` looks stale, dragging in monorepo-only ``loop:dev`` deps (e.g.
    the linux-only ``pycairo``, which needs a system cairo the import runner
    lacks). ``--frozen`` pins it to the committed lock so the bootstrap only
    reads the ``copybarista`` group and never builds monorepo dev deps.
    """
    captured: list[list[str]] = []

    def fake_run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        captured.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(sync_import_change, "_run", fake_run)

    sync_import_change._export_copybarista_requirements(
        target_dir=tmp_path, runner_temp=tmp_path
    )

    assert captured, "expected an uv export invocation"
    argv = captured[0]
    assert "export" in argv
    assert "--frozen" in argv, f"uv export must be frozen; got {argv}"


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
            validation_commands=(),
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
        validation_commands: tuple[str, ...],
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
                *validation_commands,
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
            validation_commands=("uv run pytest",),
            refresh_public_lockfile=False,
        )
    )

    # Requirements are exported (stubbed), then import, then validate -- both
    # receive the same pinned requirements path.
    reqs = str(tmp_path / "copybarista-requirements.txt")
    assert calls[0] == ["import", str(target / "package"), str(target), reqs]
    assert calls[1] == [
        "validate",
        str(target / "package"),
        str(tmp_path),
        reqs,
        "uv run pytest",
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


def test_validate_target_runs_checks_against_exported_tree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], Path | None]] = []
    tree = Path("/sentinel/validation-tree")

    def fake_run(
        argv: list[str], *, cwd: Path | None = None, **_: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append((argv, cwd))
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

    monkeypatch.setattr(sync_import_change, "_run", fake_run)
    monkeypatch.setattr(sync_import_change, "_export_public_tree", fake_export)

    _validate_target(
        request=_import_request(target_dir=Path("/repo/target")),
        project=Path("/repo/pkg"),
        validation_commands=("uv sync --all-groups", "uv run pytest"),
        runner_temp=Path("/sentinel/runner"),
        requirements=Path("/sentinel/copybarista-requirements.txt"),
    )

    # Each validation command runs via bash -c against the exported public tree.
    assert calls == [
        (["bash", "-c", "uv sync --all-groups"], tree),
        (["bash", "-c", "uv run pytest"], tree),
    ]


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
        "--frozen",
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


def test_last_synced_public_sha_reads_a_squash_merged_subject(
    tmp_path: Path,
) -> None:
    """A squash-merged import still records the baseline.

    GitHub appends ``(#N)`` to the subject when a PR is squash-merged, which
    is how these imports land. An end-anchored match skips those commits and
    silently returns an OLDER baseline -- the export's data-loss guard then
    compares against stale state, and the import's three-way merge gets a
    wrong common ancestor, manufacturing conflicts that are not real.
    """
    older = "a" * 40
    newer = "b" * 40
    _git_repo_with_commits(
        root=tmp_path,
        subjects=[
            f"Import Sagent public changes {older}",
            f"Import Sagent public changes {newer} (#77)",
        ],
    )

    assert (
        last_synced_public_sha(
            target_dir=tmp_path, sync_label="Sagent", base_branch="main"
        )
        == newer
    )


def test_last_synced_public_sha_ignores_trailing_prose(tmp_path: Path) -> None:
    """Only the PR-number suffix is tolerated, not arbitrary trailing text."""
    good = "a" * 40
    _git_repo_with_commits(
        root=tmp_path,
        subjects=[
            f"Import Sagent public changes {good}",
            f"Import Sagent public changes {'b' * 40} but reverted later",
        ],
    )

    assert (
        last_synced_public_sha(
            target_dir=tmp_path, sync_label="Sagent", base_branch="main"
        )
        == good
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


def test_import_pr_auto_merges_when_enabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A clean import merges itself; a human adds nothing to that decision.

    The public change is already reviewed and already live -- the source is
    the render authority, not a second approval gate. Waiting on a human is
    also what stalls the export, since an unmerged import blocks it.
    """
    merged: list[list[str]] = []

    def fake_run_gh(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        if "merge" in argv:
            merged.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="")

    monkeypatch.setattr(sync_import_change, "_run_gh", fake_run_gh)

    sync_import_change._merge_import_pr(
        branch="pkg/import/sha-abc",
        target_repo="rekursiv-ai/source",
        title="Import Package public changes abc",
        sync_label="Package",
        cwd=tmp_path,
    )

    assert merged, "a clean import must merge without waiting for a human"
    assert "--squash" in merged[0]
    assert "--auto" in merged[0]


def test_import_pr_merges_directly_when_auto_merge_unavailable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Without branch protection, ``--auto`` fails; merge immediately instead."""
    calls: list[list[str]] = []

    def fake_run_gh(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if "--auto" in argv:
            return subprocess.CompletedProcess(
                argv, 1, stdout="", stderr="Protected branch rules not configured."
            )
        return subprocess.CompletedProcess(argv, 0, stdout="")

    monkeypatch.setattr(sync_import_change, "_run_gh", fake_run_gh)

    sync_import_change._merge_import_pr(
        branch="pkg/import/sha-abc",
        target_repo="rekursiv-ai/source",
        title="Import Package public changes abc",
        sync_label="Package",
        cwd=tmp_path,
    )

    assert len(calls) == 2, "must retry without --auto"
    assert "--auto" not in calls[1]


def test_pr_title_sha_is_readable_by_the_ledger(tmp_path: Path) -> None:
    """The title an import writes must parse as the marker it later reads.

    ``_open_or_update_target_pr`` wrote an abbreviated SHA while
    ``last_synced_public_sha`` required the full 40 characters, so a landed
    import recorded nothing the ledger could see -- the export's guard then
    reported the just-imported commit as unimported and skipped forever.
    """
    full = "0e8b8406cdd2" + "0" * 28
    title = f"Import Sagent public changes {sync_import_change._pr_title_sha(full)}"
    _git_repo_with_commits(root=tmp_path, subjects=[title])

    assert (
        last_synced_public_sha(
            target_dir=tmp_path, sync_label="Sagent", base_branch="main"
        )
        == full
    )
