"""Tests for Copybarista's exception hierarchy."""

from __future__ import annotations

from copybarista import errors


def test_every_error_the_module_defines_is_user_facing() -> None:
    # `cli.main` reports a CopybaristaError as one stderr line and an exit code;
    # an error outside the hierarchy would escape it as a traceback.
    defined = {
        name: value
        for name, value in vars(errors).items()
        if isinstance(value, type) and issubclass(value, Exception)
    }

    assert sorted(defined) == [
        "ConfigError",
        "CopybaristaError",
        "ExportError",
        "GlobError",
        "ImportRequestError",
        "LeakCheckError",
        "OutputMismatchError",
        "TransformError",
    ]
    assert [
        name
        for name, error in defined.items()
        if not issubclass(error, errors.CopybaristaError)
    ] == []


if __name__ == "__main__":
    from copybarista.lib.testing.runner import test_main

    test_main(__file__)
