"""Destination publishing for transformed export trees.

Destination functions receive an already staged tree and are responsible only
for publishing that tree. Safety checks live at this boundary because folder
destinations can replace large directory trees.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import shutil

from copybarista.errors import ExportError
from copybarista.workflow import StagedTree


DestinationStatus = Literal["created", "updated", "noop"]


@dataclass(frozen=True, slots=True, kw_only=True)
class DestinationResult:
    """Summary of one destination write."""

    status: DestinationStatus
    ref: str = ""


def write_folder_destination(
    staged_tree: StagedTree,
    *,
    destination: Path,
    source_ref: Path,
    source_root: Path,
    replace_existing: bool = False,
    consume_staging: bool = False,
) -> DestinationResult:
    """Replace a local folder with the transformed tree.

    Existing destinations require an explicit force flag and still pass
    safety validation. Symlinks are refused before path resolution so a link
    cannot redirect replacement to an unexpected target. Callers that own a
    temporary staging directory can move it into place to avoid a second full
    tree copy.

    Args:
      staged_tree: Transformed tree to publish.
      destination: Folder path to create or replace.
      source_ref: Source checkout root.
      source_root: Source workflow root.
      replace_existing: Whether an existing destination may be replaced.
      consume_staging: Move the staged tree into place instead of copying it.

    Returns:
      result: Destination write status and destination path.

    Raises:
      ExportError: If replacing the folder would be unsafe.

    """
    if destination.is_symlink():
        raise ExportError(f"Refusing to replace symlink destination: {destination}")
    destination = destination.resolve()
    _validate_destination(
        source_ref=source_ref.resolve(),
        source_root=source_root.resolve(),
        destination=destination,
        replacing_existing=replace_existing and destination.exists(),
    )
    status: DestinationStatus = "updated" if destination.exists() else "created"
    if destination.exists():
        if not replace_existing:
            raise ExportError(
                f"Destination already exists; pass --force to replace it: {destination}"
            )
        shutil.rmtree(destination)
    validate_staged_symlinks(staged_tree.root)
    if consume_staging:
        shutil.move(str(staged_tree.root), destination)
    else:
        shutil.copytree(staged_tree.root, destination, symlinks=True)
    return DestinationResult(status=status, ref=destination.as_posix())


def validate_staged_symlinks(root: Path) -> None:
    """Reject staged symlinks that point outside the staged tree."""
    resolved_root = root.resolve()
    for path in root.rglob("*"):
        if not path.is_symlink():
            continue
        target = (path.parent / path.readlink()).resolve(strict=False)
        if not target.is_relative_to(resolved_root):
            raise ExportError(f"Symlink points outside staged tree: {path}")


def _validate_destination(
    source_ref: Path,
    source_root: Path,
    destination: Path,
    replacing_existing: bool,
) -> None:
    """Reject destination paths where full-tree replacement would be unsafe.

    Folder export deletes the destination before copying the staged tree, so it
    refuses the filesystem root, home directory, source checkout, source root,
    and existing home-directory descendants.
    """
    home = Path.home().resolve()
    dangerous = {Path("/").resolve(), home, source_ref, source_root}
    if destination in dangerous:
        raise ExportError(f"Refusing to replace dangerous destination: {destination}")
    if replacing_existing and destination.is_relative_to(home):
        raise ExportError("Destination must not be inside the home directory")
    if destination.is_relative_to(source_root):
        raise ExportError("Destination must not be inside source root")
    if destination.is_relative_to(source_ref):
        raise ExportError("Destination must not be inside source checkout")
