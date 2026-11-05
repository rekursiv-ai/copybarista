"""Tests for glob include and exclude behavior."""

from __future__ import annotations

import re

import pytest

from copybarista.errors import GlobError
from copybarista.globs import (
    GlobSet,
    Globstar,
    _escape_char_class,
    validate_pattern,
)


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


def test_excludes_checks_only_exclude_patterns_ignoring_includes():
    """`excludes` tests the exclude set alone, disregarding the include set.

    An out-of-selection path (never subject to the includes) must still be
    testable against the exclude patterns -- e.g. an identity-mapped public path
    the config explicitly suppresses. `matches` cannot express this because it
    requires an include hit; `excludes` isolates the exclude decision.
    """
    globs = GlobSet(include=("pkg/**",), exclude=("typings/secret/**",))

    assert globs.excludes("typings/secret/key.pyi")
    assert not globs.excludes("typings/brotli/__init__.pyi")
    # Path not included AND not excluded: matches() is False, excludes() is False.
    assert not globs.matches("typings/brotli/__init__.pyi")


def test_brace_alternation_matches_style_choices():
    globs = GlobSet(include=("src/{main,test}.py",), exclude=())

    assert globs.matches("src/main.py")
    assert globs.matches("src/test.py")
    assert not globs.matches("src/other.py")


def test_brace_alternation_policy_is_configurable():
    globs = GlobSet(include=("src/{main}.py",), min_brace_choices=1)

    assert globs.matches("src/main.py")


def test_excludes_follow_the_same_brace_policy_as_includes():
    globs = GlobSet(include=("**",), exclude=("src/{main}.py",), min_brace_choices=1)

    assert globs.excludes("src/main.py")
    assert globs.matches("src/test.py")


def test_character_class_matches_style_choices():
    globs = GlobSet(include=("src/[ab].py",), exclude=())

    assert globs.matches("src/a.py")
    assert globs.matches("src/b.py")
    assert not globs.matches("src/c.py")


def test_negated_character_class_matches_every_other_character():
    globs = GlobSet(include=("src/[!a].py",))

    assert globs.matches("src/b.py")
    assert not globs.matches("src/a.py")


def test_character_class_range_spans_its_endpoints():
    globs = GlobSet(include=("src/[a-c].py", "edge/[-a].py"))

    assert globs.matches("src/b.py")
    assert not globs.matches("src/d.py")
    # A hyphen at either end of a class is literal, not a range.
    assert globs.matches("edge/-.py")
    assert not globs.matches("edge/b.py")


def test_question_mark_matches_one_character_within_a_segment():
    globs = GlobSet(include=("src/?.py",))

    assert globs.matches("src/a.py")
    assert not globs.matches("src/ab.py")
    assert not globs.matches("src//.py")


def test_backslash_makes_a_glob_character_literal():
    globs = GlobSet(include=("src/\\*.py", "src/\\[a.py"))

    assert globs.matches("src/*.py")
    assert globs.matches("src/[a.py")
    assert not globs.matches("src/a.py")


@pytest.mark.parametrize(
    "pattern",
    [
        "",
        "/absolute/**",
        "../outside/**",
        "src/[ab.py",
        "src/{main,test.py",
        "src/]a[.py",
        "src/[].py",
        "src/[!].py",
        "src/[a/b].py",
        "src/{main}.py",
        "src/{main,}.py",
    ],
)
def test_rejects_unsupported_or_unsafe_patterns(pattern: str):
    with pytest.raises(GlobError):
        GlobSet(include=(pattern,))


@pytest.mark.parametrize("path", ["/X/aX/", "\\X\\aX\\", "X/aX"])
def test_path_normalization_preserves_literal_edge_characters(path: str) -> None:
    included = GlobSet(include=("X/aX",))
    excluded = GlobSet(include=("**",), exclude=("X/aX",))

    assert included.matches(path)
    assert excluded.excludes(path)
    assert not excluded.matches(path)


@pytest.mark.parametrize("pattern", ["X/aX/", " X ", "a/"])
def test_validate_pattern_strips_only_trailing_slashes(pattern: str) -> None:
    assert validate_pattern(pattern) == pattern.rstrip("/")


@pytest.mark.parametrize(
    ("pattern", "matched", "missed"),
    [
        ("[azc]", "z", "X"),
        ("[a-]", "-", "b"),
        ("[ab][cd]", "ad", "acX"),
        ("[ab]X", "aX", "aY"),
        ("{a,b}{c,d}", "ad", "ax"),
        ("{a,b}X", "aX", "aY"),
        ("**X", "a/bX", "a/bY"),
        ("*X", "aX", "aY"),
        ("a\\*", "a*", "abc"),
    ],
)
def test_glob_tokens_keep_adjacent_literals(
    pattern: str,
    matched: str,
    missed: str,
) -> None:
    globs = GlobSet(include=(pattern,))
    assert globs.matches(matched)
    assert not globs.matches(missed)


def test_trailing_escape_is_literal_not_an_incomplete_escape() -> None:
    globs = GlobSet(include=("a\\",))

    assert validate_pattern("a\\") == "a\\"
    assert not globs.matches("a")


@pytest.mark.parametrize(
    ("content", "matched", "missed"),
    [
        ("-a", "-a", "bz"),
        ("a-", "-a", "bz"),
        ("a-c", "abc", "dz"),
        ("[]", "[]", "az"),
        ("\\", "\\", "az"),
        ("^", "^", "az"),
    ],
)
def test_character_class_escape_preserves_regex_language(
    content: str,
    matched: str,
    missed: str,
) -> None:
    pattern = re.compile(f"[{_escape_char_class(content)}]")

    assert all(pattern.fullmatch(char) for char in matched)
    assert all(pattern.fullmatch(char) is None for char in missed)


@pytest.mark.parametrize("pattern", ["[z-a]", "[9-0]"])
def test_character_classes_reject_reversed_ranges(pattern: str) -> None:
    with pytest.raises(re.error, match="bad character range"):
        GlobSet(include=(pattern,))


@pytest.mark.parametrize("globstar", ["one_or_more", "zero_or_more"])
def test_brace_choices_inherit_globstar_policy(globstar: Globstar) -> None:
    globs = GlobSet(
        include=("{**/a,plain}",),
        globstar=globstar,
    )

    assert globs.matches("x/a")
    assert globs.matches("a") == (globstar == "zero_or_more")
    assert globs.matches("plain")


@pytest.mark.parametrize(
    ("pattern", "message"),
    [
        ("", "Glob pattern must not be empty"),
        ("/a", "Glob pattern must be relative without '..': /a"),
        ("a/../b", "Glob pattern must be relative without '..': a/../b"),
        ("[", "Unbalanced glob syntax in pattern: ["),
        ("]", "Unbalanced glob syntax in pattern: ]"),
        ("[[a]", "Unbalanced glob syntax in pattern: [[a]"),
        ("\\*a[", "Unbalanced glob syntax in pattern: \\*a["),
        ("[]", "Empty character class in glob pattern: []"),
        ("[!]", "Invalid character class in glob pattern: [!]"),
        ("{a}", "Invalid brace alternation in glob pattern: {a}"),
        ("{,a}", "Invalid brace alternation in glob pattern: {,a}"),
    ],
)
def test_invalid_glob_diagnostics_identify_the_pattern(
    pattern: str,
    message: str,
) -> None:
    with pytest.raises(GlobError) as error:
        GlobSet(include=(pattern,))
    assert str(error.value) == message


if __name__ == "__main__":
    from copybarista.lib.testing.runner import test_main

    test_main(__file__)
