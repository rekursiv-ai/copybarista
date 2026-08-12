#!/bin/sh
# ruff: noqa: EXE003, D300 -- Polyglot shell/Python script.
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
from typing import Final, TextIO

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time


def _copybarista_argv(*, requirements: Path) -> list[str]:
    """Return the argv prefix that runs copybarista dependency-free.

    `uv run --no-project` skips monorepo-root resolution (no torch/jax/tf/
    pycairo build); --with-requirements supplies copybarista's only third-party
    deps (see _export_copybarista_requirements). The package imports under its
    monorepo path, so callers run this with cwd=target_dir. A project
    dependency-group cannot install here because --group is ignored under
    --no-project; the group is instead exported to a requirements file.

    `requirements` must be absolute: callers run with cwd=target_dir, so a
    relative path would be resolved twice (target/target/...).
    """
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


def _export_copybarista_requirements(*, target_dir: Path, runner_temp: Path) -> Path:
    """Export the `copybarista` dependency group from the lock to a pinned file.

    `uv run --with-requirements` consults no lockfile, so to keep copybarista's
    standalone deps reproducible (and matched to the monorepo) we export the
    `copybarista` group from the checkout's uv.lock into a requirements file and
    feed that. This is the single source of truth -- no separately maintained
    version pins.
    """
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


DEFAULT_RUNNER_TEMP = Path(tempfile.gettempdir())
DEFAULT_SYNC_LABEL: Final = "Copybarista"
DEFAULT_SYNC_USER_EMAIL: Final = "copybarista@example.com"
DEFAULT_SYNC_USER_NAME: Final = "copybarista"
CONTROL_CHAR_BOUND: Final = 32
GITHUB_RETRY_ATTEMPTS: Final = 3
GITHUB_RETRY_DELAY_SEC: Final = 2


def main() -> int:
    """The main function. Return the process exit code."""
    return run()


def run(argv: list[str] | None = None) -> int:
    """Run public-to-source import validation and optional PR creation."""
    args = _parser().parse_args(argv)
    if args.print_synced_base:
        # Resolve the merge baseline from the target's own import history and
        # print it for the workflow to consume; no import request is built.
        sys.stdout.write(
            last_synced_public_sha(
                target_dir=Path(args.target_dir).resolve(),
                sync_label=args.sync_label,
                base_branch=args.base_branch,
                fallback=args.fallback_sha,
            )
            + "\n"
        )
        return 0
    for name in ("project_path", "public_base_ref", "public_head_ref"):
        if getattr(args, name) is None:
            _parser().error(f"--{name.replace('_', '-')} is required for an import")
    # Resolve filesystem inputs to absolute paths. Copybarista subprocesses run
    # with cwd=target_dir; relative path args would otherwise be resolved a
    # second time against that cwd (target/target/...).
    request = ImportRequest(
        public_base=Path(args.public_base).resolve(),
        public_head=Path(args.public_head).resolve(),
        target_dir=Path(args.target_dir).resolve(),
        target_repo=args.target_repo,
        project_path=Path(args.project_path),
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
        report=Path(args.report).resolve(),
        open_pr=_string_bool(args.open_pr),
        auto_merge=_string_bool(args.auto_merge),
        open_pr_only=args.open_pr_only,
        runner_temp=Path(args.runner_temp).resolve(),
        validation_commands=tuple(args.validation_command),
        refresh_public_lockfile=args.refresh_public_lockfile,
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


def run_import_sync(request: ImportRequest) -> None:
    """Import public changes into source, validate, and optionally open a PR."""
    project = request.target_dir / request.project_path
    if request.open_pr_only:
        _log("Opening or updating target import PR.")
        _open_or_update_target_pr(request=request)
        return
    requirements = _export_copybarista_requirements(
        target_dir=request.target_dir, runner_temp=request.runner_temp
    )
    # A failure anywhere below lands no ledger marker, and the export guard
    # reads that ledger -- so this failing does not merely lose one import, it
    # stops the project exporting until a marker lands. Say so where the
    # failure is read, since a bare traceback hides the consequence.
    try:
        _log("Importing public changes into target source.")
        _run_import_change(request=request, project=project, requirements=requirements)
        _log("Validating target checkout.")
        _validate_target(
            request=request,
            project=project,
            validation_commands=request.validation_commands,
            runner_temp=request.runner_temp,
            requirements=requirements,
        )
    except BaseException:
        _log(
            f"::error::The {request.sync_label} import of public commit "
            f"{request.public_sha[:12]} failed, so no import is recorded for "
            "it. Until one lands, the export guard blocks every "
            f"{request.sync_label} export and the public repository stops "
            "receiving updates."
        )
        raise
    if request.open_pr:
        _log("Opening or updating target import PR.")
        _open_or_update_target_pr(request=request)


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
        "--print-synced-base",
        action="store_true",
        help=(
            "Print the public SHA the target last imported (from its history) "
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
    return parser


def _run_import_change(
    *, request: ImportRequest, project: Path, requirements: Path
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
                # instead of demanding exact base reproduction. With no drift
                # this is byte-identical to a strict import.
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


def _validate_target(
    *,
    request: ImportRequest,
    project: Path,
    validation_commands: tuple[str, ...],
    runner_temp: Path,
    requirements: Path,
) -> None:
    """Validate the imported change by checking its exported public tree.

    The imported tree under ``project`` is monorepo-form (``loop.*`` imports,
    monorepo-root deps). Validating it directly would require the monorepo
    environment -- including the ML stack (torch, jax, tf, pycairo, ...) a CPU
    import runner cannot build. Instead this exports the public-form tree (the
    artifact actually published: ``sagent.*`` imports, public ``pyproject.toml``
    / ``uv.lock``) and runs the validation commands against *that*. The exported
    env has the package's real runtime deps but never the monorepo ML stack, so
    a CPU runner validates exactly what ships.

    ``validation_commands`` is the single source of truth
    (``copybarista.sync.toml`` ``sync.validation_commands``) that also drives the
    public repository's ``package-validation.yml`` and the source-to-public
    export gate. Running the same shell commands here makes all three verify
    byte-identical checks. Each command runs through ``bash -c`` in the exported
    tree, so it self-contains its ``uv sync`` and may use shell features.
    """
    tree = _export_public_tree(
        request=request,
        project=project,
        runner_temp=runner_temp,
        requirements=requirements,
    )
    for command in validation_commands:
        _run(["bash", "-c", command], cwd=tree)


def _export_public_tree(
    *, request: ImportRequest, project: Path, runner_temp: Path, requirements: Path
) -> Path:
    """Export the public-form tree for the imported project and return its path.

    Runs ``copybarista export`` (dependency-free) against the post-import source
    checkout, producing the transformed public package (``sagent.*`` imports,
    public ``pyproject.toml`` / ``uv.lock``) under a fresh directory.
    """
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
    _run(["git", "init", "--quiet"], cwd=tree)
    _run(["git", "add", "--all"], cwd=tree)
    return tree


def _open_or_update_target_pr(*, request: ImportRequest) -> None:
    """Commit the import and create or update the target PR.

    Commits even when the merge produced no file changes. Two different
    questions share this path: "is there a diff to review?" and "has the
    source absorbed this public SHA?". Only the first is answered by an empty
    tree. The export guard asks the second, and answers it by searching target
    history for this commit's subject -- so returning early here left the guard
    reading "not imported" for content the source demonstrably already had.

    An empty diff at this point is the strongest possible yes: the three-way
    merge applied without conflict and ``_validate_target`` passed the full
    gate suite on the exported result. A transform that dropped content, or a
    wrong ``--public-base``, raises before reaching this function.
    """
    if not _git_has_changes(path=request.target_dir, rel=request.project_path):
        _log("Import produced no target changes; recording the SHA anyway.")

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
            # The commit IS the ledger, so it must exist even with no diff.
            "--allow-empty",
            "--author",
            _commit_author(request.sync_user_name, request.sync_user_email),
            "-m",
            import_commit_subject_prefix(request.sync_label) + request.public_sha,
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
    """The SHA form the import PR title carries, which the ledger parses back."""
    return public_sha


def _merge_import_pr(
    *, branch: str, target_repo: str, title: str, sync_label: str, cwd: Path
) -> None:
    """Merge the import PR, preferring auto-merge.

    A clean import needs no human decision. The public change is already
    reviewed and already published; the source is the authority on how it
    RENDERS, not a second approval gate. Waiting for a click is also what
    stalls the other direction, since the export refuses to run while an
    import is outstanding.

    A conflicting or failing import never reaches here -- it raises before
    the PR is opened -- so this path only ever merges an import that applied
    cleanly and passed the same checks the source requires of any change.

    Mirrors the export's merge policy: ``--auto`` needs a deferrable merge
    (branch protection or pending checks), so a repo with neither rejects it
    and the merge is issued directly instead.

    Args:
      branch: The import branch to merge.
      target_repo: The source repository holding the PR.
      title: Squash-commit subject; the ledger reads the imported SHA from it.
      sync_label: Import label, used in the squash body.
      cwd: Directory to run the GitHub CLI from.

    """
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
            "merging the import PR directly."
        )
        _run_gh(merge_argv, cwd=cwd)
        return
    raise SystemExit(result.returncode)


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


class ImportBaseError(RuntimeError):
    """Raised when the target records no prior import to use as a baseline."""


def import_commit_subject_prefix(sync_label: str) -> str:
    """Return the fixed prefix of a landed import's commit subject."""
    return f"Import {sync_label} public changes "


def last_synced_public_sha(
    *, target_dir: Path, sync_label: str, base_branch: str, fallback: str = ""
) -> str:
    """Return the newest public SHA already imported into the target branch.

    The merge-import baseline must be the public commit the target tree
    currently reflects, not the pushed commit's parent. Those diverge whenever
    an import fails to land (validation error, unmerged PR, conflict): the
    parent marches forward while the target stays pinned to its last successful
    import, so a parent-based baseline feeds the three-way merge a wrong common
    ancestor and manufactures spurious conflicts.

    Each landed import records its public SHA in the commit subject
    (``Import <label> public changes <sha>``, written by
    ``_open_or_update_target_pr``), so the target's own history is the source of
    truth for what it last synced. Walk the branch newest-first and return the
    SHA from the first subject that matches the full template.

    Args:
      target_dir: Root of the target repository checkout.
      sync_label: Import label, e.g. ``Sagent``; scopes the commit search.
      base_branch: Target branch to walk, e.g. ``main``.
      fallback: SHA to return when the branch records no import -- a
        first-import baseline (e.g. the branch tip before the push). Empty
        string means raise.

    Returns:
      sha: The most recently imported public SHA, or ``fallback`` when the
        branch has no landed import and ``fallback`` is set.

    Raises:
      ImportBaseError: When the branch records no import commit and no fallback
        is provided.

    """
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
    # ``(#N)`` is GitHub's squash-merge suffix, which is how these imports
    # land. Anchoring on the SHA alone skipped every squash-merged commit and
    # silently returned an older baseline.
    pattern = re.compile(rf"^{re.escape(prefix)}([0-9a-f]{{40}})(?: \(#\d+\))?$")
    for subject in subjects:
        match = pattern.match(subject)
        if match is not None:
            sha = match[1]
            assert isinstance(sha, str)
            return sha
    if fallback:
        return fallback
    raise ImportBaseError(
        f"No landed '{sync_label}' import commit found on "
        f"'{base_branch}'; cannot resolve the merge baseline."
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


def _child_env() -> dict[str, str]:
    """Return the parent env without ``VIRTUAL_ENV``.

    Every command here runs through ``uv``, which re-derives the environment
    from the target ``--project`` / cwd. An inherited ``VIRTUAL_ENV`` (e.g. the
    operator's activated loop venv) only triggers uv's "does not match the
    project environment" warning, so drop it for a clean run.
    """
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
    result = subprocess.run(  # noqa: S603 -- args constructed internally, not from user input
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


def _run_gh(
    argv: list[str],
    *,
    cwd: Path,
    capture: bool = False,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run a GitHub CLI command with retries for transient API failures.

    With ``check=False`` a non-retryable failure is returned to the caller
    instead of raising, so the caller can inspect stderr and recover (the
    auto-merge fallback reads it to detect a repo that cannot defer merges).
    """
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
    """Write one flushed workflow log line to stderr.

    Diagnostics go to stderr so stdout stays a clean machine-readable channel
    (``--print-synced-base`` emits only the resolved SHA there). GitHub merges
    both streams into the Action log, so human-facing output is unchanged.
    """
    sys.stderr.write(f"{message}\n")
    sys.stderr.flush()


if __name__ == "__main__":
    raise SystemExit(main())
# vim: ft=python
