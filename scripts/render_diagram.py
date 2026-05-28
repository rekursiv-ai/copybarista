#!/bin/sh
# ruff: noqa: EXE003, D300 -- Polyglot shell/Python script.
# fmt: off
'''' 2>/dev/null #
exec uv --quiet run --no-project python3 "$0" "$@"
Render a Mermaid (.mmd) source file to a lossless WebP asset.

Used to regenerate diagram assets such as ``assets/copybarista-sync.webp``
from their checked-in Mermaid sources. Lossless WebP keeps diagram strokes
and text crisp and is typically 50-60% smaller than the rendered PNG.

Requires Node.js (for ``npx``/mermaid-cli) and ``cwebp`` on PATH.

Usage::

    uv run python scripts/render_diagram.py assets/copybarista-sync.mmd
'''
# fmt: on

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

import argparse
import shutil
import subprocess
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def render(input_path: Path, output_path: Path, width: int, scale: int) -> None:
    """Render ``input_path`` to ``output_path`` via mmdc + cwebp."""
    npx = _require_tool("npx")
    cwebp = _require_tool("cwebp")
    with TemporaryDirectory() as tmp:
        png = Path(tmp) / "diagram.png"
        subprocess.run(  # noqa: S603 -- fixed command with validated file paths.
            [
                npx,
                "--yes",
                "--package",
                "@mermaid-js/mermaid-cli",
                "mmdc",
                "--input",
                str(input_path),
                "--output",
                str(png),
                "--width",
                str(width),
                "--scale",
                str(scale),
                "--backgroundColor",
                "white",
            ],
            check=True,
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(  # noqa: S603 -- fixed command with generated input.
            [
                cwebp,
                "-lossless",
                "-z",
                "9",
                "-quiet",
                str(png),
                "-o",
                str(output_path),
            ],
            check=True,
        )


def _require_tool(name: str) -> str:
    """Return the resolved executable path for a required CLI tool."""
    executable = shutil.which(name)
    if executable is None:
        sys.exit(f"required tool not on PATH: {name}")
    return executable


def main() -> None:
    """Parse arguments and render the requested diagram."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n", 2)[2])
    parser.add_argument(
        "input",
        type=Path,
        help="Mermaid source path (typically ending in .mmd)",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="output WebP path (defaults to input path with .webp extension)",
    )
    parser.add_argument(
        "--width",
        type=int,
        default=1400,
        help="render width in CSS pixels before scaling (default: 1400)",
    )
    parser.add_argument(
        "--scale",
        type=int,
        default=2,
        help="DPI scale factor for HiDPI sharpness (default: 2)",
    )
    args = parser.parse_args()

    if not args.input.is_file():
        sys.exit(f"input not found: {args.input}")

    output = args.output or args.input.with_suffix(".webp")
    render(args.input, output, args.width, args.scale)
    sys.stdout.write(f"wrote {output}\n")


if __name__ == "__main__":
    main()
