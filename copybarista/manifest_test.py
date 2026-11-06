"""Tests for export manifest serialization."""

from __future__ import annotations

from typing import TYPE_CHECKING

import hashlib

from treekle import json_loads

from copybarista.manifest import (
    ExportManifest,
    ManifestEntry,
    TransformFileReport,
    TransformReport,
    file_entry,
)


if TYPE_CHECKING:
    from pathlib import Path


def test_manifest_json_omits_elapsed_time_for_determinism():
    first = ExportManifest(files=(), transforms=(), elapsed_sec=0.1).to_json()
    second = ExportManifest(files=(), transforms=(), elapsed_sec=9.9).to_json()

    assert first == second
    assert json_loads(first, dict[str, list[object]]) == {"files": [], "transforms": []}


def test_nonempty_manifest_json_preserves_records_and_exact_formatting():
    manifest = ExportManifest(
        files=(ManifestEntry(source="src/a", destination="a", size=1, sha256="abc"),),
        transforms=(
            TransformReport(
                id="rewrite",
                type="replace",
                path="a",
                changed=1,
                count=2,
                files=(TransformFileReport(source="a", destination="a", count=2),),
            ),
        ),
        elapsed_sec=9.9,
    )

    assert manifest.to_json() == (
        "{\n"
        '  "files": [\n'
        "    {\n"
        '      "destination": "a",\n'
        '      "sha256": "abc",\n'
        '      "size": 1,\n'
        '      "source": "src/a"\n'
        "    }\n"
        "  ],\n"
        '  "transforms": [\n'
        "    {\n"
        '      "changed": 1,\n'
        '      "count": 2,\n'
        '      "files": [\n'
        "        {\n"
        '          "count": 2,\n'
        '          "destination": "a",\n'
        '          "source": "a"\n'
        "        }\n"
        "      ],\n"
        '      "id": "rewrite",\n'
        '      "path": "a",\n'
        '      "type": "replace"\n'
        "    }\n"
        "  ]\n"
        "}\n"
    )


def test_file_entry_hashes_a_regular_files_bytes(tmp_path: Path):
    path = tmp_path / "a.txt"
    path.write_bytes(b"content\n")

    entry = file_entry(source="src/a.txt", destination="a.txt", path=path)

    assert (entry.source, entry.destination, entry.size) == ("src/a.txt", "a.txt", 8)
    assert entry.sha256 == hashlib.sha256(b"content\n").hexdigest()


def test_file_entry_hashes_symlink_target_text(tmp_path: Path):
    target = tmp_path / "target.txt"
    target.write_text("content\n", encoding="utf-8")
    link = tmp_path / "link.txt"
    link.symlink_to(target)

    entry = file_entry(source="link.txt", destination="link.txt", path=link)
    link_data = target.as_posix().encode()

    assert entry.size == len(link_data)
    assert entry.sha256 == hashlib.sha256(link_data).hexdigest()


if __name__ == "__main__":
    from copybarista.lib.testing.runner import test_main

    test_main(__file__)
