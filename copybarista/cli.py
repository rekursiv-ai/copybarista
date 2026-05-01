"""Copybarista command-line interface."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import argparse
import json
import sys

from copybarista.config import load_config
from copybarista.copy_bara_sky import translate_copy_bara_sky_to_toml
from copybarista.errors import (
    ConfigError,
    CopybaristaError,
    ExportError,
    GlobError,
    ImportRequestError,
    OutputMismatchError,
    TransformError,
)
from copybarista.export import export_folder
from copybarista.git import export_git
from copybarista.import_request import ImportRequest, import_change_request
from copybarista.sync_setup import (
    SyncSettings,
    check_sync_config,
    export_workflow,
    load_sync_settings,
    write_sync_scaffold,
)


def main(argv: list[str] | None = None) -> None:
    """Run the Copybarista CLI."""
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        _command_handlers()[args.command](args)
    except CopybaristaError as err:
        sys.stderr.write(f"{err}\n")
        sys.exit(_exit_code(err))


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
    init_sync.add_argument("--type-check-target", action="append", default=[])
    init_sync.add_argument("--forbidden-pr-text", action="append", default=[])
    init_sync.add_argument("--sync-user-name", default="copybarista")
    init_sync.add_argument("--sync-user-email", default="copybarista@example.com")
    init_sync.add_argument("--force", action="store_true")

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

    return parser


def _command_handlers() -> dict[str, Callable[[argparse.Namespace], None]]:
    """Return CLI subcommand dispatch table."""
    return {
        "validate": _run_validate,
        "translate": _run_translate,
        "export": _run_export,
        "publish-git": _run_publish_git,
        "import-change": _run_import_change,
        "init-sync": _run_init_sync,
        "check-sync-config": _run_check_sync_config,
        "write-export-workflow": _run_write_export_workflow,
    }


def _run_validate(args: argparse.Namespace) -> None:
    """Validate config and exit silently on success."""
    load_config(Path(args.config), workflow_name=args.workflow)


def _run_translate(args: argparse.Namespace) -> None:
    """Translate a supported `copy.bara.sky` workflow to native TOML."""
    translated = translate_copy_bara_sky_to_toml(
        Path(args.config),
        workflow_name=args.workflow,
    )
    if args.output:
        Path(args.output).write_text(translated, encoding="utf-8")
    else:
        sys.stdout.write(translated)


def _run_export(args: argparse.Namespace) -> None:
    """Run a folder export command."""
    config = load_config(Path(args.config), workflow_name=args.workflow)
    if not args.folder_dir and not config.folder.path:
        raise ConfigError(
            "copybarista export requires --folder-dir or destination.folder.path"
        )
    manifest = export_folder(
        config=config,
        source_ref=Path(args.source_ref),
        destination=Path(args.folder_dir or config.folder.path),
        force=args.force,
    )
    if args.json:
        sys.stdout.write(manifest.to_json())


def _run_publish_git(args: argparse.Namespace) -> None:
    """Run a Git publish command."""
    config = load_config(Path(args.config), workflow_name=args.workflow)
    manifest = export_git(config=config, source_ref=Path(args.source_ref))
    if args.json:
        sys.stdout.write(manifest.to_json())


def _run_import_change(args: argparse.Namespace) -> None:
    """Run a local public-change import command."""
    config = load_config(Path(args.config), workflow_name=args.workflow)
    if args.no_verify:
        sys.stderr.write(
            "Warning: --no-verify disables public-base and final re-export checks.\n"
        )
    result = import_change_request(
        ImportRequest(
            config=config,
            public_base=Path(args.public_base),
            public_head=Path(args.public_head),
            source_base=Path(args.source_base),
            destination=Path(args.destination),
            verify=not args.no_verify,
        )
    )
    if args.json:
        sys.stdout.write(json.dumps(result.to_dict(), indent=2, sort_keys=True) + "\n")


def _run_init_sync(args: argparse.Namespace) -> None:
    """Write package sync scaffolding."""
    settings = SyncSettings(
        package_name=args.package_name,
        sync_label=args.sync_label or args.package_name,
        source_root=args.source_root,
        public_repo=args.public_repo,
        source_repo=args.source_repo,
        copybarista_project_path=args.copybarista_project_path,
        smoke_import=args.smoke_import,
        type_check_targets=tuple(args.type_check_target)
        or (args.smoke_import, "tests"),
        forbidden_pr_text=tuple(args.forbidden_pr_text),
        sync_user_name=args.sync_user_name,
        sync_user_email=args.sync_user_email,
    )
    for path in write_sync_scaffold(
        root=Path(args.root),
        settings=settings,
        force=args.force,
    ):
        sys.stdout.write(f"wrote {path}\n")


def _run_check_sync_config(args: argparse.Namespace) -> None:
    """Validate package sync scaffolding."""
    check_sync_config(root=Path(args.root))


def _run_write_export_workflow(args: argparse.Namespace) -> None:
    """Write the source-repository export workflow."""
    settings = load_sync_settings(Path(args.sync_config))
    text = export_workflow(settings)
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)


def _exit_code(err: CopybaristaError) -> int:
    """Return the release-gate exit code for a user-facing error."""
    if isinstance(err, (ConfigError, GlobError)):
        return 1
    if isinstance(err, (OutputMismatchError, TransformError)):
        return 2
    if isinstance(err, (ExportError, ImportRequestError)):
        return 3
    return 1
