"""Tests for source-to-public sync helpers."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import subprocess
import sys

import pytest

from copybarista.config import Transform
from copybarista.scripts import sync_export_pr, sync_import_change
from copybarista.scripts.sync_export_pr import (
    ExportRequest,
    PrBodyEntry,
    PrMetadataPatch,
    PrReplayState,
    SourceAuthor,
    _applied_marker_from_pr_body,
    _base_pr_state,
    _commit_author,
    _export_commit_message,
    _export_public_tree,
    _generated_commit_author,
    _gh_pr_exists,
    _open_or_update_export_pr,
    _parse_pr_metadata_log,
    _pending_import_prs,
    _postleakcheck_validation,
    _preleakcheck_validation,
    _public_head_unimported,
    _public_pr_text,
    _render_pr_body,
    _replace_pr_state,
    _replace_tree,
    _source_rev_digest,
    _state_from_pr_body,
    _validate_public,
    export_branch_name,
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
        validation_commands=(),
        dry_run=False,
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

    sync_export_pr.run(
        [
            "--project-path",
            "packages/configgle",
            "--source-branch",
            "main",
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


def test_main_accepts_dry_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[ExportRequest] = []

    def fake_run_export_sync(request: ExportRequest) -> None:
        captured.append(request)

    monkeypatch.setattr(sync_export_pr, "run_export_sync", fake_run_export_sync)

    sync_export_pr.run(
        [
            "--project-path",
            "packages/example",
            "--source-branch",
            "main",
            "--dry-run",
        ]
    )

    assert captured[0].dry_run


def test_main_derives_request_from_project_positional(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    project_dir, source_dir = _project_with_sync_settings(tmp_path)
    captured: list[ExportRequest] = []

    def fake_run_export_sync(request: ExportRequest) -> None:
        captured.append(request)

    def fake_git_toplevel(start: Path) -> Path:
        del start
        return source_dir

    monkeypatch.setattr(sync_export_pr, "run_export_sync", fake_run_export_sync)
    monkeypatch.setattr(sync_export_pr, "_git_toplevel", fake_git_toplevel)

    sync_export_pr.run([str(project_dir), "--dry-run"])

    request = captured[0]
    assert request.dry_run
    assert request.source_dir == source_dir
    assert request.project_path == Path("packages/sagent")
    assert request.public_dir == source_dir.parent / "sagent"
    assert request.target_repo == "rekursiv-ai/sagent"
    assert request.smoke_import == "sagent"
    assert request.type_check_targets == (".",)
    assert request.forbidden_pr_text == ("src",)
    assert request.refresh_public_lockfile
    assert request.sync_label == "Sagent"
    assert request.replay_settings.scope == "sagent"


def test_main_explicit_flag_overrides_sync_setting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    project_dir, source_dir = _project_with_sync_settings(tmp_path)
    captured: list[ExportRequest] = []

    def fake_run_export_sync(request: ExportRequest) -> None:
        captured.append(request)

    def fake_git_toplevel(start: Path) -> Path:
        del start
        return source_dir

    monkeypatch.setattr(sync_export_pr, "run_export_sync", fake_run_export_sync)
    monkeypatch.setattr(sync_export_pr, "_git_toplevel", fake_git_toplevel)

    sync_export_pr.run([str(project_dir), "--dry-run", "--smoke-import", "alt_package"])

    assert captured[0].smoke_import == "alt_package"


def _project_with_sync_settings(tmp_path: Path) -> tuple[Path, Path]:
    source_dir = tmp_path / "src"
    project_dir = source_dir / "packages" / "sagent"
    project_dir.mkdir(parents=True)
    (project_dir / "copy.barista.toml").write_text("# stub\n", encoding="utf-8")
    (project_dir / "copybarista.sync.toml").write_text(
        """\
[sync]
package_name = "sagent"
sync_label = "Sagent"
sync_user_name = "sagent-bot"
sync_user_email = "sagent-bot@example.com"
source_root = "packages/sagent"
public_repo = "rekursiv-ai/sagent"
source_repo = "rekursiv-ai/src"
copybarista_project_path = "packages/copybarista"
smoke_import = "sagent"
type_check_targets = ["."]
forbidden_pr_text = ["src"]
refresh_public_lockfile = true

[pull_request]
default_title = "Update Sagent export"
default_body = "Updates the generated Sagent public repository export."
""",
        encoding="utf-8",
    )
    return project_dir, source_dir


def test_main_accepts_auto_merge_value_arg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[ExportRequest] = []

    def fake_run_export_sync(request: ExportRequest) -> None:
        captured.append(request)

    monkeypatch.setattr(sync_export_pr, "run_export_sync", fake_run_export_sync)

    sync_export_pr.run(
        [
            "--project-path",
            "packages/example",
            "--source-branch",
            "main",
            "--auto-merge=false",
        ]
    )

    assert not captured[0].auto_merge


def test_main_accepts_auto_merge_as_boolean_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[ExportRequest] = []

    def fake_run_export_sync(request: ExportRequest) -> None:
        captured.append(request)

    monkeypatch.setattr(sync_export_pr, "run_export_sync", fake_run_export_sync)

    sync_export_pr.run(
        [
            "--project-path",
            "packages/example",
            "--source-branch",
            "main",
            "--auto-merge",
        ]
    )

    assert captured[0].auto_merge


def test_main_passes_github_source_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[ExportRequest] = []
    monkeypatch.setenv(
        "GITHUB_EVENT_HEAD_COMMIT_MESSAGE",
        "Commit title\n\nCommit body.",
    )

    def fake_run_export_sync(request: ExportRequest) -> None:
        captured.append(request)

    monkeypatch.setattr(sync_export_pr, "run_export_sync", fake_run_export_sync)

    sync_export_pr.run(
        [
            "--project-path",
            "packages/example",
            "--source-branch",
            "main",
            "--use-source-message-pr-text",
        ]
    )

    assert captured[0].pr_title == "Commit title"
    assert captured[0].pr_body == "Commit body."


def test_main_rejects_manual_branch_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run_export_sync(request: ExportRequest) -> None:
        raise AssertionError(request)

    monkeypatch.delenv("GITHUB_REF_NAME", raising=False)
    monkeypatch.delenv("GITHUB_SHA", raising=False)
    monkeypatch.setattr(sync_export_pr, "run_export_sync", fake_run_export_sync)

    with pytest.raises(SystemExit) as error:
        sync_export_pr.run(["--project-path", "packages/example"])

    assert error.value.code == 2


def test_main_accepts_skip_source_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[ExportRequest] = []

    def fake_run_export_sync(request: ExportRequest) -> None:
        captured.append(request)

    monkeypatch.setattr(sync_export_pr, "run_export_sync", fake_run_export_sync)

    sync_export_pr.run(
        [
            "--project-path",
            "packages/example",
            "--source-branch",
            "main",
            "--skip-source-validation",
        ]
    )

    assert captured[0].skip_source_validation


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


def test_parse_pr_metadata_skips_unknown_field(
    capsys: pytest.CaptureFixture[str],
):
    patches = _parse_pr_metadata_log(
        _metadata_log(
            "Copybarista-PR-Description: typo'd field\n"
            "Copybarista-PR-Title: Public title\n"
        ),
        forbidden_text=(),
    )

    assert patches[0].title == "Public title"
    assert "Description: unknown field; ignored" in capsys.readouterr().err


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


def test_parse_pr_metadata_body_stops_at_blank_line_before_squashed_prose():
    """A body must not absorb a later squashed commit's prose.

    Squash-merge concatenates every squashed commit message into one
    ``%B``. A ``Copybarista-PR-Body:`` in an earlier commit is followed
    by a blank line and then the next commit's ordinary subject/body.
    The body must terminate at that blank line so unrelated prose (here
    a private symbol) is neither absorbed into the public body nor leak
    -checked.
    """
    patches = _parse_pr_metadata_log(
        _metadata_log(
            "Copybarista-PR-Title: Public title\n"
            "Copybarista-PR-Body:\n"
            "Public body text.\n"
            "\n"
            "Second squashed commit: touches TrainLoop.Config internals.\n"
        ),
        forbidden_text=("TrainLoop",),
    )

    assert tuple(patch.body for patch in patches) == ("Public body text.",)


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


def test_parse_pr_metadata_rewrites_source_path_via_literal_transform():
    # A PR body referencing a source path is REWRITTEN to its public form by the
    # same export replace-transform, then passes the leak check -- rather than the
    # author having to hand-scrub the token per commit. Regression: a private
    # module prefix in the body used to hard-fail the export.
    # (Synthetic ``priv``/``pub`` tokens keep this test's own text export-safe.)
    transforms = (
        Transform(
            id="lib", type="replace", path="**", before="priv.mod", after="pub.mod"
        ),
    )
    patches = _parse_pr_metadata_log(
        _metadata_log(
            "Copybarista-PR-Title: Fix import path\n"
            "Copybarista-PR-Body:\n"
            "Use submodule path `priv.mod.testing.main`.\n"
        ),
        forbidden_text=("priv.", "priv/"),
        text_transforms=transforms,
    )
    assert tuple(p.body for p in patches) == (
        "Use submodule path `pub.mod.testing.main`.",
    )


def test_parse_pr_metadata_rewrites_source_path_via_regex_group_transform():
    # The regex_groups template form (priv.pkg.${s} -> pub.${s}) rewrites a
    # dotted identifier reference in the title, then the leak check passes.
    transforms = (
        Transform(
            id="pkg",
            type="replace",
            path="**",
            before="priv.pkg.${s}",
            after="pub.${s}",
            regex_groups=(("s", "[A-Za-z_]"),),
        ),
    )
    patches = _parse_pr_metadata_log(
        _metadata_log(
            "Copybarista-PR-Title: Touch priv.pkg.fetch\n"
            "Copybarista-PR-Body:\nBody text.\n"
        ),
        forbidden_text=("priv.",),
        text_transforms=transforms,
    )
    assert tuple(p.title for p in patches) == ("Touch pub.fetch",)


def test_parse_pr_metadata_leak_check_still_guards_untransformed_text():
    # The rewrite is not a bypass: a forbidden token no transform covers still
    # fails the leak check.
    transforms = (
        Transform(
            id="lib", type="replace", path="**", before="priv.mod", after="pub.mod"
        ),
    )
    with pytest.raises(sync_export_pr.PrMetadataError, match=r"abcdef1.*Body"):
        _parse_pr_metadata_log(
            _metadata_log("Copybarista-PR-Body:\nMentions private-org/secret repo.\n"),
            forbidden_text=("private-org/secret",),
            text_transforms=transforms,
        )


def test_prefix_term_ignores_prose_word():
    # A dotted/path prefix term must not trip on the same word ending a sentence.
    patches = _parse_pr_metadata_log(
        _metadata_log(
            "Copybarista-PR-Title: t\n"
            "Copybarista-PR-Body:\nReimplementing the package. Also uses package/ "
            "style prose here.\n"
        ),
        forbidden_text=("package.", "package/"),
    )
    assert tuple(p.body for p in patches) == (
        "Reimplementing the package. Also uses package/ style prose here.",
    )


def test_prefix_term_rejects_dotted_import():
    with pytest.raises(sync_export_pr.PrMetadataError, match=r"abcdef1.*Body"):
        _parse_pr_metadata_log(
            _metadata_log("Copybarista-PR-Body:\nImport package.module.agent.\n"),
            forbidden_text=("package.",),
        )


def test_prefix_term_rejects_path():
    with pytest.raises(sync_export_pr.PrMetadataError, match=r"abcdef1.*Body"):
        _parse_pr_metadata_log(
            _metadata_log("Copybarista-PR-Body:\nSee package/lib/web for details.\n"),
            forbidden_text=("package/",),
        )


def test_non_prefix_term_still_substring_matches():
    # A term without a trailing separator keeps plain substring matching, so a
    # trailing period does not shield it.
    with pytest.raises(sync_export_pr.PrMetadataError, match=r"abcdef1.*Body"):
        _parse_pr_metadata_log(
            _metadata_log("Copybarista-PR-Body:\nMentions company/source.\n"),
            forbidden_text=("company/source",),
        )


# Package references the prefix matcher must still catch (false-negative guard).
# Each prefix is followed by a token continuation.
@pytest.mark.parametrize(
    "text",
    [
        "package.experimental",
        "package.lib.web.paper",
        "package.web",
        "import package.foo",
        ":mod:`package.lib.web.paper`",  # A doc-ref is still a reference.
        "package/lib",
        "package/experimental/agent",
        "package/bin",
        "package/.github/workflows",  # A dotfile path is still a reference.
        "package/-weird",  # A non-word path character still continues the path.
        "see package.lib, plus more",  # Punctuation after a word still matches.
    ],
)
def test_prefix_matcher_catches_real_references(text: str) -> None:
    term = "package." if "package." in text else "package/"
    assert sync_export_pr._forbidden_term_present(term, text)


# Prose uses of the bare word that MUST be allowed (false-positive guard).
@pytest.mark.parametrize(
    "text",
    [
        "reimplementing the walk loop.",  # sentence-final period
        "the walk loop. Eliminates bugs",  # period + space
        "uses loop/ style prose",  # slash + space
        "the event loop",  # no separator at all
        "backtick ``loop.``",  # separator then closing backtick
        "(see the loop.)",  # separator then closing paren
        "the loop/",  # slash at end-of-string
    ],
)
def test_prefix_matcher_allows_prose(text: str) -> None:
    term = "loop/" if "loop/" in text else "loop."
    assert not sync_export_pr._forbidden_term_present(term, text)


def test_plain_term_matches_anywhere() -> None:
    # A separator-less term is a pure substring check (no boundary logic).
    assert sync_export_pr._forbidden_term_present("LOOP_ENV", "the LOOP_ENV var")
    assert not sync_export_pr._forbidden_term_present("LOOP_ENV", "the loop env")


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


def test_replay_append_body_keeps_multiple_entries_from_same_commit():
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
                author=SourceAuthor(name="Commit Author", email="author@example.com"),
                body="Shared update.",
                body_mode="append",
            ),
            PrMetadataPatch(
                commit_sha="1111111",
                scope="sagent",
                title="",
                author=SourceAuthor(name="Commit Author", email="author@example.com"),
                body="Scoped update.",
                body_mode="append",
            ),
        ),
    )

    assert state.body_entries == (
        PrBodyEntry(commit_sha="1111111", text="Shared update."),
        PrBodyEntry(commit_sha="1111111", text="Scoped update."),
    )


def test_replace_pr_state_allows_empty_overrides():
    base = PrReplayState(
        title="Title",
        authors=(),
        body_intro="Body",
        body_entries=(),
        applied_source_rev="rev",
        applied_source_digest="digest",
        metadata_count=0,
    )

    assert _replace_pr_state(
        base,
        title="",
        body_intro="",
        applied_source_rev="",
        applied_source_digest="",
    ) == PrReplayState(
        title="",
        authors=(),
        body_intro="",
        body_entries=(),
        applied_source_rev="",
        applied_source_digest="",
        metadata_count=0,
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


def test_base_pr_state_recovers_existing_entry_authors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    previous_sha = "1111111"
    current_sha = "2222222"
    body = _render_pr_body(
        state=PrReplayState(
            title="Public title",
            authors=(),
            body_intro="Public summary.",
            body_entries=(PrBodyEntry(commit_sha=previous_sha, text="Old update."),),
            applied_source_rev=previous_sha,
            applied_source_digest=_source_rev_digest(previous_sha),
            metadata_count=1,
        ),
        branch="copybarista/export/main",
        sync_label="Copybarista",
        publish_source_rev=False,
    )
    request = _export_request(tmp_path)

    def fake_run(
        argv: list[str],
        **_: object,
    ) -> subprocess.CompletedProcess[str]:
        if argv[:2] == ["git", "rev-list"]:
            return subprocess.CompletedProcess(argv, 0, stdout=f"{previous_sha}\n")
        if argv[:4] == ["git", "show", "-s", "--format=%aN%x00%aE"]:
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout="Previous Author\0previous@example.com\n",
            )
        raise AssertionError(argv)

    monkeypatch.setattr(sync_export_pr, "_run", fake_run)

    state = _base_pr_state(
        request=request,
        current_pr=sync_export_pr.CurrentPr(
            title="Public title",
            body=body,
            number=7,
            url="https://example.test/pr/7",
        ),
        current_source_rev=current_sha,
        current_source_digest=_source_rev_digest(current_sha),
    )

    assert state.authors == (
        SourceAuthor(name="Previous Author", email="previous@example.com"),
    )
    assert "Recovered PR entry author:" in capsys.readouterr().out


def test_base_pr_state_drops_stale_raw_entry_markers(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    stale_sha = "1111111"
    body = (
        "## Summary\n\n"
        "Public summary.\n\n"
        "### Updates\n\n"
        f"<!-- copybarista:pr-entry source=sha:{stale_sha} -->\n"
        "- Old update.\n\n"
        "----\n"
        "Copybarista export branch: `copybarista/export/main`\n\n"
        f"<!-- copybarista:pr-state version=1 applied=sha256:{_source_rev_digest(stale_sha)} -->"
    )
    request = _export_request(tmp_path)

    def fake_run(
        argv: list[str],
        **_: object,
    ) -> subprocess.CompletedProcess[str]:
        if argv[:3] == ["git", "merge-base", "--is-ancestor"]:
            return subprocess.CompletedProcess(argv, 1, stdout="")
        raise AssertionError(argv)

    monkeypatch.setattr(sync_export_pr, "_run", fake_run)

    state = _base_pr_state(
        request=request,
        current_pr=sync_export_pr.CurrentPr(
            title="Public title",
            body=body,
            number=7,
            url="https://example.test/pr/7",
        ),
        current_source_rev="2222222",
        current_source_digest=_source_rev_digest("2222222"),
    )

    assert state.body_entries == ()
    assert "Dropped stale PR entry marker: marker=sha:111" in capsys.readouterr().out


def test_base_pr_state_drops_stale_entry_markers(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    previous_sha = "1111111"
    current_sha = "2222222"
    stale_marker = "sha256:missing"
    body = (
        "## Summary\n\n"
        "Public summary.\n\n"
        "### Updates\n\n"
        f"<!-- copybarista:pr-entry source={stale_marker} -->\n"
        "- Old update.\n"
        f"<!-- copybarista:pr-entry source=sha256:{_source_rev_digest(previous_sha)} -->\n"
        "- Reachable update.\n\n"
        "----\n"
        "Copybarista export branch: `copybarista/export/main`\n\n"
        f"<!-- copybarista:pr-state version=1 "
        f"applied=sha256:{_source_rev_digest(previous_sha)} -->"
    )
    request = _export_request(tmp_path)

    def fake_run(
        argv: list[str],
        **_: object,
    ) -> subprocess.CompletedProcess[str]:
        if argv[:2] == ["git", "rev-list"]:
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout=f"{current_sha}\n{previous_sha}\n",
            )
        if argv[:4] == ["git", "show", "-s", "--format=%aN%x00%aE"]:
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout="Previous Author\0previous@example.com\n",
            )
        raise AssertionError(argv)

    monkeypatch.setattr(sync_export_pr, "_run", fake_run)

    state = _base_pr_state(
        request=request,
        current_pr=sync_export_pr.CurrentPr(
            title="Public title",
            body=body,
            number=7,
            url="https://example.test/pr/7",
        ),
        current_source_rev=current_sha,
        current_source_digest=_source_rev_digest(current_sha),
    )

    assert state.authors == (
        SourceAuthor(name="Previous Author", email="previous@example.com"),
    )
    assert state.body_entries == (
        PrBodyEntry(
            commit_sha=f"sha256:{_source_rev_digest(previous_sha)}",
            text="Reachable update.",
        ),
    )
    output = capsys.readouterr().out
    assert "Dropped stale PR entry marker: marker=sha256:missing" in output
    assert "Recovered PR entry author:" in output


def test_resolve_pr_replay_plan_logs_replay_summary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    base_request = _export_request(tmp_path)
    request = replace(
        base_request,
        replay_settings=replace(base_request.replay_settings, scope="sagent"),
    )

    def fake_current_source_rev(*, source_dir: Path, fallback: str) -> str:
        del source_dir, fallback
        return "222222222222"

    def fake_current_pr(
        *, branch: str, repo: str, cwd: Path
    ) -> sync_export_pr.CurrentPr | None:
        del branch, repo, cwd
        return None

    def fake_branch_markers(*, branch: str, cwd: Path) -> sync_export_pr.BranchMarkers:
        del branch, cwd
        return sync_export_pr.BranchMarkers(
            source_digest="",
            replay_base_digest="",
            exists=True,
        )

    def fake_replay_base(
        *,
        request: ExportRequest,
        current_source_rev: str,
        current_pr: sync_export_pr.CurrentPr | None,
        branch_markers: sync_export_pr.BranchMarkers,
    ) -> str:
        del request, current_source_rev, current_pr, branch_markers
        return "111111111111"

    def fake_source_pr_metadata(
        *,
        source_dir: Path,
        replay_base: str,
        current_source_rev: str,
        forbidden_text: tuple[str, ...],
        scope: str,
        text_transforms: tuple[Transform, ...] = (),
    ) -> tuple[PrMetadataPatch, ...]:
        del source_dir, replay_base, current_source_rev, forbidden_text, scope
        del text_transforms
        return (
            PrMetadataPatch(
                commit_sha="222222222222",
                scope="sagent",
                title="Public title",
                author=SourceAuthor(name="Source Author", email="source@example.com"),
                body="Public update.",
                body_mode="append",
            ),
        )

    monkeypatch.setattr(
        sync_export_pr,
        "_current_source_rev",
        fake_current_source_rev,
    )
    monkeypatch.setattr(
        sync_export_pr,
        "_current_pr",
        fake_current_pr,
    )
    monkeypatch.setattr(
        sync_export_pr,
        "_branch_markers",
        fake_branch_markers,
    )
    monkeypatch.setattr(
        sync_export_pr,
        "_replay_base",
        fake_replay_base,
    )
    monkeypatch.setattr(
        sync_export_pr,
        "_source_pr_metadata",
        fake_source_pr_metadata,
    )

    plan = sync_export_pr._resolve_pr_replay_plan(request=request, pr_template="")

    assert plan.state.authors == (
        SourceAuthor(name="Source Author", email="source@example.com"),
    )
    output = capsys.readouterr().out
    assert "PR replay source range: base=1111111 head=2222222" in output
    assert "scope=sagent branch=copybarista/export/main" in output
    assert "PR metadata replay: patches=1 commits=1" in output
    assert "authors=Source Author <source@example.com>" in output


def test_render_pr_body_rejects_empty_raw_source_marker():
    with pytest.raises(sync_export_pr.PrReplayError, match="applied source marker"):
        _render_pr_body(
            state=PrReplayState(
                title="Public title",
                authors=(),
                body_intro="Public body.",
                body_entries=(),
                applied_source_rev="",
                applied_source_digest="digest",
                metadata_count=0,
            ),
            branch="copybarista/export/main",
            sync_label="Copybarista",
            publish_source_rev=True,
        )


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

    assert (
        state.body_entries[0].commit_sha == f"sha256:{_source_rev_digest(source_rev)}"
    )
    assert state.body_entries[0].text == "First line.\nSecond line."


def test_state_from_pr_body_skips_malformed_entry_marker():
    body = (
        "## Summary\n\n"
        "Public summary.\n\n"
        "### Updates\n\n"
        "<!-- copybarista:pr-entry -->\n"
        "- Corrupt old update.\n"
        "<!-- copybarista:pr-entry source=sha256:reachable -->\n"
        "- Reachable update.\n\n"
        "----\n"
        "Copybarista export branch: `copybarista/export/main`\n\n"
        "<!-- copybarista:pr-state version=1 applied=sha256:beef -->\n"
    )

    state = _state_from_pr_body(
        title="Public title",
        body=body,
        default_body="Default body.",
    )

    assert state.body_entries == (
        PrBodyEntry(commit_sha="sha256:reachable", text="Reachable update."),
    )


def test_state_from_pr_body_preserves_horizontal_rule_in_summary():
    source_rev = "abc123abc123"
    body = _render_pr_body(
        state=PrReplayState(
            title="Public title",
            authors=(),
            body_intro="Before\n\n----\n\nAfter",
            body_entries=(),
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

    assert state.body_intro == "Before\n\n----\n\nAfter"


def test_applied_marker_ignores_marker_text_in_summary():
    body = (
        "## Summary\n\n"
        "See `<!-- copybarista:pr-state version=1 applied=sha256:dead -->`\n"
        "in the docs.\n\n"
        "----\n"
        "Copybarista export branch: `copybarista/export/main`\n\n"
        "<!-- copybarista:pr-state version=1 applied=sha256:beef -->\n"
    )

    assert _applied_marker_from_pr_body(body) == "sha256:beef"


def test_applied_marker_rejects_multiple_footer_state_markers():
    body = (
        "Body\n"
        "----\n"
        "Copybarista export branch: `copybarista/export/main`\n\n"
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


def test_branch_markers_fetches_force_updated_remote_branch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[list[str]] = []

    def fake_run(
        argv: list[str],
        **_: object,
    ) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if argv[:2] == ["git", "fetch"]:
            return subprocess.CompletedProcess(argv, 0, stdout="")
        if argv[:2] == ["git", "rev-parse"]:
            return subprocess.CompletedProcess(argv, 1, stdout="")
        raise AssertionError(argv)

    monkeypatch.setattr(sync_export_pr, "_run", fake_run)

    assert not sync_export_pr._branch_markers(
        branch="copybarista/export/main",
        cwd=tmp_path,
    ).exists
    assert calls[0] == [
        "git",
        "fetch",
        "origin",
        "+refs/heads/copybarista/export/main:refs/remotes/origin/copybarista/export/main",
    ]


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


def test_replay_base_floors_resolved_base_to_bootstrap(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A bootstrap base floors a resolved base that predates it.

    ``replay_bootstrap_base`` documents "skip commits at or before this
    revision" -- a one-time floor past historical commits whose PR-body trailers
    embed forbidden monorepo text. When the branch/PR marker resolves to a base
    OLDER than the bootstrap base, the replay must start at the bootstrap base so
    the leaky commits are never scanned; otherwise a stuck export can never
    recover because it keeps re-reading the poisoned trailer.
    """
    request = replace(
        _export_request(tmp_path),
        replay_settings=replace(
            _export_request(tmp_path).replay_settings,
            bootstrap_base="bootstrap-base",
        ),
    )

    def fake_resolve_source_marker(
        *, source_dir: Path, marker: str, marker_source: str
    ) -> str:
        del marker, marker_source
        assert source_dir == tmp_path
        return "old-marked-base"

    def fake_is_ancestor(*, source_dir: Path, ancestor: str, rev: str) -> bool:
        assert source_dir == tmp_path
        # The resolved marker base is an ancestor of the bootstrap base.
        return ancestor == "old-marked-base" and rev == "bootstrap-base"

    monkeypatch.setattr(
        sync_export_pr, "_resolve_source_marker", fake_resolve_source_marker
    )
    monkeypatch.setattr(sync_export_pr, "_is_source_ancestor", fake_is_ancestor)

    assert (
        sync_export_pr._replay_base(
            request=request,
            current_source_rev="source-head",
            current_pr=None,
            branch_markers=sync_export_pr.BranchMarkers(
                source_digest="applied-digest",
                replay_base_digest="",
                exists=True,
            ),
        )
        == "bootstrap-base"
    )


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

    pr_open = _open_or_update_export_pr(
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
    assert pr_open
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

    pr_open = _open_or_update_export_pr(
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
    # No changes and no existing PR: nothing for auto-merge to act on.
    assert not pr_open


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


@pytest.mark.parametrize(
    "branch",
    [
        "copybarista/export/foo.lock/bar",
        "copybarista/export/.hidden",
    ],
)
def test_export_branch_name_rejects_git_invalid_components(branch: str):
    with pytest.raises(SystemExit) as error:
        export_branch_name(
            explicit=branch,
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

    assert calls[0][:3] == [
        sys.executable,
        "-m",
        "copybarista",
    ]
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


def test_gh_pr_exists_rejects_malformed_json(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 0, stdout="")

    monkeypatch.setattr(sync_export_pr, "_run", fake_run)

    with pytest.raises(sync_export_pr.PrReplayError, match="GitHub PR list"):
        _gh_pr_exists(
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
    monkeypatch.setattr("time.sleep", no_sleep)

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
    monkeypatch.setattr("time.sleep", no_sleep)

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
    assert not (destination / ".github/workflows/import.yml").exists()
    assert (destination / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert not (destination / "stale.txt").exists()
    assert not (destination / "pkg/old.py").exists()
    assert (destination / "pkg/module.py").read_text(encoding="utf-8") == "new\n"


def test_run_export_sync_renders_body_with_exported_pr_template(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    public_dir = tmp_path / "public"
    source_dir = tmp_path / "source"
    public_dir.mkdir()
    source_dir.mkdir()
    opened: list[str] = []

    def fake_resolve_pr_replay_plan(
        *, request: ExportRequest, pr_template: str
    ) -> sync_export_pr.PrReplayPlan:
        del request, pr_template
        return sync_export_pr.PrReplayPlan(
            state=_pr_state(),
            body="",
            replay_base="",
            replay_base_digest="",
            current_pr=None,
        )

    def fake_export_public_tree(
        *, project: Path, source_dir: Path, export_dir: Path, manifest: Path
    ) -> None:
        del project, source_dir, manifest
        (export_dir / ".github").mkdir()
        (export_dir / ".github/PULL_REQUEST_TEMPLATE.md").write_text(
            "## Summary\n\nExported template placeholder.\n",
            encoding="utf-8",
        )
        (export_dir / "pkg").mkdir()
        (export_dir / "pkg/module.py").write_text("source\n", encoding="utf-8")

    def fake_validate_public(
        *,
        public_dir: Path,
        validation_commands: tuple[str, ...],
    ) -> None:
        del public_dir, validation_commands

    def fake_open_or_update_export_pr(
        *, request: ExportRequest, pr_plan: sync_export_pr.PrReplayPlan
    ) -> bool:
        del request
        opened.append(pr_plan.body)
        return True

    monkeypatch.setattr(
        sync_export_pr, "_resolve_pr_replay_plan", fake_resolve_pr_replay_plan
    )
    monkeypatch.setattr(sync_export_pr, "_export_public_tree", fake_export_public_tree)
    monkeypatch.setattr(sync_export_pr, "_validate_public", fake_validate_public)
    monkeypatch.setattr(
        sync_export_pr, "_open_or_update_export_pr", fake_open_or_update_export_pr
    )

    sync_export_pr.run_export_sync(
        replace(
            _export_request(tmp_path),
            source_dir=source_dir,
            public_dir=public_dir,
            skip_source_validation=True,
        )
    )

    assert opened
    assert opened[0].startswith("## Summary\n\nPublic body.")
    assert "Exported template placeholder" not in opened[0]


def test_run_export_sync_skips_auto_merge_when_no_pr(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A no-op export (no PR) must not attempt auto-merge on a missing PR."""
    public_dir = tmp_path / "public"
    source_dir = tmp_path / "source"
    public_dir.mkdir()
    source_dir.mkdir()
    merged: list[str] = []

    def fake_resolve_pr_replay_plan(
        *, request: ExportRequest, pr_template: str
    ) -> sync_export_pr.PrReplayPlan:
        del request, pr_template
        return sync_export_pr.PrReplayPlan(
            state=_pr_state(),
            body="",
            replay_base="",
            replay_base_digest="",
            current_pr=None,
        )

    def fake_export_public_tree(
        *, project: Path, source_dir: Path, export_dir: Path, manifest: Path
    ) -> None:
        del project, source_dir, export_dir, manifest

    def fake_validate_public(
        *,
        public_dir: Path,
        validation_commands: tuple[str, ...],
    ) -> None:
        del public_dir, validation_commands

    def fake_open_or_update_export_pr(
        *, request: ExportRequest, pr_plan: sync_export_pr.PrReplayPlan
    ) -> bool:
        del request, pr_plan
        return False  # No changes and no existing PR.

    def fake_enable_auto_merge(*, request: ExportRequest, pr_title: str) -> None:
        del request, pr_title
        merged.append("merge")

    monkeypatch.setattr(
        sync_export_pr, "_resolve_pr_replay_plan", fake_resolve_pr_replay_plan
    )
    monkeypatch.setattr(sync_export_pr, "_export_public_tree", fake_export_public_tree)
    monkeypatch.setattr(sync_export_pr, "_validate_public", fake_validate_public)
    monkeypatch.setattr(
        sync_export_pr, "_open_or_update_export_pr", fake_open_or_update_export_pr
    )
    monkeypatch.setattr(
        sync_export_pr, "_enable_export_pr_auto_merge", fake_enable_auto_merge
    )

    sync_export_pr.run_export_sync(
        replace(
            _export_request(tmp_path),
            source_dir=source_dir,
            public_dir=public_dir,
            skip_source_validation=True,
            auto_merge=True,
        )
    )

    assert merged == []


def test_enable_auto_merge_falls_back_to_direct_merge_without_protection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """When --auto is rejected (no branch protection), merge the PR directly."""
    request = _export_request(tmp_path)
    calls: list[list[str]] = []

    def fake_run(
        argv: list[str],
        **_: object,
    ) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if "--auto" in argv:
            return subprocess.CompletedProcess(
                argv,
                1,
                stdout="",
                stderr=(
                    "GraphQL: Pull request Protected branch rules not configured "
                    "for this branch (enablePullRequestAutoMerge)"
                ),
            )
        return subprocess.CompletedProcess(argv, 0, stdout="merged\n")

    monkeypatch.setattr(sync_export_pr, "_run", fake_run)

    sync_export_pr._enable_export_pr_auto_merge(request=request, pr_title="T")

    merge_calls = [c for c in calls if c[:3] == ["gh", "pr", "merge"]]
    assert len(merge_calls) == 2  # the --auto attempt, then the direct fallback
    assert "--auto" in merge_calls[0]
    assert "--auto" not in merge_calls[1]


def test_enable_auto_merge_reraises_unrelated_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A non-protection merge failure must still abort the export."""
    request = _export_request(tmp_path)

    def fake_run(
        argv: list[str],
        **_: object,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            argv, 1, stdout="", stderr="GraphQL: some other error"
        )

    monkeypatch.setattr(sync_export_pr, "_run", fake_run)

    with pytest.raises(SystemExit):
        sync_export_pr._enable_export_pr_auto_merge(request=request, pr_title="T")


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
        *, request: ExportRequest, pr_template: str
    ) -> sync_export_pr.PrReplayPlan:
        del request, pr_template
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
        validation_commands: tuple[str, ...],
    ) -> None:
        del validation_commands
        (public_dir / "uv.lock").write_text("validation lock\n", encoding="utf-8")

    def fake_open_or_update_export_pr(
        *, request: ExportRequest, pr_plan: sync_export_pr.PrReplayPlan
    ) -> bool:
        del pr_plan
        lockfile = request.public_dir / "uv.lock"
        opened.append(lockfile.read_text(encoding="utf-8") if lockfile.exists() else "")
        return True

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


def test_dry_run_uses_empty_repo_for_existing_non_git_directory(
    tmp_path: Path,
) -> None:
    public_dir = tmp_path / "public"
    public_dir.mkdir()
    sentinel = public_dir / "source-tree-file.py"
    sentinel.write_text("preserve me\n", encoding="utf-8")

    dry_public_dir = sync_export_pr._clone_public_checkout_for_dry_run(
        public_dir=public_dir
    )
    try:
        assert (dry_public_dir / ".git").is_dir()
        assert sentinel.read_text(encoding="utf-8") == "preserve me\n"
    finally:
        sync_export_pr._delete_path(dry_public_dir)


def test_run_export_sync_dry_run_uses_temp_public_checkout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    public_dir = tmp_path / "public"
    source_dir = tmp_path / "source"
    dry_public_dir = tmp_path / "dry-public"
    public_dir.mkdir()
    source_dir.mkdir()
    (public_dir / "existing.txt").write_text("original\n", encoding="utf-8")
    calls: list[tuple[str, Path]] = []

    def fake_clone_public_checkout_for_dry_run(*, public_dir: Path) -> Path:
        calls.append(("clone", public_dir))
        dry_public_dir.mkdir()
        (dry_public_dir / "existing.txt").write_text("original\n", encoding="utf-8")
        return dry_public_dir

    def fake_resolve_pr_replay_plan(
        *, request: ExportRequest, pr_template: str
    ) -> sync_export_pr.PrReplayPlan:
        calls.append(("resolve", request.public_dir))
        assert pr_template == ""
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
        calls.append(("export", export_dir))
        (export_dir / "exported.txt").write_text("exported\n", encoding="utf-8")

    def fake_validate_public(
        *,
        public_dir: Path,
        validation_commands: tuple[str, ...],
    ) -> None:
        del validation_commands
        calls.append(("validate", public_dir))
        assert (public_dir / "exported.txt").read_text(encoding="utf-8") == "exported\n"

    def fail_open_or_update_export_pr(
        *, request: ExportRequest, pr_plan: sync_export_pr.PrReplayPlan
    ) -> None:
        del request, pr_plan
        raise AssertionError("dry run must not mutate public PR state")

    monkeypatch.setattr(
        sync_export_pr,
        "_clone_public_checkout_for_dry_run",
        fake_clone_public_checkout_for_dry_run,
    )
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
        fail_open_or_update_export_pr,
    )

    sync_export_pr.run_export_sync(
        replace(
            _export_request(tmp_path),
            source_dir=source_dir,
            public_dir=public_dir,
            dry_run=True,
            skip_source_validation=True,
        )
    )

    assert (public_dir / "existing.txt").read_text(encoding="utf-8") == "original\n"
    assert not (public_dir / "exported.txt").exists()
    assert [(name, path) for name, path in calls if name in {"clone", "resolve"}] == [
        ("clone", public_dir),
        ("resolve", dry_public_dir),
    ]
    assert calls[-1][0] == "validate"
    assert calls[-1][1] != public_dir
    assert not dry_public_dir.exists()


def test_run_export_sync_refreshes_public_lockfile_before_validation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    public_dir = tmp_path / "public"
    source_dir = tmp_path / "source"
    public_dir.mkdir()
    source_dir.mkdir()
    calls: list[str] = []

    def fake_resolve_pr_replay_plan(
        *, request: ExportRequest, pr_template: str
    ) -> sync_export_pr.PrReplayPlan:
        del request, pr_template
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
        validation_commands: tuple[str, ...],
    ) -> None:
        del validation_commands
        assert (public_dir / "uv.lock").read_text(encoding="utf-8") == "public lock\n"
        calls.append("validate")

    def fake_open_or_update_export_pr(
        *, request: ExportRequest, pr_plan: sync_export_pr.PrReplayPlan
    ) -> bool:
        del pr_plan
        assert (request.public_dir / "uv.lock").read_text(encoding="utf-8") == (
            "public lock\n"
        )
        calls.append("open")
        return True

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


def test_postleakcheck_validation_runs_ty_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0)

    def fake_basedpyright(*, project: Path, targets: tuple[str, ...]) -> None:
        calls.append(["basedpyright", str(project), *targets])

    monkeypatch.setattr(sync_export_pr, "_run", fake_run)
    monkeypatch.setattr(sync_export_pr, "_run_basedpyright", fake_basedpyright)

    _postleakcheck_validation(project=Path("/repo/pkg"))

    assert [
        "uv",
        "--quiet",
        "--project",
        "/repo/pkg",
        "run",
        "ty",
        "check",
        ".",
    ] in calls
    # Source-side basedpyright targets ``.`` (source layout), not the
    # export-relative type_check_targets.
    assert ["basedpyright", "/repo/pkg", "."] in calls


def test_preleakcheck_validation_runs_lint_not_types(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pre-leak phase runs ruff but defers ty/basedpyright/pytest."""
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0)

    def fake_basedpyright(*, project: Path, targets: tuple[str, ...]) -> None:
        calls.append(["basedpyright", str(project), *targets])

    monkeypatch.setattr(sync_export_pr, "_run", fake_run)
    monkeypatch.setattr(sync_export_pr, "_run_basedpyright", fake_basedpyright)

    _preleakcheck_validation(project=Path("/repo/pkg"))

    verbs = [argv[5] for argv in calls if len(argv) > 5 and argv[4] == "run"]
    assert "ruff" in verbs
    assert "ty" not in verbs
    assert "pytest" not in verbs
    assert not any(argv[0] == "basedpyright" for argv in calls)


def test_validate_public_runs_each_validation_command_via_bash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], Path | None]] = []
    public_dir = Path("/public")

    def fake_run(
        argv: list[str], *, cwd: Path | None = None, **_: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append((argv, cwd))
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(sync_export_pr, "_run", fake_run)

    commands = ("uv sync --all-groups", "uv run pytest")
    _validate_public(public_dir=public_dir, validation_commands=commands)

    assert calls == [
        (["bash", "-c", "uv sync --all-groups"], public_dir),
        (["bash", "-c", "uv run pytest"], public_dir),
    ]


def test_validate_public_runs_nothing_without_commands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(sync_export_pr, "_run", fake_run)

    _validate_public(public_dir=Path("/public"), validation_commands=())

    assert calls == []


def test_pending_import_branches_lists_open_import_prs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard asks GitHub for open import PRs on the source repo."""
    calls: list[list[str]] = []

    def fake_run_gh(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout='[{"number": 68, "headRefName": "trackinizer/import/sha-abc"}]',
        )

    monkeypatch.setattr(sync_export_pr, "_run_gh", fake_run_gh)

    pending = _pending_import_prs(
        prefix="trackinizer/import/", repo="rekursiv-ai/loop", cwd=Path("/src")
    )

    assert pending == ("#68 trackinizer/import/sha-abc",)
    assert calls
    assert calls[0][:3] == ["gh", "pr", "list"]


def test_pending_import_branches_ignores_other_projects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sibling project's import must not block this project's export."""

    def fake_run_gh(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout='[{"number": 70, "headRefName": "madcatter/import/sha-def"}]',
        )

    monkeypatch.setattr(sync_export_pr, "_run_gh", fake_run_gh)

    assert (
        _pending_import_prs(
            prefix="trackinizer/import/", repo="rekursiv-ai/loop", cwd=Path("/src")
        )
        == ()
    )


def test_run_export_sync_skips_while_an_import_is_pending(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A pending import means the source is stale; exporting would revert it.

    The export force-writes the public checkout, so running it while the
    source has not yet absorbed a public change silently reverts that change.
    Skipping leaves the public repo stale instead, which is recoverable.
    """
    request = replace(
        _export_request(tmp_path),
        import_branch_prefix="trackinizer/import/",
        source_repo="rekursiv-ai/loop",
    )
    ran: list[str] = []

    def fake_pending(**_: object) -> tuple[str, ...]:
        return ("#68 trackinizer/import/sha-abc",)

    def fake_run_export_sync(request: ExportRequest) -> None:
        del request
        ran.append("ran")

    monkeypatch.setattr(sync_export_pr, "_pending_import_prs", fake_pending)
    monkeypatch.setattr(sync_export_pr, "_run_export_sync", fake_run_export_sync)

    sync_export_pr.run_export_sync(request)

    assert ran == [], "export must not run while an import PR is open"


def test_run_export_sync_proceeds_with_no_pending_import(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    request = replace(
        _export_request(tmp_path),
        import_branch_prefix="trackinizer/import/",
        source_repo="rekursiv-ai/loop",
    )
    ran: list[str] = []

    def fake_pending(**_: object) -> tuple[str, ...]:
        return ()

    def fake_run_export_sync(request: ExportRequest) -> None:
        del request
        ran.append("ran")

    monkeypatch.setattr(sync_export_pr, "_pending_import_prs", fake_pending)
    monkeypatch.setattr(sync_export_pr, "_run_export_sync", fake_run_export_sync)

    sync_export_pr.run_export_sync(request)

    assert ran == ["ran"]


def test_run_export_sync_skips_when_public_head_is_unimported(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The content check gates the export even with no import PR open."""
    request = replace(
        _export_request(tmp_path),
        import_branch_prefix="sagent/import/",
        source_repo="rekursiv-ai/loop",
        sync_label="Sagent",
    )
    ran: list[str] = []

    def no_pending(**_: object) -> tuple[str, ...]:
        return ()

    def unimported(**_: object) -> str:
        return "bbbb222"

    def fake_run_export_sync(request: ExportRequest) -> None:
        del request
        ran.append("ran")

    monkeypatch.setattr(sync_export_pr, "_pending_import_prs", no_pending)
    monkeypatch.setattr(sync_export_pr, "_public_head_unimported", unimported)
    monkeypatch.setattr(sync_export_pr, "_run_export_sync", fake_run_export_sync)

    sync_export_pr.run_export_sync(request)

    assert ran == [], "a failed import must still block the export"


def test_public_head_unimported_aborts_when_the_ledger_is_unreadable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unanswerable guard must block, not wave the export through.

    Returning "" on error made any transient git failure re-enable the
    force-write -- availability over data safety on the one check whose
    entire job is data safety. A bootstrap (no landed import at all) is
    distinguishable: it raises ImportBaseError, which is not an error.
    """

    def boom(**_: object) -> str:
        raise OSError("git unavailable")

    def head(**_: object) -> str:
        return "bbbb222"

    monkeypatch.setattr(sync_export_pr, "_public_head_sha", head)
    monkeypatch.setattr(sync_export_pr, "last_synced_public_sha", boom)

    with pytest.raises(sync_export_pr.ExportGuardError, match="ledger"):
        _public_head_unimported(
            public_dir=Path("/public"),
            source_dir=Path("/src"),
            sync_label="Sagent",
            base_branch="main",
            sync_user_email="rekursiv-bot@rekursiv.ai",
        )


def test_public_head_unimported_allows_a_bootstrap_with_no_landed_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A first export has nothing to revert, so it must not be blocked."""

    def no_import(**_: object) -> str:
        raise sync_import_change.ImportBaseError("no landed import")

    def head(**_: object) -> str:
        return "bbbb222"

    monkeypatch.setattr(sync_export_pr, "_public_head_sha", head)
    monkeypatch.setattr(sync_export_pr, "last_synced_public_sha", no_import)

    assert (
        _public_head_unimported(
            public_dir=Path("/public"),
            source_dir=Path("/src"),
            sync_label="Sagent",
            base_branch="main",
            sync_user_email="rekursiv-bot@rekursiv.ai",
        )
        == ""
    )


def test_public_head_unimported_ignores_export_bot_commits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Commits the export itself authored are not work to preserve.

    The public head is always AHEAD of the last import (the export's own
    commit lands after it), so an ancestry test reports every project as
    diverged forever. What matters is whether the unimported span contains
    anything the export did not write.
    """

    def head(**_: object) -> str:
        return "bbbb222"

    def synced(**_: object) -> str:
        return "aaaa111"

    def only_bot(**_: object) -> tuple[str, ...]:
        return ()

    monkeypatch.setattr(sync_export_pr, "_public_head_sha", head)
    monkeypatch.setattr(sync_export_pr, "_last_synced_public_sha", synced)
    monkeypatch.setattr(sync_export_pr, "_foreign_commits_since", only_bot)

    assert (
        _public_head_unimported(
            public_dir=Path("/public"),
            source_dir=Path("/src"),
            sync_label="Sagent",
            base_branch="main",
            sync_user_email="rekursiv-bot@rekursiv.ai",
        )
        == ""
    )


def test_public_head_unimported_blocks_on_a_human_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A human commit in the unimported span must stop the force-write."""

    def head(**_: object) -> str:
        return "bbbb222"

    def synced(**_: object) -> str:
        return "aaaa111"

    def has_human(**_: object) -> tuple[str, ...]:
        return ("2c5fefb Release sagent 0.1.13",)

    monkeypatch.setattr(sync_export_pr, "_public_head_sha", head)
    monkeypatch.setattr(sync_export_pr, "_last_synced_public_sha", synced)
    monkeypatch.setattr(sync_export_pr, "_foreign_commits_since", has_human)

    assert (
        _public_head_unimported(
            public_dir=Path("/public"),
            source_dir=Path("/src"),
            sync_label="Sagent",
            base_branch="main",
            sync_user_email="rekursiv-bot@rekursiv.ai",
        )
        == "bbbb222"
    )


def test_foreign_commits_since_reports_an_unreadable_range(tmp_path: Path) -> None:
    """A range git cannot resolve counts as foreign, and says why.

    The public checkout was cloned at depth 1, so `<last-import>..HEAD` was
    unresolvable and this returned "foreign" for every project -- the export
    skipped forever with a message blaming an unimported commit rather than
    the shallow clone. Failing closed is right; failing closed SILENTLY is
    what made it take three rounds to find.
    """
    repo = _git_repo(tmp_path / "repo", [])

    foreign = sync_export_pr._foreign_commits_since(
        base="0" * 40,
        head="HEAD",
        author_email="bot@example.com",
        cwd=repo,
    )

    assert foreign, "an unresolvable range must block the export"
    assert "unreadable" in foreign[0]


def test_foreign_commits_since_separates_bot_from_human(tmp_path: Path) -> None:
    """Only commits the export did not author count as work to preserve."""
    repo = _git_repo(
        tmp_path / "repo",
        [
            ("base", "bot@example.com"),
            ("generated export", "bot@example.com"),
            ("human release", "dev@example.com"),
        ],
    )
    base = subprocess.run(  # noqa: S603 -- fixed argv, test-only.
        ["git", "-C", str(repo), "rev-parse", "HEAD~2"],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    foreign = sync_export_pr._foreign_commits_since(
        base=base,
        head="HEAD",
        author_email="bot@example.com",
        cwd=repo,
    )

    assert [line.split(" ", 1)[1] for line in foreign] == ["human release"]


def _git_repo(root: Path, commits: list[tuple[str, str]]) -> Path:
    """Create a Git repo with one commit per ``(subject, author_email)``."""
    root.mkdir(parents=True, exist_ok=True)

    def run(*args: str) -> None:
        subprocess.run(  # noqa: S603 -- fixed argv, test-only.
            ["git", "-C", str(root), *args],  # noqa: S607
            check=True,
            capture_output=True,
        )

    subprocess.run(  # noqa: S603 -- fixed argv, test-only.
        ["git", "init", "-q", "-b", "main", str(root)],  # noqa: S607
        check=True,
    )
    run("config", "commit.gpgsign", "false")
    run("config", "user.name", "Test")
    run("config", "user.email", "test@example.com")
    for i, (subject, email) in enumerate(commits):
        (root / "f.txt").write_text(f"{i}\n", encoding="utf-8")
        run("add", "f.txt")
        run("-c", f"user.email={email}", "commit", "-q", "-m", subject)
    return root
