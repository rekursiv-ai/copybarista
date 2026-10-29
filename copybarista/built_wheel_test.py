"""The copybarista wheel, built and run outside the checkout."""

from __future__ import annotations

import pytest

from copybarista.lib.testing.wheel import built_wheel


pytestmark = pytest.mark.cli_uv


def test_the_wheel_serves_its_cli_outside_the_checkout() -> None:
    wheel = built_wheel("copybarista", anchor=__file__)

    assert "site-packages" in wheel.run(
        "import copybarista; from copybarista.cli import main; "
        "print(copybarista.__file__)",
    )


if __name__ == "__main__":
    from copybarista.lib.testing.runner import test_main

    test_main(__file__)
