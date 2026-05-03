"""Integration tests for shipped examples."""

from __future__ import annotations

from pathlib import Path

import os
import shutil
import subprocess
import sys

import pytest

from copybarista.cli import main


pytestmark = pytest.mark.integration

EXAMPLES_ROOT = Path(__file__).resolve().parent
SOURCE_REPO = EXAMPLES_ROOT / "python-package" / "source-repo"
GITHUB_EXAMPLE = EXAMPLES_ROOT / "python-package" / "github"
CONFIG = SOURCE_REPO / "copy.barista.toml"
PACKAGE_TESTS = Path("packages/widget/tests")


def test_python_package_example_exports_and_imports_public_change(
    tmp_path: Path,
):
    public_base = tmp_path / "public-base"
    public_head = tmp_path / "public-head"
    destination = tmp_path / "source-destination"

    _run_pytest(root=SOURCE_REPO, tests=SOURCE_REPO / PACKAGE_TESTS)
    _export(source_ref=SOURCE_REPO, destination=public_base)
    _assert_public_export(public_base)
    _run_pytest(root=public_base, tests=public_base / "tests")

    shutil.copytree(public_base, public_head)
    (public_head / "widget/__init__.py").write_text(
        '"""Tiny package used by the Copybarista examples."""\n\n'
        'NAME = "widget"\n'
        'DESCRIPTION = "Public package example."\n\n\n'
        "def label() -> str:\n"
        '    """Return the public package label."""\n'
        "    return NAME\n",
        encoding="utf-8",
    )
    shutil.copytree(SOURCE_REPO, destination)

    main(
        [
            "import-change",
            str(CONFIG),
            "--public-base",
            str(public_base),
            "--public-head",
            str(public_head),
            "--source-base",
            str(SOURCE_REPO),
            "--destination",
            str(destination),
        ]
    )

    assert 'DESCRIPTION = "Public package example."' in (
        destination / "packages/widget/widget/__init__.py"
    ).read_text(encoding="utf-8")
    _run_pytest(root=destination, tests=destination / PACKAGE_TESTS)


def test_github_workflow_examples_call_copybarista_commands():
    source_to_public = (GITHUB_EXAMPLE / "source-to-public.yml").read_text(
        encoding="utf-8"
    )
    public_to_source = (GITHUB_EXAMPLE / "public-to-source.yml").read_text(
        encoding="utf-8"
    )

    assert 'uvx copybarista export "source/$PROJECT_PATH/copy.barista.toml"' in (
        source_to_public
    )
    assert '"source/$PROJECT_PATH"' in source_to_public
    assert '--folder-dir "$RUNNER_TEMP/public-export"' in source_to_public
    assert "--exclude .github/" in source_to_public
    assert "PYTHONDONTWRITEBYTECODE=1" in source_to_public
    assert "-p no:cacheprovider -o addopts= tests" in source_to_public
    assert "COPYBARISTA_EXPORT_BRANCH" in source_to_public
    assert "copybarista/export/{0}" in source_to_public
    assert "auto_merge" in source_to_public
    assert 'if [ "${{ inputs.auto_merge }}" = "true" ]; then' in (source_to_public)
    assert 'gh pr merge "$EXPORT_BRANCH"' in source_to_public
    assert "Do not push manual commits to this generated branch." in (source_to_public)
    assert "gh pr create" in source_to_public

    assert 'uvx copybarista import-change "source/$PROJECT_PATH/copy.barista.toml"' in (
        public_to_source
    )
    assert "--public-base public-base" in public_to_source
    assert "--public-head public-head" in public_to_source
    assert '--source-base "source/$PROJECT_PATH"' in public_to_source
    assert '--destination "source/$PROJECT_PATH"' in public_to_source
    assert (
        "github.event.before != '0000000000000000000000000000000000000000'"
        in public_to_source
    )
    assert "Public-to-source import is not configured; skipping." in public_to_source
    assert 'if [ "${{ github.event_name }}" = "workflow_dispatch" ]; then' in (
        public_to_source
    )
    assert "if: steps.settings.outputs.enabled == 'true'" in public_to_source
    assert "PYTHONDONTWRITEBYTECODE=1" in public_to_source
    assert 'source_base_ref="$(git rev-parse HEAD)"' in public_to_source
    assert 'pr_title="Import public changes ${head_ref:0:12}"' in public_to_source
    assert "Public base: ${{ steps.refs.outputs.base_ref }}" in public_to_source
    assert "Public head: ${{ steps.refs.outputs.head_ref }}" in public_to_source
    assert "Source base: $source_base_ref" in public_to_source
    assert "copybarista/import/sha-" in public_to_source
    assert "gh pr create" in public_to_source


def _export(*, source_ref: Path, destination: Path) -> None:
    """Run the documented folder export command."""
    main(
        [
            "export",
            str(CONFIG),
            str(source_ref),
            "--folder-dir",
            str(destination),
            "--force",
        ]
    )


def _assert_public_export(public_root: Path) -> None:
    """Assert the example export performed its documented transformations."""
    assert sorted(
        path.relative_to(public_root).as_posix()
        for path in public_root.rglob("*")
        if path.is_file()
    ) == [
        "README.md",
        "pyproject.toml",
        "tests/__init__.py",
        "tests/test_widget.py",
        "widget/__init__.py",
    ]
    readme = (public_root / "README.md").read_text(encoding="utf-8")
    test_file = (public_root / "tests/test_widget.py").read_text(encoding="utf-8")
    assert "copybarista:private:start" not in readme
    assert "Internal release note" not in readme
    assert "import widget as widget_module" in test_file
    assert "from widget import label" in test_file
    assert "packages.widget" not in test_file


def _run_pytest(*, root: Path, tests: Path) -> None:
    """Run example tests with isolated pytest options."""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(root)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    # The test invokes the current Python interpreter with a fixed argv list.
    subprocess.run(  # noqa: S603 -- args constructed internally, not from user input
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "-o",
            "addopts=",
            str(tests),
        ],
        check=True,
        cwd=EXAMPLES_ROOT.parent,
        env=env,
        text=True,
        capture_output=True,
    )
