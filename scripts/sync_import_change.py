#!/bin/sh
# ruff: noqa: EXE003, D300 -- Polyglot shell/Python script.
# fmt: off
'''' 2>/dev/null #
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../../../.." && pwd)"
exec env PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}" uv --quiet run --no-project --with pyyaml --with ruff python3 "$0" "$@"
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
from typing import TextIO

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time


DEFAULT_RUNNER_TEMP = Path(tempfile.gettempdir())
DEFAULT_SYNC_LABEL = "Copybarista"
DEFAULT_SYNC_USER_EMAIL = "copybarista@example.com"
DEFAULT_SYNC_USER_NAME = "copybarista"
DEFAULT_IMPORT_BRANCH_PREFIX = "copybarista/import/"
CONTROL_CHAR_BOUND = 32
DEFAULT_TYPE_CHECK_TARGETS = (".",)
GITHUB_RETRY_ATTEMPTS = 3
GITHUB_RETRY_DELAY_SEC = 2


def main(argv: list[str] | None = None) -> None:
    """Run public-to-source import validation and optional PR creation."""
    args = _parser().parse_args(argv)
    request = ImportRequest(
        public_base=Path(args.public_base),
        public_head=Path(args.public_head),
        target_dir=Path(args.target_dir),
        target_repo=args.target_repo,
        project_path=Path(args.project_path),
        copybarista_project_path=Path(args.copybarista_project_path)
        if args.copybarista_project_path
        else Path(args.project_path),
        base_branch=args.base_branch,
        public_repo=args.public_repo,
        public_sha=args.public_sha,
        public_base_ref=args.public_base_ref,
        public_head_ref=args.public_head_ref,
        branch=import_branch_name(
            explicit=args.branch,
            public_sha=args.public_sha,
            prefix=args.branch_prefix,
        ),
        sync_label=args.sync_label,
        sync_user_name=args.sync_user_name,
        sync_user_email=args.sync_user_email,
        report=Path(args.report),
        open_pr=_string_bool(args.open_pr),
        open_pr_only=args.open_pr_only,
        runner_temp=Path(args.runner_temp),
        type_check_targets=tuple(args.type_check_target) or DEFAULT_TYPE_CHECK_TARGETS,
        refresh_public_lockfile=args.refresh_public_lockfile,
    )
    run_import_sync(request)


@dataclass(frozen=True, slots=True, kw_only=True)
class ImportRequest:
    """Typed namespace for one import sync run."""

    public_base: Path
    public_head: Path
    target_dir: Path
    target_repo: str
    project_path: Path
    copybarista_project_path: Path
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
    runner_temp: Path
    type_check_targets: tuple[str, ...]
    refresh_public_lockfile: bool


def run_import_sync(request: ImportRequest) -> None:
    """Import public changes into source, validate, and optionally open a PR."""
    project = request.target_dir / request.project_path
    copybarista_project = request.target_dir / request.copybarista_project_path
    if request.open_pr_only:
        _log("Opening or updating target import PR.")
        _open_or_update_target_pr(request=request)
        return
    _log("Preparing Copybarista tool environment.")
    _run(
        ["uv", "--quiet", "--project", str(copybarista_project), "sync", "--all-groups"]
    )
    _log("Preparing target source environment.")
    _run(["uv", "--quiet", "--project", str(project), "sync", "--all-groups"])
    _log("Importing public changes into target source.")
    _run_import_change(request=request, project=project)
    _log("Validating target checkout.")
    _validate_target(project=project, type_check_targets=request.type_check_targets)
    if request.open_pr:
        _log("Opening or updating target import PR.")
        _open_or_update_target_pr(request=request)


def _parser() -> argparse.ArgumentParser:
    """Build the public-to-source sync CLI parser."""
    parser = argparse.ArgumentParser(
        description="Open or update a Copybarista import PR."
    )
    parser.add_argument("--public-base", default="public-base")
    parser.add_argument("--public-head", default="public-head")
    parser.add_argument("--target-dir", default="target")
    parser.add_argument("--target-repo", default=os.environ.get("TARGET_REPO", ""))
    parser.add_argument("--project-path", required=True)
    parser.add_argument(
        "--copybarista-project-path",
        default=os.environ.get("COPYBARISTA_TOOL_PROJECT_PATH", ""),
        help="Source checkout path for the project that provides copybarista.",
    )
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
    parser.add_argument("--public-base-ref", required=True)
    parser.add_argument("--public-head-ref", required=True)
    parser.add_argument(
        "--branch",
        default=os.environ.get("COPYBARISTA_IMPORT_BRANCH", ""),
    )
    parser.add_argument(
        "--branch-prefix",
        default=os.environ.get(
            "COPYBARISTA_IMPORT_BRANCH_PREFIX",
            DEFAULT_IMPORT_BRANCH_PREFIX,
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
        "--open-pr-only",
        action="store_true",
        help="Only create or update the source PR for already-imported changes.",
    )
    parser.add_argument(
        "--runner-temp",
        default=os.environ.get("RUNNER_TEMP", str(DEFAULT_RUNNER_TEMP)),
    )
    parser.add_argument(
        "--type-check-target",
        action="append",
        default=[],
        help="Path passed to basedpyright. Repeat for multiple targets.",
    )
    parser.add_argument(
        "--refresh-public-lockfile",
        action="store_true",
        help="Ignore generated public uv.lock while importing source-owned changes.",
    )
    return parser


def _run_import_change(*, request: ImportRequest, project: Path) -> None:
    """Run `copybarista import-change` and capture its JSON report."""
    copybarista_project = request.target_dir / request.copybarista_project_path
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
        _run(
            [
                "uv",
                "--quiet",
                "--project",
                str(copybarista_project),
                "run",
                "copybarista",
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
                "--json",
            ],
            stdout=output,
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
    # Public lockfiles generated after export are reproducibility artifacts, not
    # source-owned files. Dropping them preserves strict verification for the
    # Copybarista-managed tree without making every reverse import fail.
    shutil.copytree(
        source,
        destination,
        symlinks=True,
        ignore=shutil.ignore_patterns(".git", "uv.lock"),
    )
    return destination


def _validate_target(*, project: Path, type_check_targets: tuple[str, ...]) -> None:
    """Run source checkout checks after importing public changes."""
    _run(
        [
            "uv",
            "--quiet",
            "--project",
            str(project),
            "run",
            "ruff",
            "check",
            "--no-fix",
            "--no-cache",
            ".",
        ],
        cwd=project,
    )
    _run(
        [
            "uv",
            "--quiet",
            "--project",
            str(project),
            "run",
            "ruff",
            "format",
            "--check",
            "--no-cache",
            ".",
        ],
        cwd=project,
    )
    _run_basedpyright(project=project, targets=type_check_targets)
    _run(
        [
            "uv",
            "--quiet",
            "--project",
            str(project),
            "run",
            "ty",
            "check",
        ],
        cwd=project,
    )
    _run(
        [
            "uv",
            "--quiet",
            "--project",
            str(project),
            "run",
            "pytest",
            "-q",
            "-m",
            "not integration",
        ],
        cwd=project,
    )


def _run_basedpyright(*, project: Path, targets: tuple[str, ...]) -> None:
    """Run basedpyright for one target source checkout."""
    _run(
        [
            "uv",
            "--quiet",
            "--project",
            str(project),
            "run",
            "basedpyright",
            *targets,
        ],
        cwd=project,
    )


def _open_or_update_target_pr(*, request: ImportRequest) -> None:
    """Commit imported source changes and create or update the target PR."""
    if not _git_has_changes(path=request.target_dir, rel=request.project_path):
        _log("Import produced no target changes.")
        return

    branch = request.branch
    source_base_ref = _git_head(cwd=request.target_dir)
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
            "--author",
            _commit_author(request.sync_user_name, request.sync_user_email),
            "-m",
            f"Import {request.sync_label} public changes {request.public_sha}",
        ],
        cwd=request.target_dir,
    )
    _run(
        ["git", "push", "--force-with-lease", "origin", branch],
        cwd=request.target_dir,
    )

    title = f"Import {request.sync_label} public changes {request.public_sha[:12]}"
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
        return
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


def import_change_pr_body(
    *,
    public_repo: str,
    public_sha: str,
    public_base_ref: str,
    public_head_ref: str,
    source_base_ref: str,
    sync_label: str,
) -> str:
    """Return the target import-change PR body."""
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


def _git_head(*, cwd: Path) -> str:
    """Return the current Git HEAD SHA."""
    return _run(["git", "rev-parse", "HEAD"], cwd=cwd, capture=True).stdout.strip()


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
    return bool(json.loads(result.stdout))


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
    result = subprocess.run(  # noqa: S603 -- args constructed internally, not from user input
        argv,
        cwd=cwd,
        check=False,
        stdout=subprocess.PIPE if capture else stdout,
        stderr=subprocess.PIPE if capture else None,
        text=True,
    )
    if check and result.returncode != 0:
        raise SystemExit(result.returncode)
    return result


def _run_gh(
    argv: list[str],
    *,
    cwd: Path,
    capture: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run a GitHub CLI command with retries for transient API failures."""
    for attempt in range(1, GITHUB_RETRY_ATTEMPTS + 1):
        result = _run(argv, cwd=cwd, check=False, capture=True)
        if result.returncode == 0:
            if not capture:
                _write_process_output(result)
            return result
        if attempt == GITHUB_RETRY_ATTEMPTS or not _retryable_github_failure(result):
            _write_process_output(result)
            raise SystemExit(result.returncode)
        _log(
            "GitHub CLI command failed with a transient API error; "
            f"retrying in {GITHUB_RETRY_DELAY_SEC} seconds "
            f"({attempt}/{GITHUB_RETRY_ATTEMPTS})."
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


def _log(message: str) -> None:
    """Write one flushed workflow log line."""
    sys.stdout.write(f"{message}\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
