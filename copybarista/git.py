"""Git destination export support.

Git exports stage the workflow into a temporary tree, clone or create the
configured destination, replace the worktree contents, and push one squash
commit when the exported content changed. Remote URLs are never initialized as
local bare repositories; only explicit local paths and `file://` URLs get that
convenience behavior.
"""

from __future__ import annotations

import contextlib
import hashlib
import re
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol
from urllib.parse import unquote, urlparse

from copybarista.commands import CommandResult, CommandRunner, resolve_executable
from copybarista.config import GitDestination, WorkflowConfig
from copybarista.destinations import DestinationResult, validate_staged_symlinks
from copybarista.errors import ExportError
from copybarista.manifest import ExportManifest
from copybarista.workflow import StagedTree, WorkflowRunner

SCP_STYLE_URL = re.compile(r"^[^/@:\s]+@[^/:\s]+:.+")
LS_REMOTE_NO_MATCH = 2


class GitCommands(Protocol):
    """Runs Git command lines for Git destination operations."""

    def run(self, argv: list[str], *, check: bool = True) -> CommandResult:
        """Run one command and return its captured result."""
        ...


@dataclass(frozen=True, slots=True, kw_only=True)
class GitRuntime:
    """Configurable command boundary for Git destination operations.

    The export logic keeps Git process execution, cache location, and source
    revision label policy in one object so tests and automation can supply
    their own command runner without patching module globals.

    Attributes:
      git: Git executable path.
      commands: Command runner used for every Git invocation.
      cache_root: Bare mirror cache root for destination clones.
      source_rev_label: Commit-message label for the source revision.

    """

    git: str = field(default_factory=lambda: resolve_executable("git"))
    commands: GitCommands = field(default_factory=CommandRunner)
    cache_root: Path = field(
        default_factory=lambda: Path.home() / ".cache" / "copybarista" / "git"
    )
    source_rev_label: str = "Copybarista-Source-Rev"


def export_git(
    config: WorkflowConfig, source_ref: Path, *, runtime: GitRuntime | None = None
) -> ExportManifest:
    """Export a workflow to a Git destination as one commit.

    Args:
      config: Workflow config.
      source_ref: Source checkout root.
      runtime: Optional Git command/cache configuration.

    Returns:
      manifest: Manifest for the transformed tree committed to Git.

    Raises:
      ExportError: If Git export cannot complete.

    """
    runtime = _runtime(runtime)
    if not config.git.url:
        raise ExportError("destination.git.url is required for Git export")
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="copybarista-git-") as tmp:
        staging = Path(tmp) / "staging"
        staged_tree = WorkflowRunner(config=config, source_ref=source_ref).stage(
            staging
        )
        write_git_destination(
            staged_tree,
            destination=config.git,
            origin_rev_id=_origin_rev_id(source_ref, runtime=runtime),
            runtime=runtime,
        )
    return ExportManifest(
        files=staged_tree.files,
        transforms=staged_tree.transforms,
        elapsed_sec=time.perf_counter() - started,
    )


def write_git_destination(
    staged_tree: StagedTree,
    *,
    destination: GitDestination,
    origin_rev_id: str = "",
    runtime: GitRuntime | None = None,
) -> DestinationResult:
    """Publish a transformed tree as a single commit on a Git branch.

    Args:
      staged_tree: Transformed tree to commit.
      destination: Git destination config.
      origin_rev_id: Optional source commit label value.
      runtime: Optional Git command/cache configuration.

    Returns:
      result: Destination write status and commit reference.

    Raises:
      ExportError: If Git export cannot complete.

    """
    runtime = _runtime(runtime)
    if not destination.url:
        raise ExportError("destination.git.url is required for Git export")
    with tempfile.TemporaryDirectory(prefix="copybarista-git-worktree-") as tmp:
        worktree = Path(tmp) / "worktree"
        _prepare_destination(destination, worktree, runtime=runtime)
        _replace_worktree_contents(source=staged_tree.root, worktree=worktree)
        commit = _commit_and_push(
            destination,
            worktree,
            origin_rev_id=origin_rev_id,
            runtime=runtime,
        )
    if not commit:
        return DestinationResult(status="noop", ref=destination.url)
    return DestinationResult(status="updated", ref=commit)


def _prepare_destination(
    destination: GitDestination, worktree: Path, *, runtime: GitRuntime | None = None
) -> None:
    """Create or clone the Git destination and check out the target branch."""
    runtime = _runtime(runtime)
    _ensure_local_remote(destination.url, runtime=runtime)
    cache = _sync_cached_bare_repo(destination.url, runtime=runtime)
    _git("clone", str(cache), str(worktree), runtime=runtime)
    _git(
        "-C",
        str(worktree),
        "remote",
        "set-url",
        "origin",
        destination.url,
        runtime=runtime,
    )
    if _remote_branch_exists(destination, worktree, runtime=runtime):
        _git(
            "-C",
            str(worktree),
            "checkout",
            "-B",
            destination.branch,
            f"origin/{destination.branch}",
            runtime=runtime,
        )
    else:
        _git(
            "-C",
            str(worktree),
            "checkout",
            "--orphan",
            destination.branch,
            runtime=runtime,
        )


def _ensure_local_remote(url: str, *, runtime: GitRuntime | None = None) -> None:
    """Initialize an empty local bare remote or validate an existing one."""
    runtime = _runtime(runtime)
    path = _local_remote_path(url)
    if path is None:
        return
    if not path.exists():
        raise ExportError(
            f"Local Git remote path must already exist or be an empty directory: {path}"
        )
    if not path.is_dir():
        raise ExportError(f"Local Git remote path is not a directory: {path}")
    if not any(path.iterdir()):
        _git("init", "--bare", str(path), runtime=runtime)
        return
    if not (path / "HEAD").is_file():
        raise ExportError(f"Local Git remote path is not a Git repository: {path}")


def _local_remote_path(url: str) -> Path | None:
    """Return the filesystem path for local Git URLs.

    SCP-style SSH remotes have no URL scheme, so they must be detected before
    treating scheme-less values as local paths.
    """
    if SCP_STYLE_URL.match(url):
        return None
    parsed = urlparse(url)
    if parsed.scheme == "file":
        return Path(unquote(parsed.path)).expanduser()
    if parsed.scheme:
        return None
    return Path(url).expanduser()


def _sync_cached_bare_repo(url: str, *, runtime: GitRuntime | None = None) -> Path:
    """Return a bare mirror cache updated from the destination URL."""
    runtime = _runtime(runtime)
    cache = runtime.cache_root / hashlib.sha256(url.encode()).hexdigest()
    if cache.exists():
        _git("-C", str(cache), "remote", "update", "--prune", runtime=runtime)
        return cache
    cache.parent.mkdir(parents=True, exist_ok=True)
    _git("clone", "--mirror", url, str(cache), runtime=runtime)
    return cache


def _remote_branch_exists(
    destination: GitDestination, worktree: Path, *, runtime: GitRuntime | None = None
) -> bool:
    """Return whether the configured remote branch already exists."""
    runtime = _runtime(runtime)
    result = runtime.commands.run(
        [
            runtime.git,
            "-C",
            str(worktree),
            "ls-remote",
            "--exit-code",
            "--heads",
            "origin",
            destination.branch,
        ],
        check=False,
    )
    if result.returncode == 0:
        return True
    if result.returncode == LS_REMOTE_NO_MATCH and not result.stderr.strip():
        return False
    raise ExportError(result.stderr.strip() or "Failed to inspect Git destination")


def _replace_worktree_contents(source: Path, worktree: Path) -> None:
    """Replace all non-Git worktree contents with the staged tree."""
    validate_staged_symlinks(source)
    for path in worktree.iterdir():
        if path.name == ".git":
            continue
        _remove_worktree_entry(path)
    for path in source.iterdir():
        _copy_worktree_entry(source=path, destination=worktree / path.name)


def _copy_worktree_entry(*, source: Path, destination: Path) -> None:
    """Copy one staged entry without overwriting a Git submodule root."""
    if _is_submodule_root(destination):
        raise ExportError(f"Refusing to overwrite Git submodule: {destination}")
    if source.is_dir() and not source.is_symlink():
        shutil.copytree(source, destination, symlinks=True, dirs_exist_ok=True)
    else:
        shutil.copy2(source, destination, follow_symlinks=False)


def _remove_worktree_entry(path: Path) -> None:
    """Remove a worktree entry while preserving nested Git submodules."""
    if path.is_dir() and not path.is_symlink():
        if _is_submodule_root(path):
            return
        for child in path.iterdir():
            _remove_worktree_entry(child)
        with contextlib.suppress(OSError):
            path.rmdir()
        return
    path.unlink()


def _is_submodule_root(path: Path) -> bool:
    """Return whether a path is a Git submodule checkout root."""
    return path.is_dir() and (path / ".git").exists()


def _commit_and_push(
    destination: GitDestination,
    worktree: Path,
    *,
    origin_rev_id: str = "",
    runtime: GitRuntime | None = None,
) -> str:
    """Commit staged changes and push, returning the new commit hash."""
    runtime = _runtime(runtime)
    if destination.committer_name:
        _git(
            "-C",
            str(worktree),
            "config",
            "user.name",
            destination.committer_name,
            runtime=runtime,
        )
    if destination.committer_email:
        _git(
            "-C",
            str(worktree),
            "config",
            "user.email",
            destination.committer_email,
            runtime=runtime,
        )
    _verify_user_info_configured(worktree, runtime=runtime)
    _git("-C", str(worktree), "add", "-A", runtime=runtime)
    if not _has_staged_changes(worktree, runtime=runtime):
        return ""
    _git(
        "-C",
        str(worktree),
        "commit",
        "-m",
        _commit_message(
            origin_rev_id=origin_rev_id,
            source_rev_label=runtime.source_rev_label,
        ),
        runtime=runtime,
    )
    commit = _git(
        "-C", str(worktree), "rev-parse", "HEAD", runtime=runtime
    ).stdout.strip()
    _git(
        "-C",
        str(worktree),
        "push",
        "origin",
        f"HEAD:{destination.branch}",
        runtime=runtime,
    )
    return commit


def _has_staged_changes(worktree: Path, *, runtime: GitRuntime | None = None) -> bool:
    """Return whether `git add -A` staged any content changes."""
    runtime = _runtime(runtime)
    result = runtime.commands.run(
        [runtime.git, "-C", str(worktree), "diff", "--cached", "--quiet"],
        check=False,
    )
    if result.returncode in (0, 1):
        return result.returncode == 1
    raise ExportError("Failed to inspect staged Git changes")


def _verify_user_info_configured(
    worktree: Path, *, runtime: GitRuntime | None = None
) -> None:
    """Reject commits without configured Git committer identity."""
    result = _git("-C", str(worktree), "config", "-l", runtime=_runtime(runtime))
    lines = result.stdout.splitlines()
    name_configured = any(line.startswith("user.name=") for line in lines)
    email_configured = any(line.startswith("user.email=") for line in lines)
    if not name_configured or not email_configured:
        raise ExportError(
            "'user.name' and/or 'user.email' are not configured. Please run "
            "`git config --global SETTING VALUE` to set them"
        )


def _commit_message(
    *, origin_rev_id: str, source_rev_label: str = "Copybarista-Source-Rev"
) -> str:
    """Build a squash commit message with an optional source revision label."""
    summary = "Project import generated by Copybarista."
    if not origin_rev_id:
        return summary
    return f"{summary}\n\n{source_rev_label}: {origin_rev_id}"


def _origin_rev_id(source_ref: Path, *, runtime: GitRuntime | None = None) -> str:
    """Return the Git revision for the source checkout when available."""
    runtime = _runtime(runtime)
    result = runtime.commands.run(
        [runtime.git, "-C", str(source_ref), "rev-parse", "HEAD"],
        check=False,
    )
    if result.returncode == 0:
        return result.stdout.strip()
    return ""


def _git(*args: str, runtime: GitRuntime | None = None) -> CommandResult:
    """Run a Git command through the shared command boundary."""
    runtime = _runtime(runtime)
    return runtime.commands.run([runtime.git, *args])


def _runtime(runtime: GitRuntime | None) -> GitRuntime:
    """Return the supplied Git runtime or the default runtime."""
    return runtime if runtime is not None else GitRuntime()
