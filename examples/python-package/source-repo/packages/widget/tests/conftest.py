"""Pytest setup for the source package example."""

from __future__ import annotations

from pathlib import Path
from typing import Final

import sys


_CWD: Final = Path(__file__).resolve().parent


sys.path.insert(0, str(_CWD.parents[2]))
