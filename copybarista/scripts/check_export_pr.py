#!/bin/sh
# ruff: noqa: EXE003, D300, D205 -- Polyglot shell/Python script.
# fmt: off
'''' 2>/dev/null #
exec uv --quiet --project "$(dirname "$0")" run --frozen --no-sync python3 "$0" "$@"
Fail when a public export PR has stayed open past a time limit.

An export opens or updates its PR on the public repository, enables
auto-merge, and exits green whether or not the PR ever merges. A PR whose
checks fail therefore sits open while every export run stays green. The export
workflow runs this check after each export and on a schedule, so its status
turns red once an export PR stops landing.

Usage:
    check_export_pr.py --repo OWNER/NAME --branch EXPORT_BRANCH
'''
# fmt: on

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol, cast

import argparse
import json
import subprocess


def main() -> int:
    """Run the program; return the process exit code.

    Returns:
      exit_code: 0 if no export PR is stale, 1 otherwise.

    """
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n", 2)[2] if __doc__ else None,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_arguments(parser)
    flags = cast(_Flags, parser.parse_args())
    listed = _gh(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            flags.repo,
            "--head",
            flags.branch,
            "--state",
            "open",
            "--json",
            "url,createdAt",
            "--jq",
            "map({url, created_at: .createdAt})",
        ],
    )
    stale = stale_export_prs(
        _export_prs(listed),
        now=_now(),
        max_age=timedelta(hours=flags.max_age_hours),
    )
    for pr in stale:
        print(
            f"::error::{pr.url} has been open since {pr.created_at}, over "
            f"{flags.max_age_hours:g}h; its checks are likely failing.",
        )
    return 1 if stale else 0


@dataclass(frozen=True, slots=True, kw_only=True)
class ExportPr:
    """An open export PR, as ``gh pr list`` reports it.

    Attributes:
      url: The PR's web address.
      created_at: When the PR opened, in ISO 8601.

    """

    url: str
    created_at: str


def stale_export_prs(
    prs: list[ExportPr],
    *,
    now: datetime,
    max_age: timedelta,
) -> list[ExportPr]:
    """Return the PRs open longer than ``max_age`` at ``now``.

    Args:
      prs: Open export PRs.
      now: The current time, timezone-aware.
      max_age: How long a PR may stay open.

    Returns:
      stale: The PRs past ``max_age``, in input order.

    """
    return [pr for pr in prs if now - datetime.fromisoformat(pr.created_at) > max_age]


class _Flags(Protocol):
    """Parsed command-line flags."""

    repo: str
    branch: str
    max_age_hours: float


def _add_arguments(parser: argparse.ArgumentParser) -> None:
    """Register flags on ``parser``."""
    parser.add_argument("--repo", required=True, help="Public repository, OWNER/NAME.")
    parser.add_argument("--branch", required=True, help="The export PR's head branch.")
    # An export PR merges within one CI run, which takes under an hour.
    parser.add_argument(
        "--max-age-hours",
        type=float,
        default=2.0,
        help="Hours an export PR may stay open (default: %(default)s).",
    )


# Stdlib ``json``, not the house JSON reader: the workflow runs this file from a
# sparse checkout of the scripts directory alone, with no package to import.
def _export_prs(listed: str) -> list[ExportPr]:
    """Read ``gh pr list`` output, renamed by its ``--jq``, into ``ExportPr``s."""
    rows = cast(list[dict[str, str]], json.loads(listed))
    return [ExportPr(url=row["url"], created_at=row["created_at"]) for row in rows]


def _gh(argv: list[str]) -> str:
    """Run ``gh`` and return its stdout."""
    return subprocess.run(  # noqa: S603 -- Fixed gh argv; values are flags this job sets.
        argv,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _now() -> datetime:
    """Return the current time in UTC."""
    return datetime.now(UTC)


if __name__ == "__main__":
    raise SystemExit(main())
# vim: ft=python
