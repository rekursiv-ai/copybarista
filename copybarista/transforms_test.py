"""Tests for Copybarista text transforms."""

from __future__ import annotations

from pathlib import Path

import pytest

from copybarista.config import Transform
from copybarista.errors import TransformError
from copybarista.manifest import ManifestEntry
from copybarista.transforms import apply_transforms


def test_required_replace_changes_all_literal_matches(tmp_path: Path):
    path = tmp_path / "module_test.py"
    path.write_text("from old import A\nfrom old import B\n", encoding="utf-8")

    (result,) = apply_transforms(
        tmp_path,
        (
            Transform(
                id="replace-import",
                type="replace",
                path="module_test.py",
                before="from old import",
                after="from new import",
            ),
        ),
    )

    assert path.read_text(encoding="utf-8") == (
        "from new import A\nfrom new import B\n"
    )
    assert result.changed == 1
    assert result.count == 2
    assert [(file.source, file.destination, file.count) for file in result.files] == [
        ("module_test.py", "module_test.py", 2)
    ]


def test_replace_reports_total_occurrences_and_source_mapping(tmp_path: Path):
    first = tmp_path / "first.py"
    second = tmp_path / "second.py"
    first.write_text("old old\n", encoding="utf-8")
    second.write_text("old\n", encoding="utf-8")

    (result,) = apply_transforms(
        tmp_path,
        (
            Transform(
                id="replace-token",
                type="replace",
                path="*.py",
                before="old",
                after="new",
            ),
        ),
        files=(
            _entry(source="project/first.py", destination="first.py"),
            _entry(source="project/second.py", destination="second.py"),
        ),
    )

    assert first.read_text(encoding="utf-8") == "new new\n"
    assert second.read_text(encoding="utf-8") == "new\n"
    assert result.changed == 2
    assert result.count == 3
    assert [(file.source, file.destination, file.count) for file in result.files] == [
        ("project/first.py", "first.py", 2),
        ("project/second.py", "second.py", 1),
    ]


def test_required_replace_fails_on_no_op(tmp_path: Path):
    (tmp_path / "module_test.py").write_text("from new import A\n", encoding="utf-8")

    with pytest.raises(TransformError, match="no changes"):
        apply_transforms(
            tmp_path,
            (
                Transform(
                    id="replace-import",
                    type="replace",
                    path="module_test.py",
                    before="from old import",
                    after="from new import",
                ),
            ),
        )


def test_optional_replace_allows_no_op(tmp_path: Path):
    path = tmp_path / "module_test.py"
    path.write_text("from new import A\n", encoding="utf-8")

    (result,) = apply_transforms(
        tmp_path,
        (
            Transform(
                id="replace-import",
                type="replace",
                path="module_test.py",
                before="from old import",
                after="from new import",
                required=False,
            ),
        ),
    )

    assert path.read_text(encoding="utf-8") == "from new import A\n"
    assert result.changed == 0
    assert result.count == 0
    assert result.files == ()


def test_replace_rejects_empty_before(tmp_path: Path):
    (tmp_path / "module_test.py").write_text("value\n", encoding="utf-8")

    with pytest.raises(TransformError, match="non-empty"):
        apply_transforms(
            tmp_path,
            (
                Transform(
                    id="replace-empty",
                    type="replace",
                    path="module_test.py",
                    before="",
                    after="new",
                ),
            ),
        )


def test_strip_block_removes_inclusive_markers(tmp_path: Path):
    path = tmp_path / "README.md"
    path.write_text(
        "public\n\n"
        "<!-- copybarista:strip:start -->\n"
        "internal\n"
        "<!-- copybarista:strip:end -->\n\n"
        "more public\n",
        encoding="utf-8",
    )

    (result,) = apply_transforms(
        tmp_path,
        (
            Transform(
                id="strip-readme",
                type="strip_block",
                path="README.md",
                start="<!-- copybarista:strip:start -->",
                end="<!-- copybarista:strip:end -->",
            ),
        ),
    )

    assert path.read_text(encoding="utf-8") == "public\n\nmore public\n"
    assert result.changed == 1
    assert result.count == 1
    assert [(file.source, file.destination, file.count) for file in result.files] == [
        ("README.md", "README.md", 1)
    ]


def test_strip_block_reports_blocks_removed(tmp_path: Path):
    path = tmp_path / "README.md"
    path.write_text(
        "public\n"
        "<!-- copybarista:strip:start -->\n"
        "internal one\n"
        "<!-- copybarista:strip:end -->\n"
        "middle\n"
        "<!-- copybarista:strip:start -->\n"
        "internal two\n"
        "<!-- copybarista:strip:end -->\n"
        "more public\n",
        encoding="utf-8",
    )

    (result,) = apply_transforms(
        tmp_path,
        (
            Transform(
                id="strip-readme",
                type="strip_block",
                path="README.md",
                start="<!-- copybarista:strip:start -->",
                end="<!-- copybarista:strip:end -->",
            ),
        ),
        files=(_entry(source="project/README.md", destination="README.md"),),
    )

    assert path.read_text(encoding="utf-8") == "public\nmiddle\nmore public\n"
    assert result.changed == 1
    assert result.count == 2
    assert [(file.source, file.destination, file.count) for file in result.files] == [
        ("project/README.md", "README.md", 2)
    ]


def test_strip_block_fails_when_markers_are_missing(tmp_path: Path):
    (tmp_path / "README.md").write_text("public\n", encoding="utf-8")

    with pytest.raises(TransformError, match="marker"):
        apply_transforms(
            tmp_path,
            (
                Transform(
                    id="strip-readme",
                    type="strip_block",
                    path="README.md",
                    start="<!-- copybarista:strip:start -->",
                    end="<!-- copybarista:strip:end -->",
                ),
            ),
        )


def test_strip_block_rejects_empty_markers(tmp_path: Path):
    (tmp_path / "README.md").write_text("public\n", encoding="utf-8")

    with pytest.raises(TransformError, match="non-empty"):
        apply_transforms(
            tmp_path,
            (
                Transform(
                    id="strip-readme",
                    type="strip_block",
                    path="README.md",
                    start="",
                    end="<!-- copybarista:strip:end -->",
                ),
            ),
        )


def test_strip_block_rejects_reversed_markers(tmp_path: Path):
    (tmp_path / "README.md").write_text(
        "<!-- copybarista:strip:end -->\n"
        "public\n"
        "<!-- copybarista:strip:start -->\n"
        "internal\n"
        "<!-- copybarista:strip:end -->\n",
        encoding="utf-8",
    )

    with pytest.raises(TransformError, match="before start"):
        apply_transforms(
            tmp_path,
            (
                Transform(
                    id="strip-readme",
                    type="strip_block",
                    path="README.md",
                    start="<!-- copybarista:strip:start -->",
                    end="<!-- copybarista:strip:end -->",
                ),
            ),
        )


def test_strip_block_rejects_nested_start_marker(tmp_path: Path):
    (tmp_path / "README.md").write_text("A1A2B3B4", encoding="utf-8")

    with pytest.raises(TransformError, match="nested start"):
        apply_transforms(
            tmp_path,
            (
                Transform(
                    id="strip-readme",
                    type="strip_block",
                    path="README.md",
                    start="A",
                    end="B",
                ),
            ),
        )


def test_strip_block_non_inclusive_preserves_spacing(tmp_path: Path):
    path = tmp_path / "README.md"
    path.write_text(
        "public\n"
        "<!-- copybarista:strip:start -->\n"
        "internal\n"
        "<!-- copybarista:strip:end -->\n"
        "more public\n",
        encoding="utf-8",
    )

    apply_transforms(
        tmp_path,
        (
            Transform(
                id="strip-readme",
                type="strip_block",
                path="README.md",
                start="<!-- copybarista:strip:start -->",
                end="<!-- copybarista:strip:end -->",
                inclusive=False,
            ),
        ),
    )

    assert path.read_text(encoding="utf-8") == (
        "public\n<!-- copybarista:strip:end -->\nmore public\n"
    )


def test_strip_block_non_inclusive_handles_repeated_blocks(tmp_path: Path):
    path = tmp_path / "README.md"
    path.write_text(
        "public\n"
        "<!-- copybarista:strip:start -->\n"
        "internal one\n"
        "<!-- copybarista:strip:end -->\n"
        "middle\n"
        "<!-- copybarista:strip:start -->\n"
        "internal two\n"
        "<!-- copybarista:strip:end -->\n"
        "done\n",
        encoding="utf-8",
    )

    reports = apply_transforms(
        tmp_path,
        (
            Transform(
                id="strip-readme",
                type="strip_block",
                path="README.md",
                start="<!-- copybarista:strip:start -->",
                end="<!-- copybarista:strip:end -->",
                inclusive=False,
            ),
        ),
    )

    assert reports[0].count == 2
    assert path.read_text(encoding="utf-8") == (
        "public\n"
        "<!-- copybarista:strip:end -->\n"
        "middle\n"
        "<!-- copybarista:strip:end -->\n"
        "done\n"
    )


def test_replace_required_reports_symlink_only_match(tmp_path: Path):
    target = tmp_path / "target.txt"
    target.write_text("old\n", encoding="utf-8")
    (tmp_path / "link.txt").symlink_to(target)

    with pytest.raises(TransformError, match="only matched symlinks"):
        apply_transforms(
            tmp_path,
            (
                Transform(
                    id="replace-link",
                    type="replace",
                    path="link.txt",
                    before="old",
                    after="new",
                ),
            ),
        )


def test_matching_binary_file_fails_clearly(tmp_path: Path):
    (tmp_path / "asset.bin").write_bytes(b"\xff\xfeold")

    with pytest.raises(TransformError, match="UTF-8"):
        apply_transforms(
            tmp_path,
            (
                Transform(
                    id="replace-binary",
                    type="replace",
                    path="asset.bin",
                    before="old",
                    after="new",
                ),
            ),
        )


def test_move_renames_file(tmp_path: Path):
    (tmp_path / "old").mkdir()
    src = tmp_path / "old" / "readme.md"
    src.write_text("hello\n", encoding="utf-8")

    (result,) = apply_transforms(
        tmp_path,
        (
            Transform(
                id="move-readme",
                type="move",
                path="old/readme.md",
                destination="new/readme.md",
            ),
        ),
    )

    assert not src.exists()
    assert (tmp_path / "new" / "readme.md").read_text(encoding="utf-8") == "hello\n"
    assert result.changed == 1
    assert result.count == 1


def test_move_renames_directory(tmp_path: Path):
    (tmp_path / "old" / "sub").mkdir(parents=True)
    (tmp_path / "old" / "a.txt").write_text("a\n", encoding="utf-8")
    (tmp_path / "old" / "sub" / "b.txt").write_text("b\n", encoding="utf-8")

    (result,) = apply_transforms(
        tmp_path,
        (
            Transform(
                id="move-dir",
                type="move",
                path="old",
                destination="new",
            ),
        ),
    )

    assert not (tmp_path / "old").exists()
    assert (tmp_path / "new" / "a.txt").read_text(encoding="utf-8") == "a\n"
    assert (tmp_path / "new" / "sub" / "b.txt").read_text(encoding="utf-8") == "b\n"
    assert result.changed == 2
    assert result.count == 2


def test_required_move_fails_when_source_missing(tmp_path: Path):
    with pytest.raises(TransformError, match="no files"):
        apply_transforms(
            tmp_path,
            (
                Transform(
                    id="move-missing",
                    type="move",
                    path="nonexistent.md",
                    destination="target.md",
                ),
            ),
        )


def test_optional_move_allows_missing_source(tmp_path: Path):
    (result,) = apply_transforms(
        tmp_path,
        (
            Transform(
                id="move-missing",
                type="move",
                path="nonexistent.md",
                destination="target.md",
                required=False,
            ),
        ),
    )

    assert result.changed == 0
    assert result.count == 0
    assert result.files == ()


def _entry(source: str, destination: str) -> ManifestEntry:
    return ManifestEntry(
        source=source,
        destination=destination,
        size=0,
        sha256="",
    )
