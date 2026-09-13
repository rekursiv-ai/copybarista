"""Tests for regex-group replacement templates."""

from __future__ import annotations

import pytest

from copybarista.errors import ConfigError
from copybarista.template import compile_module_replace, compile_replace


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
        before="foo${x}bar",
        after="bar${x}foo",
        regex_groups=(("x", "[A-Z]+"),),
    )
    assert template.apply("fooABCbar") == "barABCfoo"


def test_count_reports_number_of_matches() -> None:
    template = compile_replace(
        before="x.${s}",
        after="y.${s}",
        regex_groups=(("s", "[a-z]"),),
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
            "from widget import x",
        )
        == "from acme.internal.widget import x"
    )
    assert (
        compile_replace(before=after, after=before, regex_groups=()).apply(
            "from widget.providers import y",
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
            before="a${x}",
            after="${y}",
            regex_groups=(("x", "[0-9]"), ("y", "[0-9]")),
        )


def test_rejects_invalid_group_regex() -> None:
    # Reported per group rather than from the assembled pattern, so the message
    # names the offending group.
    with pytest.raises(ConfigError, match=r"regex_groups\.x is not valid regex"):
        compile_replace(before="a${x}", after="a${x}", regex_groups=(("x", "[under"),))


@pytest.mark.parametrize(
    ("groups", "culprit"),
    [
        pytest.param(
            (("a", "[unclosed"), ("b", "[^!]*")),
            "a",
            id="unterminated-class",
        ),
        pytest.param((("a", "(grp"), ("b", ")x")), "a", id="unbalanced-paren"),
    ],
)
def test_rejects_a_malformed_group_masked_by_a_later_group(
    groups: tuple[tuple[str, str], ...],
    culprit: str,
) -> None:
    """Each group must be validated ALONE, not only in the assembled pattern.

    ``_build_pattern`` concatenates the groups, so a malformed one can be
    re-balanced by whatever follows it: ``[unclosed`` stays an open character
    class that swallows text until the next group closes it, and ``(grp`` is
    closed by a later ``)``. The combined regex then compiles into something
    that means nothing like what was written, while the same group alone raises.

    Real Copybara validates per group and refuses to load the config
    (``'regex_groups' includes invalid regex for key a``, exit 2), so accepting
    it here admits a ``.sky`` that cannot run there at all.
    """
    with pytest.raises(ConfigError, match=rf"regex_groups\.{culprit}"):
        compile_replace(before="${a}MID${b}", after="", regex_groups=groups)


@pytest.mark.parametrize(
    ("source", "public"),
    [
        pytest.param(
            "from acme.internal.lib import userdirs\n",
            "from widget.lib import userdirs\n",
            id="from-parent-import-leaf",
        ),
        pytest.param(
            "from acme.internal.lib.userdirs import data_dir\n",
            "from widget.lib.userdirs import data_dir\n",
            id="from-module-import-name",
        ),
        pytest.param(
            "import acme.internal.lib.userdirs\n",
            "import widget.lib.userdirs\n",
            id="import-dotted",
        ),
        pytest.param(
            'lazy_import("acme.internal.lib.userdirs")\n',
            'lazy_import("widget.lib.userdirs")\n',
            id="string-token",
        ),
        pytest.param(
            "acme.internal.lib.userdirs.data_dir()\n",
            "widget.lib.userdirs.data_dir()\n",
            id="attribute-access",
        ),
        pytest.param(
            "from acme.internal.lib import (\n    absent,\n    userdirs,\n)\n",
            "from widget.lib import (\n    absent,\n    userdirs,\n)\n",
            id="from-parent-parenthesized",
        ),
        pytest.param(
            "from acme.internal.lib import absent, userdirs\n",
            "from widget.lib import absent, userdirs\n",
            id="from-parent-second-name",
        ),
    ],
)
def test_module_replace_covers_every_import_spelling(source: str, public: str) -> None:
    """One dotted-module rule rewrites the token and the ``from parent import leaf`` form.

    Configs used to carry a separate literal rule per spelling (the dotted
    token AND ``from parent import leaf``); a spelling nobody listed shipped an
    internal import. ``compile_module_replace`` derives every form from the one
    dotted path.
    """
    template = compile_module_replace(
        before="acme.internal.lib.userdirs",
        after="widget.lib.userdirs",
    )
    assert template.apply(source) == public
    reverse = compile_module_replace(
        before="widget.lib.userdirs",
        after="acme.internal.lib.userdirs",
    )
    assert reverse.apply(public) == source


@pytest.mark.parametrize(
    "text",
    [
        pytest.param(
            "from acme.internal.lib import userdirs_fixture\n", id="prefix-name"
        ),
        pytest.param("acme.internal.lib.userdirs_extra.x\n", id="prefix-token"),
        pytest.param("my_acme.internal.lib.userdirs\n", id="suffix-of-identifier"),
        pytest.param("from acme.internal.libx import userdirs\n", id="parent-prefix"),
    ],
)
def test_module_replace_leaves_identifier_neighbours_alone(text: str) -> None:
    template = compile_module_replace(
        before="acme.internal.lib.userdirs",
        after="widget.lib.userdirs",
    )
    assert template.apply(text) == text


def test_module_replace_across_a_leaf_rename() -> None:
    """``after`` may move the leaf under a new parent; both spellings follow."""
    template = compile_module_replace(
        before="acme.internal.custom_json",
        after="widget.custom_json",
    )
    assert (
        template.apply("from acme.internal import custom_json\n")
        == "from widget import custom_json\n"
    )
    assert (
        template.apply("from acme.internal.custom_json import decode\n")
        == "from widget.custom_json import decode\n"
    )


def test_module_replace_rejects_a_non_dotted_path() -> None:
    with pytest.raises(ConfigError, match="dotted module path"):
        compile_module_replace(before="acme.internal.lib.userdirs", after="widget")
    with pytest.raises(ConfigError, match="dotted module path"):
        compile_module_replace(before="acme.internal.lib.user-dirs", after="w.x")


if __name__ == "__main__":
    from copybarista.lib.testing.main import test_main

    test_main(__file__)
