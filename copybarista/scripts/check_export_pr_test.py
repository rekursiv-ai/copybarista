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


def _pr(*, opened_hours_ago: float) -> ExportPr:
    created = _NOW - timedelta(hours=opened_hours_ago)
    return ExportPr(url=_URL, created_at=created.isoformat().replace("+00:00", "Z"))


def test_a_pr_inside_the_limit_is_not_stale() -> None:
    assert (
        stale_export_prs(
            [_pr(opened_hours_ago=1.9)],
            now=_NOW,
            max_age=timedelta(hours=2),
        )
        == []
    )


def test_a_pr_past_the_limit_is_stale() -> None:
    stuck = _pr(opened_hours_ago=21)

    assert stale_export_prs(
        [_pr(opened_hours_ago=0.1), stuck],
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


def test_main_fails_and_names_a_stuck_pr(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, calls = _run_main(
        monkeypatch,
        listed=f'[{{"url": "{_URL}", "created_at": "2026-10-28T20:04:21Z"}}]',
    )

    assert code == 1
    assert calls[0][:6] == ["gh", "pr", "list", "--repo", "example/sagent", "--head"]
    assert calls[0][6] == "sagent/export/main"
    assert f"::error::{_URL} has been open since 2026-10-28T20:04:21Z" in (
        capsys.readouterr().out
    )


def test_main_passes_without_an_open_pr(monkeypatch: pytest.MonkeyPatch) -> None:
    code, _ = _run_main(monkeypatch, listed="[]")

    assert code == 0


if __name__ == "__main__":
    from copybarista.lib.testing.runner import test_main

    test_main(__file__)
