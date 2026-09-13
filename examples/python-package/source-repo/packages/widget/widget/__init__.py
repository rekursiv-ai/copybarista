"""Tiny package used by the Copybarista examples."""

# house-ignore[globals, init-facade] -- Example shaped like a third-party repo, not house code.
NAME = "widget"


def label() -> str:
    """Return the public package label.

    Returns:
      NAME: The str.

    """
    return NAME
