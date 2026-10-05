"""Tests for public release-tree policy checks."""

from __future__ import annotations

from pathlib import Path

import ast
import sys

import pytest

from copybarista.scripts import check_release_tree
from copybarista.scripts.check_release_tree import check_tree


def _write_required_tree(root: Path) -> None:
    for path in (
        root / ".github/workflows/ci.yml",
        root / ".github/workflows/copybarista-to-loop.yml",
        root / "LICENSE",
        root / "README.md",
        root / "copybarista/__init__.py",
        root / "copybarista/scripts/__init__.py",
        root / "pyproject.toml",
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("ok\n", encoding="utf-8")


def test_check_tree_accepts_required_release_shape(tmp_path: Path):
    _write_required_tree(tmp_path)

    assert check_tree(root=tmp_path) == ()


def test_check_tree_accepts_package_validation_workflow_name(tmp_path: Path):
    _write_required_tree(tmp_path)
    (tmp_path / ".github/workflows/ci.yml").unlink()
    (tmp_path / ".github/workflows/package-validation.yml").write_text(
        "ok\n",
        encoding="utf-8",
    )

    assert check_tree(root=tmp_path) == ()


def test_check_tree_allows_deliberate_forbidden_text_fixtures(tmp_path: Path):
    _write_required_tree(tmp_path)
    fixture = tmp_path / "copybarista/scripts/check_pr_text_test.py"
    fixture.write_text("value = 'mono" + "repo/lib'\n", encoding="utf-8")

    assert check_tree(root=tmp_path) == ()


def test_check_tree_rejects_private_generated_and_vcs_paths(tmp_path: Path):
    _write_required_tree(tmp_path)
    for path in (
        tmp_path / "private/SPEC.md",
        tmp_path / "site/index.html",
        tmp_path / "copy.bara.sky",
        tmp_path / "copy.barista.toml",
        tmp_path / ".github/workflows/pages.yml",
        tmp_path / ".pytest_cache/v/cache/nodeids",
        tmp_path / "copybarista/__pycache__/config.pyc",
        tmp_path / "copybarista.egg-info/PKG-INFO",
        tmp_path / "pkg/.git/HEAD",
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("bad\n", encoding="utf-8")

    errors = check_tree(root=tmp_path)

    assert any("Private implementation files" in error for error in errors)
    assert any("Generated directory" in error for error in errors)
    assert any("Python bytecode" in error for error in errors)
    assert any("Build metadata" in error for error in errors)
    assert any("VCS metadata" in error for error in errors)
    assert any("Source-only release file" in error for error in errors)


def test_check_tree_rejects_private_sync_readme_markers(tmp_path: Path):
    _write_required_tree(tmp_path)
    (tmp_path / "README.md").write_text(
        "<!-- copybarista:private-sync:start -->\n"
        "Private notes.\n"
        "<!-- copybarista:private-sync:end -->\n",
        encoding="utf-8",
    )

    errors = check_tree(root=tmp_path)

    assert any("Private sync marker" in error for error in errors)


def test_check_tree_rejects_unstripped_internal_marker(tmp_path: Path):
    """A surviving marker means the monorepo-only strip did not run.

    Asserted on its own file: the shared source-only-text test also writes a
    bad ``.gitignore`` and ``pyproject.toml``, so its single
    ``any("Source-only config text")`` passes even when the pre-commit rule
    matches nothing.
    """
    _write_required_tree(tmp_path)
    (tmp_path / ".pre-commit-config.yaml").write_text(
        "repos: []\n# copybarista:internal:start\n",
        encoding="utf-8",
    )

    errors = check_tree(root=tmp_path)

    assert any(
        "Source-only config text" in error and ".pre-commit-config.yaml" in error
        for error in errors
    )


def test_check_tree_rejects_source_only_config_text(tmp_path: Path):
    _write_required_tree(tmp_path)
    (tmp_path / ".gitignore").write_text(
        "!private/testdata/**/.venv/\n",
        encoding="utf-8",
    )
    (tmp_path / "pyproject.toml").write_text(
        '[tool.ruff.lint.per-file-ignores]\n"private/**" = ["INP001"]\n',
        encoding="utf-8",
    )
    (tmp_path / "docs/guide.md").parent.mkdir()
    (tmp_path / "docs/guide.md").write_text(
        "Run from " + "/Users" + "/dan" + "/loop.\n",
        encoding="utf-8",
    )

    errors = check_tree(root=tmp_path)

    assert any("Source-only config text" in error for error in errors)
    assert any("local developer path" in error for error in errors)


def test_check_tree_rejects_the_exporting_package_namespace(tmp_path: Path):
    """The one monorepo reference this package's own export can emit.

    ``blocked_text`` covered the sibling namespaces but not this package's own
    -- the namespace whose rewrite this very export performs, and the one a
    missed ``[[transform]]`` leaves behind. The package config blocks it; this
    script, the only text scan that runs after the export lands, did not.

    The probe strings are built from an f-string rather than written literally:
    this file ships publicly and is scanned by the very rule under test, and a
    formatter collapses adjacent string literals back into one.
    """
    package = "copybarista"
    _write_required_tree(tmp_path)
    (tmp_path / "docs/guide.md").parent.mkdir(exist_ok=True)
    (tmp_path / "docs/guide.md").write_text(
        f"See loop/{package} and loop.{package}.config\n",
        encoding="utf-8",
    )

    errors = check_tree(root=tmp_path)

    assert any("monorepo path" in error for error in errors)


def test_check_tree_rejects_private_project_names_in_root_docs(tmp_path: Path):
    """Any casing of a private name is blocked, not the two that were listed.

    The rule is a case-insensitive regex rather than a substring set, so the
    mixed casing here -- which no enumeration would have listed -- still trips.
    """
    _write_required_tree(tmp_path)
    (tmp_path / "CHANGELOG.md").write_text(
        "Published " + "kNoWoP" + " sync notes.\n",
        encoding="utf-8",
    )

    errors = check_tree(root=tmp_path)

    assert any("private project name" in error for error in errors)


def test_check_tree_allows_root_git_for_checked_out_public_repo(tmp_path: Path):
    _write_required_tree(tmp_path)
    (tmp_path / ".git/HEAD").parent.mkdir()
    (tmp_path / ".git/HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")

    assert any("VCS metadata" in error for error in check_tree(root=tmp_path))
    assert check_tree(root=tmp_path, allow_root_git=True) == ()


def test_check_tree_reports_missing_required_paths(tmp_path: Path):
    errors = check_tree(root=tmp_path)

    assert any("one of .github/workflows/ci.yml" in error for error in errors)
    assert any("pyproject.toml" in error for error in errors)


def test_run_cli_reports_errors_and_honors_root_git_flag(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert check_release_tree.run([str(tmp_path)]) == 1
    assert "Missing required release path" in capsys.readouterr().err

    _write_required_tree(tmp_path)
    assert check_release_tree.run([str(tmp_path)]) == 0
    (tmp_path / ".git/HEAD").parent.mkdir()
    (tmp_path / ".git/HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    assert check_release_tree.run([str(tmp_path)]) == 1
    assert "VCS metadata" in capsys.readouterr().err
    assert check_release_tree.run([str(tmp_path), "--allow-root-git"]) == 0


def test_check_tree_rejects_every_blocked_text_variant(tmp_path: Path):
    _write_required_tree(tmp_path)
    probes = {
        "experimental.md": "loop/experimental",
        "lib.md": "loop.lib",
        "copybarista.md": "loop/copybarista",
        "users.md": "/Users/dan",
        "home.md": "~/loop",
        "knowop.md": "kNoWOp2",
        "start.md": "<!-- copybarista:private-sync:start -->",
        "end.md": "<!-- copybarista:private-sync:end -->",
    }
    for name, text in probes.items():
        (tmp_path / "docs" / name).parent.mkdir(exist_ok=True)
        (tmp_path / "docs" / name).write_text(text, encoding="utf-8")

    errors = check_tree(root=tmp_path)

    assert sum("monorepo path" in error for error in errors) == 3
    assert sum("local developer path" in error for error in errors) == 2
    assert sum("private project name" in error for error in errors) == 1
    assert sum("Private sync marker" in error for error in errors) == 2


def test_check_tree_rejects_each_source_only_config_token(tmp_path: Path):
    _write_required_tree(tmp_path)
    values = {
        ".gitignore": "private/testdata\n",
        ".pre-commit-config.yaml": "copybarista:internal\n",
        "pyproject.toml": '"private/**"\n"private/testdata/**/*.py"\n',
    }
    for name, text in values.items():
        (tmp_path / name).write_text(text, encoding="utf-8")

    errors = check_tree(root=tmp_path)

    assert sum("Source-only config text" in error for error in errors) == 4


def test_check_tree_ignores_skipped_and_symlinked_text_files(tmp_path: Path):
    _write_required_tree(tmp_path)
    skipped = tmp_path / "copybarista/scripts/check_pr_text_test.py"
    skipped.write_text("loop/experimental\n", encoding="utf-8")
    source = tmp_path / "docs/source.md"
    source.parent.mkdir()
    source.write_text("loop/experimental\n", encoding="utf-8")
    (tmp_path / "docs/link.md").symlink_to(source)

    errors = check_tree(root=tmp_path)

    assert sum("monorepo path" in error for error in errors) == 1


def test_check_tree_reports_all_path_policy_categories(tmp_path: Path):
    _write_required_tree(tmp_path)
    paths = (
        "private/x",
        "site/x",
        "copy.bara.sky",
        "copy.barista.toml",
        ".github/workflows/pages.yml",
        ".pytest_cache/x",
        "pkg/__pycache__/x.pyc",
        "pkg/x.egg-info/PKG-INFO",
        ".coverage",
    )
    for relative in paths:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x\n", encoding="utf-8")

    errors = check_tree(root=tmp_path)

    assert any("Private implementation files" in error for error in errors)
    assert sum("Source-only release file" in error for error in errors) >= 4
    assert any("Generated directory" in error for error in errors)
    assert any("Python bytecode" in error for error in errors)
    assert any("Build metadata" in error for error in errors)
    assert any("Coverage data" in error for error in errors)


def test_parser_help_preserves_description(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        check_release_tree.run(["--help"])

    assert raised.value.code == 0
    assert "Validate that an exported Copybarista tree is safe to publish." in (
        capsys.readouterr().out
    )


def test_check_tree_reports_exact_root_path_and_group_separator(tmp_path: Path):
    errors = check_tree(root=tmp_path)
    assert (
        "Missing required release path: one of .github/workflows/ci.yml, "
        ".github/workflows/package-validation.yml"
    ) in errors

    _write_required_tree(tmp_path)
    (tmp_path / ".coverage").write_text("x\n", encoding="utf-8")
    assert "Coverage data must not be exported: .coverage" in check_tree(root=tmp_path)


def test_uppercase_private_sync_markers_are_not_markers(tmp_path: Path):
    _write_required_tree(tmp_path)
    (tmp_path / "README.md").write_text(
        "<!-- COPYBARISTA:PRIVATE-SYNC:START -->\n",
        encoding="utf-8",
    )

    assert not any(
        "Private sync marker" in error for error in check_tree(root=tmp_path)
    )


def test_content_errors_report_exact_labels(tmp_path: Path):
    _write_required_tree(tmp_path)
    probes = {
        "experimental.md": "loop" + "/experimental",
        "lib.md": "loop" + ".lib",
        "copybarista.md": "loop" + "/copybarista",
        "users.md": "/Users" + "/dan",
        "home.md": "~/" + "loop",
        "knowop.md": "knowop" + "2",
    }
    for name, text in probes.items():
        path = tmp_path / "docs" / name
        path.parent.mkdir(exist_ok=True)
        path.write_text(text, encoding="utf-8")

    errors = check_release_tree._content_errors(tmp_path)

    assert {
        "monorepo path must not be exported: docs/experimental.md",
        "monorepo path must not be exported: docs/lib.md",
        "monorepo path must not be exported: docs/copybarista.md",
        "local developer path must not be exported: docs/users.md",
        "local developer path must not be exported: docs/home.md",
        "private project name must not be exported: docs/knowop.md",
    } <= set(errors)


# Every caller runs the script by path from a bare checkout, where neither this
# package nor its dependencies import: the export's validation step, the public
# repo's CI and the release docs. Mutmut's trampoline import is its own.
def test_the_script_imports_only_the_standard_library():
    tree = ast.parse(Path(check_release_tree.__file__).read_text(encoding="utf-8"))
    roots = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0
    }

    assert roots - {"mutmut"} <= sys.stdlib_module_names


def test_parser_uses_the_third_docstring_line(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        check_release_tree,
        "__doc__",
        "first line\nsecond line\nthird line\nfourth line",
    )

    assert check_release_tree._parser().description == "third line\nfourth line"


if __name__ == "__main__":
    from copybarista.lib.testing.main import test_main

    test_main(__file__)
