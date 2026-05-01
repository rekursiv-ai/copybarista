"""Tests for export manifest serialization."""

from __future__ import annotations

from pathlib import Path

import hashlib
import json

from copybarista.manifest import ExportManifest, file_entry


def test_manifest_json_omits_elapsed_time_for_determinism():
    first = ExportManifest(files=(), transforms=(), elapsed_sec=0.1).to_json()
    second = ExportManifest(files=(), transforms=(), elapsed_sec=9.9).to_json()

    assert first == second
    assert json.loads(first) == {"files": [], "transforms": []}


def test_file_entry_hashes_symlink_target_text(tmp_path: Path):
    target = tmp_path / "target.txt"
    target.write_text("content\n", encoding="utf-8")
    link = tmp_path / "link.txt"
    link.symlink_to(target)

    entry = file_entry(source="link.txt", destination="link.txt", path=link)
    link_data = target.as_posix().encode()

    assert entry.size == len(link_data)
    assert entry.sha256 == hashlib.sha256(link_data).hexdigest()
