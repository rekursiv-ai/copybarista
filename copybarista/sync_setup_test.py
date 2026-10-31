"""Tests for reusable package sync scaffolding."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Final, Unpack
from typing_extensions import TypedDict

import dataclasses
import os
import re
import shlex
import shutil
import subprocess
import sys

from treekle import from_plain

import pytest
import yaml

from copybarista.action_pins import GITHUB_ACTION_PINS, action_ref
from copybarista.errors import ConfigError
from copybarista.sync_setup import (
    CI_COMMANDS,
    PG_MAJOR,
    SyncSettings,
    _render_template,
    check_sync_config,
    export_inputs,
    export_workflow,
    import_workflow,
    load_sync_settings,
    package_validation_workflow,
    publish_npm_workflow,
    sync_toml,
    workflow_dir,
    write_sync_scaffold,
)


_CWD: Final = Path(__file__).resolve().parent

if TYPE_CHECKING:
    from collections.abc import Callable


class _SyncSettingsFields(TypedDict, total=False, closed=True):
    """The ``SyncSettings`` fields a test overrides."""

    export_watch_paths: tuple[str, ...]
    import_branch_prefix: str
    npm_build_commands: tuple[str, ...]
    npm_packages: tuple[str, ...]
    package_name: str
    publish_source_rev: bool
    refresh_public_lockfile: bool
    release_check_script: str
    replay_bootstrap_base: str
    replay_bootstrap_base_comment: tuple[str, ...]
    require_pr_metadata: bool
    skip_source_validation: bool
    sync_label: str
    sync_token_login: str
    system_packages: tuple[str, ...]
    validation_python_versions: tuple[str, ...]


def _settings(**fields: Unpack[_SyncSettingsFields]) -> SyncSettings:
    built = SyncSettings(
        package_name="configgle",
        sync_label="Configgle",
        source_root="packages/configgle",
        public_repo="example/configgle",
        source_repo="example/source",
        copybarista_project_path="tools/copybarista",
        type_check_targets=("configgle", "tests"),
        forbidden_pr_text=("loop",),
    )
    return dataclasses.replace(built, **fields)


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
    assert tmp_path / ".github/workflows/configgle-to-loop.yml" in written
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
    (tmp_path / ".github/workflows/configgle-to-loop.yml").unlink()

    with pytest.raises(ConfigError, match="Missing sync files"):
        check_sync_config(root=tmp_path)


def test_check_sync_config_rejects_missing_package_validation_workflow(
    tmp_path: Path,
):
    write_sync_scaffold(root=tmp_path, settings=_settings())
    (tmp_path / ".github/workflows/package-validation.yml").unlink()

    with pytest.raises(ConfigError, match="Missing sync files"):
        check_sync_config(root=tmp_path)


def test_the_scaffold_pins_a_checkout_to_the_newest_supported_python(tmp_path: Path):
    write_sync_scaffold(root=tmp_path, settings=_settings())

    assert (tmp_path / ".python-version").read_text(encoding="utf-8") == "3.14\n"


@pytest.mark.parametrize(
    ("name", "text", "message"),
    [
        (".python-version", None, "Missing sync files"),
        (".python-version", "3.15\n", r"\.python-version must hold 3\.14"),
        (
            ".gitignore",
            "*.pyc\n.python-version\n",
            r"\.gitignore must not ignore \.python-version",
        ),
    ],
    ids=["missing", "another version", "ignored"],
)
def test_check_sync_config_rejects_a_checkout_without_its_python_pin(
    tmp_path: Path,
    name: str,
    text: str | None,
    message: str,
):
    # Without the pin, uv in a public checkout picks the newest Python installed,
    # which can be one the package's wheels do not yet exist for.
    write_sync_scaffold(root=tmp_path, settings=_settings())
    if text is None:
        (tmp_path / name).unlink()
    else:
        (tmp_path / name).write_text(text, encoding="utf-8")

    with pytest.raises(ConfigError, match=message):
        check_sync_config(root=tmp_path)


def test_check_sync_config_accepts_a_commented_python_pin_ignore(tmp_path: Path):
    write_sync_scaffold(root=tmp_path, settings=_settings())
    (tmp_path / ".gitignore").write_text("# .python-version\n", encoding="utf-8")

    check_sync_config(root=tmp_path)


def _setup_steps(workflow: str) -> list[tuple[str, object]]:
    """Return each setup-python and setup-uv step's ``python-version``, in order."""
    # YAML 1.1 reads the ``on:`` key as ``True``.
    jobs = from_plain(yaml.safe_load(workflow), dict[object, object])["jobs"]
    found: list[tuple[str, object]] = []
    for job in from_plain(jobs, dict[str, dict[str, object]]).values():
        for step in from_plain(job["steps"], list[dict[str, object]]):
            uses = str(step.get("uses", ""))
            if "setup-python" in uses or "setup-uv" in uses:
                given = from_plain(step.get("with", {}), dict[str, object])
                found.append(
                    (uses.split("@", maxsplit=1)[0], given.get("python-version")),
                )
    return found


# A checkout's ``.python-version`` outranks the interpreter setup-python installs, so
# without it every job would test the pinned version instead of the one it names.
@pytest.mark.parametrize(
    "render",
    [
        lambda: package_validation_workflow(_settings()),
        lambda: package_validation_workflow(
            _settings(validation_python_versions=("3.12", "3.13")),
        ),
        lambda: export_workflow(_settings(), inputs=()),
        lambda: import_workflow(_settings()),
    ],
    ids=["validation", "validation matrix", "export", "import"],
)
def test_setup_uv_runs_the_python_setup_python_installed(
    render: Callable[[], str],
):
    steps = _setup_steps(render())

    assert [action for action, _ in steps] == [
        "actions/setup-python",
        "astral-sh/setup-uv",
    ]
    assert steps[1][1] == steps[0][1]


def test_check_sync_config_rejects_workflow_drift(tmp_path: Path):
    write_sync_scaffold(root=tmp_path, settings=_settings())
    workflow = tmp_path / ".github/workflows/configgle-to-loop.yml"
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
    (tmp_path / ".github/workflows/configgle-to-loop.yml").write_text(
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
    workflow = tmp_path / ".github/workflows/configgle-to-loop.yml"
    text = workflow.read_text(encoding="utf-8")
    start = text.index('base_ref="$(uv')
    end = text.index('github.event.before }}")', start) + len(
        'github.event.before }}")',
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
    workflow = tmp_path / ".github/workflows/configgle-to-loop.yml"
    text = workflow.read_text(encoding="utf-8")
    start = text.index("      - id: refs\n")
    checkout = GITHUB_ACTION_PINS["actions/checkout"]
    end = text.index(f"      - uses: {checkout.uses}\n", start)
    block = text[start:end]
    workflow.write_text(
        (text[:start] + text[end:]).replace(
            "      - id: settings\n",
            block + "      - id: settings\n",
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="before the step that resolves"):
        check_sync_config(root=tmp_path)


def test_check_sync_config_rejects_import_command_drift(tmp_path: Path):
    write_sync_scaffold(root=tmp_path, settings=_settings())
    workflow = tmp_path / ".github/workflows/configgle-to-loop.yml"
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
        root=tmp_path,
        settings=_settings(import_branch_prefix="configgle/import/"),
    )
    config = tmp_path / "copybarista.sync.toml"
    config.write_text(
        config.read_text(encoding="utf-8").replace("configgle/import/", "main"),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="import_branch_prefix"):
        load_sync_settings(config)


@pytest.mark.parametrize("key", ["validation_commands", "smoke_import", "typo"])
def test_load_sync_settings_rejects_a_key_it_does_not_read(tmp_path: Path, key: str):
    """A key the loader ignores would look like a setting that takes effect."""
    write_sync_scaffold(root=tmp_path, settings=_settings())
    config = tmp_path / "copybarista.sync.toml"
    config.write_text(
        config.read_text(encoding="utf-8").replace(
            "[sync]\n",
            f'[sync]\n{key} = ["uv run pytest"]\n',
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match=rf"sync\.{key} is not a setting"):
        load_sync_settings(config)


def test_every_package_validates_with_the_ci_commands():
    """No package chooses its commands, so none can validate differently."""
    for settings in (
        _settings(),
        _settings(package_name="sagent", validation_python_versions=("3.12", "3.13")),
    ):
        assert _step_run(
            package_validation_workflow(settings),
            job="validate",
            name="Validate package",
        ) == "".join(f"{command}\n" for command in CI_COMMANDS)


def test_the_import_validates_with_the_ci_commands():
    run = _step_run(
        import_workflow(_settings()),
        job="import-change",
        name="Import public tree into target repository",
    )

    flags = shlex.split(run.replace("\\\n", " "))
    commands = [
        flags[index + 1]
        for index, flag in enumerate(flags)
        if flag == "--validation-command"
    ]
    assert commands == list(CI_COMMANDS)


def test_the_export_leaves_validation_to_public_ci():
    assert "--validation-command" not in export_workflow(_settings(), inputs=())


def test_every_hook_ci_skips_runs_at_pre_push():
    """A hook skipped at pre-commit runs in its stronger pre-push form instead.

    ``CI_COMMANDS`` skips the ``superseded-at-pre-push`` alias at pre-commit and
    then runs the pre-push stage, so a hook given that alias with no pre-push
    counterpart would run nowhere in public CI.
    """
    # The nearest one up: the monorepo nests this package a level deeper than
    # the public repository does.
    path = next(
        directory / ".pre-commit-config.yaml"
        for directory in _CWD.parents
        if (directory / ".pre-commit-config.yaml").is_file()
    )
    config = from_plain(
        yaml.safe_load(path.read_text(encoding="utf-8")),
        dict[str, object],
    )
    hooks = [
        from_plain(hook, dict[str, object])
        for repo in from_plain(config["repos"], list[dict[str, object]])
        for hook in from_plain(repo.get("hooks", []), list[object])
    ]
    skipped = [
        hook["id"] for hook in hooks if hook.get("alias") == "superseded-at-pre-push"
    ]
    pre_push = {
        hook["id"]
        for hook in hooks
        if "pre-push" in from_plain(hook.get("stages", []), list[str])
    }

    assert "SKIP=superseded-at-pre-push" in CI_COMMANDS[1]
    assert "--hook-stage pre-push" in CI_COMMANDS[2]
    assert skipped
    assert [hook for hook in skipped if f"{hook}-global" not in pre_push] == []


def test_the_export_job_installs_no_system_packages():
    """The export runs no package checks, so nothing there needs them.

    Rendered anyway, the step ran ``bash install-libjpeg-turbo.sh`` from the
    job's root, where the script is not, and failed every priml export.
    """
    workflow = export_workflow(
        _settings(system_packages=("ripgrep", "libturbojpeg")),
        inputs=(),
    )

    names = [step.get("name") for step in _job_steps(workflow, job="export-pr")]
    assert "Install system packages" not in names
    assert "install-libjpeg-turbo" not in workflow


def test_libturbojpeg_installs_with_the_shipped_script():
    """No apt release carries libjpeg-turbo 3, which pyturbojpeg 2 needs."""
    run = _step_run(
        package_validation_workflow(
            _settings(system_packages=("ripgrep", "libturbojpeg")),
        ),
        job="validate",
        name="Install system packages",
    )

    assert "apt-get install -y --no-install-recommends ripgrep\n" in run
    assert run.endswith("bash install-libjpeg-turbo.sh\n")


_NPM_PACKAGES: Final = ("npm/configgle-a", "npm/configgle-b")
_NPM_BUILD_COMMANDS: Final = ("node npm/build.js",)


def test_npm_settings_round_trip_through_the_sync_config(tmp_path: Path):
    original = _settings(
        npm_packages=_NPM_PACKAGES,
        npm_build_commands=_NPM_BUILD_COMMANDS,
    )
    path = tmp_path / "copybarista.sync.toml"
    path.write_text(sync_toml(original), encoding="utf-8")

    assert load_sync_settings(path) == original
    assert load_sync_settings(path).npm_packages == _NPM_PACKAGES


def test_no_workflow_mentions_npm_until_a_package_asks_for_it():
    """The npm job is opt-in: every other package's workflows stay as they were."""
    settings = _settings()

    for text in (
        package_validation_workflow(settings),
        import_workflow(settings),
        export_workflow(settings, inputs=()),
    ):
        assert "npm" not in text
    assert settings.npm_packages == ()
    assert settings.npm_build_commands == ()


def test_publish_npm_workflow_publishes_with_provenance_through_trusted_publishing():
    text = publish_npm_workflow(
        _settings(npm_packages=_NPM_PACKAGES, npm_build_commands=_NPM_BUILD_COMMANDS),
    )
    parsed = from_plain(yaml.safe_load(text), dict[object, object])
    job = from_plain(
        from_plain(parsed["jobs"], dict[str, object])["publish"],
        dict[str, object],
    )

    assert job["permissions"] == {"contents": "read", "id-token": "write"}
    assert job["environment"] == "npm"
    assert parsed["permissions"] == {"contents": "read"}
    assert "npm publish" in text
    assert "--provenance --access public" in text
    assert "NODE_AUTH_TOKEN" not in text
    assert "secrets." not in text
    assert "npm@^11.5.1" in text
    for package in _NPM_PACKAGES:
        assert package in text
    uses = [str(step.get("uses", "")) for step in _job_steps(text, job="publish")]
    assert GITHUB_ACTION_PINS["actions/setup-node"].ref in uses
    assert GITHUB_ACTION_PINS["actions/checkout"].ref in uses


def test_publish_npm_workflow_builds_then_waits_for_pypi_then_publishes():
    text = publish_npm_workflow(
        _settings(
            npm_packages=("npm/configgle-a",),
            npm_build_commands=("node npm/build.js", "echo second"),
        ),
    )
    names = [str(step.get("name", "")) for step in _job_steps(text, job="publish")]

    assert names == [
        "",
        "",
        "Install npm 11.5.1 or later",
        "Build packages",
        "Publish packages",
    ]
    assert text.index("node npm/build.js") < text.index("echo second")
    assert text.index("echo second") < text.index("https://pypi.org/pypi/")
    assert text.index("https://pypi.org/pypi/") < text.index("npm publish")
    assert "npm view" in text


def test_publish_npm_workflow_without_build_commands_has_no_build_step():
    names = [
        str(step.get("name", ""))
        for step in _job_steps(
            publish_npm_workflow(_settings(npm_packages=("npm/configgle-a",))),
            job="publish",
        )
    ]

    assert "Build packages" not in names


def test_publish_npm_workflow_needs_a_package():
    with pytest.raises(ConfigError, match="npm_packages"):
        publish_npm_workflow(_settings())


@pytest.mark.parametrize(
    "package",
    ["../outside", "/abs/path", "npm/a b", "npm/a;b", "npm/$(id)", ""],
)
def test_npm_package_paths_must_be_safe_to_put_in_a_shell_line(package: str):
    with pytest.raises(ConfigError, match="npm_packages"):
        publish_npm_workflow(_settings(npm_packages=(package,)))


def test_npm_build_commands_without_packages_are_refused():
    with pytest.raises(ConfigError, match="npm_build_commands"):
        sync_toml(_settings(npm_build_commands=("node build.js",)))


@pytest.mark.parametrize("command", ["", "  ", "# comment", "one\ntwo"])
def test_npm_build_commands_must_be_executable_single_lines(command: str):
    with pytest.raises(ConfigError, match="npm_build_commands"):
        sync_toml(_settings(npm_packages=("npm/a",), npm_build_commands=(command,)))


def test_scaffold_writes_the_npm_workflow_only_when_enabled(tmp_path: Path):
    plain = tmp_path / "plain"
    npm = tmp_path / "npm"

    write_sync_scaffold(root=plain, settings=_settings())
    written = write_sync_scaffold(
        root=npm,
        settings=_settings(
            npm_packages=_NPM_PACKAGES,
            npm_build_commands=_NPM_BUILD_COMMANDS,
        ),
    )

    assert not (plain / ".github/workflows/publish-npm.yml").exists()
    assert npm / ".github/workflows/publish-npm.yml" in written
    check_sync_config(root=plain)
    check_sync_config(root=npm)


def test_check_sync_config_requires_the_npm_workflow_when_enabled(tmp_path: Path):
    write_sync_scaffold(
        root=tmp_path,
        settings=_settings(
            npm_packages=_NPM_PACKAGES,
            npm_build_commands=_NPM_BUILD_COMMANDS,
        ),
    )
    (tmp_path / ".github/workflows/publish-npm.yml").unlink()

    with pytest.raises(ConfigError, match="Missing sync files"):
        check_sync_config(root=tmp_path)


def test_check_sync_config_rejects_npm_workflow_drift(tmp_path: Path):
    write_sync_scaffold(
        root=tmp_path,
        settings=_settings(
            npm_packages=_NPM_PACKAGES,
            npm_build_commands=_NPM_BUILD_COMMANDS,
        ),
    )
    workflow = tmp_path / ".github/workflows/publish-npm.yml"
    workflow.write_text(
        workflow.read_text(encoding="utf-8").replace(
            "id-token: write",
            "id-token: write\n      contents: write",
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match=r"publish-npm\.yml is not what"):
        check_sync_config(root=tmp_path)


def _export_workflow(settings: SyncSettings) -> str:
    """Render the export workflow with no inputs outside its source root."""
    return export_workflow(settings, inputs=())


@pytest.mark.parametrize(
    "render",
    [package_validation_workflow, import_workflow],
    ids=["package-validation", "import"],
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
    same run. Both workflows that install packages render from one helper, so
    both are checked.
    """
    workflow = render(_settings(system_packages=("ripgrep",)))

    assert "timeout " in workflow, "apt is unbounded; a mirror stall hangs the job"
    assert "for attempt in" in workflow, (
        "apt has no retry; a transient mirror stall fails the whole workflow"
    )


@pytest.mark.parametrize(
    ("render", "job", "guard"),
    [
        (package_validation_workflow, "validate", {}),
        (
            import_workflow,
            "import-change",
            {"if": "steps.refs.outputs.import == 'true'"},
        ),
    ],
    ids=["package-validation", "import"],
)
def test_apt_steps_cache_debs_and_serve_postgresql_from_pgdg(
    render: Callable[[SyncSettings], str],
    job: str,
    guard: dict[str, str],
) -> None:
    """Pin both rendered apt steps; only the import job gates them on its refs.

    ``postgresql`` must never reach the distro install line: the metapackage
    would pull Ubuntu's older major beside PGDG's pinned one.
    """
    workflow = render(
        _settings(system_packages=("ripgrep", "postgresql")),
    )
    steps = _job_steps(workflow, job=job)
    cache = steps[
        _step_index(steps, lambda step: step.get("name") == "Cache apt packages")
    ]
    install = steps[
        _step_index(steps, lambda step: step.get("name") == "Install system packages")
    ]
    with_config = from_plain(cache.pop("with"), dict[str, object])

    assert cache == {
        "name": "Cache apt packages",
        **guard,
        "uses": action_ref("actions/cache"),
    }
    assert with_config.pop("path") == "/var/cache/apt/archives"
    assert re.fullmatch(
        r"apt-\$\{\{ runner\.os \}\}-[0-9a-f]{16}",
        str(with_config.pop("key")),
    )
    assert not with_config
    assert install == {
        "name": "Install system packages",
        **guard,
        "run": (
            "for attempt in 1 2 3; do\n"
            "  timeout 120 sudo apt-get update && break\n"
            '  echo "apt-get update stalled (attempt $attempt)" >&2\n'
            "  sleep $((attempt * 10))\n"
            "done\n"
            "timeout 300 sudo apt-get install -y --no-install-recommends ripgrep\n"
            "sudo apt-get install -y --no-install-recommends "
            "postgresql-common ca-certificates curl gnupg\n"
            "sudo /usr/share/postgresql-common/pgdg/apt.postgresql.org.sh -y\n"
            "timeout 120 sudo apt-get update\n"
            "timeout 300 sudo apt-get install -y --no-install-recommends "
            f"postgresql-{PG_MAJOR} postgresql-client-{PG_MAJOR} "
            f"postgresql-{PG_MAJOR}-pgvector\n"
        ),
    }


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
            "contents: read",
            "contents: write",
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match=r"package-validation\.yml is not what"):
        check_sync_config(root=tmp_path)


@pytest.mark.parametrize(
    "label",
    ["Config→gle", "a\u2028b", "a\u2029b", "a\x85b", "a\x7fb", "a\x9fb"],
)
def test_import_workflow_keeps_the_sync_label_verbatim(label: str):
    """YAML folds raw U+0085/U+2028/U+2029 and rejects raw DEL and C1 controls."""
    parsed = from_plain(
        yaml.safe_load(import_workflow(_settings(sync_label=label))),
        dict[object, object],
    )
    job = from_plain(
        from_plain(parsed["jobs"], dict[str, object])["import-change"],
        dict[str, object],
    )

    assert from_plain(job["env"], dict[str, object])["COPYBARISTA_SYNC_LABEL"] == label


def test_export_workflow_uses_metadata_without_package_specific_env_names():
    workflow = export_workflow(_settings(), inputs=())

    assert workflow.count('name: "loop→configgle"') == 2
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
        ),
        inputs=(),
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
    workflow = export_workflow(
        _settings(refresh_public_lockfile=True),
        inputs=(),
    )

    assert "--refresh-public-lockfile" in workflow


def test_export_workflow_lets_the_export_read_sibling_export_runs():
    """The export waits on sibling runs with the workflow token, not the sync token."""
    workflow = from_plain(
        yaml.safe_load(export_workflow(_settings(), inputs=())),
        dict[object, object],
    )
    assert workflow["permissions"] == {
        "contents": "read",
        "actions": "read",
        "pull-requests": "read",
    }
    jobs = from_plain(workflow["jobs"], dict[str, dict[str, object]])
    env = from_plain(jobs["export-pr"]["env"], dict[str, str])
    assert env["COPYBARISTA_ACTIONS_TOKEN"] == "${{ github.token }}"  # noqa: S105 -- An Actions expression, not a secret.


def test_export_workflow_installs_every_copybarista_dependency():
    """The export runs copybarista with ``--no-project``, so each import needs a ``--with``."""
    assert "--with tomlkit" in export_workflow(_settings(), inputs=())


def test_export_workflow_can_run_release_check_script():
    workflow = export_workflow(
        _settings(release_check_script="scripts/check.py"),
        inputs=(),
    )

    assert "--release-check-script scripts/check.py" in workflow


def test_import_workflow_can_ignore_generated_public_lockfile():
    workflow = import_workflow(
        _settings(refresh_public_lockfile=True),
    )

    assert "--refresh-public-lockfile" in workflow


def test_export_workflow_can_guard_sync_token_login():
    sync_actor = "rekursiv-bot"
    workflow = export_workflow(
        _settings(sync_token_login=sync_actor),
        inputs=(),
    )

    assert 'COPYBARISTA_SYNC_TOKEN_LOGIN: "rekursiv-bot"' in workflow
    assert "gh api user --jq .login" in workflow
    assert "git author settings cannot change GitHub push or PR attribution" in workflow


def _app_settings(tmp_path: Path) -> SyncSettings:
    """Load the App opt-in through the same TOML surface packages use."""
    config = tmp_path / "copybarista.sync.toml"
    config.write_text(
        sync_toml(_settings()).replace(
            "[sync]\n",
            '[sync]\nsync_app_slug = "example-sync"\n',
        ),
        encoding="utf-8",
    )
    return load_sync_settings(config)


def test_app_authentication_round_trips(tmp_path: Path):
    settings = _app_settings(tmp_path)

    assert 'sync_app_slug = "example-sync"' in sync_toml(settings)
    assert "sync_token_login" not in sync_toml(settings)


def test_auth_fragment_omits_the_file_banner():
    fragment = _render_template(
        "sync-personal-auth.yml.tmpl",
        {},
        include_banner=False,
    )

    assert fragment.startswith("      - name: Verify sync token identity\n")


@pytest.mark.parametrize(
    ("render", "job", "destination"),
    [
        (_export_workflow, "export-pr", "example/configgle"),
        (import_workflow, "import-change", "${{ env.TARGET_REPO }}"),
    ],
)
def test_app_workflows_mint_scoped_tokens(
    tmp_path: Path,
    render: Callable[[SyncSettings], str],
    job: str,
    destination: str,
):
    text = render(_app_settings(tmp_path))
    steps = _job_steps(text, job=job)
    mint = steps[_step_index(steps, lambda step: step.get("id") == "sync-app-token")]
    inputs = from_plain(mint["with"], dict[str, object])

    assert mint["uses"] == action_ref("actions/create-github-app-token")
    assert inputs["app-id"] == "${{ vars.SYNC_APP_ID }}"
    assert inputs["private-key"] == "${{ secrets.SYNC_APP_PRIVATE_KEY }}"
    assert inputs["repositories"] == destination
    assert inputs["permission-contents"] == "write"
    assert inputs["permission-pull-requests"] == "write"
    assert inputs["permission-workflows"] == "write"
    assert "COPYBARISTA_SYNC_TOKEN" not in text
    assert "COPYBARISTA_IMPORT_TOKEN" not in text
    assert "gh api user" not in text
    assert "${{ steps.sync-app-token.outputs.token }}" in text


def test_app_import_keeps_write_credentials_off_validation(tmp_path: Path):
    steps = _import_steps(import_workflow(_app_settings(tmp_path)))
    checkout = steps[_step_index(steps, lambda step: _checkout_path(step) == "target")]
    validation = steps[
        _step_index(
            steps,
            lambda step: (
                step.get("name") == "Import public tree into target repository"
            ),
        )
    ]

    assert (
        from_plain(checkout["with"], dict[str, object])["persist-credentials"] is False
    )
    assert "env" not in validation
    assert [
        str(step.get("name", step.get("uses", "")))
        for step in steps
        if "${{ steps.sync-app-token.outputs.token }}" in str(step)
    ] == [action_ref("actions/checkout"), "Open or update target import PR"]


@pytest.mark.parametrize("slug", ["wrong app", "Example-App", "../app"])
def test_app_settings_reject_invalid_slugs(tmp_path: Path, slug: str):
    _app_settings(tmp_path)
    config = tmp_path / "copybarista.sync.toml"
    config.write_text(
        config.read_text(encoding="utf-8").replace("example-sync", slug),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="sync_app_slug"):
        load_sync_settings(config)


def test_app_settings_reject_conflicting_personal_identity(tmp_path: Path):
    _app_settings(tmp_path)
    config = tmp_path / "copybarista.sync.toml"
    config.write_text(
        config.read_text(encoding="utf-8").replace(
            'sync_token_login = ""',
            'sync_token_login = "some-user"',
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="only one"):
        load_sync_settings(config)


@pytest.mark.cli_bash
@pytest.mark.parametrize("actual", ["example-sync", "different-app", ""])
def test_app_identity_guard_rejects_another_app(tmp_path: Path, actual: str):
    steps = _job_steps(_export_workflow(_app_settings(tmp_path)), job="export-pr")
    guard = steps[
        _step_index(steps, lambda step: step.get("name") == "Verify sync App identity")
    ]
    env = from_plain(guard["env"], dict[str, str])
    env["ACTUAL_SYNC_APP_SLUG"] = actual
    bash = shutil.which("bash")
    assert bash is not None

    result = subprocess.run(  # noqa: S603 -- Execute the generated identity guard.
        [bash, "--noprofile", "--norc", "-eo", "pipefail", "-c", str(guard["run"])],
        env={**os.environ, **env},
        capture_output=True,
        text=True,
        check=False,
    )

    assert (result.returncode == 0) is (actual == "example-sync")


def test_export_workflow_skips_the_source_repos_uv_cache():
    steps = _job_steps(export_workflow(_settings(), inputs=()), job="export-pr")
    setup_uv = steps[
        _step_index(
            steps,
            lambda step: step.get("uses") == action_ref("astral-sh/setup-uv"),
        )
    ]

    assert from_plain(setup_uv["with"], dict[str, object])["enable-cache"] is False


def test_generated_workflows_keep_readable_line_continuations():
    workflow = export_workflow(_settings(), inputs=())

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
    assert "id: settings" in workflow
    assert "Import back to loop is not configured; skipping." in workflow
    assert 'if [ "${{ github.event_name }}" = "workflow_dispatch" ]; then' in workflow
    assert "if: steps.settings.outputs.enabled == 'true'" in workflow
    assert "--open-pr false" in workflow
    assert "--open-pr-only" in workflow
    assert '--branch-prefix "$COPYBARISTA_IMPORT_BRANCH_PREFIX"' in workflow
    assert '--sync-label "$COPYBARISTA_SYNC_LABEL"' in workflow
    assert "GH_TOKEN: ${{ secrets.COPYBARISTA_IMPORT_TOKEN }}" in workflow


def test_import_workflow_uses_checked_out_public_head_sha():
    steps = _import_steps(import_workflow(_settings()))
    for step_name in (
        "Import public tree into target repository",
        "Open or update target import PR",
    ):
        run = str(
            steps[
                _step_index(
                    steps,
                    lambda step, expected=step_name: step.get("name") == expected,
                )
            ]["run"],
        )
        assert 'public_sha="$(git -C public-head rev-parse HEAD)"' in run
        assert '--public-sha "$public_sha"' in run
        assert '--public-sha "$GITHUB_SHA"' not in run


def test_import_workflow_does_not_trust_push_commit_metadata():
    workflow = import_workflow(_settings())

    assert "github.event.head_commit.author" not in workflow
    assert "github.event.head_commit.message" not in workflow


def test_import_workflow_records_push_before_reporting_validation_failure():
    steps = _import_steps(import_workflow(_settings()))
    import_index = _step_index(
        steps,
        lambda step: step.get("name") == "Import public tree into target repository",
    )
    pr_index = _step_index(
        steps,
        lambda step: step.get("name") == "Open or update target import PR",
    )
    report_index = _step_index(
        steps,
        lambda step: step.get("name") == "Report import validation failure",
    )

    assert (
        "--record-on-validation-failure \"${{ github.event_name == 'push' }}\""
        in str(steps[import_index]["run"])
    )
    assert import_index < pr_index < report_index
    assert "always()" in str(steps[report_index]["if"])
    assert "exit 1" in str(steps[report_index]["run"])


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
        steps,
        lambda step: step.get("name") == "Capture trusted import helper",
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


@pytest.mark.cli_bash
@pytest.mark.cli_git
@pytest.mark.cli_python_subprocess
def test_import_workflow_skips_a_push_the_source_already_reflects(tmp_path: Path):
    """An export landing on public main must not open an empty import PR.

    Every export squash-merges onto public main, and that push re-runs this
    workflow. The source already holds that content, so the import is a no-op,
    yet it still opened an empty ledger PR that blocked the next export until
    someone merged it. A commit behind the synced base is reflected too: main
    moved on before this run checked it out.
    """
    shas = _public_history_with_export(tmp_path)

    for head, before in (("export", "prior"), ("prior", "initial")):
        outputs = _resolve_push_refs(tmp_path, head=shas[head], before=shas[before])
        assert outputs.get("import") == "false", head


@pytest.mark.cli_bash
@pytest.mark.cli_git
@pytest.mark.cli_python_subprocess
def test_import_workflow_imports_a_push_past_the_synced_base(tmp_path: Path):
    """A public commit after the newest export is new content and must import."""
    shas = _public_history_with_export(tmp_path)

    outputs = _resolve_push_refs(tmp_path, head=shas["human"], before=shas["export"])

    assert outputs == {
        "base_ref": shas["export"],
        "head_ref": shas["human"],
        "import": "true",
    }


def test_import_workflow_gates_every_import_step_on_the_refs_decision():
    """A skipped push must not check out, validate, import, or open a PR."""
    steps = _import_steps(
        import_workflow(_settings(system_packages=("git",))),
    )
    refs = _step_index(steps, lambda step: step.get("id") == "refs")

    assert [
        str(step.get("name", step.get("uses", "")))
        for step in steps[refs + 1 :]
        if "steps.refs.outputs.import == 'true'" not in str(step.get("if"))
    ] == ["Upload import report"]


def test_import_workflow_keeps_import_token_off_public_code_steps():
    """The import token stays on the credential-free checkout and the PR step."""
    steps = _import_steps(import_workflow(_settings()))
    ledger = steps[_step_index(steps, lambda step: _checkout_path(step) == "target")]
    with_config = from_plain(ledger["with"], dict[str, object])

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


def test_import_workflow_quotes_sync_label():
    workflow = import_workflow(
        _settings(sync_label="Configgle's Core"),
    )

    assert 'COPYBARISTA_SYNC_LABEL: "Configgle\'s Core"' in workflow


def _scaffold_with_import_workflow(
    tmp_path: Path,
    *,
    packages: tuple[str, ...],
    edit: Callable[[str], str],
) -> None:
    """Scaffold a package, then rewrite its generated import workflow."""
    write_sync_scaffold(
        root=tmp_path,
        settings=_settings(system_packages=packages),
    )
    workflow = tmp_path / ".github/workflows/configgle-to-loop.yml"
    workflow.write_text(edit(workflow.read_text(encoding="utf-8")), encoding="utf-8")


def test_check_sync_config_accepts_package_requests_as_rendered(tmp_path: Path):
    """``postgresql`` is the pinned server; ``libturbojpeg`` is its installer."""
    _scaffold_with_import_workflow(
        tmp_path,
        packages=("ripgrep", "postgresql", "libturbojpeg"),
        edit=lambda text: text,
    )

    check_sync_config(root=tmp_path)


@pytest.mark.parametrize(
    ("dropped", "missing"),
    [
        (f" postgresql-{PG_MAJOR} ", "postgresql"),
        (" ripgrep\n", "ripgrep"),
        (" public-head/install-libjpeg-turbo.sh", "libturbojpeg"),
    ],
)
def test_check_sync_config_names_the_system_package_the_import_skips(
    tmp_path: Path,
    dropped: str,
    missing: str,
):
    _scaffold_with_import_workflow(
        tmp_path,
        packages=("ripgrep", "postgresql", "libturbojpeg"),
        edit=lambda text: text.replace(
            dropped,
            dropped.replace(dropped.strip(), "other"),
            1,
        ),
    )

    with pytest.raises(ConfigError) as error:
        check_sync_config(root=tmp_path)

    assert str(error.value) == (
        f"<repo>-to-loop.yml must install system package {missing!r} "
        "(see system_packages)."
    )


def test_check_sync_config_names_the_missing_install_step(tmp_path: Path):
    _scaffold_with_import_workflow(
        tmp_path,
        packages=("ripgrep",),
        edit=lambda text: text.replace(
            "name: Install system packages",
            "name: Install other things",
        ),
    )

    with pytest.raises(ConfigError) as error:
        check_sync_config(root=tmp_path)

    assert str(error.value) == (
        "<repo>-to-loop.yml must define step Install system packages."
    )


def test_check_sync_config_names_import_steps_that_are_not_a_list(tmp_path: Path):
    _scaffold_with_import_workflow(
        tmp_path,
        packages=(),
        edit=lambda text: text.replace("    steps:\n", "    steps: {}\n    old:\n", 1),
    )

    with pytest.raises(ConfigError) as error:
        check_sync_config(root=tmp_path)

    assert str(error.value) == (
        "<repo>-to-loop.yml jobs.import-change.steps must be a YAML list."
    )


def test_render_template_names_the_template_with_an_unrendered_token():
    """Every public template renders whole, so only a direct call reaches the check."""
    with pytest.raises(AssertionError) as error:
        _render_template("copybarista.sync.toml.tmpl", {})

    assert str(error.value) == (
        "Unrendered token in template copybarista.sync.toml.tmpl."
    )


def test_render_template_banner_names_its_template():
    assert export_workflow(_settings(), inputs=()).startswith(
        "# DO NOT EDIT -- generated from internal-to-external.yml.tmpl and this\n",
    )


def test_export_workflow_watches_source_and_sync_helpers():
    workflow = export_workflow(_settings(), inputs=())

    assert '"packages/configgle/**"' in workflow
    assert '"tools/copybarista/scripts/sync_export_pr.py"' in workflow
    assert '"tools/copybarista/scripts/sync_import_change.py"' in workflow


def test_export_workflow_watches_every_export_input():
    """A file outside the source root that the export reads starts the export."""
    workflow = export_workflow(
        _settings(),
        inputs=("pyproject.toml", "shared/exports.delta.toml", "lib/util.py"),
    )

    assert '      - "pyproject.toml"' in workflow
    assert '      - "shared/exports.delta.toml"' in workflow
    assert '      - "lib/util.py"' in workflow
    # The private negatives still close the list, so no input re-includes them.
    assert workflow.index('"lib/util.py"') < workflow.index('"!**/*private*"')


def test_export_inputs_name_every_file_the_export_reads(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text("a = 1\n", encoding="utf-8")
    (tmp_path / "shared.delta.toml").write_text("", encoding="utf-8")
    (tmp_path / "lib/files").mkdir(parents=True)
    (tmp_path / "lib/util.py").write_text("", encoding="utf-8")
    package = tmp_path / "packages/configgle"
    package.mkdir(parents=True)
    (package / "pyproject.delta.toml").write_text(
        '[pyproject]\nbase = "pyproject.toml"\nextends = ["shared.delta.toml"]\n',
        encoding="utf-8",
    )
    config = package / "copy.barista.toml"
    config.write_text(
        '[workflow]\nsource_root = "packages/configgle"\n\n[files]\n'
        '\n[[files.copy]]\nsource = "lib/util.py"\ndestination = "util.py"\n'
        '\n[[files.copy]]\nsource = "lib/files"\ndestination = "files"\n'
        '\n[files.pyproject]\ndelta = "packages/configgle/pyproject.delta.toml"\n',
        encoding="utf-8",
    )

    inputs = export_inputs(
        _settings(refresh_public_lockfile=True),
        config_path=config,
        source_ref=tmp_path,
    )

    assert inputs == (
        "lib/util.py",
        "lib/files/**",
        "packages/configgle/pyproject.delta.toml",
        "pyproject.toml",
        "shared.delta.toml",
        "uv.lock",
    )
    assert "uv.lock" not in export_inputs(
        _settings(),
        config_path=config,
        source_ref=tmp_path,
    )


def test_export_workflow_lists_an_input_once():
    workflow = export_workflow(
        _settings(export_watch_paths=(".codespell-ignore", "lib/files/**")),
        inputs=(
            ".codespell-ignore",
            "lib/files/**",
            "packages/configgle/pyproject.delta.toml",
        ),
    )

    assert workflow.count('".codespell-ignore"') == 1
    assert workflow.count('"lib/files/**"') == 1


def test_export_workflow_skips_an_input_a_watch_glob_covers():
    workflow = export_workflow(
        _settings(export_watch_paths=("lib/testing/**",)),
        inputs=("lib/testing/runner.py",),
    )

    assert "lib/testing/runner.py" not in workflow
    # The source root's own ``packages/configgle/**`` already covers the delta.
    assert "packages/configgle/pyproject.delta.toml" not in workflow


def test_export_workflow_watches_additional_source_paths():
    workflow = export_workflow(
        _settings(export_watch_paths=(".codespell-ignore", "docs/POLICY.md")),
        inputs=(),
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


def _import_steps(workflow: str) -> list[dict[str, object]]:
    """Return the parsed steps of the generated import job, in file order."""
    return _job_steps(workflow, job="import-change")


def _job_steps(workflow: str, *, job: str) -> list[dict[str, object]]:
    """Return the parsed steps of one generated workflow job, in file order."""
    parsed = from_plain(yaml.safe_load(workflow), dict[object, object])
    jobs = from_plain(parsed["jobs"], dict[str, object])
    job_config = from_plain(jobs[job], dict[str, object])
    steps = from_plain(job_config["steps"], list[object])
    return [from_plain(step, dict[str, object]) for step in steps]


def _step_run(workflow: str, *, job: str, name: str) -> str:
    """Return the ``run`` script of the named step of one generated job."""
    steps = _job_steps(workflow, job=job)
    return str(steps[_step_index(steps, lambda step: step.get("name") == name)]["run"])


def _step_index(
    steps: list[dict[str, object]],
    predicate: Callable[[dict[str, object]], bool],
) -> int:
    """Return the position of the one step matching `predicate`."""
    matches = [index for index, step in enumerate(steps) if predicate(step)]
    assert len(matches) == 1, f"expected exactly one matching step, got {matches}"
    return matches[0]


def _checkout_path(step: dict[str, object]) -> str:
    """Return the `path` a checkout step writes to, or empty for other steps."""
    if step.get("uses") != action_ref("actions/checkout"):
        return ""
    with_config = from_plain(step.get("with"), dict[str, object], default={})
    return str(with_config.get("path", ""))


# Public main: initial, prior, a landed Configgle export (its export branch points at
# it, which is what authenticates it), then a later human commit. Trees play no part in
# the decision, so every commit is empty. `target` aliases the public repo: it walks to
# no import subjects, i.e. a source with no ledger, without a second repository.
def _public_history_with_export(root: Path) -> dict[str, str]:
    """Create the `target` and `public-history` checkouts; return public SHAs."""
    public = root / "public-history"
    _git(root, "init", "-q", "-b", "main", str(public))
    (root / "target").symlink_to(public)
    commits = (
        ("initial", "Initial commit"),
        ("prior", "Prior change"),
        ("export", "Publish export\n\nConfiggle export branch: configgle/export/main"),
        ("human", "Human change"),
    )
    for _, message in commits:
        _git(public, "commit", "-q", "--allow-empty", "-m", message)
    log = _git(public, "log", "--reverse", "--format=%H").split()
    shas = {name: sha for (name, _), sha in zip(commits, log, strict=True)}
    _git(public, "branch", "configgle/export/main", shas["export"])
    return shas


# Executes the rendered step rather than matching its text: the decision spans the
# shell, the trusted helper's baseline walk, and git ancestry, and a substring check
# passes while any one of them is wired to the wrong directory or output name.
def _resolve_push_refs(root: Path, *, head: str, before: str) -> dict[str, str]:
    """Run 'Resolve public refs' as a push of `head`; return its step outputs."""
    steps = _import_steps(import_workflow(_settings()))
    run = str(steps[_step_index(steps, lambda step: step.get("id") == "refs")]["run"])
    expressions = {"github.event_name": "push", "github.event.before": before}
    bin_dir = root / "bin"
    bin_dir.mkdir(exist_ok=True)
    # The step runs the helper via `uv ... run --no-project python`; this test's own
    # interpreter stands in, so no uv environment is resolved per call.
    (bin_dir / "uv").write_text(
        f'#!/bin/sh\nwhile [ "$1" != python ]; do shift; done\nshift\n'
        f'exec "{sys.executable}" "$@"\n',
        encoding="utf-8",
    )
    (bin_dir / "uv").chmod(0o755)
    output = root / "github-output"
    output.write_text("", encoding="utf-8")
    bash = shutil.which("bash")
    assert bash is not None
    subprocess.run(  # noqa: S603 -- Fixed bash argv; the script is the rendered workflow step.
        [
            bash,
            "--noprofile",
            "--norc",
            "-eo",
            "pipefail",
            "-c",
            re.sub(r"\$\{\{ (.+?) \}\}", lambda m: expressions.get(m[1], ""), run),
        ],
        cwd=root,
        check=True,
        env={
            **os.environ,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "GITHUB_OUTPUT": str(output),
            "GITHUB_SHA": head,
            "COPYBARISTA_SYNC_LABEL": "Configgle",
            "TRUSTED_IMPORT_SCRIPT": str(
                _CWD / "scripts" / "sync_import_change.py",
            ),
        },
    )
    return {
        key: value
        for key, _, value in (
            line.partition("=")
            for line in output.read_text(encoding="utf-8").splitlines()
        )
    }


def _git(cwd: Path, *args: str) -> str:
    """Run git in `cwd` with a fixed identity; return its stripped stdout."""
    git = shutil.which("git")
    assert git is not None
    return subprocess.run(  # noqa: S603 -- Fixed git subcommands over test paths.
        [
            git,
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


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


if __name__ == "__main__":
    from copybarista.lib.testing.runner import test_main

    test_main(__file__)
