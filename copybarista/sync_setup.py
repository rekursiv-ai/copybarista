"""Generate and validate reusable package sync scaffolding."""

from __future__ import annotations

from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import cast

import json
import keyword
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
    "copy.bara.sky",
    "copy.barista.toml",
    "copybarista.sync.toml",
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
      sync_token_login: GitHub login that must own the sync token.
      export_branch_prefix: Optional source-to-public branch prefix.
      import_branch_prefix: Optional public-to-source branch prefix.
      pr_default_title: Default public PR title when commit metadata is absent.
      pr_default_body: Default public PR body when commit metadata is absent.
      require_pr_metadata: Whether exports fail when no metadata is replayed.
      pr_metadata_source: Source of PR metadata. Currently commit_messages only.
      replay_bootstrap_base: Optional source revision for one-time migrations.
      publish_source_rev: Whether public markers may include raw source SHAs.
      refresh_public_lockfile: Whether export runs generate a public uv.lock.

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
    sync_token_login: str = ""
    export_branch_prefix: str = ""
    import_branch_prefix: str = ""
    pr_default_title: str = ""
    pr_default_body: str = ""
    require_pr_metadata: bool = False
    pr_metadata_source: str = "commit_messages"
    replay_bootstrap_base: str = ""
    publish_source_rev: bool = False
    refresh_public_lockfile: bool = False

    def __post_init__(self) -> None:
        """Fill derived validation defaults."""
        if not self.validation_commands:
            object.__setattr__(
                self,
                "validation_commands",
                _default_validation_commands(
                    smoke_import=self.smoke_import,
                    type_check_targets=self.type_check_targets,
                ),
            )
        if not self.pr_default_title:
            object.__setattr__(
                self,
                "pr_default_title",
                f"Update {self.sync_label} export",
            )
        if not self.pr_default_body:
            object.__setattr__(
                self,
                "pr_default_body",
                f"Updates the generated {self.sync_label} public repository export.",
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
    pull_request = raw.get("pull_request", {})
    if not isinstance(pull_request, dict):
        raise ConfigError("copybarista.sync.toml [pull_request] must be a table.")
    pull_request = cast("dict[str, object]", pull_request)
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
        refresh_public_lockfile=_optional_sync_bool(
            sync,
            "refresh_public_lockfile",
            default=False,
        ),
        sync_user_name=_optional_str(
            sync, "sync_user_name", default=DEFAULT_SYNC_USER_NAME
        ),
        sync_user_email=_optional_str(
            sync, "sync_user_email", default=DEFAULT_SYNC_USER_EMAIL
        ),
        sync_token_login=_optional_str(sync, "sync_token_login", default=""),
        export_branch_prefix=_optional_str(sync, "export_branch_prefix", default=""),
        import_branch_prefix=_optional_str(sync, "import_branch_prefix", default=""),
        pr_default_title=_optional_pr_str(
            pull_request,
            "default_title",
            default="",
        ),
        pr_default_body=_optional_pr_str(
            pull_request,
            "default_body",
            default="",
        ),
        require_pr_metadata=_optional_pr_bool(
            pull_request,
            "require_pr_metadata",
            default=False,
        ),
        pr_metadata_source=_optional_pr_str(
            pull_request,
            "metadata_source",
            default="commit_messages",
        ),
        replay_bootstrap_base=_optional_pr_str(
            pull_request,
            "replay_bootstrap_base",
            default="",
        ),
        publish_source_rev=_optional_pr_bool(
            pull_request,
            "publish_source_rev",
            default=False,
        ),
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
    if not config.leak_check.forbidden_path:
        raise ConfigError("copy.barista.toml must define forbidden path leak checks.")
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
    return _render_template(
        "copy.barista.toml.tmpl",
        {
            "PACKAGE_NAME": _toml_str(settings.package_name),
            "SOURCE_ROOT": _toml_str(settings.source_root),
            "EXCLUDES": excludes,
        },
    )


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
    return _render_template(
        "copybarista.sync.toml.tmpl",
        {
            "PACKAGE_NAME": _toml_str(settings.package_name),
            "SYNC_LABEL": _toml_str(settings.sync_label),
            "SYNC_USER_NAME": _toml_str(settings.sync_user_name),
            "SYNC_USER_EMAIL": _toml_str(settings.sync_user_email),
            "SYNC_TOKEN_LOGIN": _toml_str(settings.sync_token_login),
            "SOURCE_ROOT": _toml_str(settings.source_root),
            "PUBLIC_REPO": _toml_str(settings.public_repo),
            "SOURCE_REPO": _toml_str(settings.source_repo),
            "COPYBARISTA_PROJECT_PATH": _toml_str(settings.copybarista_project_path),
            "SMOKE_IMPORT": _toml_str(settings.smoke_import),
            "EXPORT_BRANCH_PREFIX": _toml_str(settings.export_prefix),
            "IMPORT_BRANCH_PREFIX": _toml_str(settings.import_prefix),
            "PR_DEFAULT_TITLE": _toml_str(settings.pr_default_title),
            "PR_DEFAULT_BODY": _toml_str(settings.pr_default_body),
            "REQUIRE_PR_METADATA": _toml_bool(settings.require_pr_metadata),
            "PR_METADATA_SOURCE": _toml_str(settings.pr_metadata_source),
            "REPLAY_BOOTSTRAP_BASE": _toml_str(settings.replay_bootstrap_base),
            "PUBLISH_SOURCE_REV": _toml_bool(settings.publish_source_rev),
            "REFRESH_PUBLIC_LOCKFILE": _toml_bool(settings.refresh_public_lockfile),
            "TYPE_CHECK_TARGETS": targets,
            "FORBIDDEN_PR_TEXT": forbidden,
            "VALIDATION_PYTHON_VERSIONS": python_versions,
            "VALIDATION_COMMANDS": commands,
        },
    )


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
    python_setup = (
        f"          python-version: {_yaml_str(settings.validation_python_versions[0])}"
        if len(settings.validation_python_versions) == 1
        else "          python-version: ${{ matrix.python-version }}"
    )
    strategy = ""
    if len(settings.validation_python_versions) > 1:
        python_versions = ", ".join(
            _yaml_str(version) for version in settings.validation_python_versions
        )
        strategy = "\n".join(
            (
                "    strategy:",
                "      matrix:",
                f"        python-version: [{python_versions}]",
                "",
            )
        )
    return _render_template(
        "package-validation.yml.tmpl",
        {
            "STRATEGY": strategy,
            "PYTHON_SETUP": python_setup,
            "COMMANDS": commands,
        },
    )


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
    pr_replay_flags = _shell_args(
        _pr_replay_args(settings),
        indent="            ",
    )
    forbidden = ",".join(settings.forbidden_pr_text)
    project_path = f"source/{settings.copybarista_project_path}"
    script_path = f"{project_path}/scripts/sync_export_pr.py"
    return _render_template(
        "source-to-public.yml.tmpl",
        {
            "SOURCE_ROOT_PATH": _yaml_str(f"{settings.source_root}/**"),
            "EXPORT_SCRIPT_PATH": _yaml_str(
                f"{settings.copybarista_project_path}/scripts/sync_export_pr.py"
            ),
            "IMPORT_SCRIPT_PATH": _yaml_str(
                f"{settings.copybarista_project_path}/scripts/sync_import_change.py"
            ),
            "PUBLIC_REPO": _yaml_str(settings.public_repo),
            "EXPORT_BRANCH": _yaml_str(f"{settings.export_prefix}main"),
            "SYNC_LABEL": _yaml_str(settings.sync_label),
            "FORBIDDEN_PR_TEXT": _yaml_str(forbidden),
            "SYNC_USER_NAME": _yaml_str(settings.sync_user_name),
            "SYNC_USER_EMAIL": _yaml_str(settings.sync_user_email),
            "SYNC_TOKEN_LOGIN": _yaml_str(settings.sync_token_login),
            "COPYBARISTA_PROJECT_PATH": _sh(project_path),
            "SYNC_EXPORT_SCRIPT": _sh(script_path),
            "SOURCE_ROOT": _sh(settings.source_root),
            "EXPORT_BRANCH_PREFIX": _sh(settings.export_prefix),
            "PR_DEFAULT_TITLE": _sh(settings.pr_default_title),
            "PR_DEFAULT_BODY": _sh(settings.pr_default_body),
            "PR_REPLAY_FLAGS": pr_replay_flags,
            "TYPE_TARGETS": type_targets,
            "SMOKE_IMPORT": _sh(settings.smoke_import),
        },
    )


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
    return _render_template(
        "public-to-source.yml.tmpl",
        {
            "IMPORT_BRANCH_PREFIX": _yaml_str(settings.import_prefix),
            "SYNC_LABEL": _yaml_str(settings.sync_label),
            "SYNC_USER_NAME": _yaml_str(settings.sync_user_name),
            "SYNC_USER_EMAIL": _yaml_str(settings.sync_user_email),
            "EXPORT_PREFIX_EXPR": _github_expr_str(settings.export_prefix),
            "SYNC_USER_EMAIL_EXPR": _github_expr_str(settings.sync_user_email),
            "EXPORT_BRANCH_MESSAGE_EXPR": _github_expr_str(
                f"{settings.sync_label} export branch:"
            ),
            "TYPE_TARGETS": type_targets,
        },
    )


def _render_template(name: str, values: dict[str, str]) -> str:
    """Render one package-data template with explicit token replacement."""
    text = (
        resources.files("copybarista.templates")
        .joinpath(name)
        .read_text(encoding="utf-8")
    )
    for key, value in values.items():
        text = text.replace(f"@@{key}@@", value)
    if "@@" in text:
        raise AssertionError(f"Unrendered token in template {name}.")
    return text


def _required_paths(root: Path) -> tuple[Path, ...]:
    """Return generated public sync files that must stay present."""
    return (
        root / "copy.barista.toml",
        root / "copybarista.sync.toml",
        root / ".github" / "workflows" / "sync-to-source.yml",
        root / ".github" / "workflows" / "package-validation.yml",
    )


def _validate_import_workflow_yaml(
    *, workflow_text: str, settings: SyncSettings
) -> None:
    """Validate the exported public-to-source workflow matches settings."""
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
        "TARGET_REPO": "${{ vars.COPYBARISTA_SOURCE_REPO }}",
        "TARGET_PROJECT_PATH": "${{ vars.COPYBARISTA_TARGET_PROJECT_PATH }}",
        "COPYBARISTA_TOOL_PROJECT_PATH": "${{ vars.COPYBARISTA_TOOL_PROJECT_PATH }}",
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
    """Validate package CI matches generated sync validation commands."""
    try:
        parsed: object = yaml.safe_load(workflow_text)
    except yaml.YAMLError as err:
        raise ConfigError(f"Cannot read package validation workflow: {err}") from err
    if not isinstance(parsed, dict):
        raise ConfigError("package-validation.yml must be a YAML mapping.")
    workflow = cast("dict[str, object]", parsed)
    jobs = _yaml_mapping(workflow.get("jobs"), "jobs")
    job = _yaml_mapping(jobs.get("validate"), "jobs.validate")
    if job.get("name") != "Lint, type-check, test, and build":
        raise ConfigError(
            "package-validation.yml jobs.validate.name must be "
            "'Lint, type-check, test, and build'."
        )
    if len(settings.validation_python_versions) == 1:
        steps = _yaml_list(job.get("steps"), "jobs.validate.steps")
        setup_step = _workflow_uses_step(steps, "actions/setup-python@v5")
        with_config = _yaml_mapping(
            setup_step.get("with"), "jobs.validate.steps.setup-python.with"
        )
        if with_config.get("python-version") != settings.validation_python_versions[0]:
            raise ConfigError(
                "package-validation.yml setup-python python-version must match "
                "validation_python_versions."
            )
    else:
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
    """Return the shell script for a named workflow step."""
    for step in steps:
        step_map = _yaml_mapping(step, f"jobs.import-change.steps.{name}")
        if step_map.get("name") == name:
            run = step_map.get("run")
            if not isinstance(run, str):
                raise ConfigError(f"sync-to-source.yml step {name} must define run.")
            return run
    raise ConfigError(f"sync-to-source.yml must define step {name}.")


def _workflow_uses_step(steps: list[object], uses: str) -> dict[str, object]:
    """Return the first workflow step using an action."""
    for step in steps:
        step_map = _yaml_mapping(step, f"workflow step {uses}")
        if step_map.get("uses") == uses:
            return step_map
    raise ConfigError(f"Workflow must define step using {uses}.")


def _yaml_mapping(value: object, name: str) -> dict[str, object]:
    """Return `value` as a YAML mapping or raise a config error."""
    if not isinstance(value, dict):
        raise ConfigError(f"sync-to-source.yml {name} must be a YAML mapping.")
    return cast("dict[str, object]", value)


def _yaml_list(value: object, name: str) -> list[object]:
    """Return `value` as a YAML list or raise a config error."""
    if not isinstance(value, list):
        raise ConfigError(f"sync-to-source.yml {name} must be a YAML list.")
    return cast("list[object]", value)


def _required_str(sync: dict[str, object], key: str) -> str:
    """Read a required non-empty string from the sync table."""
    value = sync.get(key, "")
    if not isinstance(value, str) or not value:
        raise ConfigError(f"copybarista.sync.toml must define sync.{key}.")
    return value


def _optional_str(sync: dict[str, object], key: str, *, default: str) -> str:
    """Read an optional string from the sync table."""
    value = sync.get(key, default)
    if not isinstance(value, str):
        raise ConfigError(f"copybarista.sync.toml sync.{key} must be a string.")
    return value


def _optional_sync_bool(sync: dict[str, object], key: str, *, default: bool) -> bool:
    """Read an optional boolean from the sync table."""
    value = sync.get(key, default)
    if not isinstance(value, bool):
        raise ConfigError(f"copybarista.sync.toml sync.{key} must be a boolean.")
    return value


def _optional_pr_str(pull_request: dict[str, object], key: str, *, default: str) -> str:
    """Read an optional string from the pull_request table."""
    value = pull_request.get(key, default)
    if not isinstance(value, str):
        raise ConfigError(
            f"copybarista.sync.toml [pull_request].{key} must be a string."
        )
    return value


def _optional_pr_bool(
    pull_request: dict[str, object], key: str, *, default: bool
) -> bool:
    """Read an optional boolean from the pull_request table."""
    value = pull_request.get(key, default)
    if not isinstance(value, bool):
        raise ConfigError(
            f"copybarista.sync.toml [pull_request].{key} must be a boolean."
        )
    return value


def _optional_str_tuple(
    sync: dict[str, object], key: str, *, default: tuple[str, ...]
) -> tuple[str, ...]:
    """Read an optional list of strings from the sync table."""
    if key not in sync:
        return default
    return _required_str_tuple(sync, key)


def _required_str_tuple(sync: dict[str, object], key: str) -> tuple[str, ...]:
    """Read a required list of strings from the sync table."""
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
    """Build the default exported-package validation command list."""
    type_targets = " ".join(shlex.quote(target) for target in type_check_targets)
    return (
        "uv sync --all-groups",
        "uv run ruff check --no-fix --no-cache .",
        "uv run ruff format --check --no-cache .",
        "uv run codespell .",
        "uv run ty check",
        f"uv run basedpyright {type_targets}",
        "uv run pytest",
        f'uv run python -c "import {smoke_import}"',
        "uv build",
    )


def _validate_settings(settings: SyncSettings) -> None:
    """Validate sync settings before writing workflow files."""
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
    _validate_python_module_name(settings.smoke_import, name="smoke_import")
    if not settings.validation_python_versions:
        raise ConfigError("sync.validation_python_versions cannot be empty.")
    if not settings.validation_commands:
        raise ConfigError("sync.validation_commands cannot be empty.")
    for command in settings.validation_commands:
        if "\n" in command or not command.strip():
            raise ConfigError("sync.validation_commands must contain shell commands.")
    if not settings.pr_default_title.strip():
        raise ConfigError(
            "copybarista.sync.toml [pull_request].default_title cannot be empty."
        )
    if not settings.pr_default_body.strip():
        raise ConfigError(
            "copybarista.sync.toml [pull_request].default_body cannot be empty."
        )
    if settings.pr_metadata_source != "commit_messages":
        raise ConfigError(
            "copybarista.sync.toml [pull_request].metadata_source must be "
            "'commit_messages'."
        )
    if any(ord(char) < CONTROL_CHAR_BOUND for char in settings.replay_bootstrap_base):
        raise ConfigError(
            "copybarista.sync.toml [pull_request].replay_bootstrap_base must not "
            "contain control characters."
        )
    _validate_branch_prefix(settings.export_prefix, name="export_branch_prefix")
    _validate_branch_prefix(settings.import_prefix, name="import_branch_prefix")


def _validate_python_module_name(value: str, *, name: str) -> None:
    """Reject module names that cannot be safely embedded in smoke commands."""
    parts = value.split(".")
    if not all(part.isidentifier() and not keyword.iskeyword(part) for part in parts):
        raise ConfigError(f"sync.{name} must be a dotted Python module name.")


def _validate_branch_prefix(prefix: str, *, name: str) -> None:
    """Reject branch prefixes that can overwrite protected or arbitrary refs."""
    if not prefix or prefix in {"main", "master"} or prefix.startswith(("-", "/")):
        raise ConfigError(f"sync.{name} is not a safe generated branch prefix.")
    branch = f"{prefix}check"
    if not _valid_git_branch_name(branch):
        raise ConfigError(f"sync.{name} is not a safe generated branch prefix.")


def _valid_git_branch_name(branch: str) -> bool:
    """Return whether `branch` is safe for generated sync pushes."""
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
    """Format CLI flag pairs as a continued shell command."""
    pairs = [
        f"{indent}{_sh(args[idx])} {_sh(args[idx + 1])}"
        for idx in range(0, len(args), 2)
    ]
    return " \\\n".join(pairs)


def _shell_args(args: list[str], *, indent: str) -> str:
    """Format CLI args as continued shell command lines."""
    return " \\\n".join(f"{indent}{_sh(arg)}" for arg in args)


def _shell_script(commands: tuple[str, ...], *, indent: str) -> str:
    """Indent validation commands for generated workflow YAML."""
    return "\n".join(f"{indent}{command}" for command in commands)


def _pr_replay_args(settings: SyncSettings) -> list[str]:
    """Return source export script flags for PR replay settings."""
    args = [
        "--pr-scope",
        settings.package_name,
        "--pr-default-title",
        settings.pr_default_title,
        "--pr-default-body",
        settings.pr_default_body,
    ]
    if settings.require_pr_metadata:
        args.append("--require-pr-metadata")
    if settings.replay_bootstrap_base:
        args.extend(("--replay-bootstrap-base", settings.replay_bootstrap_base))
    if settings.publish_source_rev:
        args.append("--publish-source-rev")
    if settings.refresh_public_lockfile:
        args.append("--refresh-public-lockfile")
    return args


def _toml_str(value: str) -> str:
    """Return a TOML-compatible quoted string."""
    return json.dumps(value)


def _toml_bool(value: bool) -> str:
    """Return a TOML-compatible boolean."""
    return "true" if value else "false"


def _yaml_str(value: str) -> str:
    """Return a YAML-compatible quoted string."""
    return json.dumps(value)


def _github_expr_str(value: str) -> str:
    """Return a single-quoted GitHub expression string literal."""
    return "'" + value.replace("'", "''") + "'"


def _sh(value: str) -> str:
    """Return a shell-quoted argument."""
    return shlex.quote(value)


def _branch_slug(value: str) -> str:
    """Return the generated branch slug for a package name."""
    slug = "".join(char if char.isalnum() else "-" for char in value.casefold()).strip(
        "-"
    )
    if not slug:
        raise ConfigError(
            "sync.package_name must contain at least one alphanumeric character."
        )
    return slug
