"""Folder export implementation."""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

from copybarista.config import WorkflowConfig
from copybarista.destinations import write_folder_destination
from copybarista.manifest import ExportManifest
from copybarista.workflow import WorkflowRunner


def export_folder(
    config: WorkflowConfig,
    source_ref: Path,
    destination: Path,
    *,
    force: bool = False,
) -> ExportManifest:
    """Export a transformed source subtree to a local folder.

    Args:
      config: Workflow config.
      source_ref: Source checkout root.
      destination: Destination directory.
      force: Replace an existing destination after safety checks.

    Returns:
      manifest: Export manifest.

    Raises:
      ExportError: If export cannot complete safely.

    """
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="copybarista-") as tmp:
        staging = Path(tmp) / "staging"
        staged_tree = WorkflowRunner(config=config, source_ref=source_ref).stage(
            staging
        )
        write_folder_destination(
            staged_tree,
            destination=destination,
            source_ref=source_ref,
            source_root=source_ref / config.source_root,
            replace_existing=force,
        )
    return ExportManifest(
        files=staged_tree.files,
        transforms=staged_tree.transforms,
        elapsed_sec=time.perf_counter() - started,
    )
