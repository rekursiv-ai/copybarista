"""Change-request import support for public repository edits.

The importer computes the public diff, maps paths back to the source-of-truth
checkout, reverses supported transforms, writes a review branch checkout, and
re-exports to prove the public tree is reproduced.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from os import walk
from pathlib import Path, PurePosixPath
from typing import Literal

import shutil
import stat
import subprocess
import sys
import tempfile

from copybarista.commands import CommandRunner
from copybarista.config import Transform, WorkflowConfig
from copybarista.errors import ImportRequestError
from copybarista.export import export_folder
from copybarista.globs import GlobSet, Globstar
from copybarista.template import compile_replace


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
        entries: dict[str, TreeEntry] = {}
        for current, dirnames, filenames in walk(root):
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
        """Return created, deleted, modified, and type-changed paths."""
        changes: list[TreeChange] = []
        for path in sorted(set(self.entries) | set(other.entries)):
            before = self.entries.get(path)
            after = other.entries.get(path)
            if before is None:
                changes.append(TreeChange(path=path, action="created"))
            elif after is None:
                changes.append(TreeChange(path=path, action="deleted"))
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
    destination_prefix_exclude: GlobSet | None = field(init=False)

    def __post_init__(self) -> None:
        """Compile file selection once for all changed paths."""
        object.__setattr__(
            self,
            "matcher",
            GlobSet(
                include=self.config.files.include,
                exclude=self.config.files.exclude,
                globstar=self.config.globstar,
            ),
        )
        object.__setattr__(
            self,
            "destination_prefix_exclude",
            GlobSet(
                include=self.config.files.destination_prefix_exclude,
                globstar=self.config.globstar,
            )
            if self.config.files.destination_prefix_exclude
            else None,
        )

    def source_path(self, public_path: str) -> str:
        """Return the source-relative path for a public path.

        Args:
          public_path: Public repository path relative to the exported root.

        Returns:
          source_path: Source checkout path relative to the source root.

        Raises:
          ImportRequestError: If the path is excluded, metadata, or unmapped.

        """
        if _is_metadata_path(public_path):
            raise ImportRequestError(
                f"Public path is excluded or unmapped: {public_path}"
            )
        source_public_path = _reverse_move_path(
            public_path=public_path,
            transforms=self.config.transforms,
        )
        if self._is_generated_path(source_public_path):
            raise ImportRequestError(
                f"Public path is excluded or unmapped: {public_path}"
            )
        copied_source = self._copied_source_path(source_public_path)
        if copied_source:
            return copied_source
        source_rel = self._source_relative_path(source_public_path)
        if not self.matcher.matches(source_rel):
            raise ImportRequestError(
                f"Public path is excluded or unmapped: {public_path}"
            )
        if not self.config.source_root:
            return source_rel
        return f"{self.config.source_root}/{source_rel}"

    def _is_generated_path(self, public_path: str) -> bool:
        """Return whether `public_path` was generated by `[[files.write]]`."""
        return any(
            public_path == file_write.path for file_write in self.config.files.write
        )

    def _copied_source_path(self, public_path: str) -> str:
        """Map a public path produced by `[[files.copy]]` back to its source."""
        for file_copy in self.config.files.copy:
            matcher = GlobSet(
                include=file_copy.include,
                exclude=file_copy.exclude,
                globstar=self.config.globstar,
            )
            if public_path == file_copy.destination:
                if matcher.matches(Path(file_copy.source).name):
                    return file_copy.source
                raise ImportRequestError(
                    f"Public path is excluded or unmapped: {public_path}"
                )
            prefix = f"{file_copy.destination}/"
            if not public_path.startswith(prefix):
                continue
            rel = public_path.removeprefix(prefix)
            if matcher.matches(rel):
                return f"{file_copy.source}/{rel}"
            raise ImportRequestError(
                f"Public path is excluded or unmapped: {public_path}"
            )
        return ""

    def _source_relative_path(self, public_path: str) -> str:
        """Strip the configured destination prefix from a public path."""
        prefix = self.config.files.destination_prefix
        if not prefix:
            return public_path
        prefix_path = f"{prefix}/"
        if public_path.startswith(prefix_path):
            return public_path.removeprefix(prefix_path)
        if (
            self.destination_prefix_exclude is not None
            and self.destination_prefix_exclude.matches(public_path)
        ):
            return public_path
        raise ImportRequestError(f"Public path is excluded or unmapped: {public_path}")


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
        """Return a JSON-serializable report."""
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
            ]
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
        """Build and validate the import plan."""
        _validate_import_destination(self.destination)
        if self.verify and not self.merge_import:
            self._check_public_base()
        diff = TreeSnapshot.from_root(self.public_base).diff(
            TreeSnapshot.from_root(self.public_head)
        )
        mapper = PathMapper(config=self.config)
        changes = tuple(
            ImportChange(
                public=change.path,
                source=mapper.source_path(change.path),
                action=change.action,
                transforms=self._reverse_transform_ids(change.path),
            )
            for change in diff.changes
        )
        return ImportPlan(changes=changes)

    def import_changes(self) -> ImportResult:
        """Apply the public diff to the destination checkout."""
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
                    self._check_public_head()
        except Exception:
            _restore_originals(originals)
            raise
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
        if public_path.is_dir():
            raise ImportRequestError(f"Cannot import directory change: {change.public}")
        head_data = self._reverse_content(
            public_path=change.public, data=public_path.read_bytes()
        )
        if change.action == "type_changed" or target.is_symlink():
            _delete_path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(head_data)
        shutil.copymode(public_path, target)
        self._reformat_imported_source(change=change, target=target)
        return change

    def _reformat_imported_source(self, *, change: ImportChange, target: Path) -> None:
        """Re-run ``ruff_format`` forward on a freshly reversed source file.

        ``ruff_format`` has no content inverse, so ``_reverse_content`` skips it
        on import. But a namespace ``replace`` reverses by pure text
        substitution that preserves physical line order: a public file whose
        imports are sorted under the *public* namespace can land unsorted under
        the *source* namespace whenever the two namespaces sort their import
        groups differently. Re-applying ruff (isort + format) forward on the
        written file restores source-namespace order, so importing never
        pollutes the source tree with lint violations.

        Runs with ``cwd=self.destination`` so ruff discovers the source tree's
        own config (e.g. ``known-first-party``), and only when a ``ruff_format``
        transform's glob matches this change's public path.
        """
        if not any(
            transform.type == "ruff_format"
            and _matches_transform(transform, change.public, self.config.globstar)
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

    def _merge_changes(
        self, changes: tuple[ImportChange, ...]
    ) -> tuple[ImportChange, ...]:
        """Reconcile every change by three-way merge, raising on conflicts.

        Merges happen in PUBLIC space: the current source is exported once, then
        each file is merged as ``diff3(source_export, public_base, public_head)``
        and the result reversed back to source form. Merging publicly keeps the
        non-reversible direction (e.g. ``strip_block``) out of the common
        already-applied path -- a file whose export already equals the public
        head needs no reversal at all.
        """
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
                    change=change, source_export=source_export
                )
                applied.append(resolved)
                if conflicted:
                    conflicts.append(resolved.source)
        if conflicts:
            raise ImportRequestError(
                "Merge import produced conflicts in: " + ", ".join(sorted(conflicts))
            )
        return tuple(applied)

    def _merge_change(
        self, *, change: ImportChange, source_export: Path
    ) -> tuple[ImportChange, bool]:
        """Reconcile one change by three-way merge in public space.

        Mirrors Copybara's ``MergeImportTool``: the public head is the incoming
        change, the public base is the merge baseline, and the source's exported
        form is the local side. A source already at head is a no-op
        (``skipped``); independent text drift merges cleanly (``merged``);
        overlapping edits record a conflict for the caller to surface. Deletions,
        symlinks, and directory changes are not text-mergeable: they are
        force-propagated from the public head via ``_apply_change`` (matching
        Copybara, which propagates origin deletions regardless of destination
        drift).

        Returns:
          resolved: The change annotated with its merge outcome.
          conflicted: Whether the three-way merge produced conflict markers.

        """
        target = _validated_target(
            destination=self.destination, relative_path=change.source
        )
        public_path = self.public_head / change.public
        if (
            change.action == "deleted"
            or public_path.is_symlink()
            or public_path.is_dir()
        ):
            return self._apply_change(change), False
        head_public = public_path.read_bytes()
        ours_public = (
            (source_export / change.public).read_bytes()
            if (source_export / change.public).is_file()
            else b""
        )
        if ours_public == head_public:
            return _with_outcome(change, "skipped"), False
        base_path = self.public_base / change.public
        base_public = (
            base_path.read_bytes()
            if base_path.is_file() and not base_path.is_symlink()
            else b""
        )
        if ours_public == base_public:
            return self._apply_change(change), False
        merged_public, conflicted = _three_way_merge(
            current=ours_public, base=base_public, incoming=head_public
        )
        merged_source = self._reverse_content(
            public_path=change.public, data=merged_public
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(merged_source)
        shutil.copymode(public_path, target)
        self._reformat_imported_source(change=change, target=target)
        return _with_outcome(change, "merged"), conflicted

    def _reverse_content(self, *, public_path: str, data: bytes) -> bytes:
        """Undo supported content transforms for one public file."""
        content = data
        match_path = _reverse_move_path(
            public_path=public_path,
            transforms=self.config.transforms,
        )
        for transform in reversed(self.config.transforms):
            if transform.type in ("move", "ruff_format") or not transform.reversible:
                continue
            if not _matches_transform(transform, match_path, self.config.globstar):
                continue
            if transform.type == "strip_block":
                if self._strip_block_is_noop(
                    public_path=public_path, transform=transform
                ):
                    # The source file has no block for this transform, so the
                    # strip removed nothing on export and the public content
                    # reverses unchanged. Skip it (copybara treats no-op
                    # transforms as no-ops, not errors).
                    continue
                raise ImportRequestError(
                    f"Public path requires non-reversible transform "
                    f"'{transform.id}': {public_path}"
                )
            try:
                text = content.decode()
            except UnicodeDecodeError as err:
                raise ImportRequestError(
                    f"Public path requires text reversal but is not UTF-8: "
                    f"{public_path}"
                ) from err
            # A regex_groups transform anchors its reverse symmetrically, so the
            # reversal is unambiguous by construction -- no heuristic guard.
            # For a plain literal transform, strict imports use this guard as a
            # proxy for "is the reversal unambiguous"; merge imports establish
            # that ground truth directly by comparing the source's actual export
            # to the public base/head, so the heuristic is redundant and wrong
            # there (the source legitimately carries exported text from drift).
            if not transform.regex_groups and not self.merge_import:
                self._check_injective_reverse(
                    public_path=public_path,
                    transform=transform,
                    text=text,
                )
            content = _reverse_replace(transform=transform, text=text).encode()
        return content

    def _check_injective_reverse(
        self, *, public_path: str, transform: Transform, text: str
    ) -> None:
        """Reject automatic reversals that cannot be mapped back unambiguously."""
        reverse_before = _reverse_before(transform)
        if not reverse_before:
            raise ImportRequestError(
                f"Public path requires non-reversible empty replacement "
                f"for transform '{transform.id}': {public_path}"
            )
        if _has_explicit_reversal(transform):
            return
        source_path = self.source_base / _source_path(
            config=self.config,
            public_path=public_path,
        )
        if source_path.exists() and not source_path.is_symlink():
            source_text = _read_import_text(
                path=source_path,
                label=f"Source base path is not UTF-8: {source_path}",
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
            before_text = transform.before
            explained = (
                source_text.count(before_text) * before_text.count(reverse_before)
                if before_text and reverse_before in before_text
                else 0
            )
            if source_text.count(reverse_before) > explained:
                raise ImportRequestError(
                    f"Source base already contains exported replacement text "
                    f"for transform '{transform.id}': {public_path}"
                )
        base_path = self.public_base / public_path
        if base_path.exists() and not base_path.is_symlink():
            base_text = _read_import_text(
                path=base_path,
                label=f"Public base path is not UTF-8: {public_path}",
            )
            base_count = base_text.count(reverse_before)
        else:
            base_count = 0
        if text.count(reverse_before) > base_count:
            raise ImportRequestError(
                f"Public path adds exported replacement text for transform "
                f"'{transform.id}': {public_path}"
            )

    def _reverse_transform_ids(self, public_path: str) -> tuple[str, ...]:
        """Return reversible transform IDs that affect a public path.

        In merge mode a non-reversible match is not fatal here: a file whose
        export already matches the public head is reconciled without any
        reversal, so the decision is deferred to ``_reverse_content``.
        """
        ids: list[str] = []
        match_path = _reverse_move_path(
            public_path=public_path,
            transforms=self.config.transforms,
        )
        for transform in reversed(self.config.transforms):
            if transform.type in ("move", "ruff_format") or not transform.reversible:
                continue
            if _matches_transform(transform, match_path, self.config.globstar):
                if transform.type == "strip_block":
                    if self._strip_block_is_noop(
                        public_path=public_path, transform=transform
                    ):
                        continue
                    if self.merge_import:
                        continue
                    raise ImportRequestError(
                        f"Public path requires non-reversible transform "
                        f"'{transform.id}': {public_path}"
                    )
                ids.append(transform.id)
        return tuple(ids)

    def _strip_block_is_noop(self, *, public_path: str, transform: Transform) -> bool:
        """Return whether a strip_block transform did nothing to this path.

        A strip_block removes a source-only block on export; it is only
        non-reversible when the SOURCE file actually contains that block (the
        removed content is absent from the public tree and cannot be
        reconstructed). When the source file has no start marker -- e.g. public
        sample code that merely matches the transform's glob -- the strip
        removed nothing, so importing the public change back is loss-free.

        This mirrors Copybara, which classifies a transform that changes
        nothing as a no-op rather than an error (Replace.java / FilterReplace.java:
        ``TransformationStatus.noop(... "was a no-op because it didn't ...")``).
        """
        source_path = self.source_base / _source_path(
            config=self.config,
            public_path=public_path,
        )
        if not source_path.exists() or source_path.is_symlink():
            return True
        source_text = _read_import_text(
            path=source_path,
            label=f"Source base path is not UTF-8: {source_path}",
        )
        return transform.start not in source_text

    def _check_public_base(self) -> None:
        """Verify the supplied source base reproduces the public base tree."""
        with tempfile.TemporaryDirectory(prefix="copybarista-import-base-") as tmp:
            exported = Path(tmp) / "public-base"
            export_folder(
                config=self.config,
                source_ref=self.source_base,
                destination=exported,
                force=True,
            )
            if TreeSnapshot.from_root(exported) != TreeSnapshot.from_root(
                self.public_base
            ):
                raise ImportRequestError(
                    "Configured source base does not reproduce public base"
                )

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
            if TreeSnapshot.from_root(exported) != TreeSnapshot.from_root(
                self.public_head
            ):
                raise ImportRequestError(
                    "Imported source tree does not reproduce public head"
                )


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
    """Import a public change request into a source-of-truth checkout."""
    return ChangeRequestImporter(
        config=request.config,
        public_base=request.public_base,
        public_head=request.public_head,
        source_base=request.source_base,
        destination=request.destination,
        verify=request.verify,
        merge_import=request.merge_import,
    ).import_changes()


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


def _reverse_move_path(*, public_path: str, transforms: tuple[Transform, ...]) -> str:
    """Map a post-move public path back to the pre-move staged path."""
    path = PurePosixPath(public_path)
    for transform in reversed(transforms):
        if transform.type != "move":
            continue
        destination = PurePosixPath(transform.destination)
        if path == destination:
            path = PurePosixPath(transform.path)
        elif path.is_relative_to(destination):
            path = PurePosixPath(transform.path) / path.relative_to(destination)
    return path.as_posix()


def _matches_transform(
    transform: Transform, public_path: str, globstar: Globstar
) -> bool:
    """Return whether a transform applies to a public path."""
    return GlobSet(include=(transform.path,), globstar=globstar).matches(public_path)


def _has_explicit_reversal(transform: Transform) -> bool:
    """Return whether a transform defines a custom public-to-source rewrite."""
    return bool(transform.reverse_before or transform.reverse_after)


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


def _reverse_replace(*, transform: Transform, text: str) -> str:
    """Apply one transform's reverse replacement to public text.

    A ``regex_groups`` transform reverses by swapping its ``before`` and
    ``after`` templates and re-running the same compiled-template machinery
    (Copybara ``Replace.reverse()``). The boundary anchors declared in the
    groups therefore apply symmetrically, so a non-injective literal token (a
    short public package name) is reversed only where the anchors say it is a
    real module reference -- never inside an identifier (``pkg_x``) or dotfile
    (``.pkg``). A plain literal transform falls back to ``str.replace``.

    Args:
      transform: Transform whose reverse replacement to apply.
      text: Public-side text to reverse.

    Returns:
      reversed_text: ``text`` with the reverse replacement applied.

    """
    reverse_before = _reverse_before(transform)
    reverse_after = _reverse_after(transform)
    if transform.regex_groups:
        return compile_replace(
            before=reverse_before,
            after=reverse_after,
            regex_groups=transform.regex_groups,
        ).apply(text)
    return text.replace(reverse_before, reverse_after)


def _with_outcome(change: ImportChange, outcome: ChangeOutcome) -> ImportChange:
    """Return a copy of a change annotated with a merge outcome."""
    return replace(change, outcome=outcome)


def _three_way_merge(
    *, current: bytes, base: bytes, incoming: bytes
) -> tuple[bytes, bool]:
    """Three-way merge ``incoming`` onto ``current`` relative to ``base``.

    Shells to ``git merge-file``, the diff3 engine Copybara's ``MergeImportTool``
    uses. The argument orientation matches Copybara exactly: it runs
    ``diff3 -m origin baseline destination`` (``CommandLineDiffUtil.merge``),
    treating the incoming source-of-truth change as the primary (``ours``) side
    and the local checkout as ``theirs``. Here ``incoming`` is the public head
    (the SoT change) and ``current`` is the local source, so ``incoming`` is
    passed first. Conflicting hunks keep both sides wrapped in conflict markers.

    Returns:
      merged: The merged file content, including conflict markers on overlap.
      conflicted: Whether the merge left unresolved conflict markers.

    """
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
        # git merge-file returns the capped conflict count (0-127); a clean merge
        # is 0. Values >=128 signal a fatal error (e.g. a binary file it cannot
        # merge), so the count can never collide with the error range.
        if result.returncode >= 128:
            raise ImportRequestError(
                f"Three-way merge failed: git merge-file exited "
                f"{result.returncode}: {result.stderr.decode(errors='replace')}"
            )
        return result.stdout, result.returncode > 0


def _source_path(*, config: WorkflowConfig, public_path: str) -> str:
    """Return the source path corresponding to one public path."""
    return PathMapper(config=config).source_path(public_path)


def _read_import_text(*, path: Path, label: str) -> str:
    """Read import text while preserving caller-specific error context."""
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as err:
        raise ImportRequestError(label) from err


def _is_metadata_path(public_path: str) -> bool:
    """Return whether a path belongs to VCS or Copybarista metadata."""
    parts = Path(public_path).parts
    return (
        bool(VCS_DIRS.intersection(parts))
        or public_path == ".copybarista"
        or public_path.startswith(".copybarista/")
    )


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
    *, destination: Path, changes: tuple[ImportChange, ...]
) -> tuple[_OriginalPath, ...]:
    """Snapshot touched destination paths before applying an import plan."""
    originals: list[_OriginalPath] = []
    backup_root: Path | None = None
    for idx, change in enumerate(changes):
        path = _validated_target(destination=destination, relative_path=change.source)
        if path.exists() or path.is_symlink():
            if backup_root is None:
                backup_root = Path(
                    tempfile.mkdtemp(prefix="copybarista-import-backup-")
                )
            backup = backup_root / str(idx)
            if path.is_dir() and not path.is_symlink():
                shutil.copytree(path, backup, symlinks=True)
            else:
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, backup, follow_symlinks=False)
            originals.append(_OriginalPath(path=path, backup=backup))
        else:
            originals.append(_OriginalPath(path=path, backup=None))
    return tuple(originals)


def _restore_originals(originals: tuple[_OriginalPath, ...]) -> None:
    """Restore destination paths captured before a failed import."""
    backup_parents = {
        original.backup.parent for original in originals if original.backup is not None
    }
    for original in reversed(originals):
        _delete_path(original.path)
        if original.backup is None:
            continue
        original.path.parent.mkdir(parents=True, exist_ok=True)
        if original.backup.is_dir() and not original.backup.is_symlink():
            shutil.copytree(original.backup, original.path, symlinks=True)
        else:
            shutil.copy2(original.backup, original.path, follow_symlinks=False)
    for parent in backup_parents:
        shutil.rmtree(parent, ignore_errors=True)


def _validate_import_destination(destination: Path) -> None:
    """Reject destination roots where import writes would be unsafe.

    Imports mutate an existing checkout, so the root must already be a real
    directory and must not be a symlink, filesystem root, home directory, or VCS
    metadata path.
    """
    if destination.is_symlink():
        raise ImportRequestError(f"Refusing symlink destination: {destination}")
    if not destination.is_dir():
        raise ImportRequestError(f"Import destination must exist: {destination}")
    resolved = destination.resolve()
    home = Path.home().resolve()
    if resolved in {Path("/").resolve(), home}:
        raise ImportRequestError(f"Refusing dangerous destination: {destination}")
    if VCS_DIRS.intersection(resolved.parts):
        raise ImportRequestError(f"Refusing VCS metadata destination: {destination}")


def _validated_target(*, destination: Path, relative_path: str) -> Path:
    """Return a destination target after escape and metadata checks.

    This guards every write/delete target: public paths may not be metadata,
    absolute, contain `..`, pass through symlink ancestors, or resolve outside
    the destination checkout.
    """
    if _is_metadata_path(relative_path):
        raise ImportRequestError(
            f"Public path is excluded or unmapped: {relative_path}"
        )
    root = destination.resolve()
    relative = Path(relative_path)
    target = destination / relative
    if relative.is_absolute() or ".." in relative.parts:
        raise ImportRequestError(f"Import target escapes destination: {relative_path}")
    current = destination
    for part in relative.parts[:-1]:
        current = current / part
        if current.is_symlink():
            raise ImportRequestError(
                f"Import target escapes destination: {relative_path}"
            )
    parent = target.parent.resolve(strict=False)
    if not parent.is_relative_to(root):
        raise ImportRequestError(f"Import target escapes destination: {relative_path}")
    if target.exists() or target.is_symlink():
        try:
            if not target.resolve(strict=False).is_relative_to(root):
                raise ImportRequestError(
                    f"Import target escapes destination: {relative_path}"
                )
        except RuntimeError as err:
            raise ImportRequestError(
                f"Import target cannot be resolved: {relative_path}"
            ) from err
    return target
