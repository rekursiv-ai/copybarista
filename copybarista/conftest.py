"""Package-level pytest fixtures for copybarista.

Exists to bind the autouse XDG isolation fixture at the package root.
``GitRuntime`` defaults its bare-repo cache to ``cache_dir() / "rekursiv-ai"``
(``git.py``), so an unisolated test can read or clobber the developer's -- and,
after export, the installer's -- real clone cache.
"""

from __future__ import annotations

from copybarista.lib.userdirs_fixture import (
    isolate_user_dirs,
    pytest_configure,
)


# Re-exported, not merely imported: an autouse fixture reaches only the
# directory of the conftest that names it, so binding it here is what widens it
# to the whole package.
__all__ = ["isolate_user_dirs", "pytest_configure"]
