"""Tests for reusable package sync scaffolding."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from copybarista.errors import ConfigError
from copybarista.sync_setup import (
    SyncSettings,
    check_sync_config,
    export_workflow,
    import_workflow,
    load_sync_settings,
    package_validation_workflow,
    write_sync_scaffold,
)


def _settings(**kwargs: Any) -> SyncSettings:
    values: dict[str, Any] = {
        "package_name": "configgle",
        "sync_label": "Configgle",
        "source_root": "loop/lib/configgle",
        "public_repo": "rekursiv-ai/configgle",
        "source_repo": "rekursiv-ai/loop",
        "copybarista_project_path": "loop/experimental/copybarista",
        "smoke_import": "configgle",
        "type_check_targets": ("configgle", "tests"),
        "forbidden_pr_text": ("loop",),
    }
    values.update(kwargs)
    return SyncSettings(**values)


def test_write_sync_scaffold_uses_stable_public_file_names(tmp_path: Path):
    written = write_sync_scaffold(root=tmp_path, settings=_settings())

    assert tmp_path / "copy.barista.toml" in written
    assert tmp_path / "copybarista.sync.toml" in written
    assert tmp_path / ".github/workflows/sync-to-source.yml" in written
    assert tmp_path / ".github/workflows/package-validation.yml" in written
    assert not (tmp_path / "private").exists()
    assert not (tmp_path / "scripts/sync_configgle_export.py").exists()


def test_sync_metadata_stores_package_name_as_data(tmp_path: Path):
    write_sync_scaffold(root=tmp_path, settings=_settings())

    text = (tmp_path / "copybarista.sync.toml").read_text(encoding="utf-8")

    assert 'package_name = "configgle"' in text
    assert 'sync_label = "Configgle"' in text
    assert 'export_branch_prefix = "configgle/export/"' in text
    assert 'import_branch_prefix = "configgle/import/"' in text


def test_generated_export_config_keeps_public_sync_files(tmp_path: Path):
    write_sync_scaffold(root=tmp_path, settings=_settings())

    text = (tmp_path / "copy.barista.toml").read_text(encoding="utf-8")

    assert '"copy.barista.toml"' not in text
    assert '"copybarista.sync.toml"' not in text


def test_check_sync_config_accepts_generated_scaffold(tmp_path: Path):
    write_sync_scaffold(root=tmp_path, settings=_settings())

    check_sync_config(root=tmp_path)


def test_check_sync_config_rejects_missing_public_import_workflow(tmp_path: Path):
    write_sync_scaffold(root=tmp_path, settings=_settings())
    (tmp_path / ".github/workflows/sync-to-source.yml").unlink()

    with pytest.raises(ConfigError, match="Missing sync files"):
        check_sync_config(root=tmp_path)


def test_check_sync_config_rejects_missing_package_validation_workflow(
    tmp_path: Path,
):
    write_sync_scaffold(root=tmp_path, settings=_settings())
    (tmp_path / ".github/workflows/package-validation.yml").unlink()

    with pytest.raises(ConfigError, match="Missing sync files"):
        check_sync_config(root=tmp_path)


def test_check_sync_config_rejects_workflow_drift(tmp_path: Path):
    write_sync_scaffold(root=tmp_path, settings=_settings())
    workflow = tmp_path / ".github/workflows/sync-to-source.yml"
    workflow.write_text(
        workflow.read_text(encoding="utf-8").replace(
            'TARGET_REPO: "rekursiv-ai/loop"',
            'TARGET_REPO: "wrong/repo"',
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="TARGET_REPO"):
        check_sync_config(root=tmp_path)


def test_check_sync_config_reports_malformed_workflow_yaml(tmp_path: Path):
    write_sync_scaffold(root=tmp_path, settings=_settings())
    (tmp_path / ".github/workflows/sync-to-source.yml").write_text(
        "jobs: [\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="Cannot read sync workflow"):
        check_sync_config(root=tmp_path)


def test_check_sync_config_rejects_import_command_drift(tmp_path: Path):
    write_sync_scaffold(root=tmp_path, settings=_settings())
    workflow = tmp_path / ".github/workflows/sync-to-source.yml"
    workflow.write_text(
        workflow.read_text(encoding="utf-8").replace(
            '--project-path "$TARGET_PROJECT_PATH"',
            "--project-path wrong/path",
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="--project-path"):
        check_sync_config(root=tmp_path)


def test_load_sync_settings_reports_malformed_toml(tmp_path: Path):
    config = tmp_path / "copybarista.sync.toml"
    config.write_text("[sync\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="Cannot read sync config"):
        load_sync_settings(config)


def test_load_sync_settings_uses_defaults_for_optional_fields(tmp_path: Path):
    config = tmp_path / "copybarista.sync.toml"
    config.write_text(
        """
        [sync]
        package_name = "configgle"
        sync_label = "Configgle"
        source_root = "loop/lib/configgle"
        public_repo = "rekursiv-ai/configgle"
        source_repo = "rekursiv-ai/loop"
        copybarista_project_path = "loop/experimental/copybarista"
        smoke_import = "configgle"
        type_check_targets = ["configgle"]
        forbidden_pr_text = []
        """,
        encoding="utf-8",
    )

    settings = load_sync_settings(config)

    assert settings.sync_user_name == "copybarista"
    assert settings.sync_user_email == "copybarista@example.com"
    assert settings.export_prefix == "configgle/export/"
    assert settings.import_prefix == "configgle/import/"
    assert settings.validation_python_versions == ("3.12",)
    assert settings.validation_commands == (
        "uv sync --all-groups",
        "uv run ruff check .",
        "uv run basedpyright configgle",
        "uv run pytest",
        'uv run python -c "import configgle"',
        "uv build",
    )


def test_load_sync_settings_rejects_wrong_array_shape(tmp_path: Path):
    config = tmp_path / "copybarista.sync.toml"
    config.write_text(
        """
        [sync]
        package_name = "configgle"
        sync_label = "Configgle"
        source_root = "loop/lib/configgle"
        public_repo = "rekursiv-ai/configgle"
        source_repo = "rekursiv-ai/loop"
        copybarista_project_path = "loop/experimental/copybarista"
        smoke_import = "configgle"
        export_branch_prefix = "configgle/export/"
        import_branch_prefix = "configgle/import/"
        type_check_targets = "configgle"
        forbidden_pr_text = []
        """,
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="type_check_targets"):
        load_sync_settings(config)


def test_load_sync_settings_rejects_unsafe_branch_prefix(tmp_path: Path):
    write_sync_scaffold(root=tmp_path, settings=_settings())
    config = tmp_path / "copybarista.sync.toml"
    config.write_text(
        config.read_text(encoding="utf-8").replace("configgle/import/", "main"),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="import_branch_prefix"):
        load_sync_settings(config)


def test_package_validation_workflow_runs_configured_commands():
    workflow = package_validation_workflow(
        _settings(
            validation_python_versions=("3.12", "3.13"),
            validation_commands=(
                "uv sync --all-groups",
                "uv run pytest",
            ),
        )
    )

    assert 'python-version: ["3.12", "3.13"]' in workflow
    assert "uv sync --all-groups" in workflow
    assert "uv run pytest" in workflow


def test_check_sync_config_rejects_package_validation_drift(tmp_path: Path):
    write_sync_scaffold(root=tmp_path, settings=_settings())
    workflow = tmp_path / ".github/workflows/package-validation.yml"
    workflow.write_text(
        workflow.read_text(encoding="utf-8").replace(
            "uv run pytest",
            "uv run pytest tests/test_one.py",
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="validation_commands"):
        check_sync_config(root=tmp_path)


def test_export_workflow_uses_metadata_without_package_specific_env_names():
    workflow = export_workflow(_settings())

    assert "configgle/export/main" in workflow
    assert (
        "group: copybarista-export-${{ github.workflow }}-${{ github.ref }}" in workflow
    )
    assert '--sync-label "$SYNC_LABEL"' in workflow
    assert '--auto-merge="$COPYBARISTA_AUTO_MERGE"' in workflow
    assert "CONFIGGLE" not in workflow
    assert "sync_configgle" not in workflow
    assert "source/loop/experimental/copybarista/scripts/sync_export_pr.py" in workflow


def test_generated_workflows_keep_readable_line_continuations():
    workflow = export_workflow(_settings())

    assert "run: |\n          uv --quiet" in workflow
    assert " \\\n            --source-dir source" in workflow


def test_import_workflow_uses_metadata_and_splits_trusted_pr_step():
    workflow = import_workflow(_settings())

    assert 'TARGET_REPO: "rekursiv-ai/loop"' in workflow
    assert 'TARGET_PROJECT_PATH: "loop/lib/configgle"' in workflow
    assert 'COPYBARISTA_IMPORT_BRANCH_PREFIX: "configgle/import/"' in workflow
    assert (
        "github.event.pull_request.head.repo.full_name == github.repository" in workflow
    )
    assert (
        "!startsWith(github.event.pull_request.head.ref, 'configgle/export/')"
        in workflow
    )
    assert 'git check-ref-format --allow-onelevel "$ref"' in workflow
    assert (
        "github.event.head_commit.author.email != 'copybarista@example.com'" in workflow
    )
    assert "--open-pr false" in workflow
    assert "--open-pr-only" in workflow
    assert '--branch-prefix "$COPYBARISTA_IMPORT_BRANCH_PREFIX"' in workflow
    assert '--sync-label "$COPYBARISTA_SYNC_LABEL"' in workflow
    assert "GH_TOKEN: ${{ secrets.COPYBARISTA_IMPORT_TOKEN }}" in workflow


def test_import_workflow_escapes_github_expression_strings():
    workflow = import_workflow(_settings(sync_label="Configgle's Core"))

    assert "Configgle''s Core export branch:" in workflow
    assert "Configgle's Core export branch:" not in workflow


def test_export_workflow_watches_source_and_sync_helpers():
    workflow = export_workflow(_settings())

    assert '"loop/lib/configgle/**"' in workflow
    assert '"loop/experimental/copybarista/scripts/sync_export_pr.py"' in workflow
    assert '"loop/experimental/copybarista/scripts/sync_import_change.py"' in workflow


def test_write_sync_scaffold_refuses_to_overwrite_without_force(tmp_path: Path):
    write_sync_scaffold(root=tmp_path, settings=_settings())

    with pytest.raises(ConfigError, match="Refusing to overwrite"):
        write_sync_scaffold(root=tmp_path, settings=_settings())


def test_write_sync_scaffold_can_overwrite_with_force(tmp_path: Path):
    write_sync_scaffold(root=tmp_path, settings=_settings())

    written = write_sync_scaffold(root=tmp_path, settings=_settings(), force=True)

    assert tmp_path / "copy.barista.toml" in written


def test_generated_toml_escapes_strings(tmp_path: Path):
    write_sync_scaffold(
        root=tmp_path,
        settings=_settings(sync_label='Configgle "Core"'),
    )

    loaded = load_sync_settings(tmp_path / "copybarista.sync.toml")

    assert loaded.sync_label == 'Configgle "Core"'
