"""Tests for the GitHub Action pin table's accessors."""

from __future__ import annotations

import pytest

from copybarista.action_pins import GITHUB_ACTION_PINS, ActionPin, action_ref


def test_a_pin_spells_its_ref_and_its_workflow_line() -> None:
    pin = ActionPin(action="owner/name", sha="a" * 40, version="v7.0.1")

    assert pin.ref == f"owner/name@{'a' * 40}"
    assert pin.uses == f"owner/name@{'a' * 40} # v7.0.1"


@pytest.mark.parametrize(
    ("version", "major"),
    [("v7.0.1", 7), ("v10.1.0", 10), ("v1", 1), ("2.3", 2)],
)
def test_major_reads_the_leading_version_number(version: str, major: int) -> None:
    assert ActionPin(action="owner/name", sha="a" * 40, version=version).major == major


def test_action_ref_returns_the_pinned_ref() -> None:
    assert action_ref("actions/checkout") == GITHUB_ACTION_PINS["actions/checkout"].ref


def test_action_ref_refuses_an_unpinned_action() -> None:
    with pytest.raises(KeyError, match="owner/unpinned"):
        action_ref("owner/unpinned")


if __name__ == "__main__":
    from copybarista.lib.testing.runner import test_main

    test_main(__file__)
