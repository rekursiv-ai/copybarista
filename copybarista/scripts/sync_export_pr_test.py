"""Tests for source-to-public sync helpers."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Final, TextIO

import shutil
import subprocess
import sys
import tempfile
import time

from treekle import from_plain

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
    _await_upstream_exports,
    _base_pr_state,
    _commit_author,
    _current_pr,
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
    _refresh_public_lockfile,
    _render_pr_body,
    _replace_pr_state,
    _replace_tree,
    _source_rev_digest,
    _state_from_pr_body,
    _upstream_export_workflows,
    export_branch_name,
    export_pr_text,
    replay_pr_metadata,
)
from copybarista.scripts.sync_import_change_test import FakeGit, RecordingRun
from copybarista.sync_setup import SyncSettings, write_sync_scaffold


if TYPE_CHECKING:
    from collections.abc import Callable


_CWD: Final = Path(__file__).resolve().parent


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
        ],
    )

    assert captured[0].project_path == Path("packages/configgle")
    assert captured[0].release_check_script == Path("scripts/check_release_tree.py")


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
        ],
    )

    assert captured[0].dry_run


def test_main_derives_request_from_project_positional(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
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
    assert request.forbidden_pr_text == ("src",)
    assert request.refresh_public_lockfile
    assert request.sync_label == "Sagent"
    assert request.replay_settings.scope == "sagent"


def test_main_explicit_flag_overrides_sync_setting(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
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

    sync_export_pr.run(
        [str(project_dir), "--dry-run", "--sync-label", "Alt"],
    )

    assert captured[0].sync_label == "Alt"


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
        ],
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
        ],
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
        ],
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
        ],
    )

    assert captured[0].skip_source_validation


def test_parse_pr_metadata_accepts_append_body():
    patches = _parse_pr_metadata_log(
        _metadata_log(
            "Prepare public export\n\n"
            "Copybarista-PR-Title: Public title\n"
            "Copybarista-PR-Body-Mode: append\n"
            "Copybarista-PR-Body:\n"
            "Public body text.\n",
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
        ],
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
                "Copybarista-PR-Author: Public author\n",
            ),
            forbidden_text=(),
        )


def test_parse_pr_metadata_skips_unknown_field(
    capsys: pytest.CaptureFixture[str],
):
    patches = _parse_pr_metadata_log(
        _metadata_log(
            "Copybarista-PR-Description: typo'd field\n"
            "Copybarista-PR-Title: Public title\n",
        ),
        forbidden_text=(),
    )

    assert patches[0].title == "Public title"
    assert "Description: unknown field; ignored" in capsys.readouterr().err


def test_parse_pr_metadata_rejects_duplicate_title():
    with pytest.raises(sync_export_pr.PrMetadataError, match=r"abcdef1.*Title"):
        _parse_pr_metadata_log(
            _metadata_log(
                "Copybarista-PR-Title: First\nCopybarista-PR-Title: Second\n",
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
            "Configgle body.\n",
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
            "Sagent body.\n",
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
            "Second squashed commit: touches TrainLoop.Config internals.\n",
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


def test_parse_pr_metadata_drops_unscrubbable_text_instead_of_failing():
    """An unscrubbable description is DROPPED; the export still runs.

    Halting was the wrong trade. The body is a human description replayed into
    the public PR: nothing downstream depends on it, and a pushed commit
    message cannot be edited, so a single bad word used to wedge every
    subsequent export until someone hand-advanced the replay base. Dropping
    the field prevents the leak just as completely and costs only prose.
    """
    patches = _parse_pr_metadata_log(
        _metadata_log("Copybarista-PR-Body:\nMentions private-source.\n"),
        forbidden_text=("private-source",),
    )

    assert tuple(p.body for p in patches) == ("",)


def test_parse_pr_metadata_rewrites_source_path_via_literal_transform():
    # A PR body referencing a source path is REWRITTEN to its public form by the
    # same export replace-transform, then passes the leak check -- rather than the
    # author having to hand-scrub the token per commit. Regression: a private
    # module prefix in the body used to hard-fail the export.
    # (Synthetic ``priv``/``pub`` tokens keep this test's own text export-safe.)
    transforms = (
        Transform(
            id="lib",
            type="replace",
            path="**",
            before="priv.mod",
            after="pub.mod",
        ),
    )
    patches = _parse_pr_metadata_log(
        _metadata_log(
            "Copybarista-PR-Title: Fix import path\n"
            "Copybarista-PR-Body:\n"
            "Use submodule path `priv.mod.testing.main`.\n",
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
            "Copybarista-PR-Body:\nBody text.\n",
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
            id="lib",
            type="replace",
            path="**",
            before="priv.mod",
            after="pub.mod",
        ),
    )
    patches = _parse_pr_metadata_log(
        _metadata_log("Copybarista-PR-Body:\nMentions private-org/secret repo.\n"),
        forbidden_text=("private-org/secret",),
        text_transforms=transforms,
    )

    assert tuple(p.body for p in patches) == ("",)


def test_prefix_term_ignores_prose_word():
    # A dotted/path prefix term must not trip on the same word ending a sentence.
    patches = _parse_pr_metadata_log(
        _metadata_log(
            "Copybarista-PR-Title: t\n"
            "Copybarista-PR-Body:\nReimplementing the package. Also uses package/ "
            "style prose here.\n",
        ),
        forbidden_text=("package.", "package/"),
    )
    assert tuple(p.body for p in patches) == (
        "Reimplementing the package. Also uses package/ style prose here.",
    )


def test_prefix_term_rejects_dotted_import():
    patches = _parse_pr_metadata_log(
        _metadata_log("Copybarista-PR-Body:\nImport package.module.agent.\n"),
        forbidden_text=("package.",),
    )

    assert tuple(p.body for p in patches) == ("",)


def test_prefix_term_rejects_path():
    patches = _parse_pr_metadata_log(
        _metadata_log("Copybarista-PR-Body:\nSee package/lib/web for details.\n"),
        forbidden_text=("package/",),
    )

    assert tuple(p.body for p in patches) == ("",)


def test_non_prefix_term_still_substring_matches():
    # A term without a trailing separator keeps plain substring matching, so a
    # trailing period does not shield it.
    patches = _parse_pr_metadata_log(
        _metadata_log("Copybarista-PR-Body:\nMentions company/source.\n"),
        forbidden_text=("company/source",),
    )

    assert tuple(p.body for p in patches) == ("",)


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
        "reimplementing the walk loop.",  # sentence-final period.
        "the walk loop. Eliminates bugs",  # Period + space.
        "uses loop/ style prose",  # Slash + space.
        "the event loop",  # No separator at all.
        "backtick ``loop.``",  # Separator then closing backtick.
        "(see the loop.)",  # Separator then closing paren.
        "the loop/",  # Slash at end-of-string.
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
        *,
        branch: str,
        repo: str,
        cwd: Path,
    ) -> sync_export_pr.CurrentPr | None:
        del branch, repo, cwd
        return None

    def fake_branch_markers(
        *,
        branch: str,
        cwd: Path,
    ) -> sync_export_pr.BranchMarkers:
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
        *,
        source_dir: Path,
        marker: str,
        marker_source: str,
    ) -> str:
        calls.append(f"{source_dir}|{marker_source}:{marker}")
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
    assert calls == [f"{tmp_path}|generated branch source marker:sha256:applied-digest"]


def test_branch_without_source_marker_replays_after_its_replay_base(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[tuple[Path, str, str]] = []

    def resolve(*, source_dir: Path, marker: str, marker_source: str) -> str:
        calls.append((source_dir, marker, marker_source))
        return "migration-base"

    monkeypatch.setattr(sync_export_pr, "_resolve_source_marker", resolve)

    assert (
        sync_export_pr._replay_base(
            request=_export_request(tmp_path),
            current_source_rev="source-head",
            current_pr=None,
            branch_markers=sync_export_pr.BranchMarkers(
                source_digest="",
                replay_base_digest="base-digest",
                exists=True,
            ),
        )
        == "migration-base"
    )
    assert calls == [
        (tmp_path, "sha256:base-digest", "generated branch replay-base marker"),
    ]


def test_resolved_replay_base_resolves_a_branch_replay_base_marker(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The branch's replay-base digest resolves in the source, named as that marker."""
    calls: list[tuple[Path, str, str]] = []

    def resolve(*, source_dir: Path, marker: str, marker_source: str) -> str:
        calls.append((source_dir, marker, marker_source))
        return "migration-base"

    monkeypatch.setattr(sync_export_pr, "_resolve_source_marker", resolve)

    assert (
        sync_export_pr._resolved_replay_base(
            request=_export_request(tmp_path),
            current_source_rev="source-head",
            current_pr=None,
            branch_markers=sync_export_pr.BranchMarkers(
                source_digest="",
                replay_base_digest="base-digest",
                exists=True,
            ),
        )
        == "migration-base"
    )
    assert calls == [
        (tmp_path, "sha256:base-digest", "generated branch replay-base marker"),
    ]


def test_resolved_replay_base_names_the_migration_for_an_unmarked_pr(
    tmp_path: Path,
) -> None:
    with pytest.raises(
        sync_export_pr.PrReplayError,
        match=(
            r"^Existing generated PR has no Copybarista replay marker\. Branch: "
            r"copybarista/export/main\. Configure replay_bootstrap_base or rerun "
            r"with manual PR text once to migrate the PR\.$"
        ),
    ):
        sync_export_pr._resolved_replay_base(
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


@pytest.mark.parametrize(
    ("bootstrap_base", "expected"),
    [("bootstrap-base", "bootstrap-base"), ("", "parent-of-source-head")],
)
def test_unmarked_branch_replays_after_bootstrap_else_the_parent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    bootstrap_base: str,
    expected: str,
) -> None:
    def parent(*, source_dir: Path, rev: str) -> str:
        assert source_dir == tmp_path
        return f"parent-of-{rev}"

    monkeypatch.setattr(sync_export_pr, "_source_parent", parent)
    request = _export_request(tmp_path)
    request = replace(
        request,
        replay_settings=replace(request.replay_settings, bootstrap_base=bootstrap_base),
    )

    assert (
        sync_export_pr._resolved_replay_base(
            request=request,
            current_source_rev="source-head",
            current_pr=None,
            branch_markers=sync_export_pr.BranchMarkers(
                source_digest="",
                replay_base_digest="",
                exists=True,
            ),
        )
        == expected
    )


def test_unmarked_pr_replays_after_bootstrap(tmp_path: Path) -> None:
    request = _export_request(tmp_path)
    request = replace(
        request,
        replay_settings=replace(request.replay_settings, bootstrap_base="boot"),
    )

    assert (
        sync_export_pr._resolved_replay_base(
            request=request,
            current_source_rev="source-head",
            current_pr=sync_export_pr.CurrentPr(
                title="Old title",
                body="Old body.",
                number=7,
                url="https://example.test/pr/7",
            ),
            branch_markers=sync_export_pr.BranchMarkers(
                source_digest="digest",
                replay_base_digest="",
                exists=True,
            ),
        )
        == "boot"
    )


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
        *,
        source_dir: Path,
        marker: str,
        marker_source: str,
    ) -> str:
        del marker, marker_source
        assert source_dir == tmp_path
        return "old-marked-base"

    def fake_is_ancestor(*, source_dir: Path, ancestor: str, rev: str) -> bool:
        assert source_dir == tmp_path
        # The resolved marker base is an ancestor of the bootstrap base.
        return ancestor == "old-marked-base" and rev == "bootstrap-base"

    monkeypatch.setattr(
        sync_export_pr,
        "_resolve_source_marker",
        fake_resolve_source_marker,
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
    with pytest.raises(
        sync_export_pr.PrReplayError,
        match=(
            r"^Existing generated PR has no Copybarista replay marker\. Branch: "
            r"copybarista/export/main\. Configure replay_bootstrap_base or rerun "
            r"with manual PR text once to migrate the PR\.$"
        ),
    ):
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


def test_existing_pr_marker_takes_precedence_over_branch_marker(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def resolve(*, source_dir: Path, marker: str, marker_source: str) -> str:
        assert source_dir == tmp_path
        assert marker == "sha256:pr-digest"
        assert marker_source == "PR applied marker"
        return "pr-source"

    monkeypatch.setattr(sync_export_pr, "_resolve_source_marker", resolve)
    assert (
        sync_export_pr._replay_base(
            request=_export_request(tmp_path),
            current_source_rev="source-head",
            current_pr=sync_export_pr.CurrentPr(
                title="Public title",
                body="<!-- copybarista:pr-state version=1 applied=sha256:pr-digest -->\n",
                number=7,
                url="https://example.test/pr/7",
            ),
            branch_markers=sync_export_pr.BranchMarkers(
                source_digest="branch-digest",
                replay_base_digest="",
                exists=True,
            ),
        )
        == "pr-source"
    )


def test_release_tree_check_runs_the_project_policy_against_the_export(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[list[str]] = []

    def run(argv: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(sync_export_pr, "_run", run)
    sync_export_pr._check_release_tree(
        project=tmp_path / "source",
        root=tmp_path / "export",
        script=Path("scripts/check_release_tree.py"),
    )
    assert calls == [
        [
            sys.executable,
            str(tmp_path / "source/scripts/check_release_tree.py"),
            str(tmp_path / "export"),
        ],
    ]


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
        encoding="utf-8",
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


def test_an_export_pr_merged_mid_run_stays_in_the_export_branch_history(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The branch keeps the merged squash's tree, which the export guard reads.

    Auto-merge squashed the open export PR 44s into a run that cloned main before it;
    the run force-pushed a branch built on the stale main, the squash's tree left the
    branch history, and the guard blocked every later export as unimported work. Here
    main moves during the push: the export is rebuilt on it and pushed again.
    ``private/sync_export_pr_git_parity.py`` runs the same export with real git.
    """
    git = _ScriptedGit(
        cwd=tmp_path,
        left=[
            *_pushed_before_main_moved(tmp_path),
            (["git", "reset", "--soft", "refs/remotes/origin/main"], 0),
            (["git", "diff", "--cached", "--quiet"], 1),
            _committed(tmp_path),
            *_pushed(holds_main=True),
        ],
    )
    monkeypatch.setattr(sync_export_pr, "_run", git)
    created: list[list[str]] = []
    _stub_pr_calls(monkeypatch, created)

    assert _open_or_update_export_pr(
        request=_export_request(tmp_path),
        pr_plan=_merged_pr_plan(),
    )

    assert git.left == []
    assert [argv[2] for argv in created] == ["create"]


def test_an_export_main_already_holds_opens_no_pr(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Rebuilt on main, the export changes nothing: the branch is main, and no PR.

    The merged export branch was deleted, so fetching it fails, and goes on.
    """
    git = _ScriptedGit(
        cwd=tmp_path,
        left=[
            *_pushed_before_main_moved(tmp_path, fetched=128),
            (["git", "reset", "--soft", "refs/remotes/origin/main"], 0),
            (["git", "diff", "--cached", "--quiet"], 0),
            _pushed(holds_main=True)[0],
        ],
    )
    monkeypatch.setattr(sync_export_pr, "_run", git)
    created: list[list[str]] = []
    _stub_pr_calls(monkeypatch, created)

    assert not _open_or_update_export_pr(
        request=_export_request(tmp_path),
        pr_plan=_merged_pr_plan(),
    )

    assert git.left == []
    assert created == []


@dataclass(slots=True, kw_only=True)
class _ScriptedGit:
    """Stands in for ``_run``: answers each git command of ``left`` in turn, only."""

    cwd: Path
    left: list[tuple[list[str], int]]

    def __call__(
        self,
        argv: list[str],
        *,
        cwd: Path | None = None,
        check: bool = True,
        capture: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        assert self.left, f"unexpected git command {argv}"
        (expected, code), *self.left = self.left
        assert (argv, cwd) == (expected, self.cwd)
        # ``_run`` exits on a failure it checks; a step that fails is never checked.
        assert code == 0 or not check, argv
        if not capture:
            # Uncaptured, ``_run`` leaves the output to the job's log.
            return subprocess.CompletedProcess(argv, code)
        stdout = " M f.txt\n" if argv[1:2] == ["status"] else ""
        return subprocess.CompletedProcess(argv, code, stdout=stdout)


def _pushed_before_main_moved(
    cwd: Path,
    *,
    fetched: int = 0,
) -> list[tuple[list[str], int]]:
    """Answer the export's commit and its first push, which main moved past."""
    branch = "copybarista/export/main"
    return [
        (["git", "status", "--porcelain"], 0),
        (["git", "config", "user.name", "copybarista"], 0),
        (["git", "config", "user.email", "copybarista@example.com"], 0),
        (
            [
                "git",
                "fetch",
                "origin",
                f"+refs/heads/{branch}:refs/remotes/origin/{branch}",
            ],
            fetched,
        ),
        (["git", "switch", "-C", branch], 0),
        (["git", "add", "-A"], 0),
        _committed(cwd),
        *_pushed(holds_main=False),
    ]


def _committed(cwd: Path) -> tuple[list[str], int]:
    """Answer the export commit, its message in ``cwd``."""
    return (
        [
            "git",
            "commit",
            "--author",
            "copybarista <copybarista@example.com>",
            "--file",
            str(cwd / "copybarista-commit-message.txt"),
        ],
        0,
    )


def _pushed(*, holds_main: bool) -> list[tuple[list[str], int]]:
    """Answer a push, the fetch of main after it, and whether the branch holds main."""
    return [
        (["git", "push", "--force-with-lease", "origin", "copybarista/export/main"], 0),
        (
            [
                "git",
                "fetch",
                "--no-tags",
                "origin",
                "+refs/heads/main:refs/remotes/origin/main",
            ],
            0,
        ),
        (
            ["git", "merge-base", "--is-ancestor", "refs/remotes/origin/main", "HEAD"],
            0 if holds_main else 1,
        ),
    ]


def _stub_pr_calls(monkeypatch: pytest.MonkeyPatch, created: list[list[str]]) -> None:
    """Make the merged PR read as closed, and record every other ``gh`` call."""

    def no_open_pr(*, branch: str, repo: str, cwd: Path) -> bool:
        del branch, repo, cwd
        return False

    def run_gh(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        created.append(argv)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(sync_export_pr, "_gh_pr_exists", no_open_pr)
    monkeypatch.setattr(sync_export_pr, "_run_gh", run_gh)


def _merged_pr_plan() -> sync_export_pr.PrReplayPlan:
    """Return a plan that still names the export PR the run saw open."""
    return sync_export_pr.PrReplayPlan(
        state=_pr_state(),
        body="body\n",
        replay_base="",
        replay_base_digest="",
        current_pr=sync_export_pr.CurrentPr(
            title="Public title",
            body="body\n",
            number=422,
            url="https://example.test/pr/422",
        ),
    )


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


def test_replay_deduplicates_entries_and_preserves_applied_provenance():
    author = SourceAuthor(name="Author", email="author@example.com")
    patch = PrMetadataPatch(
        commit_sha="new",
        scope="",
        title="",
        author=author,
        body="New update.",
        body_mode="append",
    )
    base = replace(
        _pr_state(),
        body_entries=(PrBodyEntry(commit_sha="old", text="Old update."),),
    )
    state = replay_pr_metadata(
        base=base,
        patches=(
            replace(patch, commit_sha="old", body="Old update."),
            patch,
            patch,
            replace(patch, body=""),
        ),
    )
    assert state == replace(
        base,
        authors=(author,),
        body_entries=(
            *base.body_entries,
            PrBodyEntry(commit_sha="new", text="New update."),
        ),
        metadata_count=4,
    )


def test_replay_replacement_allows_reappending_a_discarded_entry():
    author = SourceAuthor(name="Author", email="author@example.com")
    patch = PrMetadataPatch(
        commit_sha="old",
        scope="",
        title="",
        author=author,
        body="Old update.",
        body_mode="append",
    )
    base = replace(
        _pr_state(),
        body_entries=(PrBodyEntry(commit_sha="old", text="Old update."),),
    )
    state = replay_pr_metadata(
        base=base,
        patches=(replace(patch, body="Replacement.", body_mode="replace"), patch),
    )
    assert state == replace(
        base,
        authors=(author,),
        body_intro="Replacement.",
        metadata_count=2,
    )


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


def test_current_pr_reads_the_open_generated_pr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gh = _OpenPrGh(view='{"number": 7, "title": "T", "body": "B", "url": "u"}')
    monkeypatch.setattr(sync_export_pr, "_run", gh.run)

    assert _current_pr(
        branch="copybarista/export/main",
        repo="rekursiv-ai/copybarista",
        cwd=Path.cwd(),
    ) == sync_export_pr.CurrentPr(title="T", body="B", number=7, url="u")
    assert [argv[:3] for argv in gh.ran] == [["gh", "pr", "list"], ["gh", "pr", "view"]]


def test_current_pr_is_none_without_an_open_pr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gh = _OpenPrGh(view="", listed="[]")
    monkeypatch.setattr(sync_export_pr, "_run", gh.run)

    assert (
        _current_pr(
            branch="copybarista/export/main",
            repo="rekursiv-ai/copybarista",
            cwd=Path.cwd(),
        )
        is None
    )
    assert [argv[:3] for argv in gh.ran] == [["gh", "pr", "list"]]


@pytest.mark.parametrize(
    ("view", "message"),
    [
        ("[]", "is not a mapping"),
        ('{"number": "7"}', "is not an integer"),
    ],
)
def test_current_pr_rejects_a_malformed_pr_state(
    monkeypatch: pytest.MonkeyPatch,
    view: str,
    message: str,
) -> None:
    monkeypatch.setattr(sync_export_pr, "_run", _OpenPrGh(view=view).run)

    with pytest.raises(sync_export_pr.PrReplayError, match=message):
        _current_pr(
            branch="copybarista/export/main",
            repo="rekursiv-ai/copybarista",
            cwd=Path.cwd(),
        )


@dataclass(slots=True, kw_only=True)
class _OpenPrGh:
    """Answer the two ``gh pr`` reads ``_current_pr`` makes; refuse anything else."""

    view: str
    listed: str = '[{"number": 7}]'
    ran: list[list[str]] = field(default_factory=list)

    def run(
        self,
        argv: list[str],
        *,
        cwd: Path,
        check: bool,
        capture: bool,
        env: dict[str, str] | None,
    ) -> subprocess.CompletedProcess[str]:
        del cwd, env
        if check or not capture:
            raise AssertionError(f"gh ran with check={check}, capture={capture}")
        self.ran.append(argv)
        if argv[:3] == ["gh", "pr", "list"]:
            return subprocess.CompletedProcess(argv, 0, stdout=self.listed, stderr="")
        if argv[:3] == ["gh", "pr", "view"] and argv[-1] == "number,title,body,url":
            return subprocess.CompletedProcess(argv, 0, stdout=self.view, stderr="")
        raise AssertionError(f"_OpenPrGh does not model {argv}")


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

    def no_sleep(seconds: float) -> None:
        del seconds

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

    def no_sleep(seconds: float) -> None:
        del seconds

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
        *,
        request: ExportRequest,
        pr_template: str,
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
        *,
        project: Path,
        source_dir: Path,
        export_dir: Path,
        manifest: Path,
    ) -> None:
        del project, source_dir, manifest
        (export_dir / ".github").mkdir()
        (export_dir / ".github/PULL_REQUEST_TEMPLATE.md").write_text(
            "## Summary\n\nExported template placeholder.\n",
            encoding="utf-8",
        )
        (export_dir / "pkg").mkdir()
        (export_dir / "pkg/module.py").write_text("source\n", encoding="utf-8")

    def fake_open_or_update_export_pr(
        *,
        request: ExportRequest,
        pr_plan: sync_export_pr.PrReplayPlan,
    ) -> bool:
        del request
        opened.append(pr_plan.body)
        return True

    monkeypatch.setattr(
        sync_export_pr,
        "_resolve_pr_replay_plan",
        fake_resolve_pr_replay_plan,
    )
    monkeypatch.setattr(
        sync_export_pr,
        "_export_public_tree",
        fake_export_public_tree,
    )
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
        ),
    )

    assert opened
    assert opened[0].startswith("## Summary\n\nPublic body.")
    assert "Exported template placeholder" not in opened[0]


@pytest.mark.parametrize("pr_open", [True, False], ids=["pr", "no-pr"])
def test_run_export_sync_merges_then_waits_on_public_ci_only_with_a_pr(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    pr_open: bool,
) -> None:
    """A no-op export (no PR) must not attempt auto-merge on a missing PR."""
    public_dir = tmp_path / "public"
    source_dir = tmp_path / "source"
    public_dir.mkdir()
    source_dir.mkdir()
    merged: list[str] = []

    def fake_resolve_pr_replay_plan(
        *,
        request: ExportRequest,
        pr_template: str,
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
        *,
        project: Path,
        source_dir: Path,
        export_dir: Path,
        manifest: Path,
    ) -> None:
        del project, source_dir, export_dir, manifest

    def fake_open_or_update_export_pr(
        *,
        request: ExportRequest,
        pr_plan: sync_export_pr.PrReplayPlan,
    ) -> bool:
        del request, pr_plan
        return pr_open

    def fake_enable_auto_merge(*, request: ExportRequest, pr_title: str) -> None:
        del request, pr_title
        merged.append("merge")

    def fake_await_public_ci(*, request: ExportRequest) -> None:
        del request
        merged.append("public ci")

    monkeypatch.setattr(
        sync_export_pr,
        "_resolve_pr_replay_plan",
        fake_resolve_pr_replay_plan,
    )
    monkeypatch.setattr(
        sync_export_pr,
        "_export_public_tree",
        fake_export_public_tree,
    )
    monkeypatch.setattr(
        sync_export_pr,
        "_open_or_update_export_pr",
        fake_open_or_update_export_pr,
    )
    monkeypatch.setattr(
        sync_export_pr,
        "_enable_export_pr_auto_merge",
        fake_enable_auto_merge,
    )
    monkeypatch.setattr(sync_export_pr, "_await_public_ci", fake_await_public_ci)

    sync_export_pr.run_export_sync(
        replace(
            _export_request(tmp_path),
            source_dir=source_dir,
            public_dir=public_dir,
            skip_source_validation=True,
            auto_merge=True,
        ),
    )

    # A PR merges, then the export waits on public CI; no PR, nothing to wait on.
    assert merged == (["merge", "public ci"] if pr_open else [])


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
    assert len(merge_calls) == 2  # The --auto attempt, then the direct fallback.
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
            argv,
            1,
            stdout="",
            stderr="GraphQL: some other error",
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
        *,
        request: ExportRequest,
        pr_template: str,
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
        *,
        project: Path,
        source_dir: Path,
        export_dir: Path,
        manifest: Path,
    ) -> None:
        del project, source_dir, manifest
        (export_dir / "pkg").mkdir()
        (export_dir / "pkg/module.py").write_text("source\n", encoding="utf-8")

    def fake_open_or_update_export_pr(
        *,
        request: ExportRequest,
        pr_plan: sync_export_pr.PrReplayPlan,
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
    monkeypatch.setattr(
        sync_export_pr,
        "_export_public_tree",
        fake_export_public_tree,
    )
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
        ),
    )

    assert opened == [""]
    assert not (public_dir / "uv.lock").exists()


def test_dry_run_uses_empty_repo_for_existing_non_git_directory(
    git: FakeGit,
    tmp_path: Path,
) -> None:
    public_dir = tmp_path / "public"
    public_dir.mkdir()
    sentinel = public_dir / "source-tree-file.py"
    sentinel.write_text("preserve me\n", encoding="utf-8")

    dry_public_dir = sync_export_pr._clone_public_checkout_for_dry_run(
        public_dir=public_dir,
    )
    try:
        assert (dry_public_dir / ".git").is_dir()
        assert sentinel.read_text(encoding="utf-8") == "preserve me\n"
        (initial,) = git.repos[dry_public_dir.resolve()].refs["refs/heads/main"]
        assert initial.message == "initial\n"
    finally:
        sync_export_pr._delete_path(dry_public_dir)


def test_delete_path_ignores_missing_child_during_recursive_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "checkout"
    path.mkdir()

    def missing_child(path: Path) -> None:
        del path
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr(shutil, "rmtree", missing_child)

    sync_export_pr._delete_path(path)


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
        *,
        request: ExportRequest,
        pr_template: str,
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
        *,
        project: Path,
        source_dir: Path,
        export_dir: Path,
        manifest: Path,
    ) -> None:
        del project, source_dir, manifest
        calls.append(("export", export_dir))
        (export_dir / "exported.txt").write_text("exported\n", encoding="utf-8")

    def fail_open_or_update_export_pr(
        *,
        request: ExportRequest,
        pr_plan: sync_export_pr.PrReplayPlan,
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
    monkeypatch.setattr(
        sync_export_pr,
        "_export_public_tree",
        fake_export_public_tree,
    )
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
        ),
    )

    assert (public_dir / "existing.txt").read_text(encoding="utf-8") == "original\n"
    assert not (public_dir / "exported.txt").exists()
    assert [(name, path) for name, path in calls if name in {"clone", "resolve"}] == [
        ("clone", public_dir),
        ("resolve", dry_public_dir),
    ]
    assert calls[-1][0] == "export"
    assert not dry_public_dir.exists()


def test_refresh_public_lockfile_upgrades_git_branch_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A branch-tracking dependency re-resolves to branch HEAD on every export.

    Plain ``uv lock`` keeps the SHA already in the lockfile, and the export
    installs it with ``uv sync --frozen`` -- so without the upgrade flags the
    public package ships against a sibling's stale commit forever.
    """
    public_dir = tmp_path / "public"
    public_dir.mkdir()
    (public_dir / "pyproject.toml").write_text(
        "[project]\n"
        'name = "sagent"\n'
        "\n"
        "[tool.uv.sources]\n"
        'wesearch = { git = "https://example.invalid/wesearch", branch = "main" }\n'
        'pinned = { git = "https://example.invalid/pinned", rev = "abc123" }\n'
        'local = { path = "../local" }\n',
        encoding="utf-8",
    )
    argv: list[list[str]] = []

    def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        argv.append(command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(sync_export_pr, "_run", fake_run)

    _refresh_public_lockfile(public_dir=public_dir, seed=tmp_path / "absent.lock")

    assert argv == [["uv", "lock", "--upgrade-package", "wesearch"]]
    assert not (public_dir / "uv.lock").exists()


def test_refresh_public_lockfile_without_git_sources_runs_plain_lock(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    public_dir = tmp_path / "public"
    public_dir.mkdir()
    (public_dir / "pyproject.toml").write_text(
        '[project]\nname = "configgle"\n',
        encoding="utf-8",
    )
    argv: list[list[str]] = []

    def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        argv.append(command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(sync_export_pr, "_run", fake_run)
    seed = tmp_path / "source-uv.lock"
    seed.write_text("source pins\n", encoding="utf-8")

    _refresh_public_lockfile(public_dir=public_dir, seed=seed)

    assert argv == [["uv", "lock"]]
    # Seeded: ``uv lock`` keeps the source's pins for every shared package.
    assert (public_dir / "uv.lock").read_text(encoding="utf-8") == "source pins\n"


def test_upstream_exports_are_the_sibling_exports_the_lock_tracks(
    tmp_path: Path,
) -> None:
    """A branch-tracking source on a sibling's public repo names its export."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        "[tool.uv.sources]\n"
        'treekle = { git = "https://github.com/rekursiv-ai/treekle", branch = "main" }\n'
        'stubs = { git = "https://github.com/rekursiv-ai/typings", branch = "main" }\n'
        'pinned = { git = "https://github.com/rekursiv-ai/wesearch", rev = "abc" }\n'
        'other = { git = "https://example.invalid/other", branch = "main" }\n',
        encoding="utf-8",
    )

    # The public copy of this test has no monorepo, so it builds the source tree.
    source_dir = tmp_path / "source"
    for root, repo in (
        ("src/treekle", "treekle"),
        ("src/typings", "typings"),
        ("src/labs/sagent", "sagent"),
        # Deeper than a package sits: a doc fixture, not a sibling export.
        ("src/tool/examples/demo", "treekle"),
    ):
        name = root.rsplit("/", 1)[-1]
        write_sync_scaffold(
            root=source_dir / root,
            settings=SyncSettings(
                package_name=name,
                sync_label=name.title(),
                source_root=root,
                public_repo=f"rekursiv-ai/{repo}",
                source_repo="example/source",
                copybarista_project_path="src/tool",
                forbidden_pr_text=("private",),
            ),
        )

    workflows = _upstream_export_workflows(pyproject=pyproject, source_dir=source_dir)

    assert workflows == ("loop-to-treekle.yml", "loop-to-typings.yml")


def _public_ci(
    monkeypatch: pytest.MonkeyPatch,
    *,
    prs: list[str],
    runs: list[str],
) -> list[list[str]]:
    """Fake ``gh``: each ``pr view`` and ``run list`` answers its next reply."""
    replies = {"pr": iter(prs), "run": iter(runs)}
    calls: list[list[str]] = []

    def fake_run_gh(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout=next(replies[argv[1]]))

    monkeypatch.setattr(sync_export_pr, "_run_gh", fake_run_gh)
    monkeypatch.setattr(time, "sleep", _no_sleep)
    return calls


def _no_sleep(seconds: float) -> None:
    del seconds


_MERGED: Final = '{"state": "MERGED", "mergeCommit": {"oid": "c0ffee"}}'


def test_the_export_waits_for_public_ci_on_what_it_merged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The export reports what public CI says, not just that it merged.

    It went green on merging alone, so sagent's exports failed public CI for a
    day with every export run green.
    """
    calls = _public_ci(
        monkeypatch,
        prs=['{"state": "OPEN", "mergeCommit": null}', _MERGED],
        runs=[
            "[]",
            '[{"status": "in_progress", "conclusion": ""}]',
            '[{"status": "completed", "conclusion": "success"}]',
        ],
    )

    sync_export_pr._await_public_ci(request=_export_request(tmp_path))

    run_lists = [argv for argv in calls if argv[1] == "run"]
    assert {"c0ffee", "package-validation.yml", "example/public"} <= set(run_lists[0])


@pytest.mark.parametrize(
    ("replies", "says"),
    [
        (
            ([_MERGED], ['[{"status": "completed", "conclusion": "failure"}]']),
            "failure",
        ),
        ((['{"state": "CLOSED", "mergeCommit": null}'], list[str]()), "closed"),
    ],
    ids=["ci-failed", "pr-closed"],
)
def test_the_export_fails_when_public_ci_does_not_pass(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    replies: tuple[list[str], list[str]],
    says: str,
) -> None:
    prs, runs = replies
    _public_ci(monkeypatch, prs=prs, runs=runs)

    with pytest.raises(SystemExit):
        sync_export_pr._await_public_ci(request=_export_request(tmp_path))

    assert says in capsys.readouterr().out


def _run_lists(
    monkeypatch: pytest.MonkeyPatch,
    *,
    first: subprocess.CompletedProcess[str],
) -> list[list[str]]:
    """Fake ``gh run list``: ``first`` answers once, then a passed run does."""
    replies = iter(
        [
            first,
            subprocess.CompletedProcess(
                [],
                0,
                stdout='[{"status": "completed", "conclusion": "success"}]',
                stderr="",
            ),
        ],
    )
    calls: list[list[str]] = []

    def fake_run(
        argv: list[str],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        assert kwargs["cwd"] == Path("/public")
        calls.append(argv)
        return next(replies)

    monkeypatch.setattr(sync_export_pr, "_run", fake_run)
    monkeypatch.setattr(time, "sleep", _no_sleep)
    return calls


_SHA: Final = "0123456789abcdef0123456789abcdef01234567"
_RUN_LIST: Final = [
    "gh",
    "run",
    "list",
    "--repo",
    "example/public",
    "--commit",
    _SHA,
    "--workflow",
    "package-validation.yml",
    "--json",
    "status,conclusion",
]


def test_public_ci_names_each_conclusion_that_failed(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _run_lists(
        monkeypatch,
        first=subprocess.CompletedProcess(
            [],
            0,
            stdout='[{"status": "completed", "conclusion": "failure"},'
            ' {"status": "completed", "conclusion": "cancelled"}]',
            stderr="",
        ),
    )

    with pytest.raises(SystemExit):
        sync_export_pr._await_ci(repo="example/public", sha=_SHA, cwd=Path("/public"))

    assert capsys.readouterr().out == (
        "::error::public CI on example/public@0123456789ab: failure, cancelled\n"
    )


def test_public_ci_names_a_listing_that_is_not_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _run_lists(
        monkeypatch,
        first=subprocess.CompletedProcess([], 0, stdout="not json", stderr=""),
    )

    with pytest.raises(
        sync_export_pr.PrReplayError,
        match=r"^public CI runs was not valid JSON\.$",
    ):
        sync_export_pr._await_ci(repo="example/public", sha=_SHA, cwd=Path("/public"))


def test_public_ci_waits_for_a_workflow_the_merge_just_added(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """GitHub lists a workflow's runs only once it has read it from the new main.

    The first website export merged the commit adding its CI workflow, asked for
    that workflow's runs a second later, and failed on the 404 that answered.
    """
    calls = _run_lists(
        monkeypatch,
        first=subprocess.CompletedProcess(
            [],
            1,
            stdout="",
            stderr="HTTP 404: workflow package-validation.yml not found on the "
            "default branch (https://api.github.com/repos/o/r/actions/workflows/"
            "package-validation.yml)\n",
        ),
    )

    sync_export_pr._await_ci(repo="example/public", sha=_SHA, cwd=Path("/public"))

    assert calls == [_RUN_LIST, _RUN_LIST]
    # The listing is read, not echoed: the log line is the only output.
    assert (
        capsys.readouterr().out == "Public CI passed on example/public@0123456789ab.\n"
    )


def test_public_ci_fails_on_any_other_listing_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _run_lists(
        monkeypatch,
        first=subprocess.CompletedProcess(
            [],
            1,
            stdout="",
            stderr="HTTP 403: Resource not accessible by integration\n",
        ),
    )

    with pytest.raises(SystemExit, match="1"):
        sync_export_pr._await_ci(
            repo="example/public",
            sha="c0ffee",
            cwd=Path("/public"),
        )

    assert len(calls) == 1


def test_public_ci_is_read_with_the_workflow_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The sync App holds no Actions permission; the workflow's token reads runs."""
    tokens: list[str] = []

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        tokens.append(from_plain(kwargs["env"], dict[str, str])["GH_TOKEN"])
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout='[{"status": "completed", "conclusion": "success"}]',
        )

    monkeypatch.setattr(sync_export_pr, "_run", fake_run)
    monkeypatch.setenv("GH_TOKEN", "sync-token")
    monkeypatch.setenv("COPYBARISTA_ACTIONS_TOKEN", "actions-token")

    sync_export_pr._await_ci(repo="example/public", sha="c0ffee", cwd=Path("/public"))

    assert tokens == ["actions-token"]


@pytest.mark.parametrize(
    "run",
    ['[{"status": "in_progress", "conclusion": ""}]', "[]"],
    ids=["never-finishes", "never-starts"],
)
def test_the_export_fails_when_public_ci_does_not_finish_in_time(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    run: str,
) -> None:
    """A run that never starts is red too, not a wait without end."""
    sleeps: list[float] = []
    _public_ci(monkeypatch, prs=[_MERGED], runs=[run] * 1000)
    monkeypatch.setattr(time, "sleep", sleeps.append)

    with pytest.raises(SystemExit):
        sync_export_pr._await_public_ci(request=_export_request(tmp_path))

    assert (
        "public CI on example/public did not finish in 45 minutes"
        in capsys.readouterr().out
    )
    # One poll a minute keeps 11 exports inside the workflow token's hourly limit.
    assert set(sleeps) == {sync_export_pr.PUBLIC_CI_POLL_SEC}
    assert len(sleeps) == sync_export_pr.PUBLIC_CI_WAIT_SEC / 60


def test_await_upstream_exports_waits_until_each_run_completes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The lock waits for a sibling export of the same commit to publish."""
    statuses = iter(['[{"status": "in_progress"}]', '[{"status": "completed"}]'])
    calls: list[list[str]] = []
    sleeps: list[float] = []

    def fake_run_gh(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout=next(statuses))

    monkeypatch.setattr(sync_export_pr, "_run_gh", fake_run_gh)
    monkeypatch.setattr(time, "sleep", sleeps.append)

    _await_upstream_exports(
        workflows=("loop-to-typings.yml",),
        source_repo="example/source",
        source_sha="8b466d40b6",
        cwd=Path("/src"),
    )

    assert len(calls) == 2
    assert calls[0][:3] == ["gh", "run", "list"]
    assert {"loop-to-typings.yml", "8b466d40b6", "example/source"} <= set(calls[0])
    assert len(sleeps) == 1


def test_await_upstream_exports_lists_runs_with_the_workflow_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The sync token may not read source Actions; the workflow's own token can."""
    tokens: list[str] = []

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        tokens.append(from_plain(kwargs["env"], dict[str, str])["GH_TOKEN"])
        return subprocess.CompletedProcess(argv, 0, stdout='[{"status": "completed"}]')

    monkeypatch.setattr(sync_export_pr, "_run", fake_run)
    monkeypatch.setenv("GH_TOKEN", "sync-token")
    monkeypatch.setenv("COPYBARISTA_ACTIONS_TOKEN", "actions-token")

    _await_upstream_exports(
        workflows=("loop-to-typings.yml",),
        source_repo="example/source",
        source_sha="8b466d40b6",
        cwd=Path("/src"),
    )

    assert tokens == ["actions-token"]


def test_await_upstream_exports_locks_anyway_when_runs_are_unreadable(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[list[str]] = []

    def fake_run_gh(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="HTTP 403")

    monkeypatch.setattr(sync_export_pr, "_run_gh", fake_run_gh)

    _await_upstream_exports(
        workflows=("loop-to-treekle.yml", "loop-to-typings.yml"),
        source_repo="example/source",
        source_sha="8b466d40b6",
        cwd=Path("/src"),
    )

    assert len(calls) == 1
    # The reason is logged: a silent skip hid the token gap for a whole push.
    assert "HTTP 403" in capsys.readouterr().err


def test_await_upstream_exports_skips_a_manual_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run_gh(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        raise AssertionError(argv)

    monkeypatch.setattr(sync_export_pr, "_run_gh", fake_run_gh)

    _await_upstream_exports(
        workflows=("loop-to-typings.yml",),
        source_repo="example/source",
        source_sha="manual",
        cwd=Path("/src"),
    )


def test_await_upstream_exports_gives_up_after_its_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stuck sibling delays the lock, never blocks it past the deadline."""
    sleeps: list[float] = []

    def fake_run_gh(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 0, stdout='[{"status": "queued"}]')

    monkeypatch.setattr(sync_export_pr, "_run_gh", fake_run_gh)
    monkeypatch.setattr(time, "sleep", sleeps.append)

    _await_upstream_exports(
        workflows=("loop-to-typings.yml",),
        source_repo="example/source",
        source_sha="8b466d40b6",
        cwd=Path("/src"),
    )

    assert sum(sleeps) >= sync_export_pr.UPSTREAM_EXPORT_WAIT_SEC


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
        *,
        request: ExportRequest,
        pr_template: str,
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
        *,
        project: Path,
        source_dir: Path,
        export_dir: Path,
        manifest: Path,
    ) -> None:
        del project, source_dir, manifest
        (export_dir / "pyproject.toml").write_text("[project]\n", encoding="utf-8")

    def fake_refresh_public_lockfile(*, public_dir: Path, seed: Path) -> None:
        assert seed == source_dir / "uv.lock"
        calls.append("lock")
        (public_dir / "uv.lock").write_text("public lock\n", encoding="utf-8")

    def fake_open_or_update_export_pr(
        *,
        request: ExportRequest,
        pr_plan: sync_export_pr.PrReplayPlan,
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
    monkeypatch.setattr(
        sync_export_pr,
        "_export_public_tree",
        fake_export_public_tree,
    )
    monkeypatch.setattr(
        sync_export_pr,
        "_refresh_public_lockfile",
        fake_refresh_public_lockfile,
    )
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
        ),
    )

    # Public CI validates the PR; the export runs no package checks of its own.
    assert calls == ["lock", "open"]


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
        prefix="trackinizer/import/",
        repo="rekursiv-ai/loop",
        cwd=Path("/src"),
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
            prefix="trackinizer/import/",
            repo="rekursiv-ai/loop",
            cwd=Path("/src"),
        )
        == ()
    )


def test_run_export_sync_fails_while_an_import_is_pending(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An open import PR must FAIL the run, not skip it and pass.

    Blocking is correct: the export force-writes the public checkout, so
    running it before the source absorbs a public change reverts that change.
    But the block does not clear on its own -- an import PR can stall
    unmerged. Exiting green let madcatter sit un-exported for ~40h behind
    an empty import PR while every export run reported success.
    """
    request = replace(
        _export_request(tmp_path),
        import_branch_prefix="madcatter/import/",
        source_repo="rekursiv-ai/loop",
        sync_label="Madcatter",
    )

    def one_pending(**_: object) -> tuple[str, ...]:
        return ("#598 madcatter/import/sha-abc",)

    def unreached(request: ExportRequest) -> None:
        raise AssertionError(f"the export must not run: {request.sync_label}")

    monkeypatch.setattr(sync_export_pr, "_pending_import_prs", one_pending)
    monkeypatch.setattr(sync_export_pr, "_run_export_sync", unreached)

    with pytest.raises(sync_export_pr.ExportGuardError) as excinfo:
        sync_export_pr.run_export_sync(request)

    message = str(excinfo.value)
    assert "#598 madcatter/import/sha-abc" in message, "must name the open PR"
    assert "Madcatter" in message, "the error must name the project"


@pytest.mark.usefixtures("git")
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
    """The content check gates the export even with no import PR open.

    It RAISES rather than returning: this block never clears on its own, since
    a failed import opens no PR for anyone to merge. Exiting green here is what
    let wesearch sit wedged across three successful-looking export runs.
    """
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

    with pytest.raises(sync_export_pr.ExportGuardError, match="bbbb222"):
        sync_export_pr.run_export_sync(request)

    assert ran == [], "a failed import must still block the export"


def test_skipped_export_annotates_the_workflow_run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An unimported public head must FAIL the run, not annotate and pass.

    Blocking is correct -- exporting would revert the public commit -- but a
    green exit means the repository stalls indefinitely with every run
    reporting success, which is exactly how projects sat un-exported while CI
    stayed green. The error names the blocking commit and the project so the
    log says what to land, and the non-zero exit is what makes anyone read it.
    """
    request = replace(
        _export_request(tmp_path),
        import_branch_prefix="sagent/import/",
        source_repo="rekursiv-ai/loop",
        sync_label="Sagent",
    )

    def no_pending(**_: object) -> tuple[str, ...]:
        return ()

    def unimported(**_: object) -> str:
        return "bbbb222"

    def unreached(request: ExportRequest) -> None:
        raise AssertionError(f"the export must not run: {request.sync_label}")

    monkeypatch.setattr(sync_export_pr, "_pending_import_prs", no_pending)
    monkeypatch.setattr(sync_export_pr, "_public_head_unimported", unimported)
    monkeypatch.setattr(sync_export_pr, "_run_export_sync", unreached)

    with pytest.raises(sync_export_pr.ExportGuardError) as excinfo:
        sync_export_pr.run_export_sync(request)

    message = str(excinfo.value)
    assert "bbbb222" in message, "the error must name the blocking commit"
    assert "Sagent" in message, "the error must name the project"
    del capsys


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
    """A first export over an export-authored repo has nothing to revert.

    "No landed import" alone is not a licence to overwrite: a public repo can
    hold human commits before the first import ever runs. Bootstrap requires
    BOTH no import history and no foreign commits.
    """

    def no_import(**_: object) -> str:
        raise sync_import_change.ImportBaseError("no landed import")

    def head(**_: object) -> str:
        return "bbbb222"

    def nothing_foreign(**_: object) -> tuple[str, ...]:
        return ()

    def empty_tree(**_: object) -> str:
        return "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

    monkeypatch.setattr(sync_export_pr, "_public_head_sha", head)
    monkeypatch.setattr(sync_export_pr, "last_synced_public_sha", no_import)
    monkeypatch.setattr(sync_export_pr, "_foreign_commits_since", nothing_foreign)
    monkeypatch.setattr(sync_export_pr, "_git_empty_tree", empty_tree)

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


def test_foreign_commits_since_reports_an_unreadable_range(
    git: FakeGit,
    tmp_path: Path,
) -> None:
    """A range git cannot resolve counts as foreign, and says why.

    The public checkout was cloned at depth 1, so `<last-import>..HEAD` was
    unresolvable and this returned "foreign" for every project -- the export
    skipped forever with a message blaming an unimported commit rather than
    the shallow clone. Failing closed is right; failing closed SILENTLY is
    what made it take three rounds to find.
    """
    repo = tmp_path / "repo"
    _ = git.commit(repo, message="base", files={"f.txt": "0\n"})

    foreign = sync_export_pr._foreign_commits_since(
        base="0" * 40,
        head="HEAD",
        author_email="bot@example.com",
        cwd=repo,
    )

    assert foreign, "an unresolvable range must block the export"
    assert "unreadable" in foreign[0]


def test_foreign_commits_since_separates_bot_from_human(
    git: FakeGit,
    tmp_path: Path,
) -> None:
    """Only commits the export did not author count as work to preserve."""
    repo = tmp_path / "repo"
    base, _, _ = _commits(
        git,
        repo,
        [
            ("base", "bot@example.com"),
            ("generated export", "bot@example.com"),
            ("human release", "dev@example.com"),
        ],
    )

    foreign = sync_export_pr._foreign_commits_since(
        base=base,
        head="HEAD",
        author_email="bot@example.com",
        cwd=repo,
    )

    assert [line.split(" ", 1)[1] for line in foreign] == ["human release"]


def test_import_then_export_completes_the_loop(
    git: FakeGit,
    tmp_path: Path,
) -> None:
    """A landed import must unblock the export of the commit it imported.

    This is the seam every component test missed. The import wrote a marker,
    the ledger parsed markers, and the guard consulted the ledger -- each
    correct alone, while the loop stayed broken for a day because the SHA the
    writer emitted was not the SHA the reader accepted. Every export skipped
    and reported success.

    Drive the real title through the real ledger into the real guard, over
    throwaway repositories, and require the guard to permit the export.
    """
    public = tmp_path / "public"
    _, public_head = _commits(
        git,
        public,
        [
            ("generated export", "bot@example.com"),
            ("human change", "dev@example.com"),
        ],
    )

    # Before the import lands, the human commit is unabsorbed: the guard must
    # block, because force-writing would destroy it.
    source = tmp_path / "source"
    _ = _commits(git, source, [("unrelated source work", "dev@example.com")])
    assert (
        sync_export_pr._public_head_unimported(
            public_dir=public,
            source_dir=source,
            sync_label="Pkg",
            base_branch="main",
            sync_user_email="bot@example.com",
        )
        == public_head
    ), "an unimported human commit must block the export"

    # The import lands, recording the marker exactly as the import path writes it.
    title = f"Import Pkg public changes {sync_import_change._pr_title_sha(public_head)}"
    _ = git.commit(source, message=title, files={})

    # Now the guard must permit the export; otherwise the loop deadlocks.
    assert (
        sync_export_pr._public_head_unimported(
            public_dir=public,
            source_dir=source,
            sync_label="Pkg",
            base_branch="main",
            sync_user_email="bot@example.com",
        )
        == ""
    ), "a landed import must unblock the export it imported"


@pytest.mark.parametrize("human", [False, True])
def test_a_first_export_walks_the_whole_public_history_for_human_work(
    git: FakeGit,
    tmp_path: Path,
    human: bool,
) -> None:
    """With no import landed, every public commit since the empty tree is checked."""
    public, source = tmp_path / "public", tmp_path / "source"
    shas = _commits(
        git,
        public,
        [
            ("generated export", "bot@example.com"),
            *([("human change", "dev@example.com")] if human else []),
        ],
    )
    _ = _commits(git, source, [("unrelated source work", "dev@example.com")])

    assert sync_export_pr._public_head_unimported(
        public_dir=public,
        source_dir=source,
        sync_label="Pkg",
        base_branch="main",
        sync_user_email="bot@example.com",
    ) == (shas[-1] if human else "")


def test_a_command_is_logged_and_run_without_the_callers_virtualenv(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    ran = RecordingRun()
    monkeypatch.setattr(subprocess, "run", ran)
    monkeypatch.setenv("VIRTUAL_ENV", "/elsewhere/.venv")
    monkeypatch.setenv("KEPT", "1")

    done = sync_export_pr._run(["git", "log", "a b"], cwd=tmp_path, capture=True)

    assert done.stdout == "out"
    assert capsys.readouterr().out == "+ git log 'a b'\n"
    ((argv, options),) = ran.calls
    env = options.pop("env")
    assert isinstance(env, dict)
    assert "VIRTUAL_ENV" not in env
    assert env["KEPT"] == "1"
    assert (argv, options) == (
        ["git", "log", "a b"],
        {
            "cwd": tmp_path,
            "check": False,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
        },
    )


def test_an_uncaptured_command_writes_where_told_with_the_env_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ran = RecordingRun()
    monkeypatch.setattr(subprocess, "run", ran)

    sync_export_pr._run(["gh", "pr", "list"], stdout=subprocess.DEVNULL, env={"A": "1"})

    assert ran.calls == [
        (
            ["gh", "pr", "list"],
            {
                "cwd": None,
                "check": False,
                "stdout": subprocess.DEVNULL,
                "stderr": None,
                "text": True,
                "env": {"A": "1"},
            },
        ),
    ]


def test_a_failed_checked_command_replays_its_output_and_exits_with_its_status(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(subprocess, "run", RecordingRun(returncode=3))

    with pytest.raises(SystemExit) as exited:
        sync_export_pr._run(["git", "push"], capture=True)
    assert exited.value.code == 3
    assert capsys.readouterr() == ("+ git push\nout", "err")
    assert sync_export_pr._run(["git", "push"], check=False).returncode == 3


@pytest.fixture
def git(monkeypatch: pytest.MonkeyPatch) -> FakeGit:
    """Route the export's git, and the ledger walk's, to a fresh ``FakeGit``."""
    fake = FakeGit()
    monkeypatch.setattr(sync_export_pr, "_run", fake.run)
    monkeypatch.setattr(sync_import_change, "_run", fake.run)
    return fake


def _commits(git: FakeGit, root: Path, commits: list[tuple[str, str]]) -> list[str]:
    """Commit one change per ``(subject, author_email)`` on ``main``; return the SHAs."""
    return [
        git.commit(root, message=subject, files={"f.txt": f"{i}\n"}, email=email)
        for i, (subject, email) in enumerate(commits)
    ]


@dataclass(slots=True, kw_only=True)
class _Answers:
    """Stands in for ``_run``: answers each expected command in turn, with its output."""

    left: list[tuple[list[str], int, str]]

    def __call__(
        self,
        argv: list[str],
        *,
        cwd: Path | None = None,
        stdout: TextIO | int | None = None,
        check: bool = True,
        capture: bool = False,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        del cwd, stdout, env
        assert self.left, f"unexpected command {argv}"
        (expected, code, out), *self.left = self.left
        assert argv == expected
        # ``_run`` exits on a failure it checks; a step that fails is never checked.
        assert code == 0 or not check, argv
        return subprocess.CompletedProcess(argv, code, stdout=out if capture else None)


def _fail_export(request: ExportRequest) -> None:
    del request
    raise sync_export_pr.PrReplayError("replay failed")


def _record_export(request: ExportRequest) -> None:
    assert request.project_path == Path("pkg")


def test_main_reads_its_arguments_from_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        ["sync_export_pr.py", "--project-path", "pkg", "--source-branch", "main"],
    )
    monkeypatch.setattr(sync_export_pr, "run_export_sync", _record_export)

    assert sync_export_pr.main() == 0


def test_run_requires_a_project_path(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit, match="2"):
        sync_export_pr.run(["--source-branch", "main"])

    assert capsys.readouterr().err.endswith(": error: --project-path is required\n")


# Each flag the parser defaults from the environment: (variable, flag dest, the
# variable's value, the parsed value).
_PARSER_ENVIRONMENT: Final = (
    ("TARGET_REPO", "target_repo", "o/public", "o/public"),
    ("BASE_BRANCH", "base_branch", "dev", "dev"),
    ("GITHUB_SHA", "source_sha", "abc123", "abc123"),
    ("COPYBARISTA_SYNC_USER_NAME", "sync_user_name", "bot", "bot"),
    ("COPYBARISTA_SYNC_USER_EMAIL", "sync_user_email", "b@x.org", "b@x.org"),
    ("GITHUB_REF_NAME", "source_branch", "feature", "feature"),
    ("COPYBARISTA_EXPORT_BRANCH", "branch", "p/export/x", "p/export/x"),
    ("COPYBARISTA_EXPORT_BRANCH_PREFIX", "branch_prefix", "p/export/", "p/export/"),
    ("COPYBARISTA_SYNC_LABEL", "sync_label", "Label", "Label"),
    ("COPYBARISTA_IMPORT_BRANCH_PREFIX", "import_branch_prefix", "p/i/", "p/i/"),
    ("COPYBARISTA_SOURCE_REPO", "source_repo", "o/source", "o/source"),
    ("COPYBARISTA_PR_TITLE", "pr_title", "Title", "Title"),
    ("COPYBARISTA_PR_BODY", "pr_body", "Body.", "Body."),
    ("COPYBARISTA_PR_DEFAULT_TITLE", "pr_default_title", "Default", "Default"),
    ("COPYBARISTA_PR_DEFAULT_BODY", "pr_default_body", "Default.", "Default."),
    ("COPYBARISTA_PR_SCOPE", "pr_scope", "pkg", "pkg"),
    ("COPYBARISTA_REQUIRE_PR_METADATA", "require_pr_metadata", "true", True),
    ("COPYBARISTA_REPLAY_BOOTSTRAP_BASE", "replay_bootstrap_base", "v1", "v1"),
    ("COPYBARISTA_PUBLISH_SOURCE_REV", "publish_source_rev", "true", True),
    ("COPYBARISTA_SOURCE_MESSAGE", "source_message", "Message", "Message"),
    ("GITHUB_EVENT_HEAD_COMMIT_MESSAGE", "source_message", "Pushed", "Pushed"),
    (
        "COPYBARISTA_USE_SOURCE_MESSAGE_PR_TEXT",
        "use_source_message_pr_text",
        "true",
        True,
    ),
    ("COPYBARISTA_AUTO_MERGE", "auto_merge", "true", True),
    ("GITHUB_WORKFLOW", "workflow", "ci", "ci"),
    ("RUNNER_TEMP", "runner_temp", "/runner", "/runner"),
    ("COPYBARISTA_RELEASE_CHECK_SCRIPT", "release_check_script", "c.py", "c.py"),
)


def _clear_parser_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset every variable the parser reads, as on a workstation."""
    for variable, *_ in _PARSER_ENVIRONMENT:
        monkeypatch.delenv(variable, raising=False)


def test_parser_defaults_without_its_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_parser_environment(monkeypatch)

    assert vars(sync_export_pr._parser().parse_args([])) == {
        "project_dir": None,
        "source_dir": "source",
        "project_path": "",
        "public_dir": "public",
        "target_repo": "",
        "base_branch": "main",
        "source_sha": "manual",
        "sync_user_name": "copybarista",
        "sync_user_email": "copybarista@example.com",
        "source_branch": "",
        "branch": "",
        "branch_prefix": "copybarista/export/",
        "sync_label": "Copybarista",
        "import_branch_prefix": "",
        "source_repo": "",
        "pr_title": "",
        "pr_body": "",
        "pr_default_title": "Update public export",
        "pr_default_body": "Updates the generated public repository export.",
        "pr_scope": "",
        "require_pr_metadata": False,
        "replay_bootstrap_base": "",
        "publish_source_rev": False,
        "source_message": "",
        "use_source_message_pr_text": False,
        "forbidden_pr_text": [],
        "auto_merge": False,
        "refresh_public_lockfile": False,
        "dry_run": False,
        "skip_source_validation": False,
        "workflow": "manual",
        "runner_temp": str(sync_export_pr.DEFAULT_RUNNER_TEMP),
        "release_check_script": "",
    }


@pytest.mark.parametrize(
    ("variable", "dest", "value", "parsed"),
    _PARSER_ENVIRONMENT,
    ids=[variable for variable, *_ in _PARSER_ENVIRONMENT],
)
def test_parser_defaults_each_flag_from_its_variable(
    monkeypatch: pytest.MonkeyPatch,
    variable: str,
    dest: str,
    value: str,
    parsed: object,
) -> None:
    _clear_parser_environment(monkeypatch)
    monkeypatch.setenv(variable, value)

    assert vars(sync_export_pr._parser().parse_args([]))[dest] == parsed


def test_a_bare_auto_merge_flag_turns_it_on(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_parser_environment(monkeypatch)

    flags = vars(sync_export_pr._parser().parse_args(["--auto-merge"]))
    assert flags["auto_merge"] is True


@pytest.mark.parametrize(
    ("invocation", "text"),
    [
        ("project_dir", "Optional path to a project directory with"),
        ("--pr-scope PR_SCOPE", "Package or repository scope used to"),
        ("--source-message SOURCE_MESSAGE", "Source commit message used when"),
        (
            "--use-source-message-pr-text, --no-use-source-message-pr-text",
            "Use the source commit first line and",
        ),
        ("--forbidden-pr-text FORBIDDEN_PR_TEXT", "Comma- or newline-separated"),
        ("--auto-merge [AUTO_MERGE]", "Enable GitHub PR auto-merge for the"),
        ("--refresh-public-lockfile", "Run uv lock in the exported public"),
        ("--dry-run", "Run against a temporary public checkout and"),
        ("--skip-source-validation", "Skip source checkout lint/type/test"),
        ("--release-check-script RELEASE_CHECK_SCRIPT", "Optional project-relative"),
    ],
)
def test_parser_help_explains_each_documented_flag(
    monkeypatch: pytest.MonkeyPatch,
    invocation: str,
    text: str,
) -> None:
    """Each flag's help directly follows its invocation in ``--help``.

    Matched on whitespace-collapsed output: argparse moves the help of a long
    invocation to the next line. Wide columns keep it from breaking a
    hyphenated word.
    """
    monkeypatch.setenv("COLUMNS", "1000")

    shown = " ".join(sync_export_pr._parser().format_help().split())

    assert f"{invocation} {text}" in shown


@pytest.mark.parametrize(
    ("docstring", "description"),
    [
        (
            "#!/bin/sh\nexec uv run\nFirst line.\n\nSecond paragraph.\n",
            "First line.\n\nSecond paragraph.\n",
        ),
        (None, None),
    ],
    ids=["docstring", "optimized away"],
)
def test_parser_describes_the_script_from_its_docstring_after_the_shebang(
    monkeypatch: pytest.MonkeyPatch,
    docstring: str | None,
    description: str | None,
) -> None:
    """The docstring's first two lines are the polyglot shell preamble."""
    monkeypatch.setattr(sync_export_pr, "__doc__", docstring)

    assert sync_export_pr._parser().description == description


def test_run_builds_the_request_from_every_flag(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Every flag reaches the request: one dropped falls back to its default."""
    _clear_parser_environment(monkeypatch)
    captured: list[ExportRequest] = []
    monkeypatch.setattr(sync_export_pr, "run_export_sync", captured.append)

    sync_export_pr.run(
        [
            *("--project-path", "pkg", "--source-dir", "src"),
            *("--public-dir", "pub", "--target-repo", "o/pub"),
            *("--base-branch", "dev", "--source-sha", "abc123"),
            *("--branch", "p/export/main", "--branch-prefix", "p/export/"),
            *("--sync-label", "Label", "--import-branch-prefix", "p/import/"),
            *("--source-repo", "o/src", "--sync-user-name", "bot"),
            *("--sync-user-email", "b@x.org", "--pr-default-title", "Title"),
            *("--pr-default-body", "Body.", "--pr-scope", "pkg"),
            *("--require-pr-metadata", "--replay-bootstrap-base", "v1"),
            *("--publish-source-rev", "--forbidden-pr-text", "zork,quux"),
            *("--auto-merge", "true", "--refresh-public-lockfile"),
            *("--skip-source-validation", "--runner-temp", str(tmp_path)),
            "--dry-run",
        ],
    )

    assert captured == [
        ExportRequest(
            source_dir=Path("src"),
            project_path=Path("pkg"),
            public_dir=Path("pub"),
            target_repo="o/pub",
            base_branch="dev",
            source_sha="abc123",
            branch="p/export/main",
            sync_label="Label",
            sync_user_name="bot",
            sync_user_email="b@x.org",
            pr_title="Title",
            pr_body="Body.",
            manual_pr_title="",
            manual_pr_body="",
            replay_settings=sync_export_pr.PrReplaySettings(
                scope="pkg",
                default_title="Title",
                default_body="Body.",
                require_metadata=True,
                bootstrap_base="v1",
                publish_source_rev=True,
            ),
            forbidden_pr_text=("zork", "quux"),
            auto_merge=True,
            refresh_public_lockfile=True,
            skip_source_validation=True,
            runner_temp=tmp_path,
            release_check_script=None,
            dry_run=True,
            import_branch_prefix="p/import/",
            source_repo="o/src",
        ),
    ]


def test_run_exits_2_on_a_replay_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sync_export_pr, "run_export_sync", _fail_export)

    with pytest.raises(SystemExit, match="2"):
        sync_export_pr.run(["--project-path", "pkg", "--source-branch", "main"])

    assert capsys.readouterr().err == "replay failed\n"


def test_export_branch_name_falls_back_to_the_source_sha() -> None:
    assert (
        export_branch_name(
            explicit="",
            source_branch="",
            source_sha="0123456789abcdef",
            prefix="pkg/export/",
        )
        == "pkg/export/sha-0123456789ab"
    )


@pytest.mark.parametrize(
    ("source_message", "message"),
    [("", "--source-message or --pr-title is required"), ("Title", "")],
    ids=["no message", "no default body"],
)
def test_export_pr_text_requires_some_text(
    capsys: pytest.CaptureFixture[str],
    source_message: str,
    message: str,
) -> None:
    with pytest.raises(SystemExit) as raised:
        export_pr_text(
            title="",
            body="",
            source_message=source_message,
            use_source_message=True,
            forbidden_text=(),
            default_body="",
        )

    if message:
        assert str(raised.value) == f"{message}.\n"
    else:
        assert raised.value.code == 2
        assert capsys.readouterr().err == "--pr-body is required.\n"


def test_a_validated_export_runs_every_step_in_order(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Lint, export with its leak checks, types and tests, release check, relock."""
    calls: list[str] = []

    def step(name: str) -> Callable[..., None]:
        def record(**_: object) -> None:
            calls.append(name)

        return record

    def plan(**_: object) -> sync_export_pr.PrReplayPlan:
        return _merged_pr_plan()

    def workflows(*, pyproject: Path, source_dir: Path) -> tuple[str, ...]:
        del pyproject, source_dir
        return ("loop-to-sibling.yml",)

    for name in (
        "_preleakcheck_validation",
        "_export_public_tree",
        "_postleakcheck_validation",
        "_check_release_tree",
        "_await_upstream_exports",
        "_refresh_public_lockfile",
    ):
        monkeypatch.setattr(sync_export_pr, name, step(name))
    monkeypatch.setattr(sync_export_pr, "_resolve_pr_replay_plan", plan)
    monkeypatch.setattr(sync_export_pr, "_render_pr_replay_plan_body", plan)
    monkeypatch.setattr(sync_export_pr, "_upstream_export_workflows", workflows)
    (tmp_path / "public").mkdir()

    sync_export_pr._run_export_sync(
        replace(
            _export_request(tmp_path),
            public_dir=tmp_path / "public",
            release_check_script=Path("scripts/check.py"),
            refresh_public_lockfile=True,
            source_repo="example/source",
            dry_run=True,
        ),
    )

    assert calls == [
        "_preleakcheck_validation",
        "_export_public_tree",
        "_postleakcheck_validation",
        "_check_release_tree",
        "_await_upstream_exports",
        "_refresh_public_lockfile",
    ]


def test_project_discovery_reads_the_release_check_from_settings(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def toplevel(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        assert argv == ["git", "rev-parse", "--show-toplevel"]
        return subprocess.CompletedProcess(argv, 0, f"{tmp_path}\n", "")

    monkeypatch.setattr(subprocess, "run", toplevel)
    settings = SyncSettings(
        package_name="pkg",
        sync_label="Pkg",
        source_root="pkg",
        public_repo="example/pkg",
        source_repo="example/source",
        copybarista_project_path="tools/copybarista",
        forbidden_pr_text=(),
        release_check_script="scripts/check.py",
    )

    argv = sync_export_pr._argv_from_settings(
        project_dir=tmp_path / "pkg",
        settings=settings,
    )

    assert argv[argv.index("--release-check-script") + 1] == "scripts/check.py"
    assert argv[argv.index("--project-path") + 1] == "pkg"


@pytest.mark.parametrize("enabled", [True, False])
def test_project_discovery_renders_every_setting_as_a_flag(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    enabled: bool,
) -> None:
    """A local run reads the sync config exactly as the CI export renders it."""
    project = tmp_path / "pkg"

    def toplevel(
        argv: list[str],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        assert argv == ["git", "rev-parse", "--show-toplevel"]
        assert kwargs["cwd"] == project
        return subprocess.CompletedProcess(argv, 0, f"{tmp_path}\n", "")

    monkeypatch.setattr(subprocess, "run", toplevel)
    settings = SyncSettings(
        package_name="pkg",
        sync_label="Pkg",
        source_root="pkg",
        public_repo="example/public",
        source_repo="example/source",
        copybarista_project_path="tools/copybarista",
        forbidden_pr_text=("zork", "quux"),
        sync_user_name="bot",
        sync_user_email="b@x.org",
        replay_bootstrap_base="v1",
        require_pr_metadata=enabled,
        publish_source_rev=enabled,
        refresh_public_lockfile=enabled,
        skip_source_validation=enabled,
        release_check_script="check.py" if enabled else "",
    )

    assert sync_export_pr._argv_from_settings(
        project_dir=project,
        settings=settings,
    ) == [
        *("--source-dir", str(tmp_path), "--project-path", "pkg"),
        *("--public-dir", str(tmp_path.parent / "public")),
        *("--target-repo", "example/public", "--source-branch", "dry-run"),
        *("--branch-prefix", "pkg/export/", "--sync-label", "Pkg"),
        *("--sync-user-name", "bot", "--sync-user-email", "b@x.org"),
        *("--pr-default-title", "Update Pkg export"),
        *("--pr-default-body", "Updates the generated Pkg public repository export."),
        *("--pr-scope", "pkg", "--replay-bootstrap-base", "v1"),
        "--require-pr-metadata" if enabled else "--no-require-pr-metadata",
        "--publish-source-rev" if enabled else "--no-publish-source-rev",
        *(
            (
                "--refresh-public-lockfile",
                "--skip-source-validation",
                "--release-check-script",
                "check.py",
            )
            if enabled
            else ()
        ),
        *("--forbidden-pr-text", "zork", "--forbidden-pr-text", "quux"),
    ]


def test_project_discovery_needs_a_git_checkout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(subprocess, "run", RecordingRun(returncode=128))

    with pytest.raises(SystemExit, match="Cannot find Git repository containing"):
        sync_export_pr._git_toplevel(Path("/elsewhere"))


def test_a_dry_run_clones_a_public_git_checkout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    public = tmp_path / "public"
    public.mkdir()
    dry = tmp_path / "dry"

    def mkdtemp(prefix: str) -> str:
        assert prefix == "copybarista-dry-public-"
        dry.mkdir()
        return str(dry)

    answers = _Answers(
        left=[
            (["git", "rev-parse", "--show-toplevel"], 0, f"{public}\n"),
            (["git", "clone", "--no-local", str(public), str(dry)], 0, ""),
        ],
    )
    monkeypatch.setattr(tempfile, "mkdtemp", mkdtemp)
    monkeypatch.setattr(sync_export_pr, "_run", answers)

    assert sync_export_pr._clone_public_checkout_for_dry_run(public_dir=public) == dry
    assert answers.left == []
    assert not dry.exists()


def test_a_missing_directory_is_no_git_checkout(tmp_path: Path) -> None:
    assert not sync_export_pr._is_git_checkout(tmp_path / "missing")


def test_basedpyright_runs_in_the_projects_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answers = _Answers(
        left=[
            (["uv", "--quiet", "--project", "pkg", "run", "basedpyright", "."], 0, ""),
        ],
    )
    monkeypatch.setattr(sync_export_pr, "_run", answers)

    sync_export_pr._run_basedpyright(project=Path("pkg"), targets=(".",))

    assert answers.left == []


def _replay_inputs(
    monkeypatch: pytest.MonkeyPatch,
    *,
    patches: tuple[PrMetadataPatch, ...],
) -> None:
    """Fake every input of ``_resolve_pr_replay_plan``: no PR, ``patches`` to replay."""

    def source_rev(*, source_dir: Path, fallback: str) -> str:
        del source_dir, fallback
        return "222222222222"

    def no_pr(*, branch: str, repo: str, cwd: Path) -> sync_export_pr.CurrentPr | None:
        del branch, repo, cwd
        return None

    def markers(*, branch: str, cwd: Path) -> sync_export_pr.BranchMarkers:
        del branch, cwd
        return sync_export_pr.BranchMarkers(
            source_digest="",
            replay_base_digest="",
            exists=False,
        )

    def replay_base(**_: object) -> str:
        return "111111111111"

    def metadata(**_: object) -> tuple[PrMetadataPatch, ...]:
        return patches

    monkeypatch.setattr(sync_export_pr, "_current_source_rev", source_rev)
    monkeypatch.setattr(sync_export_pr, "_current_pr", no_pr)
    monkeypatch.setattr(sync_export_pr, "_branch_markers", markers)
    monkeypatch.setattr(sync_export_pr, "_replay_base", replay_base)
    monkeypatch.setattr(sync_export_pr, "_source_pr_metadata", metadata)


def test_replay_requires_metadata_when_the_settings_ask(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _replay_inputs(monkeypatch, patches=())
    request = _export_request(tmp_path)
    request = replace(
        request,
        replay_settings=replace(request.replay_settings, require_metadata=True),
    )

    with pytest.raises(
        sync_export_pr.PrReplayError,
        match=r"found no Copybarista-PR-\* fields in 1111111\.\.2222222",
    ):
        sync_export_pr._resolve_pr_replay_plan(request=request, pr_template="")


def test_replay_keeps_the_manual_title_and_body(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _replay_inputs(monkeypatch, patches=())
    request = replace(
        _export_request(tmp_path),
        manual_pr_title="Manual title",
        manual_pr_body="Manual body.",
    )

    plan = sync_export_pr._resolve_pr_replay_plan(request=request, pr_template="")

    assert (plan.state.title, plan.state.body_intro) == ("Manual title", "Manual body.")


@pytest.mark.parametrize(
    ("code", "fallback", "expected"),
    [(0, "", "abc"), (128, " given ", "given")],
    ids=["the checkout", "the fallback"],
)
def test_current_source_rev_reads_the_checkout_else_the_fallback(
    monkeypatch: pytest.MonkeyPatch,
    code: int,
    fallback: str,
    expected: str,
) -> None:
    monkeypatch.setattr(
        sync_export_pr,
        "_run",
        _Answers(
            left=[(["git", "rev-parse", "HEAD"], code, "abc\n" if code == 0 else "")],
        ),
    )

    assert sync_export_pr._current_source_rev(source_dir=Path(), fallback=fallback) == (
        expected
    )


def test_current_source_rev_fails_without_either(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        sync_export_pr,
        "_run",
        _Answers(left=[(["git", "rev-parse", "HEAD"], 128, "")]),
    )

    with pytest.raises(
        sync_export_pr.PrReplayError,
        match="Cannot resolve current source",
    ):
        sync_export_pr._current_source_rev(source_dir=Path(), fallback="")


def test_foreign_commits_skip_blank_log_lines(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        sync_export_pr,
        "_run",
        _Answers(
            left=[
                (
                    ["git", "log", "a..b", "--format=%h%x00%ae%x00%s"],
                    0,
                    "\nabc\x00bot@example.com\x00Export\n\ndef\x00dev@example.com\x00Fix\n",
                ),
            ],
        ),
    )

    assert sync_export_pr._foreign_commits_since(
        base="a",
        head="b",
        author_email="bot@example.com",
        cwd=Path(),
    ) == ("def Fix",)


def test_pending_import_prs_needs_a_list(monkeypatch: pytest.MonkeyPatch) -> None:
    def gh(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 0, '{"number": 1}', "")

    monkeypatch.setattr(sync_export_pr, "_run_gh", gh)

    with pytest.raises(sync_export_pr.PrReplayError, match="is not a list"):
        _pending_import_prs(prefix="pkg/import/", repo="example/source", cwd=Path())


def test_a_replay_base_past_the_bootstrap_stands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        sync_export_pr,
        "_run",
        _Answers(
            left=[(["git", "merge-base", "--is-ancestor", "base", "boot"], 1, "")],
        ),
    )

    assert (
        sync_export_pr._floor_replay_base(
            source_dir=Path(),
            base="base",
            bootstrap_base="boot",
        )
        == "base"
    )


def test_replay_reads_metadata_from_its_base_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answers = _Answers(
        left=[
            (["git", "merge-base", "--is-ancestor", "base", "head"], 0, ""),
            (
                [
                    "git",
                    "log",
                    "-z",
                    "--reverse",
                    "--format=%H%x00%aN%x00%aE%x00%B",
                    "base..head",
                ],
                0,
                _metadata_log("Copybarista-PR-Title: Replayed\n"),
            ),
        ],
    )
    monkeypatch.setattr(sync_export_pr, "_run", answers)

    (patch,) = sync_export_pr._source_pr_metadata(
        source_dir=Path(),
        replay_base="base",
        current_source_rev="head",
        forbidden_text=(),
        scope="",
    )

    assert patch.title == "Replayed"
    assert answers.left == []


def test_replay_needs_the_history_back_to_its_base(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        sync_export_pr,
        "_run",
        _Answers(
            left=[(["git", "merge-base", "--is-ancestor", "base", "head"], 1, "")],
        ),
    )

    with pytest.raises(sync_export_pr.PrReplayError, match="history is insufficient"):
        sync_export_pr._source_pr_metadata(
            source_dir=Path(),
            replay_base="base",
            current_source_rev="head",
            forbidden_text=(),
            scope="",
        )


def test_an_open_export_pr_is_edited_after_the_push(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    git = _ScriptedGit(cwd=tmp_path, left=[*_pushed_before_main_moved(tmp_path)])
    git.left[-1] = (git.left[-1][0], 0)
    monkeypatch.setattr(sync_export_pr, "_run", git)
    called: list[list[str]] = []

    def open_pr(*, branch: str, repo: str, cwd: Path) -> bool:
        del branch, repo, cwd
        return True

    def run_gh(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        called.append(argv)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(sync_export_pr, "_gh_pr_exists", open_pr)
    monkeypatch.setattr(sync_export_pr, "_run_gh", run_gh)

    assert _open_or_update_export_pr(
        request=_export_request(tmp_path),
        pr_plan=_merged_pr_plan(),
    )

    assert git.left == []
    assert [argv[:3] for argv in called] == [["gh", "pr", "edit"]]


def test_an_export_gives_up_when_main_keeps_moving(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    moved = [
        *_pushed(holds_main=False),
        (["git", "reset", "--soft", "refs/remotes/origin/main"], 0),
        (["git", "diff", "--cached", "--quiet"], 1),
        _committed(tmp_path),
    ]
    git = _ScriptedGit(cwd=tmp_path, left=moved * 3)
    monkeypatch.setattr(sync_export_pr, "_run", git)
    commit_argv = _committed(tmp_path)[0]

    with pytest.raises(
        sync_export_pr.ExportGuardError,
        match="main kept moving across 3 export pushes",
    ):
        sync_export_pr._push_export_branch(
            request=_export_request(tmp_path),
            commit_argv=commit_argv,
        )

    assert git.left == []


def test_auto_merge_relays_what_gh_printed(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    def gh(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        assert argv[-1] == "--auto"
        return subprocess.CompletedProcess(argv, 0, "queued\n", "")

    monkeypatch.setattr(sync_export_pr, "_run_gh", gh)

    sync_export_pr._enable_export_pr_auto_merge(
        request=_export_request(tmp_path),
        pr_title="Public title",
    )

    assert capsys.readouterr().out == "queued\n"


@pytest.mark.parametrize(
    ("log", "message"),
    [
        ("abc\0Author\0a@example.com\0", "not NUL-framed records"),
        (
            _metadata_log("Copybarista-PR-Body: text\nCopybarista-PR-Title: late\n"),
            "body must be the final Copybarista-PR-\\* field",
        ),
        (
            _metadata_log("Copybarista-PR-Body-Mode: merge\nCopybarista-PR-Body: x\n"),
            "Body-Mode: must be append or replace",
        ),
        (
            _metadata_log("Copybarista-PR-Title: t\n", author=" "),
            "author name and email are required",
        ),
        (
            _metadata_log("Copybarista-PR-Title: t\n", email="dev@internal.example"),
            "Author: contains restricted source-specific text",
        ),
    ],
    ids=["misframed", "a field after the body", "a bad body mode", "no name", "a leak"],
)
def test_metadata_parsing_refuses_a_malformed_record(log: str, message: str) -> None:
    with pytest.raises(sync_export_pr.PrMetadataError, match=message):
        _parse_pr_metadata_log(log, forbidden_text=("internal.example",))


def test_metadata_closes_a_block_at_the_next_scope() -> None:
    patches = _parse_pr_metadata_log(
        _metadata_log(
            "Copybarista-PR-Title: Shared\n"
            "Copybarista-PR-Scope: pkg\n"
            "Copybarista-PR-Title: Scoped\n",
        ),
        forbidden_text=(),
        scope="pkg",
    )

    assert [(patch.scope, patch.title) for patch in patches] == [
        ("", "Shared"),
        ("pkg", "Scoped"),
    ]


def test_an_entry_without_a_marker_is_kept_as_it_is(tmp_path: Path) -> None:
    entry = PrBodyEntry(commit_sha="", text="Hand-written note.")

    assert sync_export_pr._recover_body_entries(
        source_dir=tmp_path,
        entries=(entry,),
        forbidden_text=(),
    ) == ((entry,), ())


def test_source_author_needs_a_name_and_an_email(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        sync_export_pr,
        "_run",
        _Answers(
            left=[(["git", "show", "-s", "--format=%aN%x00%aE", "abc"], 0, "Dev\n")],
        ),
    )

    with pytest.raises(
        sync_export_pr.PrReplayError,
        match="Cannot parse source author",
    ):
        sync_export_pr._source_author(
            source_dir=Path(),
            commit_sha="abc",
            forbidden_text=(),
        )


@pytest.mark.parametrize(
    ("state", "message"),
    [
        (replace(_pr_state(), title=" "), "empty title or description"),
        (
            replace(
                _pr_state(),
                body_intro="Intro.\n<!-- copybarista:pr-state version=1 applied=x -->",
            ),
            "exactly one state marker",
        ),
    ],
    ids=["no title", "a second marker"],
)
def test_render_pr_body_refuses_a_broken_state(
    state: PrReplayState,
    message: str,
) -> None:
    with pytest.raises(sync_export_pr.PrReplayError, match=message):
        _render_pr_body(
            state=state,
            branch="copybarista/export/main",
            sync_label="Copybarista",
            publish_source_rev=False,
        )


def test_render_pr_body_opens_a_template_without_a_summary_with_it() -> None:
    state = replace(
        _pr_state(),
        body_entries=(PrBodyEntry(commit_sha="", text="Hand-written note."),),
    )

    body = _render_pr_body(
        state=state,
        branch="copybarista/export/main",
        sync_label="Copybarista",
        publish_source_rev=False,
        pr_template="## Testing\n- [ ] ran\n",
    )

    assert body.startswith(
        "Public body.\n\n### Updates\n\n- Hand-written note.\n\n## Testing\n- [x] ran\n",
    )


_MARKER: Final = "<!-- copybarista:pr-state version=1 applied=sha256:ab -->"


def test_state_from_pr_body_defaults_an_empty_summary() -> None:
    state = _state_from_pr_body(
        title="Title",
        body=f"----\n{_MARKER}\n",
        default_body="Default body.",
    )

    assert (state.body_intro, state.applied_source_digest) == ("Default body.", "ab")


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (f"Intro.\n{_MARKER}\n", "missing the Copybarista export footer"),
        (
            f"Intro.\n\n### Updates\n\nstray text\n----\n{_MARKER}\n",
            "ambiguous text in Copybarista updates",
        ),
        (
            "----\n<!-- copybarista:pr-state version=1 -->\n",
            "missing applied source revision",
        ),
    ],
    ids=["no separator", "stray update text", "no applied revision"],
)
def test_state_from_pr_body_refuses_a_malformed_body(body: str, message: str) -> None:
    with pytest.raises(sync_export_pr.PrReplayError, match=message):
        _state_from_pr_body(title="Title", body=body, default_body="Default body.")


def test_branch_markers_read_the_branch_tip_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ref = "refs/remotes/origin/pkg/export/main"
    monkeypatch.setattr(
        sync_export_pr,
        "_run",
        _Answers(
            left=[
                (
                    [
                        "git",
                        "fetch",
                        "origin",
                        "+refs/heads/pkg/export/main:refs/remotes/origin/pkg/export/main",
                    ],
                    0,
                    "",
                ),
                (["git", "rev-parse", "--verify", ref], 0, ""),
                (
                    ["git", "log", "-1", "--format=%B", ref],
                    0,
                    (
                        "Title\n\ncopybarista-source-rev-sha256= src \n"
                        "copybarista-replay-base-sha256=base\nunrelated\n"
                    ),
                ),
            ],
        ),
    )

    assert sync_export_pr._branch_markers(
        branch="pkg/export/main",
        cwd=Path(),
    ) == sync_export_pr.BranchMarkers(
        source_digest="src",
        replay_base_digest="base",
        exists=True,
    )


def test_a_source_marker_resolves_to_its_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        sync_export_pr,
        "_run",
        _Answers(left=[(["git", "merge-base", "--is-ancestor", "abc", "HEAD"], 0, "")]),
    )

    assert (
        sync_export_pr._resolve_source_marker(
            source_dir=Path(),
            marker="sha:abc",
            marker_source="PR applied marker",
        )
        == "abc"
    )


@pytest.mark.parametrize(
    ("marker", "message"),
    [
        ("sha256:" + "0" * 64, "Cannot resolve PR applied marker digest 000000000000"),
        ("md5:abc", "PR applied marker has unsupported source marker md5:abc"),
    ],
    ids=["unreachable", "unsupported"],
)
def test_a_source_marker_that_resolves_to_nothing_is_refused(
    monkeypatch: pytest.MonkeyPatch,
    marker: str,
    message: str,
) -> None:
    monkeypatch.setattr(
        sync_export_pr,
        "_run",
        _Answers(left=[(["git", "rev-list", "HEAD"], 0, "abc\n")]),
    )

    with pytest.raises(sync_export_pr.PrReplayError, match=message):
        sync_export_pr._resolve_source_marker(
            source_dir=Path(),
            marker=marker,
            marker_source="PR applied marker",
        )


@pytest.mark.parametrize(
    ("code", "expected"),
    [(0, "parent"), (128, "")],
    ids=["a parent", "a root commit"],
)
def test_source_parent_is_empty_for_a_root_commit(
    monkeypatch: pytest.MonkeyPatch,
    code: int,
    expected: str,
) -> None:
    monkeypatch.setattr(
        sync_export_pr,
        "_run",
        _Answers(left=[(["git", "rev-parse", "head^"], code, "parent\n")]),
    )

    assert sync_export_pr._source_parent(source_dir=Path(), rev="head") == expected


def test_pr_text_rewrites_with_the_projects_replace_rules(tmp_path: Path) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg/copy.barista.toml").write_text(
        """
        [workflow]
        name = "pkg"
        mode = "squash"
        source_root = "pkg"

        [files]
        include = ["**"]

        [[transform]]
        type = "replace"
        path = "**/*.py"
        before = "loop.pkg"
        after = "pkg"

        [[transform]]
        type = "ruff_format"
        path = "."
        """,
        encoding="utf-8",
    )

    transforms = sync_export_pr._load_pr_text_transforms(
        source_dir=tmp_path,
        project_path=Path("pkg"),
    )

    assert [transform.type for transform in transforms] == ["replace"]
    assert sync_export_pr._rewrite_public_text("see loop.pkg.cli", transforms) == (
        "see pkg.cli"
    )


if __name__ == "__main__":
    from copybarista.lib.testing.runner import test_main

    test_main(__file__)
