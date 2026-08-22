"""Tests for change-request imports from public trees."""

from __future__ import annotations

from pathlib import Path

import json
import shutil
import stat
import subprocess

import pytest

from copybarista.cli import main
from copybarista.config import FileMove, Transform, load_config
from copybarista.errors import ImportRequestError
from copybarista.import_request import (
    ChangeRequestImporter,
    ImportRequest,
    PathMapper,
    TreeSnapshot,
    _anchor_source_only_regions,
    _removed_regions,
    _reverse_file_moves,
    _reverse_relocation,
    _ruff_format_matches,
    _splice_source_only_regions,
    _three_way_merge,
    import_change_request,
)
from copybarista.transforms import strip_source_text
from copybarista.workflow import MoveSequence, _relocate_path


def test_import_public_edit_maps_to_source_root_and_reverses_replace(
    tmp_path: Path,
):
    paths = _fixture(tmp_path)
    public_head = _copy_tree(paths.public_base, tmp_path / "public-head")
    (public_head / "pkg/module.py").write_text(
        "from copybarista.public import api\nVALUE = 'head'\n",
        encoding="utf-8",
    )
    destination = _copy_tree(paths.source_base, tmp_path / "destination")

    result = import_change_request(
        ImportRequest(
            config=load_config(paths.config),
            public_base=paths.public_base,
            public_head=public_head,
            source_base=paths.source_base,
            destination=destination,
        )
    )

    assert [
        (change.public, change.source, change.action) for change in result.changes
    ] == [
        (
            "pkg/module.py",
            "internal/demo/pkg/module.py",
            "modified",
        )
    ]
    assert (destination / "internal/demo/pkg/module.py").read_text(
        encoding="utf-8"
    ) == ("from internal.demo import api\nVALUE = 'head'\n")


def test_tree_snapshot_ignores_vcs_metadata(tmp_path: Path):
    root = tmp_path / "checkout"
    (root / ".git" / "objects").mkdir(parents=True)
    (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (root / ".hg").mkdir()
    (root / ".hg" / "requires").write_text("revlogv1\n", encoding="utf-8")
    (root / "README.md").write_text("public\n", encoding="utf-8")

    assert sorted(TreeSnapshot.from_root(root).entries) == ["README.md"]


def test_import_no_verify_ignores_vcs_metadata_changes(tmp_path: Path):
    paths = _fixture(tmp_path)
    public_head = _copy_tree(paths.public_base, tmp_path / "public-head")
    (public_head / ".git").mkdir()
    (public_head / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    destination = _copy_tree(paths.source_base, tmp_path / "destination")

    result = import_change_request(
        ImportRequest(
            config=load_config(paths.config),
            public_base=paths.public_base,
            public_head=public_head,
            source_base=paths.source_base,
            destination=destination,
            verify=False,
        )
    )

    assert result.changes == ()
    assert not (destination / ".git").exists()


def test_import_root_source_root_keeps_paths_relative(tmp_path: Path):
    paths = _fixture(tmp_path, source_root="")
    public_head = _copy_tree(paths.public_base, tmp_path / "public-head")
    (public_head / "pkg/module.py").write_text(
        "from copybarista.public import api\nVALUE = 'root'\n",
        encoding="utf-8",
    )
    destination = _copy_tree(paths.source_base, tmp_path / "destination")

    result = import_change_request(
        ImportRequest(
            config=load_config(paths.config),
            public_base=paths.public_base,
            public_head=public_head,
            source_base=paths.source_base,
            destination=destination,
        )
    )

    assert result.changes[0].source == "pkg/module.py"
    assert (destination / "pkg/module.py").read_text(encoding="utf-8") == (
        "from internal.demo import api\nVALUE = 'root'\n"
    )


def test_import_strips_destination_prefix_before_mapping(tmp_path: Path):
    paths = _fixture(tmp_path, destination_prefix="demo")
    public_head = _copy_tree(paths.public_base, tmp_path / "public-head")
    (public_head / "demo" / "pkg" / "module.py").write_text(
        "from copybarista.public import api\nVALUE = 'prefixed'\n",
        encoding="utf-8",
    )
    destination = _copy_tree(paths.source_base, tmp_path / "destination")

    result = import_change_request(
        ImportRequest(
            config=load_config(paths.config),
            public_base=paths.public_base,
            public_head=public_head,
            source_base=paths.source_base,
            destination=destination,
        )
    )

    assert result.changes[0].public == "demo/pkg/module.py"
    assert result.changes[0].source == "internal/demo/pkg/module.py"
    assert (destination / "internal/demo/pkg/module.py").read_text(
        encoding="utf-8"
    ) == "from internal.demo import api\nVALUE = 'prefixed'\n"


def test_import_maps_extra_copy_destination_to_source(tmp_path: Path):
    config = tmp_path / "copy.barista.toml"
    config.write_text(
        """
        [workflow]
        name = "demo"
        mode = "squash"
        source_root = "internal/demo"

        [files]
        include = ["**"]

        [[files.moves]]
        path = ""
        destination = "demo"

        [[files.copy]]
        source = "shared/web"
        destination = "demo/lib/web"
        include = ["*.py"]
        exclude = ["*_test.py"]
        """,
        encoding="utf-8",
    )

    mapper = PathMapper(config=load_config(config))

    assert mapper.source_path("demo/lib/web/search.py") == "shared/web/search.py"
    with pytest.raises(ImportRequestError, match="unmapped"):
        mapper.source_path("demo/lib/web/search_test.py")


def test_import_maps_extra_copy_file_destination_to_source(tmp_path: Path):
    config = tmp_path / "copy.barista.toml"
    config.write_text(
        """
        [workflow]
        name = "demo"
        mode = "squash"
        source_root = "internal/demo"

        [files]
        include = ["**"]

        [[files.copy]]
        source = "shared/json.py"
        destination = "demo/lib/json.py"
        """,
        encoding="utf-8",
    )

    mapper = PathMapper(config=load_config(config))

    assert mapper.source_path("demo/lib/json.py") == "shared/json.py"


def test_import_maps_root_copy_destination_to_source(tmp_path: Path):
    """A `[[files.copy]]` with `destination = "."` maps root files back.

    Verbatim-ship staging dirs (e.g. `<pkg>/.export`) copy to the public root
    (`destination = "."`), so a public edit to a root file like
    `pyproject.toml` or `CONTRIBUTING.md` must map back to `<source>/<file>`
    instead of failing the whole import as unmapped.
    """
    config = tmp_path / "copy.barista.toml"
    config.write_text(
        """
        [workflow]
        name = "demo"
        mode = "squash"
        source_root = "internal/demo"

        [files]
        include = ["**"]

        [[files.moves]]
        path = ""
        destination = "demo"

        [[files.copy]]
        source = "internal/demo/.export"
        destination = "."
        """,
        encoding="utf-8",
    )

    mapper = PathMapper(config=load_config(config))

    assert (
        mapper.source_path("pyproject.toml") == "internal/demo/.export/pyproject.toml"
    )
    assert (
        mapper.source_path("CONTRIBUTING.md") == "internal/demo/.export/CONTRIBUTING.md"
    )


def test_import_root_copy_yields_to_back_move(tmp_path: Path):
    """A root path a `[[files.moves]]` back-move keeps maps to the main sweep.

    When a package nests under a whole-tree move but a repo-metadata file
    (e.g. `README.md`) is moved back to the public root by an ordered back-move,
    that file is placed by the main source-root selection (a back-move in
    Copybara), NOT by the verbatim-ship `.export` copy to `.`. The reverse map
    must therefore return `<source_root>/README.md`, not `<source>/.export/...`.

    Regression: the `destination = "."` copy claimed EVERY root-level path
    matching its `**` include, so `README.md` wrongly mapped into `.export/`.
    On re-export the imported tree then held README.md at both the root (via
    the main sweep) and inside `.export` (via the copy), colliding with
    ``Export destination already exists: ./README.md``. Matches real Copybara
    reverse, which maps a public-root `README.md` back to `<source_root>`.
    """
    config = tmp_path / "copy.barista.toml"
    config.write_text(
        """
        [workflow]
        name = "demo"
        mode = "squash"
        source_root = "internal/demo"

        [files]
        include = ["**"]

        [[files.moves]]
        path = ""
        destination = "demo"

        [[files.moves]]
        path = "demo/README.md"
        destination = "README.md"

        [[files.copy]]
        source = "internal/demo/.export"
        destination = "."
        """,
        encoding="utf-8",
    )

    mapper = PathMapper(config=load_config(config))

    # README stays at root via the back-move -> main sweep source.
    assert mapper.source_path("README.md") == "internal/demo/README.md"
    # A non-excluded root file is still owned by the .export copy.
    assert (
        mapper.source_path("pyproject.toml") == "internal/demo/.export/pyproject.toml"
    )


def test_reverse_moves_is_pointwise_inverse_of_move_sequence():
    """`_reverse_file_moves` is the exact left inverse of `MoveSequence` per path.

    Every source-relative path the ordered moves place must round-trip: forward
    through `MoveSequence`, back through `_reverse_file_moves`, recovering the input
    and reporting that a move matched. Ordering matters -- the back-move for
    ``pkg/README.md`` runs after the whole-tree move forward, so its inverse must
    run first, or a package README would reverse to the wrong prefix-space path.
    """
    moves = (
        FileMove(path="", destination="demo"),
        FileMove(path="demo/README.md", destination="README.md"),
        FileMove(path="demo/docs", destination="docs"),
    )
    sequence = MoveSequence(moves=moves)
    for source_relative in (
        "pkg/module.py",
        "README.md",
        "docs/guide.md",
        "docs/nested/deep.md",
    ):
        public = sequence.destination_path(source_relative)
        recovered, moved = _reverse_file_moves(public, moves)
        assert moved
        assert recovered == source_relative


def test_relocate_path_and_reverse_relocation_are_inverses():
    """The shared forward/reverse relocation helpers round-trip every case.

    `_relocate_path` is the single forward rule for both `files.moves` and `move`
    transforms; `_reverse_relocation` inverts it. Exact match, subtree, whole-tree
    prefix, and a non-matching path must all round-trip (the last reporting no
    match), guarding the two collapsed appliers against divergence.
    """
    cases = (
        ("src", "src", "dst"),
        ("pkg/deep/mod.py", "pkg", "dst"),
        ("anything/x", "", "prefix"),
    )
    for path, source, destination in cases:
        forward = _relocate_path(path, source=source, destination=destination)
        recovered = _reverse_relocation(forward, source=source, destination=destination)
        assert recovered == path
    # A path the move does not touch: forward leaves it, reverse reports no match.
    assert _relocate_path("other/x", source="pkg", destination="dst") == "other/x"
    assert _reverse_relocation("other/x", source="pkg", destination="dst") is None


def test_reverse_moves_leaves_unmoved_path_untouched():
    """A path no move places round-trips unchanged and reports `moved=False`.

    Mirrors Copybara's unmatched ``core.move``: an out-of-prefix repo-root path
    (a ``typings/brotli`` class file) is not relocated, so its reverse is the
    identity and the caller keeps it at its shared source/public path.
    """
    moves = (FileMove(path="", destination="demo"),)
    # The whole-tree move places every path, so use a config where only a subtree
    # is captured: a bare back-move with no whole-tree move leaves siblings alone.
    partial = (FileMove(path="docs", destination="public_docs"),)
    recovered, moved = _reverse_file_moves("typings/brotli/__init__.pyi", partial)
    assert recovered == "typings/brotli/__init__.pyi"
    assert not moved
    # The whole-tree move, by contrast, captures everything.
    _, moved_all = _reverse_file_moves("demo/x.py", moves)
    assert moved_all


def test_reverse_moves_exactly_inverts_forward_placement(tmp_path: Path):
    """Reverse import inverts the ordered `[[files.moves]]` placement exactly.

    Forward export nests the tree under a prefix then lifts back-moved subtrees
    to the root; the importer must undo that same sequence in reverse order so a
    public path maps to precisely the source it came from. A back-moved subtree
    (``docs/``), a package file that keeps the prefix (``pkg/module.py``), and a
    root-level back-moved file (``README.md``) each round-trip to their source
    location, and the whole-tree move applied to a package path reverses to the
    source-root-relative path -- never the buried prefix-space path.
    """
    config = tmp_path / "copy.barista.toml"
    config.write_text(
        """
        [workflow]
        name = "demo"
        mode = "squash"
        source_root = "internal/demo"

        [files]
        include = ["**"]

        [[files.moves]]
        path = ""
        destination = "demo"

        [[files.moves]]
        path = "demo/README.md"
        destination = "README.md"

        [[files.moves]]
        path = "demo/docs"
        destination = "docs"
        """,
        encoding="utf-8",
    )

    mapper = PathMapper(config=load_config(config))

    # Whole-tree move reverses a prefix-space package path to source root.
    assert mapper.source_path("demo/pkg/module.py") == ("internal/demo/pkg/module.py")
    # Root-level back-move reverses to the source root, not `demo/README.md`.
    assert mapper.source_path("README.md") == "internal/demo/README.md"
    # Subtree back-move reverses each nested child to the source root.
    assert mapper.source_path("docs/guide.md") == "internal/demo/docs/guide.md"


def test_import_rejects_generated_file_destination(tmp_path: Path):
    config = tmp_path / "copy.barista.toml"
    config.write_text(
        """
        [workflow]
        name = "demo"
        mode = "squash"
        source_root = "internal/demo"

        [files]
        include = ["**"]

        [[files.write]]
        path = "demo/lib/web/__init__.py"
        content = ""
        """,
        encoding="utf-8",
    )

    mapper = PathMapper(config=load_config(config))

    with pytest.raises(ImportRequestError, match="unmapped"):
        mapper.source_path("demo/lib/web/__init__.py")


def test_import_public_edit_maps_moved_path_to_original_source(tmp_path: Path):
    source_base = tmp_path / "source-base"
    moved_source = source_base / "internal/demo/_stubs/pkg"
    moved_source.mkdir(parents=True)
    (moved_source / "__init__.py").write_text("VALUE = 'base'\n", encoding="utf-8")

    public_base = tmp_path / "public-base"
    public_pkg = public_base / "pkg"
    public_pkg.mkdir(parents=True)
    (public_pkg / "__init__.py").write_text("VALUE = 'base'\n", encoding="utf-8")

    public_head = _copy_tree(public_base, tmp_path / "public-head")
    (public_head / "pkg/__init__.py").write_text("VALUE = 'head'\n", encoding="utf-8")
    destination = _copy_tree(source_base, tmp_path / "destination")

    config = tmp_path / "copy.barista.toml"
    config.write_text(
        """
        [workflow]
        name = "demo"
        mode = "squash"
        source_root = "internal/demo"

        [files]
        include = ["**"]

        [[transform]]
        type = "move"
        path = "_stubs/pkg"
        destination = "pkg"
        """,
        encoding="utf-8",
    )

    result = import_change_request(
        ImportRequest(
            config=load_config(config),
            public_base=public_base,
            public_head=public_head,
            source_base=source_base,
            destination=destination,
        )
    )

    assert [(change.public, change.source) for change in result.changes] == [
        ("pkg/__init__.py", "internal/demo/_stubs/pkg/__init__.py")
    ]
    assert (destination / "internal/demo/_stubs/pkg/__init__.py").read_text(
        encoding="utf-8"
    ) == "VALUE = 'head'\n"
    assert not (destination / "internal/demo/pkg/__init__.py").exists()


def test_import_created_and_deleted_files(tmp_path: Path):
    paths = _fixture(tmp_path)
    public_head = _copy_tree(paths.public_base, tmp_path / "public-head")
    new_file = public_head / "pkg/new.py"
    new_file.write_text("VALUE = 'new'\n", encoding="utf-8")
    new_file.chmod(0o755)
    (public_head / "README.md").unlink()
    destination = _copy_tree(paths.source_base, tmp_path / "destination")

    result = import_change_request(
        ImportRequest(
            config=load_config(paths.config),
            public_base=paths.public_base,
            public_head=public_head,
            source_base=paths.source_base,
            destination=destination,
        )
    )

    assert [(change.public, change.action) for change in result.changes] == [
        ("README.md", "deleted"),
        ("pkg/new.py", "created"),
    ]
    assert not (destination / "internal/demo/README.md").exists()
    imported = destination / "internal/demo/pkg/new.py"
    assert imported.is_file()
    assert stat.S_IMODE(imported.stat().st_mode) & stat.S_IXUSR


def test_import_rolls_back_when_final_verification_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    paths = _fixture(tmp_path)
    public_head = _copy_tree(paths.public_base, tmp_path / "public-head")
    (public_head / "pkg/module.py").write_text(
        "from copybarista.public import api\nVALUE = 'changed'\n",
        encoding="utf-8",
    )
    destination = _copy_tree(paths.source_base, tmp_path / "destination")
    original = (destination / "internal/demo/pkg/module.py").read_text(encoding="utf-8")

    def fail_check(_self: ChangeRequestImporter) -> None:
        raise ImportRequestError("forced verification failure")

    monkeypatch.setattr(ChangeRequestImporter, "_check_public_head", fail_check)

    with pytest.raises(ImportRequestError, match="forced"):
        import_change_request(
            ImportRequest(
                config=load_config(paths.config),
                public_base=paths.public_base,
                public_head=public_head,
                source_base=paths.source_base,
                destination=destination,
            )
        )

    assert (destination / "internal/demo/pkg/module.py").read_text(
        encoding="utf-8"
    ) == original


def test_import_rejects_symlink_ancestor_in_destination(tmp_path: Path):
    paths = _fixture(tmp_path)
    public_head = _copy_tree(paths.public_base, tmp_path / "public-head")
    (public_head / "pkg/module.py").write_text(
        "from copybarista.public import api\nVALUE = 'changed'\n",
        encoding="utf-8",
    )
    destination = tmp_path / "destination"
    (destination / "internal").mkdir(parents=True)
    escape = tmp_path / "escape"
    escape.mkdir()
    (destination / "internal/demo").symlink_to(escape, target_is_directory=True)

    with pytest.raises(ImportRequestError, match="escapes destination"):
        import_change_request(
            ImportRequest(
                config=load_config(paths.config),
                public_base=paths.public_base,
                public_head=public_head,
                source_base=paths.source_base,
                destination=destination,
            )
        )

    assert not (escape / "pkg/module.py").exists()


def test_import_rejects_vcs_metadata_destination(tmp_path: Path):
    paths = _fixture(tmp_path)
    destination = tmp_path / "repo" / ".git"
    destination.mkdir(parents=True)

    with pytest.raises(ImportRequestError, match="VCS metadata"):
        import_change_request(
            ImportRequest(
                config=load_config(paths.config),
                public_base=paths.public_base,
                public_head=paths.public_base,
                source_base=paths.source_base,
                destination=destination,
            )
        )


def test_import_rejects_ambiguous_added_exported_text(tmp_path: Path):
    paths = _fixture(tmp_path)
    public_head = _copy_tree(paths.public_base, tmp_path / "public-head")
    (public_head / "pkg/module.py").write_text(
        "from copybarista.public import api\n"
        "MESSAGE = 'from copybarista.public appears in public docs'\n",
        encoding="utf-8",
    )

    with pytest.raises(ImportRequestError, match="adds exported replacement"):
        import_change_request(
            ImportRequest(
                config=load_config(paths.config),
                public_base=paths.public_base,
                public_head=public_head,
                source_base=paths.source_base,
                destination=_copy_tree(paths.source_base, tmp_path / "destination"),
            )
        )


def test_import_rejects_source_base_with_natural_exported_text(tmp_path: Path):
    paths = _fixture(tmp_path)
    source_file = paths.source_base / "internal/demo/pkg/module.py"
    source_file.write_text(
        "from internal.demo import api\n"
        "MESSAGE = 'from copybarista.public appears naturally'\n",
        encoding="utf-8",
    )
    public_base = _copy_tree(paths.public_base, tmp_path / "public-base-natural")
    (public_base / "pkg/module.py").write_text(
        "from copybarista.public import api\n"
        "MESSAGE = 'from copybarista.public appears naturally'\n",
        encoding="utf-8",
    )
    public_head = _copy_tree(public_base, tmp_path / "public-head")
    (public_head / "pkg/module.py").write_text(
        "from copybarista.public import api\n"
        "MESSAGE = 'from copybarista.public still appears naturally'\n",
        encoding="utf-8",
    )

    with pytest.raises(ImportRequestError, match="already contains"):
        import_change_request(
            ImportRequest(
                config=load_config(paths.config),
                public_base=public_base,
                public_head=public_head,
                source_base=paths.source_base,
                destination=_copy_tree(paths.source_base, tmp_path / "destination"),
            )
        )


def test_import_explicit_reversal_allows_natural_exported_text(tmp_path: Path):
    paths = _fixture(tmp_path)
    config = tmp_path / "copy-explicit-reverse.toml"
    config.write_text(
        """
        [workflow]
        name = "demo"
        mode = "squash"
        source_root = "internal/demo"

        [files]
        include = ["**"]
        exclude = ["private.txt"]

        [[transform]]
        type = "replace"
        path = "pkg/*.py"
        before = "from internal.demo"
        after = "from copybarista.public"
        reverse_before = "from copybarista.public import"
        reverse_after = "from internal.demo import"
        """,
        encoding="utf-8",
    )
    source_file = paths.source_base / "internal/demo/pkg/module.py"
    source_file.write_text(
        "from internal.demo import api\n"
        "MESSAGE = 'from copybarista.public appears naturally'\n",
        encoding="utf-8",
    )
    public_base = _copy_tree(paths.public_base, tmp_path / "public-base-natural")
    (public_base / "pkg/module.py").write_text(
        "from copybarista.public import api\n"
        "MESSAGE = 'from copybarista.public appears naturally'\n",
        encoding="utf-8",
    )
    public_head = _copy_tree(public_base, tmp_path / "public-head")
    (public_head / "pkg/module.py").write_text(
        "from copybarista.public import api\n"
        "MESSAGE = 'from copybarista.public still appears naturally'\n",
        encoding="utf-8",
    )
    destination = _copy_tree(paths.source_base, tmp_path / "destination")

    import_change_request(
        ImportRequest(
            config=load_config(config),
            public_base=public_base,
            public_head=public_head,
            source_base=paths.source_base,
            destination=destination,
            verify=False,
        )
    )

    assert (destination / "internal/demo/pkg/module.py").read_text(
        encoding="utf-8"
    ) == (
        "from internal.demo import api\n"
        "MESSAGE = 'from copybarista.public still appears naturally'\n"
    )


@pytest.mark.cli_python_subprocess
def test_import_reverse_replace_leaves_imports_isort_clean(tmp_path: Path):
    """Invariant: importing a public change must not pollute source with lint
    violations. A namespace ``replace`` is pure text substitution that preserves
    physical line order, so a public file whose imports are sorted under the
    *public* namespace can land unsorted under the *internal* namespace whenever
    the two namespaces sort their import groups differently. The import must
    re-apply ``ruff_format`` (isort) on the reversed source form so the written
    file is isort-clean.

    The fixture mirrors the real inversion: a public ``pub.lib`` member sorts
    before ``pub.providers``, but after the reverse rewrite the corresponding
    source members are a shallow package and a deeper one whose alphabetical
    order flips, so the public ordering is wrong under the source namespace.
    """
    source_base = tmp_path / "source-base"
    source_project = source_base / "internal/demo"
    (source_project / "pkg").mkdir(parents=True)
    # Source form: ``shallow.lib`` must sort AFTER ``deep.pkg`` (s > d), so the
    # isort-clean source orders providers first, lib second.
    (source_project / "pkg/module.py").write_text(
        "from deep.pkg.providers import client\n"
        "from shallow.lib import util\n"
        "\n"
        "VALUE = (client, util)\n",
        encoding="utf-8",
    )

    public_base = tmp_path / "public-base"
    (public_base / "pkg").mkdir(parents=True)
    # Public form: ``pub.lib`` sorts BEFORE ``pub.providers`` (l < p): lib first.
    public_body = (
        "from pub.lib import util\n"
        "from pub.providers import client\n"
        "\n"
        "VALUE = (client, util)\n"
    )
    (public_base / "pkg/module.py").write_text(public_body, encoding="utf-8")

    public_head = _copy_tree(public_base, tmp_path / "public-head")
    (public_head / "pkg/module.py").write_text(
        public_body.replace("'base'", "'head'") + "EXTRA = 1\n",
        encoding="utf-8",
    )

    config = tmp_path / "copy.barista.toml"
    config.write_text(
        """
        [workflow]
        name = "demo"
        mode = "squash"
        source_root = "internal/demo"

        [files]
        include = ["**"]

        [[transform]]
        type = "replace"
        path = "pkg/*.py"
        before = "from shallow.lib"
        after = "from pub.lib"

        [[transform]]
        type = "replace"
        path = "pkg/*.py"
        before = "from deep.pkg.providers"
        after = "from pub.providers"

        [[transform]]
        type = "ruff_format"
        path = "pkg/module.py"
        """,
        encoding="utf-8",
    )

    destination = _copy_tree(source_base, tmp_path / "destination")
    # The source tree's ruff config drives isort grouping; enable it so the
    # post-import reformat re-sorts under the source namespace.
    (destination / "pyproject.toml").write_text(
        '[tool.ruff.lint]\nselect = ["I"]\n', encoding="utf-8"
    )
    import_change_request(
        ImportRequest(
            config=load_config(config),
            public_base=public_base,
            public_head=public_head,
            source_base=source_base,
            destination=destination,
            verify=False,
        )
    )

    written = (destination / "internal/demo/pkg/module.py").read_text(encoding="utf-8")
    # The reversed imports must be re-sorted into source-namespace order
    # (providers before lib), not left in public order (lib before providers).
    assert written.index("from deep.pkg.providers") < written.index(
        "from shallow.lib"
    ), f"imports left unsorted under source namespace:\n{written}"

    # Same invariant must hold on the merge-import path, which writes through a
    # separate three-way-merge branch.
    merge_dest = _copy_tree(source_base, tmp_path / "merge-destination")
    (merge_dest / "pyproject.toml").write_text(
        '[tool.ruff.lint]\nselect = ["I"]\n', encoding="utf-8"
    )
    import_change_request(
        ImportRequest(
            config=load_config(config),
            public_base=public_base,
            public_head=public_head,
            source_base=source_base,
            destination=merge_dest,
            merge_import=True,
            verify=False,
        )
    )
    merged = (merge_dest / "internal/demo/pkg/module.py").read_text(encoding="utf-8")
    assert merged.index("from deep.pkg.providers") < merged.index("from shallow.lib"), (
        f"merge import left imports unsorted under source namespace:\n{merged}"
    )


def test_import_reformats_with_whole_tree_ruff_format_path(tmp_path: Path):
    """A whole-tree ``ruff_format`` (``path = "."``) reformats imported files.

    Every shipped config declares ``ruff_format`` with ``path = "."`` (format the
    whole staged tree), not a per-file glob. The post-import reformat must treat
    that whole-tree marker as matching every reversed file; otherwise it never
    runs and a namespace reversal that reorders import groups lands unsorted,
    tripping ``I001`` in the import PR (the wesearch s2 import regression).
    """
    source_base = tmp_path / "source-base"
    source_project = source_base / "internal/demo"
    (source_project / "pkg").mkdir(parents=True)
    (source_project / "pkg/module.py").write_text(
        "from deep.pkg.providers import client\n"
        "from shallow.lib import util\n"
        "\n"
        "VALUE = (client, util)\n",
        encoding="utf-8",
    )
    public_base = tmp_path / "public-base"
    (public_base / "pkg").mkdir(parents=True)
    public_body = (
        "from pub.lib import util\n"
        "from pub.providers import client\n"
        "\n"
        "VALUE = (client, util)\n"
    )
    (public_base / "pkg/module.py").write_text(public_body, encoding="utf-8")
    public_head = _copy_tree(public_base, tmp_path / "public-head")
    (public_head / "pkg/module.py").write_text(
        public_body + "EXTRA = 1\n", encoding="utf-8"
    )
    config = tmp_path / "copy.barista.toml"
    config.write_text(
        """
        [workflow]
        name = "demo"
        mode = "squash"
        source_root = "internal/demo"

        [files]
        include = ["**"]

        [[transform]]
        type = "replace"
        path = "pkg/*.py"
        before = "from shallow.lib"
        after = "from pub.lib"

        [[transform]]
        type = "replace"
        path = "pkg/*.py"
        before = "from deep.pkg.providers"
        after = "from pub.providers"

        [[transform]]
        type = "ruff_format"
        path = "."
        """,
        encoding="utf-8",
    )
    destination = _copy_tree(source_base, tmp_path / "destination")
    (destination / "pyproject.toml").write_text(
        '[tool.ruff.lint]\nselect = ["I"]\n', encoding="utf-8"
    )

    import_change_request(
        ImportRequest(
            config=load_config(config),
            public_base=public_base,
            public_head=public_head,
            source_base=source_base,
            destination=destination,
            verify=False,
        )
    )

    written = (destination / "internal/demo/pkg/module.py").read_text(encoding="utf-8")
    assert written.index("from deep.pkg.providers") < written.index(
        "from shallow.lib"
    ), f"whole-tree ruff_format did not reformat imported file:\n{written}"


@pytest.mark.parametrize(
    ("ruff_path", "public_path", "expected"),
    [
        # Whole-tree markers match every file.
        (".", "pkg/module.py", True),
        ("", "pkg/module.py", True),
        ("./", "module.py", True),
        # A subtree path matches the directory and everything under it, mirroring
        # forward ``root / transform.path`` (a whole-subtree format), but nothing
        # outside it -- and not a sibling sharing the name as a prefix.
        ("pkg", "pkg", True),
        ("pkg", "pkg/module.py", True),
        ("pkg", "pkg/sub/deep.py", True),
        ("pkg/", "pkg/module.py", True),
        ("pkg", "other/module.py", False),
        ("pkg", "pkgother/module.py", False),
        # A single-file target matches exactly that file.
        ("pkg/module.py", "pkg/module.py", True),
        ("pkg/module.py", "pkg/other.py", False),
    ],
)
def test_ruff_format_matches_treats_path_as_subtree(
    ruff_path: str, public_path: str, expected: bool
) -> None:
    """``ruff_format`` path matching mirrors the forward whole-subtree format.

    Forward ``_ruff_format`` formats ``root / transform.path`` as a subtree, so a
    subdir path (``pkg``) must match every file under it on import -- not only the
    literal path string. A literal-glob match would silently skip the post-import
    reformat for every file under a non-``"."`` target.
    """
    transform = Transform(
        id="fmt", type="ruff_format", path=ruff_path, required=False, reversible=True
    )
    assert _ruff_format_matches(transform, public_path) is expected


def test_import_rejects_empty_after_reverse_replace(tmp_path: Path):
    paths = _fixture(tmp_path, with_transform=False)
    config = tmp_path / "copy-empty-after.toml"
    config.write_text(
        """
        [workflow]
        name = "demo"
        mode = "squash"
        source_root = "internal/demo"

        [files]
        include = ["**"]
        exclude = ["private.txt"]

        [[transform]]
        type = "replace"
        path = "pkg/*.py"
        before = "from internal.demo"
        after = ""
        """,
        encoding="utf-8",
    )
    public_base = _copy_tree(paths.public_base, tmp_path / "public-base-empty-after")
    (public_base / "pkg/module.py").write_text(
        " import api\nVALUE = 'base'\n",
        encoding="utf-8",
    )
    public_head = _copy_tree(public_base, tmp_path / "public-head")
    (public_head / "pkg/module.py").write_text(
        " import api\nVALUE = 'head'\n",
        encoding="utf-8",
    )

    with pytest.raises(ImportRequestError, match="empty replacement"):
        import_change_request(
            ImportRequest(
                config=load_config(config),
                public_base=public_base,
                public_head=public_head,
                source_base=paths.source_base,
                destination=_copy_tree(paths.source_base, tmp_path / "destination"),
            )
        )


def test_import_allows_relative_symlink_staying_inside(tmp_path: Path):
    paths = _fixture(tmp_path)
    public_head = _copy_tree(paths.public_base, tmp_path / "public-head")
    (public_head / "pkg/readme").symlink_to("../README.md")
    destination = _copy_tree(paths.source_base, tmp_path / "destination")

    result = import_change_request(
        ImportRequest(
            config=load_config(paths.config),
            public_base=paths.public_base,
            public_head=public_head,
            source_base=paths.source_base,
            destination=destination,
        )
    )

    assert [(change.public, change.action) for change in result.changes] == [
        ("pkg/readme", "created")
    ]
    assert (destination / "internal/demo/pkg/readme").is_symlink()


def test_import_rejects_relative_symlink_escaping_public_tree(tmp_path: Path):
    paths = _fixture(tmp_path)
    public_head = _copy_tree(paths.public_base, tmp_path / "public-head")
    (public_head / "pkg/readme").symlink_to("../../escape")

    with pytest.raises(ImportRequestError, match="Symlink target escapes"):
        import_change_request(
            ImportRequest(
                config=load_config(paths.config),
                public_base=paths.public_base,
                public_head=public_head,
                source_base=paths.source_base,
                destination=_copy_tree(paths.source_base, tmp_path / "destination"),
            )
        )


def test_import_rejects_excluded_public_path(tmp_path: Path):
    paths = _fixture(tmp_path)
    public_head = _copy_tree(paths.public_base, tmp_path / "public-head")
    (public_head / "private.txt").write_text("secret\n", encoding="utf-8")

    with pytest.raises(ImportRequestError, match="excluded or unmapped"):
        import_change_request(
            ImportRequest(
                config=load_config(paths.config),
                public_base=paths.public_base,
                public_head=public_head,
                source_base=paths.source_base,
                destination=_copy_tree(paths.source_base, tmp_path / "destination"),
            )
        )


def test_import_reinserts_stripped_block_from_source(tmp_path: Path):
    """A public edit to a strip_block file imports by re-inserting the source
    block verbatim, rather than failing the whole import.

    strip_block is not invertible (the block is absent from the public tree),
    so the importer splices the source's block back at its original position and
    applies the public edit around it. Re-exporting strips the block again, so
    the destination still reproduces the public head; the import PR's CI is the
    human-review gate for any semantic conflict.
    """
    paths = _fixture(tmp_path, include_strip_block=True)
    public_head = _copy_tree(paths.public_base, tmp_path / "public-head")
    (public_head / "README.md").write_text("public edit\n", encoding="utf-8")
    destination = _copy_tree(paths.source_base, tmp_path / "destination")

    import_change_request(
        ImportRequest(
            config=load_config(paths.config),
            public_base=paths.public_base,
            public_head=public_head,
            source_base=paths.source_base,
            destination=destination,
        )
    )

    # The imported README carries the public edit AND the source-only block.
    imported = (destination / "internal/demo/README.md").read_text(encoding="utf-8")
    assert imported == (
        "public edit\n<!-- internal:start -->\nprivate\n<!-- internal:end -->\n"
    )


def _internal_lines_config(tmp_path: Path) -> Path:
    """Write a minimal config with one ``internal_lines`` transform."""
    config = tmp_path / "copy.barista.toml"
    config.write_text(
        """
        [workflow]
        name = "demo"
        mode = "squash"
        source_root = "internal/demo"

        [files]
        include = ["**"]

        [[transform]]
        type = "internal_lines"
        path = "pkg/*.py"
        start = "# copybarista:internal"
        required = false
        """,
        encoding="utf-8",
    )
    return config


def _run_internal_lines_import(
    *, tmp_path: Path, config: Path, source_module: str, public_head_module: str
) -> Path:
    """Export the source, apply the public edit, import it back, return the dest file."""
    transform = load_config(config).transforms[0]
    source_base = tmp_path / "source-base"
    (source_base / "internal/demo/pkg").mkdir(parents=True)
    (source_base / "internal/demo/pkg/m.py").write_text(source_module, encoding="utf-8")
    public_base = tmp_path / "public-base"
    (public_base / "pkg").mkdir(parents=True)
    (public_base / "pkg/m.py").write_text(
        strip_source_text(source_module, transform), encoding="utf-8"
    )
    public_head = tmp_path / "public-head"
    (public_head / "pkg").mkdir(parents=True)
    (public_head / "pkg/m.py").write_text(public_head_module, encoding="utf-8")
    destination = _copy_tree(source_base, tmp_path / "destination")
    import_change_request(
        ImportRequest(
            config=load_config(config),
            public_base=public_base,
            public_head=public_head,
            source_base=source_base,
            destination=destination,
            merge_import=True,
        )
    )
    return destination / "internal/demo/pkg/m.py"


def test_merge_import_reconciles_collapse_with_surviving_neighbor(
    tmp_path: Path,
):
    """A collapse merges when a kept neighbor line still ALIGNS to the public text.

    Source has a multi-line tuple whose entries carry ``# copybarista:internal``
    (stripped on export); a public edit collapses the tuple to one line, reflowing
    the entry lines past verbatim recognition -- but a bracketing kept line
    (``return items``) survives unchanged. Diffing the source's exported form
    against public aligns that surviving line, so the importer re-inserts the
    stripped line beside its nearest aligned neighbor (right before ``return``).
    The result re-strips to public head exactly, so re-export reproduces public.
    Placement is left for human review in the import PR.
    """
    config = _internal_lines_config(tmp_path)
    source_module = (
        "def fn(name):\n"
        "    items = (\n"
        '        "ITEM_A",\n'
        '        "SECRET_B",  # copybarista:internal\n'
        "    )\n"
        "    return items\n"
    )
    public_head_module = (
        "def fn(name, *, extra):\n"
        '    items = ("ITEM_NEW", "ITEM_A")\n'
        "    return items\n"
    )

    imported = _run_internal_lines_import(
        tmp_path=tmp_path,
        config=config,
        source_module=source_module,
        public_head_module=public_head_module,
    ).read_text(encoding="utf-8")

    # Public rewrite landed AND the source-only internal line survived.
    assert '"SECRET_B",  # copybarista:internal' in imported
    assert "extra" in imported
    assert "ITEM_NEW" in imported
    # Placement lands on its OWN line -- never spliced into the middle of a
    # surviving public line.
    for line in imported.splitlines():
        if "# copybarista:internal" in line:
            assert line.lstrip().startswith('"SECRET_B"')
    # The import's correctness contract: re-export reproduces the public head.
    transform = load_config(config).transforms[0]
    assert strip_source_text(imported, transform) == public_head_module


def test_merge_import_appends_trailing_run_when_all_context_rewritten(tmp_path: Path):
    """A trailing run whose whole context was rewritten APPENDS at end, not reject.

    The public rewrite replaces every line of the (only, last) function; the
    stripped line has no surviving kept line before OR after it. Its position is
    then unambiguous: nothing survived after it, so it appends at the end. The
    result re-strips to public head exactly, so re-export reproduces public --
    placement is left for human review in the import PR.
    """
    config = _internal_lines_config(tmp_path)
    source_module = (
        "def fallbacks(name):\n"
        "    items = (\n"
        '        "COMMON",\n'
        '        "SECRET",  # copybarista:internal\n'
        "    )\n"
        "    return items\n"
    )
    public_head_module = (
        "def fallbacks(name, *, allow):\n"
        '    picked = ("NEW", "COMMON")\n'
        "    return [p for p in picked if p in allow]\n"
    )
    imported = _run_internal_lines_import(
        tmp_path=tmp_path,
        config=config,
        source_module=source_module,
        public_head_module=public_head_module,
    ).read_text(encoding="utf-8")

    assert '"SECRET",  # copybarista:internal' in imported
    transform = load_config(config).transforms[0]
    assert strip_source_text(imported, transform) == public_head_module


def test_merge_import_never_jumps_run_backward_into_earlier_scope(tmp_path: Path):
    """A run whose preceding context was rewritten anchors FORWARD, never backward.

    The stripped run's own function body is rewritten in public (no line inside it
    aligns), but an UNRELATED earlier line (a module-level import) still aligns.
    Placement must NOT jump backward to that earlier line -- dropping the run at
    module top level, an entirely different scope, which the re-strip gate cannot
    catch (the marker line strips away wherever it lands). Instead it anchors to
    the nearest aligned line AT/AFTER the run, so the run lands at the tail of the
    rewritten span it belonged to (here within ``a``, before ``def b``).
    """
    config = _internal_lines_config(tmp_path)
    source_module = (
        "import os\n"
        "def a():\n"
        "    tmp = compute()\n"
        "    secret = 0  # copybarista:internal\n"
        "    return tmp\n"
        "def b():\n"
        "    return 2\n"
    )
    public_head_module = (
        "import os\ndef a(x):\n    return x * 2\ndef b():\n    return 2\n"
    )
    imported = _run_internal_lines_import(
        tmp_path=tmp_path,
        config=config,
        source_module=source_module,
        public_head_module=public_head_module,
    ).read_text(encoding="utf-8")

    lines = imported.splitlines()
    secret_line = next(i for i, s in enumerate(lines) if "secret = 0" in s)
    import_line = next(i for i, s in enumerate(lines) if s.startswith("import os"))
    def_a_line = next(i for i, s in enumerate(lines) if s.startswith("def a"))
    def_b_line = next(i for i, s in enumerate(lines) if s.startswith("def b"))
    # Must NOT sit at module top (right after 'import os', before def a).
    assert secret_line != import_line + 1
    # Lands within a's region: after def a, before def b.
    assert def_a_line < secret_line < def_b_line
    transform = load_config(config).transforms[0]
    assert strip_source_text(imported, transform) == public_head_module


def test_merge_import_appends_run_when_last_function_wholly_rewritten(tmp_path: Path):
    """A run in the last function, whose whole body was rewritten, appends at end.

    The run's preceding context is rewritten AND no kept line aligns after it, so
    it is trailing: append at the end. Re-export strips it again, reproducing the
    public head; placement is left for human review in the import PR.
    """
    config = _internal_lines_config(tmp_path)
    source_module = (
        "def a(name):\n"
        "    tmp = build()\n"
        "    secret = 0  # copybarista:internal\n"
        "    return tmp\n"
    )
    public_head_module = "def a(name, *, extra):\n    return [extra, 1, 2]\n"
    imported = _run_internal_lines_import(
        tmp_path=tmp_path,
        config=config,
        source_module=source_module,
        public_head_module=public_head_module,
    ).read_text(encoding="utf-8")

    assert "secret = 0  # copybarista:internal" in imported
    transform = load_config(config).transforms[0]
    assert strip_source_text(imported, transform) == public_head_module


def test_merge_import_reverses_else_block_with_public_edit_below(tmp_path: Path):
    """An ``if internal/else/endif`` block reverses even with a public edit nearby.

    An else-branch ``strip_block`` does not delete a region: on export it replaces
    the whole block with its else branch, uncommented. The importer must reverse it
    by restoring the full source block (internal branch + markers) around the
    public edit, not refuse. Regression: else-branch strips raised "cannot be
    reversed by re-insertion", wedging any import of a file that carries one.
    Re-export replays the same substitution, reproducing the public head.
    """
    source_module = (
        "HEADER = 0\n"
        "# copybarista:if internal\n"
        '_DEFAULT = "INTERNAL_VALUE"\n'
        "# copybarista:else\n"
        '# _DEFAULT = "PUBLIC_VALUE"\n'
        "# copybarista:endif\n"
        "def f():\n"
        "    return 1\n"
    )
    config = tmp_path / "copy.barista.toml"
    config.write_text(
        """
        [workflow]
        name = "demo"
        mode = "squash"
        source_root = "internal/demo"

        [files]
        include = ["**"]

        [[transform]]
        type = "strip_block"
        path = "pkg/*.py"
        start = "# copybarista:if internal"
        else = "# copybarista:else"
        end = "# copybarista:endif"
        required = false
        """,
        encoding="utf-8",
    )
    transform = load_config(config).transforms[0]
    exported = strip_source_text(source_module, transform)
    # public edit: change the function body BELOW the else block.
    public_head_module = exported.replace("return 1", "return 2")

    source_base = tmp_path / "source-base"
    (source_base / "internal/demo/pkg").mkdir(parents=True)
    (source_base / "internal/demo/pkg/m.py").write_text(source_module, encoding="utf-8")
    public_base = tmp_path / "public-base"
    (public_base / "pkg").mkdir(parents=True)
    (public_base / "pkg/m.py").write_text(exported, encoding="utf-8")
    public_head = tmp_path / "public-head"
    (public_head / "pkg").mkdir(parents=True)
    (public_head / "pkg/m.py").write_text(public_head_module, encoding="utf-8")
    destination = _copy_tree(source_base, tmp_path / "destination")

    import_change_request(
        ImportRequest(
            config=load_config(config),
            public_base=public_base,
            public_head=public_head,
            source_base=source_base,
            destination=destination,
            merge_import=True,
        )
    )

    imported = (destination / "internal/demo/pkg/m.py").read_text(encoding="utf-8")
    # The internal branch and its markers are restored around the public edit.
    assert "# copybarista:if internal" in imported
    assert '_DEFAULT = "INTERNAL_VALUE"' in imported
    assert "return 2" in imported
    # Re-export reproduces the public head.
    assert strip_source_text(imported, transform) == public_head_module


def test_merge_import_reconciles_public_rewrite_of_stripped_region_context(
    tmp_path: Path,
):
    """A public rewrite BELOW an ``internal_lines`` region merges, not refuses.

    A public edit rewrites lines beneath a stripped region, leaving the region's
    neighbor lines intact. The offset splice no longer re-strips back to the
    incoming public text (the size change above shifts the offset), so the old
    per-file gate refused with "does not reproduce". The importer must instead
    anchor the source-only line to its (unique, surviving) neighbor and re-insert
    it there. Re-export then strips the line again, reproducing public head.
    """
    source_module = (
        "HEADER = 1\n"
        "\n"
        "SECRET = 2  # copybarista:internal\n"
        "\n"
        "def public_fn(name):\n"
        '    return "old body"\n'
    )
    # The public author rewrites lines BELOW the stripped region (the function
    # body), leaving the marker line untouched. The insertion offset for the
    # source-only line is displaced by the size change above it, so the plain
    # offset splice no longer re-strips back to public -- but diff3 reconciles
    # cleanly because nothing overlaps the marker line itself.
    public_head_module = (
        "HEADER = 1\n"
        "\n"
        "\n"
        "def public_fn(name, *, extra):\n"
        '    result = "new body"\n'
        "    return result\n"
    )
    source_base = tmp_path / "source-base"
    (source_base / "internal/demo/pkg").mkdir(parents=True)
    (source_base / "internal/demo/pkg/m.py").write_text(source_module, encoding="utf-8")
    config = tmp_path / "copy.barista.toml"
    config.write_text(
        """
        [workflow]
        name = "demo"
        mode = "squash"
        source_root = "internal/demo"

        [files]
        include = ["**"]

        [[transform]]
        type = "internal_lines"
        path = "pkg/*.py"
        start = "# copybarista:internal"
        required = false
        """,
        encoding="utf-8",
    )
    # public base = the source's real export (internal line stripped).
    stripped = strip_source_text(
        source_module,
        load_config(config).transforms[0],
    )
    public_base = tmp_path / "public-base"
    (public_base / "pkg").mkdir(parents=True)
    (public_base / "pkg/m.py").write_text(stripped, encoding="utf-8")
    public_head = tmp_path / "public-head"
    (public_head / "pkg").mkdir(parents=True)
    (public_head / "pkg/m.py").write_text(public_head_module, encoding="utf-8")
    destination = _copy_tree(source_base, tmp_path / "destination")

    import_change_request(
        ImportRequest(
            config=load_config(config),
            public_base=public_base,
            public_head=public_head,
            source_base=source_base,
            destination=destination,
            merge_import=True,
        )
    )

    imported = (destination / "internal/demo/pkg/m.py").read_text(encoding="utf-8")
    # The public rewrite landed AND the source-only internal line was preserved.
    assert "SECRET = 2  # copybarista:internal" in imported
    assert "extra" in imported
    assert "new body" in imported
    # Re-stripping the imported source reproduces the public head exactly.
    assert (
        strip_source_text(imported, load_config(config).transforms[0])
        == public_head_module
    )


@pytest.mark.parametrize(
    ("transform", "source", "edit_from", "edit_to"),
    [
        # Canonical block: start marker at line start, inclusive.
        (
            Transform(id="x", type="strip_block", path="m", start="# S", end="# E"),
            "keep a\n# S\nsecret\n# E\nkeep b\n",
            "keep a",
            "keep A",
        ),
        # internal_lines: one marker-carrying line per region.
        (
            Transform(id="x", type="internal_lines", path="m", start="# INT"),
            "import a\nimport secret  # INT\nVALUE = 1\n",
            "VALUE = 1",
            "VALUE = 2",
        ),
        # inclusive=False: export keeps the marker lines, removes only the
        # interior between them.
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
            "keep b",
            "keep B",
        ),
        # Mid-line start marker: text before the block does not end in a newline,
        # so export does NOT collapse the trailing newline after the end marker.
        (
            Transform(id="x", type="strip_block", path="m", start="# S", end="# E"),
            "prefix # S\nsecret\n# E\nkeep\n",
            "keep",
            "kept",
        ),
        # Multiple blocks in one file.
        (
            Transform(id="x", type="strip_block", path="m", start="# S", end="# E"),
            "a\n# S\nx\n# E\nb\n# S\ny\n# E\nc\n",
            "b",
            "B",
        ),
        # Removed block content coincides with kept text: a byte-diff derivation
        # mis-attributes the region (matches kept ``code`` against the exported
        # form); a marker-anchored derivation stays exact. The edit is after the
        # last block so no region offset shifts.
        (
            Transform(id="x", type="strip_block", path="m", start="# S", end="# E"),
            "# S\n# E\nimport os\n# S\n# E\ncode\ncode\n# S\ndata\n# E\ntail\n",
            "tail",
            "TAIL",
        ),
        # Blank lines after an inclusive block are collapsed into the cut, so the
        # removed region must include them for an exact round-trip.
        (
            Transform(id="x", type="strip_block", path="m", start="# S", end="# E"),
            "a\n# S\nx\n# E\n\n\nb\n",
            "b",
            "B",
        ),
    ],
)
def test_splice_source_only_regions_reinserts_and_round_trips(
    transform: Transform,
    source: str,
    edit_from: str,
    edit_to: str,
):
    """Re-inserting a transform's source-only regions must round-trip.

    The contract: derive the public tree from the REAL export of the source
    (``strip_source_text``, the same code the export runs), apply a public edit,
    splice the source's removed regions back into it, and re-export that result.
    Re-export must reproduce the edited public text exactly. The export function
    is the ground-truth oracle, so ``_removed_regions`` must agree with it across
    every block shape (inclusive/exclusive markers, mid-line markers, multiple
    blocks) or the round-trip breaks. A hand-written approximation of the
    stripping (the earlier bug) drifts from the real export and corrupts.
    """
    exported = strip_source_text(source, transform)
    public = exported.replace(edit_from, edit_to)
    reversed_text = _splice_source_only_regions(
        source_text=source, public_text=public, transform=transform
    )
    assert strip_source_text(reversed_text, transform) == public


_INTERNAL_LINES = Transform(id="il", type="internal_lines", path="m.py", start="# INT")


def test_anchor_preserves_source_order_of_multiple_runs():
    """Two source-only runs must keep their SOURCE order when placed together.

    When several runs collapse onto the same public anchor position (their local
    context was rewritten to a single line), inserting them must preserve their
    original file order. Regression: a bottom-up splice at one index prepended
    each later run, reversing them -- the two distinct removed regions came out
    swapped while re-strip still equalled public (the gate cannot see order).
    """
    source = (
        "aaa\n"
        "first_A = 0  # INT\n"
        "first_B = 0  # INT\n"
        "bbb\n"
        "second_A = 0  # INT\n"
        "second_B = 0  # INT\n"
        "tail\n"
    )
    public = "X\ntail\n"

    out = _anchor_source_only_regions(
        source_text=source, public_text=public, transform=_INTERNAL_LINES
    )
    assert out is not None
    assert strip_source_text(out, _INTERNAL_LINES) == public
    # first_* run must appear before second_* run.
    assert out.index("first_A") < out.index("second_A")


def test_anchor_rejects_non_monotonic_public_reorder():
    """A public block REORDER (non-monotonic alignment) must not silently detach.

    When public reorders whole blocks so the run's neighbors move past each other,
    the run's surviving neighbors no longer bracket a single slot; placing it by
    one neighbor detaches it from the others. That is an undetermined position and
    must be rejected, not silently emitted at the wrong spot. Regression: the run
    landed at end-of-file, detached from its true neighbors.
    """
    source = "h1\nh2\nnote = 0  # INT\nt1\nt2\n"
    public = "t1\nt2\nh1\nh2\n"  # blocks reordered

    out = _anchor_source_only_regions(
        source_text=source, public_text=public, transform=_INTERNAL_LINES
    )
    # The run's slot did not survive as a contiguous region -> reject.
    assert out is None


def test_anchor_places_trailing_run_after_rewritten_neighbor():
    """A run at EOF whose neighbor was rewritten appends after it, not reject.

    When a run is the last thing in the file and its only neighbor (the preceding
    line) was rewritten, tail placement is unambiguous: it goes after that
    rewritten line. Regression: this was falsely rejected because no kept line
    followed the run and the before-neighbor did not align.
    """
    source = "keep_top\nreal = 1\nnote = 0  # INT\n"
    public = "keep_top\nreal = 2\n"  # 'real' rewritten

    out = _anchor_source_only_regions(
        source_text=source, public_text=public, transform=_INTERNAL_LINES
    )
    assert out is not None
    assert strip_source_text(out, _INTERNAL_LINES) == public
    # The run lands after the rewritten 'real = 2' line (tail of its region).
    assert out.index("note") > out.index("real = 2")


def test_removed_regions_rejects_else_block_rewrite():
    """An ``else``-branch strip_block cannot be reversed by re-insertion.

    Export keeps the else branch and uncomments it, so the public tree holds a
    rewritten form absent from source. ``_removed_regions`` must refuse rather
    than fabricate a region that would corrupt the source on import.
    """
    transform = Transform(
        id="x",
        type="strip_block",
        path="m",
        start="# IF",
        end="# ENDIF",
        else_marker="# ELSE",
    )
    source = "a\n# IF\ninternal\n# ELSE\n# public_line\n# ENDIF\nb\n"
    with pytest.raises(ImportRequestError, match="cannot be reversed"):
        _removed_regions(source_text=source, transform=transform)


def test_removed_regions_allows_else_transform_on_file_without_block():
    """An else-transform whose glob matches a file with no block is a no-op.

    The else-branch rejection must key on the source file actually containing
    the block, not merely matching the transform's glob. A file like
    ``ratelimit.py`` that carries no ``# IF`` marker was never rewritten, so
    importing an unrelated edit to it must succeed with zero re-inserted regions
    rather than being blocked as non-reversible.
    """
    transform = Transform(
        id="x",
        type="strip_block",
        path="**/*.py",
        start="# IF",
        end="# ENDIF",
        else_marker="# ELSE",
    )
    source = "def limiter() -> None:\n    return None\n"
    assert _removed_regions(source_text=source, transform=transform) == []


def test_import_allows_strip_block_glob_match_without_block(tmp_path: Path):
    """A strip_block transform that finds no block in the source file is a
    no-op, so importing a public change to that file must succeed.

    Copybara treats a transform that changes nothing as a no-op rather than an
    error (see Replace.java: ``TransformationStatus.noop(... "was a no-op
    because it didn't ...")`` and the same in FilterReplace.java). Copybarista's
    importer previously rejected any path merely *matching* a strip_block glob,
    even when the file contained no block markers -- diverging from that
    behaviour. This guards the no-op case: the strip removed nothing, so the
    public content reverses unchanged.
    """
    paths = _fixture(tmp_path, include_strip_block_noop=True)
    public_head = _copy_tree(paths.public_base, tmp_path / "public-head")
    (public_head / "pkg/module.py").write_text(
        "from copybarista.public import api\nVALUE = 'edited'\n",
        encoding="utf-8",
    )

    import_change_request(
        ImportRequest(
            config=load_config(paths.config),
            public_base=paths.public_base,
            public_head=public_head,
            source_base=paths.source_base,
            destination=_copy_tree(paths.source_base, tmp_path / "destination"),
        )
    )


def test_import_rejects_public_base_mismatch(tmp_path: Path):
    paths = _fixture(tmp_path)
    public_base = _copy_tree(paths.public_base, tmp_path / "bad-public-base")
    (public_base / "README.md").write_text("stale\n", encoding="utf-8")

    with pytest.raises(ImportRequestError, match="public base"):
        import_change_request(
            ImportRequest(
                config=load_config(paths.config),
                public_base=public_base,
                public_head=paths.public_base,
                source_base=paths.source_base,
                destination=_copy_tree(paths.source_base, tmp_path / "destination"),
            )
        )


def test_tree_snapshot_diff_reports_create_modify_delete(tmp_path: Path):
    base = tmp_path / "base"
    head = tmp_path / "head"
    base.mkdir()
    head.mkdir()
    (base / "delete.txt").write_text("delete\n", encoding="utf-8")
    (base / "modify.txt").write_text("old\n", encoding="utf-8")
    (head / "modify.txt").write_text("new\n", encoding="utf-8")
    (head / "create.txt").write_text("create\n", encoding="utf-8")

    diff = TreeSnapshot.from_root(base).diff(TreeSnapshot.from_root(head))

    assert [(change.path, change.action) for change in diff.changes] == [
        ("create.txt", "created"),
        ("delete.txt", "deleted"),
        ("modify.txt", "modified"),
    ]


def test_path_mapper_rejects_excluded_path(tmp_path: Path):
    config = load_config(_fixture(tmp_path).config)
    mapper = PathMapper(config=config)

    assert mapper.source_path("pkg/module.py") == ("internal/demo/pkg/module.py")
    with pytest.raises(ImportRequestError, match="excluded or unmapped"):
        mapper.source_path("private.txt")


def test_cli_import_change_writes_json_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    paths = _fixture(tmp_path)
    public_head = _copy_tree(paths.public_base, tmp_path / "public-head")
    (public_head / "pkg/module.py").write_text(
        "from copybarista.public import api\nVALUE = 'cli'\n",
        encoding="utf-8",
    )
    destination = _copy_tree(paths.source_base, tmp_path / "destination")

    main(
        [
            "import-change",
            str(paths.config),
            "--public-base",
            str(paths.public_base),
            "--public-head",
            str(public_head),
            "--source-base",
            str(paths.source_base),
            "--destination",
            str(destination),
            "--json",
        ]
    )

    data = json.loads(capsys.readouterr().out)
    assert data["changes"][0]["public"] == "pkg/module.py"
    assert data["changes"][0]["source"] == "internal/demo/pkg/module.py"


def test_cli_import_change_mismatch_exits_three(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    paths = _fixture(tmp_path)
    bad_base = _copy_tree(paths.public_base, tmp_path / "bad-public-base")
    (bad_base / "README.md").write_text("stale\n", encoding="utf-8")

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "import-change",
                str(paths.config),
                "--public-base",
                str(bad_base),
                "--public-head",
                str(paths.public_base),
                "--source-base",
                str(paths.source_base),
                "--destination",
                str(_copy_tree(paths.source_base, tmp_path / "destination")),
            ]
        )

    assert exc.value.code == 3
    assert "public base" in capsys.readouterr().err


def test_cli_import_change_no_verify_warns(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    paths = _fixture(tmp_path)

    main(
        [
            "import-change",
            str(paths.config),
            "--public-base",
            str(paths.public_base),
            "--public-head",
            str(paths.public_base),
            "--source-base",
            str(paths.source_base),
            "--destination",
            str(_copy_tree(paths.source_base, tmp_path / "destination")),
            "--no-verify",
        ]
    )

    assert "--no-verify disables" in capsys.readouterr().err


def test_importer_type_exposes_plan_boundary(tmp_path: Path):
    paths = _fixture(tmp_path)
    importer = ChangeRequestImporter(
        config=load_config(paths.config),
        public_base=paths.public_base,
        public_head=paths.public_base,
        source_base=paths.source_base,
        destination=_copy_tree(paths.source_base, tmp_path / "destination"),
    )

    assert importer.plan().changes == ()


def test_merge_import_matches_strict_when_source_has_no_drift(tmp_path: Path):
    """With no source drift, merge import reproduces the strict result exactly."""
    paths = _fixture(tmp_path)
    public_head = _copy_tree(paths.public_base, tmp_path / "public-head")
    (public_head / "pkg/module.py").write_text(
        "from copybarista.public import api\nVALUE = 'head'\n",
        encoding="utf-8",
    )
    new_file = public_head / "pkg/new.py"
    new_file.write_text("VALUE = 'new'\n", encoding="utf-8")
    new_file.chmod(0o755)
    (public_head / "README.md").unlink()

    def run(*, merge_import: bool) -> Path:
        destination = _copy_tree(
            paths.source_base,
            tmp_path / ("merge" if merge_import else "strict"),
        )
        import_change_request(
            ImportRequest(
                config=load_config(paths.config),
                public_base=paths.public_base,
                public_head=public_head,
                source_base=paths.source_base,
                destination=destination,
                merge_import=merge_import,
            )
        )
        return destination

    strict = run(merge_import=False)
    merged = run(merge_import=True)

    assert TreeSnapshot.from_root(strict) == TreeSnapshot.from_root(merged)


def test_strict_import_rejects_source_ahead_of_public_base(tmp_path: Path):
    """Strict import fails when the source already carries the change."""
    paths = _fixture(tmp_path)
    source_file = paths.source_base / "internal/demo/pkg/module.py"
    source_file.write_text(
        "from internal.demo import api\nVALUE = 'head'\n",
        encoding="utf-8",
    )
    public_head = _copy_tree(paths.public_base, tmp_path / "public-head")
    (public_head / "pkg/module.py").write_text(
        "from copybarista.public import api\nVALUE = 'head'\n",
        encoding="utf-8",
    )

    with pytest.raises(ImportRequestError, match="does not reproduce public base"):
        import_change_request(
            ImportRequest(
                config=load_config(paths.config),
                public_base=paths.public_base,
                public_head=public_head,
                source_base=paths.source_base,
                destination=_copy_tree(paths.source_base, tmp_path / "destination"),
            )
        )


def test_merge_import_skips_change_already_applied_in_source(tmp_path: Path):
    """Merge import treats a source already at head as a no-op."""
    paths = _fixture(tmp_path)
    source_file = paths.source_base / "internal/demo/pkg/module.py"
    source_file.write_text(
        "from internal.demo import api\nVALUE = 'head'\n",
        encoding="utf-8",
    )
    public_head = _copy_tree(paths.public_base, tmp_path / "public-head")
    (public_head / "pkg/module.py").write_text(
        "from copybarista.public import api\nVALUE = 'head'\n",
        encoding="utf-8",
    )
    destination = _copy_tree(paths.source_base, tmp_path / "destination")

    result = import_change_request(
        ImportRequest(
            config=load_config(paths.config),
            public_base=paths.public_base,
            public_head=public_head,
            source_base=paths.source_base,
            destination=destination,
            merge_import=True,
        )
    )

    assert [change.outcome for change in result.changes] == ["skipped"]
    assert (destination / "internal/demo/pkg/module.py").read_text(
        encoding="utf-8"
    ) == "from internal.demo import api\nVALUE = 'head'\n"


def test_merge_import_three_way_merges_independent_drift(tmp_path: Path):
    """Merge import folds public head into independently drifted source."""
    paths = _fixture(tmp_path)
    source_file = paths.source_base / "internal/demo/pkg/module.py"
    source_file.write_text(
        "from internal.demo import api\nVALUE = 'base'\n\n\ndef helper():\n"
        "    pass\n\n\ndef local_only():\n    return 1\n",
        encoding="utf-8",
    )
    public_base = _copy_tree(paths.public_base, tmp_path / "public-base-merge")
    (public_base / "pkg/module.py").write_text(
        "from copybarista.public import api\nVALUE = 'base'\n\n\ndef helper():\n"
        "    pass\n",
        encoding="utf-8",
    )
    public_head = _copy_tree(public_base, tmp_path / "public-head")
    (public_head / "pkg/module.py").write_text(
        "from copybarista.public import api\nVALUE = 'head'\n\n\ndef helper():\n"
        "    pass\n",
        encoding="utf-8",
    )
    destination = _copy_tree(paths.source_base, tmp_path / "destination")

    result = import_change_request(
        ImportRequest(
            config=load_config(paths.config),
            public_base=public_base,
            public_head=public_head,
            source_base=paths.source_base,
            destination=destination,
            merge_import=True,
        )
    )

    assert [change.outcome for change in result.changes] == ["merged"]
    assert (destination / "internal/demo/pkg/module.py").read_text(
        encoding="utf-8"
    ) == (
        "from internal.demo import api\nVALUE = 'head'\n\n\ndef helper():\n    pass\n"
        "\n\ndef local_only():\n    return 1\n"
    )


def _numbered_module(*, first: str | None = None, last: str | None = None) -> str:
    """Return a 20-line module body; override the first/last data line.

    The wide gap between the overridable lines lets one edit near the top and
    another near the bottom three-way-merge cleanly (non-overlapping hunks).
    """
    lines = ["from internal.demo import api", *(f"L{i} = {i}" for i in range(20))]
    if first is not None:
        lines[1] = first
    if last is not None:
        lines[-1] = last
    return "\n".join(lines) + "\n"


def test_merge_import_replaces_symlink_target_without_writing_through(
    tmp_path: Path,
):
    """A drifted symlink at the destination is replaced, not written through.

    Strict import deletes a symlink target before writing; the merge path must
    hold the same contract, or a clean three-way merge writes through the link
    and mutates its referent instead of restoring the intended regular file.
    """
    paths = _fixture(tmp_path)
    public_base = _copy_tree(paths.public_base, tmp_path / "public-base-merge")
    (public_base / "pkg/module.py").write_text(
        _numbered_module().replace("from internal.demo", "from copybarista.public"),
        encoding="utf-8",
    )
    public_head = _copy_tree(public_base, tmp_path / "public-head")
    (public_head / "pkg/module.py").write_text(
        _numbered_module(first="L0 = 'head'").replace(
            "from internal.demo", "from copybarista.public"
        ),
        encoding="utf-8",
    )
    # Source drifts the bottom line; head drifts the top line -> clean merge.
    (paths.source_base / "internal/demo/pkg/module.py").write_text(
        _numbered_module(last="L19 = 'srcdrift'"), encoding="utf-8"
    )
    destination = _copy_tree(paths.source_base, tmp_path / "destination")
    # Destination drift: the imported module became an in-tree symlink.
    module = destination / "internal/demo/pkg/module.py"
    referent = destination / "internal/demo/pkg/other.py"
    referent.write_text("REFERENT UNTOUCHED\n", encoding="utf-8")
    module.unlink()
    module.symlink_to("other.py")

    result = import_change_request(
        ImportRequest(
            config=load_config(paths.config),
            public_base=public_base,
            public_head=public_head,
            source_base=paths.source_base,
            destination=destination,
            merge_import=True,
        )
    )

    assert [change.outcome for change in result.changes] == ["merged"]
    assert not module.is_symlink()
    assert module.is_file()
    body = module.read_text(encoding="utf-8")
    assert "L0 = 'head'" in body
    assert "L19 = 'srcdrift'" in body
    # The symlink's former referent must be untouched.
    assert referent.read_text(encoding="utf-8") == "REFERENT UNTOUCHED\n"


def test_merge_import_type_change_matches_strict(tmp_path: Path):
    """A symlink->file type change force-propagates head, matching strict.

    A type change is not text-mergeable: the merge path must route it to the
    same force-propagation strict uses, not diff3 the file bytes against an
    empty base (which manufactures a spurious conflict).
    """
    paths = _fixture(tmp_path)
    # Base and source have pkg/x as a symlink; head replaces it with a file.
    for tree in (paths.public_base, paths.source_base / "internal/demo"):
        (tree / "pkg/real.txt").write_text("real\n", encoding="utf-8")
        (tree / "pkg/x").symlink_to("real.txt")
    public_head = _copy_tree(paths.public_base, tmp_path / "public-head")
    (public_head / "pkg/x").unlink()
    (public_head / "pkg/x").write_text("now a regular file\n", encoding="utf-8")

    def run(*, merge_import: bool) -> Path:
        destination = _copy_tree(
            paths.source_base, tmp_path / ("merge" if merge_import else "strict")
        )
        import_change_request(
            ImportRequest(
                config=load_config(paths.config),
                public_base=paths.public_base,
                public_head=public_head,
                source_base=paths.source_base,
                destination=destination,
                merge_import=merge_import,
            )
        )
        return destination

    strict = run(merge_import=False)
    merged = run(merge_import=True)

    assert TreeSnapshot.from_root(strict) == TreeSnapshot.from_root(merged)
    x = merged / "internal/demo/pkg/x"
    assert not x.is_symlink()
    assert x.read_text(encoding="utf-8") == "now a regular file\n"


def test_merge_import_regex_groups_reverse_only_rewrites_module_tokens(
    tmp_path: Path,
):
    """A ``regex_groups`` reverse rewrites module tokens, never lookalikes.

    The reverse of ``widget`` -> ``acme.internal.widget`` must rewrite the real
    module references (``from widget``, ``widget.x``) yet leave intact every
    public ``widget`` that is not a module token: an identifier substring
    (``widget_state``), a dotfile (``.widget``), and prose (``a widget model``).
    The source drifts so the merge path runs a full reverse rather than the
    skipped fast path -- exercising the bug class that a plain ``str.replace``
    reverse silently corrupts.
    """
    paths = _regex_groups_fixture(tmp_path)
    public_head = _copy_tree(paths.public_base, tmp_path / "public-head")
    (public_head / "pkg/module.py").write_text(
        "from widget import api\n"
        "HEAD = 1\n"
        "x = widget.providers.load()\n"
        "self.widget_state = 'base'\n"
        'rules = root / ".widget" / "rules"\n'
        '"""Configure a widget model here."""\n',
        encoding="utf-8",
    )
    destination = _copy_tree(paths.source_base, tmp_path / "destination")

    result = import_change_request(
        ImportRequest(
            config=load_config(paths.config),
            public_base=paths.public_base,
            public_head=public_head,
            source_base=paths.source_base,
            destination=destination,
            merge_import=True,
        )
    )

    assert [change.outcome for change in result.changes] == ["merged"]
    assert (destination / "internal/demo/pkg/module.py").read_text(
        encoding="utf-8"
    ) == (
        "from acme.internal.widget import api\n"
        "HEAD = 1\n"
        "x = acme.internal.widget.providers.load()\n"
        "self.widget_state = 'base'\n"
        'rules = root / ".widget" / "rules"\n'
        '"""Configure a widget model here."""\n'
        "LOCAL = 9\n"
    )


def test_import_overlapping_namespace_transforms_do_not_double_prefix(
    tmp_path: Path,
):
    """Reversing a dotted + bare namespace pair never doubles the source prefix.

    Mirrors the wesearch config: a dotted ``loop.pkg.${s}`` <-> ``pkg.${s}`` rule
    plus a bare unanchored ``loop.pkg`` <-> ``pkg`` mop-up rule. Forward, the
    dotted rule runs first and the bare rule only catches leftovers. Reversing
    naively (each rule applied to the previous rule's output) makes the bare
    reverse rewrite ``pkg.x`` -> ``loop.pkg.x`` and the dotted reverse then match
    ``pkg.x`` inside it, producing ``loop.loop.pkg.x``. The reverse must instead
    rewrite each public token exactly once.

    Runs under ``merge_import`` -- the mode the sync automation uses
    (``scripts/sync_import_change.py`` passes ``--merge-import``), which skips
    the strict-mode injective guard and whole-tree re-export check. That is the
    exact path that shipped the doubled ``loop.loop.wesearch`` import PR.
    """
    source_base = tmp_path / "source-base"
    source_project = source_base / "internal/demo"
    (source_project / "pkg").mkdir(parents=True)
    (source_project / "pkg/module.py").write_text(
        "from loop.acme.errors import FetchError\n"
        "from loop.acme.fetch import fetch\n"
        'data_dir = home("loop.acme")\n',
        encoding="utf-8",
    )
    public_base = tmp_path / "public-base"
    (public_base / "pkg").mkdir(parents=True)
    (public_base / "pkg/module.py").write_text(
        "from acme.errors import FetchError\n"
        "from acme.fetch import fetch\n"
        'data_dir = home("acme")\n',
        encoding="utf-8",
    )
    public_head = _copy_tree(public_base, tmp_path / "public-head")
    (public_head / "pkg/module.py").write_text(
        "from acme.errors import FetchError\n"
        "from acme.fetch import fetch\n"
        "from acme.paper import PaperRecord\n"
        'data_dir = home("acme")\n',
        encoding="utf-8",
    )
    config = tmp_path / "copy.barista.toml"
    config.write_text(
        """
        [workflow]
        name = "demo"
        mode = "squash"
        source_root = "internal/demo"

        [files]
        include = ["**"]

        [[transform]]
        type = "replace"
        path = "pkg/*.py"
        before = "loop.acme.${s}"
        after = "acme.${s}"
        regex_groups = { s = "[A-Za-z_]" }
        required = false

        [[transform]]
        type = "replace"
        path = "pkg/*.py"
        before = "loop.acme"
        after = "acme"
        required = false
        """,
        encoding="utf-8",
    )
    destination = _copy_tree(source_base, tmp_path / "destination")

    import_change_request(
        ImportRequest(
            config=load_config(config),
            public_base=public_base,
            public_head=public_head,
            source_base=source_base,
            destination=destination,
            merge_import=True,
        )
    )

    assert (destination / "internal/demo/pkg/module.py").read_text(
        encoding="utf-8"
    ) == (
        "from loop.acme.errors import FetchError\n"
        "from loop.acme.fetch import fetch\n"
        "from loop.acme.paper import PaperRecord\n"
        'data_dir = home("loop.acme")\n'
    )


def _regex_groups_fixture(tmp_path: Path) -> _FixturePaths:
    """Build a fixture using Copybara-style ``regex_groups`` namespace rewrites."""
    source_base = tmp_path / "source-base"
    source_project = source_base / "internal/demo"
    (source_project / "pkg").mkdir(parents=True)
    (source_project / "pkg/module.py").write_text(
        "from acme.internal.widget import api\n"
        "x = acme.internal.widget.providers.load()\n"
        "self.widget_state = 'base'\n"
        'rules = root / ".widget" / "rules"\n'
        '"""Configure a widget model here."""\n'
        "LOCAL = 9\n",
        encoding="utf-8",
    )
    public_base = tmp_path / "public-base"
    (public_base / "pkg").mkdir(parents=True)
    (public_base / "pkg/module.py").write_text(
        "from widget import api\n"
        "x = widget.providers.load()\n"
        "self.widget_state = 'base'\n"
        'rules = root / ".widget" / "rules"\n'
        '"""Configure a widget model here."""\n',
        encoding="utf-8",
    )
    config = tmp_path / "copy.barista.toml"
    config.write_text(
        """
        [workflow]
        name = "demo"
        mode = "squash"
        source_root = "internal/demo"

        [files]
        include = ["**"]

        [[transform]]
        type = "replace"
        path = "pkg/*.py"
        before = "acme.internal.widget.${s}"
        after = "widget.${s}"
        regex_groups = { s = "[A-Za-z_]" }
        required = false

        [[transform]]
        type = "replace"
        path = "pkg/*.py"
        before = "from acme.internal.widget "
        after = "from widget "
        required = false
        """,
        encoding="utf-8",
    )
    return _FixturePaths(
        config=config, public_base=public_base, source_base=source_base
    )


def test_merge_import_reports_conflicting_drift(tmp_path: Path):
    """Merge import raises and lists files whose drift conflicts with head."""
    paths = _fixture(tmp_path)
    source_file = paths.source_base / "internal/demo/pkg/module.py"
    source_file.write_text(
        "from internal.demo import api\nVALUE = 'local'\n",
        encoding="utf-8",
    )
    public_head = _copy_tree(paths.public_base, tmp_path / "public-head")
    (public_head / "pkg/module.py").write_text(
        "from copybarista.public import api\nVALUE = 'head'\n",
        encoding="utf-8",
    )
    destination = _copy_tree(paths.source_base, tmp_path / "destination")
    original = (destination / "internal/demo/pkg/module.py").read_text(encoding="utf-8")

    with pytest.raises(ImportRequestError, match=r"pkg/module\.py") as excinfo:
        import_change_request(
            ImportRequest(
                config=load_config(paths.config),
                public_base=paths.public_base,
                public_head=public_head,
                source_base=paths.source_base,
                destination=destination,
                merge_import=True,
            )
        )

    assert "conflict" in str(excinfo.value).lower()
    assert (destination / "internal/demo/pkg/module.py").read_text(
        encoding="utf-8"
    ) == original


def test_merge_import_preserves_executable_bit_on_merged_file(tmp_path: Path):
    """A clean merge carries the public head's executable bit to the source."""
    paths = _fixture(tmp_path)
    source_file = paths.source_base / "internal/demo/pkg/module.py"
    source_file.write_text(
        "from internal.demo import api\nVALUE = 'base'\n\n\ndef helper():\n"
        "    pass\n\n\ndef local_only():\n    return 1\n",
        encoding="utf-8",
    )
    public_base = _copy_tree(paths.public_base, tmp_path / "public-base-merge")
    (public_base / "pkg/module.py").write_text(
        "from copybarista.public import api\nVALUE = 'base'\n\n\ndef helper():\n"
        "    pass\n",
        encoding="utf-8",
    )
    public_head = _copy_tree(public_base, tmp_path / "public-head")
    head_file = public_head / "pkg/module.py"
    head_file.write_text(
        "from copybarista.public import api\nVALUE = 'head'\n\n\ndef helper():\n"
        "    pass\n",
        encoding="utf-8",
    )
    head_file.chmod(0o755)
    destination = _copy_tree(paths.source_base, tmp_path / "destination")

    result = import_change_request(
        ImportRequest(
            config=load_config(paths.config),
            public_base=public_base,
            public_head=public_head,
            source_base=paths.source_base,
            destination=destination,
            merge_import=True,
        )
    )

    assert [change.outcome for change in result.changes] == ["merged"]
    imported = destination / "internal/demo/pkg/module.py"
    assert stat.S_IMODE(imported.stat().st_mode) & stat.S_IXUSR


def test_merge_import_rolls_back_earlier_merge_on_later_conflict(tmp_path: Path):
    """A conflict in one file rolls back a cleanly merged earlier file."""
    paths = _fixture(tmp_path)
    (paths.source_base / "internal/demo/pkg/clean.py").write_text(
        "from internal.demo import api\nVALUE = 'base'\n\n\ndef helper():\n"
        "    pass\n\n\ndef local_only():\n    return 1\n",
        encoding="utf-8",
    )
    (paths.source_base / "internal/demo/pkg/module.py").write_text(
        "from internal.demo import api\nVALUE = 'local'\n",
        encoding="utf-8",
    )
    public_base = _copy_tree(paths.public_base, tmp_path / "public-base-merge")
    (public_base / "pkg/clean.py").write_text(
        "from copybarista.public import api\nVALUE = 'base'\n\n\ndef helper():\n"
        "    pass\n",
        encoding="utf-8",
    )
    public_head = _copy_tree(public_base, tmp_path / "public-head")
    # clean.py merges cleanly (head edits a region the source did not touch).
    (public_head / "pkg/clean.py").write_text(
        "from copybarista.public import api\nVALUE = 'head'\n\n\ndef helper():\n"
        "    pass\n",
        encoding="utf-8",
    )
    # module.py conflicts (both sides edited the same line).
    (public_head / "pkg/module.py").write_text(
        "from copybarista.public import api\nVALUE = 'head'\n",
        encoding="utf-8",
    )
    destination = _copy_tree(paths.source_base, tmp_path / "destination")
    clean_before = (destination / "internal/demo/pkg/clean.py").read_text(
        encoding="utf-8"
    )

    with pytest.raises(ImportRequestError, match="conflict"):
        import_change_request(
            ImportRequest(
                config=load_config(paths.config),
                public_base=public_base,
                public_head=public_head,
                source_base=paths.source_base,
                destination=destination,
                merge_import=True,
            )
        )

    assert (destination / "internal/demo/pkg/clean.py").read_text(
        encoding="utf-8"
    ) == clean_before


def test_merge_import_does_not_write_or_reformat_conflict_markers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A conflicting merge never writes marker bytes or reformats them.

    Conflict-marker text is invalid source; the import rolls back regardless, so
    it must not reach the destination or the ruff reformat pass.
    """
    paths = _fixture(tmp_path)
    (paths.source_base / "internal/demo/pkg/module.py").write_text(
        "from internal.demo import api\nVALUE = 'local'\n", encoding="utf-8"
    )
    public_base = _copy_tree(paths.public_base, tmp_path / "public-base-merge")
    public_head = _copy_tree(public_base, tmp_path / "public-head")
    (public_head / "pkg/module.py").write_text(
        "from copybarista.public import api\nVALUE = 'head'\n", encoding="utf-8"
    )
    destination = _copy_tree(paths.source_base, tmp_path / "destination")
    module = destination / "internal/demo/pkg/module.py"
    module_before = module.read_text(encoding="utf-8")

    def fail_reformat(self: object, *, change: object, target: Path) -> None:
        del self, change, target
        raise AssertionError("reformat must not run on a conflicting merge")

    monkeypatch.setattr(
        ChangeRequestImporter, "_reformat_imported_source", fail_reformat
    )

    with pytest.raises(ImportRequestError, match="conflict"):
        import_change_request(
            ImportRequest(
                config=load_config(paths.config),
                public_base=public_base,
                public_head=public_head,
                source_base=paths.source_base,
                destination=destination,
                merge_import=True,
            )
        )

    body = module.read_text(encoding="utf-8")
    assert "<<<<<<<" not in body
    assert body == module_before


def test_merge_import_propagates_delete_despite_source_drift(tmp_path: Path):
    """A public-head deletion is force-propagated even when the source drifted."""
    paths = _fixture(tmp_path)
    (paths.source_base / "internal/demo/pkg/module.py").write_text(
        "from internal.demo import api\nVALUE = 'local'\n",
        encoding="utf-8",
    )
    public_head = _copy_tree(paths.public_base, tmp_path / "public-head")
    (public_head / "pkg/module.py").unlink()
    destination = _copy_tree(paths.source_base, tmp_path / "destination")

    result = import_change_request(
        ImportRequest(
            config=load_config(paths.config),
            public_base=paths.public_base,
            public_head=public_head,
            source_base=paths.source_base,
            destination=destination,
            merge_import=True,
        )
    )

    assert [change.action for change in result.changes] == ["deleted"]
    assert not (destination / "internal/demo/pkg/module.py").exists()


def test_merge_import_raises_on_binary_conflict(tmp_path: Path):
    """A drifted binary file that cannot be diff3-merged raises, not corrupts."""
    paths = _fixture(tmp_path, with_transform=False)
    (paths.source_base / "internal/demo/pkg/module.py").write_bytes(
        b"\x00\x01LOCAL\x02\x03\n"
    )
    public_base = _copy_tree(paths.public_base, tmp_path / "public-base-bin")
    (public_base / "pkg/module.py").write_bytes(b"\x00\x01BASE\x02\x03\n")
    public_head = _copy_tree(public_base, tmp_path / "public-head")
    (public_head / "pkg/module.py").write_bytes(b"\x00\x01HEAD\x02\x03\n")
    destination = _copy_tree(paths.source_base, tmp_path / "destination")
    original = (destination / "internal/demo/pkg/module.py").read_bytes()

    with pytest.raises(ImportRequestError):
        import_change_request(
            ImportRequest(
                config=load_config(paths.config),
                public_base=public_base,
                public_head=public_head,
                source_base=paths.source_base,
                destination=destination,
                merge_import=True,
            )
        )

    assert (destination / "internal/demo/pkg/module.py").read_bytes() == original


@pytest.mark.parametrize(
    ("current", "base", "incoming"),
    [
        # Clean merge: each side edits a different region.
        (b"a\nLOCAL\nc\nx\ny\n", b"a\nb\nc\nx\ny\n", b"a\nb\nc\nP\ny\n"),
        # Conflict: both sides edit the same line differently.
        (b"a\nLOCAL\nc\n", b"a\nb\nc\n", b"a\nPUBLIC\nc\n"),
        # No-op incoming: incoming equals base, source drifted.
        (b"a\nLOCAL\nc\n", b"a\nb\nc\n", b"a\nb\nc\n"),
        # Conflict with surrounding context on both sides.
        (b"x\ny\nLOCAL\nz\nw\n", b"x\ny\nb\nz\nw\n", b"x\ny\nPUB\nz\nw\n"),
    ],
)
def test_three_way_merge_byte_matches_diff3(
    current: bytes, base: bytes, incoming: bytes, tmp_path: Path
) -> None:
    """``_three_way_merge`` reproduces ``diff3 -m`` byte-for-byte.

    Copybara merges with ``diff3 -m origin baseline destination``
    (``CommandLineDiffUtil``); this pins our ``git merge-file`` invocation to
    the identical engine, orientation, labels, and conflict markers. Inputs are
    newline-terminated -- the domain of exported source files (ruff enforces a
    final newline); diff3 and git merge-file differ only on malformed
    missing-EOL conflict hunks, which exported source never produces.
    """
    diff3 = shutil.which("diff3")
    if diff3 is None:
        pytest.skip("diff3 is unavailable")
    assert diff3 is not None
    incoming_path = tmp_path / "incoming"
    base_path = tmp_path / "base"
    current_path = tmp_path / "current"
    incoming_path.write_bytes(incoming)
    base_path.write_bytes(base)
    current_path.write_bytes(current)
    expected = subprocess.run(  # noqa: S603 -- fixed argv from shutil.which, no shell.
        [
            diff3,
            "-m",
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

    merged, conflicted = _three_way_merge(current=current, base=base, incoming=incoming)

    assert merged == expected.stdout
    assert conflicted == (expected.returncode == 1)


class _FixturePaths:
    def __init__(
        self,
        *,
        config: Path,
        public_base: Path,
        source_base: Path,
    ) -> None:
        self.config = config
        self.public_base = public_base
        self.source_base = source_base


def _fixture(
    tmp_path: Path,
    *,
    source_root: str = "internal/demo",
    destination_prefix: str = "",
    include_strip_block: bool = False,
    include_strip_block_noop: bool = False,
    with_transform: bool = True,
) -> _FixturePaths:
    source_base = tmp_path / "source-base"
    source_project = source_base / source_root if source_root else source_base
    source_project.mkdir(parents=True)
    (source_project / "pkg").mkdir()
    (source_project / "pkg/module.py").write_text(
        "from internal.demo import api\nVALUE = 'base'\n",
        encoding="utf-8",
    )
    readme = (
        "public readme\n<!-- internal:start -->\nprivate\n<!-- internal:end -->\n"
        if include_strip_block
        else "public readme\n"
    )
    (source_project / "README.md").write_text(readme, encoding="utf-8")

    public_base = tmp_path / "public-base"
    public_project = (
        public_base / destination_prefix if destination_prefix else public_base
    )
    public_project.mkdir(parents=True)
    (public_project / "pkg").mkdir(parents=True)
    (public_project / "pkg/module.py").write_text(
        "from copybarista.public import api\nVALUE = 'base'\n",
        encoding="utf-8",
    )
    (public_base / "README.md").write_text("public readme\n", encoding="utf-8")

    config = tmp_path / "copy.barista.toml"
    transform_path = f"{destination_prefix + '/' if destination_prefix else ''}pkg/*.py"
    replace_transform = (
        f"""
        [[transform]]
        type = "replace"
        path = "{transform_path}"
        before = "from internal.demo"
        after = "from copybarista.public"
        """
        if with_transform
        else ""
    )
    strip_block = (
        """
        [[transform]]
        type = "strip_block"
        path = "README.md"
        start = "<!-- internal:start -->"
        end = "<!-- internal:end -->"
        """
        if include_strip_block
        else ""
    )
    # A strip_block whose glob matches the .py module, which contains no block
    # markers: the transform is a no-op on it.
    if include_strip_block_noop:
        strip_block += f"""
        [[transform]]
        type = "strip_block"
        path = "{destination_prefix + "/" if destination_prefix else ""}pkg/*.py"
        start = "# copybarista:internal:start"
        end = "# copybarista:internal:end"
        required = false
        """
    moves = (
        f"""
        [[files.moves]]
        path = ""
        destination = "{destination_prefix}"

        [[files.moves]]
        path = "{destination_prefix}/README.md"
        destination = "README.md"
        """
        if destination_prefix
        else ""
    )
    config.write_text(
        f"""
        [workflow]
        name = "demo"
        mode = "squash"
        source_root = "{source_root}"

        [files]
        include = ["**"]
        exclude = ["private.txt"]
        {moves}
        {replace_transform}
        {strip_block}
        """,
        encoding="utf-8",
    )
    return _FixturePaths(
        config=config,
        public_base=public_base,
        source_base=source_base,
    )


def test_merge_import_strip_block_reexports_to_public_head(tmp_path: Path):
    """Merge import of a strip_block edit reproduces the public head.

    Exercises the full merge path -- export source, three-way merge, reverse by
    re-insertion, write, and the re-export gate -- for a public edit to a file
    carrying a source-only block. The imported source must both keep the block
    and, when re-exported, byte-match the public head (asserted by the gate,
    which now runs in merge mode too). A wrong re-insertion offset would make the
    gate fail.
    """
    paths = _fixture(tmp_path, include_strip_block=True)
    public_head = _copy_tree(paths.public_base, tmp_path / "public-head")
    (public_head / "README.md").write_text("public edit\n", encoding="utf-8")
    destination = _copy_tree(paths.source_base, tmp_path / "destination")

    import_change_request(
        ImportRequest(
            config=load_config(paths.config),
            public_base=paths.public_base,
            public_head=public_head,
            source_base=paths.source_base,
            destination=destination,
            merge_import=True,
        )
    )

    imported = (destination / "internal/demo/README.md").read_text(encoding="utf-8")
    assert imported == (
        "public edit\n<!-- internal:start -->\nprivate\n<!-- internal:end -->\n"
    )


def test_reinsert_gate_rejects_public_edit_that_disturbs_stripped_region(
    tmp_path: Path,
):
    """A public edit that disturbs a stripped region fails the re-insert gate.

    When a public edit rewrites the context around a source-only block (here it
    introduces a stray start marker), re-inserting the source region can no
    longer strip back to the incoming public text. The per-file re-insert gate
    must reject this rather than write a source tree whose export has drifted.
    Exercises the real reversal path (no monkeypatch) in merge mode, which has no
    whole-tree public-head check of its own.
    """
    paths = _fixture(tmp_path, include_strip_block=True)
    public_head = _copy_tree(paths.public_base, tmp_path / "public-head")
    # The public author writes a line that itself contains the start marker,
    # with no matching end marker: re-inserting the source block then re-stripping
    # cannot reproduce this public text.
    (public_head / "README.md").write_text(
        "public edit <!-- internal:start --> oops\n", encoding="utf-8"
    )
    destination = _copy_tree(paths.source_base, tmp_path / "destination")
    original = (destination / "internal/demo/README.md").read_text(encoding="utf-8")

    with pytest.raises(ImportRequestError, match=r"strip marker|stripped region"):
        import_change_request(
            ImportRequest(
                config=load_config(paths.config),
                public_base=paths.public_base,
                public_head=public_head,
                source_base=paths.source_base,
                destination=destination,
                merge_import=True,
            )
        )

    # On failure the destination must be rolled back, never left drifted.
    assert (destination / "internal/demo/README.md").read_text(
        encoding="utf-8"
    ) == original


def _copy_tree(source: Path, destination: Path) -> Path:
    shutil.copytree(source, destination, symlinks=True)
    return destination


def _unmapped_fixture(tmp_path: Path) -> _FixturePaths:
    """Build a fixture with a path that maps to no config rule.

    The package ships under a whole-tree ``[[files.moves]]`` to ``pub``; an
    out-of-prefix repo-root path (``typings/brotli/...``) exists in the public
    tree but has no ``[[files.copy]]`` and matches no move -- i.e. it is
    UNMAPPED, the ``typings/brotli`` class left behind when an export mapping is
    dropped from config. Mirrors Copybara, where such a path matches no
    ``core.move`` rule and keeps its identical path on both sides.
    """
    source_base = tmp_path / "source-base"
    (source_base / "internal/demo/pkg").mkdir(parents=True)
    (source_base / "internal/demo/pkg/module.py").write_text(
        "VALUE = 'base'\n", encoding="utf-8"
    )
    public_base = tmp_path / "public-base"
    (public_base / "pub/pkg").mkdir(parents=True)
    (public_base / "pub/pkg/module.py").write_text("VALUE = 'base'\n", encoding="utf-8")
    (public_base / "typings/brotli").mkdir(parents=True)
    (public_base / "typings/brotli/__init__.pyi").write_text(
        "MODE_GENERIC: int\n", encoding="utf-8"
    )
    config = tmp_path / "copy.barista.toml"
    config.write_text(
        """
        [workflow]
        name = "demo"
        mode = "squash"
        source_root = "internal/demo"

        [files]
        include = ["**"]

        [[files.moves]]
        path = ""
        destination = "pub"
        """,
        encoding="utf-8",
    )
    return _FixturePaths(
        config=config,
        public_base=public_base,
        source_base=source_base,
    )


def test_source_path_maps_prefixed_path_and_leaves_unmapped_as_identity(
    tmp_path: Path,
):
    """An unmapped public path resolves to its identical path, never raising.

    Mirrors Copybara's ``CopyOrMove``: a path matching no relocation is untouched
    (its source path equals its public path). A prefixed path still maps back
    through the prefix. Regression: ``PathMapper.source_path`` raised
    ``excluded or unmapped`` on the out-of-prefix path, wedging import.
    """
    paths = _unmapped_fixture(tmp_path)
    mapper = PathMapper(config=load_config(paths.config))

    assert mapper.source_path("pub/pkg/module.py") == "internal/demo/pkg/module.py"
    assert (
        mapper.source_path("typings/brotli/__init__.pyi")
        == "typings/brotli/__init__.pyi"
    )


def test_merge_import_propagates_delete_of_unmapped_path(tmp_path: Path):
    """Deleting an unmapped public path imports as a no-op, not an error.

    The ``typings/brotli`` class: the path is gone from source already, so its
    public deletion has nothing to remove on the source side. Copybara's reverse
    handles this cleanly (unmatched ``core.move``); our importer must too.
    """
    paths = _unmapped_fixture(tmp_path)
    public_head = _copy_tree(paths.public_base, tmp_path / "public-head")
    (public_head / "typings/brotli/__init__.pyi").unlink()
    destination = _copy_tree(paths.source_base, tmp_path / "destination")

    result = import_change_request(
        ImportRequest(
            config=load_config(paths.config),
            public_base=paths.public_base,
            public_head=public_head,
            source_base=paths.source_base,
            destination=destination,
            merge_import=True,
        )
    )

    # The deletion targets a path absent from source: a no-op that must not raise
    # and must leave the source tree untouched.
    assert [change.action for change in result.changes] == ["deleted"]
    assert (destination / "internal/demo/pkg/module.py").read_text(
        encoding="utf-8"
    ) == "VALUE = 'base'\n"


def test_source_path_rejects_excluded_path_outside_prefix(tmp_path: Path):
    """An `exclude`d path outside `destination_prefix` is rejected, not identity.

    A path matching an `exclude` glob is deliberately kept out of the export, so
    it can never appear in a faithful public tree. If the public repo adds such a
    path, importing it must raise rather than silently write it at its identity
    source path. Regression: with a whole-tree move set, an out-of-prefix
    path that no move relocates returned its
    identity path WITHOUT consulting `self.matcher`, so an `exclude` glob matching
    an out-of-prefix path was dropped. The genuinely-unmapped case (no matching
    exclude, e.g. `typings/brotli`) must still resolve to identity -- only a path
    the config explicitly excludes is rejected.
    """
    config = tmp_path / "copy.barista.toml"
    config.write_text(
        """
        [workflow]
        name = "demo"
        mode = "squash"
        source_root = "internal/demo"

        [files]
        include = ["**"]
        exclude = ["typings/secret/**"]

        [[files.moves]]
        path = ""
        destination = "pub"
        """,
        encoding="utf-8",
    )

    mapper = PathMapper(config=load_config(config))

    # Explicitly excluded out-of-prefix path added/modified: rejected.
    with pytest.raises(ImportRequestError, match="unmapped"):
        mapper.source_path("typings/secret/key.pyi")
    # DELETING an excluded path is a source-side no-op, not a rejection: it
    # resolves to identity so the whole import does not wedge (brotli class).
    assert mapper.source_path("typings/secret/key.pyi", action="deleted") == (
        "typings/secret/key.pyi"
    )
    # Unmapped-but-not-excluded out-of-prefix path: identity (brotli class).
    assert mapper.source_path("typings/brotli/__init__.pyi") == (
        "typings/brotli/__init__.pyi"
    )


def test_merge_import_propagates_delete_of_excluded_path(tmp_path: Path):
    """Deleting an EXCLUDED public path imports as a no-op, not a hard error.

    Regression: rejecting an excluded add/modify (so a faithful export's invariant
    holds) must NOT extend to deletions. ``plan()`` maps every change -- including
    deletes -- through ``source_path`` before the ``deleted`` action is handled, so
    a delete of an excluded path (e.g. a stale ``htmlcov/`` artifact removed
    upstream) would raise and wedge the entire import, the exact failure class the
    ``typings/brotli`` fix removed. The delete must resolve to identity and no-op
    on the absent source path.
    """
    config = tmp_path / "copy.barista.toml"
    config.write_text(
        """
        [workflow]
        name = "demo"
        mode = "squash"
        source_root = "internal/demo"

        [files]
        include = ["**"]
        exclude = ["htmlcov/**"]

        [[files.moves]]
        path = ""
        destination = "pub"
        """,
        encoding="utf-8",
    )
    public_base = tmp_path / "public-base"
    (public_base / "pub/pkg").mkdir(parents=True)
    (public_base / "pub/pkg/module.py").write_text("VALUE = 'base'\n", encoding="utf-8")
    (public_base / "htmlcov").mkdir()
    (public_base / "htmlcov/index.html").write_text("<html>\n", encoding="utf-8")
    public_head = _copy_tree(public_base, tmp_path / "public-head")
    (public_head / "htmlcov/index.html").unlink()
    source_base = tmp_path / "source-base"
    (source_base / "internal/demo/pkg").mkdir(parents=True)
    (source_base / "internal/demo/pkg/module.py").write_text(
        "VALUE = 'base'\n", encoding="utf-8"
    )
    destination = _copy_tree(source_base, tmp_path / "destination")

    result = import_change_request(
        ImportRequest(
            config=load_config(config),
            public_base=public_base,
            public_head=public_head,
            source_base=source_base,
            destination=destination,
            merge_import=True,
        )
    )

    assert [change.action for change in result.changes] == ["deleted"]
    # The source tree is untouched: the excluded deletion targets an absent path.
    assert (destination / "internal/demo/pkg/module.py").read_text(
        encoding="utf-8"
    ) == "VALUE = 'base'\n"


def test_merge_import_applies_modify_of_unmapped_path_at_identity(tmp_path: Path):
    """Modifying an unmapped public path writes it at its identical source path.

    Copybara keeps an unmatched path at its identical location on both sides, so a
    public edit to ``typings/brotli`` lands at ``typings/brotli`` in source.
    """
    paths = _unmapped_fixture(tmp_path)
    # Source carries the unmapped path too (it is shipped, just unmapped by config).
    (paths.source_base / "typings/brotli").mkdir(parents=True)
    (paths.source_base / "typings/brotli/__init__.pyi").write_text(
        "MODE_GENERIC: int\n", encoding="utf-8"
    )
    public_head = _copy_tree(paths.public_base, tmp_path / "public-head")
    (public_head / "typings/brotli/__init__.pyi").write_text(
        "MODE_GENERIC: int\nMODE_TEXT: int\n", encoding="utf-8"
    )
    destination = _copy_tree(paths.source_base, tmp_path / "destination")

    import_change_request(
        ImportRequest(
            config=load_config(paths.config),
            public_base=paths.public_base,
            public_head=public_head,
            source_base=paths.source_base,
            destination=destination,
            merge_import=True,
        )
    )

    assert (destination / "typings/brotli/__init__.pyi").read_text(
        encoding="utf-8"
    ) == "MODE_GENERIC: int\nMODE_TEXT: int\n"
