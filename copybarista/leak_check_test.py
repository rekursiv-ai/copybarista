"""Tests for transformed-tree leak checks."""

from __future__ import annotations

from pathlib import Path

import pytest

from copybarista.config import ForbiddenPathRule, ForbiddenTextRule, LeakCheck
from copybarista.errors import LeakCheckError
from copybarista.leak_check import check_leaks, enforce_leak_check


def test_check_leaks_reports_forbidden_paths_and_text(tmp_path: Path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "module.py").write_text(
        "from internal_pkg.lib import json\n", encoding="utf-8"
    )
    (tmp_path / "private").mkdir()
    (tmp_path / "private" / "notes.md").write_text("secret\n", encoding="utf-8")

    violations = check_leaks(
        root=tmp_path,
        policy=LeakCheck(
            forbidden_path=(
                ForbiddenPathRule(
                    id="private-paths",
                    paths=("private/**",),
                ),
            ),
            forbidden_text=(
                ForbiddenTextRule(
                    id="loop-imports",
                    pattern=r"\binternal_pkg\.",
                    paths=("**/*.py",),
                ),
            ),
        ),
    )

    assert [violation.format() for violation in violations] == [
        "private-paths: private/notes.md: forbidden path was exported",
        "loop-imports: pkg/module.py:1: forbidden text matched",
    ]


def test_enforce_leak_check_does_not_echo_matched_text(tmp_path: Path):
    (tmp_path / "module.py").write_text("token = 'SECRET-123'\n", encoding="utf-8")

    with pytest.raises(LeakCheckError) as exc:
        enforce_leak_check(
            root=tmp_path,
            policy=LeakCheck(
                forbidden_text=(
                    ForbiddenTextRule(
                        id="token",
                        pattern=r"SECRET-\d+",
                        paths=("**",),
                    ),
                ),
            ),
        )

    assert "SECRET-123" not in str(exc.value)
    assert "token: module.py:1" in str(exc.value)


def test_text_leak_check_includes_root_files_when_policy_does(tmp_path: Path):
    (tmp_path / "README.md").write_text("contains private_marker\n", encoding="utf-8")

    violations = check_leaks(
        root=tmp_path,
        policy=LeakCheck(
            forbidden_text=(
                ForbiddenTextRule(
                    id="private-marker",
                    pattern="private_marker",
                    paths=("*.md", "**/*.md"),
                ),
            ),
        ),
    )

    assert [violation.format() for violation in violations] == [
        "private-marker: README.md:1: forbidden text matched",
    ]


def test_text_leak_check_skips_symlink_contents(tmp_path: Path):
    target = tmp_path / "target.txt"
    target.write_text("private\n", encoding="utf-8")
    target.unlink()
    (tmp_path / "link.txt").symlink_to(target)

    assert (
        check_leaks(
            root=tmp_path,
            policy=LeakCheck(
                forbidden_text=(
                    ForbiddenTextRule(
                        id="private",
                        pattern="private",
                    ),
                ),
            ),
        )
        == ()
    )
