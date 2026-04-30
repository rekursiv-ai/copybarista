"""Benchmark Copybarista folder export."""

from __future__ import annotations

import argparse
import json
import platform as platform_lib
import shutil
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from statistics import median

from copybarista.config import load_config
from copybarista.destinations import write_folder_destination
from copybarista.manifest import ExportManifest
from copybarista.workflow import WorkflowRunner


@dataclass(frozen=True, slots=True, kw_only=True)
class BenchmarkSample:
    """One timed benchmark run."""

    total_sec: float
    phases_sec: dict[str, float]
    file_count: int
    byte_count: int


@dataclass(frozen=True, slots=True, kw_only=True)
class BenchmarkResult:
    """Timing summary for one benchmark target."""

    name: str
    runs: tuple[float, ...]
    median_sec: float
    samples: tuple[BenchmarkSample, ...] = ()
    phase_medians_sec: dict[str, float] = field(default_factory=dict)
    file_count: int = 0
    byte_count: int = 0
    destination_mode: str = "cold"
    platform: str = field(default_factory=platform_lib.platform)
    python: str = field(default_factory=platform_lib.python_version)


@dataclass(frozen=True, slots=True, kw_only=True)
class BenchmarkReport:
    """Timing report for Copybarista."""

    copybarista: BenchmarkResult

    def to_json(self) -> str:
        """Serialize the benchmark report as deterministic JSON."""
        return json.dumps(asdict(self), indent=2, sort_keys=True) + "\n"


def run_copybarista_benchmark(
    config_path: Path,
    source_ref: Path,
    *,
    runs: int,
) -> BenchmarkResult:
    """Run repeated Copybarista folder exports and return elapsed times."""
    samples: list[BenchmarkSample] = []
    with tempfile.TemporaryDirectory(prefix="copybarista-bench-") as tmp:
        root = Path(tmp)
        samples.extend(
            _run_copybarista_sample(
                config_path=config_path,
                root=root,
                run_id=idx,
                source_ref=source_ref,
            )
            for idx in range(runs)
        )
    return _result(name="copybarista", samples=samples)


def _run_copybarista_sample(
    *,
    config_path: Path,
    root: Path,
    run_id: int,
    source_ref: Path,
) -> BenchmarkSample:
    """Run one Copybarista export sample inside an existing temp root."""
    phases: dict[str, float] = {}
    total_started = time.perf_counter()
    config_started = time.perf_counter()
    config = load_config(config_path)
    phases["config_load"] = time.perf_counter() - config_started

    staging = root / f"staging-{run_id}"
    destination = root / f"copybarista-{run_id}"

    def record_stage_phase(phase: str, elapsed_sec: float) -> None:
        """Record staged workflow sub-phases in the sample phase map."""
        phases[f"stage.{phase}"] = elapsed_sec

    stage_started = time.perf_counter()
    staged_tree = WorkflowRunner(config=config, source_ref=source_ref).stage(
        staging,
        record_phase=record_stage_phase,
    )
    phases["stage"] = time.perf_counter() - stage_started

    write_started = time.perf_counter()
    write_folder_destination(
        staged_tree,
        destination=destination,
        source_ref=source_ref,
        source_root=source_ref / config.source_root,
        replace_existing=True,
    )
    phases["destination_write"] = time.perf_counter() - write_started

    manifest_started = time.perf_counter()
    manifest = ExportManifest(
        files=staged_tree.files,
        transforms=staged_tree.transforms,
        elapsed_sec=time.perf_counter() - total_started,
    )
    phases["manifest"] = time.perf_counter() - manifest_started

    cleanup_started = time.perf_counter()
    shutil.rmtree(destination)
    shutil.rmtree(staging)
    phases["cleanup"] = time.perf_counter() - cleanup_started
    return BenchmarkSample(
        total_sec=time.perf_counter() - total_started,
        phases_sec=phases,
        file_count=len(manifest.files),
        byte_count=sum(entry.size for entry in manifest.files),
    )


def build_report(
    config_path: Path,
    source_ref: Path,
    *,
    runs: int,
) -> BenchmarkReport:
    """Build a benchmark report for Copybarista."""
    copybarista = run_copybarista_benchmark(
        config_path=config_path,
        source_ref=source_ref,
        runs=runs,
    )
    return BenchmarkReport(copybarista=copybarista)


def main() -> None:
    """Run the benchmark helper CLI."""
    args = _parser().parse_args()
    report = build_report(
        config_path=Path(args.config),
        source_ref=Path(args.source_ref),
        runs=args.runs,
    )
    if args.json:
        sys.stdout.write(report.to_json())
    else:
        _print_text_report(report)


def _parser() -> argparse.ArgumentParser:
    """Build the benchmark CLI parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config")
    parser.add_argument("source_ref")
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--json", action="store_true")
    return parser


def _result(name: str, samples: list[BenchmarkSample]) -> BenchmarkResult:
    """Summarize benchmark samples into median timing fields."""
    if not samples:
        raise ValueError("Benchmark requires at least one run")
    timings = [sample.total_sec for sample in samples]
    return BenchmarkResult(
        name=name,
        runs=tuple(timings),
        median_sec=median(timings),
        samples=tuple(samples),
        phase_medians_sec=_phase_medians(samples),
        file_count=samples[-1].file_count,
        byte_count=samples[-1].byte_count,
    )


def _phase_medians(samples: list[BenchmarkSample]) -> dict[str, float]:
    """Return median elapsed time for each named benchmark phase."""
    phases = sorted({name for sample in samples for name in sample.phases_sec})
    return {
        phase: median(
            sample.phases_sec[phase] for sample in samples if phase in sample.phases_sec
        )
        for phase in phases
    }


def _print_text_report(report: BenchmarkReport) -> None:
    """Print a compact human-readable benchmark report."""
    sys.stdout.write(
        f"copybarista median: {report.copybarista.median_sec:.6f}s "
        f"runs={_format_runs(report.copybarista.runs)} "
        f"files={report.copybarista.file_count} "
        f"bytes={report.copybarista.byte_count}\n"
        f"copybarista phases: {_format_phases(report.copybarista.phase_medians_sec)}\n"
    )


def _format_runs(runs: tuple[float, ...]) -> str:
    """Format run timings for text output."""
    return ",".join(f"{run:.6f}" for run in runs)


def _format_phases(phases: dict[str, float]) -> str:
    """Format phase timings for text output."""
    return ", ".join(f"{name}={value:.6f}s" for name, value in phases.items())


if __name__ == "__main__":
    main()
