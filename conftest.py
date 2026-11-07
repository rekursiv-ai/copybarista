"""Export-root pytest resource setup for copybarista.

Binds the resource-marker hook and the per-user-directory isolation at the
repository root so they reach tests living OUTSIDE the ``copybarista`` package --
``tests/`` in particular. A conftest hook applies to its own directory and below,
so the package-level conftest alone would leave those tests' resource markers
with no timeout and their per-user directories unisolated.
"""

from copybarista.lib.testing.resources import (
    isolate_user_dirs,
    pytest_collection_modifyitems,
    pytest_configure,
)


__all__ = ["isolate_user_dirs", "pytest_collection_modifyitems", "pytest_configure"]
