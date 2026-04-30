"""Validate that an exported Copybarista tree is safe to publish.

This check is intentionally narrower than the normal test suite. It catches
sync mistakes that make PRs hard to review: private fixtures, build artifacts,
virtual environments, cache directories, bytecode, nested VCS metadata, and
missing workflow files. Run it against the raw export before replacing the
public checkout, and run it again in the public repository with
`--allow-root-git`.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

BLOCKED_DIR_NAMES = frozenset(
    (
        ".hg",
        ".pytest_cache",
        ".ruff_cache",
        ".svn",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "htmlcov",
    )
)
REQUIRED_PATHS = (
    ".github/workflows/ci.yml",
    ".github/workflows/sync-to-source.yml",
    "LICENSE",
    "README.md",
    "copybarista",
    "pyproject.toml",
    "scripts",
)


def main(argv: list[str] | None = None) -> None:
    """Run the release-tree policy check."""
    args = _parser().parse_args(argv)
    errors = check_tree(root=Path(args.root), allow_root_git=args.allow_root_git)
    if errors:
        for error in errors:
            sys.stderr.write(f"{error}\n")
        sys.exit(1)


def check_tree(*, root: Path, allow_root_git: bool = False) -> tuple[str, ...]:
    """Return release-tree policy violations.

    Args:
      root: Exported tree or public checkout root.
      allow_root_git: Whether a root-level `.git` checkout directory is allowed.

    Returns:
      errors: Human-readable policy violations.

    """
    if not root.is_dir():
        return (f"Release tree root does not exist: {root}",)
    errors = [
        f"Missing required release path: {required}"
        for required in REQUIRED_PATHS
        if not (root / required).exists()
    ]
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        errors.extend(_path_errors(rel=rel, allow_root_git=allow_root_git))
    return tuple(errors)


def _parser() -> argparse.ArgumentParser:
    """Build the release-tree validation CLI parser."""
    parser = argparse.ArgumentParser(
        description="Validate Copybarista public release tree policy."
    )
    parser.add_argument("root")
    parser.add_argument("--allow-root-git", action="store_true")
    return parser


def _path_errors(*, rel: str, allow_root_git: bool) -> tuple[str, ...]:
    """Return release-policy errors for one relative path."""
    parts = Path(rel).parts
    errors: list[str] = []
    if allow_root_git and parts and parts[0] == ".git":
        return ()
    if ".git" in parts:
        errors.append(f"VCS metadata must not be exported: {rel}")
    if parts and parts[0] == "private":
        errors.append(f"Private implementation files must not be exported: {rel}")
    if parts and parts[0] == ".coverage":
        errors.append(f"Coverage data must not be exported: {rel}")
    for part in parts:
        if part in BLOCKED_DIR_NAMES:
            errors.append(f"Generated directory must not be exported: {rel}")
            break
        if part.endswith(".egg-info"):
            errors.append(f"Build metadata must not be exported: {rel}")
            break
    if rel.endswith(".pyc"):
        errors.append(f"Python bytecode must not be exported: {rel}")
    return tuple(errors)


if __name__ == "__main__":
    main()
