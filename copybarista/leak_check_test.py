"""Tests for transformed-tree leak checks."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from copybarista.config import (
    FileMove,
    FileSelection,
    ForbiddenPathRule,
    ForbiddenTextRule,
    LeakCheck,
    Transform,
    parse_config,
)
from copybarista.errors import LeakCheckError
from copybarista.leak_check import (
    check_leaks,
    enforce_leak_check,
)


if TYPE_CHECKING:
    from pathlib import Path


def test_check_leaks_reports_forbidden_paths_and_text(tmp_path: Path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "module.py").write_text(
        "from internal_pkg.lib import json\n",
        encoding="utf-8",
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
        files=FileSelection(include=("**",), exclude=()),
    )

    assert [violation.format() for violation in violations] == [
        "private-paths: private/notes.md: forbidden path was exported",
        "loop-imports: pkg/module.py:1: forbidden text matched",
    ]


def test_enforce_leak_check_does_not_echo_matched_text(tmp_path: Path):
    (tmp_path / "module.py").write_text("token = 'SECRET-123'\n", encoding="utf-8")
    config = parse_config(
        {
            "workflow": {"name": "demo", "mode": "squash", "source_root": "src"},
            "files": {"include": ["**"]},
            "leak_check": {
                "forbidden_text": [
                    {"id": "token", "pattern": r"SECRET-\d+", "paths": ["**"]},
                ],
            },
        },
    )

    with pytest.raises(LeakCheckError) as exc:
        enforce_leak_check(root=tmp_path, config=config)

    assert "SECRET-123" not in str(exc.value)
    assert "token: module.py:1" in str(exc.value)


def test_empty_leak_check_policy_does_not_walk_root(tmp_path: Path):
    missing = tmp_path / "missing"

    assert (
        check_leaks(
            root=missing,
            policy=LeakCheck(),
            files=FileSelection(include=("**",), exclude=()),
        )
        == ()
    )


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
        files=FileSelection(include=("**",), exclude=()),
    )

    assert [violation.format() for violation in violations] == [
        "private-marker: README.md:1: forbidden text matched",
    ]


def test_excluded_path_is_forbidden_after_moves(tmp_path: Path):
    """An exported path whose move-inverse is excluded is a leak.

    ``files.exclude`` only filters the source sweep; a ``[[files.copy]]`` can put
    the same file back. A path no move placed is a copy destination outside the
    exclude's source-root space, so ``secret/root_copy.py`` is not judged.
    """
    _write(
        tmp_path,
        "pkg/secret/a.py",
        "pkg/ok.py",
        "docs/internal.md",
        "docs/guide.md",
        "secret/root_copy.py",
    )
    files = FileSelection(
        include=("**",),
        exclude=("secret/**", "docs/internal.md"),
        moves=(
            FileMove(path="", destination="pkg"),
            FileMove(path="pkg/docs", destination="docs"),
        ),
    )

    violations = check_leaks(root=tmp_path, policy=LeakCheck(), files=files)

    assert [violation.format() for violation in violations] == [
        "excluded-path: docs/internal.md: files.exclude path was exported",
        "excluded-path: pkg/secret/a.py: files.exclude path was exported",
    ]


def test_excluded_path_without_moves_is_checked_in_place(tmp_path: Path):
    _write(tmp_path, "v1/old.py", "new.py")
    files = FileSelection(include=("**",), exclude=("v1/**",))

    violations = check_leaks(root=tmp_path, policy=LeakCheck(), files=files)

    assert [violation.path for violation in violations] == ["v1/old.py"]


def test_excluded_path_undoes_move_transforms(tmp_path: Path):
    """A ``move`` transform relocates a staged file before the leak check runs."""
    _write(tmp_path, "pyproject.toml")
    files = FileSelection(include=("**",), exclude=("pyproject.public.toml",))
    transforms = (
        Transform(
            id="m",
            type="move",
            path="pyproject.public.toml",
            destination="pyproject.toml",
        ),
    )

    violations = check_leaks(
        root=tmp_path,
        policy=LeakCheck(),
        files=files,
        transforms=transforms,
    )

    assert [violation.path for violation in violations] == ["pyproject.toml"]


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
            files=FileSelection(include=("**",), exclude=()),
        )
        == ()
    )


def _write(root: Path, *rels: str) -> None:
    for rel in rels:
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text("x\n", encoding="utf-8")


if __name__ == "__main__":
    from copybarista.lib.testing.main import test_main

    test_main(__file__)
