"""Unit tests for Git destination helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import hashlib

import pytest

from copybarista import git as git_module
from copybarista.commands import CommandResult
from copybarista.config import (
    FileSelection,
    FolderDestination,
    GitDestination,
    WorkflowConfig,
)
from copybarista.destinations import DestinationResult
from copybarista.errors import ExportError
from copybarista.git import (
    GitRuntime,
    _commit_and_push,
    _commit_message,
    _ensure_local_remote,
    _has_staged_changes,
    _local_remote_path,
    _prepare_destination,
    _remote_branch_exists,
    _replace_worktree_contents,
    _sync_cached_bare_repo,
    _verify_user_info_configured,
    write_git_destination,
)
from copybarista.workflow import StagedTree


def test_local_remote_path_rejects_scp_style_url():
    assert _local_remote_path("git@github.com:org/repo.git") is None


def test_local_remote_path_accepts_file_url(tmp_path: Path):
    remote = tmp_path / "remote.git"

    assert _local_remote_path(f"file://{remote.as_posix()}") == remote


def test_local_remote_path_accepts_relative_local_path():
    assert _local_remote_path("out/remote.git") == Path("out/remote.git")


def test_export_git_requires_destination_url(tmp_path: Path):
    config = _workflow_config(git=GitDestination())

    with pytest.raises(ExportError, match="url"):
        git_module.export_git(config=config, source_ref=tmp_path)


def test_export_git_stages_manifest_without_real_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    source_ref = tmp_path / "repo"
    project = source_ref / "project"
    project.mkdir(parents=True)
    (project / "README.md").write_text("hello\n", encoding="utf-8")

    def fake_write(
        staged_tree: StagedTree,
        *,
        destination: GitDestination,
        origin_rev_id: str = "",
        runtime: GitRuntime | None = None,
    ) -> DestinationResult:
        assert destination.url == "ssh://example.com/repo.git"
        assert origin_rev_id == ""
        assert runtime is not None
        assert (staged_tree.root / "README.md").read_text(encoding="utf-8") == "hello\n"
        return DestinationResult(status="updated", ref="abc123")

    monkeypatch.setattr(git_module, "write_git_destination", fake_write)

    manifest = git_module.export_git(
        config=_workflow_config(
            git=GitDestination(url="ssh://example.com/repo.git", branch="main")
        ),
        source_ref=source_ref,
    )

    assert [entry.destination for entry in manifest.files] == ["README.md"]


def test_write_git_destination_uses_temporary_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    source = tmp_path / "source"
    (source / "pkg").mkdir(parents=True)
    (source / "README.md").write_text("hello\n", encoding="utf-8")
    (source / "pkg" / "module.py").write_text("value = 1\n", encoding="utf-8")
    observed: list[tuple[bool, bool, bool]] = []

    def fake_prepare(
        destination: GitDestination,
        worktree: Path,
        *,
        runtime: GitRuntime | None = None,
    ) -> None:
        assert destination.url == "ssh://example.com/repo.git"
        assert runtime is not None
        worktree.mkdir()
        (worktree / ".git").mkdir()
        (worktree / "stale.txt").write_text("stale\n", encoding="utf-8")
        (worktree / "old").mkdir()

    def fake_commit(
        destination: GitDestination,
        worktree: Path,
        *,
        origin_rev_id: str = "",
        runtime: GitRuntime | None = None,
    ) -> str:
        assert destination.branch == "main"
        assert origin_rev_id == ""
        assert runtime is not None
        observed.append(
            (
                (worktree / ".git").is_dir(),
                (worktree / "stale.txt").exists(),
                (worktree / "pkg" / "module.py").is_file(),
            )
        )
        return "abc123"

    monkeypatch.setattr(git_module, "_prepare_destination", fake_prepare)
    monkeypatch.setattr(git_module, "_commit_and_push", fake_commit)

    result = write_git_destination(
        StagedTree(root=source, files=(), transforms=()),
        destination=GitDestination(url="ssh://example.com/repo.git", branch="main"),
    )

    assert result.status == "updated"
    assert result.ref == "abc123"
    assert observed == [(True, False, True)]


def test_replace_worktree_contents_preserves_nested_submodules(tmp_path: Path):
    source = tmp_path / "source"
    (source / "pkg").mkdir(parents=True)
    (source / "pkg" / "module.py").write_text("value = 1\n", encoding="utf-8")
    worktree = tmp_path / "worktree"
    (worktree / ".git").mkdir(parents=True)
    (worktree / "vendor" / "submodule").mkdir(parents=True)
    (worktree / "vendor" / "submodule" / ".git").write_text(
        "gitdir: ../../.git/modules/vendor/submodule\n",
        encoding="utf-8",
    )
    (worktree / "vendor" / "stale.txt").write_text("stale\n", encoding="utf-8")

    _replace_worktree_contents(source=source, worktree=worktree)

    assert (worktree / ".git").is_dir()
    assert (worktree / "vendor" / "submodule" / ".git").is_file()
    assert not (worktree / "vendor" / "stale.txt").exists()
    assert (worktree / "pkg" / "module.py").is_file()


def test_replace_worktree_contents_rejects_staged_symlink_escape(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "escape").symlink_to("../outside.txt")
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / ".git").mkdir()

    with pytest.raises(ExportError, match="outside staged tree"):
        _replace_worktree_contents(source=source, worktree=worktree)


def test_prepare_destination_checks_out_existing_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    calls: list[tuple[str, ...]] = []
    cache = tmp_path / "cache.git"

    def fake_git(*args: str, runtime: GitRuntime | None = None) -> CommandResult:
        assert runtime is not None
        calls.append(args)
        return CommandResult(returncode=0, stdout="", stderr="")

    def remote_branch_exists(
        destination: GitDestination,
        worktree: Path,
        *,
        runtime: GitRuntime | None = None,
    ) -> bool:
        assert runtime is not None
        assert destination.branch == "main"
        assert worktree == tmp_path / "worktree"
        return True

    def skip_local_remote(url: str, *, runtime: GitRuntime | None = None) -> None:
        assert runtime is not None
        assert url == "ssh://example.com/repo.git"

    def sync_cache(url: str, *, runtime: GitRuntime | None = None) -> Path:
        assert runtime is not None
        assert url == "ssh://example.com/repo.git"
        return cache

    monkeypatch.setattr(git_module, "_ensure_local_remote", skip_local_remote)
    monkeypatch.setattr(git_module, "_sync_cached_bare_repo", sync_cache)
    monkeypatch.setattr(git_module, "_remote_branch_exists", remote_branch_exists)
    monkeypatch.setattr(git_module, "_git", fake_git)

    _prepare_destination(
        GitDestination(url="ssh://example.com/repo.git", branch="main"),
        tmp_path / "worktree",
    )

    assert calls[0] == ("clone", str(cache), str(tmp_path / "worktree"))
    assert calls[1] == (
        "-C",
        str(tmp_path / "worktree"),
        "remote",
        "set-url",
        "origin",
        "ssh://example.com/repo.git",
    )
    assert calls[2][-2:] == ("main", "origin/main")


def test_prepare_destination_creates_orphan_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    calls: list[tuple[str, ...]] = []
    cache = tmp_path / "cache.git"

    def fake_git(*args: str, runtime: GitRuntime | None = None) -> CommandResult:
        assert runtime is not None
        calls.append(args)
        return CommandResult(returncode=0, stdout="", stderr="")

    def skip_local_remote(url: str, *, runtime: GitRuntime | None = None) -> None:
        assert runtime is not None
        assert url == "ssh://example.com/repo.git"

    def sync_cache(url: str, *, runtime: GitRuntime | None = None) -> Path:
        assert runtime is not None
        assert url == "ssh://example.com/repo.git"
        return cache

    def missing_branch(
        destination: GitDestination,
        worktree: Path,
        *,
        runtime: GitRuntime | None = None,
    ) -> bool:
        assert runtime is not None
        assert destination.branch == "main"
        assert worktree == tmp_path / "worktree"
        return False

    monkeypatch.setattr(git_module, "_ensure_local_remote", skip_local_remote)
    monkeypatch.setattr(git_module, "_sync_cached_bare_repo", sync_cache)
    monkeypatch.setattr(git_module, "_remote_branch_exists", missing_branch)
    monkeypatch.setattr(git_module, "_git", fake_git)

    _prepare_destination(
        GitDestination(url="ssh://example.com/repo.git", branch="main"),
        tmp_path / "worktree",
    )

    assert calls[2][-3:] == ("checkout", "--orphan", "main")


def test_ensure_local_remote_initializes_empty_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    calls: list[tuple[str, ...]] = []
    remote = tmp_path / "remote.git"
    remote.mkdir()

    def fake_git(*args: str, runtime: GitRuntime | None = None) -> CommandResult:
        assert runtime is not None
        calls.append(args)
        return CommandResult(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(git_module, "_git", fake_git)

    _ensure_local_remote(remote.as_posix())

    assert remote.parent.is_dir()
    assert calls == [("init", "--bare", remote.as_posix())]


def test_ensure_local_remote_rejects_missing_path(
    tmp_path: Path,
):
    with pytest.raises(ExportError, match="already exist"):
        _ensure_local_remote((tmp_path / "remote.git").as_posix())


def test_ensure_local_remote_rejects_non_git_directory(tmp_path: Path):
    remote = tmp_path / "remote.git"
    remote.mkdir()
    (remote / "README.md").write_text("not git\n", encoding="utf-8")

    with pytest.raises(ExportError, match="not a Git repository"):
        _ensure_local_remote(remote.as_posix())


def test_sync_cached_bare_repo_clones_missing_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    calls: list[tuple[str, ...]] = []

    def fake_git(*args: str, runtime: GitRuntime | None = None) -> CommandResult:
        assert runtime is not None
        calls.append(args)
        return CommandResult(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(git_module, "_git", fake_git)

    cache = _sync_cached_bare_repo(
        "ssh://example.com/repo.git",
        runtime=GitRuntime(cache_root=tmp_path / "cache"),
    )

    assert cache.parent == tmp_path / "cache"
    assert calls == [("clone", "--mirror", "ssh://example.com/repo.git", str(cache))]


def test_sync_cached_bare_repo_fetches_existing_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    calls: list[tuple[str, ...]] = []
    cache_root = tmp_path / "cache"
    existing = cache_root / hashlib.sha256(b"ssh://example.com/repo.git").hexdigest()
    existing.mkdir(parents=True)

    def fake_git(*args: str, runtime: GitRuntime | None = None) -> CommandResult:
        assert runtime is not None
        calls.append(args)
        return CommandResult(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(git_module, "_git", fake_git)

    assert (
        _sync_cached_bare_repo(
            "ssh://example.com/repo.git",
            runtime=GitRuntime(cache_root=cache_root),
        )
        == existing
    )
    assert calls == [("-C", str(existing), "remote", "update", "--prune")]


def test_remote_branch_exists_handles_git_exit_codes():
    fake = _FakeCommands(result=CommandResult(returncode=2, stdout="", stderr=""))

    assert not _remote_branch_exists(
        GitDestination(url="ssh://example.com/repo.git", branch="main"),
        Path("worktree"),
        runtime=GitRuntime(commands=fake),
    )
    assert fake.calls[0][-2:] == ["origin", "main"]


def test_remote_branch_exists_reports_unexpected_failure():
    runtime = GitRuntime(
        commands=_FakeCommands(
            result=CommandResult(returncode=128, stdout="", stderr="bad remote")
        )
    )

    with pytest.raises(ExportError, match="bad remote"):
        _remote_branch_exists(
            GitDestination(url="ssh://example.com/repo.git", branch="main"),
            Path("worktree"),
            runtime=runtime,
        )


def test_remote_branch_exists_treats_exit_two_with_stderr_as_failure():
    runtime = GitRuntime(
        commands=_FakeCommands(
            result=CommandResult(returncode=2, stdout="", stderr="auth failed")
        )
    )

    with pytest.raises(ExportError, match="auth failed"):
        _remote_branch_exists(
            GitDestination(url="ssh://example.com/repo.git", branch="main"),
            Path("worktree"),
            runtime=runtime,
        )


def test_has_staged_changes_handles_git_exit_codes():
    fake = _FakeCommands(result=CommandResult(returncode=1, stdout="", stderr=""))

    assert _has_staged_changes(Path("worktree"), runtime=GitRuntime(commands=fake))
    assert fake.calls[0][-3:] == ["diff", "--cached", "--quiet"]


def test_has_staged_changes_reports_unexpected_failure():
    runtime = GitRuntime(
        commands=_FakeCommands(
            result=CommandResult(returncode=128, stdout="", stderr="")
        )
    )

    with pytest.raises(ExportError, match="staged"):
        _has_staged_changes(Path("worktree"), runtime=runtime)


def test_commit_and_push_uses_configured_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    calls: list[tuple[str, ...]] = []

    def fake_git(*args: str, runtime: GitRuntime | None = None) -> CommandResult:
        assert runtime is not None
        calls.append(args)
        if args[-2:] == ("config", "-l"):
            stdout = "user.name=Copybarista\nuser.email=copybarista@example.com\n"
        elif "rev-parse" in args:
            stdout = "abc123\n"
        else:
            stdout = ""
        return CommandResult(returncode=0, stdout=stdout, stderr="")

    def has_staged_changes(
        worktree: Path, *, runtime: GitRuntime | None = None
    ) -> bool:
        assert runtime is not None
        assert worktree == tmp_path
        return True

    monkeypatch.setattr(git_module, "_git", fake_git)
    monkeypatch.setattr(git_module, "_has_staged_changes", has_staged_changes)

    commit = _commit_and_push(
        GitDestination(
            url="ssh://example.com/repo.git",
            branch="main",
            committer_name="Copybarista",
            committer_email="copybarista@example.com",
        ),
        tmp_path,
        origin_rev_id="source123",
        runtime=GitRuntime(),
    )

    assert commit == "abc123"
    assert calls[0][-2:] == ("user.name", "Copybarista")
    assert calls[1][-2:] == ("user.email", "copybarista@example.com")
    assert calls[3][-2:] == ("add", "-A")
    assert calls[4][-2:] == (
        "-m",
        "Project import generated by Copybarista.\n\nCopybarista-Source-Rev: source123",
    )
    assert calls[-1][-2:] == ("origin", "HEAD:main")


def test_commit_and_push_returns_noop_without_staged_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    calls: list[tuple[str, ...]] = []

    def fake_git(*args: str, runtime: GitRuntime | None = None) -> CommandResult:
        assert runtime is not None
        calls.append(args)
        stdout = (
            "user.name=Copybarista\nuser.email=copybarista@example.com\n"
            if args[-2:] == ("config", "-l")
            else ""
        )
        return CommandResult(returncode=0, stdout=stdout, stderr="")

    def has_staged_changes(
        worktree: Path, *, runtime: GitRuntime | None = None
    ) -> bool:
        assert runtime is not None
        assert worktree == tmp_path
        return False

    monkeypatch.setattr(git_module, "_git", fake_git)
    monkeypatch.setattr(git_module, "_has_staged_changes", has_staged_changes)

    assert (
        _commit_and_push(
            GitDestination(
                url="ssh://example.com/repo.git",
                committer_name="Copybarista",
                committer_email="copybarista@example.com",
            ),
            tmp_path,
            runtime=GitRuntime(),
        )
        == ""
    )
    assert calls[-1] == ("-C", str(tmp_path), "add", "-A")


def test_verify_user_info_configured_rejects_missing_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(
        git_module,
        "_git",
        _empty_git_config,
    )

    with pytest.raises(ExportError, match=r"user\.name"):
        _verify_user_info_configured(tmp_path, runtime=GitRuntime())


def test_commit_message_includes_copybarista_origin_label():
    assert _commit_message(origin_rev_id="abc123") == (
        "Project import generated by Copybarista.\n\nCopybarista-Source-Rev: abc123"
    )


def _empty_git_config(*_args: str, runtime: GitRuntime | None = None) -> CommandResult:
    assert runtime is not None
    return CommandResult(returncode=0, stdout="", stderr="")


@dataclass(slots=True, kw_only=True)
class _FakeCommands:
    result: CommandResult
    calls: list[list[str]] = field(default_factory=list)
    checks: list[bool] = field(default_factory=list)

    def run(self, argv: list[str], *, check: bool = True) -> CommandResult:
        self.calls.append(argv)
        self.checks.append(check)
        return self.result


def _workflow_config(*, git: GitDestination) -> WorkflowConfig:
    return WorkflowConfig(
        name="demo",
        mode="squash",
        source_root="project",
        files=FileSelection(include=("**",), exclude=()),
        transforms=(),
        folder=FolderDestination(),
        git=git,
    )
