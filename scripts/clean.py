"""Remove generated Copybarista development artifacts.

The script intentionally deletes only known project-local paths. It avoids a
generic recursive cleanup so fixture directories that model generated files
remain available for tests.
"""

from __future__ import annotations

from pathlib import Path

import argparse
import shutil
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]

DEFAULT_TARGETS = (
    ".coverage",
    ".pytest_cache",
    ".ruff_cache",
    "build",
    "dist",
    "htmlcov",
    "copybarista.egg-info",
    "copybarista/__pycache__",
    "tests/__pycache__",
    "private/__pycache__",
)


def main() -> None:
    """Run the cleanup command."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--venv",
        action="store_true",
        help="also remove the project-local .venv directory",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print paths that would be removed without deleting them",
    )
    args = parser.parse_args()

    targets: list[str] = list(DEFAULT_TARGETS)
    if args.venv:
        targets.append(".venv")

    for relative_target in targets:
        target = _safe_project_path(relative_target)
        if not target.exists() and not target.is_symlink():
            continue
        if args.dry_run:
            sys.stdout.write(f"{target}\n")
            continue
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        else:
            target.unlink()


def _safe_project_path(relative_target: str) -> Path:
    """Return a project-local cleanup path or fail closed."""
    target = (PROJECT_ROOT / relative_target).resolve()
    if target == PROJECT_ROOT or not target.is_relative_to(PROJECT_ROOT):
        raise ValueError(f"Refusing to clean path outside project: {target}")
    return target


if __name__ == "__main__":
    main()
