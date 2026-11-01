"""Tests for the Copybarista command-line interface."""

from __future__ import annotations

from typing import TYPE_CHECKING

from treekle import from_plain, json_loads

import pytest

from copybarista import cli
from copybarista.cli import _exit_code, _parser, main
from copybarista.errors import (
    ConfigError,
    CopybaristaError,
    ExportError,
    ImportRequestError,
    LeakCheckError,
    OutputMismatchError,
    TransformError,
)
from copybarista.import_request import ImportResult
from copybarista.manifest import ExportManifest
from copybarista.sync_setup import (
    DEFAULT_SYNC_USER_EMAIL,
    DEFAULT_SYNC_USER_NAME,
    load_sync_settings,
)


if TYPE_CHECKING:
    from pathlib import Path

    from copybarista.config import WorkflowConfig
    from copybarista.import_request import ImportRequest


def _config(path: Path, source_root: str = "project") -> None:
    path.write_text(
        f"""
        [workflow]
        name = "demo"
        mode = "squash"
        source_root = "{source_root}"

        [files]
        include = ["**"]
        """,
        encoding="utf-8",
    )


def test_cli_validate_accepts_valid_config(tmp_path: Path):
    config = tmp_path / "copy.barista.toml"
    _config(config)

    assert main(["validate", str(config)]) == 0


def test_cli_export_writes_json_manifest(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    source = tmp_path / "repo"
    project = source / "project"
    project.mkdir(parents=True)
    (project / "README.md").write_text("hello\n", encoding="utf-8")
    config = tmp_path / "copy.barista.toml"
    _config(config)

    main(
        [
            "export",
            str(config),
            str(source),
            "--folder-dir",
            str(tmp_path / "out"),
            "--json",
        ],
    )

    manifest = json_loads(capsys.readouterr().out, dict[str, object])
    files = from_plain(manifest["files"], list[dict[str, object]])
    assert files[0]["destination"] == "README.md"


def test_cli_reports_copybarista_errors(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    config = tmp_path / "copy.barista.toml"
    _config(config, source_root="missing")

    code = main(
        [
            "export",
            str(config),
            str(tmp_path / "repo"),
            "--folder-dir",
            str(tmp_path / "out"),
        ],
    )

    assert code == 3
    assert "Source root" in capsys.readouterr().err


def test_cli_export_requires_folder_destination(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    config = tmp_path / "copy.barista.toml"
    _config(config)

    code = main(["export", str(config), str(tmp_path / "repo")])

    assert code == 1
    assert "--folder-dir" in capsys.readouterr().err


def test_cli_reports_transform_errors_as_release_gate_failures(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    source = tmp_path / "repo"
    project = source / "project"
    project.mkdir(parents=True)
    (project / "README.md").write_text("hello\n", encoding="utf-8")
    config = tmp_path / "copy.barista.toml"
    config.write_text(
        """
        [workflow]
        name = "demo"
        mode = "squash"
        source_root = "project"

        [files]
        include = ["**"]

        [[transform]]
        type = "replace"
        path = "README.md"
        before = "missing"
        after = "updated"
        """,
        encoding="utf-8",
    )

    code = main(
        [
            "export",
            str(config),
            str(source),
            "--folder-dir",
            str(tmp_path / "out"),
        ],
    )

    assert code == 2
    assert "no changes" in capsys.readouterr().err


def test_cli_check_leaks_reports_policy_violations(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    root = tmp_path / "out"
    root.mkdir()
    (root / "module.py").write_text(
        "from internal_pkg.lib import json\n",
        encoding="utf-8",
    )
    config = tmp_path / "copy.barista.toml"
    config.write_text(
        """
        [workflow]
        name = "demo"
        mode = "squash"
        source_root = "project"

        [files]
        include = ["**"]

        [[leak_check.forbidden_text]]
        id = "loop-imports"
        pattern = "\\\\binternal_pkg\\\\."
        paths = ["*.py", "**/*.py"]
        """,
        encoding="utf-8",
    )

    code = main(["check-leaks", str(config), str(root)])

    assert code == 2
    assert "loop-imports: module.py:1" in capsys.readouterr().err


def test_cli_check_leaks_honors_config_globstar(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    """`check-leaks` must apply the config's ``globstar``, like the export path.

    Under ``zero_or_more`` a ``**/`` rule matches a root-level file; the export
    threads ``config.globstar`` so it flags it. The standalone CLI must too --
    otherwise it silently passes a tree the export gate would fail.
    """
    root = tmp_path / "out"
    root.mkdir()
    (root / "secret.txt").write_text("token\n", encoding="utf-8")
    config = tmp_path / "copy.barista.toml"
    config.write_text(
        """
        [workflow]
        name = "demo"
        mode = "squash"
        source_root = "project"
        globstar = "zero_or_more"

        [files]
        include = ["**"]

        [[leak_check.forbidden_path]]
        id = "no-secrets"
        paths = ["**/secret.txt"]
        """,
        encoding="utf-8",
    )

    code = main(["check-leaks", str(config), str(root)])

    assert code == 2
    assert "no-secrets" in capsys.readouterr().err


def test_cli_translate_prints_or_writes_the_native_config(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    sky = tmp_path / "copy.bara.sky"
    sky.write_text(
        "core.workflow(\n"
        '    name = "export",\n'
        "    origin = folder.origin(),\n"
        "    destination = folder.destination(),\n"
        '    origin_files = glob(["**"]),\n'
        '    destination_files = glob(["**"]),\n'
        '    authoring = authoring.pass_thru("Demo <demo@copybarista.test>"),\n'
        '    mode = "SQUASH",\n'
        ")\n",
        encoding="utf-8",
    )

    assert main(["translate", str(sky)]) == 0
    printed = capsys.readouterr().out
    native = tmp_path / "copy.barista.toml"
    assert main(["translate", str(sky), "--output", str(native)]) == 0

    assert printed.startswith('[workflow]\nname = "export"\n')
    assert native.read_text(encoding="utf-8") == printed
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(("flags", "printed"), [([], ""), (["--json"], "{")])
def test_cli_publish_git_exports_to_git_and_prints_the_manifest_on_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    flags: list[str],
    printed: str,
):
    config = tmp_path / "copy.barista.toml"
    _config(config)
    exported: list[tuple[str, Path]] = []

    def export_git(*, config: WorkflowConfig, source_ref: Path) -> ExportManifest:
        exported.append((config.name, source_ref))
        return ExportManifest(files=(), transforms=(), elapsed_sec=0.0)

    monkeypatch.setattr(cli, "export_git", export_git)

    assert main(["publish-git", str(config), str(tmp_path / "repo"), *flags]) == 0

    assert exported == [("demo", tmp_path / "repo")]
    out = capsys.readouterr().out
    assert out.startswith(printed)
    if printed:
        assert json_loads(out, dict[str, object]) == {"files": [], "transforms": []}


@pytest.mark.parametrize(
    "case",
    [
        pytest.param(([], True, False, False), id="defaults"),
        pytest.param(
            (["--no-verify", "--merge-import", "--json"], False, True, True),
            id="every-flag",
        ),
    ],
)
def test_cli_import_change_runs_the_import_its_flags_describe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    case: tuple[list[str], bool, bool, bool],
):
    # With --no-verify it warns, and with --json it prints the report.
    flags, verify, merge, warned = case
    printed = warned
    config = tmp_path / "copy.barista.toml"
    _config(config)
    requests: list[ImportRequest] = []

    def import_change_request(request: ImportRequest) -> ImportResult:
        requests.append(request)
        return ImportResult(changes=())

    monkeypatch.setattr(cli, "import_change_request", import_change_request)
    paths = {name: tmp_path / name for name in ("base", "head", "source", "out")}

    assert (
        main(
            [
                "import-change",
                str(config),
                *("--public-base", str(paths["base"])),
                *("--public-head", str(paths["head"])),
                *("--source-base", str(paths["source"])),
                *("--destination", str(paths["out"])),
                *flags,
            ],
        )
        == 0
    )

    [request] = requests
    assert (request.public_base, request.public_head) == (paths["base"], paths["head"])
    assert (request.source_base, request.destination) == (paths["source"], paths["out"])
    assert (request.verify, request.merge_import) == (verify, merge)
    captured = capsys.readouterr()
    assert ("--no-verify disables" in captured.err) is warned
    assert (
        json_loads(captured.out, dict[str, object]) if printed else captured.out
    ) == ({"changes": []} if printed else "")


def test_cli_write_export_workflow_writes_to_an_output_file(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    main(
        [
            "init-sync",
            str(tmp_path),
            "--package-name",
            "configgle",
            "--source-root",
            "packages/configgle",
            "--public-repo",
            "example/configgle",
            "--source-repo",
            "example/source",
            "--copybarista-project-path",
            "tools/copybarista",
        ],
    )
    capsys.readouterr()
    output = tmp_path / "loop-to-configgle.yml"

    main(
        [
            "write-export-workflow",
            str(tmp_path / "copybarista.sync.toml"),
            "--output",
            str(output),
        ],
    )

    assert "configgle/export/main" in output.read_text(encoding="utf-8")
    assert capsys.readouterr().out == ""


def test_cli_release_gate_exit_code_mapping():
    assert _exit_code(CopybaristaError("other")) == 1
    assert _exit_code(ConfigError("bad config")) == 1
    assert _exit_code(TransformError("no-op")) == 2
    assert _exit_code(OutputMismatchError("mismatch")) == 2
    assert _exit_code(LeakCheckError("leak")) == 2
    assert _exit_code(ExportError("git failed")) == 3
    assert _exit_code(ImportRequestError("import failed")) == 3


def test_cli_rejects_unknown_commands():
    with pytest.raises(SystemExit) as exc:
        main(["nope"])

    assert exc.value.code == 2


_IMPORT_CHANGE_REQUIRED = (
    "--public-base",
    "pb",
    "--public-head",
    "ph",
    "--source-base",
    "sb",
    "--destination",
    "d",
)
_INIT_SYNC_REQUIRED = (
    "--package-name",
    "pkg",
    "--source-root",
    "sr",
    "--public-repo",
    "pr",
    "--source-repo",
    "so",
    "--copybarista-project-path",
    "cp",
)


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["validate", "c"], {"config": "c", "workflow": "export"}),
        (
            ["translate", "c"],
            {"config": "c", "workflow": "export", "output": ""},
        ),
        (
            ["export", "c", "s"],
            {
                "config": "c",
                "source_ref": "s",
                "folder_dir": "",
                "workflow": "export",
                "force": False,
                "json": False,
            },
        ),
        (
            ["publish-git", "c", "s"],
            {"config": "c", "source_ref": "s", "workflow": "export_git", "json": False},
        ),
        (
            ["check-leaks", "c", "r"],
            {"config": "c", "root": "r", "workflow": "export"},
        ),
        (
            ["import-change", "c", *_IMPORT_CHANGE_REQUIRED],
            {
                "config": "c",
                "public_base": "pb",
                "public_head": "ph",
                "source_base": "sb",
                "destination": "d",
                "workflow": "export",
                "no_verify": False,
                "merge_import": False,
                "json": False,
            },
        ),
        (
            ["init-sync", "r", *_INIT_SYNC_REQUIRED],
            {
                "root": "r",
                "package_name": "pkg",
                "sync_label": "",
                "source_root": "sr",
                "public_repo": "pr",
                "source_repo": "so",
                "copybarista_project_path": "cp",
                "release_check_script": "",
                "forbidden_pr_text": [],
                "validation_python_version": [],
                "refresh_public_lockfile": False,
                "sync_user_name": DEFAULT_SYNC_USER_NAME,
                "sync_user_email": DEFAULT_SYNC_USER_EMAIL,
                "overwrite": False,
            },
        ),
        (["check-sync-config", "r"], {"root": "r"}),
        (
            ["write-export-workflow", "s"],
            {"sync_config": "s", "output": "", "source_ref": "."},
        ),
        (["write-public-workflows", "s"], {"sync_config": "s"}),
    ],
)
def test_cli_parses_each_command_with_its_defaults(
    argv: list[str],
    expected: dict[str, object],
):
    assert vars(_parser().parse_args(argv)) == {"command": argv[0], **expected}


def test_cli_sets_every_flag_by_its_documented_name():
    parsed = vars(
        _parser().parse_args(
            [
                "init-sync",
                "r",
                *_INIT_SYNC_REQUIRED,
                *("--forbidden-pr-text", "c", "--forbidden-pr-text", "d"),
                *("--validation-python-version", "3.12"),
                *("--validation-python-version", "3.13"),
            ],
        ),
    )

    assert parsed["forbidden_pr_text"] == ["c", "d"]
    assert parsed["validation_python_version"] == ["3.12", "3.13"]
    assert vars(
        _parser().parse_args(
            [
                "import-change",
                "c",
                *_IMPORT_CHANGE_REQUIRED,
                *("--no-verify", "--merge-import", "--json"),
            ],
        ),
    ) == {
        "command": "import-change",
        "config": "c",
        "public_base": "pb",
        "public_head": "ph",
        "source_base": "sb",
        "destination": "d",
        "workflow": "export",
        "no_verify": True,
        "merge_import": True,
        "json": True,
    }


@pytest.mark.parametrize(
    ("command", "required", "index"),
    [
        (command, required, index)
        for command, required in (
            ("import-change", _IMPORT_CHANGE_REQUIRED),
            ("init-sync", _INIT_SYNC_REQUIRED),
        )
        for index in range(0, len(required), 2)
    ],
    ids=[
        f"{command}{required[index]}"
        for command, required in (
            ("import-change", _IMPORT_CHANGE_REQUIRED),
            ("init-sync", _INIT_SYNC_REQUIRED),
        )
        for index in range(0, len(required), 2)
    ],
)
def test_cli_rejects_a_command_missing_a_required_flag(
    command: str,
    required: tuple[str, ...],
    index: int,
    capsys: pytest.CaptureFixture[str],
):
    with pytest.raises(SystemExit) as exc:
        _parser().parse_args(
            [command, "x", *required[:index], *required[index + 2 :]],
        )

    assert exc.value.code == 2
    assert required[index] in capsys.readouterr().err


def test_cli_requires_a_command():
    with pytest.raises(SystemExit) as exc:
        _parser().parse_args([])

    assert exc.value.code == 2


def test_cli_help_names_the_program_and_each_command():
    text = _parser().format_help()

    assert text.startswith("usage: copybarista ")
    for line in (
        "Validate a Copybarista config",
        "Export to a local folder",
        "Write package sync scaffolding",
    ):
        assert line in text


def test_cli_uses_publish_git_command_name(
    capsys: pytest.CaptureFixture[str],
):
    with pytest.raises(SystemExit) as exc:
        main(["export-git", "--help"])

    assert exc.value.code == 2
    assert "publish-git" in capsys.readouterr().err


def test_cli_init_sync_writes_generic_scaffold(tmp_path: Path):
    main(
        [
            "init-sync",
            str(tmp_path),
            "--package-name",
            "configgle",
            "--sync-label",
            "Configgle",
            "--source-root",
            "packages/configgle",
            "--public-repo",
            "example/configgle",
            "--source-repo",
            "example/source",
            "--copybarista-project-path",
            "tools/copybarista",
        ],
    )

    assert (tmp_path / "copy.barista.toml").exists()
    assert (tmp_path / "copybarista.sync.toml").exists()
    assert (tmp_path / ".github/workflows/configgle-to-loop.yml").exists()
    assert (tmp_path / ".github/workflows/package-validation.yml").exists()
    assert not (tmp_path / "private").exists()


def test_cli_init_sync_writes_the_python_matrix(tmp_path: Path):
    main(
        [
            "init-sync",
            str(tmp_path),
            "--package-name",
            "configgle",
            "--source-root",
            "packages/configgle",
            "--public-repo",
            "example/configgle",
            "--source-repo",
            "example/source",
            "--copybarista-project-path",
            "tools/copybarista",
            # The published floor must always be present; a package may add
            # NEWER versions on top of it, never replace it. Validating on
            # 3.13 alone would leave the >=3.12 floor every package declares
            # completely untested.
            "--validation-python-version",
            "3.12",
            "--validation-python-version",
            "3.13",
        ],
    )

    workflow = (tmp_path / ".github/workflows/package-validation.yml").read_text(
        encoding="utf-8",
    )
    assert 'python-version: ["3.12", "3.13"]' in workflow


def test_cli_init_sync_can_refresh_public_lockfile(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    main(
        [
            "init-sync",
            str(tmp_path),
            "--package-name",
            "sagent",
            "--source-root",
            "packages/sagent",
            "--public-repo",
            "example/sagent",
            "--source-repo",
            "example/source",
            "--copybarista-project-path",
            "tools/copybarista",
            "--refresh-public-lockfile",
        ],
    )

    sync_config = (tmp_path / "copybarista.sync.toml").read_text(encoding="utf-8")
    capsys.readouterr()
    main(["write-export-workflow", str(tmp_path / "copybarista.sync.toml")])
    workflow = capsys.readouterr().out

    assert "refresh_public_lockfile = true" in sync_config
    assert "--refresh-public-lockfile" in workflow


def test_cli_init_sync_can_add_release_check_script(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    main(
        [
            "init-sync",
            str(tmp_path),
            "--package-name",
            "copybarista",
            "--source-root",
            "packages/copybarista",
            "--public-repo",
            "example/copybarista",
            "--source-repo",
            "example/source",
            "--copybarista-project-path",
            "tools/copybarista",
            "--release-check-script",
            "scripts/check_release_tree.py",
        ],
    )

    sync_config = (tmp_path / "copybarista.sync.toml").read_text(encoding="utf-8")
    capsys.readouterr()
    main(["write-export-workflow", str(tmp_path / "copybarista.sync.toml")])
    workflow = capsys.readouterr().out

    assert 'release_check_script = "scripts/check_release_tree.py"' in sync_config
    assert "--release-check-script scripts/check_release_tree.py" in workflow


def test_cli_check_sync_config_accepts_generated_scaffold(tmp_path: Path):
    main(
        [
            "init-sync",
            str(tmp_path),
            "--package-name",
            "configgle",
            "--source-root",
            "packages/configgle",
            "--public-repo",
            "example/configgle",
            "--source-repo",
            "example/source",
            "--copybarista-project-path",
            "tools/copybarista",
        ],
    )

    assert main(["check-sync-config", str(tmp_path)]) == 0


def test_cli_write_export_workflow_uses_sync_metadata(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    main(
        [
            "init-sync",
            str(tmp_path),
            "--package-name",
            "configgle",
            "--source-root",
            "packages/configgle",
            "--public-repo",
            "example/configgle",
            "--source-repo",
            "example/source",
            "--copybarista-project-path",
            "tools/copybarista",
        ],
    )

    capsys.readouterr()
    main(["write-export-workflow", str(tmp_path / "copybarista.sync.toml")])

    output = capsys.readouterr().out
    assert "configgle/export/main" in output
    assert "CONFIGGLE" not in output


def test_cli_write_public_workflows_rewrites_every_generated_file(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    """Both workflows render from the sync config.

    Rewriting only one leaves the other stale against it, so the subcommand
    must cover the pair.
    """
    main(
        [
            "init-sync",
            str(tmp_path),
            "--package-name",
            "configgle",
            "--source-root",
            "packages/configgle",
            "--public-repo",
            "example/configgle",
            "--source-repo",
            "example/source",
            "--copybarista-project-path",
            "tools/copybarista",
        ],
    )
    # A distinctive sentinel, not a plain word: the generated workflows carry
    # explanatory prose, and a common token (e.g. "stale") can legitimately
    # appear there -- the check is "the placeholder was overwritten", not "this
    # English word is absent".
    placeholder = "PLACEHOLDER_NOT_YET_REWRITTEN\n"
    public = tmp_path / ".export"
    generated = (
        public / ".github/workflows/package-validation.yml",
        public / ".github/workflows/configgle-to-loop.yml",
    )
    for path in generated:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(placeholder, encoding="utf-8")

    capsys.readouterr()
    main(["write-public-workflows", str(tmp_path / "copybarista.sync.toml")])

    for path in generated:
        assert placeholder.strip() not in path.read_text(encoding="utf-8"), path
    assert "--hook-stage pre-push" in generated[0].read_text(encoding="utf-8")


def test_cli_write_public_workflows_adds_the_npm_workflow_only_when_enabled(
    tmp_path: Path,
):
    """Enabling npm is a sync-config setting, like the watch paths: no flag."""
    for name in ("plain", "npm"):
        root = tmp_path / name
        main(
            [
                "init-sync",
                str(root),
                "--package-name",
                "configgle",
                "--source-root",
                "packages/configgle",
                "--public-repo",
                "example/configgle",
                "--source-repo",
                "example/source",
                "--copybarista-project-path",
                "tools/copybarista",
            ],
        )
        (root / ".export/.github/workflows").mkdir(parents=True)
    sync = tmp_path / "npm/copybarista.sync.toml"
    sync.write_text(
        sync.read_text(encoding="utf-8").replace(
            "npm_packages = [\n\n]",
            'npm_packages = [\n  "npm/configgle",\n]',
        ),
        encoding="utf-8",
    )

    for name in ("plain", "npm"):
        main(["write-public-workflows", str(tmp_path / name / "copybarista.sync.toml")])

    assert not (tmp_path / "plain/.export/.github/workflows/publish-npm.yml").exists()
    assert "npm publish" in (
        tmp_path / "npm/.export/.github/workflows/publish-npm.yml"
    ).read_text(encoding="utf-8")


def test_cli_init_sync_writes_its_identity_and_overwrites_only_on_request(
    tmp_path: Path,
):
    argv = [
        "init-sync",
        str(tmp_path),
        *_INIT_SYNC_REQUIRED,
        *("--sync-user-name", "bot", "--sync-user-email", "bot@example.org"),
    ]
    assert main(argv) == 0

    settings = load_sync_settings(tmp_path / "copybarista.sync.toml")
    assert (settings.sync_user_name, settings.sync_user_email) == (
        "bot",
        "bot@example.org",
    )
    assert main(argv) == 1
    assert main([*argv, "--overwrite"]) == 0


def test_cli_write_public_workflows_keeps_the_ci_a_tree_owns(tmp_path: Path):
    """A tree that is no Python package writes its own CI; regenerating keeps it."""
    main(
        [
            "init-sync",
            str(tmp_path),
            "--package-name",
            "site",
            "--source-root",
            "packages/site",
            "--public-repo",
            "example/site",
            "--source-repo",
            "example/source",
            "--copybarista-project-path",
            "tools/copybarista",
        ],
    )
    sync = tmp_path / "copybarista.sync.toml"
    sync.write_text(
        sync.read_text(encoding="utf-8")
        .replace("python_package = true", "python_package = false")
        .replace("skip_source_validation = false", "skip_source_validation = true"),
        encoding="utf-8",
    )
    owned = tmp_path / ".github/workflows/package-validation.yml"
    owned.write_text("name: Package validation\n", encoding="utf-8")

    main(["write-public-workflows", str(sync)])

    assert owned.read_text(encoding="utf-8") == "name: Package validation\n"


if __name__ == "__main__":
    from copybarista.lib.testing.runner import test_main

    test_main(__file__)
