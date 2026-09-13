"""Tests that show source imports before Copybarista rewrites them."""

# house-ignore[test-naming] -- Example shaped like a third-party repo, not house code.
from packages.widget.widget import label

import packages.widget.widget as widget_module


def test_label() -> None:
    """Verify the example package function."""
    if label() != "widget":
        raise AssertionError("label() should return the public package name")


def test_module_import() -> None:
    """Verify the module import form also gets rewritten."""
    if widget_module.NAME != "widget":
        raise AssertionError("widget_module.NAME should match the package name")


if __name__ == "__main__":
    from copybarista.lib.testing.main import test_main

    test_main(__file__)
