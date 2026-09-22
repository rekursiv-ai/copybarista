"""Copybarista command-line interface."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, cast

import argparse
import json
import sys

from copybarista.config import load_config
from copybarista.copy_bara_sky import (
    translate_copy_bara_sky_to_toml,
)
from copybarista.errors import (
    ConfigError,
    CopybaristaError,
    ExportError,
    GlobError,
    ImportRequestError,
    LeakCheckError,
    OutputMismatchError,
    TransformError,
)
from copybarista.export import export_folder
from copybarista.git import export_git
from copybarista.import_request import (
    ImportRequest,
    import_change_request,
)
from copybarista.leak_check import enforce_leak_check
from copybarista.sync_setup import (
    DEFAULT_SYNC_USER_EMAIL,
    DEFAULT_SYNC_USER_NAME,
    DEFAULT_VALIDATION_PYTHON_VERSIONS,
    SyncSettings,
    check_sync_config,
    export_workflow,
    import_workflow,
    import_workflow_filename,
    load_sync_settings,
    package_validation_workflow,
    write_sync_scaffold,
)


def main(argv: list[str] | None = None) -> int:
    """Run the Copybarista CLI. Return the process exit code.

    Args:
      argv: Command-line arguments; defaults to sys.argv[1:].

    Returns:
      exit_code: 0 on success, non-zero on CopybaristaError or command failure.

    """
    parser = _parser()
    flags = cast(_Flags, parser.parse_args(argv))
    try:
        {
            "validate": _run_validate,
            "translate": _run_translate,
            "export": _run_export,
            "publish-git": _run_publish_git,
            "check-leaks": _run_check_leaks,
            "import-change": _run_import_change,
            "init-sync": _run_init_sync,
            "check-sync-config": _run_check_sync_config,
            "write-export-workflow": _run_write_export_workflow,
            "write-public-workflows": _run_write_public_workflows,
        }[flags.command](flags)
    except CopybaristaError as err:
        sys.stderr.write(f"{err}\n")
        return _exit_code(err)
    return 0


class _Flags(Protocol):
    """Parsed command-line flags across every subcommand.

    One Protocol, not one per subparser: argparse returns a single namespace
    and each ``_run_*`` reads only the attributes its subparser registered.
    """

    command: str
    config: str
    workflow: str
    output: str
    source_ref: str
    folder_dir: str
    force: bool
    json: bool
    root: str
    public_base: str
    public_head: str
    source_base: str
    destination: str
    no_verify: bool
    merge_import: bool
    package_name: str
    sync_label: str
    source_root: str
    public_repo: str
    source_repo: str
    copybarista_project_path: str
    smoke_import: str
    release_check_script: str
    type_check_target: list[str]
    forbidden_pr_text: list[str]
    validation_python_version: list[str]
    validation_command: list[str]
    refresh_public_lockfile: bool
    sync_user_name: str
    sync_user_email: str
    overwrite: bool
    sync_config: str


def _parser() -> argparse.ArgumentParser:
    """Build the CLI parser without executing any command behavior."""
    parser = argparse.ArgumentParser(prog="copybarista")
    sub = parser.add_subparsers(dest="command", required=True)

    validate = sub.add_parser("validate", help="Validate a Copybarista config")
    validate.add_argument("config")
    validate.add_argument("--workflow", default="export")

    translate = sub.add_parser(
        "translate",
        help="Translate a supported copy.bara.sky workflow to Copybarista TOML",
    )
    translate.add_argument("config")
    translate.add_argument("--workflow", default="export")
    translate.add_argument("--output", default="")

    export = sub.add_parser("export", help="Export to a local folder")
    export.add_argument("config")
    export.add_argument("source_ref")
    export.add_argument("--folder-dir", default="")
    export.add_argument("--workflow", default="export")
    export.add_argument("--force", action="store_true")
    export.add_argument("--json", action="store_true")

    publish_git = sub.add_parser(
        "publish-git",
        help="Publish an exported tree to a Git destination",
    )
    publish_git.add_argument("config")
    publish_git.add_argument("source_ref")
    publish_git.add_argument("--workflow", default="export_git")
    publish_git.add_argument("--json", action="store_true")

    check_leaks = sub.add_parser(
        "check-leaks",
        help="Check a tree against config leak policy",
    )
    check_leaks.add_argument("config")
    check_leaks.add_argument("root")
    check_leaks.add_argument("--workflow", default="export")

    import_change = sub.add_parser(
        "import-change",
        help="Import a public change into a source-of-truth checkout",
    )
    import_change.add_argument("config")
    import_change.add_argument("--public-base", required=True)
    import_change.add_argument("--public-head", required=True)
    import_change.add_argument("--source-base", required=True)
    import_change.add_argument("--destination", required=True)
    import_change.add_argument("--workflow", default="export")
    import_change.add_argument("--no-verify", action="store_true")
    import_change.add_argument(
        "--merge-import",
        action="store_true",
        help="Three-way merge each change instead of requiring the source to "
        "reproduce the public base exactly.",
    )
    import_change.add_argument("--json", action="store_true")

    init_sync = sub.add_parser("init-sync", help="Write package sync scaffolding")
    init_sync.add_argument("root")
    init_sync.add_argument("--package-name", required=True)
    init_sync.add_argument("--sync-label", default="")
    init_sync.add_argument("--source-root", required=True)
    init_sync.add_argument("--public-repo", required=True)
    init_sync.add_argument("--source-repo", required=True)
    init_sync.add_argument("--copybarista-project-path", required=True)
    init_sync.add_argument("--smoke-import", required=True)
    init_sync.add_argument("--release-check-script", default="")
    init_sync.add_argument("--type-check-target", action="append", default=[])
    init_sync.add_argument("--forbidden-pr-text", action="append", default=[])
    init_sync.add_argument("--validation-python-version", action="append", default=[])
    init_sync.add_argument("--validation-command", action="append", default=[])
    init_sync.add_argument("--refresh-public-lockfile", action="store_true")
    init_sync.add_argument("--sync-user-name", default=DEFAULT_SYNC_USER_NAME)
    init_sync.add_argument("--sync-user-email", default=DEFAULT_SYNC_USER_EMAIL)
    init_sync.add_argument("--overwrite", action="store_true")

    check_sync = sub.add_parser(
        "check-sync-config",
        help="Validate package sync scaffolding",
    )
    check_sync.add_argument("root")

    export_sync = sub.add_parser(
        "write-export-workflow",
        help="Write a source-repository export workflow to stdout or a file",
    )
    export_sync.add_argument("sync_config")
    export_sync.add_argument("--output", default="")

    public_sync = sub.add_parser(
        "write-public-workflows",
        help="Rewrite a package's generated .export/ workflows in place",
    )
    public_sync.add_argument("sync_config")

    return parser


def _run_validate(flags: _Flags) -> None:
    """Validate config and exit silently on success."""
    load_config(Path(flags.config), workflow_name=flags.workflow)


def _run_translate(flags: _Flags) -> None:
    """Translate a supported `copy.bara.sky` workflow to native TOML."""
    translated = translate_copy_bara_sky_to_toml(
        Path(flags.config),
        workflow_name=flags.workflow,
    )
    if flags.output:
        Path(flags.output).write_text(translated, encoding="utf-8")
    else:
        sys.stdout.write(translated)


def _run_export(flags: _Flags) -> None:
    """Run a folder export command."""
    config = load_config(Path(flags.config), workflow_name=flags.workflow)
    if not flags.folder_dir and not config.folder.path:
        raise ConfigError(
            "copybarista export requires --folder-dir or destination.folder.path",
        )
    manifest = export_folder(
        config=config,
        source_ref=Path(flags.source_ref),
        destination=Path(flags.folder_dir or config.folder.path),
        force=flags.force,
    )
    if flags.json:
        sys.stdout.write(manifest.to_json())


def _run_publish_git(flags: _Flags) -> None:
    """Run a Git publish command."""
    config = load_config(Path(flags.config), workflow_name=flags.workflow)
    manifest = export_git(config=config, source_ref=Path(flags.source_ref))
    if flags.json:
        sys.stdout.write(manifest.to_json())


def _run_check_leaks(flags: _Flags) -> None:
    """Run leak policy checks against an existing tree."""
    config = load_config(Path(flags.config), workflow_name=flags.workflow)
    # Thread ``config.globstar`` so the standalone gate applies the same ``**``
    # semantics as the export path (workflow.stage); omitting it defaults to
    # ``one_or_more`` and can pass a tree a ``zero_or_more`` export would fail.
    enforce_leak_check(
        root=Path(flags.root),
        policy=config.leak_check,
        globstar=config.globstar,
    )


def _run_import_change(flags: _Flags) -> None:
    """Run a local public-change import command."""
    config = load_config(Path(flags.config), workflow_name=flags.workflow)
    if flags.no_verify:
        sys.stderr.write(
            "Warning: --no-verify disables public-base and final re-export checks.\n",
        )
    result = import_change_request(
        ImportRequest(
            config=config,
            public_base=Path(flags.public_base),
            public_head=Path(flags.public_head),
            source_base=Path(flags.source_base),
            destination=Path(flags.destination),
            verify=not flags.no_verify,
            merge_import=flags.merge_import,
        ),
    )
    if flags.json:
        sys.stdout.write(json.dumps(result.to_dict(), indent=2, sort_keys=True) + "\n")


def _run_init_sync(flags: _Flags) -> None:
    """Write package sync scaffolding."""
    settings = SyncSettings(
        package_name=flags.package_name,
        sync_label=flags.sync_label or flags.package_name,
        source_root=flags.source_root,
        public_repo=flags.public_repo,
        source_repo=flags.source_repo,
        copybarista_project_path=flags.copybarista_project_path,
        smoke_import=flags.smoke_import,
        release_check_script=flags.release_check_script,
        type_check_targets=tuple(flags.type_check_target)
        or (flags.smoke_import, "tests"),
        forbidden_pr_text=tuple(flags.forbidden_pr_text),
        validation_python_versions=tuple(flags.validation_python_version)
        or DEFAULT_VALIDATION_PYTHON_VERSIONS,
        validation_commands=tuple(flags.validation_command),
        refresh_public_lockfile=flags.refresh_public_lockfile,
        sync_user_name=flags.sync_user_name,
        sync_user_email=flags.sync_user_email,
    )
    for path in write_sync_scaffold(
        root=Path(flags.root),
        settings=settings,
        force=flags.overwrite,
    ):
        sys.stdout.write(f"wrote {path}\n")


def _run_check_sync_config(flags: _Flags) -> None:
    """Validate package sync scaffolding."""
    check_sync_config(root=Path(flags.root))


def _run_write_export_workflow(flags: _Flags) -> None:
    """Write the source-repository export workflow."""
    settings = load_sync_settings(Path(flags.sync_config))
    text = export_workflow(settings)
    if flags.output:
        Path(flags.output).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)


# Both files bake in ``sync.validation_commands``, so changing that list must rewrite
# both or the byte-parity test fails on whichever was missed. ``write_sync_scaffold``
# would also overwrite the hand-tuned ``copy.barista.toml``, which is why this writes
# only the two.
def _run_write_public_workflows(flags: _Flags) -> None:
    """Rewrite the generated workflows under a package's ``.export/``."""
    config = Path(flags.sync_config)
    settings = load_sync_settings(config)
    workflows = config.parent / ".export/.github/workflows"
    for name, text in (
        ("package-validation.yml", package_validation_workflow(settings)),
        (import_workflow_filename(settings), import_workflow(settings)),
    ):
        (workflows / name).write_text(text, encoding="utf-8")
        sys.stdout.write(f"wrote {workflows / name}\n")


def _exit_code(err: CopybaristaError) -> int:
    """Return the release-gate exit code for a user-facing error."""
    if isinstance(err, (ConfigError, GlobError)):
        return 1
    if isinstance(err, (LeakCheckError, OutputMismatchError, TransformError)):
        return 2
    if isinstance(err, (ExportError, ImportRequestError)):
        return 3
    return 1
