"""Pytest setup for the source package example."""

from __future__ import annotations

from pathlib import Path

import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
