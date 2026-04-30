"""Export manifest models."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True, kw_only=True)
class ManifestEntry:
    """Source-to-destination mapping and stable identity for one exported file."""

    source: str
    destination: str
    size: int
    sha256: str


@dataclass(frozen=True, slots=True, kw_only=True)
class TransformFileReport:
    """Per-file transform change report for manifest consumers."""

    source: str
    destination: str
    count: int


@dataclass(frozen=True, slots=True, kw_only=True)
class TransformReport:
    """Report for one transform execution.

    `changed` counts files and `count` counts edit occurrences. Keeping both
    values separate makes manifests useful for review without overloading a
    single field.
    """

    id: str
    type: str
    path: str
    changed: int
    count: int
    files: tuple[TransformFileReport, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class ExportManifest:
    """Machine-readable export report.

    The serialized JSON is deterministic and omits elapsed time so two exports
    of the same source tree can be compared byte-for-byte.
    """

    files: tuple[ManifestEntry, ...]
    transforms: tuple[TransformReport, ...]
    elapsed_sec: float

    def to_json(self) -> str:
        """Serialize manifest as deterministic JSON."""
        data = asdict(self)
        del data["elapsed_sec"]
        return json.dumps(data, indent=2, sort_keys=True) + "\n"


def file_entry(source: str, destination: str, path: Path) -> ManifestEntry:
    """Build a manifest entry for a copied file or symlink."""
    data = _manifest_bytes(path)
    return ManifestEntry(
        source=source,
        destination=destination,
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
    )


def _manifest_bytes(path: Path) -> bytes:
    """Return bytes used for deterministic file identity.

    Symlink manifests hash the link target rather than the linked file. That
    matches the exported tree shape and prevents a symlink from depending on
    machine-local target contents.
    """
    if path.is_symlink():
        return path.readlink().as_posix().encode()
    return path.read_bytes()
