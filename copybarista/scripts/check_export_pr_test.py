"""Tests for the stuck-export-PR check."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final

from copybarista.scripts import check_export_pr
from copybarista.scripts.check_export_pr import ExportPr, stale_export_prs


if TYPE_CHECKING:
    import pytest


_NOW: Final = datetime(2026, 10, 29, 17, 0, tzinfo=UTC)
_URL: Final = "https://github.com/example/sagent/pull/729"


def _stamp(hours_ago: float) -> str:
    return (_NOW - timedelta(hours=hours_ago)).isoformat().replace("+00:00", "Z")


def _pr(*, pushed_hours_ago: float) -> ExportPr:
    return ExportPr(url=_URL, pushed_at=_stamp(pushed_hours_ago))


def test_a_pr_pushed_inside_the_limit_is_not_stale() -> None:
    assert (
        stale_export_prs(
            [_pr(pushed_hours_ago=1.9)],
            now=_NOW,
            max_age=timedelta(hours=2),
        )
        == []
    )


def test_a_pr_not_pushed_past_the_limit_is_stale() -> None:
    stuck = _pr(pushed_hours_ago=21)

    assert stale_export_prs(
        [_pr(pushed_hours_ago=0.1), stuck],
        now=_NOW,
        max_age=timedelta(hours=2),
    ) == [stuck]


def _run_main(
    monkeypatch: pytest.MonkeyPatch,
    *,
    listed: str,
) -> tuple[int, list[list[str]]]:
    calls: list[list[str]] = []

    def fake_gh(argv: list[str]) -> str:
        calls.append(argv)
        return listed

    monkeypatch.setattr(check_export_pr, "_gh", fake_gh)
    monkeypatch.setattr(check_export_pr, "_now", lambda: _NOW)
    monkeypatch.setattr(
        "sys.argv",
        [
            "check_export_pr.py",
            "--repo",
            "example/sagent",
            "--branch",
            "sagent/export/main",
        ],
    )
    return check_export_pr.main(), calls


def _listed(*, created_hours_ago: float, pushed_hours_ago: float) -> str:
    """``gh pr list --json url,createdAt,commits`` output for one PR."""
    return (
        f'[{{"url": "{_URL}", "createdAt": "{_stamp(created_hours_ago)}", '
        f'"commits": [{{"committedDate": "{_stamp(created_hours_ago)}"}}, '
        f'{{"committedDate": "{_stamp(pushed_hours_ago)}"}}]}}]'
    )


def test_main_fails_and_names_a_pr_with_no_recent_push(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, calls = _run_main(
        monkeypatch,
        listed=_listed(created_hours_ago=30, pushed_hours_ago=21),
    )

    assert code == 1
    assert calls[0][:6] == ["gh", "pr", "list", "--repo", "example/sagent", "--head"]
    assert calls[0][6] == "sagent/export/main"
    assert f"::error::{_URL} has had no push since {_stamp(21)}" in (
        capsys.readouterr().out
    )


def test_main_passes_a_long_lived_pr_the_export_just_updated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The export reuses one PR, so its age is from its last push.

    Measured from creation, sagent's #729 read 23h old three minutes after the
    export updated it, and failed the export run it then merged in.
    """
    code, _ = _run_main(
        monkeypatch,
        listed=_listed(created_hours_ago=30, pushed_hours_ago=0.05),
    )

    assert code == 0


def test_main_passes_without_an_open_pr(monkeypatch: pytest.MonkeyPatch) -> None:
    code, _ = _run_main(monkeypatch, listed="[]")

    assert code == 0


if __name__ == "__main__":
    from copybarista.lib.testing.runner import test_main

    test_main(__file__)
