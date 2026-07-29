"""Tests for supported `copy.bara.sky` config parsing."""

from __future__ import annotations

from pathlib import Path

import pytest

from copybarista.cli import main
from copybarista.config import FileCopy, load_config, parse_config
from copybarista.copy_bara_sky import (
    TranslatedWorkflow,
    translate_copy_bara_sky_to_toml,
)
from copybarista.errors import ConfigError


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
        ("ROOT = {'bad': 'shape'}\n", "Unsupported copy.bara.sky expression"),
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
