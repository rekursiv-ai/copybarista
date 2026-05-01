"""Run a source-to-public Copybarista GitHub sync.

GitHub Actions should stay as a thin wrapper: check out repositories, set up
Python and uv, pass credentials through `GH_TOKEN`, then call this script. The
script owns the sync behavior so branch naming, validation, project checks, PR
body generation, and no-diff handling can be tested outside Actions.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

DEFAULT_RUNNER_TEMP = Path(tempfile.gettempdir())
DEFAULT_SYNC_USER_EMAIL = "copybarista@example.com"
DEFAULT_SYNC_USER_NAME = "copybarista"
DEFAULT_EXPORT_TITLE = "Update public export"
DEFAULT_EXPORT_DESCRIPTION = "Updates the generated public repository export."
CONTROL_CHAR_BOUND = 32
DEFAULT_TYPE_CHECK_TARGETS = (".",)
GITHUB_RETRY_ATTEMPTS = 3
GITHUB_RETRY_DELAY_SEC = 2


def main(argv: list[str] | None = None) -> None:
    """Run source-to-public export validation and PR creation."""
    args = _parser().parse_args(argv)
    forbidden_pr_text = _split_forbidden_text(args.forbidden_pr_text)
    pr_text = export_pr_text(
        title=args.pr_title,
        body=args.pr_body,
        source_message=args.source_message,
        use_source_message=args.use_source_message_pr_text,
        forbidden_text=forbidden_pr_text,
    )
    request = ExportRequest(
        source_dir=Path(args.source_dir),
        project_path=Path(args.project_path),
        public_dir=Path(args.public_dir),
        target_repo=args.target_repo,
        base_branch=args.base_branch,
        source_sha=args.source_sha,
        branch=export_branch_name(
            explicit=args.branch,
            source_branch=args.source_branch,
            source_sha=args.source_sha,
        ),
        sync_user_name=args.sync_user_name,
        sync_user_email=args.sync_user_email,
        pr_title=pr_text.title,
        pr_body=pr_text.body,
        auto_merge=_string_bool(args.auto_merge),
        runner_temp=Path(args.runner_temp),
        release_check_script=Path(args.release_check_script)
        if args.release_check_script
        else None,
        type_check_targets=tuple(args.type_check_target) or DEFAULT_TYPE_CHECK_TARGETS,
        smoke_import=args.smoke_import,
    )
    run_export_sync(request)


@dataclass(frozen=True, slots=True, kw_only=True)
class ExportRequest:
    """Typed namespace for one export sync run."""

    source_dir: Path
    project_path: Path
    public_dir: Path
    target_repo: str
    base_branch: str
    source_sha: str
    branch: str
    sync_user_name: str
    sync_user_email: str
    pr_title: str
    pr_body: str
    auto_merge: bool
    runner_temp: Path
    release_check_script: Path | None
    type_check_targets: tuple[str, ...]
    smoke_import: str


@dataclass(frozen=True, slots=True, kw_only=True)
class ExportPrText:
    """Resolved public PR title and body text."""

    title: str
    body: str


def run_export_sync(request: ExportRequest) -> None:
    """Validate, export, replace the public checkout, and open/update a PR."""
    project = request.source_dir / request.project_path
    export_dir = Path(tempfile.mkdtemp(prefix="copybarista-public-"))
    manifest = request.runner_temp / "copybarista-manifest.json"
    dist_dir = request.runner_temp / "copybarista-dist"

    _log("Validating source checkout.")
    _validate_source(project=project, type_check_targets=request.type_check_targets)
    _log("Exporting public tree.")
    _export_public_tree(
        project=project,
        source_dir=request.source_dir,
        export_dir=export_dir,
        manifest=manifest,
    )
    if request.release_check_script:
        _log("Checking exported release tree.")
        _check_release_tree(
            project=project,
            root=export_dir,
            script=request.release_check_script,
        )
    _log("Replacing public checkout contents.")
    _replace_tree(source=export_dir, destination=request.public_dir)
    _log("Validating public checkout.")
    _validate_public(
        public_dir=request.public_dir,
        dist_dir=dist_dir,
        release_check_script=request.release_check_script,
        type_check_targets=request.type_check_targets,
        smoke_import=request.smoke_import,
    )
    _log("Opening or updating export PR.")
    _open_or_update_export_pr(request=request)
    if request.auto_merge:
        _enable_export_pr_auto_merge(request=request)


def _parser() -> argparse.ArgumentParser:
    """Build the source-to-public sync CLI parser."""
    parser = argparse.ArgumentParser(
        description="Open or update a Copybarista export PR."
    )
    parser.add_argument("--source-dir", default="source")
    parser.add_argument("--project-path", required=True)
    parser.add_argument("--public-dir", default="public")
    parser.add_argument("--target-repo", default=os.environ.get("TARGET_REPO", ""))
    parser.add_argument("--base-branch", default=os.environ.get("BASE_BRANCH", "main"))
    parser.add_argument("--source-sha", default=os.environ.get("GITHUB_SHA", "manual"))
    parser.add_argument(
        "--sync-user-name",
        default=os.environ.get("COPYBARISTA_SYNC_USER_NAME", DEFAULT_SYNC_USER_NAME),
    )
    parser.add_argument(
        "--sync-user-email",
        default=os.environ.get("COPYBARISTA_SYNC_USER_EMAIL", DEFAULT_SYNC_USER_EMAIL),
    )
    parser.add_argument(
        "--source-branch",
        default=os.environ.get("GITHUB_REF_NAME", ""),
    )
    parser.add_argument(
        "--branch",
        default=os.environ.get("COPYBARISTA_EXPORT_BRANCH", ""),
    )
    parser.add_argument(
        "--pr-title",
        default=os.environ.get("COPYBARISTA_PR_TITLE", ""),
    )
    parser.add_argument("--pr-body", default=os.environ.get("COPYBARISTA_PR_BODY", ""))
    parser.add_argument(
        "--source-message",
        default=os.environ.get("COPYBARISTA_SOURCE_MESSAGE", ""),
        help="Source commit message used when commit-message PR text is enabled.",
    )
    parser.add_argument(
        "--use-source-message-pr-text",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("COPYBARISTA_USE_SOURCE_MESSAGE_PR_TEXT"),
        help="Use the source commit first line and body as generated PR text.",
    )
    parser.add_argument(
        "--forbidden-pr-text",
        action="append",
        default=[],
        help="Comma- or newline-separated private terms forbidden in public PR text.",
    )
    parser.add_argument(
        "--auto-merge",
        default=os.environ.get("COPYBARISTA_AUTO_MERGE", "false"),
        help="Enable GitHub PR auto-merge for the generated export branch.",
    )
    parser.add_argument(
        "--workflow",
        default=os.environ.get("GITHUB_WORKFLOW", "manual"),
    )
    parser.add_argument(
        "--runner-temp",
        default=os.environ.get("RUNNER_TEMP", str(DEFAULT_RUNNER_TEMP)),
    )
    parser.add_argument(
        "--release-check-script",
        default=os.environ.get("COPYBARISTA_RELEASE_CHECK_SCRIPT", ""),
        help="Optional project-relative release-tree checker script.",
    )
    parser.add_argument(
        "--type-check-target",
        action="append",
        default=[],
        help="Path passed to basedpyright. Repeat for multiple targets.",
    )
    parser.add_argument(
        "--smoke-import",
        default=os.environ.get("COPYBARISTA_SMOKE_IMPORT", ""),
        help="Optional module imported from the built wheel as a smoke test.",
    )
    return parser


def _validate_source(*, project: Path, type_check_targets: tuple[str, ...]) -> None:
    """Run source checkout checks before creating a public export."""
    _run(["uv", "--quiet", "--project", str(project), "sync", "--all-groups"])
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
    _run_basedpyright(project=project, targets=type_check_targets)
    _run(
        [
            "uv",
            "--quiet",
            "--project",
            str(project),
            "run",
            "pytest",
            "-q",
        ],
        cwd=project,
    )


def _export_public_tree(
    *, project: Path, source_dir: Path, export_dir: Path, manifest: Path
) -> None:
    """Export the public tree and write the machine-readable manifest."""
    export_dir.mkdir(parents=True, exist_ok=True)
    manifest.parent.mkdir(parents=True, exist_ok=True)
    with manifest.open("w", encoding="utf-8") as output:
        _run(
            [
                "uv",
                "--quiet",
                "--project",
                str(project),
                "run",
                "copybarista",
                "export",
                str(project / "copy.barista.toml"),
                str(source_dir),
                "--folder-dir",
                str(export_dir),
                "--force",
                "--json",
            ],
            stdout=output,
        )


def _check_release_tree(*, project: Path, root: Path, script: Path) -> None:
    """Run release-tree policy validation against one tree."""
    _run(
        [
            "uv",
            "--quiet",
            "--project",
            str(project),
            "run",
            "python",
            str(project / script),
            str(root),
        ]
    )


def _replace_tree(*, source: Path, destination: Path) -> None:
    """Replace public package contents while preserving repo-owned metadata."""
    for path in destination.iterdir():
        if path.name in {".git", ".github"}:
            continue
        _delete_path(path)
    for path in source.iterdir():
        target = destination / path.name
        if path.is_dir() and not path.is_symlink():
            shutil.copytree(path, target, symlinks=True, dirs_exist_ok=target.exists())
        else:
            shutil.copy2(path, target, follow_symlinks=False)


def _validate_public(
    *,
    public_dir: Path,
    dist_dir: Path,
    release_check_script: Path | None,
    type_check_targets: tuple[str, ...],
    smoke_import: str,
) -> None:
    """Run public checkout checks that a contributor would run locally."""
    if release_check_script:
        _run(
            ["python", str(release_check_script), ".", "--allow-root-git"],
            cwd=public_dir,
        )
    _run(["uv", "sync", "--all-groups"], cwd=public_dir)
    _run(["uv", "run", "--all-groups", "ruff", "check", "."], cwd=public_dir)
    _run(
        ["uv", "run", "--all-groups", "ruff", "format", "--check", "."],
        cwd=public_dir,
    )
    _run_basedpyright_public(public_dir=public_dir, targets=type_check_targets)
    _run(["uv", "run", "--all-groups", "pytest", "-q"], cwd=public_dir)
    _run(["uv", "build", "--out-dir", str(dist_dir)], cwd=public_dir)
    if not smoke_import:
        return
    wheel = sorted(dist_dir.glob("*.whl"))[0]
    _run(
        [
            "uv",
            "run",
            "--isolated",
            "--with",
            str(wheel),
            "python",
            "-c",
            f"import importlib; importlib.import_module({smoke_import!r}); print('ok')",
        ],
        cwd=public_dir,
    )


def _run_basedpyright(*, project: Path, targets: tuple[str, ...]) -> None:
    """Run basedpyright for one source checkout."""
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


def _run_basedpyright_public(*, public_dir: Path, targets: tuple[str, ...]) -> None:
    """Run basedpyright for one public checkout."""
    _run(
        ["uv", "run", "--all-groups", "basedpyright", *targets],
        cwd=public_dir,
    )


def _open_or_update_export_pr(*, request: ExportRequest) -> None:
    """Commit exported changes and create or update the public PR."""
    if not _git_has_changes(request.public_dir):
        _log("Export produced no target repository changes.")
        return

    branch = request.branch
    body = export_pr_body(
        description=request.pr_body,
        branch=branch,
    )
    body_file = request.runner_temp / "copybarista-pr-body.md"
    body_file.write_text(body, encoding="utf-8")

    _run(["git", "config", "user.name", request.sync_user_name], cwd=request.public_dir)
    _run(
        ["git", "config", "user.email", request.sync_user_email],
        cwd=request.public_dir,
    )
    _fetch_branch(branch=branch, cwd=request.public_dir)
    _run(["git", "switch", "-C", branch], cwd=request.public_dir)
    _run(["git", "add", "-A"], cwd=request.public_dir)
    _run(
        [
            "git",
            "commit",
            "--author",
            _commit_author(request.sync_user_name, request.sync_user_email),
            "-m",
            request.pr_title,
        ],
        cwd=request.public_dir,
    )
    _run(
        ["git", "push", "--force-with-lease", "origin", branch],
        cwd=request.public_dir,
    )

    if _gh_pr_exists(branch=branch, repo=request.target_repo, cwd=request.public_dir):
        _run_gh(
            [
                "gh",
                "pr",
                "edit",
                branch,
                "--repo",
                request.target_repo,
                "--title",
                request.pr_title,
                "--body-file",
                str(body_file),
            ],
            cwd=request.public_dir,
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
            request.pr_title,
            "--body-file",
            str(body_file),
        ],
        cwd=request.public_dir,
    )


def _enable_export_pr_auto_merge(*, request: ExportRequest) -> None:
    """Enable squash auto-merge for the generated public export PR."""
    _run_gh(
        [
            "gh",
            "pr",
            "merge",
            request.branch,
            "--repo",
            request.target_repo,
            "--squash",
            "--subject",
            request.pr_title,
            "--body",
            f"Copybarista export branch: {request.branch}",
            "--auto",
        ],
        cwd=request.public_dir,
    )


def export_pr_text(
    *,
    title: str,
    body: str,
    source_message: str,
    use_source_message: bool,
    forbidden_text: tuple[str, ...],
) -> ExportPrText:
    """Return public export PR text from manual inputs, commit text, or defaults."""
    message_title = ""
    message_body = ""
    if use_source_message and (not title.strip() or not body.strip()):
        message_title, message_body = _split_commit_message(source_message)
    resolved_title = title.strip() or message_title or DEFAULT_EXPORT_TITLE
    resolved_body = body.strip() or message_body or DEFAULT_EXPORT_DESCRIPTION
    return ExportPrText(
        title=_public_pr_text(
            value=resolved_title,
            name="--pr-title",
            forbidden_text=forbidden_text,
        ),
        body=_public_pr_text(
            value=resolved_body,
            name="--pr-body",
            forbidden_text=forbidden_text,
        ),
    )


def export_pr_body(*, description: str, branch: str) -> str:
    """Return the public export PR body."""
    return (
        f"{description.strip()}\n\n"
        "----\n"
        f"Copybarista export branch: `{branch}`\n\n"
        "Do not push manual commits to this generated branch. Change the source "
        "repository, then rerun the export workflow with the same branch.\n"
    )


def _split_commit_message(message: str) -> tuple[str, str]:
    """Split a commit message into title and description."""
    title, separator, description = message.strip().partition("\n")
    if not title.strip():
        raise SystemExit("--source-message or --pr-title is required.\n")
    return title.strip(), description.strip() if separator else ""


def export_branch_name(*, explicit: str, source_branch: str, source_sha: str) -> str:
    """Return the source-to-public sync branch name."""
    if explicit.strip():
        return _validated_generated_branch(
            branch=explicit.strip(),
            prefix="copybarista/export/",
        )
    if source_branch.strip():
        branch = f"copybarista/export/{_branch_component(source_branch)}"
    else:
        branch = f"copybarista/export/sha-{_branch_component(source_sha[:12])}"
    return _validated_generated_branch(branch=branch, prefix="copybarista/export/")


def _commit_author(name: str, email: str) -> str:
    """Return the Git author identity for a generated sync commit."""
    return f"{name} <{email}>"


def _git_has_changes(path: Path) -> bool:
    """Return whether a checkout has pending Git changes."""
    result = _run(["git", "status", "--porcelain"], cwd=path, check=False, capture=True)
    return bool(result.stdout.strip())


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
    """Fetch a remote branch if it exists without failing on first export."""
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


def _delete_path(path: Path) -> None:
    """Delete one checkout entry before copying the export over it."""
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def _required_text(value: str, name: str) -> str:
    """Return stripped text or exit with a CLI usage error."""
    if value.strip():
        return value.strip()
    sys.stderr.write(f"{name} is required.\n")
    raise SystemExit(2)


def _public_pr_text(*, value: str, name: str, forbidden_text: tuple[str, ...]) -> str:
    """Validate PR text before it is sent to a public repository."""
    text = _required_text(value, name)
    lowered = text.casefold()
    if any(term.casefold() in lowered for term in forbidden_text):
        sys.stderr.write(f"{name} contains restricted source-specific text.\n")
        raise SystemExit(2)
    return text


def _split_forbidden_text(values: list[str]) -> tuple[str, ...]:
    """Split repeated comma- or newline-delimited forbidden text inputs."""
    terms: list[str] = []
    for value in values:
        for line in value.splitlines():
            terms.extend(part.strip() for part in line.split(",") if part.strip())
    return tuple(terms)


def _env_bool(name: str) -> bool:
    """Return whether an environment variable is truthy."""
    return os.environ.get(name, "").strip().casefold() in {"1", "true", "yes", "on"}


def _string_bool(value: str) -> bool:
    """Return whether a CLI or environment string is truthy."""
    return value.strip().casefold() in {"1", "true", "yes", "on"}


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
