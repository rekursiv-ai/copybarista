"""Tests for the Copybarista-PR metadata forbidden-text guard."""

from __future__ import annotations

from typing import TYPE_CHECKING

import sys

from copybarista.scripts.check_pr_text import _scan, main


if TYPE_CHECKING:
    from pathlib import Path

    import pytest


_CLEAN = (
    "Reorganize web utilities into submodules.\n"
    "\n"
    "- Move chrome modules under loop/widget/chrome/ "
    "(ordinary body: fine).\n"
    "\n"
    "Copybarista-PR-Scope: sagent\n"
    "Copybarista-PR-Title: Update imports for relocated modules.\n"
    "Copybarista-PR-Body-Mode: append\n"
    "Copybarista-PR-Body:\n"
    "- Challenge now imported from the `fetch` subpackage.\n"
    "- Chrome headers/useragents now under the `chrome` subpackage.\n"
)


class TestScan:
    def test_clean_metadata_passes(self) -> None:
        # The ordinary commit body may reference monorepo paths; only the
        # PR-Title/Body fields are scanned, and they are public-safe here.
        assert _scan(_CLEAN) == []

    def test_forbidden_path_in_body_flagged(self) -> None:
        message = (
            "Subject line.\n\n"
            "Copybarista-PR-Title: Update imports.\n"
            "Copybarista-PR-Body:\n"
            "- Challenge now imported from `loop.widget.fetch`.\n"
        )
        violations = _scan(message)
        assert len(violations) == 1
        field, term, _text = violations[0]
        assert field == "Copybarista-PR-Body"
        assert term == "loop."

    def test_forbidden_path_in_title_flagged(self) -> None:
        message = (
            "Subject.\n\n"
            "Copybarista-PR-Title: Refresh loop/widget export "
            "manifest.\n"
            "Copybarista-PR-Body:\nPublic-safe body.\n"
        )
        violations = _scan(message)
        assert any(
            field == "Copybarista-PR-Title" and term == "loop/"
            for field, term, _ in violations
        )

    def test_ordinary_body_not_scanned(self) -> None:
        # A monorepo path in the NON-metadata body must not flag: Copybarista
        # ignores the ordinary subject/body entirely.
        message = (
            "Move things around loop/widget/chrome and "
            "loop.widget.fetch.\n\n"
            "Copybarista-PR-Title: Public title.\n"
            "Copybarista-PR-Body:\nPublic body.\n"
        )
        assert _scan(message) == []

    def test_body_terminates_at_next_scope(self) -> None:
        # A second scoped block's fields belong to that block; the first body
        # stops at the Copybarista-PR-Scope boundary.
        message = (
            "Subject.\n\n"
            "Copybarista-PR-Scope: sagent\n"
            "Copybarista-PR-Title: Public sagent title.\n"
            "Copybarista-PR-Body:\nPublic sagent body.\n"
            "Copybarista-PR-Scope: configgle\n"
            "Copybarista-PR-Title: Public configgle title.\n"
            "Copybarista-PR-Body:\n- References loop.widget leak here.\n"
        )
        violations = _scan(message)
        assert len(violations) == 1
        assert violations[0][0] == "Copybarista-PR-Body"
        assert violations[0][1] == "loop."

    def test_multiple_forbidden_terms_all_reported(self) -> None:
        message = (
            "Subject.\n\n"
            "Copybarista-PR-Title: Public title.\n"
            "Copybarista-PR-Body:\n"
            "- Path loop/widget and module loop.widget both leak, "
            "plus LOOP_ENV.\n"
        )
        terms = {term for _field, term, _text in _scan(message)}
        assert {"loop.", "loop/", "LOOP_"} <= terms


class TestMain:
    def test_a_clean_message_passes_silently(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        message = tmp_path / "COMMIT_EDITMSG"
        message.write_text(_CLEAN, encoding="utf-8")
        monkeypatch.setattr(sys, "argv", ["check_pr_text.py", str(message)])

        assert main() == 0
        assert capsys.readouterr().err == ""

    def test_a_leak_fails_naming_each_field_and_term(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        message = tmp_path / "COMMIT_EDITMSG"
        message.write_text(
            "Subject.\n\nCopybarista-PR-Title: Move loop/widget.\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(sys, "argv", ["check_pr_text.py", str(message)])

        assert main() == 1
        assert capsys.readouterr().err.splitlines()[:3] == [
            "Copybarista-PR metadata contains forbidden monorepo references:",
            "  Copybarista-PR-Title: 'loop/' in 'Move loop/widget.'",
            "",
        ]


if __name__ == "__main__":
    from copybarista.lib.testing.runner import test_main

    test_main(__file__)
