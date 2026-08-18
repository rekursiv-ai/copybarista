"""Tests for reusable package sync scaffolding."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from copybarista.errors import ConfigError
from copybarista.sync_setup import (
    GITHUB_ACTION_PINS,
    SyncSettings,
    action_ref,
    check_sync_config,
    export_workflow,
    import_workflow,
    load_sync_settings,
    package_validation_workflow,
    sync_toml,
    workflow_dir,
    write_sync_scaffold,
)


def _settings(**kwargs: Any) -> SyncSettings:
    values: dict[str, Any] = {
        "package_name": "configgle",
        "sync_label": "Configgle",
        "source_root": "packages/configgle",
        "public_repo": "example/configgle",
        "source_repo": "example/source",
        "copybarista_project_path": "tools/copybarista",
        "smoke_import": "configgle",
        "type_check_targets": ("configgle", "tests"),
        "forbidden_pr_text": ("loop",),
    }
    values.update(kwargs)
    return SyncSettings(**values)


def test_sync_toml_round_trips_every_setting(tmp_path: Path):
    """Writing settings then loading them back must reproduce them exactly.

    A field the loader reads but the template has no slot for is written away
    silently: ``skip_source_validation`` (set by all eight live packages) came
    back ``False``, so re-scaffolding would have dropped
    ``--skip-source-validation`` from every export workflow.

    This asserts over ALL fields rather than a list of them. A per-field
    assertion only covers what its author enumerated, which is the same
    incompleteness that let the gap open; quantifying over the dataclass
    means a field added later cannot escape.
    """
    original = _settings(
        skip_source_validation=True,
        system_packages=("git",),
        replay_bootstrap_base_comment=("why this base",),
    )
    path = tmp_path / "copybarista.sync.toml"
    path.write_text(sync_toml(original), encoding="utf-8")

    assert load_sync_settings(path) == original


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
    # The RAW prefix fields, left empty so they keep deriving from the package
    # name. Writing the RESOLVED "configgle/export/" would freeze today's name
    # into the config, and a later rename would silently keep the old prefix.
    assert 'export_branch_prefix = ""' in text
    assert 'import_branch_prefix = ""' in text
    assert "[pull_request]" in text
    assert 'metadata_source = "commit_messages"' in text


def test_scaffolded_prefixes_still_derive_from_the_package_name(tmp_path: Path):
    """An empty raw prefix must still resolve to the derived branch names."""
    write_sync_scaffold(root=tmp_path, settings=_settings())

    settings = load_sync_settings(tmp_path / "copybarista.sync.toml")

    assert settings.export_prefix == "configgle/export/"
    assert settings.import_prefix == "configgle/import/"


def test_generated_export_config_blocks_source_sync_files(tmp_path: Path):
    write_sync_scaffold(root=tmp_path, settings=_settings())

    text = (tmp_path / "copy.barista.toml").read_text(encoding="utf-8")

    assert '"copy.barista.toml"' in text
    assert '"copybarista.sync.toml"' in text
    assert "[[leak_check.forbidden_path]]" in text


def test_check_sync_config_accepts_generated_scaffold(tmp_path: Path):
    write_sync_scaffold(root=tmp_path, settings=_settings())

    check_sync_config(root=tmp_path)


def test_check_sync_config_accepts_config_relying_on_default_excludes(
    tmp_path: Path,
):
    # A config that omits the Python-artifact patterns from [files].exclude is
    # valid when it opts into the default set. Only the control-file exclusions
    # (not covered by the default) remain mandatory.
    write_sync_scaffold(root=tmp_path, settings=_settings())
    config_path = tmp_path / "copy.barista.toml"
    config_path.write_text(
        """
        [workflow]
        name = "configgle"
        mode = "squash"
        source_root = "packages/configgle"

        [files]
        include = ["**"]
        use_default_python_excludes = true
        exclude = [
          "copy.bara.sky",
          "copy.barista.toml",
          "copybarista.sync.toml",
        ]

        [[leak_check.forbidden_path]]
        id = "source-only-paths"
        paths = ["private/**"]
        message = "source-only path was exported"
        """,
        encoding="utf-8",
    )

    check_sync_config(root=tmp_path)


def test_check_sync_config_rejects_config_not_opting_into_default_excludes(
    tmp_path: Path,
):
    # A managed config that neither lists Python artifacts nor opts into the
    # default set would leak caches/venvs; the gate rejects it.
    write_sync_scaffold(root=tmp_path, settings=_settings())
    config_path = tmp_path / "copy.barista.toml"
    config_path.write_text(
        """
        [workflow]
        name = "configgle"
        mode = "squash"
        source_root = "packages/configgle"

        [files]
        include = ["**"]
        exclude = [
          "copy.bara.sky",
          "copy.barista.toml",
          "copybarista.sync.toml",
        ]

        [[leak_check.forbidden_path]]
        id = "source-only-paths"
        paths = ["private/**"]
        message = "source-only path was exported"
        """,
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="use_default_python_excludes"):
        check_sync_config(root=tmp_path)


def test_check_sync_config_rejects_config_missing_control_excludes(tmp_path: Path):
    write_sync_scaffold(root=tmp_path, settings=_settings())
    config_path = tmp_path / "copy.barista.toml"
    config_path.write_text(
        """
        [workflow]
        name = "configgle"
        mode = "squash"
        source_root = "packages/configgle"

        [files]
        include = ["**"]
        use_default_python_excludes = true
        exclude = ["copy.bara.sky"]

        [[leak_check.forbidden_path]]
        id = "source-only-paths"
        paths = ["private/**"]
        message = "source-only path was exported"
        """,
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match=r"must exclude copy\.barista\.toml"):
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
            "${{ vars.COPYBARISTA_SOURCE_REPO }}",
            "wrong/repo",
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


def test_check_sync_config_reports_malformed_package_validation_yaml(
    tmp_path: Path,
):
    write_sync_scaffold(root=tmp_path, settings=_settings())
    (tmp_path / ".github/workflows/package-validation.yml").write_text(
        "jobs: [\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match=r"package-validation\.yml is not what"):
        check_sync_config(root=tmp_path)


def test_check_sync_config_rejects_parent_based_import_baseline(tmp_path: Path):
    """A template that drops the ledger lookup must fail the config check."""
    write_sync_scaffold(root=tmp_path, settings=_settings())
    workflow = tmp_path / ".github/workflows/sync-to-source.yml"
    text = workflow.read_text(encoding="utf-8")
    start = text.index('base_ref="$(uv')
    end = text.index('github.event.before }}")', start) + len(
        'github.event.before }}")'
    )
    workflow.write_text(
        text[:start] + 'base_ref="${{ github.event.before }}"' + text[end:],
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="--print-synced-base"):
        check_sync_config(root=tmp_path)


def test_check_sync_config_rejects_baseline_resolved_before_the_ledger(tmp_path: Path):
    """Ordering regressions fail too: flags present but the ledger unreadable.

    Moving the ref resolution back above the target checkout leaves every flag
    in place while making the ledger unreadable where the baseline is chosen --
    exactly the shape a substring check misses.
    """
    write_sync_scaffold(root=tmp_path, settings=_settings())
    workflow = tmp_path / ".github/workflows/sync-to-source.yml"
    text = workflow.read_text(encoding="utf-8")
    start = text.index("      - id: refs\n")
    checkout = GITHUB_ACTION_PINS["actions/checkout"]
    end = text.index(f"      - uses: {checkout.uses}\n", start)
    block = text[start:end]
    workflow.write_text(
        (text[:start] + text[end:]).replace(
            "      - id: settings\n", block + "      - id: settings\n", 1
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="before the step that resolves"):
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
        source_root = "packages/configgle"
        public_repo = "rekursiv-ai/configgle"
        source_repo = "example/source"
        copybarista_project_path = "tools/copybarista"
        smoke_import = "configgle"
        type_check_targets = ["configgle"]
        forbidden_pr_text = []
        """,
        encoding="utf-8",
    )

    settings = load_sync_settings(config)

    assert settings.sync_user_name == "copybarista"
    assert settings.sync_user_email == "copybarista@example.com"
    assert settings.sync_token_login == ""
    assert settings.export_prefix == "configgle/export/"
    assert settings.import_prefix == "configgle/import/"
    assert settings.validation_python_versions == ("3.12",)
    # Lint/type/test checks are delegated to the package's own
    # .pre-commit-config.yaml; only the steps pre-commit does not own stay
    # here. Restating each tool would mean a newly added hook silently goes
    # unrun in the public repo.
    assert settings.validation_commands == (
        "uv sync --all-groups",
        "uv run pre-commit run --all-files --hook-stage pre-commit",
        "uv run pre-commit run --all-files --hook-stage pre-push",
        'uv run python -c "import configgle"',
        "uv build",
    )
    assert settings.pr_default_title == "Update Configgle export"
    assert (
        settings.pr_default_body
        == "Updates the generated Configgle public repository export."
    )
    assert not settings.require_pr_metadata
    assert settings.pr_metadata_source == "commit_messages"
    assert settings.replay_bootstrap_base == ""
    assert not settings.publish_source_rev
    assert not settings.refresh_public_lockfile
    assert settings.release_check_script == ""


def test_load_sync_settings_reads_pull_request_defaults(tmp_path: Path):
    config = tmp_path / "copybarista.sync.toml"
    config.write_text(
        """
        [sync]
        package_name = "configgle"
        sync_label = "Configgle"
        source_root = "packages/configgle"
        public_repo = "rekursiv-ai/configgle"
        source_repo = "example/source"
        copybarista_project_path = "tools/copybarista"
        smoke_import = "configgle"
        type_check_targets = ["configgle"]
        forbidden_pr_text = []

        [pull_request]
        default_title = "Prepare public release"
        default_body = "Public reviewer context."
        require_pr_metadata = true
        metadata_source = "commit_messages"
        replay_bootstrap_base = "main~10"
        publish_source_rev = true
        """,
        encoding="utf-8",
    )

    settings = load_sync_settings(config)

    assert settings.pr_default_title == "Prepare public release"
    assert settings.pr_default_body == "Public reviewer context."
    assert settings.require_pr_metadata
    assert settings.replay_bootstrap_base == "main~10"
    assert settings.publish_source_rev


def test_load_sync_settings_rejects_unknown_metadata_source(tmp_path: Path):
    write_sync_scaffold(root=tmp_path, settings=_settings())
    config = tmp_path / "copybarista.sync.toml"
    config.write_text(
        config.read_text(encoding="utf-8").replace(
            'metadata_source = "commit_messages"',
            'metadata_source = "intent_file"',
        ),
        encoding="utf-8",
    )

    with pytest.raises(
        ConfigError,
        match=r"copybarista\.sync\.toml \[pull_request\]\.metadata_source",
    ):
        load_sync_settings(config)


def test_load_sync_settings_rejects_wrong_array_shape(tmp_path: Path):
    config = tmp_path / "copybarista.sync.toml"
    config.write_text(
        """
        [sync]
        package_name = "configgle"
        sync_label = "Configgle"
        source_root = "packages/configgle"
        public_repo = "rekursiv-ai/configgle"
        source_repo = "example/source"
        copybarista_project_path = "tools/copybarista"
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
    write_sync_scaffold(
        root=tmp_path, settings=_settings(import_branch_prefix="configgle/import/")
    )
    config = tmp_path / "copybarista.sync.toml"
    config.write_text(
        config.read_text(encoding="utf-8").replace("configgle/import/", "main"),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="import_branch_prefix"):
        load_sync_settings(config)


def test_generated_workflows_reject_invalid_smoke_import():
    with pytest.raises(ConfigError, match="smoke_import"):
        package_validation_workflow(_settings(smoke_import="configgle; print(1)"))


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


@pytest.mark.parametrize(
    "render",
    [package_validation_workflow, import_workflow, export_workflow],
    ids=["package-validation", "import", "export"],
)
def test_apt_is_bounded_and_retried_in_every_workflow(
    render: Callable[[SyncSettings], str],
) -> None:
    """A stalled apt mirror must cost minutes, not the whole job budget.

    Measured: ``apt-get update`` hung from 02:23:21 to 03:07:36 on an Ubuntu
    mirror and was cancelled by the 45-minute job cap, taking two exports with
    it. The command normally finishes in ~15s, and nothing bounded it -- no
    timeout, no retry -- so one upstream stall burned every remaining minute
    and reported only "The operation was canceled".

    Both parts are load-bearing. The timeout converts a hang into a fast
    failure; the retry is what lets a transient stall still succeed, since apt
    had already fallen back from the Azure mirror to archive.ubuntu.com in that
    same run. All three workflows render from one helper, so all three are
    checked -- the export is the one that actually failed.
    """
    workflow = render(_settings(system_packages=("ripgrep",)))

    assert "timeout " in workflow, "apt is unbounded; a mirror stall hangs the job"
    assert "for attempt in" in workflow, (
        "apt has no retry; a transient mirror stall fails the whole workflow"
    )


def test_check_sync_config_rejects_package_validation_drift(tmp_path: Path):
    write_sync_scaffold(root=tmp_path, settings=_settings())
    workflow = tmp_path / ".github/workflows/package-validation.yml"
    workflow.write_text(
        workflow.read_text(encoding="utf-8").replace(
            "--hook-stage pre-push",
            "--hook-stage pre-push --hook pytest",
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match=r"package-validation\.yml is not what"):
        check_sync_config(root=tmp_path)


def test_check_sync_config_rejects_a_permissions_escalation(tmp_path: Path):
    """Byte-parity covers fields a hand-listed check never looked at.

    The previous validator inspected the job name, python-version, command
    lines, and apt packages -- so widening ``permissions`` to ``contents:
    write`` in a workflow that ships to a public repository passed.
    """
    write_sync_scaffold(root=tmp_path, settings=_settings())
    workflow = tmp_path / ".github/workflows/package-validation.yml"
    workflow.write_text(
        workflow.read_text(encoding="utf-8").replace(
            "contents: read", "contents: write"
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match=r"package-validation\.yml is not what"):
        check_sync_config(root=tmp_path)


def test_export_workflow_uses_metadata_without_package_specific_env_names():
    workflow = export_workflow(_settings())

    assert 'name: "Export Configgle public repo"' in workflow
    assert 'name: "Export Configgle and update public PR"' in workflow
    assert "configgle/export/main" in workflow
    assert (
        "group: copybarista-export-${{ github.workflow }}-${{ github.ref }}" in workflow
    )
    assert '--sync-label "$SYNC_LABEL"' in workflow
    assert '--auto-merge="$COPYBARISTA_AUTO_MERGE"' in workflow
    assert "CONFIGGLE" not in workflow
    assert "sync_configgle" not in workflow
    assert "source/tools/copybarista/scripts/sync_export_pr.py" in workflow
    assert "fetch-depth: 0" in workflow
    assert "--pr-scope" in workflow
    assert "configgle" in workflow
    assert "--pr-default-title" in workflow
    assert "--pr-default-body" in workflow
    assert "--require-pr-metadata" not in workflow


def test_export_workflow_passes_pr_replay_flags():
    workflow = export_workflow(
        _settings(
            require_pr_metadata=True,
            replay_bootstrap_base="main~10",
            publish_source_rev=True,
        )
    )

    assert "--require-pr-metadata" in workflow
    assert "--publish-source-rev" in workflow
    # The replay base rides the env var, not a CLI flag: the flag's argparse
    # default IS that env var, so emitting both made the flag always win and
    # the workflow_dispatch input silently do nothing.
    assert "--replay-bootstrap-base" not in workflow
    assert (
        "COPYBARISTA_REPLAY_BOOTSTRAP_BASE: "
        "${{ inputs.replay_bootstrap_base || 'main~10' }}" in workflow
    )


def test_export_workflow_can_refresh_public_lockfile():
    workflow = export_workflow(_settings(refresh_public_lockfile=True))

    assert "--refresh-public-lockfile" in workflow


def test_export_workflow_can_run_release_check_script():
    workflow = export_workflow(_settings(release_check_script="scripts/check.py"))

    assert "--release-check-script scripts/check.py" in workflow


def test_import_workflow_can_ignore_generated_public_lockfile():
    workflow = import_workflow(_settings(refresh_public_lockfile=True))

    assert "--refresh-public-lockfile" in workflow


def test_export_workflow_can_guard_sync_token_login():
    sync_actor = "rekursiv-bot"
    workflow = export_workflow(_settings(sync_token_login=sync_actor))

    assert 'COPYBARISTA_SYNC_TOKEN_LOGIN: "rekursiv-bot"' in workflow
    assert "gh api user --jq .login" in workflow
    assert "git author settings cannot change GitHub push or PR attribution" in workflow


def test_generated_workflows_keep_readable_line_continuations():
    workflow = export_workflow(_settings())

    assert "run: |\n          uv --quiet" in workflow
    assert " \\\n            --source-dir source" in workflow


def test_import_workflow_uses_metadata_and_splits_trusted_pr_step():
    workflow = import_workflow(_settings())

    assert "TARGET_REPO: ${{ vars.COPYBARISTA_SOURCE_REPO }}" in workflow
    assert (
        "TARGET_PROJECT_PATH: ${{ vars.COPYBARISTA_TARGET_PROJECT_PATH }}" in workflow
    )
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
    assert "id: settings" in workflow
    assert "Public-to-source import is not configured; skipping." in workflow
    assert 'if [ "${{ github.event_name }}" = "workflow_dispatch" ]; then' in workflow
    assert "if: steps.settings.outputs.enabled == 'true'" in workflow
    assert "--open-pr false" in workflow
    assert "--open-pr-only" in workflow
    assert '--branch-prefix "$COPYBARISTA_IMPORT_BRANCH_PREFIX"' in workflow
    assert '--sync-label "$COPYBARISTA_SYNC_LABEL"' in workflow
    assert "GH_TOKEN: ${{ secrets.COPYBARISTA_IMPORT_TOKEN }}" in workflow


def test_import_workflow_resolves_push_baseline_from_the_ledger():
    """A push import must merge against the last LANDED import, not the parent.

    ``github.event.before`` is the pushed commit's parent, which equals the
    tree the source last absorbed only while every import lands. The moment one
    fails or goes unmerged the parent marches on while the source stays pinned,
    so a parent baseline feeds the three-way merge a wrong common ancestor and
    manufactures conflicts. The baseline therefore comes from the target's own
    import ledger (``--print-synced-base``), with the parent kept only as the
    ``--fallback-sha`` bootstrap for a project that has never imported.
    """
    steps = _import_steps(import_workflow(_settings()))
    refs = _step_index(steps, lambda step: step.get("id") == "refs")
    helper = _step_index(
        steps, lambda step: step.get("name") == "Capture trusted import helper"
    )
    ledger = _step_index(steps, lambda step: _checkout_path(step) == "target")
    public_base = _step_index(steps, lambda step: _checkout_path(step) == "public-base")
    public_head = _step_index(steps, lambda step: _checkout_path(step) == "public-head")
    run = str(steps[refs]["run"])

    assert "--print-synced-base" in run
    assert '--fallback-sha "${{ github.event.before }}"' in run
    # Ordering is the whole defect: the ledger checkout and the helper that
    # reads it must both precede the step that chooses the baseline, and the
    # public checkouts that consume it must follow.
    assert ledger < refs, "target checkout (the ledger) must precede ref resolution"
    assert helper < refs, "import helper must be installed before ref resolution"
    assert refs < public_base, "public-base checkout must consume the resolved baseline"
    assert refs < public_head


def test_import_workflow_keeps_explicit_base_semantics_off_the_ledger():
    """Only push events infer a baseline; explicit bases stay verbatim."""
    steps = _import_steps(import_workflow(_settings()))
    run = str(steps[_step_index(steps, lambda step: step.get("id") == "refs")]["run"])

    assert 'base_ref="${{ github.event.pull_request.base.sha }}"' in run
    assert 'base_ref="$DISPATCH_PUBLIC_BASE_REF"' in run
    # The all-zeros/empty rejection and the ref-format check apply to the
    # RESOLVED baseline, so both must survive the reordering.
    assert 'if [ -z "$base_ref" ] || [ "$base_ref" = "00000000' in run
    assert 'git check-ref-format --allow-onelevel "$ref"' in run


def test_import_workflow_keeps_import_token_off_public_code_steps():
    """The import token stays on the credential-free checkout and the PR step."""
    steps = _import_steps(import_workflow(_settings()))
    ledger = steps[_step_index(steps, lambda step: _checkout_path(step) == "target")]
    with_config = cast("dict[str, Any]", ledger["with"])

    assert with_config.get("persist-credentials") is False
    assert with_config.get("token") == "${{ secrets.COPYBARISTA_IMPORT_TOKEN }}"
    # No step that runs imported public code may see the token. The only other
    # readers are the settings probe and the PR step, which pushes the branch.
    assert [
        str(step.get("name", step.get("uses", "")))
        for step in steps
        if "COPYBARISTA_IMPORT_TOKEN" in str(step)
    ] == [
        "Check settings",
        action_ref("actions/checkout"),
        "Open or update target import PR",
    ]


def test_import_workflow_escapes_github_expression_strings():
    workflow = import_workflow(_settings(sync_label="Configgle's Core"))

    assert "Configgle''s Core export branch:" in workflow
    assert "Configgle's Core export branch:" not in workflow


def test_export_workflow_watches_source_and_sync_helpers():
    workflow = export_workflow(_settings())

    assert '"packages/configgle/**"' in workflow
    assert '"tools/copybarista/scripts/sync_export_pr.py"' in workflow
    assert '"tools/copybarista/scripts/sync_import_change.py"' in workflow


def test_export_workflow_watches_additional_source_paths():
    workflow = export_workflow(
        _settings(export_watch_paths=(".codespell-ignore", "docs/POLICY.md"))
    )

    assert '      - ".codespell-ignore"' in workflow
    assert '      - "docs/POLICY.md"' in workflow


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


def _import_steps(workflow: str) -> list[dict[str, Any]]:
    """Return the parsed steps of the generated import job, in file order."""
    parsed: Any = yaml.safe_load(workflow)
    steps: Any = parsed["jobs"]["import-change"]["steps"]
    return [cast("dict[str, Any]", step) for step in cast("list[Any]", steps)]


def _step_index(
    steps: list[dict[str, Any]], predicate: Callable[[dict[str, Any]], bool]
) -> int:
    """Return the position of the one step matching `predicate`."""
    matches = [index for index, step in enumerate(steps) if predicate(step)]
    assert len(matches) == 1, f"expected exactly one matching step, got {matches}"
    return matches[0]


def _checkout_path(step: dict[str, Any]) -> str:
    """Return the `path` a checkout step writes to, or empty for other steps."""
    if step.get("uses") != action_ref("actions/checkout"):
        return ""
    with_config: Any = step.get("with", {})
    return str(cast("dict[str, Any]", with_config).get("path", ""))


def test_workflow_dir_prefers_the_staged_export_over_a_source_only_github(
    tmp_path: Path,
):
    """When both layouts exist, the shipped one wins.

    A package can keep a source-only ``.github/workflows/pages.yml`` beside
    its staged export. Validating the bare directory there would check
    a file the export excludes, and miss the workflows that actually ship.
    """
    (tmp_path / ".github/workflows").mkdir(parents=True)
    (tmp_path / ".export/.github/workflows").mkdir(parents=True)

    assert workflow_dir(tmp_path) == tmp_path / ".export/.github/workflows"


def test_workflow_dir_falls_back_for_a_fresh_scaffold(tmp_path: Path):
    """A scaffold has no ``.export/`` yet, so the bare path is correct."""
    (tmp_path / ".github/workflows").mkdir(parents=True)

    assert workflow_dir(tmp_path) == tmp_path / ".github/workflows"
