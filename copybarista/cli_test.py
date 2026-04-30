"""Tests for the Copybarista command-line interface."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from copybarista.cli import _exit_code, main
from copybarista.errors import (
    ConfigError,
    ExportError,
    ImportRequestError,
    OutputMismatchError,
    TransformError,
)


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

    main(["validate", str(config)])


def test_cli_export_writes_json_manifest(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
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
        ]
    )

    manifest = json.loads(capsys.readouterr().out)
    assert manifest["files"][0]["destination"] == "README.md"


def test_cli_reports_copybarista_errors(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    config = tmp_path / "copy.barista.toml"
    _config(config, source_root="missing")

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "export",
                str(config),
                str(tmp_path / "repo"),
                "--folder-dir",
                str(tmp_path / "out"),
            ]
        )

    assert exc.value.code == 3
    assert "Source root" in capsys.readouterr().err


def test_cli_export_requires_folder_destination(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    config = tmp_path / "copy.barista.toml"
    _config(config)

    with pytest.raises(SystemExit) as exc:
        main(["export", str(config), str(tmp_path / "repo")])

    assert exc.value.code == 1
    assert "--folder-dir" in capsys.readouterr().err


def test_cli_reports_transform_errors_as_release_gate_failures(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
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

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "export",
                str(config),
                str(source),
                "--folder-dir",
                str(tmp_path / "out"),
            ]
        )

    assert exc.value.code == 2
    assert "no changes" in capsys.readouterr().err


def test_cli_release_gate_exit_code_mapping():
    assert _exit_code(ConfigError("bad config")) == 1
    assert _exit_code(TransformError("no-op")) == 2
    assert _exit_code(OutputMismatchError("mismatch")) == 2
    assert _exit_code(ExportError("git failed")) == 3
    assert _exit_code(ImportRequestError("import failed")) == 3


def test_cli_rejects_unknown_commands():
    with pytest.raises(SystemExit) as exc:
        main(["nope"])

    assert exc.value.code == 2


def test_cli_uses_publish_git_command_name(
    capsys: pytest.CaptureFixture[str],
):
    with pytest.raises(SystemExit) as exc:
        main(["export-git", "--help"])

    assert exc.value.code == 2
    assert "publish-git" in capsys.readouterr().err
