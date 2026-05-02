"""External command execution boundaries."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import shutil
import subprocess

from copybarista.errors import ExportError


@dataclass(frozen=True, slots=True, kw_only=True)
class CommandResult:
    """Completed command data used by destination implementations."""

    returncode: int
    stdout: str
    stderr: str


@dataclass(frozen=True, slots=True, kw_only=True)
class CommandRunner:
    """Run external commands without invoking a shell."""

    def run(
        self, argv: list[str], *, check: bool = True, cwd: Path | None = None
    ) -> CommandResult:
        """Run a command and return captured output.

        Args:
          argv: Executable and argument vector. It is never passed through a
            shell.
          check: Whether a nonzero exit code should raise.
          cwd: Optional working directory for the command.

        Returns:
          result: Captured return code, stdout, and stderr.

        Raises:
          ExportError: If `check` is true and the command exits nonzero.

        """
        # The caller provides an argument vector, not a shell string.
        result = subprocess.run(  # noqa: S603
            argv,
            check=False,
            capture_output=True,
            cwd=cwd,
            text=True,
        )
        command_result = CommandResult(
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
        )
        if check and command_result.returncode != 0:
            raise ExportError(
                command_result.stderr.strip() or f"Command failed: {' '.join(argv)}"
            )
        return command_result


def resolve_executable(name: str) -> str:
    """Resolve an executable name to an absolute path when possible."""
    return shutil.which(name) or name
