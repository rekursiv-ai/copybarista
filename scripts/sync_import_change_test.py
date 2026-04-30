"""Tests for public-to-source sync helpers."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from scripts import sync_import_change
from scripts.sync_import_change import (
    _commit_author,
    _gh_pr_exists,
    _string_bool,
    import_branch_name,
    import_change_pr_body,
)

if TYPE_CHECKING:
    import pytest


def test_import_change_pr_body_contains_review_context():
    body = import_change_pr_body(
        public_repo="rekursiv-ai/copybarista",
        public_sha="abcdef1234567890",
        public_base_ref="base",
        public_head_ref="head",
        source_base_ref="source",
    )

    assert "Imports Copybarista public repository changes into the source" in body
    assert "- Public repository: `rekursiv-ai/copybarista`" in body
    assert "- Public SHA: `abcdef1234567890`" in body
    assert "- Public base: `base`" in body
    assert "- Public head: `head`" in body
    assert "- Source base: `source`" in body
    assert "`copybarista import-change`" in body
    assert "Regenerate this PR before merging if source `main` changes." in body


def test_string_bool_accepts_action_boolean_values():
    assert _string_bool("true")
    assert _string_bool("1")
    assert _string_bool("yes")
    assert not _string_bool("false")
    assert not _string_bool("")


def test_import_branch_name_uses_public_sha():
    assert (
        import_branch_name(explicit="", public_sha="abcdef1234567890")
        == "copybarista/import/sha-abcdef123456"
    )


def test_import_branch_name_allows_explicit_branch():
    assert (
        import_branch_name(
            explicit="copybarista/import/custom",
            public_sha="abcdef1234567890",
        )
        == "copybarista/import/custom"
    )


def test_commit_author_uses_sync_identity():
    assert (
        _commit_author("copybarista", "copybarista@rekursiv.ai")
        == "copybarista <copybarista@rekursiv.ai>"
    )


def test_gh_pr_exists_only_counts_open_prs(monkeypatch: pytest.MonkeyPatch):
    def fake_run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        assert argv[0:4] == ["gh", "pr", "list", "--repo"]
        assert "--state" in argv
        assert "open" in argv
        return subprocess.CompletedProcess(argv, 0, stdout="[]")

    monkeypatch.setattr(sync_import_change, "_run", fake_run)

    assert not _gh_pr_exists(
        branch="copybarista/import/sha-abcdef123456",
        repo="rekursiv-ai/source",
        cwd=Path.cwd(),
    )
