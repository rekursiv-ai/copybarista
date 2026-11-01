"""Tests for the Mermaid-to-WebP diagram renderer."""

from __future__ import annotations

from dataclasses import dataclass, field
from subprocess import CompletedProcess
from typing import TYPE_CHECKING

import shutil
import subprocess
import sys

import pytest

from copybarista.scripts import render_diagram


if TYPE_CHECKING:
    from pathlib import Path


def test_main_renders_beside_its_input_by_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "flow.mmd"
    source.write_text("graph TD; a-->b\n", encoding="utf-8")
    tools = _Tools.installed(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["render_diagram.py", str(source)])

    assert render_diagram.main() == 0

    (mmdc, mmdc_options), (cwebp, cwebp_options) = tools.ran
    png = mmdc[mmdc.index("--output") + 1]
    assert mmdc == [
        "/opt/tools/npx",
        "--yes",
        "--package",
        "@mermaid-js/mermaid-cli",
        "mmdc",
        "--input",
        str(source),
        "--output",
        png,
        "--width",
        "1400",
        "--scale",
        "2",
        "--backgroundColor",
        "white",
    ]
    assert cwebp == [
        "/opt/tools/cwebp",
        "-lossless",
        "-z",
        "9",
        "-quiet",
        png,
        "-o",
        str(tmp_path / "flow.webp"),
    ]
    assert mmdc_options == cwebp_options == {"check": True}
    assert capsys.readouterr().out == f"wrote {tmp_path / 'flow.webp'}\n"


def test_main_writes_an_explicit_output_under_new_directories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "flow.mmd"
    source.write_text("graph TD; a-->b\n", encoding="utf-8")
    output = tmp_path / "assets" / "social" / "flow.webp"
    tools = _Tools.installed(monkeypatch)
    monkeypatch.setattr(
        sys,
        "argv",
        ["render_diagram.py", str(source), "-o", str(output), "--width", "1100"],
    )

    assert render_diagram.main() == 0

    (mmdc, _), (cwebp, _) = tools.ran
    assert mmdc[mmdc.index("--width") + 1] == "1100"
    assert cwebp[-1] == str(output)
    assert output.parent.is_dir()


def test_main_refuses_a_missing_input_and_runs_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tools = _Tools.installed(monkeypatch)
    missing = tmp_path / "missing.mmd"
    monkeypatch.setattr(sys, "argv", ["render_diagram.py", str(missing)])

    assert render_diagram.main() == 1

    assert capsys.readouterr().err == f"input not found: {missing}\n"
    assert tools.ran == []


def test_render_names_a_tool_missing_from_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools = _Tools.installed(monkeypatch, names=("npx",))

    with pytest.raises(SystemExit, match="required tool not on PATH: cwebp"):
        render_diagram.render(
            tmp_path / "flow.mmd",
            tmp_path / "flow.webp",
            width=1400,
            scale=2,
        )

    assert tools.ran == []


@dataclass(slots=True, kw_only=True)
class _Tools:
    """Answer ``shutil.which`` for installed tools and record ``subprocess.run``."""

    names: tuple[str, ...]
    ran: list[tuple[list[str], dict[str, object]]] = field(default_factory=list)

    @classmethod
    def installed(
        cls,
        monkeypatch: pytest.MonkeyPatch,
        *,
        names: tuple[str, ...] = ("npx", "cwebp"),
    ) -> _Tools:
        tools = cls(names=names)
        monkeypatch.setattr(shutil, "which", tools.which)
        monkeypatch.setattr(subprocess, "run", tools.run)
        return tools

    def which(self, name: str) -> str | None:
        if name not in ("npx", "cwebp"):
            raise AssertionError(f"render_diagram looked up an unmodeled tool: {name}")
        return f"/opt/tools/{name}" if name in self.names else None

    def run(self, argv: list[str], **options: object) -> CompletedProcess[str]:
        if argv[0] not in (f"/opt/tools/{name}" for name in self.names):
            raise AssertionError(f"render_diagram ran an unresolved tool: {argv}")
        self.ran.append((argv, options))
        return CompletedProcess(argv, 0)


if __name__ == "__main__":
    from copybarista.lib.testing.runner import test_main

    test_main(__file__)
