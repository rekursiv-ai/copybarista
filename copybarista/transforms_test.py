"""Tests for Copybarista text transforms."""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING

import os
import sys

import pytest

from copybarista import transforms
from copybarista.config import Transform
from copybarista.errors import TransformError
from copybarista.manifest import ManifestEntry
from copybarista.transforms import (
    apply_transforms,
    strip_source_regions,
    strip_source_text,
    uncomment_source_text,
)


if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize(
    ("transform", "source"),
    [
        (
            Transform(id="x", type="strip_block", path="m", start="# S", end="# E"),
            "keep a\n# S\nsecret\n# E\nkeep b\n",
        ),
        (
            Transform(
                id="x",
                type="strip_block",
                path="m",
                start="# S",
                end="# E",
                inclusive=False,
            ),
            "keep a\n# S\nsecret\n# E\nkeep b\n",
        ),
        (
            Transform(id="x", type="strip_block", path="m", start="# S", end="# E"),
            "prefix # S\nsecret\n# E\nkeep\n",
        ),
        (
            Transform(id="x", type="strip_block", path="m", start="# S", end="# E"),
            "a\n# S\nx\n# E\n\n\nb\n",
        ),
        (
            Transform(id="x", type="strip_block", path="m", start="# S", end="# E"),
            "# S\nx\n# E\n# S\ny\n# E\n",
        ),
        (
            Transform(id="x", type="internal_lines", path="m", start="# INT"),
            "import a\nimport secret  # INT\nVALUE = 1\n",
        ),
    ],
)
def test_strip_source_regions_matches_export_and_reconstructs(
    transform: Transform,
    source: str,
):
    """The region oracle agrees with the export and reconstructs the source.

    ``strip_source_regions`` must return exactly the exported text
    ``strip_source_text`` produces, and re-inserting its reported regions into
    that exported text (right to left) must rebuild the original source byte for
    byte. This is the invariant the reverse-import re-insertion relies on.
    """
    stripped, regions = strip_source_regions(source, transform)
    assert stripped == strip_source_text(source, transform)
    rebuilt = stripped
    for offset, text in reversed(regions):
        rebuilt = rebuilt[:offset] + text + rebuilt[offset:]
    assert rebuilt == source


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
        ("module_test.py", "module_test.py", 2),
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


def test_module_replace_rewrites_every_spelling_in_one_pass(tmp_path: Path):
    path = tmp_path / "mod.py"
    path.write_text(
        "from internal.lib import userdirs\n"
        "from internal.lib.userdirs import data_dir\n"
        'lazy_import("internal.lib.userdirs")\n'
        "from internal.lib import userdirs_fixture\n",
        encoding="utf-8",
    )

    (result,) = apply_transforms(
        tmp_path,
        (
            Transform(
                id="userdirs",
                type="replace",
                path="mod.py",
                before="internal.lib.userdirs",
                after="pkg.lib.userdirs",
                module=True,
            ),
        ),
    )

    assert path.read_text(encoding="utf-8") == (
        "from pkg.lib import userdirs\n"
        "from pkg.lib.userdirs import data_dir\n"
        'lazy_import("pkg.lib.userdirs")\n'
        "from internal.lib import userdirs_fixture\n"
    )
    assert result.count == 3


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
        ("README.md", "README.md", 1),
    ]


def test_strip_block_removes_an_indented_block_whole_line(tmp_path: Path):
    """An indented block must not leave its leading whitespace behind.

    ``find`` returns the marker's offset, so the indent before it survived the
    cut as a whitespace-only line. ``ruff_format`` then deleted that stub on
    export, and no reversal could reproduce the public text from the stubbed
    form -- an ordinary public edit wedged the import.
    """
    path = tmp_path / "m.py"
    path.write_text(
        "def f():\n"
        "    keep = 1\n"
        "    # copybarista:internal:start\n"
        "    secret = 2\n"
        "    # copybarista:internal:end\n"
        "    return keep\n",
        encoding="utf-8",
    )

    apply_transforms(
        tmp_path,
        (
            Transform(
                id="strip-py",
                type="strip_block",
                path="m.py",
                start="# copybarista:internal:start",
                end="# copybarista:internal:end",
            ),
        ),
    )

    assert path.read_text(encoding="utf-8") == (
        "def f():\n    keep = 1\n    return keep\n"
    )


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
        ("project/README.md", "README.md", 2),
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


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("keep\n# S\nsecret\n", "did not find end marker"),
        # The nested start is the NEXT one after the block opens, not the last.
        ("# S\na\n# S\nb\n# E\n# S\nc\n# E\n", "nested start"),
    ],
)
def test_strip_block_names_a_malformed_block(
    tmp_path: Path,
    source: str,
    message: str,
) -> None:
    (tmp_path / "m.py").write_text(source, encoding="utf-8")

    with pytest.raises(TransformError, match=message):
        apply_transforms(
            tmp_path,
            (
                Transform(
                    id="x",
                    type="strip_block",
                    path="m.py",
                    start="# S",
                    end="# E",
                ),
            ),
        )


@pytest.mark.parametrize(
    ("source", "start", "end", "exported"),
    [
        # The scan resumes where it cut, so a marker the cut itself spells is text.
        ("## S\nx\n# E S\n", "# S", "# E", "# S\n"),
        # One repeated marker fences a block, as does an end extending its start.
        ("a\n# --\nsecret\n# --\nb\n", "# --", "# --", "a\nb\n"),
        ("a\n# int\nsecret\n# int end\nb\n", "# int", "# int end", "a\nb\n"),
        # A blank first line survives a block that runs to the end of the file.
        ("\n# S\nx\n# E", "# S", "# E", "\n"),
    ],
)
def test_strip_block_export_matches_the_reverse_import_oracle(
    tmp_path: Path,
    source: str,
    start: str,
    end: str,
    exported: str,
) -> None:
    """The file transform and ``strip_source_text`` strip the same way.

    The reverse import re-inserts the regions ``strip_source_text`` reports, so
    an export that strips differently from it cannot be imported back.
    """
    transform = Transform(id="x", type="strip_block", path="m.py", start=start, end=end)
    path = tmp_path / "m.py"
    path.write_text(source, encoding="utf-8")

    apply_transforms(tmp_path, (transform,))

    assert path.read_text(encoding="utf-8") == exported
    assert strip_source_text(source, transform) == exported


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


def test_strip_block_if_else_uncomments_else_branch(tmp_path: Path):
    path = tmp_path / "config.py"
    path.write_text(
        "before\n"
        "# copybarista:if internal\n"
        "INTERNAL = True\n"
        "# copybarista:else\n"
        "# INTERNAL = False\n"
        "# copybarista:endif\n"
        "after\n",
        encoding="utf-8",
    )

    (result,) = apply_transforms(
        tmp_path,
        (
            Transform(
                id="conditional",
                type="strip_block",
                path="config.py",
                start="# copybarista:if internal",
                end="# copybarista:endif",
                else_marker="# copybarista:else",
            ),
        ),
    )

    assert path.read_text(encoding="utf-8") == "before\nINTERNAL = False\nafter\n"
    assert result.changed == 1
    assert result.count == 1


def test_strip_block_if_else_multiple_lines(tmp_path: Path):
    path = tmp_path / "config.py"
    path.write_text(
        "# copybarista:if internal\n"
        'X = "wide"\n'
        'Y = "internal"\n'
        "# copybarista:else\n"
        '# X = "narrow"\n'
        '# Y = "public"\n'
        "# copybarista:endif\n",
        encoding="utf-8",
    )

    apply_transforms(
        tmp_path,
        (
            Transform(
                id="conditional",
                type="strip_block",
                path="config.py",
                start="# copybarista:if internal",
                end="# copybarista:endif",
                else_marker="# copybarista:else",
            ),
        ),
    )

    assert path.read_text(encoding="utf-8") == 'X = "narrow"\nY = "public"\n'


def test_strip_block_if_else_repeated_blocks(tmp_path: Path):
    path = tmp_path / "config.py"
    path.write_text(
        "a = 1\n"
        "# copybarista:if internal\n"
        "b = 2\n"
        "# copybarista:else\n"
        "# b = 3\n"
        "# copybarista:endif\n"
        "c = 4\n"
        "# copybarista:if internal\n"
        "d = 5\n"
        "# copybarista:else\n"
        "# d = 6\n"
        "# copybarista:endif\n"
        "e = 7\n",
        encoding="utf-8",
    )

    (result,) = apply_transforms(
        tmp_path,
        (
            Transform(
                id="conditional",
                type="strip_block",
                path="config.py",
                start="# copybarista:if internal",
                end="# copybarista:endif",
                else_marker="# copybarista:else",
            ),
        ),
    )

    assert path.read_text(encoding="utf-8") == "a = 1\nb = 3\nc = 4\nd = 6\ne = 7\n"
    assert result.count == 2


def test_strip_block_if_else_preserves_indented_comments(tmp_path: Path):
    """Lines with # that aren't comment prefixes should be preserved."""
    path = tmp_path / "config.py"
    path.write_text(
        "# copybarista:if internal\n"
        "x = 1\n"
        "# copybarista:else\n"
        "#     x = 2\n"
        "# copybarista:endif\n",
        encoding="utf-8",
    )

    apply_transforms(
        tmp_path,
        (
            Transform(
                id="conditional",
                type="strip_block",
                path="config.py",
                start="# copybarista:if internal",
                end="# copybarista:endif",
                else_marker="# copybarista:else",
            ),
        ),
    )

    assert path.read_text(encoding="utf-8") == "    x = 2\n"


def test_strip_block_if_else_indented_block(tmp_path: Path):
    """Else branch with indented # comments uncomments correctly."""
    path = tmp_path / "module.py"
    path.write_text(
        "class Foo:\n"
        "    @property\n"
        "    # copybarista:if internal\n"
        "    def internal_name(self) -> bool:\n"
        '        """Internal doc."""\n'
        "    # copybarista:else\n"
        "    # def public_name(self) -> bool:\n"
        '    #     """Public doc."""\n'
        "    # copybarista:endif\n"
        "        return False\n",
        encoding="utf-8",
    )

    apply_transforms(
        tmp_path,
        (
            Transform(
                id="conditional",
                type="strip_block",
                path="module.py",
                start="# copybarista:if internal",
                end="# copybarista:endif",
                else_marker="# copybarista:else",
            ),
        ),
    )

    assert path.read_text(encoding="utf-8") == (
        "class Foo:\n"
        "    @property\n"
        "    def public_name(self) -> bool:\n"
        '        """Public doc."""\n'
        "        return False\n"
    )


def test_strip_block_if_else_missing_else_marker_raises(tmp_path: Path):
    path = tmp_path / "config.py"
    path.write_text(
        "# copybarista:if internal\nx = 1\n# copybarista:endif\n",
        encoding="utf-8",
    )

    with pytest.raises(TransformError, match="else marker"):
        apply_transforms(
            tmp_path,
            (
                Transform(
                    id="conditional",
                    type="strip_block",
                    path="config.py",
                    start="# copybarista:if internal",
                    end="# copybarista:endif",
                    else_marker="# copybarista:else",
                ),
            ),
        )


def test_internal_lines_removes_marked_lines(tmp_path: Path):
    path = tmp_path / "module.py"
    path.write_text(
        "from foo import Bar  # copybarista:internal\n"
        "from baz import Qux\n"
        "import internal  # copybarista:internal\n"
        "x = 1\n",
        encoding="utf-8",
    )

    (result,) = apply_transforms(
        tmp_path,
        (
            Transform(
                id="omit-internal",
                type="internal_lines",
                path="module.py",
                start="# copybarista:internal",
            ),
        ),
    )

    assert path.read_text(encoding="utf-8") == "from baz import Qux\nx = 1\n"
    assert result.changed == 1
    assert result.count == 2
    assert [(f.source, f.destination, f.count) for f in result.files] == [
        ("module.py", "module.py", 2),
    ]


def test_internal_lines_marker_does_not_claim_longer_block_marker(tmp_path: Path):
    """The line marker must not match a block marker it is a prefix of.

    ``# copybarista:internal`` is a prefix of the block markers
    ``# copybarista:internal:start`` / ``:end``. A bare substring test would make
    the ``internal_lines`` transform strip those block-marker lines too, which on
    reverse-import reinserts them out of order and corrupts the tree. The line
    marker matches ONLY when it is not immediately followed by ``:``.
    """
    path = tmp_path / "module.py"
    path.write_text(
        "keep_me = 1\n"
        "# copybarista:internal:start\n"
        "block_body = 2\n"
        "# copybarista:internal:end\n"
        "drop_me = 3  # copybarista:internal\n"
        "keep_me_too = 4\n",
        encoding="utf-8",
    )

    (result,) = apply_transforms(
        tmp_path,
        (
            Transform(
                id="omit-internal",
                type="internal_lines",
                path="module.py",
                start="# copybarista:internal",
            ),
        ),
    )

    # Only the bare-marker line is removed; the block-marker lines stay untouched.
    assert path.read_text(encoding="utf-8") == (
        "keep_me = 1\n"
        "# copybarista:internal:start\n"
        "block_body = 2\n"
        "# copybarista:internal:end\n"
        "keep_me_too = 4\n"
    )
    assert result.count == 1


def test_internal_lines_across_multiple_files(tmp_path: Path):
    (tmp_path / "a.py").write_text(
        "keep\nomit  # copybarista:internal\n",
        encoding="utf-8",
    )
    (tmp_path / "b.py").write_text("# copybarista:internal\nkeep\n", encoding="utf-8")

    (result,) = apply_transforms(
        tmp_path,
        (
            Transform(
                id="omit-multi",
                type="internal_lines",
                path="*.py",
                start="# copybarista:internal",
            ),
        ),
    )

    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "keep\n"
    assert (tmp_path / "b.py").read_text(encoding="utf-8") == "keep\n"
    assert result.changed == 2
    assert result.count == 2


def test_internal_lines_required_fails_when_no_marker_found(tmp_path: Path):
    (tmp_path / "clean.py").write_text("x = 1\n", encoding="utf-8")

    with pytest.raises(TransformError, match="marker"):
        apply_transforms(
            tmp_path,
            (
                Transform(
                    id="omit-missing",
                    type="internal_lines",
                    path="clean.py",
                    start="# copybarista:internal",
                ),
            ),
        )


def test_internal_lines_optional_allows_no_matches(tmp_path: Path):
    path = tmp_path / "clean.py"
    path.write_text("x = 1\n", encoding="utf-8")

    (result,) = apply_transforms(
        tmp_path,
        (
            Transform(
                id="omit-optional",
                type="internal_lines",
                path="clean.py",
                start="# copybarista:internal",
                required=False,
            ),
        ),
    )

    assert path.read_text(encoding="utf-8") == "x = 1\n"
    assert result.changed == 0
    assert result.count == 0


def test_ruff_format_uses_current_python_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[list[str]] = []

    class FakeRunner:
        def run(self, argv: list[str], **_: object) -> object:
            calls.append(argv)
            return object()

    monkeypatch.setattr(transforms, "CommandRunner", FakeRunner)
    (tmp_path / "module.py").write_text("x = 1\n", encoding="utf-8")

    apply_transforms(
        tmp_path,
        (
            Transform(
                id="ruff",
                type="ruff_format",
                path=".",
            ),
        ),
    )

    assert calls[0][:3] == [sys.executable, "-m", "ruff"]
    assert calls[1][:3] == [sys.executable, "-m", "ruff"]


def test_ruff_format_reports_a_same_size_rewrite_within_one_tick(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Quote normalization keeps the size; a coarse clock keeps the mtime.

    The transform before ``ruff_format`` writes the file milliseconds before
    ruff starts, so on a filesystem stamping whole seconds ruff's rewrite lands
    in the same tick, and a report built from size and mtime leaves it out.
    """
    module = tmp_path / "module.py"
    module.write_text("x = 'a'\n", encoding="utf-8")
    monkeypatch.setattr(
        transforms,
        "CommandRunner",
        partial(_Rewriter, path=module, text='x = "a"\n', keep_mtime=True),
    )

    (report,) = apply_transforms(
        tmp_path,
        (Transform(id="ruff", type="ruff_format", path="."),),
    )

    assert [file.destination for file in report.files] == ["module.py"]


def test_ruff_format_reports_only_files_whose_bytes_changed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A file read because it was the newest is reported only if it changed.

    Ruff's rewrite of ``other.py`` makes it the newest file, so ``module.py``
    leaves the newest tick during the run; that alone is not a change.
    """
    module = tmp_path / "module.py"
    module.write_text("x = 1\n", encoding="utf-8")
    other = tmp_path / "other.py"
    other.write_text("y = 'b'\n", encoding="utf-8")
    os.utime(other, ns=(2_000_000_000, 2_000_000_000))
    monkeypatch.setattr(
        transforms,
        "CommandRunner",
        partial(_Rewriter, path=other, text='y = "b"\n', keep_mtime=False),
    )

    (report,) = apply_transforms(
        tmp_path,
        (Transform(id="ruff", type="ruff_format", path="."),),
    )

    assert [file.destination for file in report.files] == ["other.py"]


def test_uncomment_single_line(tmp_path: Path):
    path = tmp_path / "setup.cfg"
    path.write_text(
        '    "requests",\n    # "imagesize",  # copybarista:external\n    "click",\n',
        encoding="utf-8",
    )

    (result,) = apply_transforms(
        tmp_path,
        (
            Transform(
                id="uncomment-ext",
                type="uncomment",
                path="setup.cfg",
                start="# copybarista:external",
            ),
        ),
    )

    assert path.read_text(encoding="utf-8") == (
        '    "requests",\n    "imagesize",\n    "click",\n'
    )
    assert result.changed == 1
    assert result.count == 1


def test_uncomment_single_line_multiple_matches(tmp_path: Path):
    path = tmp_path / "deps.txt"
    path.write_text(
        "keep\n# foo  # copybarista:external\nmiddle\n# bar  # copybarista:external\n",
        encoding="utf-8",
    )

    (result,) = apply_transforms(
        tmp_path,
        (
            Transform(
                id="uncomment-ext",
                type="uncomment",
                path="deps.txt",
                start="# copybarista:external",
            ),
        ),
    )

    assert path.read_text(encoding="utf-8") == "keep\nfoo\nmiddle\nbar\n"
    assert result.count == 2


def test_uncomment_block(tmp_path: Path):
    path = tmp_path / "pyproject.toml"
    path.write_text(
        "deps = [\n"
        "# copybarista:external:start\n"
        '#     "imagesize",\n'
        '#     "numpy",\n'
        "# copybarista:external:end\n"
        "]\n",
        encoding="utf-8",
    )

    (result,) = apply_transforms(
        tmp_path,
        (
            Transform(
                id="uncomment-block",
                type="uncomment",
                path="pyproject.toml",
                start="# copybarista:external:start",
                end="# copybarista:external:end",
            ),
        ),
    )

    assert path.read_text(encoding="utf-8") == (
        'deps = [\n    "imagesize",\n    "numpy",\n]\n'
    )
    assert result.changed == 1
    assert result.count == 1


def test_uncomment_block_multiple(tmp_path: Path):
    path = tmp_path / "config.py"
    path.write_text(
        "a = 1\n"
        "# copybarista:external:start\n"
        "# b = 2\n"
        "# copybarista:external:end\n"
        "c = 3\n"
        "# copybarista:external:start\n"
        "# d = 4\n"
        "# copybarista:external:end\n"
        "e = 5\n",
        encoding="utf-8",
    )

    (result,) = apply_transforms(
        tmp_path,
        (
            Transform(
                id="uncomment-block",
                type="uncomment",
                path="config.py",
                start="# copybarista:external:start",
                end="# copybarista:external:end",
            ),
        ),
    )

    assert path.read_text(encoding="utf-8") == "a = 1\nb = 2\nc = 3\nd = 4\ne = 5\n"
    assert result.count == 2


def test_uncomment_inline_ignores_block_marker_lines(tmp_path: Path):
    # The inline external marker (start="# copybarista:external", no end) is a
    # prefix of the block markers ":external:start"/":end". The inline transform
    # must NOT claim those block-marker lines -- they belong to the paired block
    # uncomment transform, which runs first in the real config. Without the
    # not-followed-by-":" token guard, the inline split would truncate a
    # ":external:start" line at "# copybarista:external", corrupting it.
    path = tmp_path / "cli.py"
    path.write_text(
        "candidates = (\n"
        '    # "AnthropicCLI",  # copybarista:external\n'
        "    # copybarista:external:start\n"
        '    # "Other",\n'
        "    # copybarista:external:end\n"
        ")\n",
        encoding="utf-8",
    )

    (result,) = apply_transforms(
        tmp_path,
        (
            Transform(
                id="uncomment-inline",
                type="uncomment",
                path="cli.py",
                start="# copybarista:external",
            ),
        ),
    )

    # Only the inline line is uncommented; both block-marker lines and the
    # commented body between them are left verbatim for the block transform.
    assert path.read_text(encoding="utf-8") == (
        "candidates = (\n"
        '    "AnthropicCLI",\n'
        "    # copybarista:external:start\n"
        '    # "Other",\n'
        "    # copybarista:external:end\n"
        ")\n"
    )
    assert result.count == 1


def test_uncomment_block_missing_end_raises(tmp_path: Path):
    path = tmp_path / "config.py"
    path.write_text(
        "# copybarista:external:start\n# x = 1\n",
        encoding="utf-8",
    )

    with pytest.raises(TransformError, match="end marker"):
        apply_transforms(
            tmp_path,
            (
                Transform(
                    id="uncomment-block",
                    type="uncomment",
                    path="config.py",
                    start="# copybarista:external:start",
                    end="# copybarista:external:end",
                ),
            ),
        )


@pytest.mark.parametrize(
    ("source", "uncommented"),
    [
        # An empty block drops just its two marker lines.
        ("# S\n# E\ny = 2\n", "y = 2\n"),
        # The end is sought after the start line, which may name it.
        ("# S -- through # E\n# x = 1\n# E\ny = 2\n", "x = 1\ny = 2\n"),
    ],
)
def test_uncomment_block_closes_at_the_next_end_marker(
    source: str,
    uncommented: str,
) -> None:
    transform = Transform(id="x", type="uncomment", path="m.py", start="# S", end="# E")

    assert uncomment_source_text(source, transform) == (uncommented, 1)


@pytest.mark.parametrize(
    ("source", "uncommented"),
    [
        ("", ""),
        ("y = 2", "y = 2"),
        ("# x = 1  # MARK\n\ny = 2", "x = 1\n\ny = 2"),
        ("# x = 1  # MARK\n\ny = 2\n", "x = 1\n\ny = 2\n"),
    ],
)
def test_uncomment_keeps_the_ending_it_was_given(
    source: str,
    uncommented: str,
) -> None:
    """Uncommenting rewrites marked lines only, never how the text ends.

    An empty text used to come back as a lone newline.
    """
    transform = Transform(id="x", type="uncomment", path="m.py", start="# MARK")

    assert uncomment_source_text(source, transform)[0] == uncommented


@pytest.mark.parametrize(
    ("source", "exported"),
    [
        ("", ""),
        ("a\n# IF\nx\n# ELSE\n# y\n# END\nb", "a\ny\nb"),
        ("a\n# IF\nx\n# ELSE\n# y\n# END\nb\n", "a\ny\nb\n"),
    ],
)
def test_strip_block_if_else_keeps_the_ending_it_was_given(
    source: str,
    exported: str,
) -> None:
    """The ``else`` strip rewrites its blocks only, never how the text ends.

    An empty text used to come back as a lone newline.
    """
    transform = Transform(
        id="x",
        type="strip_block",
        path="m.py",
        start="# IF",
        end="# END",
        else_marker="# ELSE",
    )

    assert strip_source_text(source, transform) == exported


def test_uncomment_required_fails_when_no_marker_found(tmp_path: Path):
    (tmp_path / "clean.py").write_text("x = 1\n", encoding="utf-8")

    with pytest.raises(TransformError, match="marker"):
        apply_transforms(
            tmp_path,
            (
                Transform(
                    id="uncomment-missing",
                    type="uncomment",
                    path="clean.py",
                    start="# copybarista:external",
                ),
            ),
        )


def test_uncomment_optional_allows_no_matches(tmp_path: Path):
    path = tmp_path / "clean.py"
    path.write_text("x = 1\n", encoding="utf-8")

    (result,) = apply_transforms(
        tmp_path,
        (
            Transform(
                id="uncomment-optional",
                type="uncomment",
                path="clean.py",
                start="# copybarista:external",
                required=False,
            ),
        ),
    )

    assert path.read_text(encoding="utf-8") == "x = 1\n"
    assert result.changed == 0
    assert result.count == 0


class _Rewriter:
    """Stands in for ruff: its ``format`` call rewrites one file in place."""

    def __init__(self, *, path: Path, text: str, keep_mtime: bool) -> None:
        self._path = path
        self._text = text
        self._keep_mtime = keep_mtime

    def run(self, argv: list[str], **_: object) -> object:
        if "format" in argv:
            mtime_ns = self._path.stat().st_mtime_ns
            self._path.write_text(self._text, encoding="utf-8")
            if self._keep_mtime:
                os.utime(self._path, ns=(mtime_ns, mtime_ns))
        return object()


@pytest.mark.parametrize(
    "transform",
    [
        Transform(id="r", type="replace", path="**/m.py", before="X", after="Y"),
        Transform(id="s", type="strip_block", path="**/m.py", start="# S", end="# E"),
        Transform(id="i", type="internal_lines", path="**/m.py", start="# S"),
        Transform(id="u", type="uncomment", path="**/m.py", start="# S"),
    ],
    ids=["replace", "strip_block", "internal_lines", "uncomment"],
)
def test_every_text_transform_honors_one_or_more_globstar(
    tmp_path: Path,
    transform: Transform,
):
    """``**/m.py`` needs at least one directory, so a root ``m.py`` is untouched.

    Dropping the workflow's ``globstar`` anywhere on the way to the matcher
    falls back to ``zero_or_more``, which also rewrites the root file.
    """
    text = "X\n# S\n# X\n# E\n"
    (tmp_path / "sub").mkdir()
    (tmp_path / "m.py").write_text(text, encoding="utf-8")
    (tmp_path / "sub" / "m.py").write_text(text, encoding="utf-8")

    (report,) = apply_transforms(tmp_path, (transform,), globstar="one_or_more")

    assert (tmp_path / "m.py").read_text(encoding="utf-8") == text
    assert (tmp_path / "sub" / "m.py").read_text(encoding="utf-8") != text
    assert [file.destination for file in report.files] == ["sub/m.py"]


def test_apply_transform_alone_walks_root_and_defaults_to_one_or_more(
    tmp_path: Path,
):
    """Called without a listing or a globstar, it lists ``root`` itself."""
    (tmp_path / "sub").mkdir()
    (tmp_path / "m.py").write_text("X\n", encoding="utf-8")
    (tmp_path / "sub" / "m.py").write_text("X\n", encoding="utf-8")
    transform = Transform(
        id="swap",
        type="replace",
        path="**/m.py",
        before="X",
        after="Y",
    )

    report = transforms.apply_transform(
        tmp_path,
        transform=transform,
        sources_by_destination={},
    )

    assert (tmp_path / "m.py").read_text(encoding="utf-8") == "X\n"
    assert (tmp_path / "sub" / "m.py").read_text(encoding="utf-8") == "Y\n"
    assert (report.id, report.type, report.path) == ("swap", "replace", "**/m.py")


def _entry(source: str, destination: str) -> ManifestEntry:
    return ManifestEntry(
        source=source,
        destination=destination,
        size=0,
        sha256="",
    )


if __name__ == "__main__":
    from copybarista.lib.testing.main import test_main

    test_main(__file__)
