"""Run a source-to-public Copybarista GitHub sync.

GitHub Actions should stay as a thin wrapper: check out repositories, set up
Python and uv, pass credentials through `GH_TOKEN`, then call this script. The
script owns the sync behavior so branch naming, validation, project checks, PR
body generation, and no-diff handling can be tested outside Actions.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TextIO, cast

import argparse
import hashlib
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
DEFAULT_EXPORT_BRANCH_PREFIX = "copybarista/export/"
DEFAULT_EXPORT_TITLE = "Update public export"
DEFAULT_EXPORT_DESCRIPTION = "Updates the generated public repository export."
CONTROL_CHAR_BOUND = 32
PR_STATE_VERSION = "1"
PR_MARKER_PREFIX = "<!-- copybarista:pr-state "
PR_ENTRY_PREFIX = "<!-- copybarista:pr-entry "
DEFAULT_TYPE_CHECK_TARGETS = (".",)
GITHUB_RETRY_ATTEMPTS = 3
GITHUB_RETRY_DELAY_SEC = 2
PUBLIC_VALIDATION_ARTIFACT_DIRS = (
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
)
PUBLIC_VALIDATION_ARTIFACT_FILES = (".coverage",)
PR_TEMPLATE_PATHS = (
    Path(".github/PULL_REQUEST_TEMPLATE.md"),
    Path(".github/pull_request_template.md"),
    Path("PULL_REQUEST_TEMPLATE.md"),
    Path("pull_request_template.md"),
)
PR_DROPPED_TEMPLATE_SECTIONS = frozenset({"Checklist"})
PR_VALIDATION_TEMPLATE_SECTIONS = frozenset({"Testing", "Validation"})


def main(argv: list[str] | None = None) -> None:
    """Run source-to-public export validation and PR creation."""
    args = _parser().parse_args(argv)
    forbidden_pr_text = _split_forbidden_text(args.forbidden_pr_text)
    pr_text = export_pr_text(
        title=args.pr_title,
        body=args.pr_body,
        default_title=args.pr_default_title,
        default_body=args.pr_default_body,
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
            prefix=args.branch_prefix,
        ),
        sync_label=args.sync_label,
        sync_user_name=args.sync_user_name,
        sync_user_email=args.sync_user_email,
        pr_title=pr_text.title,
        pr_body=pr_text.body,
        manual_pr_title=args.pr_title.strip(),
        manual_pr_body=args.pr_body.strip(),
        replay_settings=PrReplaySettings(
            scope=args.pr_scope,
            default_title=pr_text.title,
            default_body=pr_text.body,
            require_metadata=args.require_pr_metadata,
            bootstrap_base=args.replay_bootstrap_base,
            publish_source_rev=args.publish_source_rev,
        ),
        forbidden_pr_text=forbidden_pr_text,
        auto_merge=_string_bool(args.auto_merge),
        skip_source_validation=args.skip_source_validation,
        runner_temp=Path(args.runner_temp),
        release_check_script=Path(args.release_check_script)
        if args.release_check_script
        else None,
        type_check_targets=tuple(args.type_check_target) or DEFAULT_TYPE_CHECK_TARGETS,
        smoke_import=args.smoke_import,
    )
    try:
        run_export_sync(request)
    except (PrMetadataError, PrReplayError) as err:
        sys.stderr.write(f"{err}\n")
        raise SystemExit(2) from err


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
    sync_label: str
    sync_user_name: str
    sync_user_email: str
    pr_title: str
    pr_body: str
    manual_pr_title: str
    manual_pr_body: str
    replay_settings: PrReplaySettings
    forbidden_pr_text: tuple[str, ...]
    auto_merge: bool
    skip_source_validation: bool
    runner_temp: Path
    release_check_script: Path | None
    type_check_targets: tuple[str, ...]
    smoke_import: str


@dataclass(frozen=True, slots=True, kw_only=True)
class ExportPrText:
    """Resolved public PR title and body text."""

    title: str
    body: str


@dataclass(frozen=True, slots=True, kw_only=True)
class PrReplaySettings:
    """Settings that control public PR metadata replay."""

    scope: str
    default_title: str
    default_body: str
    require_metadata: bool
    bootstrap_base: str
    publish_source_rev: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class SourceAuthor:
    """One git source author used for generated commit attribution."""

    name: str
    email: str


@dataclass(frozen=True, slots=True, kw_only=True)
class PrMetadataPatch:
    """One source commit's public PR metadata."""

    commit_sha: str
    scope: str
    title: str
    author: SourceAuthor
    body: str
    body_mode: str


@dataclass(frozen=True, slots=True, kw_only=True)
class PrBodyEntry:
    """One appended public PR body entry."""

    commit_sha: str
    text: str


@dataclass(frozen=True, slots=True, kw_only=True)
class PrReplayState:
    """Rendered public PR state after replaying source commit metadata."""

    title: str
    authors: tuple[SourceAuthor, ...]
    body_intro: str
    body_entries: tuple[PrBodyEntry, ...]
    applied_source_rev: str
    applied_source_digest: str
    metadata_count: int


@dataclass(frozen=True, slots=True, kw_only=True)
class CurrentPr:
    """Open generated public PR state."""

    title: str
    body: str
    number: int
    url: str


@dataclass(frozen=True, slots=True, kw_only=True)
class BranchMarkers:
    """Machine markers read from the current generated branch commit."""

    source_digest: str
    replay_base_digest: str
    exists: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class PrReplayPlan:
    """Resolved PR replay result for one export run."""

    state: PrReplayState
    body: str
    replay_base: str
    replay_base_digest: str
    current_pr: CurrentPr | None


class PrMetadataError(ValueError):
    """Commit metadata cannot be parsed or safely published."""


class PrReplayError(RuntimeError):
    """PR replay state cannot be resolved safely."""


def run_export_sync(request: ExportRequest) -> None:
    """Validate, export, replace the public checkout, and open/update a PR."""
    project = request.source_dir / request.project_path
    export_dir = Path(tempfile.mkdtemp(prefix="copybarista-public-"))
    manifest = request.runner_temp / "copybarista-manifest.json"
    dist_dir = request.runner_temp / "copybarista-dist"
    _log("Resolving export PR replay state.")
    pr_plan = _resolve_pr_replay_plan(request=request)

    if request.skip_source_validation:
        _log("Skipping source checkout validation.")
    else:
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
    validation_dir = Path(tempfile.mkdtemp(prefix="copybarista-validate-"))
    try:
        _copy_validation_tree(source=request.public_dir, destination=validation_dir)
        _validate_public(
            public_dir=validation_dir,
            dist_dir=dist_dir,
            release_check_script=request.release_check_script,
            type_check_targets=request.type_check_targets,
            smoke_import=request.smoke_import,
        )
        _remove_public_validation_artifacts(validation_dir)
    finally:
        _delete_path(validation_dir)
    _log("Opening or updating export PR.")
    _open_or_update_export_pr(request=request, pr_plan=pr_plan)
    if request.auto_merge:
        _enable_export_pr_auto_merge(request=request, pr_title=pr_plan.state.title)


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
        "--branch-prefix",
        default=os.environ.get(
            "COPYBARISTA_EXPORT_BRANCH_PREFIX",
            DEFAULT_EXPORT_BRANCH_PREFIX,
        ),
    )
    parser.add_argument(
        "--sync-label",
        default=os.environ.get("COPYBARISTA_SYNC_LABEL", DEFAULT_SYNC_LABEL),
    )
    parser.add_argument(
        "--pr-title",
        default=os.environ.get("COPYBARISTA_PR_TITLE", ""),
    )
    parser.add_argument("--pr-body", default=os.environ.get("COPYBARISTA_PR_BODY", ""))
    parser.add_argument(
        "--pr-default-title",
        default=os.environ.get("COPYBARISTA_PR_DEFAULT_TITLE", DEFAULT_EXPORT_TITLE),
    )
    parser.add_argument(
        "--pr-default-body",
        default=os.environ.get(
            "COPYBARISTA_PR_DEFAULT_BODY",
            DEFAULT_EXPORT_DESCRIPTION,
        ),
    )
    parser.add_argument(
        "--pr-scope",
        default=os.environ.get("COPYBARISTA_PR_SCOPE", ""),
        help="Package or repository scope used to select scoped Copybarista-PR blocks.",
    )
    parser.add_argument(
        "--require-pr-metadata",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("COPYBARISTA_REQUIRE_PR_METADATA"),
    )
    parser.add_argument(
        "--replay-bootstrap-base",
        default=os.environ.get("COPYBARISTA_REPLAY_BOOTSTRAP_BASE", ""),
    )
    parser.add_argument(
        "--publish-source-rev",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("COPYBARISTA_PUBLISH_SOURCE_REV"),
    )
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
        "--skip-source-validation",
        action="store_true",
        help=(
            "Skip source checkout lint/type/test validation. The exported public "
            "checkout is still validated before opening a PR."
        ),
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
                sys.executable,
                "-m",
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


def _copy_validation_tree(*, source: Path, destination: Path) -> None:
    """Copy a public checkout into a disposable validation tree."""
    for path in source.iterdir():
        if path.name == ".git":
            continue
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
            ["python", "-B", str(release_check_script), ".", "--allow-root-git"],
            cwd=public_dir,
        )
    _run(["uv", "sync", "--all-groups"], cwd=public_dir)
    _run(
        ["uv", "run", "--all-groups", "ruff", "check", "--no-fix", "--no-cache", "."],
        cwd=public_dir,
    )
    _run(
        ["uv", "run", "--all-groups", "ruff", "format", "--check", "--no-cache", "."],
        cwd=public_dir,
    )
    _run(["uv", "run", "--all-groups", "codespell", "."], cwd=public_dir)
    _run(["uv", "run", "--all-groups", "ty", "check"], cwd=public_dir)
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


def _remove_public_validation_artifacts(root: Path) -> None:
    """Remove files generated by local validation before staging a public PR."""
    for name in PUBLIC_VALIDATION_ARTIFACT_DIRS:
        for path in root.rglob(name):
            if path.exists():
                _delete_path(path)
    for name in PUBLIC_VALIDATION_ARTIFACT_FILES:
        for path in root.rglob(name):
            if path.exists():
                _delete_path(path)


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


def _resolve_pr_replay_plan(*, request: ExportRequest) -> PrReplayPlan:
    """Resolve the PR title/body by replaying source commit metadata."""
    current_source_rev = _current_source_rev(
        source_dir=request.source_dir,
        fallback=request.source_sha,
    )
    current_source_digest = _source_rev_digest(current_source_rev)
    current_pr = _current_pr(
        branch=request.branch,
        repo=request.target_repo,
        cwd=request.public_dir,
    )
    branch_markers = _branch_markers(branch=request.branch, cwd=request.public_dir)
    replay_base = _replay_base(
        request=request,
        current_source_rev=current_source_rev,
        current_pr=current_pr,
        branch_markers=branch_markers,
    )
    patches = _source_pr_metadata(
        source_dir=request.source_dir,
        replay_base=replay_base,
        current_source_rev=current_source_rev,
        forbidden_text=request.forbidden_pr_text,
        scope=request.replay_settings.scope,
    )
    if request.replay_settings.require_metadata and not patches:
        raise PrReplayError(
            "PR metadata replay found no Copybarista-PR-* fields in "
            f"{_short_rev(replay_base)}..{_short_rev(current_source_rev)}. "
            "Add commit metadata or disable [pull_request].require_pr_metadata."
        )
    state = replay_pr_metadata(
        base=_base_pr_state(
            request=request,
            current_pr=current_pr,
            current_source_rev=current_source_rev,
            current_source_digest=current_source_digest,
        ),
        patches=patches,
    )
    if request.manual_pr_title:
        state = _replace_pr_state(state, title=request.manual_pr_title)
    if request.manual_pr_body:
        state = _replace_pr_state(state, body_intro=request.manual_pr_body)
    state = _replace_pr_state(
        state,
        applied_source_rev=current_source_rev,
        applied_source_digest=current_source_digest,
    )
    return PrReplayPlan(
        state=state,
        body=_render_pr_body(
            state=state,
            branch=request.branch,
            sync_label=request.sync_label,
            publish_source_rev=request.replay_settings.publish_source_rev,
            pr_template=_read_pr_template(request.public_dir),
        ),
        replay_base=replay_base,
        replay_base_digest=_source_rev_digest(replay_base) if replay_base else "",
        current_pr=current_pr,
    )


def _current_source_rev(*, source_dir: Path, fallback: str) -> str:
    """Return the current private source checkout revision."""
    result = _run(
        ["git", "rev-parse", "HEAD"], cwd=source_dir, check=False, capture=True
    )
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    if fallback.strip():
        return fallback.strip()
    raise PrReplayError("Cannot resolve current source revision from source checkout.")


def _current_pr(*, branch: str, repo: str, cwd: Path) -> CurrentPr | None:
    """Return the open generated PR for a branch, if one exists."""
    if not _gh_pr_exists(branch=branch, repo=repo, cwd=cwd):
        return None
    result = _run_gh(
        [
            "gh",
            "pr",
            "view",
            branch,
            "--repo",
            repo,
            "--json",
            "number,title,body,url",
        ],
        cwd=cwd,
        capture=True,
    )
    parsed: object = json.loads(result.stdout)
    if not isinstance(parsed, dict):
        raise PrReplayError(f"GitHub PR state for branch {branch} is not a mapping.")
    raw = cast("dict[str, object]", parsed)
    number = raw.get("number", 0)
    if not isinstance(number, int):
        raise PrReplayError(f"GitHub PR number for branch {branch} is not an integer.")
    return CurrentPr(
        title=str(raw.get("title", "")),
        body=str(raw.get("body", "")),
        number=number,
        url=str(raw.get("url", "")),
    )


def _replay_base(
    *,
    request: ExportRequest,
    current_source_rev: str,
    current_pr: CurrentPr | None,
    branch_markers: BranchMarkers,
) -> str:
    """Return the source commit used as replay base."""
    if current_pr:
        marker = _applied_marker_from_pr_body(current_pr.body)
        if marker:
            return _resolve_source_marker(
                source_dir=request.source_dir,
                marker=marker,
                marker_source="PR applied marker",
            )
        if request.replay_settings.bootstrap_base:
            return request.replay_settings.bootstrap_base
        raise PrReplayError(
            "Existing generated PR has no Copybarista replay marker. "
            f"Branch: {request.branch}. Configure replay_bootstrap_base or rerun "
            "with manual PR text once to migrate the PR."
        )
    if branch_markers.source_digest:
        return _resolve_source_marker(
            source_dir=request.source_dir,
            marker=f"sha256:{branch_markers.source_digest}",
            marker_source="generated branch source marker",
        )
    if branch_markers.replay_base_digest:
        return _resolve_source_marker(
            source_dir=request.source_dir,
            marker=f"sha256:{branch_markers.replay_base_digest}",
            marker_source="generated branch replay-base marker",
        )
    if request.replay_settings.bootstrap_base:
        return request.replay_settings.bootstrap_base
    if not branch_markers.exists:
        return _source_parent(source_dir=request.source_dir, rev=current_source_rev)
    return _source_parent(source_dir=request.source_dir, rev=current_source_rev)


def _source_pr_metadata(
    *,
    source_dir: Path,
    replay_base: str,
    current_source_rev: str,
    forbidden_text: tuple[str, ...],
    scope: str,
) -> tuple[PrMetadataPatch, ...]:
    """Read and parse source commit PR metadata in replay order."""
    if replay_base:
        _require_source_history(
            source_dir=source_dir,
            replay_base=replay_base,
            current_source_rev=current_source_rev,
        )
        revspec = f"{replay_base}..{current_source_rev}"
    else:
        revspec = current_source_rev
    result = _run(
        [
            "git",
            "log",
            "-z",
            "--reverse",
            "--format=%H%x00%aN%x00%aE%x00%B",
            revspec,
        ],
        cwd=source_dir,
        capture=True,
    )
    return _parse_pr_metadata_log(
        result.stdout,
        forbidden_text=forbidden_text,
        scope=scope,
    )


def _require_source_history(
    *, source_dir: Path, replay_base: str, current_source_rev: str
) -> None:
    """Fail if the source checkout cannot replay the requested commit range."""
    result = _run(
        ["git", "merge-base", "--is-ancestor", replay_base, current_source_rev],
        cwd=source_dir,
        check=False,
        capture=True,
    )
    if result.returncode != 0:
        raise PrReplayError(
            "Source checkout history is insufficient for PR metadata replay. "
            f"Missing base {_short_rev(replay_base)} before "
            f"{_short_rev(current_source_rev)} in {source_dir}. Use fetch-depth: 0 "
            "or fetch the replay range before running Copybarista."
        )


def _base_pr_state(
    *,
    request: ExportRequest,
    current_pr: CurrentPr | None,
    current_source_rev: str,
    current_source_digest: str,
) -> PrReplayState:
    """Return the state used before applying replay patches."""
    if current_pr:
        if _applied_marker_from_pr_body(current_pr.body):
            return _state_from_pr_body(
                title=current_pr.title or request.replay_settings.default_title,
                body=current_pr.body,
                default_body=request.replay_settings.default_body,
            )
        if not request.replay_settings.bootstrap_base:
            raise PrReplayError("Existing generated PR body has no Copybarista marker.")
    return PrReplayState(
        title=request.replay_settings.default_title,
        authors=(),
        body_intro=request.replay_settings.default_body,
        body_entries=(),
        applied_source_rev=current_source_rev,
        applied_source_digest=current_source_digest,
        metadata_count=0,
    )


def _open_or_update_export_pr(*, request: ExportRequest, pr_plan: PrReplayPlan) -> None:
    """Commit exported changes and create or update the public PR."""
    if not _git_has_changes(request.public_dir):
        if pr_plan.current_pr and (
            pr_plan.current_pr.title != pr_plan.state.title
            or pr_plan.current_pr.body != pr_plan.body
        ):
            _write_pr_body_file(request=request, body=pr_plan.body)
            _edit_export_pr(request=request, title=pr_plan.state.title)
            return
        _log("Export produced no target repository changes.")
        return

    branch = request.branch
    message_file = request.runner_temp / "copybarista-commit-message.txt"
    message_file.write_text(
        _export_commit_message(
            title=pr_plan.state.title,
            sync_label=request.sync_label,
            branch=branch,
            source_digest=pr_plan.state.applied_source_digest,
            replay_base_digest=pr_plan.replay_base_digest,
            authors=pr_plan.state.authors,
        ),
        encoding="utf-8",
    )
    _write_pr_body_file(request=request, body=pr_plan.body)

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
            _generated_commit_author(
                authors=pr_plan.state.authors,
                fallback_name=request.sync_user_name,
                fallback_email=request.sync_user_email,
            ),
            "--file",
            str(message_file),
        ],
        cwd=request.public_dir,
    )
    _run(
        ["git", "push", "--force-with-lease", "origin", branch],
        cwd=request.public_dir,
    )

    if pr_plan.current_pr or _gh_pr_exists(
        branch=branch,
        repo=request.target_repo,
        cwd=request.public_dir,
    ):
        _edit_export_pr(request=request, title=pr_plan.state.title)
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
            pr_plan.state.title,
            "--body-file",
            str(_pr_body_file(request)),
        ],
        cwd=request.public_dir,
    )


def _enable_export_pr_auto_merge(*, request: ExportRequest, pr_title: str) -> None:
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
            pr_title,
            "--body",
            f"{request.sync_label} export branch: {request.branch}",
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
    default_title: str = DEFAULT_EXPORT_TITLE,
    default_body: str = DEFAULT_EXPORT_DESCRIPTION,
) -> ExportPrText:
    """Return public export PR text from manual inputs, commit text, or defaults."""
    message_title = ""
    message_body = ""
    if use_source_message and (not title.strip() or not body.strip()):
        message_title, message_body = _split_commit_message(source_message)
    resolved_title = title.strip() or message_title or default_title
    resolved_body = body.strip() or message_body or default_body
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


def export_pr_body(*, description: str, branch: str, sync_label: str) -> str:
    """Return the public export PR body."""
    return (
        f"{description.strip()}\n\n"
        "----\n"
        f"{sync_label} export branch: `{branch}`\n\n"
        "Do not push manual commits to this generated branch. Change the source "
        "repository, then rerun the export workflow with the same branch.\n"
    )


def _parse_pr_metadata_log(
    log_output: str, *, forbidden_text: tuple[str, ...], scope: str = ""
) -> tuple[PrMetadataPatch, ...]:
    """Parse NUL-framed git log output into PR metadata patches."""
    parts = [part for part in log_output.split("\0") if part]
    if len(parts) % 4 != 0:
        raise PrMetadataError("git log PR metadata output was not NUL-framed records.")
    patches: list[PrMetadataPatch] = []
    for idx in range(0, len(parts), 4):
        commit_patches = _parse_pr_metadata_message(
            commit_sha=parts[idx],
            commit_author=SourceAuthor(name=parts[idx + 1], email=parts[idx + 2]),
            message=parts[idx + 3],
            forbidden_text=forbidden_text,
            scope=scope,
        )
        patches.extend(commit_patches)
    return tuple(patches)


def _parse_pr_metadata_message(
    *,
    commit_sha: str,
    commit_author: SourceAuthor,
    message: str,
    forbidden_text: tuple[str, ...],
    scope: str,
) -> tuple[PrMetadataPatch, ...]:
    """Parse one commit message's Copybarista PR metadata."""
    target_scope = _normalized_scope(scope)
    lines = message.splitlines()
    patches: list[PrMetadataPatch] = []
    idx = 0
    values: dict[str, str] = {}
    block_scope = ""
    while idx < len(lines):
        line = lines[idx]
        field, separator, value = line.partition(":")
        if field == "Copybarista-PR-Scope" and separator:
            patch = _patch_from_metadata_block(
                commit_sha=commit_sha,
                commit_author=commit_author,
                values=values,
                body="",
                block_scope=block_scope,
                target_scope=target_scope,
                forbidden_text=forbidden_text,
            )
            if patch:
                patches.append(patch)
            values = {}
            block_scope = value.strip()
            idx += 1
            continue
        if line.startswith("Copybarista-PR-Body:"):
            if "Body" in values:
                raise _metadata_error(commit_sha, "Body", "duplicate field")
            body_lines, next_idx = _body_lines_until_next_scope(lines, start=idx + 1)
            for body_line in body_lines:
                if body_line.startswith("Copybarista-PR-") and not body_line.startswith(
                    "Copybarista-PR-Scope:"
                ):
                    raise _metadata_error(
                        commit_sha,
                        "Body",
                        "body must be the final Copybarista-PR-* field",
                    )
            body = "\n".join(body_lines).strip()
            values["Body"] = body
            patch = _patch_from_metadata_block(
                commit_sha=commit_sha,
                commit_author=commit_author,
                values=values,
                body=body,
                block_scope=block_scope,
                target_scope=target_scope,
                forbidden_text=forbidden_text,
            )
            if patch:
                patches.append(patch)
            values = {}
            block_scope = ""
            idx = next_idx
            continue
        if field.startswith("Copybarista-PR-") and separator:
            name = field.removeprefix("Copybarista-PR-")
            if name == "Author":
                raise _metadata_error(
                    commit_sha,
                    name,
                    "unsupported; use the git author for attribution",
                )
            if name not in {"Title", "Body-Mode", "Scope"}:
                raise _metadata_error(commit_sha, name, "unknown field")
            if name in values:
                raise _metadata_error(commit_sha, name, "duplicate field")
            values[name] = value.strip()
        idx += 1
    patch = _patch_from_metadata_block(
        commit_sha=commit_sha,
        commit_author=commit_author,
        values=values,
        body="",
        block_scope=block_scope,
        target_scope=target_scope,
        forbidden_text=forbidden_text,
    )
    if patch:
        patches.append(patch)
    return tuple(patches)


def _patch_from_metadata_block(
    *,
    commit_sha: str,
    commit_author: SourceAuthor,
    values: dict[str, str],
    body: str,
    block_scope: str,
    target_scope: str,
    forbidden_text: tuple[str, ...],
) -> PrMetadataPatch | None:
    """Build one scoped metadata patch when it applies to this export."""
    if not values:
        return None
    normalized_block_scope = _normalized_scope(block_scope)
    if normalized_block_scope and normalized_block_scope != target_scope:
        return None
    mode = values.get("Body-Mode", "append")
    if mode not in {"append", "replace"}:
        raise _metadata_error(commit_sha, "Body-Mode", "must be append or replace")
    if "Body-Mode" in values and "Body" not in values:
        raise _metadata_error(commit_sha, "Body-Mode", "requires Copybarista-PR-Body")
    _validate_metadata_text(
        commit_sha=commit_sha,
        field="Title",
        value=values.get("Title", ""),
        forbidden_text=forbidden_text,
    )
    _validate_metadata_text(
        commit_sha=commit_sha,
        field="Body",
        value=body,
        forbidden_text=forbidden_text,
    )
    return PrMetadataPatch(
        commit_sha=commit_sha,
        scope=normalized_block_scope,
        title=values.get("Title", ""),
        author=_validated_source_author(
            commit_sha=commit_sha,
            author=commit_author,
            forbidden_text=forbidden_text,
        ),
        body=body,
        body_mode=mode,
    )


def _body_lines_until_next_scope(
    lines: list[str], *, start: int
) -> tuple[list[str], int]:
    """Return body lines up to the next scoped metadata block."""
    body_lines: list[str] = []
    idx = start
    while idx < len(lines):
        if lines[idx].startswith("Copybarista-PR-Scope:"):
            break
        body_lines.append(lines[idx])
        idx += 1
    return body_lines, idx


def _validated_source_author(
    *, commit_sha: str, author: SourceAuthor, forbidden_text: tuple[str, ...]
) -> SourceAuthor:
    """Return a source author suitable for public git attribution."""
    if not author.name.strip() or not author.email.strip():
        raise _metadata_error(
            commit_sha,
            "Author",
            "source commit author name and email are required for attribution",
        )
    _validate_metadata_text(
        commit_sha=commit_sha,
        field="Author",
        value=_commit_author(author.name, author.email),
        forbidden_text=forbidden_text,
    )
    return SourceAuthor(name=author.name.strip(), email=author.email.strip())


def _normalized_scope(scope: str) -> str:
    """Return a normalized metadata scope label."""
    return scope.strip().casefold()


def replay_pr_metadata(
    *, base: PrReplayState, patches: tuple[PrMetadataPatch, ...]
) -> PrReplayState:
    """Apply source commit PR metadata patches in chronological order."""
    state = base
    seen: set[str] = {
        entry.commit_sha for entry in state.body_entries if entry.commit_sha
    }
    for patch in patches:
        entries = state.body_entries
        body_intro = state.body_intro
        authors = state.authors
        if patch.body_mode == "replace" and patch.body:
            body_intro = patch.body
            entries = ()
            seen = set[str]()
        elif patch.body and patch.commit_sha not in seen:
            entries = (
                *entries,
                PrBodyEntry(commit_sha=patch.commit_sha, text=patch.body),
            )
            seen.add(patch.commit_sha)
        authors = _append_source_author(authors=authors, author=patch.author)
        state = PrReplayState(
            title=patch.title or state.title,
            authors=authors,
            body_intro=body_intro,
            body_entries=entries,
            applied_source_rev=state.applied_source_rev,
            applied_source_digest=state.applied_source_digest,
            metadata_count=state.metadata_count + 1,
        )
    return state


def _append_source_author(
    *, authors: tuple[SourceAuthor, ...], author: SourceAuthor
) -> tuple[SourceAuthor, ...]:
    """Append one source author unless an equivalent email is already present."""
    normalized_email = author.email.casefold()
    if any(existing.email.casefold() == normalized_email for existing in authors):
        return authors
    return (*authors, author)


def _render_pr_body(
    *,
    state: PrReplayState,
    branch: str,
    sync_label: str,
    publish_source_rev: bool,
    pr_template: str = "",
) -> str:
    """Render the public PR body and Copybarista replay marker."""
    if not state.title.strip() or not state.body_intro.strip():
        raise PrReplayError("Cannot render PR body with empty title or description.")
    marker_value = (
        f"sha:{state.applied_source_rev}"
        if publish_source_rev
        else f"sha256:{state.applied_source_digest}"
    )
    if marker_value == "sha256:":
        raise PrReplayError("Cannot render PR body without an applied source marker.")
    lines = _render_pr_summary_lines(state)
    footer = "\n".join(
        (
            "",
            "----",
            f"{sync_label} export branch: `{branch}`",
            "",
            "Do not push manual commits to this generated branch. Change the source "
            "repository, then rerun the export workflow with the same branch.",
            "",
            f"{PR_MARKER_PREFIX}version={PR_STATE_VERSION} applied={marker_value} -->",
        )
    )
    body = (
        _render_pr_template_body(template=pr_template, summary_lines=lines)
        if pr_template.strip()
        else "\n".join(lines)
    )
    body = f"{body.rstrip()}\n{footer}\n"
    if body.count(PR_MARKER_PREFIX) != 1:
        raise PrReplayError("Rendered PR body must contain exactly one state marker.")
    return body


def _render_pr_summary_lines(state: PrReplayState) -> list[str]:
    """Return managed reviewer-facing summary lines."""
    lines = [state.body_intro.strip()]
    if state.body_entries:
        lines.extend(("", "### Updates", ""))
        for entry in state.body_entries:
            entry_marker = _entry_marker(entry)
            if entry_marker:
                lines.append(entry_marker)
            lines.extend(_render_body_entry(entry.text))
    return lines


def _render_pr_template_body(*, template: str, summary_lines: list[str]) -> str:
    """Fill a repository PR template without owning non-summary sections."""
    lines = template.strip("\n").splitlines()
    result: list[str] = []
    idx = 0
    wrote_summary = False
    while idx < len(lines):
        heading = _markdown_heading(lines[idx])
        if heading == "Summary":
            result.append(lines[idx])
            result.extend(("", *summary_lines, ""))
            idx = _next_section(lines, start=idx + 1)
            wrote_summary = True
            continue
        if heading in PR_VALIDATION_TEMPLATE_SECTIONS:
            result.append(lines[idx])
            idx += 1
            while idx < len(lines) and not _markdown_heading(lines[idx]):
                result.append(_checked_template_line(lines[idx]))
                idx += 1
            continue
        if heading in PR_DROPPED_TEMPLATE_SECTIONS:
            idx = _next_section(lines, start=idx + 1)
            continue
        result.append(lines[idx])
        idx += 1
    if wrote_summary:
        return "\n".join(result).rstrip()
    return "\n".join((*summary_lines, "", *result)).rstrip()


def _read_pr_template(public_dir: Path) -> str:
    """Read the target repository's PR template if it has one."""
    for relative_path in PR_TEMPLATE_PATHS:
        path = public_dir / relative_path
        if path.is_file():
            return path.read_text(encoding="utf-8")
    return ""


def _markdown_heading(line: str) -> str:
    """Return a level-two Markdown heading title."""
    if not line.startswith("## "):
        return ""
    return line.removeprefix("## ").strip()


def _next_section(lines: list[str], *, start: int) -> int:
    """Return the next level-two section index."""
    idx = start
    while idx < len(lines) and not _markdown_heading(lines[idx]):
        idx += 1
    return idx


def _checked_template_line(line: str) -> str:
    """Mark validation checklist items as completed."""
    stripped = line.lstrip()
    if stripped.startswith("- [ ]"):
        return line.replace("[ ]", "[x]", 1)
    return line


def _state_from_pr_body(*, title: str, body: str, default_body: str) -> PrReplayState:
    """Parse Copybarista-owned PR body state from the current public PR."""
    marker = _applied_marker_from_pr_body(body)
    if not marker:
        raise PrReplayError("Existing generated PR body has no Copybarista marker.")
    content = _body_before_footer(body).strip()
    content = _summary_section(content) or content
    if not content:
        content = default_body
    intro_text, entries_text = _split_updates_section(content)
    return PrReplayState(
        title=title,
        authors=(),
        body_intro=_drop_legacy_source_attribution(intro_text) or default_body,
        body_entries=_parse_body_entries(entries_text),
        applied_source_rev="",
        applied_source_digest=marker.removeprefix("sha256:")
        if marker.startswith("sha256:")
        else "",
        metadata_count=0,
    )


def _drop_legacy_source_attribution(text: str) -> str:
    """Remove old PR-body attribution now represented by git metadata."""
    return "\n".join(
        line for line in text.splitlines() if not line.startswith("Source attribution:")
    ).strip()


def _summary_section(body: str) -> str:
    """Return a templated PR body's managed Summary section."""
    lines = body.splitlines()
    for idx, line in enumerate(lines):
        if _markdown_heading(line) == "Summary":
            end = _next_section(lines, start=idx + 1)
            return "\n".join(lines[idx + 1 : end]).strip()
    return ""


def _applied_marker_from_pr_body(body: str) -> str:
    """Return the applied source marker from a generated PR body."""
    markers = [
        line.strip()
        for line in body.splitlines()
        if line.strip().startswith(PR_MARKER_PREFIX)
    ]
    if len(markers) > 1:
        raise PrReplayError("PR body has multiple Copybarista state markers.")
    if not markers:
        return ""
    marker = markers[0]
    if f"version={PR_STATE_VERSION}" not in marker:
        raise PrReplayError("PR body has unsupported Copybarista state marker version.")
    for token in marker.removeprefix(PR_MARKER_PREFIX).removesuffix(" -->").split():
        if token.startswith("applied="):
            return token.removeprefix("applied=")
    raise PrReplayError("PR body state marker is missing applied source revision.")


def _branch_markers(*, branch: str, cwd: Path) -> BranchMarkers:
    """Read Copybarista replay markers from the current generated branch."""
    _fetch_branch(branch=branch, cwd=cwd)
    ref = f"refs/remotes/origin/{branch}"
    exists = _run(
        ["git", "rev-parse", "--verify", ref], cwd=cwd, check=False, capture=True
    )
    if exists.returncode != 0:
        return BranchMarkers(source_digest="", replay_base_digest="", exists=False)
    result = _run(["git", "log", "-1", "--format=%B", ref], cwd=cwd, capture=True)
    source_digest = ""
    replay_base_digest = ""
    for line in result.stdout.splitlines():
        key, separator, value = line.partition("=")
        if separator and key == "copybarista-source-rev-sha256":
            source_digest = value.strip()
        if separator and key == "copybarista-replay-base-sha256":
            replay_base_digest = value.strip()
    return BranchMarkers(
        source_digest=source_digest,
        replay_base_digest=replay_base_digest,
        exists=True,
    )


def _resolve_source_marker(*, source_dir: Path, marker: str, marker_source: str) -> str:
    """Resolve a public marker back to a private source commit."""
    if marker.startswith("sha:"):
        return marker.removeprefix("sha:")
    if marker.startswith("sha256:"):
        return _resolve_source_digest(
            source_dir=source_dir,
            digest=marker.removeprefix("sha256:"),
            marker_source=marker_source,
        )
    raise PrReplayError(f"{marker_source} has unsupported source marker {marker}.")


def _resolve_source_digest(*, source_dir: Path, digest: str, marker_source: str) -> str:
    """Resolve a source SHA digest by scanning the source checkout history."""
    result = _run(["git", "rev-list", "HEAD"], cwd=source_dir, capture=True)
    for commit_sha in result.stdout.splitlines():
        if _source_rev_digest(commit_sha) == digest:
            return commit_sha
    raise PrReplayError(
        f"Cannot resolve {marker_source} digest {digest[:12]} in {source_dir}."
    )


def _source_parent(*, source_dir: Path, rev: str) -> str:
    """Return the source parent revision, or empty string for a root commit."""
    result = _run(
        ["git", "rev-parse", f"{rev}^"], cwd=source_dir, check=False, capture=True
    )
    if result.returncode == 0:
        return result.stdout.strip()
    return ""


def _source_rev_digest(rev: str) -> str:
    """Return the public-safe digest for a private source revision."""
    return hashlib.sha256(rev.encode("utf-8")).hexdigest()


def _metadata_error(commit_sha: str, field: str, reason: str) -> PrMetadataError:
    """Build a commit-specific PR metadata error."""
    return PrMetadataError(
        f"Commit {_short_rev(commit_sha)} Copybarista-PR-{field}: {reason}."
    )


def _validate_metadata_text(
    *, commit_sha: str, field: str, value: str, forbidden_text: tuple[str, ...]
) -> None:
    """Reject source-specific text in one public metadata field."""
    if not value:
        return
    lowered = value.casefold()
    if any(term.casefold() in lowered for term in forbidden_text):
        raise _metadata_error(
            commit_sha, field, "contains restricted source-specific text"
        )


def _replace_pr_state(
    state: PrReplayState,
    *,
    title: str = "",
    body_intro: str = "",
    applied_source_rev: str = "",
    applied_source_digest: str = "",
) -> PrReplayState:
    """Return `state` with selected scalar fields replaced."""
    return PrReplayState(
        title=title or state.title,
        authors=state.authors,
        body_intro=body_intro or state.body_intro,
        body_entries=state.body_entries,
        applied_source_rev=applied_source_rev or state.applied_source_rev,
        applied_source_digest=applied_source_digest or state.applied_source_digest,
        metadata_count=state.metadata_count,
    )


def _body_before_footer(body: str) -> str:
    """Return the managed body before the generated footer."""
    without_marker = "\n".join(
        line
        for line in body.splitlines()
        if not line.strip().startswith(PR_MARKER_PREFIX)
    )
    before_footer, separator, _footer = without_marker.partition("\n----\n")
    if not separator:
        raise PrReplayError("PR body is missing the Copybarista export footer.")
    return before_footer


def _split_updates_section(content: str) -> tuple[str, str]:
    """Split managed PR prose into intro and updates sections."""
    intro, separator, updates = content.partition("\n### Updates\n")
    return intro.strip(), updates.strip() if separator else ""


def _parse_body_entries(entries_text: str) -> tuple[PrBodyEntry, ...]:
    """Parse rendered append entries from the managed PR body."""
    if not entries_text:
        return ()
    entries: list[PrBodyEntry] = []
    current_marker = ""
    current_lines: list[str] = []
    for line in entries_text.splitlines():
        stripped = line.strip()
        if stripped.startswith(PR_ENTRY_PREFIX):
            _append_parsed_body_entry(
                entries=entries,
                commit_sha=current_marker,
                lines=current_lines,
            )
            current_marker = _entry_commit_marker(stripped)
            current_lines = []
        elif stripped.startswith("- "):
            _append_parsed_body_entry(
                entries=entries,
                commit_sha=current_marker,
                lines=current_lines,
            )
            current_lines = [stripped.removeprefix("- ").strip()]
            current_marker = ""
        elif line.startswith("  ") and current_lines:
            current_lines.append(line.strip())
        elif stripped:
            raise PrReplayError("PR body has ambiguous text in Copybarista updates.")
    _append_parsed_body_entry(
        entries=entries,
        commit_sha=current_marker,
        lines=current_lines,
    )
    return tuple(entries)


def _append_parsed_body_entry(
    *, entries: list[PrBodyEntry], commit_sha: str, lines: list[str]
) -> None:
    """Append a parsed body entry if one is in progress."""
    if not lines:
        return
    entries.append(PrBodyEntry(commit_sha=commit_sha, text="\n".join(lines).strip()))


def _render_body_entry(text: str) -> tuple[str, ...]:
    """Render one append entry as a stable Markdown bullet."""
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
    if not lines:
        return ()
    return (f"- {lines[0]}", *(f"  {line}" for line in lines[1:]))


def _entry_marker(entry: PrBodyEntry) -> str:
    """Return the public-safe marker for an appended body entry."""
    if not entry.commit_sha:
        return ""
    return f"{PR_ENTRY_PREFIX}source=sha256:{_source_rev_digest(entry.commit_sha)} -->"


def _entry_commit_marker(line: str) -> str:
    """Return the stored public-safe marker for an append entry."""
    for token in line.removeprefix(PR_ENTRY_PREFIX).removesuffix(" -->").split():
        if token.startswith("source="):
            return token.removeprefix("source=")
    raise PrReplayError("PR body entry marker is missing source.")


def _write_pr_body_file(*, request: ExportRequest, body: str) -> None:
    """Write the rendered PR body file used by GitHub CLI commands."""
    _pr_body_file(request).write_text(body, encoding="utf-8")


def _pr_body_file(request: ExportRequest) -> Path:
    """Return the temporary PR body file path."""
    return request.runner_temp / "copybarista-pr-body.md"


def _edit_export_pr(*, request: ExportRequest, title: str) -> None:
    """Update the current generated export PR."""
    _run_gh(
        [
            "gh",
            "pr",
            "edit",
            request.branch,
            "--repo",
            request.target_repo,
            "--title",
            title,
            "--body-file",
            str(_pr_body_file(request)),
        ],
        cwd=request.public_dir,
    )


def _export_commit_message(
    *,
    title: str,
    sync_label: str,
    branch: str,
    source_digest: str,
    replay_base_digest: str,
    authors: tuple[SourceAuthor, ...] = (),
) -> str:
    """Return the generated export commit message with replay markers."""
    lines = [
        title.strip(),
        "",
        f"{sync_label} export branch: {branch}",
        f"copybarista-source-rev-sha256={source_digest}",
        f"copybarista-replay-base-sha256={replay_base_digest}",
        f"copybarista-pr-state-version={PR_STATE_VERSION}",
    ]
    lines.extend(
        f"Co-authored-by: {_commit_author(author.name, author.email)}"
        for author in authors[1:]
    )
    return "\n".join(lines) + "\n"


def _short_rev(rev: str) -> str:
    """Return a short revision label for diagnostics."""
    return rev[:7] if rev else "<root>"


def _split_commit_message(message: str) -> tuple[str, str]:
    """Split a commit message into title and description."""
    title, separator, description = message.strip().partition("\n")
    if not title.strip():
        raise SystemExit("--source-message or --pr-title is required.\n")
    return title.strip(), description.strip() if separator else ""


def export_branch_name(
    *, explicit: str, source_branch: str, source_sha: str, prefix: str
) -> str:
    """Return the source-to-public sync branch name."""
    if explicit.strip():
        return _validated_generated_branch(branch=explicit.strip(), prefix=prefix)
    if source_branch.strip():
        branch = f"{prefix}{_branch_component(source_branch)}"
    else:
        branch = f"{prefix}sha-{_branch_component(source_sha[:12])}"
    return _validated_generated_branch(branch=branch, prefix=prefix)


def _commit_author(name: str, email: str) -> str:
    """Return the Git author identity for a generated sync commit."""
    return f"{name} <{email}>"


def _generated_commit_author(
    *, authors: tuple[SourceAuthor, ...], fallback_name: str, fallback_email: str
) -> str:
    """Return the primary generated commit author identity."""
    if authors:
        return _commit_author(authors[0].name, authors[0].email)
    return _commit_author(fallback_name, fallback_email)


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
