"""Setuptools build customization for Copybarista distributions."""

from __future__ import annotations

from collections.abc import Callable
from typing import cast, override

from setuptools import setup
from setuptools.command.build_py import build_py

PackageModule = tuple[str, str, str]


class BuildPyWithoutTests(build_py):
    """Build package modules while leaving adjacent test modules out of wheels."""

    @override
    def find_package_modules(
        self,
        package: str,
        package_dir: str,
    ) -> list[PackageModule]:
        """Return package modules excluding sibling `*_test.py` files.

        Args:
          package: Python package name currently being scanned by setuptools.
          package_dir: Filesystem directory for the package.

        Returns:
          modules: Setuptools package module triples for wheel inclusion.

        """
        finder = cast(
            "Callable[[build_py, str, str], list[PackageModule]]",
            build_py.find_package_modules,
        )
        modules = finder(self, package, package_dir)
        return [
            (pkg, module, path)
            for pkg, module, path in modules
            if not module.endswith("_test")
        ]


setup(cmdclass={"build_py": BuildPyWithoutTests})
