"""Change-request import support for public repository edits.

The importer computes the public diff, maps paths back to the source-of-truth
checkout, reverses supported transforms, writes a review branch checkout, and
re-exports to prove the public tree is reproduced.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from os import walk
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Literal

import difflib
import itertools
import re
import shutil
import stat
import subprocess
import sys
import tempfile

from treekle import ReadError

from copybarista.commands import CommandRunner
from copybarista.config import (
    WorkflowConfig,
    reverse_file_moves,
    reverse_move_transforms,
)
from copybarista.delta_render import DeltaError
from copybarista.errors import ImportRequestError, TransformError
from copybarista.export import export_folder
from copybarista.globs import GlobSet, Globstar
from copybarista.pyproject import DeltaConflictError, PyprojectSource, same_render
from copybarista.template import ReplaceTemplate, replace_template
from copybarista.transforms import (
    _strip_blocks_with_else,
    line_has_marker_token,
    strip_source_regions,
    strip_source_text,
    uncomment_source_text,
)


if TYPE_CHECKING:
    from copybarista.config import Transform


ChangeAction = Literal["created", "modified", "deleted", "type_changed"]
ChangeOutcome = Literal["applied", "skipped", "merged"]
EntryKind = Literal["file", "symlink"]
VCS_DIRS = frozenset((".git", ".hg", ".svn"))


@dataclass(frozen=True, slots=True, kw_only=True)
class TreeEntry:
    """A deterministic file-like tree entry.

    Snapshots record file bytes, symlink target bytes, and executable bits so
    import planning compares public tree state rather than filesystem metadata
    that varies across machines.

    Attributes:
      kind: Snapshot entry type.
      data: File bytes or symlink target bytes.
      executable: Whether a regular file has any executable bit set.

    """

    kind: EntryKind
    data: bytes
    executable: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class TreeChange:
    """One public-tree-relative change between base and head.

    Attributes:
      path: Public repository path relative to the compared roots.
      action: Diff action needed to make base match head.

    """

    path: str
    action: ChangeAction


@dataclass(frozen=True, slots=True, kw_only=True)
class TreeDiff:
    """Deterministic changed paths between two tree snapshots.

    Changes are sorted by public path so import reports and tests are stable.
    """

    changes: tuple[TreeChange, ...]


def _tree_symlink(path: Path) -> TreeEntry:
    """Build a deterministic snapshot entry for one symlink."""
    return TreeEntry(kind="symlink", data=path.readlink().as_posix().encode())


def _tree_file(path: Path) -> TreeEntry:
    """Build a deterministic snapshot entry for one regular file."""
    mode = stat.S_IMODE(path.stat().st_mode)
    executable = bool(mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH))
    return TreeEntry(
        kind="file",
        data=path.read_bytes(),
        executable=executable,
    )


def _is_metadata_path(public_path: str) -> bool:
    """Return whether a path belongs to VCS or Copybarista metadata."""
    parts = Path(public_path).parts
    return (
        bool(VCS_DIRS.intersection(parts))
        or public_path == ".copybarista"
        or public_path.startswith(".copybarista/")
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class TreeSnapshot:
    """File and symlink bytes under one root.

    Snapshots are the public diff boundary. They intentionally ignore VCS and
    `.copybarista` metadata so repository internals do not become importable
    source changes.

    Attributes:
      entries: Snapshot entries keyed by public-tree-relative path.

    """

    entries: dict[str, TreeEntry]

    @classmethod
    def from_root(cls, root: Path) -> TreeSnapshot:
        """Build a snapshot from a local tree root.

        Args:
          root: Directory to snapshot.

        Returns:
          snapshot: Deterministic file and symlink snapshot.

        """
        if root.is_symlink() or not root.is_dir():
            raise ImportRequestError(f"Cannot snapshot tree root: {root}")
        entries: dict[str, TreeEntry] = {}
        for current, dirnames, filenames in walk(
            root,
            onerror=_raise_snapshot_error,
        ):
            current_path = Path(current)
            dirnames.sort()
            for dirname in tuple(dirnames):
                path = current_path / dirname
                rel = path.relative_to(root).as_posix()
                if _is_metadata_path(rel):
                    dirnames.remove(dirname)
                    continue
                if path.is_symlink():
                    dirnames.remove(dirname)
                    entries[rel] = _tree_symlink(path)
            for filename in sorted(filenames):
                path = current_path / filename
                rel = path.relative_to(root).as_posix()
                if _is_metadata_path(rel):
                    continue
                if path.is_symlink():
                    entries[rel] = _tree_symlink(path)
                elif path.is_file():
                    entries[rel] = _tree_file(path)
        return cls(entries=entries)

    def diff(self, other: TreeSnapshot) -> TreeDiff:
        """Return created, deleted, modified, and type-changed paths.

        Args:
          other: TreeSnapshot to diff against this one.

        Returns:
          result: TreeDiff listing changes from self to other.

        """
        before_parents = _entry_parent_paths(self.entries)
        after_parents = _entry_parent_paths(other.entries)
        replaced_directories = before_parents.intersection(other.entries)
        changes: list[TreeChange] = []
        for path in sorted(set(self.entries) | set(other.entries)):
            before = self.entries.get(path)
            after = other.entries.get(path)
            if before is None:
                action: ChangeAction = (
                    "type_changed" if path in before_parents else "created"
                )
                changes.append(TreeChange(path=path, action=action))
            elif after is None:
                if _has_parent(path, parents=replaced_directories):
                    continue
                action = "type_changed" if path in after_parents else "deleted"
                changes.append(TreeChange(path=path, action=action))
            elif before.kind != after.kind:
                changes.append(TreeChange(path=path, action="type_changed"))
            elif before.data != after.data or before.executable != after.executable:
                changes.append(TreeChange(path=path, action="modified"))
        return TreeDiff(changes=tuple(changes))


@dataclass(frozen=True, slots=True, kw_only=True)
class PathMapper:
    """Map public repository paths back into the source-of-truth checkout.

    This is the first import gate after diffing: public paths must still be in
    the configured exported file set and must not target metadata.
    """

    config: WorkflowConfig
    matcher: GlobSet = field(init=False)

    def __post_init__(self) -> None:
        """Compile file selection once for all changed paths."""
        object.__setattr__(
            self,
            "matcher",
            GlobSet(
                include=self.config.files.include,
                exclude=self.config.files.effective_exclude(),
                globstar=self.config.globstar,
            ),
        )

    def source_path(
        self,
        public_path: str,
        *,
        action: ChangeAction = "modified",
        exported_source: str = "",
    ) -> str:
        """Return the source-relative path for a public path.

        Args:
          public_path: Public repository path relative to the exported root.
          action: The change action for this path. A ``deleted`` change carries no
            content to import, so an excluded path being deleted resolves to its
            identity path (a source-side no-op) rather than being rejected; only an
            add/modify of an excluded path is rejected (see below).
          exported_source: Exact source recorded by the forward export, when the
            public path already exists in that export.

        Returns:
          source_path: Source checkout path relative to the source root.

        Raises:
          ImportRequestError: If the path is excluded, metadata, or unmapped.

        """
        if _is_metadata_path(public_path):
            raise ImportRequestError(
                f"Public path is excluded or unmapped: {public_path}",
            )
        source_public_path = reverse_move_transforms(
            public_path=public_path,
            transforms=self.config.transforms,
        )
        if self._is_generated_path(source_public_path):
            raise ImportRequestError(
                f"Public path is excluded or unmapped: {public_path}",
            )
        if self.config.files.pyproject and source_public_path == "pyproject.toml":
            if action == "deleted":
                raise ImportRequestError(
                    "Public pyproject.toml is rendered from "
                    f"{self.config.files.pyproject} and cannot be deleted",
                )
            return self.config.files.pyproject
        if exported_source:
            return exported_source
        source = self._unrendered_source_path(source_public_path, action=action)
        # Export withholds the delta from every selection, so no public path is it.
        if not source or (
            self.config.files.pyproject
            and PurePosixPath(source) == PurePosixPath(self.config.files.pyproject)
        ):
            raise ImportRequestError(
                f"Public path is excluded or unmapped: {public_path}",
            )
        return source

    # First match wins, so a narrower entry listed earlier (trackinizer's own
    # ``typings/transformers`` override) takes precedence over the shared tree.
    #
    # A path a ``[[files.moves]]`` entry still places there is still exported, so
    # it is not relocated: configgle moves its own ``configgle/typings`` to public
    # ``typings``, and an edit to that must stay in configgle. The whole-tree move
    # (``path = ""``) never lands at the public root, so it does not count.
    def relocated_source_path(self, public_path: str) -> str:
        """Return a ``[[files.relocated]]`` tree's new source path, else ``""``.

        Args:
          public_path: Public repository path relative to the exported root.

        Returns:
          source_path: The relocated source path, or ``""`` when none matches.

        """
        path = PurePosixPath(public_path)
        if any(
            move.path and path.is_relative_to(move.destination)
            for move in self.config.files.moves
        ):
            return ""
        for relocation in self.config.files.relocated:
            root = PurePosixPath(relocation.public)
            if path.is_relative_to(root):
                return (
                    PurePosixPath(relocation.source) / path.relative_to(root)
                ).as_posix()
        return ""

    def _unrendered_source_path(
        self,
        source_public_path: str,
        *,
        action: ChangeAction,
    ) -> str:
        """Return the source path the selection or a copy maps, else ``""``."""
        copied_source, dedicated = self._copied_source_path(
            source_public_path,
            root_destination=False,
        )
        if copied_source:
            return copied_source
        if dedicated:
            copied_source, _ = self._copied_source_path(
                source_public_path,
                root_destination=True,
            )
            return copied_source
        relocated = self.relocated_source_path(source_public_path)
        if relocated:
            return relocated
        prefixed = self._moved_source_relative_path(source_public_path)
        if prefixed is not None:
            if not self.matcher.matches(prefixed):
                return ""
            if not self.config.source_root:
                return prefixed
            return f"{self.config.source_root}/{prefixed}"
        # A root copy covers a file only by matching it, so no root copy claimed it.
        copied_source, _ = self._copied_source_path(
            source_public_path,
            root_destination=True,
        )
        if copied_source:
            return copied_source
        # A path under no relocation rule keeps its identical path on both
        # sides -- it lives at the same repo-root location in source and
        # public. Mirrors Copybara's CopyOrMove: a path matching no move glob
        # is left untouched. Such a path is never under source_root, so it is
        # returned as-is without the source_root prefix. This is how a
        # deletion of a path whose export mapping was dropped from config
        # (the typings/brotli class) imports as a no-op instead of wedging.
        #
        # A path the config explicitly EXCLUDES is never exported, so an
        # add/modify of one in the public tree cannot come from a faithful
        # export -- reject it rather than silently write it at its identity
        # path. A DELETION of an excluded path carries no content and targets
        # a path the source never held, so it must import as an identity no-op
        # (mirrors the typings/brotli class): rejecting it would re-wedge the
        # whole import exactly as the original CI regression did.
        if action != "deleted" and self.matcher.excludes(source_public_path):
            return ""
        return source_public_path

    def _is_generated_path(self, public_path: str) -> bool:
        """Return whether `public_path` was generated by `[[files.write]]`."""
        return any(
            public_path == file_write.path for file_write in self.config.files.write
        )

    def _copied_source_path(
        self,
        public_path: str,
        *,
        root_destination: bool,
    ) -> tuple[str, bool]:
        """Return a copied source and whether a copy covered the public path."""
        covered = False
        for file_copy in self.config.files.copy:
            is_root = file_copy.destination in (".", "./")
            if is_root != root_destination:
                continue
            matcher = GlobSet(
                include=file_copy.include,
                exclude=file_copy.effective_exclude(),
                globstar=self.config.globstar,
            )
            if public_path == file_copy.destination:
                covered = True
                if matcher.matches(Path(file_copy.source).name):
                    return file_copy.source, True
                continue
            if is_root:
                if not matcher.matches(public_path):
                    continue
                return f"{file_copy.source}/{public_path}", True
            prefix = f"{file_copy.destination}/"
            if not public_path.startswith(prefix):
                continue
            covered = True
            rel = public_path.removeprefix(prefix)
            if matcher.matches(rel):
                return f"{file_copy.source}/{rel}", True
        return "", covered

    # A path the ``[[files.moves]]`` sequence placed (whether under the package prefix
    # or kept at the public root by a back-move) belongs to the source-root selection:
    # reversing the moves yields its source-relative path and the caller re-roots it
    # under ``source_root``. A path no move touched is under no relocation rule at all;
    # ``None`` signals the caller to keep it at its identical path (Copybara's
    # unmatched-``core.move`` behavior) rather than raise or wrongly re-root it under
    # ``source_root``.
    def _moved_source_relative_path(self, public_path: str) -> str | None:
        """Return the source-root-relative path, or ``None`` if unrelocated."""
        if not self.config.files.moves:
            return public_path
        reversed_path, moved = reverse_file_moves(public_path, self.config.files.moves)
        return reversed_path if moved else None


@dataclass(frozen=True, slots=True, kw_only=True)
class ImportChange:
    """A planned or applied source-of-truth change.

    Attributes:
      public: Public repository path that changed.
      source: Source checkout path that should receive the change.
      action: File action to apply.
      transforms: Reversible transform IDs applied while mapping content.
      outcome: How the importer reconciled the change with the source. Strict
          imports always ``applied``; merge imports may ``skipped`` an
          already-present change or ``merged`` independent source drift.

    """

    public: str
    source: str
    action: ChangeAction
    transforms: tuple[str, ...] = ()
    outcome: ChangeOutcome = "applied"


@dataclass(frozen=True, slots=True, kw_only=True)
class ImportPlan:
    """Validated public changes ready to write.

    Plans separate validation from mutation so import failures can happen
    before destination writes whenever possible.
    """

    changes: tuple[ImportChange, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class ImportResult:
    """Result report for one change-request import.

    This is the JSON/report boundary for GitHub workflows and local diagnosis.
    """

    changes: tuple[ImportChange, ...]

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-serializable report.

        Returns:
          result: Dict with changes array describing each import action.

        """
        return {
            "changes": [
                {
                    "public": change.public,
                    "source": change.source,
                    "action": change.action,
                    "transforms": list(change.transforms),
                    "outcome": change.outcome,
                }
                for change in self.changes
            ],
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class ChangeRequestImporter:
    """Import a public tree diff into a source-of-truth checkout.

    The importer plans from public snapshots, applies validated changes with
    rollback protection, and optionally re-exports to prove the source checkout
    recreates the public head.

    Strict imports (the default) require the source checkout to reproduce the
    public base exactly, then overwrite each changed file with the reversed
    public head. Merge imports relax this: the source may have drifted ahead of
    the public base, and each change is reconciled by a per-file three-way merge
    against the reversed public base. This mirrors Copybara's ``merge_import``
    (``MergeImportTool``), which persists destination-only changes by treating
    the origin as the source of truth and merging it onto the destination.
    """

    config: WorkflowConfig
    public_base: Path
    public_head: Path
    source_base: Path
    destination: Path
    verify: bool = True
    merge_import: bool = False

    def plan(self) -> ImportPlan:
        """Build and validate the import plan.

        Returns:
          result: ImportPlan with changes and reverse transforms.

        """
        _validate_import_destination(self.destination)
        _validate_snapshot_root(self.public_base, label="Public base")
        _validate_snapshot_root(self.public_head, label="Public head")
        source_owners = self._exported_source_owners()
        public_base = TreeSnapshot.from_root(self.public_base)
        public_head = TreeSnapshot.from_root(self.public_head)
        diff = public_base.diff(public_head)
        mapper = PathMapper(config=self.config)
        _validate_directory_replacement_owners(
            public_base=public_base,
            public_head=public_head,
            source_owners=source_owners,
            mapper=mapper,
        )
        changes: list[ImportChange] = []
        for change in diff.changes:
            exported_source = source_owners.get(change.path, "")
            # The export retiring a relocated copy is not a request to delete the
            # tree's new home, which it no longer ships at all.
            if (
                change.action == "deleted"
                and mapper.relocated_source_path(change.path)
                and not exported_source
            ):
                continue
            changes.append(
                ImportChange(
                    public=change.path,
                    source=mapper.source_path(
                        change.path,
                        action=change.action,
                        exported_source=exported_source,
                    ),
                    action=change.action,
                    transforms=self._reverse_transform_ids(change.path),
                ),
            )
        return ImportPlan(changes=tuple(changes))

    def import_changes(self) -> ImportResult:
        """Apply the public diff to the destination checkout.

        Returns:
          result: ImportResult with outcomes for each change applied.

        """
        plan = self.plan()
        originals = _capture_originals(
            destination=self.destination,
            changes=plan.changes,
        )
        try:
            if self.merge_import:
                applied = self._merge_changes(plan.changes)
            else:
                applied = tuple(self._apply_change(change) for change in plan.changes)
                if self.verify:
                    # Strict mode reverses the public head exactly, so its
                    # re-export must reproduce public head. Merge mode folds in
                    # independent source drift, so its export legitimately differs
                    # from public head; the merge path verifies each reversal
                    # per-file instead (see _check_merge_reversal).
                    self._check_public_head()
        except Exception:
            _restore_originals(originals)
            raise
        finally:
            _discard_backups(originals)
        return ImportResult(changes=applied)

    def _apply_change(self, change: ImportChange) -> ImportChange:
        """Overwrite one destination path with the reversed public head."""
        target = _validated_target(
            destination=self.destination,
            relative_path=change.source,
        )
        if change.action == "deleted":
            _delete_path(target)
            return change
        public_path = self.public_head / change.public
        if public_path.is_symlink():
            _write_symlink(
                public_path=public_path,
                target=target,
                public_root=self.public_head,
                destination_root=self.destination,
            )
            return change
        # A snapshot holds files and symlinks, so a directory head is a type change.
        if public_path.is_dir():
            _delete_path(target)
            target.mkdir(parents=True)
            shutil.copymode(public_path, dst=target)
            return change
        if self._is_pyproject(change):
            target.write_bytes(self._fold_pyproject(public_path.read_bytes()))
            return change
        head_data = self._reverse_changed_lines(
            public_path=change.public,
            source_path=change.source,
            merged_public=public_path.read_bytes(),
            ours_public=self._public_base_bytes(change.public),
        )
        if change.action == "type_changed" or target.is_symlink() or target.is_dir():
            _delete_path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(head_data)
        shutil.copymode(public_path, dst=target)
        self._reformat_imported_source(change=change, target=target)
        return change

    # ``ruff_format`` has no content inverse, so ``_reverse_content`` skips it on
    # import. But a namespace ``replace`` reverses by pure text substitution that
    # preserves physical line order: a public file whose imports are sorted under the
    # *public* namespace can land unsorted under the *source* namespace whenever the two
    # namespaces sort their import groups differently. Re-applying ruff (isort + format)
    # forward on the written file restores source-namespace order, so importing never
    # pollutes the source tree with lint violations.
    #
    # Runs with ``cwd=self.destination`` so ruff discovers the source tree's own config
    # (e.g. ``known-first-party``), and only when a ``ruff_format`` transform's glob
    # matches this change's public path. A whole-tree ``ruff_format`` (``path = "."`` /
    # ``""``, the shape every shipped config uses) formats the entire staged tree on
    # export, so it matches every reversed file here -- a literal ``.`` glob would match
    # nothing and the reformat would silently never run (leaving isort-dirty imports).
    def _reformat_imported_source(self, *, change: ImportChange, target: Path) -> None:
        """Re-run ``ruff_format`` forward on a freshly reversed source file."""
        if not any(
            transform.type == "ruff_format"
            and _ruff_format_matches(transform, change.public)
            for transform in self.config.transforms
        ):
            return
        rel = target.relative_to(self.destination).as_posix()
        runner = CommandRunner()
        runner.run(
            [
                sys.executable,
                "-m",
                "ruff",
                "check",
                "--fix",
                "--exit-zero",
                "--no-cache",
                rel,
            ],
            check=False,
            cwd=self.destination,
        )
        runner.run(
            [sys.executable, "-m", "ruff", "format", "--no-cache", rel],
            check=False,
            cwd=self.destination,
        )

    # Merges happen in PUBLIC space: the current source is exported once, then each file
    # is merged as ``diff3(source_export, public_base, public_head)`` and the result
    # reversed back to source form. Merging publicly keeps the non-reversible direction
    # (e.g. ``strip_block``) out of the common already-applied path -- a file whose
    # export already equals the public head needs no reversal at all.
    def _merge_changes(
        self,
        changes: tuple[ImportChange, ...],
    ) -> tuple[ImportChange, ...]:
        """Reconcile every change by three-way merge, raising on conflicts."""
        with tempfile.TemporaryDirectory(prefix="copybarista-import-merge-") as tmp:
            source_export = Path(tmp) / "source-export"
            export_folder(
                config=self.config,
                source_ref=self.source_base,
                destination=source_export,
                force=True,
            )
            applied: list[ImportChange] = []
            conflicts: list[str] = []
            for change in changes:
                resolved, conflicted = self._merge_change(
                    change=change,
                    source_export=source_export,
                )
                applied.append(resolved)
                if conflicted:
                    conflicts.append(resolved.source)
        if conflicts:
            raise ImportRequestError(
                "Merge import produced conflicts in: " + ", ".join(sorted(conflicts)),
            )
        return tuple(applied)

    # Normally the source's exported form at the public path (Copybara reads the origin
    # workdir; we read a single re-export as its equivalent). But a path under no
    # relocation rule is NOT shipped by the export -- it lives at its identical path in
    # the source tree and is absent from the export. For such an identity path
    # (``change.source == change.public``) fall back to the source file directly, so the
    # merge sees the real local side rather than an empty string that would spuriously
    # conflict with the incoming public edit.
    def _ours_public_entry(
        self,
        *,
        change: ImportChange,
        source_export: Path,
    ) -> TreeEntry | None:
        """Return the source's public-space file-like state for merge."""
        exported = source_export / change.public
        if exported.is_symlink():
            return _tree_symlink(exported)
        if exported.is_file():
            return _tree_file(exported)
        # The export never ships a relocated tree, so its new home is the local side.
        relocated = PathMapper(config=self.config).relocated_source_path(change.public)
        if change.source in (change.public, relocated):
            source_file = self.source_base / change.source
            if source_file.is_symlink():
                return _tree_symlink(source_file)
            if source_file.is_file():
                return _tree_file(source_file)
        return None

    def _public_base_entry(self, public_path: str) -> TreeEntry | None:
        """Return the public base's file-like state for a path."""
        base_path = self.public_base / public_path
        if base_path.is_symlink():
            return _tree_symlink(base_path)
        if base_path.is_file():
            return _tree_file(base_path)
        return None

    def _is_pyproject(self, change: ImportChange) -> bool:
        """Return whether ``change`` edits the root ``pyproject.toml`` rendered from a delta."""
        return bool(self.config.files.pyproject) and change.source == (
            self.config.files.pyproject
        )

    def _fold_pyproject(self, public: bytes) -> bytes:
        """Return the delta that renders ``public`` from the destination's base."""
        try:
            source = PyprojectSource.load(
                self.destination / self.config.files.pyproject,
                source_ref=self.destination,
            )
            return source.fold(public.decode()).encode()
        except (OSError, UnicodeDecodeError, DeltaError, ReadError) as err:
            raise ImportRequestError(
                f"Cannot fold public pyproject.toml into {self.config.files.pyproject}: {err}",
            ) from err

    # A delta has no public form to three-way merge, and lines are the wrong unit: a
    # comment the source added beside an edited key conflicts by line though neither
    # side touched the other's. The public edit is replayed key by key onto the current
    # render instead, so drift on either side survives; only a value both changed
    # conflicts.
    def _merge_pyproject(
        self,
        *,
        change: ImportChange,
        target: Path,
        ours: bytes,
        head: bytes,
    ) -> tuple[ImportChange, bool]:
        """Reconcile a public ``pyproject.toml`` edit with the delta it renders from."""
        # Bytes: only an identical file skips the fold, so a comment edit always
        # reaches the fold that rejects it.
        if ours == head:
            return _with_outcome(change, "skipped"), False
        base = self._public_base_bytes(change.public)
        # A public base spelled differently from the render (a hand-formatted
        # contribution not yet re-exported) is not drift.
        clean = _same_render(ours, base)
        try:
            source = PyprojectSource.load(
                self.destination / self.config.files.pyproject,
                source_ref=self.destination,
            )
            delta = (
                source.fold(head.decode())
                if clean
                else source.merge(base=base.decode(), head=head.decode())
            )
        except DeltaConflictError:
            return _with_outcome(change, "merged"), True
        except (OSError, UnicodeDecodeError, DeltaError, ReadError) as err:
            raise ImportRequestError(
                f"Cannot fold public pyproject.toml into {self.config.files.pyproject}: {err}",
            ) from err
        target.write_bytes(delta.encode())
        return _with_outcome(change, "applied" if clean else "merged"), False

    def _public_base_bytes(self, public_path: str) -> bytes:
        """Return the public base's regular-file bytes, or empty when absent."""
        entry = self._public_base_entry(public_path)
        return entry.data if entry is not None and entry.kind == "file" else b""

    # A namespace-collapse reverse is a guess: once the internal prefix is gone, public
    # text cannot distinguish a module reference from a path segment or prose. Where the
    # source still holds the real text, use it.
    #
    # Whole-document, not per line: ``strip_block`` and ``internal_lines`` reinsert
    # multi-line regions.
    def _reverse_changed_lines(
        self,
        *,
        public_path: str,
        source_path: str,
        merged_public: bytes,
        ours_public: bytes,
    ) -> bytes:
        """Reverse the public text, then restore lines the edit never touched."""
        if not self.merge_import:
            self._check_injective_reverses(
                public_path=public_path,
                source_path=source_path,
                data=merged_public,
            )
        reversed_whole = self._reverse_content(
            public_path=public_path,
            source_path=source_path,
            data=merged_public,
        )
        source_file = self.source_base / source_path
        if not source_file.is_file() or source_file.is_symlink():
            return reversed_whole
        try:
            source_lines = source_file.read_text(encoding="utf-8").splitlines(
                keepends=True,
            )
            reversed_lines = reversed_whole.decode().splitlines(keepends=True)
        except UnicodeDecodeError:
            return reversed_whole

        # Reversing the pre-edit export exposes what the reverse gets wrong on
        # text whose true form is known, so the two can be paired.
        try:
            reversed_ours_lines = (
                self._reverse_content(
                    public_path=public_path,
                    source_path=source_path,
                    data=ours_public,
                )
                .decode()
                .splitlines(keepends=True)
            )
        except UnicodeDecodeError:
            return reversed_whole
        # Rebuild against SOURCE, splicing in only the public edit's own hunks.
        # Restoring line-by-line cannot recover a source line the export dropped
        # entirely (ruff prunes an import a stripped region was the sole user
        # of), because that line has no counterpart in reversal space to key on.
        return _splice_public_edits(
            reversed_ours_lines=reversed_ours_lines,
            reversed_lines=reversed_lines,
            source_lines=source_lines,
        ).encode()

    # Mirrors Copybara's ``MergeImportTool``: the public head is the incoming change,
    # the public base is the merge baseline, and the source's exported form is the local
    # side. A source already at head is a no-op (``skipped``); independent text drift
    # merges cleanly (``merged``); overlapping edits record a conflict for the caller to
    # surface. Deletions, symlinks, and directory changes are not text-mergeable: they
    # are force-propagated from the public head via ``_apply_change`` (matching
    # Copybara, which propagates origin deletions regardless of destination drift).
    def _merge_change(
        self,
        *,
        change: ImportChange,
        source_export: Path,
    ) -> tuple[ImportChange, bool]:
        """Reconcile one change by three-way merge in public space."""
        target = _validated_target(
            destination=self.destination,
            relative_path=change.source,
        )
        public_path = self.public_head / change.public
        # Not text-mergeable: deletions, symlink/dir heads, and type changes
        # (e.g. symlink<->file) force-propagate from public head exactly as the
        # strict path does. Byte three-way-merge applies only to file->file.
        if (
            change.action in ("deleted", "type_changed")
            or public_path.is_symlink()
            or public_path.is_dir()
        ):
            return self._apply_change(change), False
        head_entry = _tree_file(public_path)
        ours_entry = self._ours_public_entry(
            change=change,
            source_export=source_export,
        )
        if self._is_pyproject(change):
            return self._merge_pyproject(
                change=change,
                target=target,
                ours=ours_entry.data if ours_entry is not None else b"",
                head=head_entry.data,
            )
        if ours_entry == head_entry:
            return _with_outcome(change, "skipped"), False
        base_entry = self._public_base_entry(change.public)
        if ours_entry == base_entry:
            return self._apply_change(change), False
        if ours_entry is not None and ours_entry.kind != "file":
            return self._apply_change(change), False
        head_public = head_entry.data
        ours_public = ours_entry.data if ours_entry is not None else b""
        base_public = (
            base_entry.data
            if base_entry is not None and base_entry.kind == "file"
            else b""
        )
        merged_public, conflicted = _three_way_merge(
            current=ours_public,
            base=base_public,
            incoming=head_public,
        )
        if conflicted:
            # The caller rolls the whole import back on any conflict, so the
            # conflict-marker bytes are never a valid final tree. Don't reverse
            # (which can raise on marker text), write, or reformat them -- report
            # the conflict and leave the destination untouched.
            return _with_outcome(change, "merged"), True
        merged_source = self._reverse_changed_lines(
            public_path=change.public,
            source_path=change.source,
            merged_public=merged_public,
            ours_public=ours_public,
        )
        # Mirror _apply_change: a drifted symlink target must be removed, not
        # written through -- otherwise write_bytes follows the link and mutates
        # its referent instead of restoring a regular file.
        if target.is_symlink():
            _delete_path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(merged_source)
        shutil.copymode(public_path, dst=target)
        self._reformat_imported_source(change=change, target=target)
        return _with_outcome(change, "merged"), conflicted

    # Reversible ``replace`` transforms are applied in one SIMULTANEOUS pass per
    # contiguous run (see ``_reverse_replace_all``): applying them sequentially double-
    # rewrites text whenever two forward transforms have overlapping ``after`` strings
    # (the wesearch bare/dotted namespace pair). A ``strip_block`` / ``internal_lines``
    # reversal breaks a run because it re-inserts source-only regions between replace
    # groups; those runs are fused separately, preserving each strip's exact position.
    def _reverse_content(
        self,
        *,
        public_path: str,
        source_path: str,
        data: bytes,
    ) -> bytes:
        """Undo supported content transforms for one public file."""
        content = data
        match_path = reverse_move_transforms(
            public_path=public_path,
            transforms=self.config.transforms,
        )
        pending: list[Transform] = []
        for transform in reversed(self.config.transforms):
            if transform.type in ("move", "ruff_format") or not transform.reversible:
                continue
            if not _matches_transform(transform, match_path, self.config.globstar):
                continue
            if transform.type == "uncomment":
                content = _reverse_replaces(
                    content,
                    transforms=pending,
                    public_path=public_path,
                )
                pending = []
                content = self._recomment_source_blocks(
                    public_path=public_path,
                    source_path=source_path,
                    transform=transform,
                    content=content,
                )
                continue
            if transform.type in ("strip_block", "internal_lines"):
                content = _reverse_replaces(
                    content,
                    transforms=pending,
                    public_path=public_path,
                )
                pending = []
                # Neither transform is invertible from the public tree: the
                # removed source content (a marker-delimited block, or each line
                # carrying the marker) is absent from public, so reversal cannot
                # reconstruct it. To a re-insertion heuristic a block is just a
                # line -- a contiguous source-only region -- so both go through
                # the same machinery: re-insert the source's removed region(s)
                # verbatim at their original position and let the public edits
                # apply around them. Re-export removes them again, reproducing
                # the public head; the import PR's CI is the human-review gate.
                content = self._reinsert_source_only_regions(
                    public_path=public_path,
                    source_path=source_path,
                    transform=transform,
                    content=content,
                    match_path=match_path,
                )
                continue
            pending.append(transform)
        return _reverse_replaces(content, transforms=pending, public_path=public_path)

    # Strict imports use this guard as a proxy for "is the reversal unambiguous";
    # merge imports establish that ground truth directly by comparing the source's
    # actual export to the public base/head, so the heuristic is redundant and wrong
    # there (the source legitimately carries exported text from drift).
    #
    # ``regex_groups`` transforms are not exempt: a boundary anchor still matches the
    # bare public name in a URL, a path segment, or prose.
    #
    # It reads the incoming public text, not text mid-reversal: a re-inserted
    # source-only region carries exported names the public edit never added, and a
    # later rule's reversal can produce an earlier rule's ``after``.
    def _check_injective_reverses(
        self,
        *,
        public_path: str,
        source_path: str,
        data: bytes,
    ) -> None:
        """Reject a public edit whose ``replace`` reversals are ambiguous."""
        match_path = reverse_move_transforms(
            public_path=public_path,
            transforms=self.config.transforms,
        )
        replaces = [
            transform
            for transform in self.config.transforms
            if transform.type == "replace"
            and transform.reversible
            and _matches_transform(transform, match_path, self.config.globstar)
        ]
        if not replaces:
            return
        text = _decoded(data, public_path=public_path)
        for transform in replaces:
            self._check_injective_reverse(
                public_path=public_path,
                source_path=source_path,
                transform=transform,
                text=text,
            )

    def _check_injective_reverse(
        self,
        *,
        public_path: str,
        source_path: str,
        transform: Transform,
        text: str,
    ) -> None:
        """Reject automatic reversals that cannot be mapped back unambiguously."""
        reverse_before = _reverse_before(transform)
        if not reverse_before:
            raise ImportRequestError(
                f"Public path requires non-reversible empty replacement "
                f"for transform '{transform.id}': {public_path}",
            )
        if _has_explicit_reversal(transform):
            return
        source_file = self.source_base / source_path
        if source_file.exists() and not source_file.is_symlink():
            source_text = _read_import_text(
                path=source_file,
                label=f"Source base path is not UTF-8: {source_file}",
            )
            # Reject only STANDALONE occurrences of the replacement text in the
            # source -- i.e. occurrences not already accounted for by `before`
            # strings. For namespace-prefix rewrites the replacement is a
            # substring of `before` (e.g. ``pub`` is a substring of the longer
            # internal ``a.b.pub`` that exports to ``pub``), so every source
            # `before` legitimately contains `reverse_before`; those reverse
            # cleanly and must not trip the guard. A standalone occurrence (e.g.
            # the literal exported string appearing in source prose) genuinely
            # cannot be reversed unambiguously. We compare counts: the number of
            # `reverse_before` hits explained by `before` occurrences vs the
            # total in source.
            #
            # This count math is a conservative proxy, exact only when `before`
            # and `reverse_before` do not self-overlap (``str.count`` is
            # non-overlapping, so a self-nested token like ``before="aa"`` with
            # ``reverse_before="a"`` mis-attributes and could false-reject). Every
            # shipped transform is a namespace-prefix rewrite (``loop.pkg.`` ->
            # ``pkg``) with no self-overlap, so the proxy is exact in practice.
            before_text = transform.before
            explained = (
                _match_count(transform, before_text, source_text)
                * before_text.count(reverse_before)
                if before_text and reverse_before in before_text
                else 0
            )
            if _match_count(transform, reverse_before, source_text) > explained:
                raise ImportRequestError(
                    f"Source base already contains exported replacement text "
                    f"for transform '{transform.id}': {public_path}",
                )
        base_path = self.public_base / public_path
        if base_path.exists() and not base_path.is_symlink():
            base_text = _read_import_text(
                path=base_path,
                label=f"Public base path is not UTF-8: {public_path}",
            )
            base_count = _match_count(transform, reverse_before, base_text)
        else:
            base_count = 0
        if _match_count(transform, reverse_before, text) > base_count:
            raise ImportRequestError(
                f"Public path adds exported replacement text for transform "
                f"'{transform.id}': {public_path}",
            )

    # In merge mode a non-reversible match is not fatal here: a file whose export
    # already matches the public head is reconciled without any reversal, so the
    # decision is deferred to ``_reverse_content``.
    def _reverse_transform_ids(self, public_path: str) -> tuple[str, ...]:
        """Return reversible transform IDs that affect a public path."""
        ids: list[str] = []
        match_path = reverse_move_transforms(
            public_path=public_path,
            transforms=self.config.transforms,
        )
        for transform in reversed(self.config.transforms):
            if transform.type in ("move", "ruff_format") or not transform.reversible:
                continue
            if _matches_transform(transform, match_path, self.config.globstar):
                if transform.type in ("strip_block", "internal_lines"):
                    # Not invertible, but rather than fail the import,
                    # _reverse_content re-inserts the source's removed region(s)
                    # verbatim (both strict and merge modes). The decision is
                    # deferred there; nothing to record as a reversible id here.
                    continue
                ids.append(transform.id)
        return tuple(ids)

    # Export replaces each marked block with its uncommented body, so the public text
    # holds a TRANSFORMED form rather than a subset -- the same shape as an
    # ``else``-branch ``strip_block``, and reversed the same way: locate each source
    # block's exported form and substitute the source block back. Left to the
    # ``replace`` bucket it reversed as a no-op in merge mode (its ``before``/``after``
    # are empty) and destroyed the markers.
    #
    # A block the public edit rewrote is re-commented in place, markers and all:
    # skipping it wrote the uncommented body straight to source, which re-exports to the
    # same public text and so is caught by nothing.
    def _recomment_source_blocks(
        self,
        *,
        public_path: str,
        source_path: str,
        transform: Transform,
        content: bytes,
    ) -> bytes:
        """Restore an ``uncomment`` transform's commented source form."""
        source_file = self.source_base / source_path
        if not source_file.exists() or source_file.is_symlink():
            return content
        source_text = _read_import_text(
            path=source_file,
            label=f"Source base path is not UTF-8: {source_file}",
        )
        public_text = _decoded(content, public_path=public_path)
        whole_export, _total = uncomment_source_text(source_text, transform=transform)
        public_lines = public_text.splitlines(keepends=True)
        replacements: list[tuple[int, int, str]] = []
        for source_block in _uncomment_source_blocks(source_text, transform=transform):
            exported, _count = uncomment_source_text(
                source_block.text,
                transform=transform,
            )
            # A block of markers alone exports nothing to restore.
            if not exported:
                continue
            lower, upper = _edited_block_bounds(
                public_text=public_text,
                whole_export=whole_export,
                exported_start=source_block.exported_start,
                exported_length=len(exported.splitlines(keepends=True)),
            )
            edited = "".join(public_lines[lower:upper])
            rebuilt = (
                source_block.text
                if edited == exported
                else _recomment_block(
                    edited,
                    source_block=source_block.text,
                    transform=transform,
                )
            )
            replacements.append((lower, upper, rebuilt))
        return _replace_line_spans(public_lines, replacements=replacements).encode()

    # ``strip_block`` and ``internal_lines`` both delete source-only content on export
    # (a marker-delimited block, or each line carrying the marker), which the public
    # tree cannot reconstruct. On import we splice the source's removed region(s) back
    # verbatim at their original position: the source file is the local side, and the
    # incoming public edits apply to the regions *around* them.
    #
    # Re-inserting is exact only when the incoming public text still contains the
    # exported context around each region. A public edit that rewrote that context can
    # displace a region so the rebuilt source no longer strips back to the incoming
    # public text. We verify that round-trip here -- re-strip the rebuilt source and
    # require it to reproduce ``public_text`` -- so a displaced re-insertion fails loud
    # (in both strict and merge modes) rather than silently writing a source tree whose
    # export has drifted. The strict path's ``_check_public_head`` is a whole-tree
    # backstop; merge mode folds in source drift and has no such tree-level check, so
    # this per-file gate is its safety net.
    def _reinsert_source_only_regions(
        self,
        *,
        public_path: str,
        source_path: str,
        transform: Transform,
        content: bytes,
        match_path: str,
    ) -> bytes:
        """Re-insert a transform's source-only regions into reversed content."""
        source_file = self.source_base / source_path
        if not source_file.exists() or source_file.is_symlink():
            return content
        source_text = _read_import_text(
            path=source_file,
            label=f"Source base path is not UTF-8: {source_file}",
        )
        source_text = _exported_until(
            source_text,
            transforms=self.config.transforms,
            stop=transform,
            match_path=match_path,
            globstar=self.config.globstar,
        )
        public_text = _decoded(content, public_path=public_path)
        if transform.else_marker and transform.start and transform.start in source_text:
            return _reverse_else_blocks(
                source_text=source_text,
                public_text=public_text,
                transform=transform,
            ).encode()
        rebuilt = _splice_source_only_regions(
            source_text=source_text,
            public_text=public_text,
            transform=transform,
        )
        # The offset splice is exact only when the public edit left the context
        # around each source-only region intact. When it did (the common case),
        # re-stripping the spliced result reproduces the incoming public text and
        # we are done. When a public edit REWROTE that context (e.g. reflowed the
        # construct a ``# copybarista:internal`` line lived in), the splice lands
        # at a stale offset; the anchored path below handles that.
        try:
            restripped = strip_source_text(rebuilt, transform)
        except TransformError as err:
            # The rebuilt source no longer parses back through the strip markers
            # (e.g. the public edit introduced a stray, unbalanced marker), so
            # re-stripping would remove the wrong span. Reject: this needs a human.
            raise ImportRequestError(
                f"Re-inserting source-only regions for transform '{transform.id}' "
                f"produced a source tree that cannot be re-exported for "
                f"{public_path}; a public edit disturbed a stripped region. "
                "Resolve the import by hand.",
            ) from err
        if restripped == public_text:
            return rebuilt.encode()
        # The offset splice re-strips cleanly but to something other than the
        # incoming public text: the public edit REWROTE the context around a
        # source-only region (e.g. reflowed the multi-line construct a
        # ``# copybarista:internal`` line lived in), so the region's original
        # offset lands in the wrong place -- possibly inside a rewritten line.
        # Rather than refuse, re-insert each source-only region by ANCHORING it to
        # its nearest surviving source-sibling line: the region goes right after
        # the source line that precedes it (or before the one that follows it) if
        # that line still appears in the public text. This always yields valid,
        # reviewable source; re-export strips the region again, reproducing the
        # public head around it.
        rebuilt = _anchor_source_only_regions(
            source_text=source_text,
            public_text=public_text,
            transform=transform,
        )
        if rebuilt is None:
            # A source-only run has NO surviving neighbor that aligns to the
            # public text: every kept line bracketing it was rewritten or removed,
            # so its position is undetermined. Reject rather than guess.
            raise ImportRequestError(
                f"Re-inserting source-only regions for transform '{transform.id}' "
                f"cannot place a stripped region in {public_path}: a public edit "
                "rewrote all of its surrounding context. Resolve by hand.",
            )
        # The anchored placement is best-effort, but the correctness contract is
        # absolute: re-stripping the rebuilt source MUST reproduce the incoming
        # public text (only the source-only regions were added, nothing else
        # changed). If it does not -- e.g. the public edit introduced a stray,
        # unbalanced marker that makes re-stripping remove the wrong span -- the
        # reversal is unsafe; reject rather than write a drifted source tree.
        try:
            reanchored = strip_source_text(rebuilt, transform)
        except TransformError as err:
            raise ImportRequestError(
                f"Re-inserting source-only regions for transform '{transform.id}' "
                f"produced a source tree that cannot be re-exported for "
                f"{public_path}; a public edit disturbed a stripped region. "
                "Resolve the import by hand.",
            ) from err
        if reanchored != public_text:
            raise ImportRequestError(
                f"Re-inserting source-only regions for transform '{transform.id}' "
                f"disturbed a stripped region in {public_path} so its export no "
                "longer reproduces the public content. Resolve the import by hand.",
            )
        return rebuilt.encode()

    def _exported_source_owners(self) -> dict[str, str]:
        """Return exact forward owners, verifying the strict public base."""
        with tempfile.TemporaryDirectory(prefix="copybarista-import-base-") as tmp:
            exported = Path(tmp) / "public-base"
            manifest = export_folder(
                config=self.config,
                source_ref=self.source_base,
                destination=exported,
                force=True,
            )
            if (
                self.verify
                and not self.merge_import
                and not self._reproduces(
                    TreeSnapshot.from_root(exported),
                    TreeSnapshot.from_root(self.public_base),
                )
            ):
                raise ImportRequestError(
                    "Configured source base does not reproduce public base",
                )
        return {
            PurePosixPath(entry.destination).as_posix(): entry.source
            for entry in manifest.files
        }

    def _check_public_head(self) -> None:
        """Verify the imported destination reproduces the public head tree."""
        with tempfile.TemporaryDirectory(prefix="copybarista-import-head-") as tmp:
            exported = Path(tmp) / "public-head"
            export_folder(
                config=self.config,
                source_ref=self.destination,
                destination=exported,
                force=True,
            )
            if not self._reproduces(
                TreeSnapshot.from_root(exported),
                TreeSnapshot.from_root(self.public_head),
            ):
                raise ImportRequestError(
                    "Imported source tree does not reproduce public head",
                )

    # A rendered ``pyproject.toml`` is compared by content: folding a public edit
    # into the delta re-renders it in the renderer's own spelling, so a hand-
    # formatted contribution never reproduces byte for byte. Both checks, the public
    # base before the import and the public head after it, share this one rule.
    def _reproduces(self, exported: TreeSnapshot, public: TreeSnapshot) -> bool:
        """Return whether ``exported`` matches ``public``, the render up to spelling."""
        if not self.config.files.pyproject:
            return exported == public
        ours = exported.entries.get("pyproject.toml")
        theirs = public.entries.get("pyproject.toml")
        rest = {k: v for k, v in exported.entries.items() if k != "pyproject.toml"} == {
            k: v for k, v in public.entries.items() if k != "pyproject.toml"
        }
        if ours is None or theirs is None:
            return rest and ours == theirs
        return rest and _same_render(ours.data, theirs.data)


@dataclass(frozen=True, slots=True, kw_only=True)
class ImportRequest:
    """Inputs for a local-checkout change-request import."""

    config: WorkflowConfig
    public_base: Path
    public_head: Path
    source_base: Path
    destination: Path
    verify: bool = True
    merge_import: bool = False


def import_change_request(request: ImportRequest) -> ImportResult:
    """Import a public change request into a source-of-truth checkout.

    Args:
      request: Configuration specifying source, destination, and options.

    Returns:
      result: ImportResult with applied changes and outcomes.

    """
    return ChangeRequestImporter(
        config=request.config,
        public_base=request.public_base,
        public_head=request.public_head,
        source_base=request.source_base,
        destination=request.destination,
        verify=request.verify,
        merge_import=request.merge_import,
    ).import_changes()


def _matches_transform(
    transform: Transform,
    public_path: str,
    globstar: Globstar,
) -> bool:
    """Return whether a transform applies to a public path."""
    return GlobSet(include=(transform.path,), globstar=globstar).matches(public_path)


# A strip's regions are offsets into the text that strip saw on export, which is
# the source after EVERY earlier rule, reversible or not: a replace changes line
# lengths, a strip removes lines another strip's marker also matches. Replaying
# a subset placed a public edit on the wrong line and deleted the one it edited.
# ``move`` changes no text and ``ruff_format`` has no text-level form.
def _exported_until(
    source_text: str,
    *,
    transforms: tuple[Transform, ...],
    stop: Transform,
    match_path: str,
    globstar: Globstar,
) -> str:
    """Return ``source_text`` as export handed it to transform ``stop``."""
    text = source_text
    for earlier in transforms[: transforms.index(stop)]:
        if earlier.type in ("move", "ruff_format") or not _matches_transform(
            earlier,
            match_path,
            globstar,
        ):
            continue
        if earlier.type == "replace":
            text = _forward_replace(transform=earlier, text=text)
        elif earlier.type == "uncomment":
            text = uncomment_source_text(text, transform=earlier)[0]
        else:
            text = strip_source_text(text, earlier)
    return text


def _forward_replace(*, transform: Transform, text: str) -> str:
    """Apply one ``replace`` transform as export does."""
    compiled = replace_template(
        before=transform.before,
        after=transform.after,
        regex_groups=transform.regex_groups,
        module=transform.module,
    )
    if compiled is None:
        return text.replace(transform.before, transform.after)
    return compiled.apply(text)


# Forward, ``ruff_format`` targets ``root / transform.path`` and formats that whole
# SUBTREE (``transforms._ruff_format``): ``path = "pkg"`` formats every file under
# ``pkg/``, and ``path = "."`` / ``""`` formats the entire tree. So the import-side
# match must be a subtree test, not a literal glob match -- a glob on ``"pkg"`` matches
# only the path ``"pkg"`` itself, never ``pkg/foo.py`` (and a glob on ``"."`` matches
# nothing), which would silently skip the post-import reformat for every file under the
# target.
#
# Within that subtree ruff's directory walk formats only Python sources. The post-import
# reformat names each file, and an explicit path bypasses the walk: ruff then parses a
# JSON object as a Python dict and adds trailing commas no JSON parser accepts. So a
# subtree matches only ruff's default source suffixes, while a single-file target still
# matches itself, because forward passes it to ruff verbatim.
def _ruff_format_matches(transform: Transform, public_path: str) -> bool:
    """Return whether a ``ruff_format`` transform reformats a public path."""
    path = transform.path.removesuffix("/")
    if public_path == path:
        return True
    in_subtree = path in (".", "") or public_path.startswith(f"{path}/")
    return in_subtree and public_path.endswith((".py", ".pyi", ".ipynb"))


def _has_explicit_reversal(transform: Transform) -> bool:
    """Return whether a transform defines a custom public-to-source rewrite."""
    return bool(transform.reverse_before or transform.reverse_after)


# A ``regex_groups`` template is counted through its compiled pattern, so the declared
# boundary anchors apply. Counting the raw string instead treats ``${b}`` as literal and
# finds nothing, which silently retires the guard for exactly the rules whose anchors it
# needs to respect.
def _match_count(transform: Transform, template: str, text: str) -> int:
    """Count occurrences of one side of a transform in ``text``."""
    compiled = replace_template(
        before=template,
        after=template,
        regex_groups=transform.regex_groups,
        module=transform.module,
    )
    return text.count(template) if compiled is None else compiled.count(text)


def _reverse_before(transform: Transform) -> str:
    """Return text to find when reversing this transform."""
    if _has_explicit_reversal(transform):
        return transform.reverse_before
    return transform.after


def _reverse_after(transform: Transform) -> str:
    """Return text to write when reversing this transform."""
    if _has_explicit_reversal(transform):
        return transform.reverse_after
    return transform.before


# A ``regex_groups`` transform reverses by swapping its ``before`` and ``after``
# templates and re-running the same compiled-template machinery (Copybara
# ``Replace.reverse()``). The boundary anchors declared in the groups therefore apply
# symmetrically, so a non-injective literal token (a short public package name) is
# reversed only where the anchors say it is a real module reference -- never inside an
# identifier (``pkg_x``) or dotfile (``.pkg``). A plain literal transform falls back to
# ``str.replace``.
def _reverse_replace(*, transform: Transform, text: str) -> str:
    """Apply one transform's reverse replacement to public text."""
    reverse_before = _reverse_before(transform)
    reverse_after = _reverse_after(transform)
    compiled = replace_template(
        before=reverse_before,
        after=reverse_after,
        regex_groups=transform.regex_groups,
        module=transform.module,
    )
    if compiled is None:
        return text.replace(reverse_before, reverse_after)
    return compiled.apply(text)


def _reverse_replaces(
    content: bytes,
    *,
    transforms: list[Transform],
    public_path: str,
) -> bytes:
    """Reverse one contiguous run of ``replace`` transforms in a single pass."""
    if not transforms:
        return content
    return _reverse_replace_all(
        transforms=tuple(transforms),
        text=_decoded(content, public_path=public_path),
    ).encode()


# Applying reverse replacements one after another (each fed the previous one's output)
# is wrong whenever two forward transforms have overlapping ``after`` strings. Forward,
# a later transform only mops up text an earlier one did not consume; reversed, the
# earlier transform's reverse output becomes visible to the later transform's reverse,
# which re-matches it and doubles the rewrite. A namespace-rewrite pair is the canonical
# case: a bare package name mapped from its internal prefix, plus the dotted submodule
# form of the same rule. Sequentially, the bare reverse re-prefixes the token, and the
# dotted reverse then matches inside its own output and prefixes it a second time -- the
# doubled-prefix import that shipped.
#
# A single pass fixes this: scan the ORIGINAL public text once and, at each position,
# take the longest reverse-``before`` match among all transforms, emit its
# reverse-``after``, and advance past the consumed span. No transform ever sees
# another's output, so each public token is rewritten exactly once. Longest-match-wins
# makes the dotted rule (``wesearch.${s}``, which consumes the following identifier
# char) win over the bare rule (``wesearch``) at a shared start, reproducing the forward
# pipeline's precedence in reverse.
#
# ``transforms`` are the reversible ``replace`` transforms in reversed (public-to-
# source) order; ties at equal length break toward the earlier one in that order,
# matching the sequential precedence this replaces.
def _reverse_replace_all(*, transforms: tuple[Transform, ...], text: str) -> str:
    """Apply several reverse replacements in ONE simultaneous left-to-right pass."""
    if len(transforms) == 1:
        return _reverse_replace(transform=transforms[0], text=text)
    # Compile each reverse rule once: a regex_groups rule keeps its full
    # ReplaceTemplate (pattern + after_tokens) so a match renders without
    # recompiling; a literal rule matches its escaped reverse-before and emits
    # a constant reverse-after.
    matchers = tuple(_ReverseMatcher.build(transform) for transform in transforms)
    result: list[str] = []
    pos = 0
    length = len(text)
    while pos < length:
        best: tuple[int, int, _ReverseMatcher, re.Match[str]] | None = None
        for order, matcher in enumerate(matchers):
            match = matcher.pattern.match(text, pos)
            if match is None:
                continue
            span = match.end() - match.start()
            if span == 0:
                continue
            # Longest match wins; on a tie the earlier reversed-order transform
            # wins (lower ``order``), matching the sequential precedence.
            if best is None or span > best[0] or (span == best[0] and order < best[1]):
                best = (span, order, matcher, match)
        if best is None:
            result.append(text[pos])
            pos += 1
            continue
        _, _, matcher, match = best
        result.append(matcher.render(match))
        pos = match.end()
    return "".join(result)


@dataclass(frozen=True, slots=True, kw_only=True)
class _ReverseMatcher:
    """One reverse ``replace`` rule compiled once for the simultaneous pass."""

    pattern: re.Pattern[str]
    template: ReplaceTemplate | None
    literal_after: str

    @classmethod
    def build(cls, transform: Transform) -> _ReverseMatcher:
        """Compile a transform's reverse rule for repeated position matching.

        Args:
          transform: Forward transform to invert for reverse matching.

        Returns:
          result: _ReverseMatcher with compiled regex and template.

        """
        reverse_before = _reverse_before(transform)
        template = replace_template(
            before=reverse_before,
            after=_reverse_after(transform),
            regex_groups=transform.regex_groups,
            module=transform.module,
        )
        if template is not None:
            return cls(pattern=template.pattern, template=template, literal_after="")
        return cls(
            pattern=re.compile(re.escape(reverse_before)),
            template=None,
            literal_after=_reverse_after(transform),
        )

    def render(self, match: re.Match[str]) -> str:
        """Render this rule's reverse output for one match.

        Args:
          match: Regex match to expand through this rule's template.

        Returns:
          result: Rendered replacement string.

        """
        if self.template is None:
            return self.literal_after
        return self.template.render(match)


# Each entry is ``(offset_in_stripped, verbatim_text)``: the region's exact removed text
# and the offset it occupied within the *stripped* (exported) form. Derived from the
# SAME marker walk the export uses (``strip_source_regions``), so offsets and text agree
# with the real transform for every block shape -- inclusive and exclusive markers, mid-
# line markers, gap-collapsed blank lines -- with no second, drift-prone re-derivation.
#
# A strip transform that *rewrites* rather than deletes (``strip_block`` with an
# ``else`` branch uncomments and keeps the else lines) has no verbatim source-only
# region to re-insert: its exported bytes are a transformed form absent from source.
# Such a transform cannot be reversed by re-insertion, so this raises rather than
# fabricate a bogus region -- but only when the source file actually contains the block.
# A transform whose glob matches a file that carries no marker is a no-op on that file
# (nothing was rewritten), so it reverses trivially to zero regions; rejecting it would
# wrongly block importing an unrelated edit to any file the glob happens to match.
def _removed_regions(
    *,
    source_text: str,
    transform: Transform,
) -> list[tuple[int, str]]:
    """Return each source-only region a strip transform removes on export."""
    if transform.else_marker and transform.start and transform.start in source_text:
        raise ImportRequestError(
            f"Transform '{transform.id}' has an else branch and rewrites content "
            "on export; it cannot be reversed by re-insertion",
        )
    return list(strip_source_regions(source_text, transform)[1])


# Reverses ``strip_block`` / ``internal_lines`` by inserting each removed source region
# (block or line) back into ``public_text`` at the offset it held in the stripped
# (exported) form, right to left so earlier offsets stay valid. When public edits did
# not disturb the text around a region the offset lands exactly; when they did, the
# region is still restored (possibly displaced) and the import PR's CI is the human-
# review gate. Re-export removes the regions again, reproducing the public head.
def _splice_source_only_regions(
    *,
    source_text: str,
    public_text: str,
    transform: Transform,
) -> str:
    """Re-insert a strip transform's source-only regions into reversed text."""
    regions = _removed_regions(source_text=source_text, transform=transform)
    if not regions:
        return public_text
    result = public_text
    for offset, region_text in reversed(regions):
        insert_at = min(offset, len(result))
        result = result[:insert_at] + region_text + result[insert_at:]
    return result


# An else-branch ``strip_block`` does not delete a region: on export it REPLACES the
# whole ``start .. else .. end`` block with the else branch, uncommented
# (``_strip_blocks_with_else``). To reverse, each such block's exported form (its
# uncommented else lines) is located in the public text and replaced with the full
# source block verbatim -- restoring the internal branch and the markers. Re-export
# replays the same substitution, reproducing the public head.
#
# When the public edit rewrites the exported else body, align its original position,
# keep the source-only branch, and wrap the edited body back in the original markers.
# Re-export then unwraps that edited body exactly; no verification gate can recover a
# source-only branch after an importer silently drops it.
def _reverse_else_blocks(
    *,
    source_text: str,
    public_text: str,
    transform: Transform,
) -> str:
    """Reverse an ``if internal / else / endif`` strip block into ``public_text``."""
    whole_export = strip_source_text(source_text, transform=transform)
    public_lines = public_text.splitlines(keepends=True)
    replacements: list[tuple[int, int, str]] = []
    for source_block in _else_source_blocks(source_text, transform=transform):
        exported = _strip_blocks_with_else(source_block.text, transform=transform)[0]
        if not exported:
            continue
        lower, upper = _edited_block_bounds(
            public_text=public_text,
            whole_export=whole_export,
            exported_start=source_block.exported_start,
            exported_length=len(exported.splitlines(keepends=True)),
        )
        edited = "".join(public_lines[lower:upper])
        rebuilt = (
            source_block.text
            if edited == exported
            else _recomment_else_block(
                edited_body=edited,
                source_block=source_block.text,
                transform=transform,
            )
        )
        replacements.append((lower, upper, rebuilt))
    return _replace_line_spans(public_lines, replacements=replacements)


def _edited_block_bounds(
    *,
    public_text: str,
    whole_export: str,
    exported_start: int,
    exported_length: int,
) -> tuple[int, int]:
    """Return the public line span at one block's original export position."""
    export_lines = whole_export.splitlines(keepends=True)
    public_lines = public_text.splitlines(keepends=True)
    exported_stop = exported_start + exported_length
    matcher = difflib.SequenceMatcher(a=export_lines, b=public_lines, autojunk=False)
    for tag, a_start, a_stop, b_start, b_stop in matcher.get_opcodes():
        if a_start > exported_start or exported_stop > a_stop:
            continue
        same_width = a_stop - a_start == b_stop - b_start
        if tag == "equal" or (tag == "replace" and same_width):
            lower = b_start + exported_start - a_start
            return lower, lower + exported_length
    for tag, a_start, a_stop, _b_start, _b_stop in matcher.get_opcodes():
        if tag == "equal":
            continue
        if a_start < exported_start < a_stop or a_start < exported_stop < a_stop:
            raise ImportRequestError(
                "Cannot determine transformed block positional boundary",
            )
    # The block's own lines were edited past recognition, so bound it by the
    # nearest surviving line on each side. A block that still aligned line for
    # line would lie inside one ``equal`` opcode, which the first loop returned.
    aligned = _align_kept_to_public(export_lines, public_lines=public_lines)
    before = _max_aligned(aligned, start=0, stop=exported_start)
    after = _min_aligned(aligned, start=exported_stop, stop=len(aligned))
    # The alignment only increases, so ``after`` lies past ``before``.
    lower = 0 if before is None else before + 1
    upper = len(public_lines) if after is None else after
    return lower, upper


def _replace_line_spans(
    lines: list[str],
    *,
    replacements: list[tuple[int, int, str]],
) -> str:
    """Replace non-overlapping line spans from right to left."""
    result = list(lines)
    previous_start = len(lines) + 1
    for start, stop, replacement in reversed(replacements):
        if stop > previous_start:
            raise ImportRequestError("Cannot restore overlapping transformed blocks")
        result[start:stop] = [replacement]
        previous_start = start
    return "".join(result)


# The inline form is ONE line carrying both the body and a trailing marker, so it is
# rebuilt rather than bracketed: prepending the source line would keep the pre-edit body
# and append the edit beneath it, discarding the change.
def _recomment_block(edited_body: str, source_block: str, transform: Transform) -> str:
    """Wrap an edited public body back in its source markers, commented."""
    if not transform.end:
        body_lines = edited_body.splitlines()
        if not body_lines:
            return source_block
        return "".join(
            f"{_comment_line(f'{line}  {transform.start}')}\n" for line in body_lines
        )
    # ``_uncomment_source_blocks`` opens a block on ``start`` and closes it on ``end``.
    marker_lines = source_block.splitlines(keepends=True)
    commented = "".join(
        _comment_line(line) for line in edited_body.splitlines(keepends=True)
    )
    return f"{marker_lines[0]}{commented}{marker_lines[-1]}"


def _recomment_else_block(
    *,
    edited_body: str,
    source_block: str,
    transform: Transform,
) -> str:
    """Restore an else block while retaining its edited public branch."""
    # ``_else_source_blocks`` closes a block on ``end``, and ``_strip_blocks_with_else``
    # refuses one without its else marker, so both lines are found, in that order.
    lines = source_block.splitlines(keepends=True)
    else_index = next(
        index for index, line in enumerate(lines) if transform.else_marker in line
    )
    end_index = next(index for index, line in enumerate(lines) if transform.end in line)
    commented = "".join(
        _comment_line(line) for line in edited_body.splitlines(keepends=True)
    )
    return "".join((*lines[: else_index + 1], commented, *lines[end_index:]))


def _comment_line(line: str) -> str:
    """Prefix one line with ``# ``, preserving indentation and blank lines."""
    stripped = line.lstrip()
    if not stripped.strip():
        return line
    indent = line[: len(line) - len(stripped)]
    return f"{indent}# {stripped}"


# Source is the base, not the reversal: a reversal is a guess wherever a transform is
# not injective, and it has already lost any source line the export dropped. Diffing the
# pre-edit reversal against the post-edit one isolates what the contributor actually
# changed; each such hunk is mapped onto source through a second alignment and spliced
# there.
def _splice_public_edits(
    *,
    reversed_ours_lines: list[str],
    reversed_lines: list[str],
    source_lines: list[str],
) -> str:
    """Return source with only the public edit's hunks applied."""
    # pragma: no mutate start -- autojunk=None is falsy too.
    edit = difflib.SequenceMatcher(
        a=reversed_ours_lines,
        b=reversed_lines,
        autojunk=False,
    )
    # pragma: no mutate end
    ours_to_source = _align_kept_to_public(reversed_ours_lines, source_lines)
    # Source lines the export dropped, identified POSITIONALLY: the gap between two
    # export lines that are adjacent in the export yet apart in source. A gap that
    # also holds unaligned export lines is NOT a drop: the export rewrote those
    # lines (a reflow turns one export line into several source lines), so the
    # source lines are the rewritten form of the export lines beside them.
    anchored = [(i, s) for i, s in enumerate(ours_to_source) if s is not None]
    dropped = {
        index
        for (i, s), (j, t) in itertools.pairwise(anchored)
        if j == i + 1
        for index in range(s + 1, t)
    }
    # Mixed gaps as (source start, source stop, rewrite-only), where the flag says the
    # gap's source lines are exactly the export lines' tokens re-wrapped. A gap that
    # also holds loop-only lines is not, and replacing it would delete them.
    mixed = [
        (
            s + 1,
            t,
            _same_tokens(
                reversed_ours_lines[i + 1 : j],
                source=source_lines[s + 1 : t],
            ),
        )
        for (i, s), (j, t) in itertools.pairwise(anchored)
        if j > i + 1 and t > s + 1
    ]
    result: list[str] = []
    previous_end = -1
    for tag, a_start, a_stop, b_start, b_stop in edit.get_opcodes():
        if tag == "equal":
            continue
        before = [index for index in ours_to_source[:a_start] if index is not None]
        after = [index for index in ours_to_source[a_stop:] if index is not None]
        gap_start = before[-1] + 1 if before else 0
        gap_stop = after[0] if after else len(source_lines)
        hunk = ours_to_source[a_start:a_stop]
        span = [index for index in hunk if index is not None]
        # A hunk that starts or ends on an unaligned export line owns that line's
        # whole source gap. One that cuts into the middle of an unaligned run
        # cannot say which of the gap's source lines it replaces.
        opens_gap = bool(hunk) and hunk[0] is None
        closes_gap = bool(hunk) and hunk[-1] is None
        if gap_start < gap_stop and (
            (opens_gap and a_start > 0 and ours_to_source[a_start - 1] is None)
            or (
                closes_gap
                and a_stop < len(ours_to_source)
                and ours_to_source[a_stop] is None
            )
        ):
            raise ImportRequestError(
                "Public edit splits a line the export rewrote; cannot place it in source",
            )
        if a_start == a_stop:
            # A true insertion has no source span to replace: place it after the
            # preceding source text, or after the source gap of a preceding
            # unaligned export line.
            prior_unaligned = a_start > 0 and ours_to_source[a_start - 1] is None
            anchor = gap_stop if prior_unaligned else gap_start
            end = anchor - 1
        else:
            # A REPLACEMENT with nothing aligned was mis-reversed, so bound it by
            # the surrounding aligned lines -- inserting instead would emit the
            # edit AND keep the line it replaces.
            anchor = span[0] if span and not opens_gap else gap_start
            end = span[-1] if span and not closes_gap else gap_stop - 1
        if any(
            not rewrite_only and lo <= end and anchor < hi
            for lo, hi, rewrite_only in mixed
        ):
            raise ImportRequestError(
                "Public edit replaces a reflowed line beside lines the export "
                "dropped; cannot tell which source lines it owns",
            )
        result.extend(source_lines[previous_end + 1 : anchor])
        result.extend(reversed_lines[b_start:b_stop])
        result.extend(
            source_lines[index] for index in range(anchor, end + 1) if index in dropped
        )
        previous_end = end
    result.extend(source_lines[previous_end + 1 :])
    return "".join(result)


# A reflow only moves whitespace, commas and brackets (the formatter wraps a call and
# adds a trailing comma), so the same tokens in the same order identify a rewrite.
def _same_tokens(exported: list[str], *, source: list[str]) -> bool:
    """Return whether two line runs differ only by whitespace and wrapping."""
    drop = str.maketrans("", "", ",()[]{} \t\r\n")
    return "".join(exported).translate(drop) == "".join(source).translate(drop)


@dataclass(frozen=True, slots=True, kw_only=True)
class _SourceBlock:
    """One transformed source block and its original exported line position."""

    text: str
    exported_start: int


# A block form spans ``start``..``end``; the inline form is the single line carrying the
# marker. The exported position comes from the same transform as forward export, so a
# duplicate body elsewhere cannot steal the reversal.
def _uncomment_source_blocks(
    source_text: str,
    transform: Transform,
) -> list[_SourceBlock]:
    """Return source regions and their original exported line positions."""
    start = transform.start
    lines = source_text.splitlines(keepends=True)
    blocks: list[_SourceBlock] = []
    index = 0
    total = len(lines)
    while index < total:
        if transform.end and start in lines[index]:
            block_start = index
            index += 1
            while index < total and transform.end not in lines[index]:
                index += 1
            if index < total:
                index += 1
                exported_prefix, _count = uncomment_source_text(
                    "".join(lines[:block_start]),
                    transform=replace(transform, required=False),
                )
                blocks.append(
                    _SourceBlock(
                        text="".join(lines[block_start:index]),
                        exported_start=len(
                            exported_prefix.splitlines(keepends=True),
                        ),
                    ),
                )
            continue
        if not transform.end and line_has_marker_token(lines[index], start):
            exported_prefix, _count = uncomment_source_text(
                "".join(lines[:index]),
                transform=replace(transform, required=False),
            )
            blocks.append(
                _SourceBlock(
                    text=lines[index],
                    exported_start=len(exported_prefix.splitlines(keepends=True)),
                ),
            )
        index += 1
    return blocks


def _else_source_blocks(source_text: str, transform: Transform) -> list[_SourceBlock]:
    """Return else blocks and their original exported line positions."""
    start, end = transform.start, transform.end
    lines = source_text.splitlines(keepends=True)
    blocks: list[_SourceBlock] = []
    index = 0
    total = len(lines)
    while index < total:
        if start in lines[index]:
            block_start = index
            index += 1
            while index < total and end not in lines[index]:
                index += 1
            if index < total:
                index += 1  # `include` the end-marker line.
                exported_prefix = strip_source_text(
                    "".join(lines[:block_start]),
                    transform=replace(transform, required=False),
                )
                blocks.append(
                    _SourceBlock(
                        text="".join(lines[block_start:index]),
                        exported_start=len(
                            exported_prefix.splitlines(keepends=True),
                        ),
                    ),
                )
            continue
        index += 1
    return blocks


# No guessing: the source-only lines are exactly those ``strip_source_text`` removes, so
# the KEPT source lines are the source's own exported form. Diffing that exported form
# against the incoming public text (a real line alignment, which absorbs reflow via its
# matching blocks) says, for every kept source line, which public line it became. Each
# source-only run sits immediately after a kept source line; the run is re-inserted
# right after that line's ALIGNED public position. The correctness contract is then
# verified by the caller: re-stripping the result must reproduce public exactly.
#
# A run whose preceding kept line did not align to any public line (its context was
# deleted/rewritten past recognition) has no determined position -- this returns
# ``None`` so the caller rejects rather than guess.
def _anchor_source_only_regions(
    *,
    source_text: str,
    public_text: str,
    transform: Transform,
) -> str | None:
    """Re-insert source-only regions by aligning the source's export with public."""
    source_lines = source_text.splitlines(keepends=True)
    marks = _source_only_line_mask(source_lines=source_lines, transform=transform)
    if not any(marks):
        return _splice_source_only_regions(
            source_text=source_text,
            public_text=public_text,
            transform=transform,
        )
    kept_lines = [
        line for line, mark in zip(source_lines, marks, strict=True) if not mark
    ]
    public_lines = public_text.splitlines(keepends=True)
    # Map each kept-source line index -> aligned public line index (or None).
    kept_to_public = _align_kept_to_public(kept_lines, public_lines)

    # Each source-only run sits between kept-source line ``kept_seen - 1`` (before)
    # and ``kept_seen`` (after). Its slot survived ONLY when its IMMEDIATE
    # bracketing kept lines still align AND their public positions are adjacent --
    # i.e. the run's original gap is still an intact gap in public. Placement uses
    # only that intact-slot signal; a run whose local context was rewritten (an
    # immediate neighbor is unaligned, or the aligned neighbors straddle a rewrite)
    # has an UNDETERMINED position and is rejected, rather than being dropped next
    # to a far-away aligned line in a different scope (a mis-placement the re-strip
    # gate cannot catch, since the marker line strips away wherever it lands).
    # Compute each run's public insertion index, recording runs in SOURCE order.
    insertions: list[tuple[int, str]] = []
    kept_seen = 0
    index = 0
    total = len(source_lines)
    for_public_count = len(public_lines)
    while index < total:
        if not marks[index]:
            kept_seen += 1
            index += 1
            continue
        start = index
        while index < total and marks[index]:
            index += 1
        run_text = "".join(source_lines[start:index])
        insert_at = _placement_index(
            kept_to_public=kept_to_public,
            before_index=kept_seen - 1,
            after_index=kept_seen,
            public_line_count=for_public_count,
        )
        # Detachment guard: if the run's immediate following kept line did NOT
        # align (difflib dropped it) yet its content still exists in public BEFORE
        # the chosen insert point, public reordered it ahead of the run -- placing
        # the run here would detach it from that neighbor. Reject rather than
        # silently emit the run in the wrong place (re-strip cannot catch it).
        if kept_seen < len(kept_to_public) and kept_to_public[kept_seen] is None:
            follower = kept_lines[kept_seen]
            if follower in public_lines[:insert_at]:
                return None
        insertions.append((insert_at, run_text))

    # Insert forward, tracking a cumulative offset, so runs that share the same
    # public index keep their SOURCE order (a bottom-up splice would reverse them).
    result = list(public_lines)
    for offset, (public_index, run_text) in enumerate(insertions):
        at = public_index + offset
        result[at:at] = [run_text]
    return "".join(result)


# ``before_index`` / ``after_index`` are the kept-line indices immediately before and
# after the run. Placement is decided by alignment only:
#
# - Immediate before-neighbor aligns: insert right after its public line.
# - Else the run's preceding context was rewritten; anchor FORWARD to the nearest
# aligned kept line at/after the run and insert right before it, so the run lands at the
# tail of the rewritten span it belonged to. Forward-only avoids jumping BACKWARD across
# a rewritten region into an earlier, unrelated scope (a mis-placement the re-strip gate
# cannot catch). - No aligned line before or forward: the run is trailing (nothing
# survived after it), so append at end -- an unambiguous position.
def _placement_index(
    *,
    kept_to_public: list[int | None],
    before_index: int,
    after_index: int,
    public_line_count: int,
) -> int:
    """Return the public insert index for a source-only run."""
    # The run lands in the public gap bracketed by its context: strictly after
    # every kept line BEFORE it that aligns, and at/before every kept line AFTER
    # it that aligns. The alignment only increases, so that gap is never empty; a
    # public reorder shows up as a neighbor that did not align, which the caller's
    # detachment guard rejects.
    after_min = _min_aligned(kept_to_public, after_index, len(kept_to_public))
    if after_min is None:
        # No kept line aligns AFTER the run: it is trailing. Append at the end --
        # the only unambiguous position when nothing survived after it.
        return public_line_count
    before_aligned = kept_to_public[before_index] if before_index >= 0 else None
    if before_aligned is not None:
        # Immediate preceding line survived: keep the run right after it.
        return before_aligned + 1
    # The immediate preceding line was rewritten but a later line aligns: land the
    # run right before that nearest aligned following line (tail of the rewritten
    # span it belonged to).
    return after_min


def _max_aligned(kept_to_public: list[int | None], start: int, stop: int) -> int | None:
    """Return the greatest aligned public index in ``kept_to_public[start:stop]``."""
    values = [v for v in kept_to_public[start:stop] if v is not None]
    return max(values) if values else None


def _min_aligned(kept_to_public: list[int | None], start: int, stop: int) -> int | None:
    """Return the smallest aligned public index in ``kept_to_public[start:stop]``."""
    values = [v for v in kept_to_public[start:stop] if v is not None]
    return min(values) if values else None


# Uses ``difflib.SequenceMatcher`` matching blocks: lines in an 'equal' block map one-
# to-one to their public counterparts. A kept line inside a 'replace' or 'delete' block
# (its public counterpart was rewritten or removed) maps to ``None``.
def _align_kept_to_public(
    kept_lines: list[str],
    public_lines: list[str],
) -> list[int | None]:
    """Return, per kept-source line, the aligned public line index or ``None``."""
    matcher = difflib.SequenceMatcher(a=kept_lines, b=public_lines, autojunk=False)
    aligned: list[int | None] = [None] * len(kept_lines)
    for a_start, b_start, size in matcher.get_matching_blocks():
        for offset in range(size):
            aligned[a_start + offset] = b_start + offset
    return aligned


# Read off the export's own walk rather than a second marker scan: an inclusive cut also
# takes the blank lines after its block, which a marker scan counted as kept. Absent from
# public, each aligned to nothing, so the run before it was rejected as detached, or was
# re-inserted while the blank line silently vanished from source. A line the cut only
# partly covers (a mid-line marker) stays kept; it matches no public line either way.
def _source_only_line_mask(
    *,
    source_lines: list[str],
    transform: Transform,
) -> list[bool]:
    """Return a per-line mask of which source lines the transform removes."""
    # Each region's offset is into the stripped text; the regions removed before
    # it shift that back to its source offset.
    spans: list[tuple[int, int]] = []
    shift = 0
    for offset, text in strip_source_regions("".join(source_lines), transform)[1]:
        spans.append((offset + shift, offset + shift + len(text)))
        shift += len(text)
    mask: list[bool] = []
    line_start = 0
    for line in source_lines:
        line_end = line_start + len(line)
        mask.append(any(lo <= line_start and line_end <= hi for lo, hi in spans))
        line_start = line_end
    return mask


def _with_outcome(change: ImportChange, outcome: ChangeOutcome) -> ImportChange:
    """Return a copy of a change annotated with a merge outcome."""
    return replace(change, outcome=outcome)


# The rule ``fold`` checks its own output by, so a pyproject this accepts is one a
# fold can have produced.
def _same_render(left: bytes, right: bytes) -> bool:
    """Return whether two ``pyproject.toml`` files differ at most in spelling."""
    try:
        return same_render(left.decode(), right.decode())
    except (UnicodeDecodeError, DeltaError):
        return left == right


# Shells to ``git merge-file``, the diff3 engine Copybara's ``MergeImportTool`` uses.
# The argument orientation matches Copybara exactly: it runs ``diff3 -m origin baseline
# destination`` (``CommandLineDiffUtil.merge``), treating the incoming source-of-truth
# change as the primary (``ours``) side and the local checkout as ``theirs``. Here
# ``incoming`` is the public head (the SoT change) and ``current`` is the local source,
# so ``incoming`` is passed first. Conflicting hunks keep both sides wrapped in conflict
# markers.
def _three_way_merge(
    *,
    current: bytes,
    base: bytes,
    incoming: bytes,
) -> tuple[bytes, bool]:
    """Three-way merge ``incoming`` onto ``current`` relative to ``base``."""
    git = shutil.which("git")
    if git is None:
        raise ImportRequestError("Three-way merge requires git on PATH.")
    with tempfile.TemporaryDirectory(prefix="copybarista-merge-") as tmp:
        root = Path(tmp)
        incoming_path = root / "incoming"
        base_path = root / "base"
        current_path = root / "current"
        incoming_path.write_bytes(incoming)
        base_path.write_bytes(base)
        current_path.write_bytes(current)
        result = subprocess.run(  # noqa: S603 -- fixed argv, no shell.
            [
                git,
                "merge-file",
                "-p",
                # --diff3 keeps the base section in conflict hunks, byte-matching
                # Copybara's plain ``diff3 -m`` output (CommandLineDiffUtil).
                "--diff3",
                "-L",
                "public",
                "-L",
                "base",
                "-L",
                "source",
                str(incoming_path),
                str(base_path),
                str(current_path),
            ],
            capture_output=True,
            check=False,
        )
        # ``git`` merge-file returns the capped conflict count (0-127); a clean merge
        # is 0. Values >=128 signal a fatal error (e.g. a binary file it cannot
        # merge), so the count can never collide with the error range.
        if result.returncode >= 128:
            raise ImportRequestError(
                f"Three-way merge failed: git merge-file exited "
                f"{result.returncode}: {result.stderr.decode(errors='replace')}",
            )
        return result.stdout, result.returncode > 0


def _read_import_text(*, path: Path, label: str) -> str:
    """Read import text while preserving caller-specific error context."""
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as err:
        raise ImportRequestError(label) from err


def _decoded(data: bytes, *, public_path: str) -> str:
    """Decode public bytes that a text reversal must rewrite."""
    try:
        return data.decode()
    except UnicodeDecodeError as err:
        raise ImportRequestError(
            f"Public path requires text reversal but is not UTF-8: {public_path}",
        ) from err


def _delete_path(path: Path) -> None:
    """Delete a file, symlink, or directory if present."""
    if not path.exists() and not path.is_symlink():
        return
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def _write_symlink(
    *,
    public_path: Path,
    target: Path,
    public_root: Path,
    destination_root: Path,
) -> None:
    """Copy a relative public symlink into the source checkout."""
    link = public_path.readlink()
    if link.is_absolute():
        raise ImportRequestError(f"Symlink target escapes import tree: {public_path}")
    public_target = (public_path.parent / link).resolve(strict=False)
    if not public_target.is_relative_to(public_root.resolve()):
        raise ImportRequestError(f"Symlink target escapes import tree: {public_path}")
    _delete_path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.symlink_to(link)
    _validated_target(
        destination=destination_root,
        relative_path=target.relative_to(destination_root).as_posix(),
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class _OriginalPath:
    """Original destination path state captured for import rollback."""

    path: Path
    backup: Path | None


def _capture_originals(
    *,
    destination: Path,
    changes: tuple[ImportChange, ...],
) -> tuple[_OriginalPath, ...]:
    """Snapshot touched destination paths before applying an import plan."""
    originals: list[_OriginalPath] = []
    captured: set[Path] = set()
    replaced_ancestors: list[Path] = []
    backup_root: Path | None = None
    try:
        for idx, change in enumerate(changes):
            target = _relative_target(
                destination=destination,
                relative_path=change.source,
            )
            if any(
                target != ancestor and target.is_relative_to(ancestor)
                for ancestor in replaced_ancestors
            ):
                continue
            path = _validated_target(
                destination=destination,
                relative_path=change.source,
            )
            if path.exists() or path.is_symlink():
                if backup_root is None:
                    backup_root = Path(
                        tempfile.mkdtemp(prefix="copybarista-import-backup-"),
                    )
                backup = backup_root / str(idx)
                if path.is_dir() and not path.is_symlink():
                    shutil.copytree(path, dst=backup, symlinks=True)
                else:
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, dst=backup, follow_symlinks=False)
                original = _OriginalPath(path=path, backup=backup)
            else:
                original = _OriginalPath(
                    path=_highest_absent_path(destination=destination, target=path),
                    backup=None,
                )
            if original.path not in captured:
                originals.append(original)
                captured.add(original.path)
            if change.action == "type_changed":
                replaced_ancestors.append(path)
    except Exception:
        if backup_root is not None:
            shutil.rmtree(backup_root, ignore_errors=True)
        raise
    return tuple(originals)


def _highest_absent_path(*, destination: Path, target: Path) -> Path:
    """Return the first absent path from ``destination`` toward ``target``, itself absent."""
    parts = target.relative_to(destination).parts
    return next(
        candidate
        for candidate in (
            destination.joinpath(*parts[: index + 1]) for index in range(len(parts))
        )
        if not candidate.exists() and not candidate.is_symlink()
    )


def _restore_originals(originals: tuple[_OriginalPath, ...]) -> None:
    """Restore destination paths captured before a failed import."""
    for original in reversed(originals):
        _delete_path(original.path)
        if original.backup is None:
            continue
        original.path.parent.mkdir(parents=True, exist_ok=True)
        if original.backup.is_dir() and not original.backup.is_symlink():
            shutil.copytree(original.backup, dst=original.path, symlinks=True)
        else:
            shutil.copy2(original.backup, dst=original.path, follow_symlinks=False)


def _discard_backups(originals: tuple[_OriginalPath, ...]) -> None:
    """Remove the rollback snapshot tree, on success and on failure alike."""
    for parent in {
        original.backup.parent for original in originals if original.backup is not None
    }:
        shutil.rmtree(parent, ignore_errors=True)


# Imports mutate an existing checkout, so the root must already be a real directory and
# must not be a symlink, filesystem root, home directory, or VCS metadata path.
def _validate_snapshot_root(root: Path, *, label: str) -> None:
    """Require an explicit, readable tree root before diff planning."""
    if root.is_symlink() or not root.is_dir():
        raise ImportRequestError(f"{label} root must be an existing directory: {root}")


def _validate_import_destination(destination: Path) -> None:
    """Reject destination roots where import writes would be unsafe."""
    if destination.is_symlink():
        raise ImportRequestError(f"Refusing symlink destination: {destination}")
    if not destination.is_dir():
        raise ImportRequestError(f"Import destination must exist: {destination}")
    resolved = destination.resolve()
    home = Path.home().resolve()  # noqa: TID251 -- vendor fixed path, not ours (AGENTS.md rule 3)  # house-ignore[xdg-literal] -- Safety check against deleting $HOME itself, not a path we own.
    if resolved in {Path("/").resolve(), home}:
        raise ImportRequestError(f"Refusing dangerous destination: {destination}")
    if VCS_DIRS.intersection(resolved.parts):
        raise ImportRequestError(f"Refusing VCS metadata destination: {destination}")


# This guards every write/delete target: public paths may not be metadata, absolute,
# contain `..`, pass through symlink ancestors, or resolve outside the destination
# checkout.
def _validated_target(*, destination: Path, relative_path: str) -> Path:
    """Return a destination target after escape and metadata checks."""
    target = _relative_target(
        destination=destination,
        relative_path=relative_path,
    )
    root = destination.resolve()
    relative = Path(relative_path)
    current = destination
    for part in relative.parts[:-1]:
        current = current / part
        if current.is_symlink():
            raise ImportRequestError(
                f"Import target escapes destination: {relative_path}",
            )
    parent = target.parent.resolve(strict=False)
    if not parent.is_relative_to(root):
        raise ImportRequestError(f"Import target escapes destination: {relative_path}")
    if target.exists() or target.is_symlink():
        try:
            if not target.resolve(strict=False).is_relative_to(root):
                raise ImportRequestError(
                    f"Import target escapes destination: {relative_path}",
                )
        except RuntimeError as err:
            raise ImportRequestError(
                f"Import target cannot be resolved: {relative_path}",
            ) from err
    return target


def _relative_target(*, destination: Path, relative_path: str) -> Path:
    """Return a lexically safe target without resolving existing ancestors."""
    if _is_metadata_path(relative_path):
        raise ImportRequestError(
            f"Public path is excluded or unmapped: {relative_path}",
        )
    relative = Path(relative_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise ImportRequestError(f"Import target escapes destination: {relative_path}")
    return destination / relative


def _raise_snapshot_error(error: OSError) -> None:
    """Fail closed when a snapshot walk cannot inspect part of the tree."""
    raise ImportRequestError(f"Cannot snapshot tree: {error}") from error


def _entry_parent_paths(entries: dict[str, TreeEntry]) -> set[str]:
    """Return directory paths implied by file-like snapshot entries."""
    return {
        PurePosixPath(*parts[:index]).as_posix()
        for path in entries
        for parts in (PurePosixPath(path).parts,)
        for index in range(1, len(parts))
    }


def _has_parent(path: str, *, parents: set[str]) -> bool:
    """Return whether one strict parent of ``path`` belongs to ``parents``."""
    parts = PurePosixPath(path).parts
    return any(
        PurePosixPath(*parts[:index]).as_posix() in parents
        for index in range(1, len(parts))
    )


def _validate_directory_replacement_owners(
    *,
    public_base: TreeSnapshot,
    public_head: TreeSnapshot,
    source_owners: dict[str, str],
    mapper: PathMapper,
) -> None:
    """Reject directory replacements assembled from another source subtree."""
    replaced_directories = _entry_parent_paths(public_base.entries).intersection(
        public_head.entries,
    )
    for public_root in sorted(replaced_directories):
        replacement_source = PurePosixPath(
            mapper.source_path(public_root),
        )
        for public_path, owner in source_owners.items():
            path = PurePosixPath(public_path)
            if not path.is_relative_to(public_root) or path == PurePosixPath(
                public_root,
            ):
                continue
            source_path = PurePosixPath(owner)
            if source_path.is_relative_to(replacement_source):
                continue
            raise ImportRequestError(
                f"Cannot replace public directory '{public_root}': descendant "
                f"'{public_path}' has split forward owners",
            )
