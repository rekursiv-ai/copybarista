"""Tests for source-to-public sync helpers."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import subprocess

import pytest

from scripts import sync_export_pr
from scripts.sync_export_pr import (
    ExportRequest,
    PrBodyEntry,
    PrMetadataPatch,
    PrReplayState,
    SourceAuthor,
    _applied_marker_from_pr_body,
    _commit_author,
    _export_commit_message,
    _export_public_tree,
    _generated_commit_author,
    _gh_pr_exists,
    _open_or_update_export_pr,
    _parse_pr_metadata_log,
    _public_pr_text,
    _remove_public_validation_artifacts,
    _render_pr_body,
    _replace_tree,
    _source_rev_digest,
    _state_from_pr_body,
    _validate_public,
    _validate_source,
    export_branch_name,
    export_pr_body,
    export_pr_text,
    replay_pr_metadata,
)


def _metadata_log(
    message: str,
    *,
    commit_sha: str = "abcdef123456",
    author: str = "Commit Author",
    email: str = "author@example.com",
) -> str:
    """Return one NUL-framed metadata log record."""
    return f"{commit_sha}\0{author}\0{email}\0{message}\0"


def _export_request(tmp_path: Path) -> ExportRequest:
    return ExportRequest(
        source_dir=tmp_path,
        project_path=Path(),
        public_dir=tmp_path,
        target_repo="example/public",
        base_branch="main",
        source_sha="abc123",
        branch="copybarista/export/main",
        sync_label="Copybarista",
        sync_user_name="copybarista",
        sync_user_email="copybarista@example.com",
        pr_title="Public title",
        pr_body="Public body.",
        manual_pr_title="",
        manual_pr_body="",
        replay_settings=sync_export_pr.PrReplaySettings(
            scope="copybarista",
            default_title="Public title",
            default_body="Public body.",
            require_metadata=False,
            bootstrap_base="",
            publish_source_rev=False,
        ),
        forbidden_pr_text=(),
        auto_merge=False,
        refresh_public_lockfile=False,
        skip_source_validation=False,
        runner_temp=tmp_path,
        release_check_script=None,
        type_check_targets=(".",),
        smoke_import="",
    )


def _pr_state() -> PrReplayState:
    return PrReplayState(
        title="Public title",
        authors=(),
        body_intro="Public body.",
        body_entries=(),
        applied_source_rev="abc123",
        applied_source_digest=_source_rev_digest("abc123"),
        metadata_count=0,
    )


def test_main_accepts_generic_project_validation_args(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[ExportRequest] = []

    def fake_run_export_sync(request: ExportRequest) -> None:
        captured.append(request)

    monkeypatch.setattr(sync_export_pr, "run_export_sync", fake_run_export_sync)

    sync_export_pr.main(
        [
            "--project-path",
            "packages/configgle",
            "--release-check-script",
            "scripts/check_release_tree.py",
            "--type-check-target",
            "configgle",
            "--type-check-target",
            "tests",
            "--smoke-import",
            "configgle",
        ]
    )

    assert captured[0].project_path == Path("packages/configgle")
    assert captured[0].release_check_script == Path("scripts/check_release_tree.py")
    assert captured[0].type_check_targets == ("configgle", "tests")
    assert captured[0].smoke_import == "configgle"


def test_main_accepts_skip_source_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[ExportRequest] = []

    def fake_run_export_sync(request: ExportRequest) -> None:
        captured.append(request)

    monkeypatch.setattr(sync_export_pr, "run_export_sync", fake_run_export_sync)

    sync_export_pr.main(
        [
            "--project-path",
            "packages/example",
            "--skip-source-validation",
        ]
    )

    assert captured[0].skip_source_validation


def test_export_pr_body_contains_review_context():
    body = export_pr_body(
        description="Publish the latest sync changes.",
        branch="copybarista/export/main",
        sync_label="Copybarista",
    )

    assert "Publish the latest sync changes." in body
    assert "Copybarista export branch: `copybarista/export/main`" in body
    assert "Source commit" not in body
    assert "Exported files" not in body
    assert "Workflow:" not in body
    assert "Do not push manual commits to this generated branch." in body


def test_export_pr_body_accepts_custom_sync_label():
    body = export_pr_body(
        description="Publish the latest sync changes.",
        branch="configgle/export/main",
        sync_label="Configgle",
    )

    assert "Configgle export branch: `configgle/export/main`" in body


def test_parse_pr_metadata_accepts_append_body():
    patches = _parse_pr_metadata_log(
        _metadata_log(
            "Prepare public export\n\n"
            "Copybarista-PR-Title: Public title\n"
            "Copybarista-PR-Body-Mode: append\n"
            "Copybarista-PR-Body:\n"
            "Public body text.\n"
        ),
        forbidden_text=("private",),
    )

    assert patches == (
        PrMetadataPatch(
            commit_sha="abcdef123456",
            scope="",
            title="Public title",
            author=SourceAuthor(name="Commit Author", email="author@example.com"),
            body="Public body text.",
            body_mode="append",
        ),
    )


def test_parse_pr_metadata_ignores_plain_commit_message():
    patches = _parse_pr_metadata_log(
        _metadata_log("Better tool use UX\n\nInternal source-only context."),
        forbidden_text=("source-only",),
    )

    assert patches == ()


def test_parse_pr_metadata_uses_commit_author_by_default():
    patches = _parse_pr_metadata_log(
        _metadata_log(
            "Prepare public export\n\n"
            "Copybarista-PR-Title: Public title\n"
            "Copybarista-PR-Body:\n"
            "Public body text.\n",
            author="Source Committer",
            email="source@example.com",
        ),
        forbidden_text=(),
    )

    assert patches[0].author == SourceAuthor(
        name="Source Committer",
        email="source@example.com",
    )


def test_source_pr_metadata_uses_nul_terminated_git_log(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[list[str]] = []

    def fake_run(
        argv: list[str],
        **_: object,
    ) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=(
                "abcdef123456\0"
                "Source Committer\0"
                "source@example.com\0"
                "Copybarista-PR-Title: Public title\n"
                "Copybarista-PR-Body:\n"
                "Public body.\n"
                "\0"
            ),
        )

    monkeypatch.setattr(sync_export_pr, "_run", fake_run)

    patches = sync_export_pr._source_pr_metadata(
        source_dir=tmp_path,
        replay_base="",
        current_source_rev="abcdef123456",
        forbidden_text=(),
        scope="",
    )

    assert calls == [
        [
            "git",
            "log",
            "-z",
            "--reverse",
            "--format=%H%x00%aN%x00%aE%x00%B",
            "abcdef123456",
        ]
    ]
    assert patches[0].title == "Public title"
    assert patches[0].author == SourceAuthor(
        name="Source Committer",
        email="source@example.com",
    )


def test_parse_pr_metadata_rejects_legacy_author_field():
    with pytest.raises(sync_export_pr.PrMetadataError, match="use the git author"):
        _parse_pr_metadata_log(
            _metadata_log(
                "Copybarista-PR-Title: Public title\n"
                "Copybarista-PR-Author: Public author\n"
            ),
            forbidden_text=(),
        )


def test_parse_pr_metadata_rejects_duplicate_title():
    with pytest.raises(sync_export_pr.PrMetadataError, match=r"abcdef1.*Title"):
        _parse_pr_metadata_log(
            _metadata_log(
                "Copybarista-PR-Title: First\nCopybarista-PR-Title: Second\n"
            ),
            forbidden_text=(),
        )


def test_parse_pr_metadata_keeps_matching_scoped_block():
    patches = _parse_pr_metadata_log(
        _metadata_log(
            "Update exported packages\n\n"
            "Copybarista-PR-Scope: sagent\n"
            "Copybarista-PR-Title: Sagent title\n"
            "Copybarista-PR-Body:\n"
            "Sagent body.\n"
            "Copybarista-PR-Scope: configgle\n"
            "Copybarista-PR-Title: Configgle title\n"
            "Copybarista-PR-Body:\n"
            "Configgle body.\n"
        ),
        forbidden_text=(),
        scope="configgle",
    )

    assert patches == (
        PrMetadataPatch(
            commit_sha="abcdef123456",
            scope="configgle",
            title="Configgle title",
            author=SourceAuthor(name="Commit Author", email="author@example.com"),
            body="Configgle body.",
            body_mode="append",
        ),
    )


def test_parse_pr_metadata_keeps_unscoped_and_matching_scoped_blocks():
    patches = _parse_pr_metadata_log(
        _metadata_log(
            "Update exported packages\n\n"
            "Copybarista-PR-Body:\n"
            "Shared body.\n"
            "Copybarista-PR-Scope: sagent\n"
            "Copybarista-PR-Body:\n"
            "Sagent body.\n"
        ),
        forbidden_text=(),
        scope="sagent",
    )

    assert tuple(patch.body for patch in patches) == ("Shared body.", "Sagent body.")


def test_parse_pr_metadata_rejects_body_mode_without_body():
    with pytest.raises(sync_export_pr.PrMetadataError, match=r"abcdef1.*Body-Mode"):
        _parse_pr_metadata_log(
            _metadata_log("Copybarista-PR-Body-Mode: append\n"),
            forbidden_text=(),
        )


def test_parse_pr_metadata_rejects_forbidden_text():
    with pytest.raises(sync_export_pr.PrMetadataError, match=r"abcdef1.*Body"):
        _parse_pr_metadata_log(
            _metadata_log("Copybarista-PR-Body:\nMentions private-source.\n"),
            forbidden_text=("private-source",),
        )


def test_replay_append_body_adds_entries_in_commit_order():
    state = replay_pr_metadata(
        base=PrReplayState(
            title="Default title",
            authors=(),
            body_intro="Default body.",
            body_entries=(),
            applied_source_rev="",
            applied_source_digest="",
            metadata_count=0,
        ),
        patches=(
            PrMetadataPatch(
                commit_sha="1111111",
                scope="",
                title="",
                author=SourceAuthor(name="First Author", email="first@example.com"),
                body="First update.",
                body_mode="append",
            ),
            PrMetadataPatch(
                commit_sha="2222222",
                scope="",
                title="",
                author=SourceAuthor(name="Second Author", email="second@example.com"),
                body="Second update.",
                body_mode="append",
            ),
        ),
    )

    assert state.body_entries == (
        PrBodyEntry(commit_sha="1111111", text="First update."),
        PrBodyEntry(commit_sha="2222222", text="Second update."),
    )
    assert state.authors == (
        SourceAuthor(name="First Author", email="first@example.com"),
        SourceAuthor(name="Second Author", email="second@example.com"),
    )


def test_replay_replace_body_resets_previous_append_entries():
    state = replay_pr_metadata(
        base=PrReplayState(
            title="Default title",
            authors=(),
            body_intro="Default body.",
            body_entries=(PrBodyEntry(commit_sha="1111111", text="Old update."),),
            applied_source_rev="",
            applied_source_digest="",
            metadata_count=0,
        ),
        patches=(
            PrMetadataPatch(
                commit_sha="2222222",
                scope="",
                title="New title",
                author=SourceAuthor(name="Commit Author", email="author@example.com"),
                body="Replacement body.",
                body_mode="replace",
            ),
        ),
    )

    assert state.title == "New title"
    assert state.authors == (
        SourceAuthor(name="Commit Author", email="author@example.com"),
    )
    assert state.body_intro == "Replacement body."
    assert state.body_entries == ()


def test_render_pr_body_includes_state_marker_without_raw_source_sha():
    source_rev = "abc123abc123"
    body = _render_pr_body(
        state=PrReplayState(
            title="Public title",
            authors=(SourceAuthor(name="Commit Author", email="author@example.com"),),
            body_intro="Public summary.",
            body_entries=(PrBodyEntry(commit_sha=source_rev, text="Public update."),),
            applied_source_rev=source_rev,
            applied_source_digest=_source_rev_digest(source_rev),
            metadata_count=1,
        ),
        branch="copybarista/export/main",
        sync_label="Copybarista",
        publish_source_rev=False,
    )

    assert "Source attribution:" not in body
    assert "### Updates" in body
    assert "Public update." in body
    assert "copybarista:pr-state version=1 applied=sha256:" in body
    assert source_rev not in body


def test_render_pr_body_fills_repository_template():
    source_rev = "abc123abc123"
    body = _render_pr_body(
        state=PrReplayState(
            title="Public title",
            authors=(SourceAuthor(name="Commit Author", email="author@example.com"),),
            body_intro="Public summary.",
            body_entries=(PrBodyEntry(commit_sha=source_rev, text="Public update."),),
            applied_source_rev=source_rev,
            applied_source_digest=_source_rev_digest(source_rev),
            metadata_count=1,
        ),
        branch="copybarista/export/main",
        sync_label="Copybarista",
        publish_source_rev=False,
        pr_template=(
            "## Summary\n\n"
            "- Describe the change.\n\n"
            "## Validation\n\n"
            "- [ ] `ruff check`\n"
            "- [ ] `pytest`\n\n"
            "## Checklist\n\n"
            "- [ ] I updated docs.\n\n"
            "## Notes\n\n"
            "Mention documentation impact.\n"
        ),
    )

    assert "## Summary\n\nPublic summary." in body
    assert "- Describe the change." not in body
    assert "Public update.\n\n## Validation" in body
    assert "- [x] `ruff check`" in body
    assert "- [x] `pytest`" in body
    assert "## Checklist" not in body
    assert "I updated docs." not in body
    assert "## Notes\n\nMention documentation impact." in body
    assert "Copybarista export branch: `copybarista/export/main`" in body


def test_state_from_pr_body_reads_templated_summary():
    source_rev = "abc123abc123"
    body = _render_pr_body(
        state=PrReplayState(
            title="Public title",
            authors=(SourceAuthor(name="Commit Author", email="author@example.com"),),
            body_intro="Public summary.",
            body_entries=(PrBodyEntry(commit_sha=source_rev, text="Public update."),),
            applied_source_rev=source_rev,
            applied_source_digest=_source_rev_digest(source_rev),
            metadata_count=1,
        ),
        branch="copybarista/export/main",
        sync_label="Copybarista",
        publish_source_rev=False,
        pr_template=(
            "## Summary\n\n"
            "## Testing\n\n"
            "- [ ] `uv run pytest`\n\n"
            "## Checklist\n\n"
            "- [ ] I updated docs.\n"
        ),
    )

    state = _state_from_pr_body(
        title="Public title",
        body=body,
        default_body="Default body.",
    )

    assert state.body_intro == "Public summary."
    assert state.authors == ()
    assert state.body_entries[0].text == "Public update."


def test_render_pr_body_round_trips_multiline_append_entries():
    source_rev = "abc123abc123"
    body = _render_pr_body(
        state=PrReplayState(
            title="Public title",
            authors=(),
            body_intro="Public summary.",
            body_entries=(
                PrBodyEntry(
                    commit_sha=source_rev,
                    text="First line.\nSecond line.",
                ),
            ),
            applied_source_rev=source_rev,
            applied_source_digest=_source_rev_digest(source_rev),
            metadata_count=1,
        ),
        branch="copybarista/export/main",
        sync_label="Copybarista",
        publish_source_rev=False,
    )

    state = _state_from_pr_body(
        title="Public title",
        body=body,
        default_body="Default body.",
    )

    assert state.body_entries[0].text == "First line.\nSecond line."


def test_applied_marker_rejects_multiple_state_markers():
    body = (
        "Body\n"
        "<!-- copybarista:pr-state version=1 applied=sha256:first -->\n"
        "<!-- copybarista:pr-state version=1 applied=sha256:second -->\n"
    )

    with pytest.raises(sync_export_pr.PrReplayError, match="multiple"):
        _applied_marker_from_pr_body(body)


def test_applied_marker_rejects_unsupported_version():
    body = "<!-- copybarista:pr-state version=2 applied=sha256:first -->\n"

    with pytest.raises(sync_export_pr.PrReplayError, match="unsupported"):
        _applied_marker_from_pr_body(body)


def test_unmarked_existing_branch_replays_current_commit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def fake_source_parent(*, source_dir: Path, rev: str) -> str:
        assert source_dir == tmp_path
        assert rev == "source-head"
        return "source-parent"

    monkeypatch.setattr(sync_export_pr, "_source_parent", fake_source_parent)

    assert (
        sync_export_pr._replay_base(
            request=_export_request(tmp_path),
            current_source_rev="source-head",
            current_pr=None,
            branch_markers=sync_export_pr.BranchMarkers(
                source_digest="",
                replay_base_digest="",
                exists=True,
            ),
        )
        == "source-parent"
    )


def test_marked_existing_branch_replays_after_applied_source(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[str] = []

    def fake_resolve_source_marker(
        *, source_dir: Path, marker: str, marker_source: str
    ) -> str:
        assert source_dir == tmp_path
        calls.append(f"{marker_source}:{marker}")
        return "applied-source"

    monkeypatch.setattr(
        sync_export_pr,
        "_resolve_source_marker",
        fake_resolve_source_marker,
    )

    assert (
        sync_export_pr._replay_base(
            request=_export_request(tmp_path),
            current_source_rev="source-head",
            current_pr=None,
            branch_markers=sync_export_pr.BranchMarkers(
                source_digest="applied-digest",
                replay_base_digest="migration-base-digest",
                exists=True,
            ),
        )
        == "applied-source"
    )
    assert calls == ["generated branch source marker:sha256:applied-digest"]


def test_unmarked_existing_pr_still_requires_bootstrap(tmp_path: Path) -> None:
    with pytest.raises(sync_export_pr.PrReplayError, match="Existing generated PR"):
        sync_export_pr._replay_base(
            request=_export_request(tmp_path),
            current_source_rev="source-head",
            current_pr=sync_export_pr.CurrentPr(
                title="Old title",
                body="Old body.",
                number=7,
                url="https://example.test/pr/7",
            ),
            branch_markers=sync_export_pr.BranchMarkers(
                source_digest="",
                replay_base_digest="",
                exists=True,
            ),
        )


def test_export_commit_message_contains_replay_markers():
    message = _export_commit_message(
        title="Public title",
        sync_label="Copybarista",
        branch="copybarista/export/main",
        source_digest="source-digest",
        replay_base_digest="base-digest",
        authors=(
            SourceAuthor(name="Primary Author", email="primary@example.com"),
            SourceAuthor(name="Second Author", email="second@example.com"),
        ),
    )

    assert message.startswith("Public title\n\n")
    assert "Copybarista export branch: copybarista/export/main" in message
    assert "copybarista-source-rev-sha256=source-digest" in message
    assert "copybarista-replay-base-sha256=base-digest" in message
    assert "Co-authored-by: Second Author <second@example.com>" in message


def test_no_diff_updates_existing_pr_when_replay_text_changed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    request = _export_request(tmp_path)
    calls: list[str] = []

    def no_changes(path: Path) -> bool:
        del path
        return False

    def record_edit(*, request: ExportRequest, title: str) -> None:
        del request, title
        calls.append("edit")

    monkeypatch.setattr(sync_export_pr, "_git_has_changes", no_changes)
    monkeypatch.setattr(
        sync_export_pr,
        "_edit_export_pr",
        record_edit,
    )

    _open_or_update_export_pr(
        request=request,
        pr_plan=sync_export_pr.PrReplayPlan(
            state=_pr_state(),
            body="new body\n",
            replay_base="",
            replay_base_digest="",
            current_pr=sync_export_pr.CurrentPr(
                title="Public title",
                body="old body\n",
                number=12,
                url="https://example.test/pr/12",
            ),
        ),
    )

    assert calls == ["edit"]
    assert (tmp_path / "copybarista-pr-body.md").read_text(
        encoding="utf-8"
    ) == "new body\n"


def test_no_diff_exits_when_no_pr_and_no_tree_change(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    request = _export_request(tmp_path)
    calls: list[str] = []

    def no_changes(path: Path) -> bool:
        del path
        return False

    def record_edit(*, request: ExportRequest, title: str) -> None:
        del request, title
        calls.append("edit")

    monkeypatch.setattr(sync_export_pr, "_git_has_changes", no_changes)
    monkeypatch.setattr(
        sync_export_pr,
        "_edit_export_pr",
        record_edit,
    )

    _open_or_update_export_pr(
        request=request,
        pr_plan=sync_export_pr.PrReplayPlan(
            state=_pr_state(),
            body="new body\n",
            replay_base="",
            replay_base_digest="",
            current_pr=None,
        ),
    )

    assert calls == []


def test_replay_without_metadata_keeps_generic_pr_text():
    base = PrReplayState(
        title="Update Sagent export",
        authors=(),
        body_intro="Updates the generated Sagent public repository export.",
        body_entries=(),
        applied_source_rev="abc123",
        applied_source_digest=_source_rev_digest("abc123"),
        metadata_count=0,
    )

    assert replay_pr_metadata(base=base, patches=()) == base


def test_export_pr_text_uses_defaults_without_commit_message_opt_in():
    text = export_pr_text(
        title="",
        body="",
        source_message="Improve README image\n\nRestore the exported mascot asset.",
        use_source_message=False,
        forbidden_text=("private-source",),
    )

    assert text.title == "Update public export"
    assert text.body == "Updates the generated public repository export."


def test_export_pr_text_can_use_commit_title_and_description():
    text = export_pr_text(
        title="",
        body="",
        source_message="Improve README image\n\nRestore the exported mascot asset.",
        use_source_message=True,
        forbidden_text=("private-source",),
    )

    assert text.title == "Improve README image"
    assert text.body == "Restore the exported mascot asset."


def test_export_pr_text_accepts_manual_title_and_body():
    text = export_pr_text(
        title="Public sync update",
        body="Prepare release docs.",
        source_message="Private implementation detail",
        use_source_message=True,
        forbidden_text=("private",),
    )

    assert text.title == "Public sync update"
    assert text.body == "Prepare release docs."


def test_public_pr_text_rejects_private_source_names():
    private_name = "Lo" + "op"

    with pytest.raises(SystemExit) as error:
        _public_pr_text(
            value=f"Publish changes from {private_name}",
            name="--pr-title",
            forbidden_text=(private_name,),
        )

    assert error.value.code == 2


def test_public_pr_text_accepts_reviewable_summary():
    assert (
        _public_pr_text(
            value="Prepare public repository updates",
            name="--pr-title",
            forbidden_text=("private-source",),
        )
        == "Prepare public repository updates"
    )


def test_export_branch_name_uses_source_branch():
    assert (
        export_branch_name(
            explicit="",
            source_branch="main",
            source_sha="abcdef",
            prefix="copybarista/export/",
        )
        == "copybarista/export/main"
    )


def test_export_branch_name_allows_explicit_branch():
    assert (
        export_branch_name(
            explicit="copybarista/export/custom",
            source_branch="main",
            source_sha="abcdef",
            prefix="copybarista/export/",
        )
        == "copybarista/export/custom"
    )


def test_export_branch_name_rejects_non_generated_explicit_branch():
    with pytest.raises(SystemExit) as error:
        export_branch_name(
            explicit="main",
            source_branch="main",
            source_sha="abcdef",
            prefix="copybarista/export/",
        )

    assert error.value.code == 2


def test_export_branch_name_rejects_malformed_explicit_branch():
    with pytest.raises(SystemExit) as error:
        export_branch_name(
            explicit="copybarista/export/../main",
            source_branch="main",
            source_sha="abcdef",
            prefix="copybarista/export/",
        )

    assert error.value.code == 2


def test_commit_author_uses_sync_identity():
    assert (
        _commit_author("copybarista", "copybarista@example.com")
        == "copybarista <copybarista@example.com>"
    )


def test_generated_commit_author_prefers_source_author():
    assert (
        _generated_commit_author(
            authors=(SourceAuthor(name="Source Author", email="source@example.com"),),
            fallback_name="copybarista",
            fallback_email="copybarista@example.com",
        )
        == "Source Author <source@example.com>"
    )


def test_generated_commit_author_uses_sync_identity_without_metadata():
    assert (
        _generated_commit_author(
            authors=(),
            fallback_name="copybarista",
            fallback_email="copybarista@example.com",
        )
        == "copybarista <copybarista@example.com>"
    )


def test_export_public_tree_uses_current_copybarista_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[list[str]] = []

    def fake_run(
        argv: list[str],
        **_: object,
    ) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(sync_export_pr, "_run", fake_run)

    _export_public_tree(
        project=Path("/repo/packages/example"),
        source_dir=Path("/repo"),
        export_dir=tmp_path / "export",
        manifest=tmp_path / "manifest.json",
    )

    assert calls[0][:3] == [sync_export_pr.sys.executable, "-m", "copybarista"]
    assert "--project" not in calls[0]


def test_gh_pr_exists_only_counts_open_prs(monkeypatch: pytest.MonkeyPatch):
    def fake_run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        assert argv[0:4] == ["gh", "pr", "list", "--repo"]
        assert "--state" in argv
        assert "open" in argv
        return subprocess.CompletedProcess(argv, 0, stdout="[]")

    monkeypatch.setattr(sync_export_pr, "_run", fake_run)

    assert not _gh_pr_exists(
        branch="copybarista/export/main",
        repo="rekursiv-ai/copybarista",
        cwd=Path.cwd(),
    )


def test_gh_pr_exists_retries_transient_github_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def fake_run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        if calls == 1:
            return subprocess.CompletedProcess(
                argv,
                1,
                stdout="",
                stderr="HTTP 504: try resubmitting your request",
            )
        return subprocess.CompletedProcess(argv, 0, stdout="[]", stderr="")

    def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(sync_export_pr, "_run", fake_run)
    monkeypatch.setattr(sync_export_pr.time, "sleep", no_sleep)

    assert not _gh_pr_exists(
        branch="copybarista/export/main",
        repo="rekursiv-ai/copybarista",
        cwd=Path.cwd(),
    )
    assert calls == 2


def test_gh_pr_exists_fails_loudly_after_retry_limit(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls = 0

    def fake_run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        return subprocess.CompletedProcess(
            argv,
            1,
            stdout="",
            stderr="HTTP 504: try resubmitting your request",
        )

    def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(sync_export_pr, "_run", fake_run)
    monkeypatch.setattr(sync_export_pr.time, "sleep", no_sleep)

    with pytest.raises(SystemExit) as error:
        _gh_pr_exists(
            branch="copybarista/export/main",
            repo="rekursiv-ai/copybarista",
            cwd=Path.cwd(),
        )

    assert error.value.code == 1
    assert calls == sync_export_pr.GITHUB_RETRY_ATTEMPTS
    assert "HTTP 504" in capsys.readouterr().err


def test_replace_tree_preserves_git_and_removes_stale_files(tmp_path: Path):
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    (source / ".github/workflows").mkdir(parents=True)
    (source / ".github/workflows/ci.yml").write_text(
        "name: CI\n",
        encoding="utf-8",
    )
    (source / "pkg").mkdir(parents=True)
    (source / "pkg/module.py").write_text("new\n", encoding="utf-8")
    (destination / ".git").mkdir(parents=True)
    (destination / ".git/HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (destination / ".github/workflows").mkdir(parents=True)
    (destination / ".github/workflows/import.yml").write_text(
        "name: Import\n",
        encoding="utf-8",
    )
    (destination / "stale.txt").write_text("old\n", encoding="utf-8")
    (destination / "pkg").mkdir()
    (destination / "pkg/old.py").write_text("old\n", encoding="utf-8")

    _replace_tree(source=source, destination=destination)

    assert (destination / ".git/HEAD").read_text(encoding="utf-8")
    assert (destination / ".github/workflows/import.yml").read_text(encoding="utf-8")
    assert (destination / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert not (destination / "stale.txt").exists()
    assert not (destination / "pkg/old.py").exists()
    assert (destination / "pkg/module.py").read_text(encoding="utf-8") == "new\n"


def test_run_export_sync_keeps_validation_mutations_out_of_public_checkout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    public_dir = tmp_path / "public"
    source_dir = tmp_path / "source"
    public_dir.mkdir()
    source_dir.mkdir()
    (public_dir / "uv.lock").write_text("old lock\n", encoding="utf-8")
    opened: list[str] = []

    def fake_resolve_pr_replay_plan(
        *, request: ExportRequest
    ) -> sync_export_pr.PrReplayPlan:
        del request
        return sync_export_pr.PrReplayPlan(
            state=_pr_state(),
            body="Public body.\n",
            replay_base="",
            replay_base_digest="",
            current_pr=None,
        )

    def fake_export_public_tree(
        *, project: Path, source_dir: Path, export_dir: Path, manifest: Path
    ) -> None:
        del project, source_dir, manifest
        (export_dir / "pkg").mkdir()
        (export_dir / "pkg/module.py").write_text("source\n", encoding="utf-8")

    def fake_validate_public(
        *,
        public_dir: Path,
        dist_dir: Path,
        release_check_script: Path | None,
        frozen_sync: bool,
        type_check_targets: tuple[str, ...],
        smoke_import: str,
    ) -> None:
        del (
            dist_dir,
            release_check_script,
            frozen_sync,
            type_check_targets,
            smoke_import,
        )
        (public_dir / "uv.lock").write_text("validation lock\n", encoding="utf-8")

    def fake_open_or_update_export_pr(
        *, request: ExportRequest, pr_plan: sync_export_pr.PrReplayPlan
    ) -> None:
        del pr_plan
        lockfile = request.public_dir / "uv.lock"
        opened.append(lockfile.read_text(encoding="utf-8") if lockfile.exists() else "")

    monkeypatch.setattr(
        sync_export_pr,
        "_resolve_pr_replay_plan",
        fake_resolve_pr_replay_plan,
    )
    monkeypatch.setattr(sync_export_pr, "_export_public_tree", fake_export_public_tree)
    monkeypatch.setattr(sync_export_pr, "_validate_public", fake_validate_public)
    monkeypatch.setattr(
        sync_export_pr,
        "_open_or_update_export_pr",
        fake_open_or_update_export_pr,
    )

    sync_export_pr.run_export_sync(
        replace(
            _export_request(tmp_path),
            source_dir=source_dir,
            public_dir=public_dir,
            skip_source_validation=True,
        )
    )

    assert opened == [""]
    assert not (public_dir / "uv.lock").exists()


def test_run_export_sync_refreshes_public_lockfile_before_frozen_validation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    public_dir = tmp_path / "public"
    source_dir = tmp_path / "source"
    public_dir.mkdir()
    source_dir.mkdir()
    calls: list[str] = []

    def fake_resolve_pr_replay_plan(
        *, request: ExportRequest
    ) -> sync_export_pr.PrReplayPlan:
        del request
        return sync_export_pr.PrReplayPlan(
            state=_pr_state(),
            body="Public body.\n",
            replay_base="",
            replay_base_digest="",
            current_pr=None,
        )

    def fake_export_public_tree(
        *, project: Path, source_dir: Path, export_dir: Path, manifest: Path
    ) -> None:
        del project, source_dir, manifest
        (export_dir / "pyproject.toml").write_text("[project]\n", encoding="utf-8")

    def fake_refresh_public_lockfile(*, public_dir: Path) -> None:
        calls.append("lock")
        (public_dir / "uv.lock").write_text("public lock\n", encoding="utf-8")

    def fake_validate_public(
        *,
        public_dir: Path,
        dist_dir: Path,
        release_check_script: Path | None,
        frozen_sync: bool,
        type_check_targets: tuple[str, ...],
        smoke_import: str,
    ) -> None:
        del dist_dir, release_check_script, type_check_targets, smoke_import
        assert frozen_sync
        assert (public_dir / "uv.lock").read_text(encoding="utf-8") == "public lock\n"
        calls.append("validate")

    def fake_open_or_update_export_pr(
        *, request: ExportRequest, pr_plan: sync_export_pr.PrReplayPlan
    ) -> None:
        del pr_plan
        assert (request.public_dir / "uv.lock").read_text(encoding="utf-8") == (
            "public lock\n"
        )
        calls.append("open")

    monkeypatch.setattr(
        sync_export_pr,
        "_resolve_pr_replay_plan",
        fake_resolve_pr_replay_plan,
    )
    monkeypatch.setattr(sync_export_pr, "_export_public_tree", fake_export_public_tree)
    monkeypatch.setattr(
        sync_export_pr,
        "_refresh_public_lockfile",
        fake_refresh_public_lockfile,
    )
    monkeypatch.setattr(sync_export_pr, "_validate_public", fake_validate_public)
    monkeypatch.setattr(
        sync_export_pr,
        "_open_or_update_export_pr",
        fake_open_or_update_export_pr,
    )

    sync_export_pr.run_export_sync(
        replace(
            _export_request(tmp_path),
            source_dir=source_dir,
            public_dir=public_dir,
            refresh_public_lockfile=True,
            skip_source_validation=True,
        )
    )

    assert calls == ["lock", "validate", "open"]


def test_validate_source_runs_ty_check(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0)

    def fake_basedpyright(*, project: Path, targets: tuple[str, ...]) -> None:
        calls.append(["basedpyright", str(project), *targets])

    monkeypatch.setattr(sync_export_pr, "_run", fake_run)
    monkeypatch.setattr(sync_export_pr, "_run_basedpyright", fake_basedpyright)

    _validate_source(project=Path("/repo/pkg"), type_check_targets=("configgle",))

    assert [
        "uv",
        "--quiet",
        "--project",
        "/repo/pkg",
        "run",
        "ty",
        "check",
    ] in calls


def test_validate_public_runs_ty_check(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0)

    def fake_basedpyright_public(*, public_dir: Path, targets: tuple[str, ...]) -> None:
        calls.append(["basedpyright", str(public_dir), *targets])

    monkeypatch.setattr(sync_export_pr, "_run", fake_run)
    monkeypatch.setattr(
        sync_export_pr,
        "_run_basedpyright_public",
        fake_basedpyright_public,
    )

    _validate_public(
        public_dir=Path("/public"),
        dist_dir=Path("/dist"),
        release_check_script=None,
        frozen_sync=False,
        type_check_targets=("configgle",),
        smoke_import="",
    )

    assert ["uv", "run", "--all-groups", "ty", "check"] in calls
    assert ["uv", "run", "--all-groups", "codespell", "."] in calls


def test_validate_public_uses_frozen_sync_when_lockfile_is_managed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0)

    def fake_basedpyright_public(*, public_dir: Path, targets: tuple[str, ...]) -> None:
        del public_dir, targets

    monkeypatch.setattr(sync_export_pr, "_run", fake_run)
    monkeypatch.setattr(
        sync_export_pr,
        "_run_basedpyright_public",
        fake_basedpyright_public,
    )

    _validate_public(
        public_dir=Path("/public"),
        dist_dir=Path("/dist"),
        release_check_script=None,
        frozen_sync=True,
        type_check_targets=("configgle",),
        smoke_import="",
    )

    assert ["uv", "sync", "--frozen", "--all-groups"] in calls


def test_remove_public_validation_artifacts_preserves_sources(tmp_path: Path) -> None:
    (tmp_path / "pkg/__pycache__").mkdir(parents=True)
    (tmp_path / "pkg/__pycache__/module.cpython-312.pyc").write_bytes(b"pyc")
    (tmp_path / ".pytest_cache").mkdir()
    (tmp_path / ".pytest_cache/README.md").write_text("cache\n", encoding="utf-8")
    (tmp_path / ".venv/bin").mkdir(parents=True)
    (tmp_path / ".venv/bin/python").write_text("python\n", encoding="utf-8")
    (tmp_path / ".coverage").write_text("coverage\n", encoding="utf-8")
    (tmp_path / "pkg/module.py").write_text("source\n", encoding="utf-8")

    _remove_public_validation_artifacts(tmp_path)

    assert (tmp_path / "pkg/module.py").read_text(encoding="utf-8") == "source\n"
    assert not (tmp_path / "pkg/__pycache__").exists()
    assert not (tmp_path / ".pytest_cache").exists()
    assert not (tmp_path / ".venv").exists()
    assert not (tmp_path / ".coverage").exists()
