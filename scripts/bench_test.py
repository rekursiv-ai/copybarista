"""Tests for the benchmark helper."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from scripts import bench

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_build_report_times_copybarista_export(tmp_path: Path):
    source_ref = tmp_path / "repo"
    project = source_ref / "project"
    project.mkdir(parents=True)
    (project / "README.md").write_text("hello\n", encoding="utf-8")
    config_path = tmp_path / "copy.barista.toml"
    config_path.write_text(
        """
        [workflow]
        name = "demo"
        mode = "squash"
        source_root = "project"

        [files]
        include = ["**"]
        """,
        encoding="utf-8",
    )

    report = bench.build_report(config_path=config_path, source_ref=source_ref, runs=2)

    assert report.copybarista.name == "copybarista"
    assert len(report.copybarista.runs) == 2
    assert report.copybarista.median_sec > 0
    assert report.copybarista.file_count == 1
    assert report.copybarista.byte_count == len("hello\n")
    assert report.copybarista.destination_mode == "cold"
    assert {
        "cleanup",
        "config_load",
        "destination_write",
        "manifest",
        "stage",
        "stage.copy",
        "stage.final_manifest",
        "stage.initial_manifest",
        "stage.select",
        "stage.transforms",
    }.issubset(report.copybarista.phase_medians_sec)


def test_report_json_is_machine_readable():
    report = bench.BenchmarkReport(
        copybarista=bench.BenchmarkResult(
            name="copybarista",
            runs=(0.1, 0.2, 0.3),
            median_sec=0.2,
        ),
    )

    data = json.loads(report.to_json())

    assert data["copybarista"]["median_sec"] == 0.2
    assert set(data) == {"copybarista"}


@pytest.mark.performance
def test_synthetic_export_performance_has_actionable_timings(tmp_path: Path):
    source_ref = tmp_path / "repo"
    project = source_ref / "project"
    _write_synthetic_tree(project=project, files=2_000)
    config_path = tmp_path / "copy.barista.toml"
    config_path.write_text(
        """
        [workflow]
        name = "synthetic"
        mode = "squash"
        source_root = "project"

        [files]
        include = ["**"]
        exclude = ["**/__pycache__/**", "build/**", "*.egg-info/**"]

        [[transform]]
        type = "replace"
        path = "pkg/**/*.py"
        before = "from internal.copybarista"
        after = "from copybarista"
        """,
        encoding="utf-8",
    )

    report = bench.build_report(config_path=config_path, source_ref=source_ref, runs=3)
    max_seconds = float(os.environ.get("COPYBARISTA_PERF_MAX_SECONDS", "1.50"))

    if report.copybarista.median_sec > max_seconds:
        pytest.fail(report.to_json())
    assert report.copybarista.file_count == 2_000
    assert report.copybarista.byte_count > 0
    assert all(
        report.copybarista.phase_medians_sec[name] > 0
        for name in ("config_load", "stage", "destination_write", "cleanup")
    )


@pytest.mark.performance
def test_self_export_performance_has_recordable_timings():
    source_repo_text = os.environ.get("COPYBARISTA_SOURCE_REPO", "")
    source_root = os.environ.get("COPYBARISTA_SOURCE_ROOT", "")
    if not source_repo_text or not source_root:
        pytest.skip("Set COPYBARISTA_SOURCE_REPO and COPYBARISTA_SOURCE_ROOT")
    source_repo = Path(source_repo_text).resolve()
    if not (source_repo / source_root).is_dir():
        pytest.skip("Source checkout unavailable for self-export performance test")

    report = bench.build_report(
        config_path=PROJECT_ROOT / "copy.barista.toml",
        source_ref=source_repo,
        runs=int(os.environ.get("COPYBARISTA_SELF_PERF_RUNS", "3")),
    )
    max_seconds = float(os.environ.get("COPYBARISTA_SELF_PERF_MAX_SECONDS", "1.25"))

    if report.copybarista.median_sec > max_seconds:
        pytest.fail(report.to_json())
    assert report.copybarista.file_count > 0
    assert report.copybarista.byte_count > 0
    assert "stage" in report.copybarista.phase_medians_sec


def _write_synthetic_tree(*, project: Path, files: int) -> None:
    for idx in range(files):
        package = project / "pkg" / f"group_{idx % 20}"
        package.mkdir(parents=True, exist_ok=True)
        (package / f"module_{idx}.py").write_text(
            f"from internal.copybarista import config\nVALUE = {idx}\n",
            encoding="utf-8",
        )
    cache = project / "pkg" / "__pycache__"
    cache.mkdir(parents=True)
    (cache / "ignored.pyc").write_bytes(b"ignored")
