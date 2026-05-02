"""Configuration loading and validation."""

from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module
from pathlib import Path, PurePosixPath
from typing import Final, Literal, Protocol, cast

import json
import tomllib

from copybarista.errors import ConfigError, GlobError
from copybarista.globs import validate_pattern


TransformType = Literal["replace", "strip_block", "move"]
DEFAULT_GIT_BRANCH: Final = "main"


class _SkyConfigLoader(Protocol):
    """Deferred loader for `copy.bara.sky` configs.

    `copy_bara_sky` imports config models, so `load_config` imports it lazily to
    keep the native TOML path free of circular imports and unnecessary startup
    work.
    """

    def __call__(self, path: Path, *, workflow_name: str = "export") -> WorkflowConfig:
        """Load the requested workflow from a `copy.bara.sky` config file."""
        ...


@dataclass(frozen=True, slots=True, kw_only=True)
class FolderDestination:
    """Local folder destination config.

    The folder destination is the simplest publishing target and is also used
    by verification paths that compare transformed trees before opening PRs.
    """

    path: str = ""


@dataclass(frozen=True, slots=True, kw_only=True)
class GitDestination:
    """Git destination config.

    Git export writes the same staged tree as folder export, but publishes it
    as one branch update. Committer fields are optional so CI can either supply
    them in config or rely on repository Git config.
    """

    url: str = ""
    branch: str = DEFAULT_GIT_BRANCH
    committer_name: str = ""
    committer_email: str = ""


@dataclass(frozen=True, slots=True, kw_only=True)
class FileSelection:
    """Include and exclude patterns relative to the source root.

    Keeping file selection root-relative makes config reusable when the source
    checkout location changes between local runs and GitHub Actions.
    """

    include: tuple[str, ...]
    exclude: tuple[str, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class Transform:
    """A supported file transform.

    Transforms are intentionally closed and config-backed. That keeps export
    manifests auditable and lets import verification know which public changes
    can be reversed safely.
    """

    id: str
    type: TransformType
    path: str
    before: str = ""
    after: str = ""
    reverse_before: str = ""
    reverse_after: str = ""
    start: str = ""
    end: str = ""
    inclusive: bool = True
    required: bool = True
    destination: str = ""


@dataclass(frozen=True, slots=True, kw_only=True)
class WorkflowConfig:
    """Complete Copybarista workflow config.

    A workflow is the typed boundary between config parsing and execution.
    Native TOML and supported `copy.bara.sky` inputs both normalize to this
    model before staging, export, or import code runs.
    """

    name: str
    mode: str
    source_root: str
    files: FileSelection
    transforms: tuple[Transform, ...]
    folder: FolderDestination
    git: GitDestination


def load_config(path: Path, *, workflow_name: str = "export") -> WorkflowConfig:
    """Load a Copybarista TOML or supported `copy.bara.sky` config.

    Args:
      path: Config file path.
      workflow_name: Workflow to load when `path` is a `copy.bara.sky` file.

    Returns:
      config: Validated workflow config.

    Raises:
      ConfigError: If the config is malformed or unsupported.

    """
    if path.name == "copy.bara.sky" or path.suffix == ".sky":
        load_copy_bara_sky_config = cast(
            "_SkyConfigLoader",
            import_module("copybarista.copy_bara_sky").load_copy_bara_sky_config,
        )
        return load_copy_bara_sky_config(path, workflow_name=workflow_name)
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except OSError as err:
        raise ConfigError(f"Cannot read config: {path}") from err
    except tomllib.TOMLDecodeError as err:
        raise ConfigError(f"Invalid TOML in config: {path}") from err
    return parse_config(raw)


def parse_config(raw: dict[str, object]) -> WorkflowConfig:
    """Parse a raw TOML dictionary into a workflow config.

    Args:
      raw: Parsed TOML data.

    Returns:
      config: Validated workflow config.

    Raises:
      ConfigError: If the config is malformed or unsupported.

    """
    _check_keys(raw, {"workflow", "files", "destination", "transform"}, "config")
    workflow = _table(raw, "workflow")
    _check_keys(workflow, {"name", "mode", "source_root"}, "workflow")
    mode = _string(workflow, "mode", default="squash")
    if mode != "squash":
        raise ConfigError("Only workflow.mode = 'squash' is supported")
    source_root = _relative_path(_string(workflow, "source_root"), "source_root")
    name = _string(workflow, "name", default="default")

    files = _table(raw, "files")
    _check_keys(files, {"include", "exclude"}, "files")
    selection = FileSelection(
        include=tuple(
            _glob_list(_string_list(files, "include", default=("**",)), "files.include")
        ),
        exclude=tuple(
            _glob_list(_string_list(files, "exclude", default=()), "files.exclude")
        ),
    )

    transforms = tuple(
        _parse_transform(idx=idx, raw_transform=entry)
        for idx, entry in enumerate(_list(raw, "transform"), start=1)
    )

    destination = _table(raw, "destination", default={})
    _check_keys(destination, {"folder", "git"}, "destination")
    folder = _parse_folder(destination)
    git = _parse_git(destination)
    return WorkflowConfig(
        name=name,
        mode=mode,
        source_root=source_root,
        files=selection,
        transforms=transforms,
        folder=folder,
        git=git,
    )


def workflow_to_toml(config: WorkflowConfig) -> str:
    """Serialize a workflow config to Copybarista TOML.

    This is used by `copy.bara.sky` translation so users can inspect the native
    config that the execution engine receives.

    Args:
      config: Workflow config to serialize.

    Returns:
      toml: Native Copybarista TOML text.

    """
    lines = [
        "[workflow]",
        f"name = {_toml_string(config.name)}",
        f"mode = {_toml_string(config.mode)}",
        f"source_root = {_toml_string(config.source_root)}",
        "",
        "[destination.folder]",
        f"path = {_toml_string(config.folder.path)}",
        "",
        "[destination.git]",
        f"url = {_toml_string(config.git.url)}",
        f"branch = {_toml_string(config.git.branch)}",
    ]
    if config.git.committer_name:
        lines.append(f"committer_name = {_toml_string(config.git.committer_name)}")
    if config.git.committer_email:
        lines.append(f"committer_email = {_toml_string(config.git.committer_email)}")
    lines.extend(
        [
            "",
            "[files]",
            f"include = {_toml_list(config.files.include)}",
            f"exclude = {_toml_list(config.files.exclude)}",
        ]
    )
    for transform in config.transforms:
        lines.extend(["", "[[transform]]"])
        lines.append(f"type = {_toml_string(transform.type)}")
        lines.append(f"path = {_toml_string(transform.path)}")
        if transform.id:
            lines.append(f"id = {_toml_string(transform.id)}")
        if not transform.required:
            lines.append("required = false")
        if transform.type == "replace":
            lines.append(f"before = {_toml_string(transform.before)}")
            lines.append(f"after = {_toml_string(transform.after)}")
            if transform.reverse_before or transform.reverse_after:
                lines.append(
                    f"reverse_before = {_toml_string(transform.reverse_before)}"
                )
                lines.append(f"reverse_after = {_toml_string(transform.reverse_after)}")
        elif transform.type == "move":
            lines.append(f"destination = {_toml_string(transform.destination)}")
        else:
            lines.append(f"start = {_toml_string(transform.start)}")
            lines.append(f"end = {_toml_string(transform.end)}")
            lines.append(f"inclusive = {_toml_bool(transform.inclusive)}")
    return "\n".join(lines) + "\n"


def _parse_transform(idx: int, raw_transform: object) -> Transform:
    """Parse one `[[transform]]` table into a typed transform config."""
    if not isinstance(raw_transform, dict):
        raise ConfigError("Each transform entry must be a table")
    raw_transform = cast("dict[str, object]", raw_transform)
    _check_keys(
        raw_transform,
        {
            "id",
            "type",
            "path",
            "required",
            "before",
            "after",
            "reverse_before",
            "reverse_after",
            "start",
            "end",
            "inclusive",
            "destination",
        },
        f"transform[{idx}]",
    )
    ttype = _string(raw_transform, "type")
    if ttype not in ("replace", "strip_block", "move"):
        raise ConfigError(f"Unsupported transform type: {ttype}")
    path = _glob_path(_relative_path(_string(raw_transform, "path"), "transform.path"))
    transform_id = _string(raw_transform, "id", default=f"{idx}:{ttype}:{path}")
    required = _bool(raw_transform, "required", default=True)
    if ttype == "replace":
        _check_keys(
            raw_transform,
            {
                "id",
                "type",
                "path",
                "required",
                "before",
                "after",
                "reverse_before",
                "reverse_after",
            },
            f"transform[{idx}]",
        )
        before = _string(raw_transform, "before")
        if not before:
            raise ConfigError("replace before must be non-empty")
        reverse_before = _string(raw_transform, "reverse_before", default="")
        reverse_after = _string(raw_transform, "reverse_after", default="")
        if bool(reverse_before) != bool(reverse_after):
            raise ConfigError(
                "replace reverse_before and reverse_after must be set together"
            )
        if "reverse_before" in raw_transform and not reverse_before:
            raise ConfigError("replace reverse_before must be non-empty")
        return Transform(
            id=transform_id,
            type="replace",
            path=path,
            before=before,
            after=_string(raw_transform, "after"),
            reverse_before=reverse_before,
            reverse_after=reverse_after,
            required=required,
        )
    if ttype == "move":
        _check_keys(
            raw_transform,
            {"id", "type", "path", "destination", "required"},
            f"transform[{idx}]",
        )
        if _has_glob_syntax(path):
            raise ConfigError("move path must be an exact file path")
        dest = _string(raw_transform, "destination", default="")
        if not dest:
            raise ConfigError("move destination must be non-empty")
        dest = _relative_path(dest, "transform.destination")
        return Transform(
            id=transform_id,
            type="move",
            path=path,
            destination=dest,
            required=required,
        )
    _check_keys(
        raw_transform,
        {"id", "type", "path", "required", "start", "end", "inclusive"},
        f"transform[{idx}]",
    )
    start = _string(raw_transform, "start")
    end = _string(raw_transform, "end")
    if not start or not end:
        raise ConfigError("strip_block start and end markers must be non-empty")
    if _has_glob_syntax(path):
        raise ConfigError("strip_block path must be an exact file path")
    return Transform(
        id=transform_id,
        type="strip_block",
        path=path,
        start=start,
        end=end,
        inclusive=_bool(raw_transform, "inclusive", default=True),
        required=required,
    )


def _parse_folder(destination: dict[str, object]) -> FolderDestination:
    """Parse the optional `destination.folder` table."""
    raw = _dict_value(destination, "folder", default={})
    if raw is None:
        raise ConfigError("destination.folder must be a table")
    _check_keys(raw, {"path"}, "destination.folder")
    return FolderDestination(path=_string(raw, "path", default=""))


def _parse_git(destination: dict[str, object]) -> GitDestination:
    """Parse the optional `destination.git` table."""
    raw = _dict_value(destination, "git", default={})
    if raw is None:
        raise ConfigError("destination.git must be a table")
    _check_keys(
        raw,
        {"url", "branch", "committer_name", "committer_email"},
        "destination.git",
    )
    return GitDestination(
        url=_string(raw, "url", default=""),
        branch=_string(raw, "branch", default=DEFAULT_GIT_BRANCH),
        committer_name=_string(raw, "committer_name", default=""),
        committer_email=_string(raw, "committer_email", default=""),
    )


def _relative_path(value: str, field: str) -> str:
    """Validate and normalize a config path that must stay relative."""
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts:
        raise ConfigError(f"{field} must be a relative path without '..'")
    return value.strip("/")


def _glob_path(value: str) -> str:
    """Validate a transform path glob using Copybarista's supported subset."""
    try:
        return validate_pattern(value)
    except GlobError as err:
        raise ConfigError(str(err)) from err


def _has_glob_syntax(path: str) -> bool:
    """Return whether a path uses supported glob metacharacters."""
    return any(char in path for char in "*?[]{}\\")


def _glob_list(values: list[str], field: str) -> list[str]:
    """Validate file selection glob lists using Copybarista's subset."""
    try:
        return [validate_pattern(value) for value in values]
    except GlobError as err:
        raise ConfigError(f"{field}: {err}") from err


def _check_keys(data: dict[str, object], allowed: set[str], context: str) -> None:
    """Reject keys that would otherwise be silently ignored."""
    unknown = sorted(set(data) - allowed)
    if unknown:
        names = ", ".join(unknown)
        raise ConfigError(f"Unsupported key(s) in {context}: {names}")


def _table(
    data: dict[str, object],
    key: str,
    default: dict[str, object] | None = None,
) -> dict[str, object]:
    """Read a required or defaulted TOML table."""
    value = data.get(key, default)
    if not isinstance(value, dict):
        raise ConfigError(f"{key} must be a table")
    return cast("dict[str, object]", value)


def _list(data: dict[str, object], key: str) -> list[object]:
    """Read a TOML list as untyped items for later validation."""
    value = data.get(key, [])
    if not isinstance(value, list):
        raise ConfigError(f"{key} must be a list")
    return cast("list[object]", value)


def _dict_value(
    data: dict[str, object],
    key: str,
    default: dict[str, object],
) -> dict[str, object] | None:
    """Read an optional nested TOML table, returning None on type mismatch."""
    value = data.get(key, default)
    if not isinstance(value, dict):
        return None
    return cast("dict[str, object]", value)


def _string(
    data: dict[str, object],
    key: str,
    default: str | None = None,
) -> str:
    """Read a TOML string value."""
    value = data.get(key, default)
    if not isinstance(value, str):
        raise ConfigError(f"{key} must be a string")
    return value


def _string_list(
    data: dict[str, object],
    key: str,
    default: tuple[str, ...],
) -> list[str]:
    """Read a TOML list whose items must all be strings."""
    value = data.get(key, list(default))
    if not isinstance(value, list):
        raise ConfigError(f"{key} must be a list of strings")
    value = cast("list[object]", value)
    if not all(isinstance(item, str) for item in value):
        raise ConfigError(f"{key} must be a list of strings")
    return cast("list[str]", value)


def _bool(data: dict[str, object], key: str, default: bool) -> bool:
    """Read a TOML boolean value."""
    value = data.get(key, default)
    if not isinstance(value, bool):
        raise ConfigError(f"{key} must be a boolean")
    return value


def _toml_string(value: str) -> str:
    """Serialize one string as a TOML basic string."""
    return json.dumps(value)


def _toml_bool(value: bool) -> str:
    """Serialize one boolean as TOML."""
    return "true" if value else "false"


def _toml_list(values: tuple[str, ...]) -> str:
    """Serialize a string tuple as a single-line TOML list."""
    return "[" + ", ".join(_toml_string(value) for value in values) + "]"
