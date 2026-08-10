"""Tests for supported `copy.bara.sky` config parsing."""

from __future__ import annotations

from pathlib import Path

import pytest

from copybarista.cli import main
from copybarista.config import (
    FileCopy,
    Transform,
    _parse_transform,
    load_config,
    parse_config,
)
from copybarista.copy_bara_sky import (
    TranslatedWorkflow,
    _transform_to_raw,
    translate_copy_bara_sky_to_toml,
)
from copybarista.errors import ConfigError
from copybarista.export import export_folder
from copybarista.transforms import apply_transform


def _write_sky(tmp_path: Path, source: str) -> Path:
    config_path = tmp_path / "copy.bara.sky"
    config_path.write_text(source, encoding="utf-8")
    return config_path


def test_loads_direct_sky_workflow(tmp_path: Path):
    config_path = _write_sky(
        tmp_path,
        """
        core.workflow(
            name = "export",
            origin = folder.origin(),
            destination = folder.destination(),
            origin_files = glob(["**"], exclude = ["dist/**"]),
            destination_files = glob(["**"]),
            authoring = authoring.pass_thru("Demo Export <demo@copybarista.test>"),
            mode = "SQUASH",
            transformations = [
                core.replace(
                    before = "from private import",
                    after = "from public import",
                    paths = glob(["module_test.py"]),
                ),
            ],
        )
        """,
    )

    config = load_config(config_path)

    assert config.name == "export"
    assert config.source_root == ""
    assert config.files.include == ("**",)
    assert config.files.exclude == ("dist/**",)
    assert config.transforms[0].type == "replace"
    assert config.transforms[0].path == "module_test.py"


def test_maps_sky_authoring_to_git_committer(tmp_path: Path):
    config_path = _write_sky(
        tmp_path,
        """
        core.workflow(
            name = "export",
            origin = folder.origin(),
            destination = git.destination(
                url = "file:///tmp/example.git",
                fetch = "main",
                push = "main",
            ),
            origin_files = glob(["**"]),
            destination_files = glob(["**"]),
            authoring = authoring.pass_thru("Demo User <user@copybarista.test>"),
            mode = "SQUASH",
            transformations = [],
        )
        """,
    )

    config = load_config(config_path)

    assert config.git.committer_name == "Demo User"
    assert config.git.committer_email == "user@copybarista.test"


def test_accepts_pass_thru_default_authoring(tmp_path: Path):
    config_path = _write_sky(
        tmp_path,
        """
        core.workflow(
            name = "export",
            origin = folder.origin(),
            destination = git.destination(
                url = "file:///tmp/example.git",
                push = "main",
            ),
            origin_files = glob(["**"]),
            authoring = authoring.pass_thru(
                default = "Demo User <user@copybarista.test>",
            ),
            mode = "SQUASH",
            transformations = [],
        )
        """,
    )

    config = load_config(config_path)

    assert config.git.committer_name == "Demo User"
    assert config.git.committer_email == "user@copybarista.test"


def test_accepts_positional_git_destination_url(tmp_path: Path):
    config_path = _write_sky(
        tmp_path,
        """
        core.workflow(
            name = "export",
            origin = folder.origin(),
            destination = git.destination("file:///tmp/example.git", push = "main"),
            origin_files = glob(["**"]),
            authoring = authoring.pass_thru("Demo User <user@copybarista.test>"),
            mode = "SQUASH",
            transformations = [],
        )
        """,
    )

    config = load_config(config_path)

    assert config.git.url == "file:///tmp/example.git"
    assert config.git.branch == "main"


def test_defaults_sky_git_destination_branch_to_main(tmp_path: Path):
    config_path = _write_sky(
        tmp_path,
        """
        core.workflow(
            name = "export",
            origin = folder.origin(),
            destination = git.destination("file:///tmp/example.git"),
            origin_files = glob(["**"]),
            authoring = authoring.pass_thru("Demo User <user@copybarista.test>"),
            mode = "SQUASH",
            transformations = [],
        )
        """,
    )

    config = load_config(config_path)

    assert config.git.branch == "main"


def test_accepts_absent_sky_destination_files(tmp_path: Path):
    config_path = _write_sky(
        tmp_path,
        """
        core.workflow(
            name = "export",
            origin = folder.origin(),
            destination = folder.destination(),
            origin_files = glob(["**"]),
            authoring = authoring.pass_thru(
                default = "Demo Export <demo@copybarista.test>",
            ),
            mode = "SQUASH",
            transformations = [],
        )
        """,
    )

    config = load_config(config_path)

    assert config.files.include == ("**",)


def test_accepts_absent_sky_origin_files(tmp_path: Path):
    config_path = _write_sky(
        tmp_path,
        """
        core.workflow(
            name = "export",
            origin = folder.origin(),
            destination = folder.destination(),
            authoring = authoring.pass_thru("Demo Export <demo@copybarista.test>"),
            mode = "SQUASH",
            transformations = [],
        )
        """,
    )

    config = load_config(config_path)

    assert config.files.include == ("**",)


def test_rejects_missing_sky_authoring(tmp_path: Path):
    config_path = _write_sky(
        tmp_path,
        """
        core.workflow(
            name = "export",
            origin = folder.origin(),
            destination = folder.destination(),
            origin_files = glob(["**"]),
            mode = "SQUASH",
            transformations = [],
        )
        """,
    )

    with pytest.raises(ConfigError, match="authoring"):
        load_config(config_path)


def test_accepts_sky_replace_paths_list_and_multi_glob(tmp_path: Path):
    config_path = _write_sky(
        tmp_path,
        """
        core.workflow(
            name = "export",
            origin = folder.origin(),
            destination = folder.destination(),
            origin_files = glob(["**"]),
            authoring = authoring.pass_thru("Demo Export <demo@copybarista.test>"),
            mode = "SQUASH",
            transformations = [
                core.replace(
                    before = "old",
                    after = "new",
                    paths = ["a.py", "b.py"],
                ),
                core.replace(
                    before = "private",
                    after = "public",
                    paths = glob(["c.py", "d.py"]),
                ),
            ],
        )
        """,
    )

    config = load_config(config_path)

    assert [transform.path for transform in config.transforms] == [
        "a.py",
        "b.py",
        "c.py",
        "d.py",
    ]


def test_accepts_core_transform_wrapper(tmp_path: Path):
    config_path = _write_sky(
        tmp_path,
        """
        core.workflow(
            name = "export",
            origin = folder.origin(),
            destination = folder.destination(),
            origin_files = glob(["**"]),
            authoring = authoring.pass_thru("Demo Export <demo@copybarista.test>"),
            mode = "SQUASH",
            transformations = core.transform([
                core.replace(
                    before = "old",
                    after = "new",
                    paths = glob(["a.py"]),
                ),
            ]),
        )
        """,
    )

    config = load_config(config_path)

    assert config.transforms[0].path == "a.py"


def test_accepts_core_transform_explicit_replace_reversal(tmp_path: Path):
    config_path = _write_sky(
        tmp_path,
        """
        core.workflow(
            name = "export",
            origin = folder.origin(),
            destination = folder.destination(),
            origin_files = glob(["**"]),
            authoring = authoring.pass_thru("Demo Export <demo@copybarista.test>"),
            mode = "SQUASH",
            transformations = [
                core.transform(
                    transformations = [
                        core.replace(
                            before = "internal",
                            after = "public",
                            paths = glob(["a.py"]),
                        ),
                    ],
                    reversal = [
                        core.replace(
                            before = "public import",
                            after = "internal import",
                            paths = glob(["a.py"]),
                        ),
                    ],
                ),
            ],
        )
        """,
    )

    config = load_config(config_path)

    assert config.transforms[0].before == "internal"
    assert config.transforms[0].after == "public"
    assert config.transforms[0].reverse_before == "public import"
    assert config.transforms[0].reverse_after == "internal import"


def test_accepts_core_reverse_for_literal_replace(tmp_path: Path):
    config_path = _write_sky(
        tmp_path,
        """
        core.workflow(
            name = "export",
            origin = folder.origin(),
            destination = folder.destination(),
            origin_files = glob(["**"]),
            authoring = authoring.pass_thru("Demo Export <demo@copybarista.test>"),
            mode = "SQUASH",
            transformations = core.reverse([
                core.replace(
                    before = "old",
                    after = "new",
                    paths = glob(["a.py"]),
                ),
            ]),
        )
        """,
    )

    config = load_config(config_path)

    assert config.transforms[0].before == "new"
    assert config.transforms[0].after == "old"


def test_rejects_core_reverse_move(tmp_path: Path):
    config_path = _write_sky(
        tmp_path,
        """
        core.workflow(
            name = "export",
            origin = folder.origin(),
            destination = folder.destination(),
            origin_files = glob(["**"]),
            authoring = authoring.pass_thru("Demo Export <demo@copybarista.test>"),
            mode = "SQUASH",
            transformations = core.reverse([
                core.move("project", ""),
            ]),
        )
        """,
    )

    with pytest.raises(ConfigError, match=r"core\.reverse"):
        load_config(config_path)


def test_move_to_prefix_becomes_whole_tree_move(tmp_path: Path):
    """`core.move(ROOT, PREFIX)` maps to source_root + a whole-tree move.

    A package that ships UNDER a public subdirectory (a monorepo path moved to a
    ``pkg/`` prefix) is expressed in Copybara as a move of the source root to a
    non-empty destination prefix. The translator must recognize this as the
    source-root move and emit a whole-tree ``[[files.moves]]`` entry
    (``path = ""``), not a per-file move transform, so the .sky mirrors a .toml
    that uses ``moves``.
    """
    config_path = _write_sky(
        tmp_path,
        """
        ROOT = "project"
        core.workflow(
            name = "export",
            origin = folder.origin(),
            destination = folder.destination(),
            origin_files = glob([ROOT + "/**"]),
            authoring = authoring.pass_thru("Demo Export <demo@copybarista.test>"),
            mode = "SQUASH",
            transformations = [core.move(ROOT, "pkgpub")],
        )
        """,
    )

    config = load_config(config_path)

    assert config.source_root == "project"
    assert [(move.path, move.destination) for move in config.files.moves] == [
        ("", "pkgpub"),
    ]


def test_move_out_of_prefix_becomes_back_move(tmp_path: Path):
    """A move back out of the prefix maps to an ordered back-move.

    When the package nests under a prefix but repo metadata (README, .github,
    ...) must stay at the public root, Copybara moves the whole root to the
    prefix, then moves those specific paths back to root. The translator
    preserves each such back-move verbatim and in order as a ``[[files.moves]]``
    entry so the .sky mirrors a .toml that keeps metadata at root 1:1.
    """
    config_path = _write_sky(
        tmp_path,
        """
        ROOT = "project"
        core.workflow(
            name = "export",
            origin = folder.origin(),
            destination = folder.destination(),
            origin_files = glob([ROOT + "/**"]),
            authoring = authoring.pass_thru("Demo Export <demo@copybarista.test>"),
            mode = "SQUASH",
            transformations = [
                core.move(ROOT, "pkgpub"),
                core.move("pkgpub/README.md", "README.md"),
            ],
        )
        """,
    )

    config = load_config(config_path)

    assert config.source_root == "project"
    assert [(move.path, move.destination) for move in config.files.moves] == [
        ("", "pkgpub"),
        ("pkgpub/README.md", "README.md"),
    ]


def test_move_subtree_to_root_becomes_copy_to_root(tmp_path: Path):
    """A move of an in-package subtree to the root maps to a copy to '.'.

    A verbatim-ship staging dir (e.g. ``<root>/.export`` -> the public root)
    lives inside the source root but must land at the export root. Copybara
    expresses this as ``core.move("<root>/.export", "")``. The translator
    recovers it as a ``[[files.copy]]`` to ``.`` so the .sky mirrors a .toml that
    ships ``.export`` verbatim.
    """
    config_path = _write_sky(
        tmp_path,
        """
        ROOT = "project"
        core.workflow(
            name = "export",
            origin = folder.origin(),
            destination = folder.destination(),
            origin_files = glob([ROOT + "/**"]),
            authoring = authoring.pass_thru("Demo Export <demo@copybarista.test>"),
            mode = "SQUASH",
            transformations = [
                core.move(ROOT, ""),
                core.move(ROOT + "/.export", ""),
            ],
        )
        """,
    )

    config = load_config(config_path)

    assert (
        "project/.export",
        ".",
    ) in [(copy.source, copy.destination) for copy in config.files.copy]


def test_core_copy_with_paths_becomes_file_copy_with_include(tmp_path: Path):
    """``core.copy(SRC, DEST, paths=glob([...]))`` maps to a copy with include.

    Sidecar test modules ship to a separate public dir (``*_test.py`` under the
    package root -> ``tests/``). Copybara expresses this as a ``core.copy`` of the
    package root to ``tests`` filtered by ``paths``; the translator recovers it as
    a ``[[files.copy]]`` with an ``include`` glob so the .sky mirrors the .toml.
    """
    config_path = _write_sky(
        tmp_path,
        """
        ROOT = "project"
        core.workflow(
            name = "export",
            origin = folder.origin(),
            destination = folder.destination(),
            origin_files = glob([ROOT + "/**"]),
            authoring = authoring.pass_thru("Demo Export <demo@copybarista.test>"),
            mode = "SQUASH",
            transformations = [
                core.move(ROOT, ""),
                core.copy(ROOT, "tests", paths = glob(["*_test.py"])),
            ],
        )
        """,
    )

    config = load_config(config_path)

    matching = [
        copy
        for copy in config.files.copy
        if copy.source == "project" and copy.destination == "tests"
    ]
    assert matching, "expected a files.copy of project -> tests"
    assert matching[0].include == ("*_test.py",)


def test_relocate_glob_move_matches_copybara_root_only_depth(tmp_path: Path):
    """A glob-scoped `core.move` relocates only root-level matches, like Copybara.

    ``core.move(ROOT, "tests", paths=glob(["*_test.py"]))`` RELOCATES the matched
    files. Copybara's ``paths`` glob uses single-segment ``*`` semantics, so a
    bare ``*_test.py`` matches only root-level files; a nested ``sub/foo_test.py``
    is NOT relocated and stays under the package prefix. The translator's
    sweep-exclude must therefore be exactly ``*_test.py`` (root-only), NOT a
    recursive form -- excluding ``**/*_test.py`` would wrongly drop the nested
    original that Copybara keeps under the prefix. Verified against the real
    Copybara binary: nested test files land at ``<prefix>/sub/foo_test.py``.
    """
    config_path = _write_sky(
        tmp_path,
        """
        ROOT = "project"
        core.workflow(
            name = "export",
            origin = folder.origin(),
            destination = folder.destination(),
            origin_files = glob([ROOT + "/**"]),
            authoring = authoring.pass_thru("Demo Export <demo@copybarista.test>"),
            mode = "SQUASH",
            transformations = [
                core.move(ROOT, "tests", paths = glob(["*_test.py"])),
                core.move(ROOT, "pkg"),
            ],
        )
        """,
    )

    config = load_config(config_path)

    # Root-only exclude mirrors Copybara's single-segment `paths` glob; a
    # recursive `**/*_test.py` would wrongly suppress nested tests Copybara keeps.
    assert config.files.exclude == ("*_test.py",)


def test_rejects_subtree_flatten_under_whole_tree_selection(tmp_path: Path):
    """A subtree flatten under `glob(["**"])` is rejected with a clear message.

    `origin_files=glob(["**"])` selects the whole tree; `core.move("sub/pkg", "")`
    flattens only that subtree, leaving every other path at its identity location.
    Real Copybara produces a mixed tree (flattened subtree files at root PLUS
    untouched siblings) that copybarista's single `source_root`/`destination_prefix`
    model cannot represent. The translator must reject this shape explicitly rather
    than misclassify `sub/pkg` as the whole `source_root` and then fail deep in
    prefix-stripping with a misleading "outside core.move source root" error.
    """
    config_path = _write_sky(
        tmp_path,
        """
        core.workflow(
            name = "export",
            origin = folder.origin(),
            destination = folder.destination(),
            origin_files = glob(["**"]),
            authoring = authoring.pass_thru("Demo Export <demo@copybarista.test>"),
            mode = "SQUASH",
            transformations = [core.move("sub/pkg", "")],
        )
        """,
    )

    with pytest.raises(ConfigError, match="whole-tree"):
        load_config(config_path)


def test_a_chain_of_renames_lands_the_copy_at_its_final_path(tmp_path: Path):
    """Fusing a rename must follow the whole chain, not one link.

    ``core.move(a, B)`` then ``core.move(B, C)`` left the copy at ``B`` with a
    trailing move to ``C``, reinstating the two-step staging the fuse exists to
    remove -- and with it the orphaned source directories, whenever the
    intermediate name is nested.
    """
    source = tmp_path / "repo"
    (source / "project").mkdir(parents=True)
    (source / "project" / "mod.py").write_text("x = 1\n", encoding="utf-8")
    (source / "a").mkdir()
    (source / "a" / "x.md").write_text("doc\n", encoding="utf-8")
    config_path = _write_sky(
        tmp_path,
        """
        ROOT = "project"
        core.workflow(
            name = "export",
            origin = folder.origin(),
            destination = folder.destination(),
            origin_files = glob([ROOT + "/**", "a/x.md"]),
            authoring = authoring.pass_thru("Demo Export <demo@copybarista.test>"),
            mode = "SQUASH",
            transformations = [
                core.move(ROOT, ""),
                core.move("a/x.md", "B.md"),
                core.move("B.md", "C.md"),
            ],
        )
        """,
    )
    output = tmp_path / "out"

    export_folder(
        config=load_config(config_path, workflow_name="export"),
        source_ref=source,
        destination=output,
        force=True,
    )

    assert (output / "C.md").read_text(encoding="utf-8") == "doc\n"
    assert not (output / "B.md").exists()


def test_moving_a_second_origin_root_is_not_a_rival_source_root_move(
    tmp_path: Path,
):
    """Only the move the package nests under is the source-root move.

    ``origin_files`` may name several ``/**`` roots -- the package plus vendored
    trees like ``typings/cloudpickle``. Treating "source is a root" as the whole
    signal makes every one of them a source-root move, so relocating the second
    fails as a duplicate. The package's move is the one whose destination the
    other moves are expressed relative to; a vendored tree merely gets renamed.
    """
    config_path = _write_sky(
        tmp_path,
        """
        ROOT = "project"
        core.workflow(
            name = "export",
            origin = folder.origin(),
            destination = folder.destination(),
            origin_files = glob([ROOT + "/**", "typings/cloudpickle/**"]),
            authoring = authoring.pass_thru("Demo Export <demo@copybarista.test>"),
            mode = "SQUASH",
            transformations = [
                core.move(ROOT, "pkg"),
                core.move("pkg/README.md", "README.md"),
                core.move("typings/cloudpickle", "vendor"),
            ],
        )
        """,
    )

    config = load_config(config_path)

    assert config.source_root == "project"
    assert ("typings/cloudpickle", "vendor") in [
        (copy.source, copy.destination) for copy in config.files.copy
    ]


def test_flattening_an_extra_origin_root_is_not_a_source_root_move(tmp_path: Path):
    """A move of a selected file OUTSIDE the package is not the source-root move.

    The source-root move is the one relocating the package itself, named by an
    ``origin_files`` root. Classifying on shape instead -- "empty destination and
    not under a root" -- also claims a shared-boilerplate file flattened to the
    public root, so the config dies with "Only one source-root core.move
    transform is supported", naming a construct the author never wrote.
    """
    config_path = _write_sky(
        tmp_path,
        """
        ROOT = "project"
        core.workflow(
            name = "export",
            origin = folder.origin(),
            destination = folder.destination(),
            origin_files = glob([ROOT + "/**", "ops/github/shared/LICENSE"]),
            authoring = authoring.pass_thru("Demo Export <demo@copybarista.test>"),
            mode = "SQUASH",
            transformations = [
                core.move(ROOT, "pkgpub"),
                core.move("ops/github/shared/LICENSE", ""),
            ],
        )
        """,
    )

    config = load_config(config_path)

    assert config.source_root == "project"


def test_accepts_sky_extra_origin_files_as_file_copies(tmp_path: Path):
    config_path = _write_sky(
        tmp_path,
        """
        ROOT = "project"
        core.workflow(
            name = "export",
            origin = folder.origin(),
            destination = folder.destination(),
            origin_files = glob([ROOT + "/**", ".codespell-ignore", "typings/cloudpickle/**"]),
            authoring = authoring.pass_thru("Demo Export <demo@copybarista.test>"),
            mode = "SQUASH",
            transformations = [core.move(ROOT, "")],
        )
        """,
    )

    config = load_config(config_path)

    assert config.source_root == "project"
    assert [(copy.source, copy.destination) for copy in config.files.copy] == [
        (".codespell-ignore", ".codespell-ignore"),
        ("typings/cloudpickle", "typings/cloudpickle"),
    ]


def test_extra_origin_root_renamed_by_a_move_lands_only_at_its_destination(
    tmp_path: Path,
):
    """A selected-then-moved file must not leave its source directories behind.

    An ``origin_files`` entry outside the source root becomes an identity copy,
    and a later ``core.move`` renames it. Run as two steps the copy first
    materializes ``shared/nested/LICENSE`` and the move then relocates the FILE,
    orphaning the now-empty ``shared/nested`` chain in the export. Real Copybara
    emits no such directories, so the two steps must collapse into one copy that
    writes the file at its final path.
    """
    source = tmp_path / "repo"
    (source / "project").mkdir(parents=True)
    (source / "project" / "mod.py").write_text("x = 1\n", encoding="utf-8")
    (source / "shared" / "nested").mkdir(parents=True)
    (source / "shared" / "nested" / "LICENSE").write_text("lic\n", encoding="utf-8")
    config_path = _write_sky(
        tmp_path,
        """
        ROOT = "project"
        core.workflow(
            name = "export",
            origin = folder.origin(),
            destination = folder.destination(),
            origin_files = glob([ROOT + "/**", "shared/nested/LICENSE"]),
            destination_files = glob(["**"]),
            authoring = authoring.pass_thru("Demo Export <demo@copybarista.test>"),
            mode = "SQUASH",
            transformations = [
                core.move(ROOT, ""),
                core.move("shared/nested/LICENSE", "LICENSE"),
            ],
        )
        """,
    )
    output = tmp_path / "out"

    export_folder(
        config=load_config(config_path, workflow_name="export"),
        source_ref=source,
        destination=output,
        force=True,
    )

    assert (output / "LICENSE").read_text(encoding="utf-8") == "lic\n"
    assert [
        path.relative_to(output).as_posix()
        for path in sorted(output.rglob("*"))
        if path.is_dir()
    ] == []


def test_accepts_sky_file_move_transform(tmp_path: Path):
    config_path = _write_sky(
        tmp_path,
        """
        core.workflow(
            name = "export",
            origin = folder.origin(),
            destination = folder.destination(),
            origin_files = glob(["project/**"]),
            authoring = authoring.pass_thru("Demo Export <demo@copybarista.test>"),
            mode = "SQUASH",
            transformations = [
                core.move("project", ""),
                core.move("pyproject.public.toml", "pyproject.toml"),
            ],
        )
        """,
    )

    config = load_config(config_path)

    assert config.source_root == "project"
    assert len(config.transforms) == 1
    assert config.transforms[0].type == "move"
    assert config.transforms[0].path == "pyproject.public.toml"
    assert config.transforms[0].destination == "pyproject.toml"


def test_rejects_sky_replace_multiline_without_multiline_true(tmp_path: Path):
    config_path = _write_sky(
        tmp_path,
        """
        core.workflow(
            name = "export",
            origin = folder.origin(),
            destination = folder.destination(),
            origin_files = glob(["**"]),
            authoring = authoring.pass_thru("Demo Export <demo@copybarista.test>"),
            mode = "SQUASH",
            transformations = [
                core.replace(
                    before = "old\\nvalue",
                    after = "new",
                    paths = glob(["a.py"]),
                ),
            ],
        )
        """,
    )

    with pytest.raises(ConfigError, match="multiline"):
        load_config(config_path)


def test_loads_sky_multiline_literal_replace(tmp_path: Path):
    config_path = _write_sky(
        tmp_path,
        """
        core.workflow(
            name = "export",
            origin = folder.origin(),
            destination = folder.destination(),
            origin_files = glob(["**"]),
            authoring = authoring.pass_thru("Demo Export <demo@copybarista.test>"),
            mode = "SQUASH",
            transformations = [
                core.replace(
                    before = "old\\nvalue",
                    after = "new\\nvalue",
                    multiline = True,
                    paths = glob(["a.py", "b.py"]),
                ),
            ],
        )
        """,
    )

    config = load_config(config_path)

    assert [transform.path for transform in config.transforms] == ["a.py", "b.py"]
    assert all(transform.type == "replace" for transform in config.transforms)
    assert config.transforms[0].before == "old\nvalue"
    assert config.transforms[0].after == "new\nvalue"


def test_to_raw_config_round_trips_copy_default_python_excludes_opt_in():
    workflow = TranslatedWorkflow(
        name="export",
        mode="squash",
        source_root="pkg",
        include=("**",),
        exclude=(),
        transforms=(),
        copies=(
            FileCopy(
                source="pkg/.export",
                destination=".",
                use_default_python_excludes=True,
            ),
        ),
    )

    parsed = parse_config(workflow.to_raw_config())

    # The opt-in survives the sky -> raw -> parsed round trip (the field defaults
    # to False, so a dropped field would silently disable it).
    assert parsed.files.copy[0].use_default_python_excludes is True


def test_regex_groups_survive_sky_translation_and_apply(tmp_path: Path):
    """A ``regex_groups`` replace must keep its groups through the TOML round trip.

    ``regex_groups`` is how a .sky config expresses a marker-delimited strip that
    is not a fixed literal. It travels sky -> raw dict -> parsed config, and any
    link that drops the field degrades the transform into a literal replace that
    silently matches nothing -- so assert both the parsed shape and the applied
    text.
    """
    config_path = _write_sky(
        tmp_path,
        """
        core.workflow(
            name = "export",
            origin = folder.origin(),
            destination = folder.destination(),
            origin_files = glob(["**"]),
            destination_files = glob(["**"]),
            authoring = authoring.pass_thru("Demo Export <demo@copybarista.test>"),
            mode = "SQUASH",
            transformations = [
                core.transform(
                    [
                        core.replace(
                            before = "# x:start${block}# x:end\\n",
                            after = "",
                            regex_groups = { "block" : "[\\\\s\\\\S]*?" },
                            paths = glob(["conf.yaml"]),
                        ),
                    ],
                    reversal = [],
                ),
            ],
        )
        """,
    )

    config = load_config(config_path)
    transform = config.transforms[0]

    assert transform.regex_groups == (("block", "[\\s\\S]*?"),)
    # Re-parse the emitted TOML rather than probing it for a substring: only a
    # round trip proves the serializer wrote a form the parser reads back.
    round_tripped = tmp_path / "round-trip.toml"
    round_tripped.write_text(
        translate_copy_bara_sky_to_toml(config_path), encoding="utf-8"
    )
    assert load_config(round_tripped).transforms[0].regex_groups == (
        ("block", "[\\s\\S]*?"),
    )

    root = tmp_path / "staged"
    root.mkdir()
    (root / "conf.yaml").write_text(
        "keep: 1\n# x:start\ndrop: 2\n# x:end\nkeep: 3\n", encoding="utf-8"
    )
    apply_transform(root=root, transform=transform, sources_by_destination={})

    assert (root / "conf.yaml").read_text() == "keep: 1\nkeep: 3\n"


@pytest.mark.parametrize(
    "wrapper_args",
    [
        pytest.param("reversal = [],", id="empty-reversal"),
        pytest.param(
            """reversal = [
                        core.replace(
                            before = "demo",
                            after = "@@PKG@@",
                            paths = glob(["README.md"]),
                        ),
                    ],""",
            id="explicit-reversal",
        ),
        pytest.param("", id="no-reversal"),
    ],
)
def test_ignore_noop_reaches_every_transform_group_exit(
    tmp_path: Path, wrapper_args: str
):
    """``ignore_noop`` must take effect on every path out of ``core.transform``.

    The wrapper has three exits (no ``reversal``, empty ``reversal``, explicit
    ``reversal``) and only one consumed the kwarg, so the other two accepted it
    and discarded it -- a config that reads as tolerating a no-op still failed
    the export. Parametrized over all three so a new exit cannot reintroduce the
    gap.
    """
    config_path = _write_sky(
        tmp_path,
        f"""
        core.workflow(
            name = "export",
            origin = folder.origin(),
            destination = folder.destination(),
            origin_files = glob(["**"]),
            destination_files = glob(["**"]),
            authoring = authoring.pass_thru("Demo Export <demo@copybarista.test>"),
            mode = "SQUASH",
            transformations = [
                core.transform(
                    [
                        core.replace(
                            before = "@@PKG@@",
                            after = "demo",
                            paths = glob(["README.md"]),
                        ),
                    ],
                    {wrapper_args}
                    ignore_noop = True,
                ),
            ],
        )
        """,
    )

    assert load_config(config_path).transforms[0].required is False


def test_forward_only_declaration_never_disarms_a_marker_strip(tmp_path: Path):
    """``reversal = []`` on a marker strip must not make it forward-only.

    A marker strip reverses by RE-INSERTING the source's removed region
    (``import_request`` dispatches on the transform type), and that dispatch
    sits behind a ``not transform.reversible`` skip -- so a forward-only marker
    would be skipped entirely and the import would overwrite the source file
    with the stripped public one.

    Rejecting the declaration outright (the previous behaviour) is not
    available: real Copybara REQUIRES ``reversal = []`` on the ``regex_groups``
    replace that is its only whole-line-delete spelling, so a ``.sky`` valid for
    both tools must carry it. The declaration is therefore accepted and ignored
    for marker types -- they are already reversible by re-insertion, which is
    what the declaration was asking for.
    """
    config_path = _write_sky(
        tmp_path,
        """
        core.workflow(
            name = "export",
            origin = folder.origin(),
            destination = folder.destination(),
            origin_files = glob(["**"]),
            destination_files = glob(["**"]),
            authoring = authoring.pass_thru("Demo Export <demo@copybarista.test>"),
            mode = "SQUASH",
            transformations = [
                core.transform(
                    [
                        core.replace(
                            before = "# copybarista:internal\\n",
                            after = "",
                            multiline = True,
                            paths = glob([".pre-commit-config.yaml"]),
                        ),
                    ],
                    reversal = [],
                ),
            ],
        )
        """,
    )

    transform = load_config(config_path).transforms[0]

    assert transform.type == "internal_lines"
    assert transform.reversible is True


def test_sky_replace_carries_required_and_reversible(tmp_path: Path):
    """``core.transform(reversal = [])`` and ``ignore_noop`` must reach Transform.

    The translator built ``Transform`` without either field, so every sky
    transform silently took the ``True`` defaults. A forward-only rewrite
    (Copybara's declared no-op reversal) then reversed on import -- for the
    ``@@PKG@@`` substitution that rewrites every legitimate mention of the
    package name back to the placeholder.
    """
    config_path = _write_sky(
        tmp_path,
        """
        core.workflow(
            name = "export",
            origin = folder.origin(),
            destination = folder.destination(),
            origin_files = glob(["**"]),
            destination_files = glob(["**"]),
            authoring = authoring.pass_thru("Demo Export <demo@copybarista.test>"),
            mode = "SQUASH",
            transformations = [
                core.transform(
                    [
                        core.replace(
                            before = "@@PKG@@",
                            after = "demo",
                            paths = glob(["README.md"]),
                        ),
                    ],
                    reversal = [],
                    ignore_noop = True,
                ),
            ],
        )
        """,
    )

    transform = load_config(config_path).transforms[0]

    assert transform.reversible is False
    assert transform.required is False


@pytest.mark.parametrize(
    ("before", "groups", "expected"),
    [
        pytest.param(
            "${indent}# copybarista:internal${note}\\n",
            '{ "indent" : "[^\\\\n]*", "note" : "[^\\\\n]*" }',
            ("internal_lines", "# copybarista:internal", ""),
            id="per-line",
        ),
        pytest.param(
            "# copybarista:internal:start${block}# copybarista:internal:end\\n${gap}",
            '{ "block" : "[\\\\s\\\\S]*?", "gap" : "\\\\n*" }',
            (
                "strip_block",
                "# copybarista:internal:start",
                "# copybarista:internal:end",
            ),
            id="block",
        ),
    ],
)
def test_marker_strip_spelled_with_regex_groups_maps_to_marker_transform(
    tmp_path: Path,
    before: str,
    groups: str,
    expected: tuple[str, str, str],
):
    """Copybara's marker-strip spelling must load as the native marker type.

    Real Copybara cannot delete a whole line with a literal ``core.replace``: the
    literal consumes the marker and its newline, welding the following line onto
    the previous one (verified against the copybara binary). Only the
    ``regex_groups`` form, whose ``${indent}``/``${note}`` groups absorb the rest
    of the physical line, removes the line cleanly -- and Copybara additionally
    demands ``reversal = []`` for it, since a group replace is not automatically
    reversible.

    Copybarista must therefore ACCEPT that spelling and recover the native
    marker transform from it. Mapping it to a plain ``replace`` instead would be
    non-reversible on import (an empty ``after`` cannot re-derive the removed
    text), which is why the marker types exist.
    """
    config_path = _write_sky(
        tmp_path,
        f"""
        core.workflow(
            name = "export",
            origin = folder.origin(),
            destination = folder.destination(),
            origin_files = glob(["**"]),
            destination_files = glob(["**"]),
            authoring = authoring.pass_thru("Demo Export <demo@copybarista.test>"),
            mode = "SQUASH",
            transformations = [
                core.transform(
                    [
                        core.replace(
                            before = "{before}",
                            after = "",
                            regex_groups = {groups},
                            multiline = True,
                            paths = glob([".pre-commit-config.yaml"]),
                        ),
                    ],
                    reversal = [],
                    ignore_noop = True,
                ),
            ],
        )
        """,
    )

    transform = load_config(config_path).transforms[0]

    assert (transform.type, transform.start, transform.end) == expected
    # The marker types re-insert the source region on import, so they must stay
    # reversible even though Copybara spells the rule ``reversal = []``.
    assert transform.reversible is True
    assert transform.required is False


def test_marker_strip_regex_groups_strips_the_whole_line(tmp_path: Path):
    """The recovered transform must delete the marker's entire physical line.

    Pins the behaviour the literal spelling got wrong: an inline marker at the
    end of a line must take the line break with it, leaving the following line
    intact rather than welded onto the previous one.
    """
    config_path = _write_sky(
        tmp_path,
        """
        core.workflow(
            name = "export",
            origin = folder.origin(),
            destination = folder.destination(),
            origin_files = glob(["**"]),
            destination_files = glob(["**"]),
            authoring = authoring.pass_thru("Demo Export <demo@copybarista.test>"),
            mode = "SQUASH",
            transformations = [
                core.transform(
                    [
                        core.replace(
                            before = "${indent}# copybarista:internal${note}\\n",
                            after = "",
                            regex_groups = {
                                "indent" : "[^\\\\n]*",
                                "note" : "[^\\\\n]*",
                            },
                            multiline = True,
                            paths = glob(["conf.yaml"]),
                        ),
                    ],
                    reversal = [],
                    ignore_noop = True,
                ),
            ],
        )
        """,
    )
    root = tmp_path / "staged"
    root.mkdir()
    (root / "conf.yaml").write_text(
        "keep: 1\n  |drop/me/  # copybarista:internal\n  )\n", encoding="utf-8"
    )

    apply_transform(
        root=root,
        transform=load_config(config_path).transforms[0],
        sources_by_destination={},
    )

    assert (root / "conf.yaml").read_text() == "keep: 1\n  )\n"


def _marker_sky(
    tmp_path: Path, *, before: str, after: str = "", groups: str, multiline: str = ""
) -> Path:
    """Write a one-transform sky config for the marker-recovery tests."""
    return _write_sky(
        tmp_path,
        f"""
        core.workflow(
            name = "export",
            origin = folder.origin(),
            destination = folder.destination(),
            origin_files = glob(["**"]),
            destination_files = glob(["**"]),
            authoring = authoring.pass_thru("Demo Export <demo@copybarista.test>"),
            mode = "SQUASH",
            transformations = [
                core.transform(
                    [
                        core.replace(
                            before = "{before}",
                            after = "{after}",
                            regex_groups = {groups},
                            {multiline}paths = glob(["c.yaml"]),
                        ),
                    ],
                    reversal = [],
                ),
            ],
        )
        """,
    )


@pytest.mark.parametrize(
    ("groups", "match"),
    [
        pytest.param(
            '{ "indent" : "[unclosed", "note" : "[^\\\\n]*" }',
            "regex_groups.indent",
            id="invalid-regex",
        ),
        pytest.param('{ "indent" : "[^\\\\n]*" }', "undefined", id="undefined-group"),
    ],
)
def test_marker_recovery_validates_the_groups_it_discards(
    tmp_path: Path, groups: str, match: str
):
    """Recovering a marker type must not silently drop unchecked groups.

    ``_marker_strip_from_regex_groups`` returns a marker transform and discards
    ``regex_groups`` entirely, so an invalid pattern or an undefined
    interpolation was never validated. Real Copybara refuses to LOAD both --
    ``'regex_groups' includes invalid regex for key indent`` (exit 2) and
    ``Interpolation is used but not defined: note`` (exit 1) -- so a config
    accepted here cannot run there.
    """
    config_path = _marker_sky(
        tmp_path,
        before="${indent}# copybarista:internal${note}\\n",
        groups=groups,
        multiline="multiline = True, ",
    )

    with pytest.raises(ConfigError, match=match):
        load_config(config_path)


def test_external_marker_recovers_as_uncomment(tmp_path: Path):
    """An ``:external`` block uncomments; recovering it as a strip DELETES code.

    ``# copybarista:external:*`` marks lines that must be UNCOMMENTED for the
    public export. The detector recovered every marker as ``strip_block``, which
    removes the region instead -- so the public package silently lost the code
    the marker exists to ship.

    Copybara expresses the same rule as a non-empty ``after`` that re-emits the
    captured group; verified against the binary to produce the uncommented line
    between its unchanged neighbours.
    """
    config_path = _marker_sky(
        tmp_path,
        before="# copybarista:external:start\\n# ${code}\\n# copybarista:external:end\\n",
        after="${code}\\n",
        groups='{ "code" : "[^\\\\n]*" }',
        multiline="multiline = True, ",
    )
    root = tmp_path / "staged"
    root.mkdir()
    (root / "c.yaml").write_text(
        "keep1\n# copybarista:external:start\n# import x\n"
        "# copybarista:external:end\nkeep2\n",
        encoding="utf-8",
    )

    transform = load_config(config_path).transforms[0]
    apply_transform(root=root, transform=transform, sources_by_destination={})

    assert transform.type == "uncomment"
    assert (root / "c.yaml").read_text() == "keep1\nimport x\nkeep2\n"


def test_rejects_external_marker_spelled_as_a_deletion(tmp_path: Path):
    """An ``:external`` strip has no correct reading: it would delete public code.

    There is no ``strip_block`` interpretation of an uncomment marker, so an
    empty ``after`` on one is a config error rather than something to recover.
    """
    config_path = _marker_sky(
        tmp_path,
        before="# copybarista:external:start${block}# copybarista:external:end\\n",
        groups='{ "block" : "[\\\\s\\\\S]*?" }',
        multiline="multiline = True, ",
    )

    with pytest.raises(ConfigError, match="external"):
        load_config(config_path)


def test_conditional_markers_recover_the_else_branch(tmp_path: Path):
    """``:if``/``:else``/``:endif`` keeps the else branch; a strip deletes it.

    Recovered as a plain ``strip_block`` the whole block vanishes, taking the
    PUBLIC branch with it. The ``else_marker`` form uncomments and keeps it.
    """
    config_path = _marker_sky(
        tmp_path,
        before=("# copybarista:if${a}# copybarista:else${b}# copybarista:endif\\n"),
        groups='{ "a" : "[\\\\s\\\\S]*?", "b" : "[\\\\s\\\\S]*?" }',
        multiline="multiline = True, ",
    )
    root = tmp_path / "staged"
    root.mkdir()
    (root / "c.yaml").write_text(
        "keep1\n# copybarista:if\nINTERNAL\n# copybarista:else\n# PUBLIC\n"
        "# copybarista:endif\nkeep2\n",
        encoding="utf-8",
    )

    transform = load_config(config_path).transforms[0]
    apply_transform(root=root, transform=transform, sources_by_destination={})

    assert transform.else_marker == "# copybarista:else"
    assert (root / "c.yaml").read_text() == "keep1\nPUBLIC\nkeep2\n"


def test_non_marker_text_containing_the_prefix_stays_a_replace(tmp_path: Path):
    """A literal merely CONTAINING ``copybara:`` is not a marker strip.

    The detector tested the first and last line for the substring, so a general
    ``regex_groups`` replace was hijacked into ``strip_block start='prefix '``,
    which then raises ``did not find end marker`` on text real Copybara simply
    no-ops.
    """
    config_path = _marker_sky(
        tmp_path,
        before="prefix ${g} copybara: suffix",
        groups='{ "g" : "[a-z]*" }',
    )

    assert load_config(config_path).transforms[0].type == "replace"


def test_marker_on_a_middle_line_is_recovered(tmp_path: Path):
    """Detection must inspect every literal line, not just first and last."""
    config_path = _marker_sky(
        tmp_path,
        before="# aaa${x}# copybarista:internal:start${y}# copybarista:internal:end\\n",
        groups='{ "x" : "[^!]*", "y" : "[\\\\s\\\\S]*?" }',
        multiline="multiline = True, ",
    )

    assert load_config(config_path).transforms[0].type == "strip_block"


def test_rejects_marker_recovery_without_multiline(tmp_path: Path):
    """A marker rule without ``multiline = True`` hard-fails in real Copybara.

    Measured: the same rule exits 2 with no output there, while copybarista
    recovered and applied it regardless -- a config that loads here and cannot
    run at all in the tool the ``.sky`` exists to mirror.
    """
    config_path = _marker_sky(
        tmp_path,
        before="${indent}# copybarista:internal${note}\\n",
        groups='{ "indent" : "[^\\\\n]*", "note" : "[^\\\\n]*" }',
    )

    with pytest.raises(ConfigError, match="multiline"):
        load_config(config_path)


def test_rejects_a_lone_uncomment_marker(tmp_path: Path):
    """An uncomment needs a start AND an end marker to name its block.

    A single ``:external`` line recovered as ``uncomment`` with ``end = ""``,
    which ``transforms._uncomment`` reads as the INLINE form: it splits the line
    on the marker and uncomments the prefix, not the block the author fenced.
    ``config`` accepts an empty ``end``, so nothing downstream notices.
    """
    config_path = _marker_sky(
        tmp_path,
        before="# copybarista:external\\n# ${code}\\n",
        after="${code}\\n",
        groups='{ "code" : "[^\\\\n]*" }',
        multiline="multiline = True, ",
    )

    with pytest.raises(ConfigError, match="start and end marker"):
        load_config(config_path)


def test_rejects_a_single_line_marker_without_multiline(tmp_path: Path):
    """The multiline rule must read the same for both marker spellings.

    The ``regex_groups`` branch rejected a marker rule lacking ``multiline``, but
    a literal one fell through to the newline check and reported something else
    for the same mistake. Copybara hard-fails either.
    """
    config_path = _write_sky(
        tmp_path,
        """
        core.workflow(
            name = "export",
            origin = folder.origin(),
            destination = folder.destination(),
            origin_files = glob(["**"]),
            destination_files = glob(["**"]),
            authoring = authoring.pass_thru("Demo Export <demo@copybarista.test>"),
            mode = "SQUASH",
            transformations = [
                core.replace(
                    before = "# copybarista:internal",
                    after = "",
                    paths = glob([".pre-commit-config.yaml"]),
                ),
            ],
        )
        """,
    )

    with pytest.raises(ConfigError, match="marker requires multiline"):
        load_config(config_path)


def test_rejects_a_replacement_mixing_marker_kinds(tmp_path: Path):
    """One replacement must not span two marker namespaces.

    ``:internal`` deletes its region and ``:external`` uncomments it, so a
    ``before`` pairing an ``:internal:start`` with an ``:external:end`` has no
    single meaning. The kind test was a membership check, so the ``:external``
    branch claimed the pair and UNCOMMENTED an internal block into the public
    tree -- the same class of content leak the marker map exists to prevent,
    inverted.
    """
    config_path = _marker_sky(
        tmp_path,
        before=(
            "# copybarista:internal:start\\n# code\\n# copybarista:external:end\\n"
        ),
        after="x",
        groups="{}",
        multiline="multiline = True, ",
    )

    with pytest.raises(ConfigError, match="one marker kind"):
        load_config(config_path)


def test_rejects_an_uncomment_marker_block_with_a_stray_third_marker(tmp_path: Path):
    """The marker-count guard must cover uncomment, not only strips.

    The ``:external`` branch returned before the count check, so a third marker
    line was silently dropped from the recovered block rather than reported.
    """
    config_path = _marker_sky(
        tmp_path,
        before=(
            "# copybarista:external:start\\n# copybarista:external\\n"
            "# copybarista:external:end\\n"
        ),
        after="x",
        groups="{}",
        multiline="multiline = True, ",
    )

    with pytest.raises(ConfigError, match="at most a start and end marker"):
        load_config(config_path)


def test_rejects_two_origin_copies_fused_onto_one_destination(tmp_path: Path):
    """Fusing a rename into a copy must not create two copies of one path.

    ``config._validate_moves_injective`` rejects a non-injective MOVE sequence
    because the import reverse is an exact inverse only when the forward map is
    injective. Folding those moves into copies routes around that gate, so the
    collision has to be rejected here instead -- otherwise it surfaces as a late
    export-time path clash and an ambiguous reverse.
    """
    config_path = _write_sky(
        tmp_path,
        """
        ROOT = "project"
        core.workflow(
            name = "export",
            origin = folder.origin(),
            destination = folder.destination(),
            origin_files = glob([ROOT + "/**", "a/x.md", "b/x.md"]),
            authoring = authoring.pass_thru("Demo Export <demo@copybarista.test>"),
            mode = "SQUASH",
            transformations = [
                core.move(ROOT, "pkg"),
                core.move("a/x.md", "SHARED.md"),
                core.move("b/x.md", "SHARED.md"),
            ],
        )
        """,
    )

    with pytest.raises(ConfigError, match="claimed by two"):
        load_config(config_path)


def test_rejects_marker_strip_with_more_than_two_markers(tmp_path: Path):
    """A third marker line has no meaning in either strip shape.

    One marker is a per-line strip and two delimit a block; recovering a
    ``strip_block`` from the first and last would silently discard the middle
    one.
    """
    config_path = _write_sky(
        tmp_path,
        """
        core.workflow(
            name = "export",
            origin = folder.origin(),
            destination = folder.destination(),
            origin_files = glob(["**"]),
            destination_files = glob(["**"]),
            authoring = authoring.pass_thru("Demo Export <demo@copybarista.test>"),
            mode = "SQUASH",
            transformations = [
                core.transform(
                    [
                        core.replace(
                            before = "# copybarista:a:start${x}# copybarista:a${y}# copybarista:a:end",
                            after = "",
                            regex_groups = { "x" : "[^!]*", "y" : "[^!]*" },
                            multiline = True,
                            paths = glob(["conf.yaml"]),
                        ),
                    ],
                    reversal = [],
                ),
            ],
        )
        """,
    )

    with pytest.raises(ConfigError, match="at most a start and end"):
        load_config(config_path)


def test_rejects_multiline_with_regex_groups(tmp_path: Path):
    """``multiline`` must not be accepted and then discarded.

    The ``regex_groups`` branch returned before the ``multiline`` handling, so
    the kwarg was dropped: newline spanning came from the group patterns
    instead, and the "before containing newlines requires multiline" guard was
    bypassed. A config that reads correct behaved differently.
    """
    config_path = _write_sky(
        tmp_path,
        """
        core.workflow(
            name = "export",
            origin = folder.origin(),
            destination = folder.destination(),
            origin_files = glob(["**"]),
            destination_files = glob(["**"]),
            authoring = authoring.pass_thru("Demo Export <demo@copybarista.test>"),
            mode = "SQUASH",
            transformations = [
                core.replace(
                    before = "a${g}b",
                    after = "c",
                    regex_groups = { "g" : "[a-z]*" },
                    multiline = True,
                    paths = glob(["conf.yaml"]),
                ),
            ],
        )
        """,
    )

    with pytest.raises(ConfigError, match="multiline"):
        load_config(config_path)


@pytest.mark.parametrize(
    "transform",
    [
        pytest.param(
            Transform(
                id="t",
                type="strip_block",
                path="a.md",
                start="S",
                end="E",
                else_marker="X",
            ),
            id="strip_block-else",
        ),
        pytest.param(
            Transform(id="t", type="internal_lines", path="a.yaml", start="M"),
            id="internal_lines",
        ),
        pytest.param(
            Transform(id="t", type="uncomment", path="a.yaml", start="M", end="E"),
            id="uncomment",
        ),
        pytest.param(Transform(id="t", type="ruff_format", path="."), id="ruff_format"),
        pytest.param(
            Transform(id="t", type="move", path="a.md", destination="b.md"), id="move"
        ),
    ],
)
def test_transform_survives_the_raw_config_round_trip(transform: Transform):
    """Every transform type must survive Transform -> raw -> Transform intact.

    ``.sky`` configs reach ``parse_config`` through this serialization, so a
    field it drops is silently lost and a key it invents is rejected by
    ``_check_keys``. Both had happened: ``else_marker`` vanished, and every
    non-replace/non-move type was written with ``start``/``end``/``inclusive``
    regardless of whether its parser accepts them.
    """
    round_tripped = _parse_transform(0, _transform_to_raw(transform))

    assert round_tripped == transform


def test_translate_outputs_copybarista_toml(tmp_path: Path):
    config_path = _write_sky(
        tmp_path,
        """
        core.workflow(
            name = "export",
            origin = folder.origin(),
            destination = folder.destination(),
            origin_files = glob(["**"]),
            destination_files = glob(["**"]),
            authoring = authoring.pass_thru("Demo Export <demo@copybarista.test>"),
            mode = "SQUASH",
            transformations = [],
        )
        """,
    )

    translated = translate_copy_bara_sky_to_toml(config_path)

    assert "[workflow]" in translated
    assert 'name = "export"' in translated
    assert "[files]" in translated


def test_cli_translate_writes_output(tmp_path: Path):
    config_path = _write_sky(
        tmp_path,
        """
        core.workflow(
            name = "export",
            origin = folder.origin(),
            destination = folder.destination(),
            origin_files = glob(["**"]),
            destination_files = glob(["**"]),
            authoring = authoring.pass_thru("Demo Export <demo@copybarista.test>"),
            mode = "SQUASH",
            transformations = [],
        )
        """,
    )
    output_path = tmp_path / "copy.barista.toml"

    main(["translate", str(config_path), "--output", str(output_path)])

    assert 'name = "export"' in output_path.read_text(encoding="utf-8")


def test_cli_translate_prints_to_stdout(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    config_path = _write_sky(
        tmp_path,
        """
        core.workflow(
            name = "export",
            origin = folder.origin(),
            destination = folder.destination(),
            origin_files = glob(["**"]),
            destination_files = glob(["**"]),
            authoring = authoring.pass_thru("Demo Export <demo@copybarista.test>"),
            mode = "SQUASH",
            transformations = [],
        )
        """,
    )

    main(["translate", str(config_path)])

    assert 'name = "export"' in capsys.readouterr().out


def test_cli_export_accepts_sky_config(tmp_path: Path):
    source = tmp_path / "repo"
    project = source / "project"
    project.mkdir(parents=True)
    (project / "README.md").write_text("hello\n", encoding="utf-8")
    output_path = tmp_path / "out"
    config_path = _write_sky(
        tmp_path,
        """
        ROOT = "project"
        core.workflow(
            name = "export",
            origin = folder.origin(),
            destination = folder.destination(),
            origin_files = glob([ROOT + "/**"]),
            destination_files = glob(["**"]),
            authoring = authoring.pass_thru("Demo Export <demo@copybarista.test>"),
            mode = "SQUASH",
            transformations = [core.move(ROOT, "")],
        )
        """,
    )

    main(
        [
            "export",
            str(config_path),
            str(source),
            "--folder-dir",
            str(output_path),
        ]
    )

    assert (output_path / "README.md").read_text(encoding="utf-8") == "hello\n"


def test_loads_helper_workflow_with_move_git_and_strip_block(tmp_path: Path):
    config_path = _write_sky(
        tmp_path,
        '''\
ROOT = "packages/widget"
REMOTE = "file:///tmp/widget.git"
BRANCH = "main"
FILES = glob([ROOT + "/**"], exclude = [ROOT + "/dist/**"])

def export_workflow(name, destination):
    core.workflow(
        name = name,
        origin = folder.origin(),
        destination = destination,
        origin_files = FILES,
        destination_files = glob(["**"]),
        authoring = authoring.pass_thru("Demo Export <demo@copybarista.test>"),
        mode = "SQUASH",
        transformations = [
            core.move(ROOT, ""),
            core.replace(
                before = """<!-- copybarista:strip:start -->
internal
<!-- copybarista:strip:end -->
""",
                after = "",
                multiline = True,
                paths = glob(["README.md"]),
            ),
        ],
    )

export_workflow(
    name = "export_git",
    destination = git.destination(url = REMOTE, fetch = BRANCH, push = BRANCH),
)
''',
    )

    config = load_config(config_path, workflow_name="export_git")

    assert config.name == "export_git"
    assert config.source_root == "packages/widget"
    assert config.files.include == ("**",)
    assert config.files.exclude == ("dist/**",)
    assert config.git.url == "file:///tmp/widget.git"
    assert config.git.branch == "main"
    assert config.transforms[0].type == "strip_block"
    assert config.transforms[0].start == "<!-- copybarista:strip:start -->"
    assert config.transforms[0].end == "<!-- copybarista:strip:end -->"
    assert config.transforms[0].inclusive


def test_multiline_strip_translation_preserves_copybara_block_boundaries(
    tmp_path: Path,
):
    config_path = _write_sky(
        tmp_path,
        '''\
core.workflow(
    name = "export",
    origin = folder.origin(),
    destination = folder.destination(),
    origin_files = glob(["pkg/**"]),
    destination_files = glob(["**"]),
    authoring = authoring.pass_thru("Demo Export <demo@copybarista.test>"),
    mode = "SQUASH",
    transformations = [
        core.move("pkg", ""),
        core.replace(
            before = """
<!-- copybarista:strip:start -->
internal
<!-- copybarista:strip:end -->

""",
            after = "",
            multiline = True,
            paths = glob(["README.md"]),
        ),
    ],
)
''',
    )

    config = load_config(config_path)

    assert config.transforms[0].type == "strip_block"
    assert config.transforms[0].start == "\n<!-- copybarista:strip:start -->"
    assert config.transforms[0].end == "<!-- copybarista:strip:end -->\n\n"
    assert config.transforms[0].inclusive


def test_rejects_unsupported_sky_option(tmp_path: Path):
    config_path = _write_sky(
        tmp_path,
        """
        core.workflow(
            name = "export",
            origin = git.origin(url = "https://example.com/repo.git"),
            destination = folder.destination(),
            origin_files = glob(["**"]),
            destination_files = glob(["**"]),
            mode = "SQUASH",
            transformations = [],
        )
        """,
    )

    with pytest.raises(ConfigError, match=r"Unsupported copy\.bara\.sky call"):
        load_config(config_path)


def test_rejects_missing_sky_workflow(tmp_path: Path):
    config_path = _write_sky(
        tmp_path,
        """
        core.workflow(
            name = "other",
            origin = folder.origin(),
            destination = folder.destination(),
            origin_files = glob(["**"]),
            destination_files = glob(["**"]),
            authoring = authoring.pass_thru("Demo Export <demo@copybarista.test>"),
            transformations = [],
        )
        """,
    )

    with pytest.raises(ConfigError, match="Workflow 'export' not found"):
        load_config(config_path)


def test_rejects_invalid_sky_syntax(tmp_path: Path):
    config_path = _write_sky(tmp_path, "core.workflow(")

    with pytest.raises(ConfigError, match=r"Unsupported copy\.bara\.sky syntax"):
        load_config(config_path)


@pytest.mark.parametrize(
    ("source", "match"),
    [
        ("for item in []:\n    pass\n", "Unsupported top-level"),
        ("load('//tools:defs.bzl', 'workflow')\n", "Unsupported top-level call"),
        ("A = B = 'value'\n", "Only simple NAME"),
        ("ROOT = MISSING\n", "Unknown name"),
        ("ROOT = 1 + 2\n", "Only string concatenation"),
        ("ROOT = {'bad': 1}\n", "dict keys and values must be strings"),
        (
            (
                "transform = core.replace(before='a', after='b', "
                "paths=glob(['a.txt']), regex_groups=['not', 'a', 'dict'])\n"
            ),
            "regex_groups must be a dict",
        ),
        ("FILES = glob()\n", r"glob\(\.\.\.\) requires one include list"),
        ("FILES = glob('**')\n", "glob include must be a list"),
        ("origin = folder.origin(path = 'repo')\n", "folder.origin"),
        ("dest = folder.destination(path = 'out')\n", "folder.destination"),
        ("move = core.move('src')\n", "core.move requires"),
        (
            "transform = core.replace('old', after='new', paths=glob(['a.txt']))\n",
            "core.replace positional args",
        ),
        ("transform = core.replace(before='old', after='new')\n", "paths"),
        (
            (
                "transform = core.replace(before='old', after='new', "
                "paths=glob(['a.txt']), first_only=True)\n"
            ),
            "Unsupported argument",
        ),
        (
            "author = authoring.pass_thru('Missing brackets')\n",
            "authoring.pass_thru author",
        ),
    ],
)
def test_rejects_unsupported_sky_expressions(tmp_path: Path, source: str, match: str):
    config_path = _write_sky(tmp_path, source)

    with pytest.raises(ConfigError, match=match):
        load_config(config_path)


@pytest.mark.parametrize(
    ("body", "match"),
    [
        ("helper('one', 'two')\n", "Too many positional args"),
        ("helper()\n", "Missing helper args"),
        ("helper(name='export', **{})\n", r"\*\*kwargs"),
    ],
)
def test_rejects_unsupported_sky_helper_calls(tmp_path: Path, body: str, match: str):
    config_path = _write_sky(
        tmp_path,
        f"""
        def helper(name):
            core.workflow(
                name = name,
                origin = folder.origin(),
                destination = folder.destination(),
                origin_files = glob(["**"]),
                destination_files = glob(["**"]),
                transformations = [],
            )

        {body}
        """,
    )

    with pytest.raises(ConfigError, match=match):
        load_config(config_path)


@pytest.mark.parametrize(
    ("helper_body", "match"),
    [
        ("print('unsupported')", "Unsupported helper call"),
        ("value = 'unsupported'", "Unsupported helper body"),
    ],
)
def test_rejects_unsupported_sky_helper_bodies(
    tmp_path: Path, helper_body: str, match: str
):
    config_path = _write_sky(
        tmp_path,
        f"""
        def helper():
            {helper_body}

        helper()
        """,
    )

    with pytest.raises(ConfigError, match=match):
        load_config(config_path)


@pytest.mark.parametrize(
    ("workflow_kwargs", "match"),
    [
        ("'positional'", "core.workflow positional args"),
        ("name='export', mode='ITERATIVE'", "Only mode"),
        ("name='export'", "Only folder.origin"),
        (
            "name='export', origin=folder.origin(), destination='bad'",
            "Only folder.destination",
        ),
        (
            (
                "name='export', origin=folder.origin(), "
                "destination=folder.destination(), origin_files=glob(['**']), "
                "transformations='bad'"
            ),
            "transformations",
        ),
        (
            (
                "name='export', origin=folder.origin(), "
                "destination=folder.destination(), origin_files=glob(['**']), "
                "transformations=['bad']"
            ),
            "Unsupported transformation",
        ),
        (
            (
                "name='export', origin=folder.origin(), "
                "destination=folder.destination(), origin_files=glob(['src/**']), "
                "transformations=[core.move('project', '')]"
            ),
            "outside core.move source root",
        ),
        (
            (
                "name='export', origin=folder.origin(), "
                "destination=folder.destination(), origin_files=glob(['project/**']), "
                "transformations=[core.move('project', ''), core.move('project', '')]"
            ),
            "Only one source-root core.move",
        ),
        (
            (
                "name='export', origin=folder.origin(), "
                "destination=folder.destination(), origin_files=glob(['**']), "
                "destination_files=glob(['src/**']), transformations=[]"
            ),
            "Only destination_files",
        ),
        (
            (
                "name='export', origin=folder.origin(), "
                "destination=folder.destination(), origin_files=glob(['**']), "
                "destination_files=glob(['**'], exclude=['old/**']), transformations=[]"
            ),
            "Only destination_files",
        ),
    ],
)
def test_rejects_unsupported_sky_workflows(
    tmp_path: Path, workflow_kwargs: str, match: str
):
    if "authoring" not in workflow_kwargs and not workflow_kwargs.startswith("'"):
        workflow_kwargs = (
            f"{workflow_kwargs}, "
            "authoring=authoring.pass_thru('Demo Export <demo@copybarista.test>')"
        )
    config_path = _write_sky(
        tmp_path,
        f"""
        core.workflow({workflow_kwargs})
        """,
    )

    with pytest.raises(ConfigError, match=match):
        load_config(config_path)
