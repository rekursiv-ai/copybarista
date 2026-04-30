"""Tests for external command execution."""

from __future__ import annotations

import sys

import pytest

from copybarista.commands import CommandRunner, resolve_executable
from copybarista.errors import ExportError


def test_command_runner_returns_captured_output():
    result = CommandRunner().run(
        [sys.executable, "-c", "print('hello')"],
    )

    assert result.returncode == 0
    assert result.stdout == "hello\n"
    assert result.stderr == ""


def test_command_runner_raises_with_stderr_on_failure():
    with pytest.raises(ExportError, match="bad command"):
        CommandRunner().run(
            [
                sys.executable,
                "-c",
                "import sys; sys.stderr.write('bad command'); sys.exit(7)",
            ],
        )


def test_command_runner_can_return_unchecked_failures():
    result = CommandRunner().run(
        [sys.executable, "-c", "import sys; sys.exit(3)"],
        check=False,
    )

    assert result.returncode == 3


def test_resolve_executable_keeps_unknown_names():
    assert resolve_executable("definitely-not-copybarista-tool") == (
        "definitely-not-copybarista-tool"
    )
