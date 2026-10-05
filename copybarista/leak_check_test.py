"""Tests for transformed-tree leak checks."""

from __future__ import annotations

from typing import TYPE_CHECKING

import re

import pytest

from copybarista import leak_check
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
    _entry_segments,
    _list_tree,
    _located,
    check_leaks,
    enforce_leak_check,
)


if TYPE_CHECKING:
    from pathlib import Path

    from copybarista.globs import Globstar
    from copybarista.lib.files.grep import Query


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


def test_entry_segments_sort_path_components() -> None:
    assert _entry_segments(("a/b/c.txt", True)) == ["a", "b", "c.txt"]


def test_list_tree_sorts_by_path_segments(tmp_path: Path) -> None:
    _write(tmp_path, "a-b/c.txt", "a/b.txt")

    assert _list_tree(tmp_path).paths == (
        "a",
        "a/b.txt",
        "a-b",
        "a-b/c.txt",
    )


def test_list_tree_does_not_follow_symlink_directories(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    (target / "secret.md").write_text("secret\n", encoding="utf-8")
    (tmp_path / "link").symlink_to(target, target_is_directory=True)

    assert _list_tree(tmp_path).paths == ("link", "target", "target/secret.md")


def test_list_tree_does_not_count_symlink_files_as_regular(tmp_path: Path) -> None:
    target = tmp_path / "target.md"
    target.write_text("secret\n", encoding="utf-8")
    (tmp_path / "link.md").symlink_to(target)

    assert _list_tree(tmp_path).regular_files == ("target.md",)


def test_excluded_path_honours_zero_or_more_globstar(tmp_path: Path) -> None:
    _write(tmp_path, "secret.py")
    files = FileSelection(include=("**",), exclude=("**/secret.py",))

    violations = check_leaks(
        root=tmp_path,
        policy=LeakCheck(),
        files=files,
        globstar="zero_or_more",
    )

    assert [violation.path for violation in violations] == ["secret.py"]
    assert (
        check_leaks(
            root=tmp_path,
            policy=LeakCheck(),
            files=FileSelection(include=("**",), exclude=("**/secret.py",)),
        )
        == ()
    )


def test_forbidden_path_uses_default_one_or_more_globstar(tmp_path: Path) -> None:
    _write(tmp_path, "secret.py")
    policy = LeakCheck(
        forbidden_path=(ForbiddenPathRule(id="secret", paths=("**/secret.py",)),),
    )

    assert (
        check_leaks(
            root=tmp_path,
            policy=policy,
            files=FileSelection(include=("**",), exclude=()),
        )
        == ()
    )


def test_forbidden_path_honours_zero_or_more_globstar(tmp_path: Path) -> None:
    _write(tmp_path, "secret.py")
    policy = LeakCheck(
        forbidden_path=(ForbiddenPathRule(id="secret", paths=("**/secret.py",)),),
    )

    violations = check_leaks(
        root=tmp_path,
        policy=policy,
        files=FileSelection(include=("**",), exclude=()),
        globstar="zero_or_more",
    )

    assert [violation.path for violation in violations] == ["secret.py"]


def test_located_handles_colons_in_paths_and_matching_text() -> None:
    names = {"a:12:b.md": "a:12:b.md"}

    assert _located("a:12:b.md:2: secret:payload", names=names) == (
        "a:12:b.md",
        2,
    )
    assert _located("a::3: secret", names={"a:": "a:"}) == ("a:", 3)
    assert _located(
        "a:2:3: secret",
        names={"a": "a", "a:2:3": "a:2:3"},
    ) == ("a", 2)


def test_forbidden_text_requests_content_rows(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: list[Query] = []

    def fake_grep(paths: object, query: Query) -> list[str]:
        del paths
        captured.append(query)
        return []

    monkeypatch.setattr(leak_check, "grep", fake_grep)
    _text_violations(
        tmp_path,
        ForbiddenTextRule(id="s", pattern="secret"),
    )

    assert captured[0].output_mode == "content"


def test_check_leaks_missing_root_reports_exact_error(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    policy = LeakCheck(
        forbidden_path=(ForbiddenPathRule(id="secret", paths=("**",)),),
    )

    with pytest.raises(
        LeakCheckError,
        match=re.escape(f"Leak check root does not exist: {missing}"),
    ):
        check_leaks(
            root=missing,
            policy=policy,
            files=FileSelection(include=("**",), exclude=()),
        )


def test_enforce_leak_check_forwards_transforms(tmp_path: Path) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "secret.py").write_text("x\n", encoding="utf-8")
    config = parse_config(
        {
            "workflow": {"name": "demo", "mode": "squash", "source_root": "src"},
            "files": {"include": ["**"], "exclude": ["secret.py"]},
            "transform": [
                {
                    "id": "m",
                    "type": "move",
                    "path": "secret.py",
                    "destination": "pkg/secret.py",
                },
            ],
        },
    )

    with pytest.raises(
        LeakCheckError,
        match=re.escape("excluded-path: pkg/secret.py"),
    ):
        enforce_leak_check(root=tmp_path, config=config)


def test_enforce_leak_check_forwards_globstar(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("x\n", encoding="utf-8")
    config = parse_config(
        {
            "workflow": {"name": "demo", "mode": "squash", "source_root": "src"},
            "files": {"include": ["**"]},
            "leak_check": {
                "forbidden_path": [{"id": "text", "paths": ["**/*.txt"]}],
            },
        },
    )

    assert enforce_leak_check(root=tmp_path, config=config) is None


def test_enforce_leak_check_forwards_zero_or_more_globstar(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("x\n", encoding="utf-8")
    config = parse_config(
        {
            "workflow": {
                "name": "demo",
                "mode": "squash",
                "source_root": "src",
                "globstar": "zero_or_more",
            },
            "files": {"include": ["**"]},
            "leak_check": {
                "forbidden_path": [{"id": "text", "paths": ["**/*.txt"]}],
            },
        },
    )

    with pytest.raises(LeakCheckError, match=re.escape("text: a.txt")):
        enforce_leak_check(root=tmp_path, config=config)


def test_enforce_leak_check_joins_multiple_violation_lines(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("x\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("x\n", encoding="utf-8")
    config = parse_config(
        {
            "workflow": {"name": "demo", "mode": "squash", "source_root": "src"},
            "files": {"include": ["**"]},
            "leak_check": {
                "forbidden_path": [{"id": "text", "paths": ["*.txt"]}],
            },
        },
    )

    with pytest.raises(LeakCheckError) as error:
        enforce_leak_check(root=tmp_path, config=config)
    assert str(error.value) == (
        "Leak check failed:\n"
        "text: a.txt: forbidden path was exported\n"
        "text: b.txt: forbidden path was exported"
    )


def _text_violations(
    root: Path,
    rule: ForbiddenTextRule,
    *,
    globstar: Globstar = "one_or_more",
) -> list[str]:
    return [
        violation.format()
        for violation in check_leaks(
            root=root,
            policy=LeakCheck(forbidden_text=(rule,)),
            files=FileSelection(include=("**",), exclude=()),
            globstar=globstar,
        )
    ]


def test_text_rule_reports_the_first_matching_line_of_each_file(tmp_path: Path):
    (tmp_path / "a.md").write_text("ok\nsecret one\nsecret two\n", encoding="utf-8")
    (tmp_path / "b.md").write_text("secret\n", encoding="utf-8")
    rule = ForbiddenTextRule(id="s", pattern="secret", paths=("*.md",))

    assert _text_violations(tmp_path, rule) == [
        "s: a.md:2: forbidden text matched",
        "s: b.md:1: forbidden text matched",
    ]


def test_text_rule_matches_across_lines_as_one_search_of_the_file(tmp_path: Path):
    (tmp_path / "a.md").write_text("ok\ntrax\n  issue 12345\n", encoding="utf-8")
    rule = ForbiddenTextRule(id="s", pattern=r"trax\s+issue", paths=("*.md",))

    assert _text_violations(tmp_path, rule) == ["s: a.md:2: forbidden text matched"]


def test_text_rule_scans_a_file_with_a_nul_byte_too(tmp_path: Path):
    (tmp_path / "a.md").write_bytes(b"ok\0\nsecret\n")
    rule = ForbiddenTextRule(id="s", pattern="secret", paths=("*.md",))

    assert _text_violations(tmp_path, rule) == ["s: a.md:2: forbidden text matched"]


def test_text_rule_honours_its_exclude_and_the_workflow_globstar(tmp_path: Path):
    _write(tmp_path, "top.md", "docs/deep.md", "docs/skip.md")
    for rel in ("top.md", "docs/deep.md", "docs/skip.md"):
        (tmp_path / rel).write_text("secret\n", encoding="utf-8")
    rule = ForbiddenTextRule(
        id="s",
        pattern="secret",
        paths=("**/*.md",),
        exclude=("docs/skip.md",),
    )

    assert _text_violations(tmp_path, rule) == [
        "s: docs/deep.md:1: forbidden text matched",
    ]
    assert _text_violations(tmp_path, rule, globstar="zero_or_more") == [
        "s: docs/deep.md:1: forbidden text matched",
        "s: top.md:1: forbidden text matched",
    ]


def test_text_rule_reads_a_path_holding_colons_and_digits(tmp_path: Path):
    (tmp_path / "a:12:b.md").write_text("ok\nsecret\n", encoding="utf-8")
    rule = ForbiddenTextRule(id="s", pattern="secret", paths=("*.md",))

    assert _text_violations(tmp_path, rule) == [
        "s: a:12:b.md:2: forbidden text matched",
    ]


def test_a_rule_ripgrep_cannot_run_fails_the_check_by_name(tmp_path: Path):
    (tmp_path / "a.md").write_text("x\n", encoding="utf-8")
    # Python ``re`` (the config validator) accepts ``\Z``; ripgrep refuses it.
    rule = ForbiddenTextRule(id="bad", pattern=r"x\Z", paths=("*.md",))

    with pytest.raises(
        LeakCheckError,
        match=r"^Leak check rule 'bad' could not run: ",
    ):
        _text_violations(tmp_path, rule)


def _write(root: Path, *rels: str) -> None:
    for rel in rels:
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text("x\n", encoding="utf-8")


if __name__ == "__main__":
    from copybarista.lib.testing.main import test_main

    test_main(__file__)
