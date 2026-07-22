"""Tests for regex-group replacement templates."""

from __future__ import annotations

import pytest

from copybarista.errors import ConfigError
from copybarista.template import compile_replace


_NAMESPACE_GROUPS = (("s", "[A-Za-z_]"),)
# A neutral internal namespace collapsed to a short public package name.
_BEFORE = "acme.internal.widget.${s}"
_AFTER = "widget.${s}"


def _forward(before: str, after: str, groups: tuple[tuple[str, str], ...], text: str):
    return compile_replace(before=before, after=after, regex_groups=groups).apply(text)


def _reverse(before: str, after: str, groups: tuple[tuple[str, str], ...], text: str):
    return compile_replace(before=after, after=before, regex_groups=groups).apply(text)


def test_literal_segments_match_verbatim_and_group_reemits_capture() -> None:
    template = compile_replace(
        before="foo${x}bar", after="bar${x}foo", regex_groups=(("x", "[A-Z]+"),)
    )
    assert template.apply("fooABCbar") == "barABCfoo"


def test_count_reports_number_of_matches() -> None:
    template = compile_replace(
        before="x.${s}", after="y.${s}", regex_groups=(("s", "[a-z]"),)
    )
    assert template.count("x.a and x.b but not xz") == 2
    assert template.count("nothing here") == 0


def test_dotted_submodule_reverse_rewrites_only_real_module_tokens() -> None:
    # Reverse rewrites a genuine submodule reference.
    assert (
        _reverse(_BEFORE, _AFTER, _NAMESPACE_GROUPS, "widget.providers.x")
        == "acme.internal.widget.providers.x"
    )
    # Reverse leaves an identifier substring untouched.
    assert (
        _reverse(_BEFORE, _AFTER, _NAMESPACE_GROUPS, "self.widget_state")
        == "self.widget_state"
    )
    # Reverse leaves a dotfile untouched (anchored on identifier-start after dot).
    assert (
        _reverse(_BEFORE, _AFTER, _NAMESPACE_GROUPS, '".widget" / "rules"')
        == '".widget" / "rules"'
    )
    # Reverse leaves sentence-end prose untouched.
    assert (
        _reverse(_BEFORE, _AFTER, _NAMESPACE_GROUPS, "config for widget.")
        == "config for widget."
    )


def test_dotted_submodule_round_trips() -> None:
    src = "acme.internal.widget.providers.load()"
    public = _forward(_BEFORE, _AFTER, _NAMESPACE_GROUPS, src)
    assert public == "widget.providers.load()"
    assert _reverse(_BEFORE, _AFTER, _NAMESPACE_GROUPS, public) == src


def test_literal_import_boundary_leaves_dotted_import_alone() -> None:
    # A literal trailing space anchors the bare package import; a dotted import
    # keeps its dot and is left to the submodule rule. No regex group needed.
    before, after = "from acme.internal.widget ", "from widget "
    assert (
        compile_replace(before=after, after=before, regex_groups=()).apply(
            "from widget import x"
        )
        == "from acme.internal.widget import x"
    )
    assert (
        compile_replace(before=after, after=before, regex_groups=()).apply(
            "from widget.providers import y"
        )
        == "from widget.providers import y"
    )


def test_rejects_undefined_group() -> None:
    with pytest.raises(ConfigError, match="undefined regex_groups"):
        compile_replace(before="a${missing}b", after="ab", regex_groups=())


def test_rejects_group_unused_by_before() -> None:
    with pytest.raises(ConfigError, match="never matched by before"):
        compile_replace(before="ab", after="ab", regex_groups=(("x", "[0-9]"),))


def test_rejects_after_group_absent_from_before() -> None:
    with pytest.raises(ConfigError, match="absent from before"):
        compile_replace(
            before="a${x}", after="${y}", regex_groups=(("x", "[0-9]"), ("y", "[0-9]"))
        )


def test_rejects_invalid_group_regex() -> None:
    with pytest.raises(ConfigError, match="invalid pattern"):
        compile_replace(before="a${x}", after="a${x}", regex_groups=(("x", "[under"),))
