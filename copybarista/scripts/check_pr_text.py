#!/bin/sh
# ruff: noqa: EXE003, D300 -- Polyglot shell/Python script.
# fmt: off
'''' 2>/dev/null #
exec uv --quiet --project "$(dirname "$0")" run --frozen --no-sync python3 "$0" "$@"
Reject private source references in a commit's Copybarista-PR-* metadata.

Copybarista replays each source commit's ``Copybarista-PR-Title`` /
``Copybarista-PR-Body`` trailer into the PUBLIC export PR. The export workflow
validates those fields against a forbidden-term list (its ``FORBIDDEN_PR_TEXT``)
and hard-fails the whole export when a monorepo reference (``loop.``, ``loop/``,
...) leaks in -- but only in CI, after the bad commit is already pushed and
unrewritable. This hook runs the same check at ``commit-msg`` time, so a leak is
caught locally before it can reach ``main`` and wedge the export replay.

The forbidden terms mirror the ``FORBIDDEN_PR_TEXT`` env in the export
workflows (``.github/workflows/export-{sagent,configgle}.yml``); keep them in
step. A match is a substring test, exactly as the export performs it.

Only the ``Copybarista-PR-Title`` and ``Copybarista-PR-Body`` values are scanned
-- the ordinary commit subject/body may reference monorepo paths freely
(Copybarista ignores them). ``Copybarista-PR-Body`` is a contiguous block that
runs to the next ``Copybarista-PR-Scope`` field or a blank line, matching the
export parser's framing.

Usage:
    check_pr_text.py <commit-msg-file>
'''
# fmt: on

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import argparse
import sys


def main() -> int:
    """The main function. Return the process exit code."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n", 2)[2])
    parser.add_argument("message_file", help="Path to the commit message file.")
    args = parser.parse_args()
    message = Path(args.message_file).read_text(encoding="utf-8")
    violations = _scan(message)
    if not violations:
        return 0
    lines = ["Copybarista-PR metadata contains forbidden monorepo references:"]
    lines.extend(f"  {field}: {term!r} in {text!r}" for field, term, text in violations)
    lines.append(
        "\nThese fields are replayed into the PUBLIC export PR and will hard-fail "
        "the export. Rewrite them in public terms (no loop./loop/ paths); describe "
        "the change with backticked bare module names and relative layout."
    )
    sys.stderr.write("\n".join(lines) + "\n")
    return 1


def _scan(
    message: str,
    # Mirror FORBIDDEN_PR_TEXT in the export workflows
    # (.github/workflows/export-{sagent,configgle}.yml); keep in step. A public
    # PR field must contain none of these monorepo references.
    forbidden: Sequence[str] = (
        "loop.",
        "loop/",
        "LOOP_",
        "rekursiv-ai/loop",
        "sync-to-loop",
    ),
) -> list[tuple[str, str, str]]:
    """Return ``(field, forbidden_term, offending_text)`` for each violation."""
    return [
        (field, term, value.strip())
        for field, value in _pr_field_values(message)
        for term in forbidden
        if term in value
    ]


def _pr_field_values(message: str) -> list[tuple[str, str]]:
    """Extract ``(field_name, value)`` for each Copybarista-PR title/body block."""
    lines = message.splitlines()
    values: list[tuple[str, str]] = []
    idx = 0
    while idx < len(lines):
        line = lines[idx]
        if line.startswith("Copybarista-PR-Title:"):
            values.append(("Copybarista-PR-Title", line.partition(":")[2]))
            idx += 1
            continue
        if line.startswith("Copybarista-PR-Body:"):
            body_lines, idx = _body_block(lines, start=idx + 1)
            values.append(("Copybarista-PR-Body", "\n".join(body_lines)))
            continue
        idx += 1
    return values


def _body_block(lines: list[str], *, start: int) -> tuple[list[str], int]:
    """Return the body lines from ``start`` and the index after the block.

    The body runs until the next ``Copybarista-PR-Scope`` field or a blank line,
    matching the export parser's contiguous-paragraph framing.
    """
    body: list[str] = []
    idx = start
    while idx < len(lines):
        line = lines[idx]
        if not line.strip() or line.startswith("Copybarista-PR-Scope:"):
            break
        body.append(line)
        idx += 1
    return body, idx


if __name__ == "__main__":
    sys.exit(main())
# vim: ft=python
