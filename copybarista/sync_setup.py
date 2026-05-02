"""Generate and validate reusable package sync scaffolding."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import cast

import json
import shlex
import tomllib

import yaml

from copybarista.config import load_config
from copybarista.errors import ConfigError


DEFAULT_EXCLUDES = (
    ".pytest_cache/**",
    "**/.pytest_cache/**",
    ".ruff_cache/**",
    "**/.ruff_cache/**",
    ".venv/**",
    "**/__pycache__/**",
    "**/*.pyc",
    "*.egg-info/**",
    "**/*.egg-info/**",
    "build/**",
    "dist/**",
    "htmlcov/**",
    ".coverage",
)
CONTROL_CHAR_BOUND = 32
DEFAULT_SYNC_USER_NAME = "copybarista"
DEFAULT_SYNC_USER_EMAIL = "copybarista@example.com"
DEFAULT_VALIDATION_PYTHON_VERSIONS = ("3.12",)


@dataclass(frozen=True, slots=True, kw_only=True)
class SyncSettings:
    """Reusable sync settings for one package.

    Attributes:
      package_name: Package identifier used for config and branch defaults.
      sync_label: Human-readable package name used in PR text.
      source_root: Source repository path exported as the public tree.
      public_repo: Public repository in ``owner/name`` form.
      source_repo: Source repository in ``owner/name`` form.
      copybarista_project_path: Source path containing the Copybarista project.
      smoke_import: Import target used to validate the exported package.
      type_check_targets: Paths passed to source-side basedpyright.
      forbidden_pr_text: Public PR title/body terms rejected before export.
      validation_python_versions: Python versions used by public package validation.
      validation_commands: Commands run by public package validation.
      sync_user_name: Commit author name for generated sync commits.
      sync_user_email: Commit author email for generated sync commits.
      export_branch_prefix: Optional source-to-public branch prefix.
      import_branch_prefix: Optional public-to-source branch prefix.

    """

    package_name: str
    sync_label: str
    source_root: str
    public_repo: str
    source_repo: str
    copybarista_project_path: str
    smoke_import: str
    type_check_targets: tuple[str, ...]
    forbidden_pr_text: tuple[str, ...]
    validation_python_versions: tuple[str, ...] = DEFAULT_VALIDATION_PYTHON_VERSIONS
    validation_commands: tuple[str, ...] = ()
    sync_user_name: str = DEFAULT_SYNC_USER_NAME
    sync_user_email: str = DEFAULT_SYNC_USER_EMAIL
    export_branch_prefix: str = ""
    import_branch_prefix: str = ""

    def __post_init__(self) -> None:
        """Fill derived validation defaults."""
        if self.validation_commands:
            return
        object.__setattr__(
            self,
            "validation_commands",
            _default_validation_commands(
                smoke_import=self.smoke_import,
                type_check_targets=self.type_check_targets,
            ),
        )

    @property
    def branch_slug(self) -> str:
        """Return the namespace used for generated sync branches."""
        return _branch_slug(self.package_name)

    @property
    def export_prefix(self) -> str:
        """Return the source-to-public branch prefix."""
        return self.export_branch_prefix or f"{self.branch_slug}/export/"

    @property
    def import_prefix(self) -> str:
        """Return the public-to-source branch prefix."""
        return self.import_branch_prefix or f"{self.branch_slug}/import/"


def load_sync_settings(path: Path) -> SyncSettings:
    """Load and validate package sync settings from TOML.

    Args:
      path: Path to ``copybarista.sync.toml``.

    Returns:
      settings: Validated sync settings.

    Raises:
      ConfigError: If the TOML is malformed or sync settings are invalid.

    """
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as err:
        raise ConfigError(f"Cannot read sync config {path}: {err}") from err
    sync = raw.get("sync", {})
    if not isinstance(sync, dict):
        raise ConfigError("copybarista.sync.toml must contain a [sync] table.")
    sync = cast("dict[str, object]", sync)
    settings = SyncSettings(
        package_name=_required_str(sync, "package_name"),
        sync_label=_required_str(sync, "sync_label"),
        source_root=_required_str(sync, "source_root"),
        public_repo=_required_str(sync, "public_repo"),
        source_repo=_required_str(sync, "source_repo"),
        copybarista_project_path=_required_str(sync, "copybarista_project_path"),
        smoke_import=_required_str(sync, "smoke_import"),
        type_check_targets=_required_str_tuple(sync, "type_check_targets"),
        forbidden_pr_text=_required_str_tuple(sync, "forbidden_pr_text"),
        validation_python_versions=_optional_str_tuple(
            sync,
            "validation_python_versions",
            default=DEFAULT_VALIDATION_PYTHON_VERSIONS,
        ),
        validation_commands=_optional_str_tuple(
            sync,
            "validation_commands",
            default=_default_validation_commands(
                smoke_import=_required_str(sync, "smoke_import"),
                type_check_targets=_required_str_tuple(sync, "type_check_targets"),
            ),
        ),
        sync_user_name=_optional_str(
            sync, "sync_user_name", default=DEFAULT_SYNC_USER_NAME
        ),
        sync_user_email=_optional_str(
            sync, "sync_user_email", default=DEFAULT_SYNC_USER_EMAIL
        ),
        export_branch_prefix=_optional_str(sync, "export_branch_prefix", default=""),
        import_branch_prefix=_optional_str(sync, "import_branch_prefix", default=""),
    )
    _validate_settings(settings)
    return settings


def write_sync_scaffold(
    *, root: Path, settings: SyncSettings, force: bool = False
) -> list[Path]:
    """Write reusable package sync scaffolding into a package root.

    Args:
      root: Package root where sync files are written.
      settings: Sync settings used to render generated files.
      force: Whether to overwrite existing sync files.

    Returns:
      paths: Paths written by the scaffold generator.

    Raises:
      ConfigError: If settings are invalid or files exist without ``force``.

    """
    _validate_settings(settings)
    files = {
        root / "copy.barista.toml": copy_barista_toml(settings),
        root / "copybarista.sync.toml": sync_toml(settings),
        root / ".github" / "workflows" / "sync-to-source.yml": import_workflow(
            settings
        ),
        root / ".github" / "workflows" / "package-validation.yml": (
            package_validation_workflow(settings)
        ),
    }
    existing = [str(path.relative_to(root)) for path in files if path.exists()]
    if existing and not force:
        raise ConfigError(
            "Refusing to overwrite existing sync files: " + ", ".join(existing)
        )
    written: list[Path] = []
    for path, content in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        written.append(path)
    return written


def check_sync_config(*, root: Path) -> None:
    """Validate that reusable sync scaffolding is present and consistent.

    Args:
      root: Package root containing generated sync files.

    Raises:
      ConfigError: If required files are missing or inconsistent.

    """
    missing = [
        str(path.relative_to(root))
        for path in _required_paths(root)
        if not path.exists()
    ]
    if missing:
        raise ConfigError("Missing sync files: " + ", ".join(missing))
    settings = load_sync_settings(root / "copybarista.sync.toml")
    config = load_config(root / "copy.barista.toml")
    workflow_text = (root / ".github" / "workflows" / "sync-to-source.yml").read_text(
        encoding="utf-8"
    )
    for path in DEFAULT_EXCLUDES:
        if path not in config.files.exclude:
            raise ConfigError(f"copy.barista.toml must exclude {path}.")
    _validate_import_workflow_yaml(workflow_text=workflow_text, settings=settings)
    _validate_package_validation_workflow_yaml(
        workflow_text=(
            root / ".github" / "workflows" / "package-validation.yml"
        ).read_text(encoding="utf-8"),
        settings=settings,
    )


def copy_barista_toml(settings: SyncSettings) -> str:
    """Return a baseline Copybarista export config.

    Args:
      settings: Sync settings used to render the export config.

    Returns:
      toml: Rendered ``copy.barista.toml`` contents.

    """
    excludes = "\n".join(f"  {_toml_str(path)}," for path in DEFAULT_EXCLUDES)
    return f"""[workflow]
name = {_toml_str(settings.package_name)}
mode = "squash"
source_root = {_toml_str(settings.source_root)}

[files]
include = ["**"]
exclude = [
{excludes}
]
"""


def sync_toml(settings: SyncSettings) -> str:
    """Return package sync metadata consumed by workflows and docs.

    Args:
      settings: Sync settings used to render sync metadata.

    Returns:
      toml: Rendered ``copybarista.sync.toml`` contents.

    Raises:
      ConfigError: If settings are invalid.

    """
    _validate_settings(settings)
    targets = "\n".join(
        f"  {_toml_str(target)}," for target in settings.type_check_targets
    )
    forbidden = "\n".join(
        f"  {_toml_str(term)}," for term in settings.forbidden_pr_text
    )
    python_versions = "\n".join(
        f"  {_toml_str(version)}," for version in settings.validation_python_versions
    )
    commands = "\n".join(
        f"  {_toml_str(command)}," for command in settings.validation_commands
    )
    return f"""[sync]
package_name = {_toml_str(settings.package_name)}
sync_label = {_toml_str(settings.sync_label)}
sync_user_name = {_toml_str(settings.sync_user_name)}
sync_user_email = {_toml_str(settings.sync_user_email)}
source_root = {_toml_str(settings.source_root)}
public_repo = {_toml_str(settings.public_repo)}
source_repo = {_toml_str(settings.source_repo)}
copybarista_project_path = {_toml_str(settings.copybarista_project_path)}
smoke_import = {_toml_str(settings.smoke_import)}
export_branch_prefix = {_toml_str(settings.export_prefix)}
import_branch_prefix = {_toml_str(settings.import_prefix)}
type_check_targets = [
{targets}
]
forbidden_pr_text = [
{forbidden}
]
validation_python_versions = [
{python_versions}
]
validation_commands = [
{commands}
]
"""


def package_validation_workflow(settings: SyncSettings) -> str:
    """Return the public package validation workflow.

    Args:
      settings: Sync settings used to render validation commands.

    Returns:
      yaml_text: Rendered GitHub Actions workflow contents.

    Raises:
      ConfigError: If settings are invalid.

    """
    _validate_settings(settings)
    commands = _shell_script(settings.validation_commands, indent="          ")
    python_versions = ", ".join(
        _yaml_str(version) for version in settings.validation_python_versions
    )
    return f"""name: Package validation

on:
  pull_request:
    branches: [main]
  push:
    branches: [main]

permissions:
  contents: read

jobs:
  validate:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python-version: [{python_versions}]
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: ${{{{ matrix.python-version }}}}
      - uses: astral-sh/setup-uv@v5
      - name: Validate package
        run: |
{commands}
"""


def export_workflow(settings: SyncSettings) -> str:
    """Return the source-repository export workflow for one package.

    Args:
      settings: Sync settings used to render the export workflow.

    Returns:
      yaml_text: Rendered GitHub Actions workflow contents.

    Raises:
      ConfigError: If settings are invalid.

    """
    _validate_settings(settings)
    type_targets = _shell_lines(
        [
            part
            for target in settings.type_check_targets
            for part in ("--type-check-target", target)
        ],
        indent="            ",
    )
    forbidden = ",".join(settings.forbidden_pr_text)
    return f"""name: Export public package

on:
  push:
    branches: [main]
    paths:
      - {_yaml_str(f"{settings.source_root}/**")}
      - {_yaml_str(f"{settings.copybarista_project_path}/scripts/sync_export_pr.py")}
      - {_yaml_str(f"{settings.copybarista_project_path}/scripts/sync_import_change.py")}
  workflow_dispatch:

permissions:
  contents: read

concurrency:
  group: copybarista-export-${{{{ github.workflow }}}}-${{{{ github.ref }}}}
  cancel-in-progress: false

jobs:
  export-pr:
    runs-on: ubuntu-latest
    env:
      GH_TOKEN: ${{{{ secrets.COPYBARISTA_SYNC_TOKEN }}}}
      TARGET_REPO: {_yaml_str(settings.public_repo)}
      BASE_BRANCH: main
      EXPORT_BRANCH: {_yaml_str(f"{settings.export_prefix}main")}
      SYNC_LABEL: {_yaml_str(settings.sync_label)}
      FORBIDDEN_PR_TEXT: {_yaml_str(forbidden)}
      COPYBARISTA_AUTO_MERGE: ${{{{ vars.COPYBARISTA_AUTO_MERGE || 'false' }}}}

    steps:
      - uses: actions/checkout@v4
        with:
          path: source
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - uses: astral-sh/setup-uv@v5
      - uses: actions/checkout@v4
        with:
          repository: ${{{{ env.TARGET_REPO }}}}
          token: ${{{{ secrets.COPYBARISTA_SYNC_TOKEN }}}}
          ref: ${{{{ env.BASE_BRANCH }}}}
          path: public
      - name: Open export PR
        run: |
          uv --quiet --project {_sh(f"source/{settings.copybarista_project_path}")} run python \\
            {_sh(f"source/{settings.copybarista_project_path}/scripts/sync_export_pr.py")} \\
            --source-dir source \\
            --project-path {_sh(settings.source_root)} \\
            --public-dir public \\
            --target-repo "$TARGET_REPO" \\
            --base-branch "$BASE_BRANCH" \\
            --branch "$EXPORT_BRANCH" \\
            --branch-prefix {_sh(settings.export_prefix)} \\
            --sync-label "$SYNC_LABEL" \\
            --pr-title {_sh(f"Update {settings.sync_label} export")} \\
            --pr-body {_sh(f"Updates the generated {settings.sync_label} public repository export.")} \\
            --forbidden-pr-text "$FORBIDDEN_PR_TEXT" \\
{type_targets} \\
            --smoke-import {_sh(settings.smoke_import)} \\
            --auto-merge="$COPYBARISTA_AUTO_MERGE"
"""


def import_workflow(settings: SyncSettings) -> str:
    """Return the reusable public repository import workflow.

    Args:
      settings: Sync settings used to render the import workflow.

    Returns:
      yaml_text: Rendered GitHub Actions workflow contents.

    Raises:
      ConfigError: If settings are invalid.

    """
    _validate_settings(settings)
    type_targets = _shell_lines(
        [
            part
            for target in settings.type_check_targets
            for part in ("--type-check-target", target)
        ],
        indent="            ",
    )
    return f"""name: Import public changes

on:
  pull_request:
    branches: [main]
  push:
    branches: [main]
  workflow_dispatch:
    inputs:
      public_base_ref:
        description: Public ref that represents the already-synced base tree.
        default: main
        required: true
      public_head_ref:
        description: Public ref to import. Defaults to this workflow run SHA.
        required: false

permissions:
  contents: read

concurrency:
  group: copybarista-import-${{{{ github.ref }}}}
  cancel-in-progress: false

jobs:
  import-change:
    name: Import public changes to source
    runs-on: ubuntu-latest
    env:
      DISPATCH_PUBLIC_BASE_REF: ${{{{ inputs.public_base_ref }}}}
      DISPATCH_PUBLIC_HEAD_REF: ${{{{ inputs.public_head_ref }}}}
      TARGET_REPO: {_yaml_str(settings.source_repo)}
      TARGET_PROJECT_PATH: {_yaml_str(settings.source_root)}
      COPYBARISTA_TOOL_PROJECT_PATH: {_yaml_str(settings.copybarista_project_path)}
      COPYBARISTA_IMPORT_BRANCH_PREFIX: {_yaml_str(settings.import_prefix)}
      COPYBARISTA_SYNC_LABEL: {_yaml_str(settings.sync_label)}
      COPYBARISTA_SYNC_USER_NAME: {_yaml_str(settings.sync_user_name)}
      COPYBARISTA_SYNC_USER_EMAIL: {_yaml_str(settings.sync_user_email)}

    if: |
      github.event_name == 'workflow_dispatch' ||
      (
        github.event_name == 'pull_request' &&
        github.event.pull_request.head.repo.full_name == github.repository &&
        !startsWith(github.event.pull_request.head.ref, {_github_expr_str(settings.export_prefix)})
      ) ||
      (
        github.event_name == 'push' &&
        github.event.before != '0000000000000000000000000000000000000000' &&
        github.event.head_commit.author.email != {_github_expr_str(settings.sync_user_email)} &&
        !contains(github.event.head_commit.message, {_github_expr_str(settings.export_prefix)}) &&
        !contains(github.event.head_commit.message, {_github_expr_str(f"{settings.sync_label} export branch:")})
      )

    steps:
      - name: Check token
        env:
          IMPORT_TOKEN: ${{{{ secrets.COPYBARISTA_IMPORT_TOKEN }}}}
        run: |
          if [ -z "$IMPORT_TOKEN" ]; then
            echo "::error::Set COPYBARISTA_IMPORT_TOKEN with repo and workflow scopes."
            exit 1
          fi

      - name: Set sync paths
        run: |
          echo "IMPORT_REPORT=$RUNNER_TEMP/copybarista-import-report.json" >> "$GITHUB_ENV"
          echo "TRUSTED_IMPORT_SCRIPT=$RUNNER_TEMP/sync_import_change.py" >> "$GITHUB_ENV"

      - id: refs
        name: Resolve public refs
        run: |
          if [ "${{{{ github.event_name }}}}" = "pull_request" ]; then
            base_ref="${{{{ github.event.pull_request.base.sha }}}}"
            head_ref="${{{{ github.event.pull_request.head.sha }}}}"
          elif [ "${{{{ github.event_name }}}}" = "workflow_dispatch" ]; then
            base_ref="$DISPATCH_PUBLIC_BASE_REF"
            head_ref="$DISPATCH_PUBLIC_HEAD_REF"
            if [ -z "$head_ref" ]; then
              head_ref="${{GITHUB_SHA}}"
            fi
          else
            base_ref="${{{{ github.event.before }}}}"
            head_ref="${{GITHUB_SHA}}"
          fi
          if [ -z "$base_ref" ] || [ "$base_ref" = "0000000000000000000000000000000000000000" ]; then
            echo "::error::Cannot resolve public base ref for import."
            exit 1
          fi
          for ref in "$base_ref" "$head_ref"; do
            if ! git check-ref-format --allow-onelevel "$ref"; then
              echo "::error::Invalid public ref: $ref"
              exit 1
            fi
          done
          echo "base_ref=$base_ref" >> "$GITHUB_OUTPUT"
          echo "head_ref=$head_ref" >> "$GITHUB_OUTPUT"

      - uses: actions/checkout@v4
        with:
          ref: ${{{{ steps.refs.outputs.base_ref }}}}
          path: public-base
      - uses: actions/checkout@v4
        with:
          ref: ${{{{ steps.refs.outputs.head_ref }}}}
          path: public-head
      - uses: actions/checkout@v4
        with:
          repository: ${{{{ env.TARGET_REPO }}}}
          token: ${{{{ secrets.COPYBARISTA_IMPORT_TOKEN }}}}
          ref: main
          path: target
          persist-credentials: false
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - uses: astral-sh/setup-uv@v5

      - name: Capture trusted import helper
        run: |
          install -m 600 \\
            "target/$COPYBARISTA_TOOL_PROJECT_PATH/scripts/sync_import_change.py" \\
            "$TRUSTED_IMPORT_SCRIPT"

      - name: Import public tree into target repository
        run: |
          uv --quiet --project "target/$COPYBARISTA_TOOL_PROJECT_PATH" run python \\
            "$TRUSTED_IMPORT_SCRIPT" \\
            --public-base public-base \\
            --public-head public-head \\
            --target-dir target \\
            --target-repo "$TARGET_REPO" \\
            --project-path "$TARGET_PROJECT_PATH" \\
            --copybarista-project-path "$COPYBARISTA_TOOL_PROJECT_PATH" \\
            --base-branch main \\
            --public-repo "$GITHUB_REPOSITORY" \\
            --public-sha "$GITHUB_SHA" \\
            --public-base-ref "${{{{ steps.refs.outputs.base_ref }}}}" \\
            --public-head-ref "${{{{ steps.refs.outputs.head_ref }}}}" \\
            --branch-prefix "$COPYBARISTA_IMPORT_BRANCH_PREFIX" \\
            --sync-label "$COPYBARISTA_SYNC_LABEL" \\
            --report "$IMPORT_REPORT" \\
{type_targets} \\
            --open-pr false

      - name: Open or update target import PR
        if: github.event_name != 'pull_request'
        env:
          GH_TOKEN: ${{{{ secrets.COPYBARISTA_IMPORT_TOKEN }}}}
        run: |
          python "$TRUSTED_IMPORT_SCRIPT" \\
            --target-dir target \\
            --target-repo "$TARGET_REPO" \\
            --project-path "$TARGET_PROJECT_PATH" \\
            --copybarista-project-path "$COPYBARISTA_TOOL_PROJECT_PATH" \\
            --base-branch main \\
            --public-repo "$GITHUB_REPOSITORY" \\
            --public-sha "$GITHUB_SHA" \\
            --public-base-ref "${{{{ steps.refs.outputs.base_ref }}}}" \\
            --public-head-ref "${{{{ steps.refs.outputs.head_ref }}}}" \\
            --branch-prefix "$COPYBARISTA_IMPORT_BRANCH_PREFIX" \\
            --sync-label "$COPYBARISTA_SYNC_LABEL" \\
            --report "$IMPORT_REPORT" \\
            --open-pr true \\
            --open-pr-only

      - name: Upload import report
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: copybarista-import-report
          path: ${{{{ env.IMPORT_REPORT }}}}
          if-no-files-found: ignore
"""


def _required_paths(root: Path) -> tuple[Path, ...]:
    return (
        root / "copy.barista.toml",
        root / "copybarista.sync.toml",
        root / ".github" / "workflows" / "sync-to-source.yml",
        root / ".github" / "workflows" / "package-validation.yml",
    )


def _validate_import_workflow_yaml(
    *, workflow_text: str, settings: SyncSettings
) -> None:
    try:
        parsed: object = yaml.safe_load(workflow_text)
    except yaml.YAMLError as err:
        raise ConfigError(f"Cannot read sync workflow: {err}") from err
    if not isinstance(parsed, dict):
        raise ConfigError("sync-to-source.yml must be a YAML mapping.")
    workflow = cast("dict[str, object]", parsed)
    jobs = _yaml_mapping(workflow.get("jobs"), "jobs")
    job = _yaml_mapping(jobs.get("import-change"), "jobs.import-change")
    env = _yaml_mapping(job.get("env"), "jobs.import-change.env")
    expected_env = {
        "TARGET_REPO": settings.source_repo,
        "TARGET_PROJECT_PATH": settings.source_root,
        "COPYBARISTA_TOOL_PROJECT_PATH": settings.copybarista_project_path,
        "COPYBARISTA_IMPORT_BRANCH_PREFIX": settings.import_prefix,
        "COPYBARISTA_SYNC_LABEL": settings.sync_label,
        "COPYBARISTA_SYNC_USER_NAME": settings.sync_user_name,
        "COPYBARISTA_SYNC_USER_EMAIL": settings.sync_user_email,
    }
    for key, expected in expected_env.items():
        if env.get(key) != expected:
            raise ConfigError(
                f"sync-to-source.yml jobs.import-change.env.{key} must be {expected}."
            )
    job_if = job.get("if", "")
    if not isinstance(job_if, str):
        raise ConfigError("sync-to-source.yml jobs.import-change.if must be a string.")
    for text in (
        "github.event.pull_request.head.repo.full_name == github.repository",
        f"!startsWith(github.event.pull_request.head.ref, {_github_expr_str(settings.export_prefix)})",
        f"github.event.head_commit.author.email != {_github_expr_str(settings.sync_user_email)}",
        f"!contains(github.event.head_commit.message, {_github_expr_str(settings.export_prefix)})",
        f"!contains(github.event.head_commit.message, {_github_expr_str(f'{settings.sync_label} export branch:')})",
    ):
        if text not in job_if:
            raise ConfigError(
                f"sync-to-source.yml jobs.import-change.if must contain {text}."
            )
    steps = _yaml_list(job.get("steps"), "jobs.import-change.steps")
    import_step = _workflow_step_run(steps, "Import public tree into target repository")
    pr_step = _workflow_step_run(steps, "Open or update target import PR")
    for run, texts in (
        (
            import_step,
            (
                '--target-repo "$TARGET_REPO"',
                '--project-path "$TARGET_PROJECT_PATH"',
                '--copybarista-project-path "$COPYBARISTA_TOOL_PROJECT_PATH"',
                '--branch-prefix "$COPYBARISTA_IMPORT_BRANCH_PREFIX"',
                '--sync-label "$COPYBARISTA_SYNC_LABEL"',
                "--open-pr false",
            ),
        ),
        (
            pr_step,
            (
                '--target-repo "$TARGET_REPO"',
                '--project-path "$TARGET_PROJECT_PATH"',
                '--copybarista-project-path "$COPYBARISTA_TOOL_PROJECT_PATH"',
                '--branch-prefix "$COPYBARISTA_IMPORT_BRANCH_PREFIX"',
                '--sync-label "$COPYBARISTA_SYNC_LABEL"',
                "--open-pr-only",
            ),
        ),
    ):
        for text in texts:
            if text not in run:
                raise ConfigError(f"sync-to-source.yml must reference {text}.")
    step_text = "\n".join(str(step) for step in steps)
    for text in ("sync_import_change.py", "GH_TOKEN"):
        if text not in step_text:
            raise ConfigError(f"sync-to-source.yml must reference {text}.")


def _validate_package_validation_workflow_yaml(
    *, workflow_text: str, settings: SyncSettings
) -> None:
    try:
        parsed: object = yaml.safe_load(workflow_text)
    except yaml.YAMLError as err:
        raise ConfigError(f"Cannot read package validation workflow: {err}") from err
    if not isinstance(parsed, dict):
        raise ConfigError("package-validation.yml must be a YAML mapping.")
    workflow = cast("dict[str, object]", parsed)
    jobs = _yaml_mapping(workflow.get("jobs"), "jobs")
    job = _yaml_mapping(jobs.get("validate"), "jobs.validate")
    matrix = _yaml_mapping(
        _yaml_mapping(job.get("strategy"), "jobs.validate.strategy").get("matrix"),
        "jobs.validate.strategy.matrix",
    )
    if matrix.get("python-version") != list(settings.validation_python_versions):
        raise ConfigError(
            "package-validation.yml jobs.validate.strategy.matrix.python-version "
            "must match validation_python_versions."
        )
    steps = _yaml_list(job.get("steps"), "jobs.validate.steps")
    run = _workflow_step_run(steps, "Validate package")
    commands = tuple(line.strip() for line in run.splitlines() if line.strip())
    if commands != settings.validation_commands:
        raise ConfigError(
            "package-validation.yml Validate package commands must match "
            "validation_commands."
        )


def _workflow_step_run(steps: list[object], name: str) -> str:
    for step in steps:
        step_map = _yaml_mapping(step, f"jobs.import-change.steps.{name}")
        if step_map.get("name") == name:
            run = step_map.get("run")
            if not isinstance(run, str):
                raise ConfigError(f"sync-to-source.yml step {name} must define run.")
            return run
    raise ConfigError(f"sync-to-source.yml must define step {name}.")


def _yaml_mapping(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ConfigError(f"sync-to-source.yml {name} must be a YAML mapping.")
    return cast("dict[str, object]", value)


def _yaml_list(value: object, name: str) -> list[object]:
    if not isinstance(value, list):
        raise ConfigError(f"sync-to-source.yml {name} must be a YAML list.")
    return cast("list[object]", value)


def _required_str(sync: dict[str, object], key: str) -> str:
    value = sync.get(key, "")
    if not isinstance(value, str) or not value:
        raise ConfigError(f"copybarista.sync.toml must define sync.{key}.")
    return value


def _optional_str(sync: dict[str, object], key: str, *, default: str) -> str:
    value = sync.get(key, default)
    if not isinstance(value, str):
        raise ConfigError(f"copybarista.sync.toml sync.{key} must be a string.")
    return value


def _optional_str_tuple(
    sync: dict[str, object], key: str, *, default: tuple[str, ...]
) -> tuple[str, ...]:
    if key not in sync:
        return default
    return _required_str_tuple(sync, key)


def _required_str_tuple(sync: dict[str, object], key: str) -> tuple[str, ...]:
    value = sync.get(key, [])
    if not isinstance(value, list):
        raise ConfigError(
            f"copybarista.sync.toml sync.{key} must be a list of strings."
        )
    value = cast("list[object]", value)
    if not all(isinstance(item, str) for item in value):
        raise ConfigError(
            f"copybarista.sync.toml sync.{key} must be a list of strings."
        )
    value = cast("list[str]", value)
    if not value and key == "type_check_targets":
        raise ConfigError(
            "copybarista.sync.toml sync.type_check_targets cannot be empty."
        )
    return tuple(value)


def _default_validation_commands(
    *, smoke_import: str, type_check_targets: tuple[str, ...]
) -> tuple[str, ...]:
    type_targets = " ".join(shlex.quote(target) for target in type_check_targets)
    return (
        "uv sync --all-groups",
        "uv run ruff check .",
        f"uv run basedpyright {type_targets}",
        "uv run pytest",
        f'uv run python -c "import {smoke_import}"',
        "uv build",
    )


def _validate_settings(settings: SyncSettings) -> None:
    for name, value in (
        ("package_name", settings.package_name),
        ("sync_label", settings.sync_label),
        ("sync_user_name", settings.sync_user_name),
        ("sync_user_email", settings.sync_user_email),
        ("source_root", settings.source_root),
        ("public_repo", settings.public_repo),
        ("source_repo", settings.source_repo),
        ("copybarista_project_path", settings.copybarista_project_path),
        ("smoke_import", settings.smoke_import),
    ):
        if not value.strip():
            raise ConfigError(f"sync.{name} cannot be empty.")
    if not settings.type_check_targets:
        raise ConfigError("sync.type_check_targets cannot be empty.")
    if not settings.validation_python_versions:
        raise ConfigError("sync.validation_python_versions cannot be empty.")
    if not settings.validation_commands:
        raise ConfigError("sync.validation_commands cannot be empty.")
    for command in settings.validation_commands:
        if "\n" in command or not command.strip():
            raise ConfigError("sync.validation_commands must contain shell commands.")
    _validate_branch_prefix(settings.export_prefix, name="export_branch_prefix")
    _validate_branch_prefix(settings.import_prefix, name="import_branch_prefix")


def _validate_branch_prefix(prefix: str, *, name: str) -> None:
    if not prefix or prefix in {"main", "master"} or prefix.startswith(("-", "/")):
        raise ConfigError(f"sync.{name} is not a safe generated branch prefix.")
    branch = f"{prefix}check"
    if not _valid_git_branch_name(branch):
        raise ConfigError(f"sync.{name} is not a safe generated branch prefix.")


def _valid_git_branch_name(branch: str) -> bool:
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


def _shell_lines(args: list[str], *, indent: str) -> str:
    pairs = [
        f"{indent}{_sh(args[idx])} {_sh(args[idx + 1])}"
        for idx in range(0, len(args), 2)
    ]
    return " \\\n".join(pairs)


def _shell_script(commands: tuple[str, ...], *, indent: str) -> str:
    return "\n".join(f"{indent}{command}" for command in commands)


def _toml_str(value: str) -> str:
    return json.dumps(value)


def _yaml_str(value: str) -> str:
    return json.dumps(value)


def _github_expr_str(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _sh(value: str) -> str:
    return shlex.quote(value)


def _branch_slug(value: str) -> str:
    slug = "".join(char if char.isalnum() else "-" for char in value.casefold()).strip(
        "-"
    )
    if not slug:
        raise ConfigError(
            "sync.package_name must contain at least one alphanumeric character."
        )
    return slug
