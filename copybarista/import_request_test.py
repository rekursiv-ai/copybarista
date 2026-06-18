"""Tests for change-request imports from public trees."""

from __future__ import annotations

from pathlib import Path

import json
import shutil
import stat
import subprocess

import pytest

from copybarista.cli import main
from copybarista.config import load_config
from copybarista.errors import ImportRequestError
from copybarista.import_request import (
    ChangeRequestImporter,
    ImportRequest,
    PathMapper,
    TreeSnapshot,
    _three_way_merge,
    import_change_request,
)


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
        destination_prefix = "demo"

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


def test_import_rejects_strip_block_paths(tmp_path: Path):
    paths = _fixture(tmp_path, include_strip_block=True)
    public_head = _copy_tree(paths.public_base, tmp_path / "public-head")
    (public_head / "README.md").write_text("public edit\n", encoding="utf-8")

    with pytest.raises(ImportRequestError, match="non-reversible"):
        import_change_request(
            ImportRequest(
                config=load_config(paths.config),
                public_base=paths.public_base,
                public_head=public_head,
                source_base=paths.source_base,
                destination=_copy_tree(paths.source_base, tmp_path / "destination"),
            )
        )


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
    config.write_text(
        f"""
        [workflow]
        name = "demo"
        mode = "squash"
        source_root = "{source_root}"

        [files]
        include = ["**"]
        exclude = ["private.txt"]
        destination_prefix = "{destination_prefix}"
        destination_prefix_exclude = ["README.md"]

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


def _copy_tree(source: Path, destination: Path) -> Path:
    shutil.copytree(source, destination, symlinks=True)
    return destination
