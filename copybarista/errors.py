"""Copybarista exception types."""


class CopybaristaError(Exception):
    """Base exception for user-facing Copybarista errors."""


class ConfigError(CopybaristaError):
    """Raised when a config file is invalid."""


class GlobError(CopybaristaError):
    """Raised when a glob pattern uses unsupported syntax."""


class ExportError(CopybaristaError):
    """Raised when export execution fails."""


class ImportRequestError(CopybaristaError):
    """Raised when a change request cannot be imported safely."""


class TransformError(CopybaristaError):
    """Raised when a transform fails."""


class OutputMismatchError(CopybaristaError):
    """Raised when generated output differs from the expected output."""
