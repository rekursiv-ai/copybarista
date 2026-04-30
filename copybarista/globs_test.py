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


def test_switchboard_excludes_match_expected_paths():
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


def test_nested_star_requires_at_least_one_directory():
    globs = GlobSet(include=("**",), exclude=("**/*.pyc",))

    assert globs.matches("module.pyc")
    assert not globs.matches("pkg/module.pyc")


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
