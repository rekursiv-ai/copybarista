#!/bin/sh
# ruff: noqa: EXE003, D300, D205 -- Polyglot shell/Python script.
# fmt: off
'''' 2>/dev/null #
exec uv --quiet --project "$(dirname "$0")" run --frozen --no-sync python3 "$0" "$@"
Run a public-to-source Copybarista GitHub sync.

The workflow checks out public base/head trees and a target source checkout,
then calls this script. Keeping the import, validation, branch creation, and PR
body logic here makes the GitHub Action easier to audit and gives us local unit
coverage for the behavior that changes over time.
'''
# fmt: on

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol, TextIO, cast

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib


DEFAULT_RUNNER_TEMP = Path(tempfile.gettempdir())
DEFAULT_SYNC_LABEL: Final = "Copybarista"
DEFAULT_SYNC_USER_EMAIL: Final = "copybarista@example.com"
DEFAULT_SYNC_USER_NAME: Final = "copybarista"
CONTROL_CHAR_BOUND: Final = 32
GITHUB_RETRY_ATTEMPTS: Final = 3
GITHUB_RETRY_DELAY_SEC: Final = 2


def main() -> int:
    """Run the program; return the process exit code."""
    return run()


def run(argv: list[str] | None = None) -> int:
    """Run public-to-source import validation and optional PR creation.

    Args:
      argv: Command-line arguments; None for sys.argv[1:].

    Returns:
      exit_code: 0 on success, nonzero if import/validation/PR fails.

    """
    flags = cast(_Flags, _parser().parse_args(argv))
    if flags.print_synced_base:
        # Resolve the merge baseline and print it for the workflow to consume;
        # no import request is built. The baseline covers BOTH sync directions:
        # the target's import ledger AND exports landed on public main (read
        # from --public-dir, the workflow's full-depth public checkout).
        sys.stdout.write(
            last_synced_public_sha(
                target_dir=Path(flags.target_dir).resolve(),
                sync_label=flags.sync_label,
                base_branch=flags.base_branch,
                fallback=flags.fallback_sha,
                public_dir=(
                    Path(flags.public_dir).resolve() if flags.public_dir else None
                ),
            )
            + "\n",
        )
        return 0
    missing = [
        flag
        for flag, value in (
            ("--project-path", flags.project_path),
            ("--public-base-ref", flags.public_base_ref),
            ("--public-head-ref", flags.public_head_ref),
        )
        if value is None
    ]
    if missing:
        _parser().error(f"{missing[0]} is required for an import")
    if flags.project_path is None:
        raise ValueError("Expected flags.project_path is not None.")
    if flags.public_base_ref is None:
        raise ValueError("Expected flags.public_base_ref is not None.")
    if flags.public_head_ref is None:
        raise ValueError("Expected flags.public_head_ref is not None.")
    # Resolve filesystem inputs to absolute paths. Copybarista subprocesses run
    # with cwd=target_dir; relative path args would otherwise be resolved a
    # second time against that cwd (target/target/...).
    request = ImportRequest(
        public_base=Path(flags.public_base).resolve(),
        public_head=Path(flags.public_head).resolve(),
        target_dir=Path(flags.target_dir).resolve(),
        target_repo=flags.target_repo,
        project_path=Path(flags.project_path),
        base_branch=flags.base_branch,
        public_repo=flags.public_repo,
        public_sha=flags.public_sha,
        public_base_ref=flags.public_base_ref,
        public_head_ref=flags.public_head_ref,
        branch=import_branch_name(
            explicit=flags.branch,
            public_sha=flags.public_sha,
            prefix=flags.branch_prefix,
        ),
        sync_label=flags.sync_label,
        sync_user_name=flags.sync_user_name,
        sync_user_email=flags.sync_user_email,
        report=Path(flags.report).resolve(),
        open_pr=_string_bool(flags.open_pr),
        auto_merge=_string_bool(flags.auto_merge),
        open_pr_only=flags.open_pr_only,
        runner_temp=Path(flags.runner_temp).resolve(),
        validation_commands=tuple(flags.validation_command),
        refresh_public_lockfile=flags.refresh_public_lockfile,
        record_on_validation_failure=_string_bool(
            flags.record_on_validation_failure,
        ),
    )
    run_import_sync(request)
    return 0


@dataclass(frozen=True, slots=True, kw_only=True)
class ImportRequest:
    """Typed namespace for one import sync run."""

    public_base: Path
    public_head: Path
    target_dir: Path
    target_repo: str
    project_path: Path
    base_branch: str
    public_repo: str
    public_sha: str
    public_base_ref: str
    public_head_ref: str
    branch: str
    sync_label: str
    sync_user_name: str
    sync_user_email: str
    report: Path
    open_pr: bool
    open_pr_only: bool
    auto_merge: bool = True
    runner_temp: Path
    validation_commands: tuple[str, ...]
    refresh_public_lockfile: bool
    record_on_validation_failure: bool = False

    @property
    def validation_failure_marker(self) -> Path:
        """Return the marker that preserves a red validation status."""
        return Path(f"{self.report}.validation-failed")


def run_import_sync(request: ImportRequest) -> None:
    """Public→Private into source, validate, and optionally open a PR.

    Args:
      request: ImportRequest with paths, refs, and sync settings.

    """
    project = request.target_dir / request.project_path
    if request.open_pr_only:
        _log("Opening or updating target import PR.")
        _open_or_update_target_pr(request=request)
        return
    requirements = _export_copybarista_requirements(
        target_dir=request.target_dir,
        runner_temp=request.runner_temp,
    )
    # An import failure lands no ledger marker, and the export guard reads that
    # ledger. Validation differs only for a public-main push: that commit is
    # already authoritative, so its import must land before the red status is
    # reported back to GitHub.
    try:
        _log("Importing public changes into target source.")
        _run_import_change(request=request, project=project, requirements=requirements)
    except BaseException:
        _log_missing_import(request)
        raise
    if request.record_on_validation_failure:
        request.validation_failure_marker.unlink(missing_ok=True)
    try:
        _log("Validating target checkout.")
        _validate_target(
            request=request,
            project=project,
            validation_commands=request.validation_commands,
            runner_temp=request.runner_temp,
            requirements=requirements,
        )
    except BaseException:
        if not request.record_on_validation_failure:
            _log_missing_import(request)
            raise
        request.validation_failure_marker.write_text(
            "validation failed\n",
            encoding="utf-8",
        )
        _log(
            f"::error::Validation failed for public commit "
            f"{request.public_sha[:12]}; its authoritative public-main import "
            "will still be recorded before this workflow reports failure.",
        )
    if request.open_pr:
        _log("Opening or updating target import PR.")
        _open_or_update_target_pr(request=request)


def import_change_pr_body(
    *,
    public_repo: str,
    public_sha: str,
    public_base_ref: str,
    public_head_ref: str,
    source_base_ref: str,
    sync_label: str,
) -> str:
    """Return the target import-change PR body.

    Args:
      public_repo: Public repository URL or name.
      public_sha: Full 40-character SHA being imported.
      public_base_ref: Public merge base branch/ref.
      public_head_ref: Public source branch/ref.
      source_base_ref: Target merge base branch.
      sync_label: Sync label (Copybarista, Wesearch, etc.).

    Returns:
      pr_body: Markdown PR description with import metadata.

    """
    return (
        f"Imports {sync_label} public repository changes into the source repository.\n\n"
        f"- Public repository: `{public_repo}`\n"
        f"- Public SHA: `{public_sha}`\n"
        f"- Public base: `{public_base_ref}`\n"
        f"- Public head: `{public_head_ref}`\n"
        f"- Source base: `{source_base_ref}`\n"
        "- Import report: generated by `copybarista import-change`\n"
        "\n"
        "Regenerate this PR before merging if source `main` changes.\n"
    )


def import_branch_name(*, explicit: str, public_sha: str, prefix: str) -> str:
    """Return the public-to-source sync branch name."""
    if explicit.strip():
        return _validated_generated_branch(branch=explicit.strip(), prefix=prefix)
    branch = f"{prefix}sha-{_branch_component(public_sha[:12])}"
    return _validated_generated_branch(branch=branch, prefix=prefix)


class ImportBaseError(RuntimeError):
    """Raised when the target records no prior import to use as a baseline."""


def import_commit_subject_prefix(sync_label: str) -> str:
    """Return the fixed prefix of a landed import's commit subject."""
    return f"Import {sync_label} public changes "


def import_commit_subject(sync_label: str, public_sha: str) -> str:
    """Return the ledger subject, refusing one the baseline walk cannot read.

    This subject IS the ledger: :func:`last_synced_public_sha` re-reads the
    imported SHA out of it to pick the next merge baseline, and the export
    guard asks that walk whether a force-write would revert public work. So a
    subject the walk cannot parse does not merely lose one import -- the
    project stops exporting, with every run still reporting success.

    Validating at the WRITE side is what makes that unrepresentable. The read
    side already anchors on ``[0-9a-f]{40}``; composing a subject and pushing
    it without checking left an abbreviated or reworded one to fail silently
    hours later, in a different repository, as a skipped export.

    Args:
      sync_label: Import label, e.g. ``Wesearch``.
      public_sha: Full 40-character public commit SHA being imported.

    Returns:
      subject: The commit subject to write.

    Raises:
      ImportBaseError: The composed subject would not survive the walk.

    """
    subject = import_commit_subject_prefix(sync_label) + public_sha
    if _import_subject_pattern(sync_label).match(subject) is None:
        raise ImportBaseError(
            f"Refusing to write an unreadable import ledger subject: "
            f"{subject!r}. The baseline walk requires the full 40-character "
            f"SHA, so this would silently wedge {sync_label} exports.",
        )
    return subject


def export_commit_marker(sync_label: str) -> str:
    """Return the body line that identifies a landed public export commit.

    A source->public export lands on public ``main`` by squash-merge, whose
    body is exactly ``<label> export branch: <branch>`` (see
    ``sync_export_pr._enable_export_pr_auto_merge``). The per-branch
    ``copybarista-source-rev-sha256=`` digests live on the export BRANCH commit
    and do not survive the squash, so this branch line -- the same marker the
    import workflow's own ``if:`` guard greps out of the pushed commit message
    -- is the only export identity that reaches public ``main``.

    Args:
      sync_label: Export label, e.g. ``Sagent``.

    Returns:
      marker: The literal substring that marks a landed export commit.

    """
    return f"{sync_label} export branch: "


def last_synced_public_sha(
    *,
    target_dir: Path,
    sync_label: str,
    base_branch: str,
    fallback: str = "",
    public_dir: Path | None = None,
) -> str:
    """Return the newest public SHA the target source already reflects.

    The merge-import baseline must be the public commit the target tree
    currently reflects, not the pushed commit's parent. Those diverge whenever
    an import fails to land (validation error, unmerged PR, conflict): the
    parent marches forward while the target stays pinned to its last successful
    import, so a parent-based baseline feeds the three-way merge a wrong common
    ancestor and manufactures spurious conflicts.

    The target reflects public content by TWO paths, and a correct baseline
    covers both:

    * Landed imports record their public SHA in the target commit subject
      (``Import <label> public changes <sha>``), read from target history.
    * Source->public EXPORTS advance public ``main`` with no target ledger
      entry at all. After months of exports and no import, a ledger-only
      baseline is months stale and re-presents already-exported work as
      conflicts -- the live 39-file incident.

    So when ``public_dir`` is given (the import workflow's full-depth public
    checkout), walk public history newest-first and return the newest commit
    that is either the ledger SHA or a landed export commit whose tree exists
    on the named export branch. The branch-tree check prevents arbitrary commit
    text from claiming export provenance. Without ``public_dir`` the resolution
    is ledger-only, preserving the open-PR path that has no public checkout.

    Args:
      target_dir: Root of the target repository checkout.
      sync_label: Import label, e.g. ``Sagent``; scopes the commit search.
      base_branch: Branch to walk (target ledger, and public when given).
      fallback: SHA to return when neither history records a sync -- a
        first-sync baseline (e.g. the branch tip before the push). Empty
        string means raise.
      public_dir: Full-depth public checkout. When set, exports on public
        ``main`` are eligible baselines, not just target-ledger imports.

    Returns:
      sha: The most recently synced public SHA, or ``fallback`` when neither
        history records a sync and ``fallback`` is set.

    Raises:
      ImportBaseError: When no sync is found and no fallback is provided.

    """
    ledger_sha = _ledger_public_sha(
        target_dir=target_dir,
        sync_label=sync_label,
        base_branch=base_branch,
    )
    if public_dir is not None:
        newest = _newest_synced_public_sha(
            public_dir=public_dir,
            base_branch=base_branch,
            sync_label=sync_label,
            ledger_sha=ledger_sha,
        )
        if newest:
            return newest
    if ledger_sha:
        return ledger_sha
    if fallback:
        return fallback
    raise ImportBaseError(
        f"No landed '{sync_label}' import or export commit found on "
        f"'{base_branch}'; cannot resolve the merge baseline.",
    )


def _gh_pr_exists(*, branch: str, repo: str, cwd: Path) -> bool:
    """Return whether GitHub has an open PR for a branch."""
    result = _run_gh(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            repo,
            "--state",
            "open",
            "--head",
            branch,
            "--json",
            "number",
        ],
        cwd=cwd,
        capture=True,
    )
    # Gh emits a JSON array; "[]" means no open PR. Compare the compact form
    # rather than parsing, so this standalone helper needs no typed JSON codec.
    return json.dumps(json.loads(result.stdout), separators=(",", ":")) != "[]"


def _fetch_branch(*, branch: str, cwd: Path) -> None:
    """Fetch a remote branch if it exists without failing on first import."""
    _run(
        [
            "git",
            "fetch",
            "origin",
            f"refs/heads/{branch}:refs/remotes/origin/{branch}",
        ],
        cwd=cwd,
        check=False,
    )


def _string_bool(value: str) -> bool:
    """Parse Action-style boolean strings."""
    return value.lower() in {"1", "true", "yes"}


def _branch_component(value: str) -> str:
    """Sanitize arbitrary run metadata for use in a Git branch name."""
    return "".join(char if char.isalnum() or char in "-._" else "-" for char in value)


def _validated_generated_branch(*, branch: str, prefix: str) -> str:
    """Return a safe generated branch name or exit with a usage error."""
    if not branch.startswith(prefix):
        sys.stderr.write(f"Branch must start with {prefix}\n")
        raise SystemExit(2)
    if not _valid_git_branch_name(branch):
        sys.stderr.write(f"Invalid generated branch name: {branch}\n")
        raise SystemExit(2)
    return branch


def _valid_git_branch_name(branch: str) -> bool:
    """Return whether a branch name is safe for force-updated sync branches."""
    if branch in {"main", "master"} or branch.startswith(("-", "/")):
        return False
    if branch.endswith(("/", ".", ".lock")):
        return False
    if ".." in branch or "//" in branch or "@{" in branch:
        return False
    forbidden = set(" ~^:?*[\\")
    return not any(
        char in forbidden or ord(char) < CONTROL_CHAR_BOUND for char in branch
    )


# Every command here runs through ``uv``, which re-derives the environment from the
# target ``--project`` / cwd. An inherited ``VIRTUAL_ENV`` (e.g. the operator's
# activated loop venv) only triggers uv's "does not match the project environment"
# warning, so drop it for a clean run.
def _child_env() -> dict[str, str]:
    """Return the parent env without ``VIRTUAL_ENV``."""
    env = dict(os.environ)
    env.pop("VIRTUAL_ENV", None)
    return env


def _run(
    argv: list[str],
    *,
    cwd: Path | None = None,
    stdout: TextIO | int | None = None,
    check: bool = True,
    capture: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run a subprocess while streaming commands for Action logs."""
    _log("+ " + " ".join(argv))
    # The caller provides an argument vector, not a shell string.
    result = subprocess.run(  # noqa: S603 -- This sync script passes validated argument vectors without invoking a shell, so command structure is not user-controlled.
        argv,
        cwd=cwd,
        check=False,
        stdout=subprocess.PIPE if capture else stdout,
        stderr=subprocess.PIPE if capture else None,
        text=True,
        env=_child_env(),
    )
    if check and result.returncode != 0:
        raise SystemExit(result.returncode)
    return result


# With ``check=False`` a non-retryable failure is returned to the caller instead of
# raising, so the caller can inspect stderr and recover (the auto-merge fallback reads
# it to detect a repo that cannot defer merges).
def _run_gh(
    argv: list[str],
    *,
    cwd: Path,
    capture: bool = False,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run a GitHub CLI command with retries for transient API failures."""
    for attempt in range(1, GITHUB_RETRY_ATTEMPTS + 1):
        result = _run(argv, cwd=cwd, check=False, capture=True)
        if result.returncode == 0:
            if not capture:
                _write_process_output(result)
            return result
        if attempt == GITHUB_RETRY_ATTEMPTS or not _retryable_github_failure(result):
            if not check:
                return result
            _write_process_output(result)
            raise SystemExit(result.returncode)
        _log(
            "GitHub CLI command failed with a transient API error; "
            f"retrying in {GITHUB_RETRY_DELAY_SEC} seconds "
            f"({attempt}/{GITHUB_RETRY_ATTEMPTS}).",
        )
        time.sleep(GITHUB_RETRY_DELAY_SEC)
    raise AssertionError("unreachable")


def _retryable_github_failure(result: subprocess.CompletedProcess[str]) -> bool:
    """Return whether a GitHub CLI failure is likely transient."""
    output = f"{result.stdout}\n{result.stderr}".casefold()
    return any(
        token in output
        for token in (
            "http 5",
            "timeout",
            "timed out",
            "try resubmitting",
            "temporarily unavailable",
        )
    )


def _write_process_output(result: subprocess.CompletedProcess[str]) -> None:
    """Replay captured process output to the workflow log."""
    if result.stdout:
        sys.stdout.write(result.stdout)
    if result.stderr:
        sys.stderr.write(result.stderr)


# Diagnostics go to stderr so stdout stays a clean machine-readable channel (``--print-
# synced-base`` emits only the resolved SHA there). GitHub merges both streams into the
# Action log, so human-facing output is unchanged.
def _log(message: str) -> None:
    """Write one flushed workflow log line to stderr."""
    sys.stderr.write(f"{message}\n")
    sys.stderr.flush()


# `uv run --no-project` skips monorepo-root resolution (no torch/jax/tf/ pycairo build);
# --with-requirements supplies copybarista's only third-party deps (see
# _export_copybarista_requirements). The package imports under its monorepo path, so
# callers run this with cwd=target_dir. A project dependency-group cannot install here
# because --group is ignored under --no-project; the group is instead exported to a
# requirements file.
#
# `requirements` must be absolute: callers run with cwd=target_dir, so a relative path
# would be resolved twice (target/target/...).
def _copybarista_argv(*, requirements: Path) -> list[str]:
    """Return the argv prefix that runs copybarista dependency-free."""
    return [
        "uv",
        "--quiet",
        "run",
        "--no-project",
        "--with-requirements",
        str(requirements),
        "python",
        "-m",
        # Dotted module path of the copybarista package, run via `python -m` with
        # cwd=target_dir so the import resolves under the monorepo checkout.
        "copybarista",
    ]


# `uv run --with-requirements` consults no lockfile, so to keep copybarista's standalone
# deps reproducible (and matched to the monorepo) we export the `copybarista` group from
# the checkout's uv.lock into a requirements file and feed that. This is the single
# source of truth -- no separately maintained version pins.
def _export_copybarista_requirements(*, target_dir: Path, runner_temp: Path) -> Path:
    """Export the `copybarista` dependency group from the lock to a pinned file."""
    requirements = (runner_temp / "copybarista-requirements.txt").resolve()
    requirements.write_text(
        _run(
            [
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
            ],
            cwd=target_dir,
            capture=True,
        ).stdout,
        encoding="utf-8",
    )
    return requirements


def _parser() -> argparse.ArgumentParser:
    """Build the public-to-source sync CLI parser."""
    parser = argparse.ArgumentParser(
        description=(__doc__ or "").split("\n", 2)[2],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--public-base", default="public-base")
    parser.add_argument("--public-head", default="public-head")
    parser.add_argument("--target-dir", default="target")
    parser.add_argument("--target-repo", default=os.environ.get("TARGET_REPO", ""))
    # Not argparse-required: --print-synced-base resolves the baseline from the
    # target history alone and needs none of the import-only arguments. The
    # import path validates their presence in main().
    parser.add_argument("--project-path")
    parser.add_argument("--base-branch", default=os.environ.get("BASE_BRANCH", "main"))
    parser.add_argument(
        "--public-repo",
        default=os.environ.get("GITHUB_REPOSITORY", ""),
    )
    parser.add_argument("--public-sha", default=os.environ.get("GITHUB_SHA", "manual"))
    parser.add_argument(
        "--sync-user-name",
        default=os.environ.get("COPYBARISTA_SYNC_USER_NAME", DEFAULT_SYNC_USER_NAME),
    )
    parser.add_argument(
        "--sync-user-email",
        default=os.environ.get("COPYBARISTA_SYNC_USER_EMAIL", DEFAULT_SYNC_USER_EMAIL),
    )
    parser.add_argument("--public-base-ref")
    parser.add_argument("--public-head-ref")
    parser.add_argument(
        "--branch",
        default=os.environ.get("COPYBARISTA_IMPORT_BRANCH", ""),
    )
    parser.add_argument(
        "--branch-prefix",
        default=os.environ.get(
            "COPYBARISTA_IMPORT_BRANCH_PREFIX",
            "copybarista/import/",
        ),
    )
    parser.add_argument(
        "--sync-label",
        default=os.environ.get("COPYBARISTA_SYNC_LABEL", DEFAULT_SYNC_LABEL),
    )
    parser.add_argument(
        "--report",
        default=os.environ.get("IMPORT_REPORT", "import-report.json"),
    )
    parser.add_argument("--open-pr", default="false")
    parser.add_argument(
        # Defaults ON: a clean import carries no decision a human can improve,
        # and an unmerged one blocks the export in the other direction. A
        # conflicting import never reaches the merge -- it raises first.
        "--auto-merge",
        default=os.environ.get("COPYBARISTA_AUTO_MERGE", "true"),
    )
    parser.add_argument(
        "--open-pr-only",
        action="store_true",
        help="Only create or update the source PR for already-imported changes.",
    )
    parser.add_argument(
        "--runner-temp",
        default=os.environ.get("RUNNER_TEMP", str(DEFAULT_RUNNER_TEMP)),
    )
    parser.add_argument(
        "--validation-command",
        action="append",
        default=[],
        help=(
            "Shell command run in the exported public tree to validate the "
            "imported change. Repeat for the full validation set. These are the "
            "single source of truth (copybarista.sync.toml "
            "sync.validation_commands) that also drives the public repository's "
            "package-validation.yml."
        ),
    )
    parser.add_argument(
        "--refresh-public-lockfile",
        action="store_true",
        help="Ignore generated public uv.lock while importing source-owned changes.",
    )
    parser.add_argument(
        "--record-on-validation-failure",
        default=("true" if os.environ.get("GITHUB_EVENT_NAME") == "push" else "false"),
        help=(
            "Record an already-public main-branch commit even when validation "
            "fails. Defaults on for push events so reruns of older workflows "
            "retain authoritative-public-main semantics."
        ),
    )
    parser.add_argument(
        "--print-synced-base",
        action="store_true",
        help=(
            "Print the newest public SHA the target has imported or exported "
            "and exit. Used to resolve the merge baseline before the import."
        ),
    )
    parser.add_argument(
        "--fallback-sha",
        default="",
        help=(
            "With --print-synced-base: SHA to print when the target has no "
            "landed import yet (a first-import baseline, e.g. the branch tip "
            "before the push)."
        ),
    )
    parser.add_argument(
        "--public-dir",
        default="",
        help=(
            "With --print-synced-base: a full-depth public checkout. When set, "
            "exports landed on public main are eligible baselines (not just "
            "target-ledger imports), so a long export-only streak no longer "
            "yields a stale baseline that manufactures merge conflicts."
        ),
    )
    return parser


def _run_import_change(
    *,
    request: ImportRequest,
    project: Path,
    requirements: Path,
) -> None:
    """Run `copybarista import-change` and capture its JSON report."""
    request.report.parent.mkdir(parents=True, exist_ok=True)
    with (
        tempfile.TemporaryDirectory(
            prefix="copybarista-import-public-",
            dir=request.runner_temp,
        ) as tmp,
        request.report.open("w", encoding="utf-8") as output,
    ):
        public_base = _public_tree_for_import(
            source=request.public_base,
            destination=Path(tmp) / "public-base",
            refresh_public_lockfile=request.refresh_public_lockfile,
        )
        public_head = _public_tree_for_import(
            source=request.public_head,
            destination=Path(tmp) / "public-head",
            refresh_public_lockfile=request.refresh_public_lockfile,
        )
        # All path args are absolute, so running with cwd=target_dir is safe.
        _run(
            [
                *_copybarista_argv(requirements=requirements),
                "import-change",
                str(project / "copy.barista.toml"),
                "--public-base",
                str(public_base),
                "--public-head",
                str(public_head),
                "--source-base",
                str(request.target_dir),
                "--destination",
                str(request.target_dir),
                # Merge imports tolerate a source that has drifted ahead of the
                # public base (e.g. a change applied to source before its public
                # commit imports), reconciling each file by three-way merge
                # instead of demanding exact base reproduction.
                #
                # Not equivalent to strict even with no drift: strict also runs
                # the injective-reverse guard, which merge replaces with a
                # per-file comparison against the source's own export.
                "--merge-import",
                "--json",
            ],
            stdout=output,
            cwd=request.target_dir,
        )


def _public_tree_for_import(
    *,
    source: Path,
    destination: Path,
    refresh_public_lockfile: bool,
) -> Path:
    """Return a public tree suitable for source-owned import verification."""
    if not refresh_public_lockfile:
        return source
    # The root lock is generated after export. Nested locks belong to shipped
    # examples or workspaces and remain source-owned import inputs.
    shutil.copytree(
        source,
        destination,
        symlinks=True,
        ignore=shutil.ignore_patterns(".git"),
    )
    (destination / "uv.lock").unlink(missing_ok=True)
    return destination


# The imported tree under ``project`` is monorepo-form (``loop.*`` imports, monorepo-
# root deps). Validating it directly would require the monorepo environment -- including
# the ML stack (torch, jax, tf, pycairo, ...) a CPU import runner cannot build. Instead
# this exports the public-form tree (the artifact actually published: ``sagent.*``
# imports, public ``pyproject.toml`` / ``uv.lock``) and runs the validation commands
# against *that*. The exported env has the package's real runtime deps but never the
# monorepo ML stack, so a CPU runner validates exactly what ships.
#
# ``validation_commands`` is the single source of truth (``copybarista.sync.toml``
# ``sync.validation_commands``) that also drives the public repository's ``package-
# validation.yml`` and the source-to-public export gate. Running the same shell commands
# here makes all three verify byte-identical checks. Each command runs through ``bash
# -c`` in the exported tree, so it self-contains its ``uv sync`` and may use shell
# features.
def _validate_target(
    *,
    request: ImportRequest,
    project: Path,
    validation_commands: tuple[str, ...],
    runner_temp: Path,
    requirements: Path,
) -> None:
    """Validate the imported change by checking its exported public tree."""
    tree = _export_public_tree(
        request=request,
        project=project,
        runner_temp=runner_temp,
        requirements=requirements,
    )
    for command in validation_commands:
        _run(["bash", "-c", command], cwd=tree)


# Runs ``copybarista export`` (dependency-free) against the post-import source checkout,
# producing the transformed public package (``sagent.*`` imports, public
# ``pyproject.toml`` / ``uv.lock``) under a fresh directory.
def _export_public_tree(
    *,
    request: ImportRequest,
    project: Path,
    runner_temp: Path,
    requirements: Path,
) -> Path:
    """Export the public-form tree for the imported project and return its path."""
    tree = runner_temp / "copybarista-validation-tree"
    if tree.exists():
        shutil.rmtree(tree)
    _run(
        [
            *_copybarista_argv(requirements=requirements),
            "export",
            str(project / "copy.barista.toml"),
            str(request.target_dir),
            "--folder-dir",
            str(tree),
            "--force",
        ],
        cwd=request.target_dir,
    )
    # `copybarista export` writes a plain directory, but the caller then runs
    # `sync.validation_commands` here -- and since those became
    # `pre-commit run --all-files` (previously ruff/ty/pytest invoked directly),
    # they need a git repo: pre-commit resolves `--all-files` through git and
    # otherwise aborts with "git failed. Is it installed, and are you in a Git
    # repository directory?". The list's other two consumers (the public repo's
    # package-validation.yml and the export gate) run inside a real checkout, so
    # only this path has to supply one. Staging is what makes the files visible
    # to `--all-files`; no commit is needed, and the tree is discarded after.
    if request.refresh_public_lockfile:
        # The committed lock pins branch-tracking siblings at a stale SHA; the
        # export relocks them before validating, so validating the pin here
        # failed imports on sibling bugs already fixed on their branch.
        _run(["uv", "lock", *_git_branch_upgrades(tree / "pyproject.toml")], cwd=tree)
    _run(["git", "init", "--quiet"], cwd=tree)
    _run(["git", "add", "--all"], cwd=tree)
    return tree


# Mirrors ``sync_export_pr._git_branch_upgrades``. This script runs as a detached
# ``--no-project`` copy with only the stdlib, so it cannot import that module.
def _git_branch_upgrades(pyproject: Path) -> list[str]:
    """Return ``--upgrade-package`` flags for every git-branch dependency."""
    sources: object = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    for key in ("tool", "uv", "sources"):
        sources = cast(dict[str, object], sources).get(key, {})
    return [
        flag
        for name, source in cast(dict[str, object], sources).items()
        if isinstance(source, dict) and "branch" in source
        for flag in ("--upgrade-package", name)
    ]


# Commits even when the merge produced no file changes. Two different questions share
# this path: "is there a diff to review?" and "has the source absorbed this public
# SHA?". Only the first is answered by an empty tree. The export guard asks the second,
# and answers it by searching target history for this commit's subject -- so returning
# early here left the guard reading "not imported" for content the source demonstrably
# already had.
#
# An empty diff at this point is the strongest possible yes: the three-way merge applied
# without conflict and ``_validate_target`` passed the full gate suite on the exported
# result. A transform that dropped content, or a wrong ``--public-base``, raises before
# reaching this function.
def _open_or_update_target_pr(*, request: ImportRequest) -> None:
    """Commit the import and create or update the target PR."""
    if not _git_has_changes(path=request.target_dir, rel=request.project_path):
        _log("Import produced no target changes; recording the SHA anyway.")

    branch = request.branch
    source_base_ref = _run(
        ["git", "rev-parse", "HEAD"],
        cwd=request.target_dir,
        capture=True,
    ).stdout.strip()
    body_file = request.runner_temp / "copybarista-import-change-pr-body.md"
    body_file.write_text(
        import_change_pr_body(
            public_repo=request.public_repo,
            public_sha=request.public_sha,
            public_base_ref=request.public_base_ref,
            public_head_ref=request.public_head_ref,
            source_base_ref=source_base_ref,
            sync_label=request.sync_label,
        ),
        encoding="utf-8",
    )

    _run(["git", "config", "user.name", request.sync_user_name], cwd=request.target_dir)
    _run(
        ["git", "config", "user.email", request.sync_user_email],
        cwd=request.target_dir,
    )
    _fetch_branch(branch=branch, cwd=request.target_dir)
    _run(["git", "switch", "-C", branch], cwd=request.target_dir)
    _run(["git", "add", str(request.project_path)], cwd=request.target_dir)
    _run(
        [
            "git",
            "commit",
            # The commit IS the ledger, so it must exist even with no diff.
            "--allow-empty",
            "--author",
            _commit_author(request.sync_user_name, request.sync_user_email),
            "-m",
            import_commit_subject(request.sync_label, request.public_sha),
        ],
        cwd=request.target_dir,
    )
    _run(
        ["git", "push", "--force-with-lease", "origin", branch],
        cwd=request.target_dir,
    )

    # Full SHA, not abbreviated: this subject IS the ledger. The export reads
    # the imported commit back out of it to decide whether a force-write would
    # revert public work, and an abbreviated SHA parses as no marker at all.
    title = f"Import {request.sync_label} public changes {_pr_title_sha(request.public_sha)}"
    if _gh_pr_exists(branch=branch, repo=request.target_repo, cwd=request.target_dir):
        _run_gh(
            [
                "gh",
                "pr",
                "edit",
                branch,
                "--repo",
                request.target_repo,
                "--title",
                title,
                "--body-file",
                str(body_file),
            ],
            cwd=request.target_dir,
        )
    else:
        _run_gh(
            [
                "gh",
                "pr",
                "create",
                "--repo",
                request.target_repo,
                "--base",
                request.base_branch,
                "--head",
                branch,
                "--title",
                title,
                "--body-file",
                str(body_file),
            ],
            cwd=request.target_dir,
        )
    if request.auto_merge:
        _merge_import_pr(
            branch=branch,
            target_repo=request.target_repo,
            title=title,
            sync_label=request.sync_label,
            cwd=request.target_dir,
        )


def _pr_title_sha(public_sha: str) -> str:
    """Return the SHA form the import PR title carries, which the ledger parses back."""
    return public_sha


# A clean import needs no human decision. The public change is already reviewed and
# already published; the source is the authority on how it RENDERS, not a second
# approval gate. Waiting for a click is also what stalls the other direction, since the
# export refuses to run while an import is outstanding.
#
# A conflicting import never reaches here -- it raises before the PR is opened. A
# validation failure reaches this only for a commit already merged to public main; that
# commit is authoritative and still needs its ledger entry.
#
# ``--admin`` merges without waiting on source CI. Source-only breakage (a loop caller
# of a moved API, a house-lint rule the public repo does not run) otherwise leaves the
# import PR red, and the export guard then blocks every export behind it. Landing it
# lets source CI on ``main`` report the breakage instead of jamming both directions.
#
# A token without bypass falls back to ``--auto`` (waits for checks); a repo with no
# deferrable merge rejects that too, and the merge is issued directly.
def _merge_import_pr(
    *,
    branch: str,
    target_repo: str,
    title: str,
    sync_label: str,
    cwd: Path,
) -> None:
    """Merge the import PR, preferring auto-merge."""
    merge_argv = [
        "gh",
        "pr",
        "merge",
        branch,
        "--repo",
        target_repo,
        "--squash",
        "--subject",
        title,
        "--body",
        f"{sync_label} import branch: {branch}",
    ]
    result = _run_gh([*merge_argv, "--admin"], cwd=cwd, capture=True, check=False)
    if result.returncode == 0:
        return
    _log("Admin merge unavailable; queueing auto-merge behind source checks.")
    result = _run_gh([*merge_argv, "--auto"], cwd=cwd, capture=True, check=False)
    if result.returncode == 0:
        return
    output = f"{result.stdout}\n{result.stderr}".casefold()
    if (
        "protected branch rules not configured" in output
        or "enablepullrequestautomerge" in output
    ):
        _log(
            "Auto-merge unavailable (no branch protection / pending checks); "
            "merging the import PR directly.",
        )
        _run_gh(merge_argv, cwd=cwd)
        return
    raise SystemExit(result.returncode)


def _commit_author(name: str, email: str) -> str:
    """Return the Git author identity for a generated sync commit."""
    return f"{name} <{email}>"


def _git_has_changes(*, path: Path, rel: Path) -> bool:
    """Return whether a checkout has changes under a relative path."""
    result = _run(
        ["git", "status", "--porcelain", str(rel)],
        cwd=path,
        check=False,
        capture=True,
    )
    return bool(result.stdout.strip())


# One expression, so the two directions cannot drift: a subject the writer accepts is by
# construction one the walk can read.
def _import_subject_pattern(sync_label: str) -> re.Pattern[str]:
    """Return the regex both the writer and the baseline walk agree on."""
    prefix = import_commit_subject_prefix(sync_label)
    # ``(#N)`` is GitHub's squash-merge suffix, which is how these imports land.
    return re.compile(rf"^{re.escape(prefix)}([0-9a-f]{{40}})(?: \(#\d+\))?$")


class _Flags(Protocol):
    """Parsed command-line flags."""

    public_base: str
    public_head: str
    target_dir: str
    target_repo: str
    project_path: str | None
    base_branch: str
    public_repo: str
    public_sha: str
    sync_user_name: str
    sync_user_email: str
    public_base_ref: str | None
    public_head_ref: str | None
    branch: str
    branch_prefix: str
    sync_label: str
    report: str
    open_pr: str
    auto_merge: str
    open_pr_only: bool
    runner_temp: str
    validation_command: list[str]
    refresh_public_lockfile: bool
    record_on_validation_failure: str
    print_synced_base: bool
    fallback_sha: str
    public_dir: str


# Walks the target branch newest-first for ``Import <label> public changes <sha>``
# subjects and returns the first full SHA, or the empty string when the branch records
# no landed import.
def _ledger_public_sha(*, target_dir: Path, sync_label: str, base_branch: str) -> str:
    """Return the newest public SHA in the target's import ledger, or ``""``."""
    prefix = import_commit_subject_prefix(sync_label)
    # --fixed-strings: sync_label is matched literally, never as a git BRE, so a
    # label with regex metacharacters cannot broaden or break the search.
    subjects = _run(
        [
            "git",
            "log",
            base_branch,
            "--fixed-strings",
            f"--grep={prefix}",
            "--format=%s",
        ],
        cwd=target_dir,
        capture=True,
    ).stdout.splitlines()
    # Shared with the writer, so a subject one accepts is one the other reads.
    pattern = _import_subject_pattern(sync_label)
    for subject in subjects:
        match = pattern.match(subject)
        if match is not None:
            sha = match[1]
            assert isinstance(sha, str)
            return sha
    return ""


# Walks public ``base_branch`` newest-first and returns the first commit proven to be
# reflected by source: the target ledger SHA, or an export whose tree is reachable from
# its named export branch. Commit text selects a candidate; the branch tree authenticates
# its provenance.
def _newest_synced_public_sha(
    *,
    public_dir: Path,
    base_branch: str,
    sync_label: str,
    ledger_sha: str,
) -> str:
    """Return the newest public commit the source already reflects, or ``""``."""
    records = _run(
        ["git", "log", base_branch, "-z", "--format=%H%x00%T%x00%B"],
        cwd=public_dir,
        capture=True,
    ).stdout.split("\0")
    for index in range(0, len(records) - 2, 3):
        commit_sha = records[index].strip()
        tree_sha = records[index + 1].strip()
        message = records[index + 2]
        if commit_sha == ledger_sha:
            return commit_sha
        branch = _export_branch_from_message(message=message, sync_label=sync_label)
        if branch and _branch_contains_tree(
            public_dir=public_dir,
            branch=branch,
            tree_sha=tree_sha,
        ):
            return commit_sha
    return ""


def _export_branch_from_message(*, message: str, sync_label: str) -> str:
    """Return the well-formed export branch claimed by a commit message."""
    marker = re.escape(export_commit_marker(sync_label))
    match = re.search(rf"(?m)^{marker}([^\r\n]+)$", message)
    if match is None:
        return ""
    branch = match[1].strip()
    assert isinstance(branch, str)
    if "/export/" not in branch or not _valid_git_branch_name(branch):
        return ""
    return branch


def _branch_contains_tree(*, public_dir: Path, branch: str, tree_sha: str) -> bool:
    """Return whether an export branch's history contains a public tree."""
    remote_ref = f"refs/remotes/origin/{branch}"
    # actions/checkout fetches only its requested ref, even with full history.
    _run(
        [
            "git",
            "fetch",
            "--no-tags",
            "origin",
            f"+refs/heads/{branch}:{remote_ref}",
        ],
        cwd=public_dir,
        check=False,
        capture=True,
    )
    for ref in (remote_ref, f"refs/heads/{branch}"):
        result = _run(
            ["git", "log", ref, "--format=%T"],
            cwd=public_dir,
            check=False,
            capture=True,
        )
        if result.returncode == 0 and tree_sha in result.stdout.splitlines():
            return True
    return False


def _log_missing_import(request: ImportRequest) -> None:
    """Annotate a failure that leaves the import ledger missing."""
    _log(
        f"::error::The {request.sync_label} import of public commit "
        f"{request.public_sha[:12]} failed, so no import is recorded for it. "
        "Until one lands, the export guard blocks every "
        f"{request.sync_label} export and the public repository stops receiving "
        "updates.",
    )


if __name__ == "__main__":
    raise SystemExit(main())
# vim: ft=python
