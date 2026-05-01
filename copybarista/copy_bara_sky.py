"""Compatibility module for `copy.bara.sky` configs.

This module is not a Starlark interpreter. It statically parses the small
`copy.bara.sky` config shape that Copybarista can execute and rejects unsupported
constructs with explicit configuration errors.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final, cast

import ast
import textwrap

from copybarista.config import (
    DEFAULT_GIT_BRANCH,
    Transform,
    WorkflowConfig,
    parse_config,
    workflow_to_toml,
)
from copybarista.errors import ConfigError


SUPPORTED_WORKFLOW_KEYS: Final = {
    "name",
    "origin",
    "destination",
    "origin_files",
    "destination_files",
    "authoring",
    "mode",
    "transformations",
}
CORE_MOVE_ARG_COUNT: Final = 2
STRIP_MARKER_LINE_COUNT: Final = 2


@dataclass(frozen=True, slots=True, kw_only=True)
class GlobSpec:
    """A parsed `glob(...)` include/exclude expression."""

    include: tuple[str, ...]
    exclude: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class DestinationSpec:
    """A parsed destination expression."""

    kind: str
    url: str = ""
    branch: str = DEFAULT_GIT_BRANCH


@dataclass(frozen=True, slots=True, kw_only=True)
class AuthorSpec:
    """A parsed authoring expression."""

    name: str
    email: str


@dataclass(frozen=True, slots=True, kw_only=True)
class MoveSpec:
    """A parsed `core.move` transform."""

    source: str
    destination: str


def _transform_to_raw(transform: Transform) -> dict[str, object]:
    """Serialize a typed transform into the raw config parser shape."""
    raw: dict[str, object] = {
        "type": transform.type,
        "path": transform.path,
    }
    if transform.id:
        raw["id"] = transform.id
    if not transform.required:
        raw["required"] = False
    if transform.type == "replace":
        raw["before"] = transform.before
        raw["after"] = transform.after
        if transform.reverse_before or transform.reverse_after:
            raw["reverse_before"] = transform.reverse_before
            raw["reverse_after"] = transform.reverse_after
    else:
        raw["start"] = transform.start
        raw["end"] = transform.end
        raw["inclusive"] = transform.inclusive
    return raw


def translate_copy_bara_sky_to_toml(
    path: Path, *, workflow_name: str = "export"
) -> str:
    """Translate a supported `copy.bara.sky` workflow into Copybarista TOML.

    Args:
      path: `copy.bara.sky` file path.
      workflow_name: Workflow function or assignment to translate.

    Returns:
      toml: Native Copybarista TOML for the translated workflow.

    Raises:
      ConfigError: If the workflow uses unsupported syntax or options.

    """
    workflow = _load_translated_workflow(path, workflow_name=workflow_name)
    return workflow_to_toml(parse_config(workflow.to_raw_config()))


def load_copy_bara_sky_config(
    path: Path, *, workflow_name: str = "export"
) -> WorkflowConfig:
    """Load a supported `copy.bara.sky` workflow through the TOML config path.

    Args:
      path: `copy.bara.sky` file path.
      workflow_name: Workflow function or assignment to load.

    Returns:
      config: Validated Copybarista workflow config.

    Raises:
      ConfigError: If the workflow cannot be translated to the supported model.

    """
    return parse_config(
        _load_translated_workflow(path, workflow_name=workflow_name).to_raw_config()
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class TranslatedWorkflow:
    """A workflow translated from `copy.bara.sky` syntax."""

    name: str
    mode: str
    source_root: str
    include: tuple[str, ...]
    exclude: tuple[str, ...]
    transforms: tuple[Transform, ...]
    folder_path: str = ""
    git_url: str = ""
    git_branch: str = DEFAULT_GIT_BRANCH
    git_committer_name: str = ""
    git_committer_email: str = ""

    def to_raw_config(self) -> dict[str, object]:
        """Return a raw config dictionary accepted by `parse_config`."""
        raw: dict[str, object] = {
            "workflow": {
                "name": self.name,
                "mode": self.mode,
                "source_root": self.source_root,
            },
            "files": {
                "include": list(self.include),
                "exclude": list(self.exclude),
            },
            "destination": {
                "folder": {"path": self.folder_path},
                "git": {
                    "url": self.git_url,
                    "branch": self.git_branch,
                    "committer_name": self.git_committer_name,
                    "committer_email": self.git_committer_email,
                },
            },
            "transform": [
                _transform_to_raw(transform) for transform in self.transforms
            ],
        }
        return raw


def _load_translated_workflow(
    path: Path, *, workflow_name: str = "export"
) -> TranslatedWorkflow:
    """Load a supported `copy.bara.sky` workflow as a translated workflow."""
    try:
        source = textwrap.dedent(path.read_text(encoding="utf-8"))
        module = ast.parse(source, filename=str(path))
    except SyntaxError as err:
        raise ConfigError(f"Unsupported copy.bara.sky syntax in {path}: {err}") from err
    except OSError as err:
        raise ConfigError(f"Cannot read config: {path}") from err

    parser = _CopyBaraSkyParser(module=module)
    workflows = parser.parse_workflows()
    for workflow in workflows:
        if workflow.name == workflow_name:
            return workflow
    names = ", ".join(workflow.name for workflow in workflows) or "<none>"
    raise ConfigError(f"Workflow {workflow_name!r} not found in copy.bara.sky: {names}")


class _CopyBaraSkyParser:
    """Static evaluator for supported `copy.bara.sky` expressions."""

    def __init__(self, *, module: ast.Module) -> None:
        """Initialize parser state for one config module."""
        self.module = module
        self.env: dict[str, object] = {}
        self.functions: dict[str, ast.FunctionDef] = {}

    def parse_workflows(self) -> list[TranslatedWorkflow]:
        """Return all supported workflows defined by the config."""
        workflows: list[TranslatedWorkflow] = []
        for statement in self.module.body:
            if isinstance(statement, ast.Assign):
                self._assign(statement, self.env)
            elif isinstance(statement, ast.FunctionDef):
                self.functions[statement.name] = statement
            elif isinstance(statement, ast.Expr) and isinstance(
                statement.value, ast.Call
            ):
                workflows.extend(self._evaluate_top_level_call(statement.value))
            elif isinstance(statement, ast.Expr) and isinstance(
                statement.value, ast.Constant
            ):
                continue
            else:
                raise ConfigError(
                    f"Unsupported top-level copy.bara.sky statement: "
                    f"{type(statement).__name__}"
                )
        return workflows

    def _evaluate_top_level_call(self, call: ast.Call) -> list[TranslatedWorkflow]:
        """Evaluate a top-level workflow or helper call."""
        if _call_name(call) == "core.workflow":
            return [self._workflow_from_call(call, self.env)]
        if isinstance(call.func, ast.Name) and call.func.id in self.functions:
            return self._evaluate_helper_call(call, self.env)
        raise ConfigError(
            f"Unsupported top-level call in copy.bara.sky: {_call_name(call)}"
        )

    def _evaluate_helper_call(
        self, call: ast.Call, env: dict[str, object]
    ) -> list[TranslatedWorkflow]:
        """Evaluate a simple helper function that emits workflow calls."""
        if not isinstance(call.func, ast.Name):
            raise ConfigError("Unsupported helper call")
        function = self.functions[call.func.id]
        local_env = dict(env)
        params = [arg.arg for arg in function.args.args]
        if len(call.args) > len(params):
            raise ConfigError(f"Too many positional args for helper {function.name}")
        for name, value in zip(params, call.args, strict=False):
            local_env[name] = self._eval(value, env)
        for keyword in call.keywords:
            if keyword.arg is None:
                raise ConfigError("Unsupported **kwargs in copy.bara.sky helper call")
            local_env[keyword.arg] = self._eval(keyword.value, env)
        missing = [name for name in params if name not in local_env]
        if missing:
            raise ConfigError(
                f"Missing helper args for {function.name}: {', '.join(missing)}"
            )

        workflows: list[TranslatedWorkflow] = []
        for statement in function.body:
            if isinstance(statement, ast.Expr) and isinstance(
                statement.value, ast.Call
            ):
                if _call_name(statement.value) != "core.workflow":
                    raise ConfigError(
                        f"Unsupported helper call in {function.name}: "
                        f"{_call_name(statement.value)}"
                    )
                workflows.append(self._workflow_from_call(statement.value, local_env))
            else:
                raise ConfigError(
                    f"Unsupported helper body in {function.name}: "
                    f"{type(statement).__name__}"
                )
        return workflows

    def _assign(self, statement: ast.Assign, env: dict[str, object]) -> None:
        """Evaluate one supported assignment into an environment."""
        if len(statement.targets) != 1 or not isinstance(
            statement.targets[0], ast.Name
        ):
            raise ConfigError("Only simple NAME = value assignments are supported")
        env[statement.targets[0].id] = self._eval(statement.value, env)

    def _workflow_from_call(
        self, call: ast.Call, env: dict[str, object]
    ) -> TranslatedWorkflow:
        """Translate a supported `core.workflow` call."""
        if call.args:
            raise ConfigError("core.workflow positional args are not supported")
        kwargs = self._kwargs(call, env, allowed=SUPPORTED_WORKFLOW_KEYS)
        mode = _require_string(kwargs.get("mode", "SQUASH"), "core.workflow.mode")
        if mode != "SQUASH":
            raise ConfigError("Only mode = 'SQUASH' is supported")
        if _call_name_from_value(kwargs.get("origin")) != "folder.origin":
            raise ConfigError("Only folder.origin() is supported")
        destination = kwargs.get("destination")
        if not isinstance(destination, DestinationSpec):
            raise ConfigError(
                "Only folder.destination() or git.destination() is supported"
            )
        origin_files = kwargs.get("origin_files", GlobSpec(include=("**",)))
        if not isinstance(origin_files, GlobSpec):
            raise ConfigError("core.workflow.origin_files must be glob(...)")
        destination_files = kwargs.get("destination_files")
        if destination_files is not None:
            _validate_destination_files(destination_files)
        transformations = _object_list(
            kwargs.get("transformations", []), "core.workflow.transformations"
        )
        move, transforms = self._parse_transformations(transformations)
        if "authoring" not in kwargs:
            raise ConfigError("core.workflow.authoring is required")
        source_root = move.source if move is not None else ""
        if move is not None and move.destination:
            raise ConfigError("Only core.move(SOURCE, '') is supported")

        include = _strip_prefixes(origin_files.include, source_root)
        exclude = _strip_prefixes(origin_files.exclude, source_root)
        git_url, git_branch = _git_destination_fields(destination)
        git_committer_name, git_committer_email = _git_author_fields(
            authoring=kwargs.get("authoring"),
            destination=destination,
        )
        workflow_name = _require_string(
            kwargs.get("name", "export"), "core.workflow.name"
        )
        return TranslatedWorkflow(
            name=workflow_name,
            mode="squash",
            source_root=source_root,
            include=include,
            exclude=exclude,
            transforms=tuple(transforms),
            git_url=git_url,
            git_branch=git_branch,
            git_committer_name=git_committer_name,
            git_committer_email=git_committer_email,
        )

    def _parse_transformations(
        self, transformations: list[object]
    ) -> tuple[MoveSpec | None, list[Transform]]:
        """Parse supported workflow transforms."""
        move: MoveSpec | None = None
        parsed: list[Transform] = []
        for idx, item in enumerate(transformations, start=1):
            if isinstance(item, MoveSpec):
                if move is not None:
                    raise ConfigError("Only one core.move transform is supported")
                move = item
                continue
            if isinstance(item, Transform):
                # Transformation order is observable in failure messages.
                # Keeping the ordinal in generated IDs makes translated configs
                # easier to map back to the source workflow.
                parsed.append(replace(item, id=f"{idx}:{item.type}:{item.path}"))
                continue
            if isinstance(item, list):
                item_transforms: list[Transform] = []
                for transform in cast("list[object]", item):
                    if not isinstance(transform, Transform):
                        raise ConfigError(f"Unsupported transformation: {item!r}")
                    item_transforms.append(transform)
                parsed.extend(
                    replace(
                        transform,
                        id=f"{idx}.{subidx}:{transform.type}:{transform.path}",
                    )
                    for subidx, transform in enumerate(item_transforms, start=1)
                )
                continue
            raise ConfigError(f"Unsupported transformation: {item!r}")
        return move, parsed

    def _eval(self, node: ast.AST, env: dict[str, object]) -> object:
        """Evaluate one supported expression node."""
        if isinstance(node, ast.Constant):
            if isinstance(node.value, str | bool | int | float) or node.value is None:
                return node.value
            raise ConfigError(f"Unsupported literal in copy.bara.sky: {node.value!r}")
        if isinstance(node, ast.Name):
            if node.id in env:
                return env[node.id]
            raise ConfigError(f"Unknown name in copy.bara.sky: {node.id}")
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left = self._eval(node.left, env)
            right = self._eval(node.right, env)
            if isinstance(left, str) and isinstance(right, str):
                return left + right
            raise ConfigError("Only string concatenation is supported")
        if isinstance(node, ast.List):
            return [self._eval(item, env) for item in node.elts]
        if isinstance(node, ast.Call):
            return self._eval_call(node, env)
        raise ConfigError(
            f"Unsupported copy.bara.sky expression: {type(node).__name__}"
        )

    def _eval_call(self, call: ast.Call, env: dict[str, object]) -> object:
        """Evaluate one supported function call expression."""
        name = _call_name(call)
        if name == "glob":
            result = self._glob_from_call(call, env)
        elif name == "folder.origin":
            result = _folder_origin_from_call(call)
        elif name == "folder.destination":
            result = _folder_destination_from_call(call)
        elif name == "git.destination":
            result = self._git_destination_from_call(call, env)
        elif name == "authoring.pass_thru":
            result = self._pass_thru_author_from_call(call, env)
        elif name == "core.transform":
            result = self._transform_group_from_call(call, env)
        elif name == "core.reverse":
            result = self._reverse_group_from_call(call, env)
        elif name == "core.move":
            result = self._move_from_call(call, env)
        elif name == "core.replace":
            result = self._replace_from_call(call, env)
        else:
            raise ConfigError(f"Unsupported copy.bara.sky call: {name}")
        return result

    def _glob_from_call(self, call: ast.Call, env: dict[str, object]) -> GlobSpec:
        """Evaluate a supported `glob(...)` call."""
        kwargs = self._kwargs(call, env, allowed={"exclude"})
        if len(call.args) != 1:
            raise ConfigError("glob(...) requires one include list")
        return GlobSpec(
            include=_string_tuple(self._eval(call.args[0], env), "glob include"),
            exclude=_string_tuple(kwargs.get("exclude", []), "glob exclude"),
        )

    def _git_destination_from_call(
        self, call: ast.Call, env: dict[str, object]
    ) -> DestinationSpec:
        """Evaluate a supported `git.destination(...)` call."""
        kwargs = self._kwargs(call, env, allowed={"url", "fetch", "push"})
        if len(call.args) > 1:
            raise ConfigError("git.destination accepts at most one positional url")
        positional_url = (
            _require_string(self._eval(call.args[0], env), "git.destination.url")
            if call.args
            else ""
        )
        if positional_url and "url" in kwargs:
            raise ConfigError("git.destination url specified twice")
        return DestinationSpec(
            kind="git",
            url=_require_string(
                kwargs.get("url", positional_url), "git.destination.url"
            ),
            branch=_require_string(
                kwargs.get("push", kwargs.get("fetch", DEFAULT_GIT_BRANCH)),
                "git.destination.push",
            ),
        )

    def _move_from_call(self, call: ast.Call, env: dict[str, object]) -> MoveSpec:
        """Evaluate a supported `core.move(...)` call."""
        if len(call.args) != CORE_MOVE_ARG_COUNT:
            raise ConfigError("core.move requires source and destination args")
        return MoveSpec(
            source=_require_string(self._eval(call.args[0], env), "core.move source"),
            destination=_require_string(
                self._eval(call.args[1], env), "core.move destination"
            ),
        )

    def _pass_thru_author_from_call(
        self, call: ast.Call, env: dict[str, object]
    ) -> AuthorSpec:
        """Evaluate a supported `authoring.pass_thru(...)` call."""
        kwargs = self._kwargs(call, env, allowed={"default"})
        if len(call.args) > 1:
            raise ConfigError("authoring.pass_thru accepts one author string")
        if call.args and "default" in kwargs:
            raise ConfigError("authoring.pass_thru author specified twice")
        if not call.args and "default" not in kwargs:
            raise ConfigError("authoring.pass_thru requires an author string")
        value = self._eval(call.args[0], env) if call.args else kwargs["default"]
        return _author_from_string(
            _require_string(
                value,
                "authoring.pass_thru author",
            )
        )

    def _transform_group_from_call(
        self, call: ast.Call, env: dict[str, object]
    ) -> list[object]:
        """Evaluate a supported `core.transform([...])` wrapper."""
        kwargs = self._kwargs(call, env, allowed={"transformations", "reversal"})
        if len(call.args) > 1:
            raise ConfigError("core.transform accepts one transformation list")
        if call.args and "transformations" in kwargs:
            raise ConfigError("core.transform transformations specified twice")
        transformations = (
            self._eval(call.args[0], env)
            if call.args
            else kwargs.get("transformations")
        )
        if transformations is None:
            raise ConfigError("core.transform requires one transformation list")
        forward = _object_list(transformations, "core.transform transformations")
        if "reversal" not in kwargs:
            return forward
        reversal = _object_list(kwargs["reversal"], "core.transform reversal")
        return _transforms_with_explicit_reversal(forward=forward, reversal=reversal)

    def _reverse_group_from_call(
        self, call: ast.Call, env: dict[str, object]
    ) -> list[object]:
        """Evaluate supported reversible transform groups."""
        if len(call.args) != 1 or call.keywords:
            raise ConfigError("core.reverse requires one transformation list")
        items = _object_list(
            self._eval(call.args[0], env),
            "core.reverse transformations",
        )
        flat_items: list[object] = []
        for item in items:
            if isinstance(item, list):
                flat_items.extend(cast("list[object]", item))
            else:
                flat_items.append(item)
        reversed_items: list[object] = []
        for item in reversed(flat_items):
            if isinstance(item, Transform):
                if item.type != "replace":
                    raise ConfigError("core.reverse only supports core.replace")
                reversed_items.append(
                    replace(item, before=item.after, after=item.before)
                )
                continue
            raise ConfigError("core.reverse only supports reversible transforms")
        return reversed_items

    def _replace_from_call(
        self, call: ast.Call, env: dict[str, object]
    ) -> Transform | list[Transform]:
        """Translate a supported `core.replace` call."""
        kwargs = self._kwargs(
            call,
            env,
            allowed={"before", "after", "paths", "multiline"},
        )
        if call.args:
            raise ConfigError("core.replace positional args are not supported")
        before = _require_string(kwargs.get("before", ""), "core.replace.before")
        after = _require_string(kwargs.get("after", ""), "core.replace.after")
        paths = _replace_paths(kwargs.get("paths"))
        multiline = kwargs.get("multiline", False)
        if multiline:
            if after:
                raise ConfigError("multiline core.replace is only supported for strip")
            if len(paths.include) != 1 or paths.exclude:
                raise ConfigError("multiline core.replace supports exactly one path")
            start, end = _strip_markers(before)
            return Transform(
                id="",
                type="strip_block",
                path=paths.include[0],
                start=start,
                end=end,
            )
        if "\n" in before:
            raise ConfigError(
                "core.replace before containing newlines requires multiline = True"
            )
        if not before:
            raise ConfigError("core.replace.before must be non-empty")
        if paths.exclude:
            raise ConfigError("core.replace paths must not have exclude patterns")
        return [
            Transform(
                id="",
                type="replace",
                path=path,
                before=before,
                after=after,
            )
            for path in paths.include
        ]

    def _kwargs(
        self, call: ast.Call, env: dict[str, object], *, allowed: set[str]
    ) -> dict[str, object]:
        """Evaluate supported keyword args and reject unknown kwargs."""
        values: dict[str, object] = {}
        for keyword in call.keywords:
            if keyword.arg is None:
                raise ConfigError(f"Unsupported **kwargs in {_call_name(call)}")
            if keyword.arg not in allowed:
                raise ConfigError(
                    f"Unsupported argument for {_call_name(call)}: {keyword.arg}"
                )
            values[keyword.arg] = self._eval(keyword.value, env)
        return values


def _call_name(call: ast.Call) -> str:
    """Return a dotted name for a call expression."""
    return _call_name_from_expr(call.func)


def _folder_origin_from_call(call: ast.Call) -> str:
    """Evaluate a supported `folder.origin()` call."""
    if call.args or call.keywords:
        raise ConfigError("folder.origin() options are not supported")
    return "folder.origin"


def _folder_destination_from_call(call: ast.Call) -> DestinationSpec:
    """Evaluate a supported `folder.destination()` call."""
    if call.args or call.keywords:
        raise ConfigError("folder.destination() options are not supported")
    return DestinationSpec(kind="folder")


def _call_name_from_expr(node: ast.AST) -> str:
    """Return a dotted name for a supported call target expression."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _call_name_from_expr(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return "<unsupported>"


def _call_name_from_value(value: object) -> str:
    """Return a call sentinel value if the expression evaluated to one."""
    return value if isinstance(value, str) else ""


def _git_destination_fields(destination: DestinationSpec) -> tuple[str, str]:
    """Return Git destination fields or folder-export defaults."""
    if destination.kind != "git":
        return "", DEFAULT_GIT_BRANCH
    return destination.url, destination.branch


def _replace_paths(value: object) -> GlobSpec:
    """Return replace paths from a glob or literal path list."""
    if isinstance(value, GlobSpec):
        return value
    return GlobSpec(include=_string_tuple(value, "core.replace.paths"))


def _transforms_with_explicit_reversal(
    *, forward: list[object], reversal: list[object]
) -> list[object]:
    """Attach an explicit literal replacement reversal to one forward replace."""
    forward = _flatten_transform_items(forward)
    reversal = _flatten_transform_items(reversal)
    if len(forward) != 1 or len(reversal) != 1:
        raise ConfigError("core.transform explicit reversal supports one transform")
    forward_transform = forward[0]
    reverse_transform = reversal[0]
    if not isinstance(forward_transform, Transform) or not isinstance(
        reverse_transform, Transform
    ):
        raise ConfigError("core.transform explicit reversal supports core.replace")
    if forward_transform.type != "replace" or reverse_transform.type != "replace":
        raise ConfigError("core.transform explicit reversal supports core.replace")
    return [
        replace(
            forward_transform,
            reverse_before=reverse_transform.before,
            reverse_after=reverse_transform.after,
        )
    ]


def _flatten_transform_items(items: list[object]) -> list[object]:
    """Flatten transform lists produced by path-expanded calls."""
    flattened: list[object] = []
    for item in items:
        if isinstance(item, list):
            flattened.extend(cast("list[object]", item))
        else:
            flattened.append(item)
    return flattened


def _git_author_fields(
    *, authoring: object, destination: DestinationSpec
) -> tuple[str, str]:
    """Return author fields only for Git destinations."""
    if authoring is None:
        return "", ""
    if not isinstance(authoring, AuthorSpec):
        raise ConfigError("core.workflow.authoring must be authoring.pass_thru")
    if destination.kind != "git":
        return "", ""
    return authoring.name, authoring.email


def _string_tuple(value: object, field: str) -> tuple[str, ...]:
    """Validate and freeze a string list expression."""
    if not isinstance(value, list):
        raise ConfigError(f"{field} must be a list of strings")
    strings: list[str] = []
    for item in cast("list[object]", value):
        if not isinstance(item, str):
            raise ConfigError(f"{field} must be a list of strings")
        strings.append(item)
    return tuple(strings)


def _object_list(value: object, field: str) -> list[object]:
    """Validate a list expression."""
    if not isinstance(value, list):
        raise ConfigError(f"{field} must be a list")
    return list(cast("list[object]", value))


def _require_string(value: object, field: str) -> str:
    """Return a string value or raise a field-specific config error."""
    if not isinstance(value, str):
        raise ConfigError(f"{field} must be a string")
    return value


def _author_from_string(value: str) -> AuthorSpec:
    """Parse a `Name <email>` author string."""
    name, separator, email_part = value.rpartition(" <")
    if not separator or not email_part.endswith(">"):
        raise ConfigError("authoring.pass_thru author must be 'Name <email>'")
    email = email_part[:-1]
    if not name or not email or any(char in email for char in "<> \t\r\n"):
        raise ConfigError("authoring.pass_thru author must be 'Name <email>'")
    return AuthorSpec(name=name, email=email)


def _validate_destination_files(value: object) -> None:
    """Validate the only supported destination file selection."""
    if not isinstance(value, GlobSpec):
        raise ConfigError("core.workflow.destination_files must be glob(...)")
    if value.include != ("**",) or value.exclude:
        raise ConfigError(
            'Only destination_files = glob(["**"]) is supported because '
            "Copybarista rewrites the whole destination tree"
        )


def _strip_prefixes(patterns: tuple[str, ...], source_root: str) -> tuple[str, ...]:
    """Strip a moved source root from origin file globs."""
    if not source_root:
        return patterns
    prefix = f"{source_root.rstrip('/')}/"
    stripped: list[str] = []
    for pattern in patterns:
        if pattern == source_root:
            stripped.append("**")
        elif pattern.startswith(prefix):
            stripped.append(pattern.removeprefix(prefix))
        else:
            raise ConfigError(
                f"origin_files pattern is outside core.move source root: {pattern}"
            )
    return tuple(stripped)


def _strip_markers(before: str) -> tuple[str, str]:
    """Infer strip block markers from a multiline replacement string."""
    lines = [line for line in before.splitlines() if line.strip()]
    if len(lines) < STRIP_MARKER_LINE_COUNT:
        raise ConfigError("multiline strip replacement needs start and end markers")
    return lines[0], lines[-1]
