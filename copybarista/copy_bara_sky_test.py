"""Tests for supported `copy.bara.sky` config parsing."""

from __future__ import annotations

from pathlib import Path

import pytest

from copybarista.cli import main
from copybarista.config import load_config
from copybarista.copy_bara_sky import translate_copy_bara_sky_to_toml
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
            "transform = core.replace(before='old', after='new', "
            "multiline=True, paths=glob(['a.txt']))\n",
            "multiline",
        ),
        (
            "transform = core.replace(before='old', after='new', "
            "paths=glob(['a.txt']), first_only=True)\n",
            "Unsupported argument",
        ),
        (
            "transform = core.replace(before='only one marker', after='', "
            "multiline=True, paths=glob(['README.md']))\n",
            "start and end markers",
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
            "name='export', origin=folder.origin(), "
            "destination=folder.destination(), origin_files=glob(['**']), "
            "transformations='bad'",
            "transformations",
        ),
        (
            "name='export', origin=folder.origin(), "
            "destination=folder.destination(), origin_files=glob(['**']), "
            "transformations=['bad']",
            "Unsupported transformation",
        ),
        (
            "name='export', origin=folder.origin(), "
            "destination=folder.destination(), origin_files=glob(['src/**']), "
            "transformations=[core.move('project', '')]",
            "outside core.move source root",
        ),
        (
            "name='export', origin=folder.origin(), "
            "destination=folder.destination(), origin_files=glob(['project/**']), "
            "transformations=[core.move('project', 'out')]",
            r"core.move\(SOURCE, ''\)",
        ),
        (
            "name='export', origin=folder.origin(), "
            "destination=folder.destination(), origin_files=glob(['project/**']), "
            "transformations=[core.move('project', ''), core.move('project', '')]",
            "Only one core.move",
        ),
        (
            "name='export', origin=folder.origin(), "
            "destination=folder.destination(), origin_files=glob(['**']), "
            "destination_files=glob(['src/**']), transformations=[]",
            "Only destination_files",
        ),
        (
            "name='export', origin=folder.origin(), "
            "destination=folder.destination(), origin_files=glob(['**']), "
            "destination_files=glob(['**'], exclude=['old/**']), transformations=[]",
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
