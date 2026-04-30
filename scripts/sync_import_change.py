"""Run a public-to-source Copybarista GitHub sync.

The workflow checks out public base/head trees and a target source checkout,
then calls this script. Keeping the import, validation, branch creation, and PR
body logic here makes the GitHub Action easier to audit and gives us local unit
coverage for the behavior that changes over time.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

DEFAULT_RUNNER_TEMP = Path(tempfile.gettempdir())
DEFAULT_SYNC_USER_EMAIL = "copybarista@rekursiv.ai"
DEFAULT_SYNC_USER_NAME = "copybarista"


def main(argv: list[str] | None = None) -> None:
    """Run public-to-source import validation and optional PR creation."""
    args = _parser().parse_args(argv)
    request = ImportRequest(
        public_base=Path(args.public_base),
        public_head=Path(args.public_head),
        target_dir=Path(args.target_dir),
        target_repo=args.target_repo,
        project_path=Path(args.project_path),
        base_branch=args.base_branch,
        public_repo=args.public_repo,
        public_sha=args.public_sha,
        public_base_ref=args.public_base_ref,
        public_head_ref=args.public_head_ref,
        branch=import_branch_name(explicit=args.branch, public_sha=args.public_sha),
        sync_user_name=args.sync_user_name,
        sync_user_email=args.sync_user_email,
        report=Path(args.report),
        open_pr=_string_bool(args.open_pr),
        runner_temp=Path(args.runner_temp),
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
    base_branch: str
    public_repo: str
    public_sha: str
    public_base_ref: str
    public_head_ref: str
    branch: str
    sync_user_name: str
    sync_user_email: str
    report: Path
    open_pr: bool
    runner_temp: Path


def run_import_sync(request: ImportRequest) -> None:
    """Import public changes into source, validate, and optionally open a PR."""
    project = request.target_dir / request.project_path
    _log("Preparing target source environment.")
    _run(["uv", "--quiet", "--project", str(project), "sync", "--all-groups"])
    _log("Importing public changes into target source.")
    _run_import_change(request=request, project=project)
    _log("Validating target checkout.")
    _validate_target(project=project)
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
        "--report",
        default=os.environ.get("IMPORT_REPORT", "import-report.json"),
    )
    parser.add_argument("--open-pr", default="false")
    parser.add_argument(
        "--runner-temp",
        default=os.environ.get("RUNNER_TEMP", str(DEFAULT_RUNNER_TEMP)),
    )
    return parser


def _run_import_change(*, request: ImportRequest, project: Path) -> None:
    """Run `copybarista import-change` and capture its JSON report."""
    request.report.parent.mkdir(parents=True, exist_ok=True)
    with request.report.open("w", encoding="utf-8") as output:
        _run(
            [
                "uv",
                "--quiet",
                "--project",
                str(project),
                "run",
                "copybarista",
                "import-change",
                str(project / "copy.barista.toml"),
                "--public-base",
                str(request.public_base),
                "--public-head",
                str(request.public_head),
                "--source-base",
                str(request.target_dir),
                "--destination",
                str(request.target_dir),
                "--json",
            ],
            stdout=output,
        )


def _validate_target(*, project: Path) -> None:
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
            "basedpyright",
            "copybarista",
            "scripts",
            "tests",
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
            f"Import Copybarista public changes {request.public_sha}",
        ],
        cwd=request.target_dir,
    )
    _run(
        ["git", "push", "--force-with-lease", "origin", branch],
        cwd=request.target_dir,
    )

    title = f"Import Copybarista public changes {request.public_sha[:12]}"
    if _gh_pr_exists(branch=branch, repo=request.target_repo, cwd=request.target_dir):
        _run(
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
    _run(
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
) -> str:
    """Return the target import-change PR body."""
    return (
        "Imports Copybarista public repository changes into the source repository.\n\n"
        f"- Public repository: `{public_repo}`\n"
        f"- Public SHA: `{public_sha}`\n"
        f"- Public base: `{public_base_ref}`\n"
        f"- Public head: `{public_head_ref}`\n"
        f"- Source base: `{source_base_ref}`\n"
        "- Import report: generated by `copybarista import-change`\n"
        "\n"
        "Regenerate this PR before merging if source `main` changes.\n"
    )


def import_branch_name(*, explicit: str, public_sha: str) -> str:
    """Return the public-to-source sync branch name."""
    if explicit.strip():
        return explicit.strip()
    return f"copybarista/import/sha-{_branch_component(public_sha[:12])}"


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
    result = _run(
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
    result = subprocess.run(  # noqa: S603
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


def _log(message: str) -> None:
    """Write one flushed workflow log line."""
    sys.stdout.write(f"{message}\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
