"""Tests for the Copybarista-PR metadata forbidden-text guard."""

from __future__ import annotations

from copybarista.scripts.check_pr_text import _scan


_CLEAN = (
    "Reorganize web utilities into submodules.\n"
    "\n"
    "- Move chrome modules under loop/wesearch/chrome/ "
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
            "- Challenge now imported from `loop.wesearch.fetch`.\n"
        )
        violations = _scan(message)
        assert len(violations) == 1
        field, term, _text = violations[0]
        assert field == "Copybarista-PR-Body"
        assert term == "loop."

    def test_forbidden_path_in_title_flagged(self) -> None:
        message = (
            "Subject.\n\n"
            "Copybarista-PR-Title: Refresh loop/wesearch export "
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
            "Move things around loop/wesearch/chrome and "
            "loop.wesearch.fetch.\n\n"
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
            "Copybarista-PR-Body:\n- References loop.lib leak here.\n"
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
            "- Path loop/lib and module loop.lib both leak, "
            "plus LOOP_ENV.\n"
        )
        terms = {term for _field, term, _text in _scan(message)}
        assert {"loop.", "loop/", "LOOP_"} <= terms
