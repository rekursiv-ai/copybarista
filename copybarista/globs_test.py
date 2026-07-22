"""Tests for glob include and exclude behavior."""

from __future__ import annotations

import pytest

from copybarista.errors import GlobError
from copybarista.globs import GlobSet


def test_include_globstar_matches_any_depth():
    globs = GlobSet(include=("**",), exclude=())

    assert globs.matches("README.md")
    assert globs.matches("pkg/module.py")
    assert globs.matches("pkg/subpkg/module.py")


def test_sample_excludes_match_expected_paths():
    globs = GlobSet(
        include=("**",),
        exclude=(
            ".venv/**",
            "**/__pycache__/**",
            "*.egg-info/**",
            "**/*.egg-info/**",
            "build/**",
            "scratch*",
        ),
    )

    assert not globs.matches(".venv/bin/python")
    assert not globs.matches("pkg/__pycache__/module.cpython-312.pyc")
    assert not globs.matches("pkg.egg-info/PKG-INFO")
    assert not globs.matches("pkg/subpkg.egg-info/PKG-INFO")
    assert not globs.matches("build/lib/module.py")
    assert not globs.matches("scratch-temp")
    assert globs.matches("pkg/module.py")


def test_root_star_does_not_match_nested_path():
    globs = GlobSet(include=("**",), exclude=("*.pyc",))

    assert not globs.matches("module.pyc")
    assert globs.matches("pkg/module.pyc")


def test_globstar_slash_zero_or_more_matches_root_and_nested():
    """Under ``zero_or_more``, ``**/*.py`` matches root-level and nested files."""
    globs = GlobSet(include=("**/*.py",), globstar="zero_or_more")

    assert globs.matches("module.py")
    assert globs.matches("pkg/module.py")
    assert globs.matches("pkg/sub/module.py")
    assert not globs.matches("module.pyc")


def test_globstar_slash_one_or_more_skips_root_files():
    """Under ``one_or_more`` (default), ``**/*.py`` does not match root files."""
    globs = GlobSet(include=("**/*.py",))

    assert not globs.matches("module.py")
    assert globs.matches("pkg/module.py")
    assert globs.matches("pkg/sub/module.py")


def test_globstar_exclude_zero_or_more_catches_root_files():
    """Under ``zero_or_more``, ``**/*.pyc`` excludes both root and nested."""
    globs = GlobSet(include=("**",), exclude=("**/*.pyc",), globstar="zero_or_more")

    assert not globs.matches("module.pyc")
    assert not globs.matches("pkg/module.pyc")
    assert globs.matches("module.py")


def test_globstar_exclude_one_or_more_keeps_root_files():
    """Under ``one_or_more`` (default), ``**/*.pyc`` skips root files."""
    globs = GlobSet(include=("**",), exclude=("**/*.pyc",))

    assert globs.matches("module.pyc")
    assert not globs.matches("pkg/module.pyc")


def test_prefix_globstar_zero_or_more_matches_flat_and_nested():
    """Under ``zero_or_more``, ``dir/**/*.ext`` matches direct and subdir files."""
    globs = GlobSet(include=("examples/**/*.py",), globstar="zero_or_more")

    assert globs.matches("examples/foo.py")
    assert globs.matches("examples/sub/foo.py")
    assert globs.matches("examples/a/b/foo.py")
    assert not globs.matches("examples/foo.txt")
    assert not globs.matches("other/foo.py")


def test_prefix_globstar_one_or_more_skips_direct_children():
    """Under ``one_or_more`` (default), ``dir/**/*.ext`` requires a subdir."""
    globs = GlobSet(include=("examples/**/*.py",))

    assert not globs.matches("examples/foo.py")
    assert globs.matches("examples/sub/foo.py")
    assert globs.matches("examples/a/b/foo.py")


def test_mid_path_globstar_zero_or_more_matches_zero_segments():
    """Under ``zero_or_more``, ``a/**/b.py`` matches both flat and nested."""
    globs = GlobSet(include=("a/**/b.py",), globstar="zero_or_more")

    assert globs.matches("a/b.py")
    assert globs.matches("a/x/b.py")
    assert globs.matches("a/x/y/b.py")
    assert not globs.matches("b.py")


def test_mid_path_globstar_one_or_more_requires_segment():
    """Under ``one_or_more`` (default), ``a/**/b.py`` requires a middle segment."""
    globs = GlobSet(include=("a/**/b.py",))

    assert not globs.matches("a/b.py")
    assert globs.matches("a/x/b.py")
    assert globs.matches("a/x/y/b.py")


def test_paths_are_normalized_to_forward_slashes():
    globs = GlobSet(include=("pkg/**",), exclude=("pkg/__pycache__/**",))

    assert globs.matches("pkg\\module.py")
    assert not globs.matches("pkg\\__pycache__\\module.pyc")


def test_brace_alternation_matches_style_choices():
    globs = GlobSet(include=("src/{main,test}.py",), exclude=())

    assert globs.matches("src/main.py")
    assert globs.matches("src/test.py")
    assert not globs.matches("src/other.py")


def test_brace_alternation_policy_is_configurable():
    globs = GlobSet(include=("src/{main}.py",), min_brace_choices=1)

    assert globs.matches("src/main.py")


def test_character_class_matches_style_choices():
    globs = GlobSet(include=("src/[ab].py",), exclude=())

    assert globs.matches("src/a.py")
    assert globs.matches("src/b.py")
    assert not globs.matches("src/c.py")


@pytest.mark.parametrize(
    "pattern",
    [
        "",
        "/absolute/**",
        "../outside/**",
        "src/[ab.py",
        "src/{main,test.py",
    ],
)
def test_rejects_unsupported_or_unsafe_patterns(pattern: str):
    with pytest.raises(GlobError):
        GlobSet(include=(pattern,))
