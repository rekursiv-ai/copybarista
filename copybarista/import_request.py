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

import re
import shutil
import stat
import subprocess
import sys
import tempfile

from copybarista.commands import CommandRunner
from copybarista.config import Transform, WorkflowConfig
from copybarista.errors import ImportRequestError, TransformError
from copybarista.export import export_folder
from copybarista.globs import GlobSet, Globstar
from copybarista.template import compile_replace
from copybarista.transforms import (
    _strip_blocks_with_else,
    line_has_internal_marker,
    strip_source_regions,
    strip_source_text,
)


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

    def source_path(
        self, public_path: str, *, action: ChangeAction = "modified"
    ) -> str:
        """Return the source-relative path for a public path.

        Args:
          public_path: Public repository path relative to the exported root.
          action: The change action for this path. A ``deleted`` change carries no
            content to import, so an excluded path being deleted resolves to its
            identity path (a source-side no-op) rather than being rejected; only an
            add/modify of an excluded path is rejected (see below).

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
        prefixed = self._prefixed_source_relative_path(source_public_path)
        if prefixed is None:
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
                raise ImportRequestError(
                    f"Public path is excluded or unmapped: {public_path}"
                )
            return source_public_path
        if not self.matcher.matches(prefixed):
            raise ImportRequestError(
                f"Public path is excluded or unmapped: {public_path}"
            )
        if not self.config.source_root:
            return prefixed
        return f"{self.config.source_root}/{prefixed}"

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
            if file_copy.destination in (".", "./"):
                # A directory copy with ``destination = "."`` lands its tree at
                # the public root. It owns the root-level files it supplies (e.g.
                # ``pyproject.toml``, ``CONTRIBUTING.md``); paths under the
                # package's ``destination_prefix`` come from the main source-root
                # selection, not this copy, so only claim root-level paths.
                #
                # A root path listed in ``destination_prefix_exclude`` is kept at
                # the public root by the MAIN source-root selection (a back-move
                # in Copybara), not by this ``.`` copy. It must yield to the main
                # sweep so the reverse map returns ``<source_root>/<path>``; the
                # ``.`` copy claiming it would place it inside ``.export`` and
                # collide with the root copy on re-export. Matches Copybara
                # reverse, which routes such a file back through the back-move.
                if (
                    "/" in public_path
                    or not matcher.matches(public_path)
                    or (
                        self.destination_prefix_exclude is not None
                        and self.destination_prefix_exclude.matches(public_path)
                    )
                ):
                    continue
                return f"{file_copy.source}/{public_path}"
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

    def _prefixed_source_relative_path(self, public_path: str) -> str | None:
        """Return the source-root-relative path, or ``None`` if unrelocated.

        A path under ``destination_prefix`` (or listed in
        ``destination_prefix_exclude``, which the prefix leaves at root) belongs
        to the source-root selection: its prefix is stripped and the caller
        re-roots it under ``source_root``. A path matching neither is under no
        relocation rule at all; ``None`` signals the caller to keep it at its
        identical path (Copybara's unmatched-``core.move`` behavior) rather than
        raise or wrongly re-root it under ``source_root``.
        """
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
        return None


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
                source=mapper.source_path(change.path, action=change.action),
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
                    # Strict mode reverses the public head exactly, so its
                    # re-export must reproduce public head. Merge mode folds in
                    # independent source drift, so its export legitimately differs
                    # from public head; the merge path verifies each reversal
                    # per-file instead (see _check_merge_reversal).
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

    def _ours_public_bytes(self, *, change: ImportChange, source_export: Path) -> bytes:
        """Return the source's public-space bytes for the merge 'ours' side.

        Normally the source's exported form at the public path (Copybara reads the
        origin workdir; we read a single re-export as its equivalent). But a path
        under no relocation rule is NOT shipped by the export -- it lives at its
        identical path in the source tree and is absent from the export. For such
        an identity path (``change.source == change.public``) fall back to the
        source file directly, so the merge sees the real local side rather than an
        empty string that would spuriously conflict with the incoming public edit.
        """
        exported = source_export / change.public
        if exported.is_file():
            return exported.read_bytes()
        if change.source == change.public:
            source_file = self.source_base / change.source
            if source_file.is_file() and not source_file.is_symlink():
                return source_file.read_bytes()
        return b""

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
        # Not text-mergeable: deletions, symlink/dir heads, and type changes
        # (e.g. symlink<->file) force-propagate from public head exactly as the
        # strict path does. Byte three-way-merge applies only to file->file.
        if (
            change.action in ("deleted", "type_changed")
            or public_path.is_symlink()
            or public_path.is_dir()
        ):
            return self._apply_change(change), False
        head_public = public_path.read_bytes()
        ours_public = self._ours_public_bytes(
            change=change, source_export=source_export
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
        if conflicted:
            # The caller rolls the whole import back on any conflict, so the
            # conflict-marker bytes are never a valid final tree. Don't reverse
            # (which can raise on marker text), write, or reformat them -- report
            # the conflict and leave the destination untouched.
            return _with_outcome(change, "merged"), True
        merged_source = self._reverse_content(
            public_path=change.public, data=merged_public
        )
        # Mirror _apply_change: a drifted symlink target must be removed, not
        # written through -- otherwise write_bytes follows the link and mutates
        # its referent instead of restoring a regular file.
        if target.is_symlink():
            _delete_path(target)
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
            if transform.type in ("strip_block", "internal_lines"):
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
                    public_path=public_path, transform=transform, content=content
                )
                continue
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
                if transform.type in ("strip_block", "internal_lines"):
                    # Not invertible, but rather than fail the import,
                    # _reverse_content re-inserts the source's removed region(s)
                    # verbatim (both strict and merge modes). The decision is
                    # deferred there; nothing to record as a reversible id here.
                    continue
                ids.append(transform.id)
        return tuple(ids)

    def _reinsert_source_only_regions(
        self, *, public_path: str, transform: Transform, content: bytes
    ) -> bytes:
        """Re-insert a transform's source-only regions into reversed content.

        ``strip_block`` and ``internal_lines`` both delete source-only content
        on export (a marker-delimited block, or each line carrying the marker),
        which the public tree cannot reconstruct. On import we splice the
        source's removed region(s) back verbatim at their original position: the
        source file is the local side, and the incoming public edits apply to the
        regions *around* them.

        Re-inserting is exact only when the incoming public text still contains
        the exported context around each region. A public edit that rewrote that
        context can displace a region so the rebuilt source no longer strips back
        to the incoming public text. We verify that round-trip here -- re-strip
        the rebuilt source and require it to reproduce ``public_text`` -- so a
        displaced re-insertion fails loud (in both strict and merge modes) rather
        than silently writing a source tree whose export has drifted. The strict
        path's ``_check_public_head`` is a whole-tree backstop; merge mode folds
        in source drift and has no such tree-level check, so this per-file gate is
        its safety net.
        """
        source_path = self.source_base / _source_path(
            config=self.config, public_path=public_path
        )
        if not source_path.exists() or source_path.is_symlink():
            return content
        source_text = _read_import_text(
            path=source_path,
            label=f"Source base path is not UTF-8: {source_path}",
        )
        try:
            public_text = content.decode()
        except UnicodeDecodeError as err:
            raise ImportRequestError(
                f"Public path requires text reversal but is not UTF-8: {public_path}"
            ) from err
        if transform.else_marker and transform.start and transform.start in source_text:
            return _reverse_else_blocks(
                source_text=source_text,
                public_text=public_text,
                transform=transform,
            ).encode()
        rebuilt = _splice_source_only_regions(
            source_text=source_text, public_text=public_text, transform=transform
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
                "Resolve the import by hand."
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
            source_text=source_text, public_text=public_text, transform=transform
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
                "Resolve the import by hand."
            ) from err
        if reanchored != public_text:
            raise ImportRequestError(
                f"Re-inserting source-only regions for transform '{transform.id}' "
                f"disturbed a stripped region in {public_path} so its export no "
                "longer reproduces the public content. Resolve the import by hand."
            )
        return rebuilt.encode()

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


def _removed_regions(
    *, source_text: str, transform: Transform
) -> list[tuple[int, str]]:
    """Return each source-only region a strip transform removes on export.

    Each entry is ``(offset_in_stripped, verbatim_text)``: the region's exact
    removed text and the offset it occupied within the *stripped* (exported)
    form. Derived from the SAME marker walk the export uses
    (``strip_source_regions``), so offsets and text agree with the real transform
    for every block shape -- inclusive and exclusive markers, mid-line markers,
    gap-collapsed blank lines -- with no second, drift-prone re-derivation.

    A strip transform that *rewrites* rather than deletes (``strip_block`` with
    an ``else`` branch uncomments and keeps the else lines) has no verbatim
    source-only region to re-insert: its exported bytes are a transformed form
    absent from source. Such a transform cannot be reversed by re-insertion, so
    this raises rather than fabricate a bogus region -- but only when the source
    file actually contains the block. A transform whose glob matches a file that
    carries no marker is a no-op on that file (nothing was rewritten), so it
    reverses trivially to zero regions; rejecting it would wrongly block importing
    an unrelated edit to any file the glob happens to match.
    """
    if transform.else_marker and transform.start and transform.start in source_text:
        raise ImportRequestError(
            f"Transform '{transform.id}' has an else branch and rewrites content "
            "on export; it cannot be reversed by re-insertion"
        )
    return list(strip_source_regions(source_text, transform)[1])


def _splice_source_only_regions(
    *, source_text: str, public_text: str, transform: Transform
) -> str:
    """Re-insert a strip transform's source-only regions into reversed text.

    Reverses ``strip_block`` / ``internal_lines`` by inserting each removed
    source region (block or line) back into ``public_text`` at the offset it
    held in the stripped (exported) form, right to left so earlier offsets stay
    valid. When public edits did not disturb the text around a region the offset
    lands exactly; when they did, the region is still restored (possibly
    displaced) and the import PR's CI is the human-review gate. Re-export removes
    the regions again, reproducing the public head.
    """
    regions = _removed_regions(source_text=source_text, transform=transform)
    if not regions:
        return public_text
    result = public_text
    for offset, region_text in reversed(regions):
        insert_at = min(offset, len(result))
        result = result[:insert_at] + region_text + result[insert_at:]
    return result


def _reverse_else_blocks(
    *, source_text: str, public_text: str, transform: Transform
) -> str:
    """Reverse an ``if internal / else / endif`` strip block into ``public_text``.

    An else-branch ``strip_block`` does not delete a region: on export it REPLACES
    the whole ``start .. else .. end`` block with the else branch, uncommented
    (``_strip_blocks_with_else``). To reverse, each such block's exported form (its
    uncommented else lines) is located in the public text and replaced with the
    full source block verbatim -- restoring the internal branch and the markers.
    Re-export replays the same substitution, reproducing the public head.

    When a block's exported form is not found (the public edit rewrote it) the
    substitution is skipped for that block: the internal branch cannot be
    re-derived, so the import carries the public edit forward and the missing
    internal branch is a human-review item -- never a silently corrupted tree,
    because the caller's re-strip gate then rejects a source that no longer
    reproduces public.
    """
    result = public_text
    for source_block in _else_source_blocks(source_text, transform):
        exported = _strip_blocks_with_else_text(source_block, transform)
        if exported and exported in result:
            result = result.replace(exported, source_block, 1)
    return result


def _else_source_blocks(source_text: str, transform: Transform) -> list[str]:
    """Return each verbatim ``start .. end`` else-block found in the source."""
    start, end = transform.start, transform.end
    if not start or not end:
        return []
    lines = source_text.splitlines(keepends=True)
    blocks: list[str] = []
    index = 0
    total = len(lines)
    while index < total:
        if start in lines[index]:
            block_start = index
            index += 1
            while index < total and end not in lines[index]:
                index += 1
            if index < total:
                index += 1  # include the end-marker line
                blocks.append("".join(lines[block_start:index]))
            continue
        index += 1
    return blocks


def _strip_blocks_with_else_text(block_text: str, transform: Transform) -> str:
    """Return the exported form of one else-block (its uncommented else branch)."""
    return _strip_blocks_with_else(block_text, transform)[0]


def _anchor_source_only_regions(
    *, source_text: str, public_text: str, transform: Transform
) -> str:
    """Re-insert source-only regions anchored to their surviving source siblings.

    Used when a public edit rewrote the immediate context of a stripped region so
    the offset splice would land inside a rewritten line. Instead of an offset,
    each contiguous run of source-only lines is anchored to the nearest KEPT
    (exported) source line that precedes it; the run is inserted right after that
    anchor line in the public text. When the preceding kept line did not survive
    the public rewrite, the run is anchored to the nearest following kept line and
    inserted before it. When neither neighbor survived, the run is appended at the
    region's original offset as a last resort (still valid, just less precise).

    The result always re-strips back to a superset of the public text with the
    source-only lines removed, so re-export reproduces the public head. Placement
    is best-effort and meant for human review in the import PR.
    """
    source_lines = source_text.splitlines(keepends=True)
    marks = _source_only_line_mask(source_lines=source_lines, transform=transform)
    if not any(marks):
        return _splice_source_only_regions(
            source_text=source_text, public_text=public_text, transform=transform
        )
    # Group contiguous source-only lines into runs, each with the kept source line
    # immediately before and after it (empty string when at a file boundary).
    runs: list[tuple[str, str, str]] = []
    index = 0
    total = len(source_lines)
    while index < total:
        if not marks[index]:
            index += 1
            continue
        start = index
        while index < total and marks[index]:
            index += 1
        before = source_lines[start - 1] if start > 0 else ""
        after = source_lines[index] if index < total else ""
        runs.append((before, "".join(source_lines[start:index]), after))
    result = public_text
    for before, region_text, after in reversed(runs):
        result = _insert_anchored_region(
            public_text=result,
            before_anchor=before,
            after_anchor=after,
            region_text=region_text,
        )
    return result


def _source_only_line_mask(
    *, source_lines: list[str], transform: Transform
) -> list[bool]:
    """Return a per-line mask of which source lines the transform removes."""
    if transform.type == "internal_lines":
        marker = transform.start
        return [line_has_internal_marker(line, marker) for line in source_lines]
    # strip_block: mark lines inside a start..end block (inclusive of markers when
    # the transform is inclusive, exclusive of them otherwise).
    start, end = transform.start, transform.end
    inside = False
    mask: list[bool] = []
    for line in source_lines:
        is_start = bool(start) and start in line
        is_end = bool(end) and end in line
        if is_start and not inside:
            inside = True
            mask.append(transform.inclusive)
            continue
        if is_end and inside:
            inside = False
            mask.append(transform.inclusive)
            continue
        mask.append(inside)
    return mask


def _insert_anchored_region(
    *, public_text: str, before_anchor: str, after_anchor: str, region_text: str
) -> str:
    """Insert one source-only run into ``public_text`` beside a surviving anchor.

    Prefers inserting right after the ``before_anchor`` line; falls back to right
    before the ``after_anchor`` line; if neither survives verbatim, tries the
    anchor's distinctive token (the longest run of identifier/quote characters),
    so a line the public rewrite merely reflowed (e.g. dropped a trailing comma or
    collapsed onto one line) still anchors near its original neighbor. Only if no
    trace of either anchor survives does the run append at end-of-file (still
    valid source that re-strips away on export).
    """
    cut = _anchor_cut(public_text, before_anchor, after=False)
    if cut is not None:
        return public_text[:cut] + region_text + public_text[cut:]
    cut = _anchor_cut(public_text, after_anchor, after=True)
    if cut is not None:
        return public_text[:cut] + region_text + public_text[cut:]
    if public_text and not public_text.endswith("\n"):
        public_text += "\n"
    return public_text + region_text


def _anchor_cut(public_text: str, anchor: str, *, after: bool) -> int | None:
    """Return a LINE-BOUNDARY insert offset next to ``anchor``, or None.

    Matches the whole anchor line first, then falls back to the anchor's most
    distinctive token so a reflowed line still anchors. ``after`` selects whether
    the run lands before (True) or after (False) the matched line. The returned
    offset is ALWAYS a line boundary -- the start of the matched line (when
    inserting before a following sibling) or the start of the next line (when
    inserting after a preceding sibling) -- so a source-only run never splices
    into the middle of a surviving public line, even when the anchor matched only
    a token embedded in a reflowed line.
    """
    if not anchor:
        return None
    for needle in (anchor.strip(), _distinctive_token(anchor)):
        if not needle:
            continue
        at = public_text.rfind(needle) if not after else public_text.find(needle)
        if at == -1:
            continue
        if after:
            # Start of the matched line: the run lands before the following
            # sibling on its own line.
            return public_text.rfind("\n", 0, at) + 1
        # Start of the line AFTER the matched line: the run lands after the
        # preceding sibling on its own line.
        line_end = public_text.find("\n", at)
        return line_end + 1 if line_end != -1 else len(public_text)
    return None


def _distinctive_token(line: str) -> str:
    """Return the longest identifier/quoted token on a line, for loose anchoring."""
    tokens = re.findall(r'"[^"]+"|\'[^\']+\'|[A-Za-z_][A-Za-z0-9_.]+', line)
    return max(tokens, key=len) if tokens else ""


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
