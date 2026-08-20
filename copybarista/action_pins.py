"""Single source of truth for third-party GitHub Action pins."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True, slots=True, kw_only=True)
class ActionPin:
    """One action's immutable revision, plus the version humans read."""

    action: str
    sha: str
    version: str

    @property
    def major(self) -> int:
        """Return the semantic-version major from the readable version."""
        return int(self.version.removeprefix("v").split(".", 1)[0])

    @property
    def ref(self) -> str:
        """What parsed YAML holds; comments are not part of the value."""
        return f"{self.action}@{self.sha}"

    @property
    def uses(self) -> str:
        """What workflow source says: immutable ref plus readable major."""
        return f"{self.ref} # {self.version}"


GITHUB_ACTION_PINS: Final = {
    pin.action: pin
    for pin in (
        ActionPin(
            action="actions/cache",
            sha="caa296126883cff596d87d8935842f9db880ef25",
            version="v5",
        ),
        ActionPin(
            action="actions/cache/restore",
            sha="caa296126883cff596d87d8935842f9db880ef25",
            version="v5",
        ),
        ActionPin(
            action="actions/cache/save",
            sha="caa296126883cff596d87d8935842f9db880ef25",
            version="v5",
        ),
        ActionPin(
            action="actions/checkout",
            sha="fbc6f3992d24b796d5a048ff273f7fcc4a7b6c09",
            version="v5",
        ),
        ActionPin(
            action="actions/configure-pages",
            sha="45bfe0192ca1faeb007ade9deae92b16b8254a0d",
            version="v6",
        ),
        ActionPin(
            action="actions/deploy-pages",
            sha="cd2ce8fcbc39b97be8ca5fce6e763baed58fa128",
            version="v5",
        ),
        ActionPin(
            action="actions/download-artifact",
            sha="37930b1c2abaa49bbe596cd826c3c89aef350131",
            version="v7",
        ),
        ActionPin(
            action="actions/setup-node",
            sha="a0853c24544627f65ddf259abe73b1d18a591444",
            version="v5",
        ),
        ActionPin(
            action="actions/setup-python",
            sha="ece7cb06caefa5fff74198d8649806c4678c61a1",
            version="v6",
        ),
        ActionPin(
            action="actions/upload-artifact",
            sha="043fb46d1a93c77aae656e7c1c64a875d1fc6a0a",
            version="v7",
        ),
        ActionPin(
            action="actions/upload-pages-artifact",
            sha="fc324d3547104276b827a68afc52ff2a11cc49c9",
            version="v5",
        ),
        ActionPin(
            action="astral-sh/setup-uv",
            sha="37802adc94f370d6bfd71619e3f0bf239e1f3b78",
            version="v7",
        ),
        ActionPin(
            action="docker/build-push-action",
            sha="53b7df96c91f9c12dcc8a07bcb9ccacbed38856a",
            version="v7",
        ),
        ActionPin(
            action="docker/login-action",
            sha="dbcb813823bdd20940b903addbd779551569679f",
            version="v4",
        ),
        ActionPin(
            action="docker/setup-buildx-action",
            sha="bb05f3f5519dd87d3ba754cc423b652a5edd6d2c",
            version="v4",
        ),
        ActionPin(
            action="pypa/gh-action-pypi-publish",
            sha="dc37677b2e1c63e2034f94d8a5b11f265b73ba33",
            version="v1.14.2",
        ),
        ActionPin(
            action="ruby/setup-ruby",
            sha="95ef2b042f9d7a56d8268cba8559e2842e2ad01b",
            version="v1",
        ),
    )
}
"""Every third-party GitHub Action revision this repository permits.

Pinned to full commit SHAs, not tags: tags are mutable. The trailing version in
``ActionPin.uses`` is load-bearing because workflow review needs a readable
release line while the runner consumes the immutable commit.
"""


def action_ref(action: str) -> str:
    """Return the pinned ``owner/name@sha`` reference for one action."""
    return GITHUB_ACTION_PINS[action].ref
