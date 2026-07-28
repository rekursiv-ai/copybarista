"""Compatibility module for `copy.bara.sky` configs.

This module is not a Starlark interpreter. It statically parses the small
`copy.bara.sky` config shape that Copybarista can execute and rejects unsupported
constructs with explicit configuration errors.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import cast

import ast
import textwrap

from copybarista.config import (
    DEFAULT_GIT_BRANCH,
    FileCopy,
    Transform,
    WorkflowConfig,
    parse_config,
    workflow_to_toml,
)
from copybarista.errors import ConfigError


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


@dataclass(frozen=True, slots=True, kw_only=True)
class CopySpec:
    """A parsed `core.copy` transform.

    ``core.copy(SOURCE, DEST, paths=glob([...]))`` copies the ``paths``-matched
    files from ``SOURCE`` to ``DEST`` (leaving the originals in place). Maps to a
    ``[[files.copy]]`` with an ``include`` glob.
    """

    source: str
    destination: str
    include: tuple[str, ...] = ("**",)
    relocate: bool = False


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
    elif transform.type == "move":
        raw["destination"] = transform.destination
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
    destination_prefix: str = ""
    destination_prefix_exclude: tuple[str, ...] = ()
    copies: tuple[FileCopy, ...] = ()
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
                "destination_prefix": self.destination_prefix,
                "destination_prefix_exclude": list(self.destination_prefix_exclude),
                "copy": [
                    {
                        "source": copy.source,
                        "destination": copy.destination,
                        "include": list(copy.include),
                        "exclude": list(copy.exclude),
                        "use_default_python_excludes": (
                            copy.use_default_python_excludes
                        ),
                    }
                    for copy in self.copies
                ],
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
        kwargs = self._kwargs(
            call,
            env,
            allowed={
                "name",
                "origin",
                "destination",
                "origin_files",
                "destination_files",
                "authoring",
                "mode",
                "transformations",
            },
        )
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
        (
            source_root_move,
            transforms,
            prefix_excludes,
            subtree_copies,
            sweep_excludes,
        ) = self._parse_transformations(
            transformations, origin_roots=_origin_move_roots(origin_files.include)
        )
        if "authoring" not in kwargs:
            raise ConfigError("core.workflow.authoring is required")
        source_root = source_root_move.source if source_root_move is not None else ""
        destination_prefix = (
            source_root_move.destination if source_root_move is not None else ""
        )
        _reject_partial_flatten_of_whole_tree(
            source_root_move=source_root_move,
            origin_include=origin_files.include,
        )

        include, origin_copies = _strip_prefixes_and_file_copies(
            origin_files.include, source_root
        )
        copies = (*origin_copies, *subtree_copies)
        if source_root and not include:
            raise ConfigError(
                f"origin_files pattern is outside core.move source root: {source_root}"
            )
        exclude = _strip_prefixes(origin_files.exclude, source_root)
        # A subtree shipped verbatim to root by its own copy must not ALSO be
        # swept in under the prefix by the main selection: exclude it. Copybara
        # gets this for free because its move relocates the files out of the
        # selection; our copy reads from source directly, so exclude explicitly.
        # .export-style subtree copies + relocate (glob-move) copies must not
        # ALSO be swept in under the prefix; exclude them from the main selection.
        exclude = (*exclude, *sweep_excludes)
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
            destination_prefix=destination_prefix,
            destination_prefix_exclude=prefix_excludes,
            include=include,
            exclude=exclude,
            transforms=tuple(transforms),
            copies=tuple(copies),
            git_url=git_url,
            git_branch=git_branch,
            git_committer_name=git_committer_name,
            git_committer_email=git_committer_email,
        )

    def _parse_transformations(
        self,
        transformations: list[object],
        *,
        origin_roots: frozenset[str] = frozenset(),
    ) -> tuple[
        MoveSpec | None,
        list[Transform],
        tuple[str, ...],
        tuple[FileCopy, ...],
        tuple[str, ...],
    ]:
        """Parse supported workflow transforms.

        A ``core.move(SOURCE, DEST)`` whose SOURCE is the origin-files root is the
        source-root move: DEST empty flattens the package to the public root, and
        a non-empty DEST ships it under that prefix (mapped to
        ``destination_prefix``). A move OUT of that prefix back to the root
        (``core.move("<prefix>/x", "x")``) keeps ``x`` at the public root and
        maps to a ``destination_prefix_exclude`` entry. A move of an in-package
        subtree to the root (``core.move("<root>/.export", "")``) maps to a
        ``[[files.copy]]`` to ``.`` (a verbatim-ship staging dir). Any other move
        is a per-file move transform.
        """
        # Pass 1: locate the source-root move so its prefix/root is known before
        # classifying the other moves, which may appear before or after it.
        source_root_move = self._source_root_move(transformations, origin_roots)
        prefix = source_root_move.destination if source_root_move is not None else ""
        root = source_root_move.source if source_root_move is not None else ""
        parsed: list[Transform] = []
        prefix_excludes: list[str] = []
        subtree_copies: list[FileCopy] = []
        sweep_excludes: list[str] = []
        for idx, item in enumerate(transformations, start=1):
            if isinstance(item, MoveSpec):
                if item is source_root_move:
                    continue
                back = _prefix_back_move(item, prefix)
                if back is not None:
                    # A move can carry a file or a directory; the translator
                    # cannot tell which. Emit both the bare path (matches a file)
                    # and ``<path>/**`` (matches a directory's children) so the
                    # kept-at-root selection covers either, matching the .toml's
                    # per-file/per-tree destination_prefix_exclude entries.
                    prefix_excludes.append(back)
                    prefix_excludes.append(f"{back}/**")
                    continue
                if _is_subtree_to_root_move(item, root):
                    # Copybara's move only relocates SELECTED files, so
                    # origin_files excludes (rebuildable caches) never ride along.
                    # Our copy reads from source directly, so carry the same cache
                    # excludes to keep the verbatim ship free of junk.
                    subtree_copies.append(
                        FileCopy(
                            source=item.source,
                            destination=".",
                            exclude=(
                                ".ruff_cache/**",
                                "**/.ruff_cache/**",
                                "__pycache__/**",
                                "**/__pycache__/**",
                                "*.pyc",
                                "**/*.pyc",
                                ".pytest_cache/**",
                                "**/.pytest_cache/**",
                            ),
                        )
                    )
                    _add_sweep_exclude(sweep_excludes, item.source, root, ("**",))
                    continue
                parsed.append(
                    Transform(
                        id=f"{idx}:move:{item.source}",
                        type="move",
                        path=item.source,
                        destination=item.destination,
                    )
                )
                continue
            if isinstance(item, CopySpec):
                subtree_copies.append(
                    FileCopy(
                        source=item.source,
                        destination=item.destination,
                        include=item.include,
                    )
                )
                if item.relocate:
                    _add_sweep_exclude(sweep_excludes, item.source, root, item.include)
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
        return (
            source_root_move,
            parsed,
            tuple(prefix_excludes),
            tuple(subtree_copies),
            tuple(sweep_excludes),
        )

    def _source_root_move(
        self, transformations: list[object], origin_roots: frozenset[str]
    ) -> MoveSpec | None:
        """Return the single source-root move, or None; reject duplicates.

        The source-root move relocates the whole package: its source is an
        origin-files root. A move with an empty destination whose source is a
        SUBPATH of a root (e.g. ``<root>/.export`` -> "") is a subtree-to-root
        move, not the source-root move, and is excluded here.
        """
        found: MoveSpec | None = None
        for item in transformations:
            if not isinstance(item, MoveSpec):
                continue
            is_root = item.source in origin_roots or (
                not item.destination
                and not _is_subpath_of_any(item.source, origin_roots)
            )
            if is_root:
                if found is not None:
                    raise ConfigError(
                        "Only one source-root core.move transform is supported"
                    )
                found = item
        return found

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
        elif name == "core.copy":
            result = self._copy_from_call(call, env)
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

    def _move_from_call(
        self, call: ast.Call, env: dict[str, object]
    ) -> MoveSpec | CopySpec:
        """Evaluate a supported `core.move(...)` call.

        A plain ``core.move(source, destination)`` is a whole-path move. A
        glob-scoped ``core.move(source, destination, paths=glob([...]))``
        RELOCATES only the matched files, mapping to a copy-with-include plus a
        sweep-exclude (CopySpec with ``relocate=True``).
        """
        if len(call.args) != 2:
            raise ConfigError("core.move requires source and destination args")
        source = _require_string(self._eval(call.args[0], env), "core.move source")
        destination = _require_string(
            self._eval(call.args[1], env), "core.move destination"
        )
        kwargs = self._kwargs(call, env, allowed={"paths"})
        paths = kwargs.get("paths")
        if paths is None:
            return MoveSpec(source=source, destination=destination)
        if not isinstance(paths, GlobSpec):
            raise ConfigError("core.move paths must be glob(...)")
        if paths.exclude:
            raise ConfigError("core.move paths must not have exclude patterns")
        return CopySpec(
            source=source,
            destination=destination,
            include=paths.include,
            relocate=True,
        )

    def _copy_from_call(self, call: ast.Call, env: dict[str, object]) -> CopySpec:
        """Evaluate a supported `core.copy(source, destination, paths=...)` call."""
        if len(call.args) != 2:
            raise ConfigError("core.copy requires source and destination args")
        kwargs = self._kwargs(call, env, allowed={"paths"})
        paths = kwargs.get("paths")
        include: tuple[str, ...] = ("**",)
        if paths is not None:
            if not isinstance(paths, GlobSpec):
                raise ConfigError("core.copy paths must be glob(...)")
            if paths.exclude:
                raise ConfigError("core.copy paths must not have exclude patterns")
            include = paths.include
        return CopySpec(
            source=_require_string(self._eval(call.args[0], env), "core.copy source"),
            destination=_require_string(
                self._eval(call.args[1], env), "core.copy destination"
            ),
            include=include,
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
            if not before:
                raise ConfigError("core.replace.before must be non-empty")
            if paths.exclude:
                raise ConfigError("core.replace paths must not have exclude patterns")
            if not after and _looks_like_strip_block(before):
                if len(paths.include) != 1:
                    raise ConfigError("multiline strip replacement supports one path")
                start, end = _strip_markers(before)
                return Transform(
                    id="",
                    type="strip_block",
                    path=paths.include[0],
                    start=start,
                    end=end,
                    inclusive=True,
                )
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


def _prefix_back_move(move: MoveSpec, prefix: str) -> str | None:
    """Return the prefix-relative path a back-move keeps at root, or None.

    A ``core.move("<prefix>/x", "x")`` moves ``x`` out of the destination prefix
    back to the public root -- i.e. ``x`` is a ``destination_prefix_exclude``
    entry. Recognized only when a prefix exists and the destination is exactly
    the source with the ``<prefix>/`` stripped.
    """
    if not prefix:
        return None
    prefix_slash = f"{prefix}/"
    if not move.source.startswith(prefix_slash):
        return None
    if move.destination == move.source.removeprefix(prefix_slash):
        return move.destination
    return None


def _is_subpath_of_any(path: str, roots: frozenset[str]) -> bool:
    """Return whether ``path`` is strictly under any of ``roots``."""
    return any(path.startswith(f"{root}/") for root in roots)


def _is_subtree_to_root_move(move: MoveSpec, root: str) -> bool:
    """Return whether a move ships an in-package subtree to the export root.

    ``core.move("<root>/.export", "")`` moves a verbatim-ship staging dir that
    lives inside the source root to the public root; it maps to a copy to ``.``.
    """
    return bool(root) and not move.destination and move.source.startswith(f"{root}/")


def _add_sweep_exclude(
    excludes: list[str],
    copy_source: str,
    source_root: str,
    include: tuple[str, ...],
) -> None:
    """Add main-sweep excludes for a copy whose files must not also be swept.

    ``copy_source`` is the full monorepo path of the copy's source; when it lies
    under ``source_root`` the excludes are emitted source-root-relative. A
    whole-subtree copy (include ``("**",)``, e.g. ``<root>/.export``) excludes the
    subtree (``.export/**``); a relocate of a glob subset from the root itself
    (e.g. ``*_test.py``) excludes exactly that glob so no original remains under
    the prefix.
    """
    prefix = f"{source_root.rstrip('/')}/" if source_root else ""
    if source_root and copy_source == source_root:
        # Relocate of a glob subset from the package root: exclude the glob(s).
        excludes.extend(include)
        return
    if not (prefix and copy_source.startswith(prefix)):
        return
    rel = copy_source.removeprefix(prefix)
    if include == ("**",):
        excludes.append(f"{rel}/**")
    else:
        excludes.extend(f"{rel}/{pattern}" for pattern in include)


def _origin_move_roots(include: tuple[str, ...]) -> frozenset[str]:
    """Return candidate source-root paths from origin_files include patterns.

    A ``core.move(ROOT, PREFIX)`` names its source as the package root that
    origin_files selects via ``ROOT + "/**"``. Collecting those roots lets the
    transform parser recognize a source-root move (which carries the
    ``destination_prefix``) even when its destination is a non-empty prefix
    rather than the empty root.
    """
    roots: set[str] = set()
    for pattern in include:
        if pattern.endswith("/**"):
            roots.add(pattern.removesuffix("/**"))
    return frozenset(roots)


def _reject_partial_flatten_of_whole_tree(
    *, source_root_move: MoveSpec | None, origin_include: tuple[str, ...]
) -> None:
    """Reject a subtree flatten under a whole-tree (``**``) origin selection.

    When ``origin_files`` selects the entire tree (``**`` with no ``<root>/**``
    pattern) and a ``core.move(SUB, "")`` flattens only a strict subtree, real
    Copybara leaves every sibling path at its identity location while lifting the
    subtree to the root -- a mixed tree copybarista's single ``source_root`` /
    ``destination_prefix`` model cannot represent. Reject it here with a clear
    message rather than misclassify ``SUB`` as the whole ``source_root`` and fail
    later in prefix-stripping with a misleading "outside core.move source root".
    """
    if source_root_move is None:
        return
    selects_whole_tree = "**" in origin_include and not _origin_move_roots(
        origin_include
    )
    if selects_whole_tree and source_root_move.source not in ("", "."):
        raise ConfigError(
            "core.move flattening a subtree under a whole-tree origin selection "
            "(glob(['**'])) is unsupported: it mixes lifted-subtree and "
            "identity-kept paths, which has no single source_root/destination_prefix"
        )


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


def _strip_prefixes_and_file_copies(
    patterns: tuple[str, ...], source_root: str
) -> tuple[tuple[str, ...], tuple[FileCopy, ...]]:
    """Strip source-root globs and preserve extra roots as file-copy entries."""
    if not source_root:
        return patterns, ()
    prefix = f"{source_root.rstrip('/')}/"
    stripped: list[str] = []
    copies: list[FileCopy] = []
    for pattern in patterns:
        if pattern == source_root:
            stripped.append("**")
        elif pattern.startswith(prefix):
            stripped.append(pattern.removeprefix(prefix))
        else:
            copies.append(_file_copy_from_origin_pattern(pattern))
    return tuple(stripped), tuple(copies)


def _file_copy_from_origin_pattern(pattern: str) -> FileCopy:
    """Translate an extra ``origin_files`` root into a Copybarista file copy."""
    if pattern.endswith("/**"):
        source = pattern.removesuffix("/**")
        return FileCopy(source=source, destination=source)
    return FileCopy(source=pattern, destination=pattern)


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
                f"origin_files exclude pattern is outside core.move source root: {pattern}"
            )
    return tuple(stripped)


def _strip_markers(before: str) -> tuple[str, str]:
    """Infer strip block markers from a multiline replacement string."""
    lines = [line for line in before.splitlines() if line.strip()]
    start = lines[0]
    if before.startswith("\n"):
        start = "\n" + start
    end = lines[-1]
    suffix = before[before.rfind(end) + len(end) :]
    if suffix.startswith("\n\n"):
        end += "\n\n"
    return start, end


def _looks_like_strip_block(before: str) -> bool:
    """Return whether a multiline replacement is a marker-delimited strip."""
    lines = [line for line in before.splitlines() if line.strip()]
    # A strip block needs at least a start and an end marker line.
    if len(lines) < 2:
        return False
    marker_text = "\n".join((lines[0], lines[-1]))
    return "copybarista:" in marker_text or "copybara:" in marker_text
