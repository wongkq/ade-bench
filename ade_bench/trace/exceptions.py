"""Custom exceptions for the trace subsystem."""


class TraceError(Exception):
    """Base class for all trace-related errors."""


class TraceMissingError(TraceError):
    """Raised when a referenced trace file does not exist."""


class TraceCorruptedError(TraceError):
    """Raised when a trace file is too damaged to be safely loaded."""


class TraceMismatchError(TraceError):
    """Raised when replay finds no matching recording (and on_mismatch=error)."""


class TraceStartupError(TraceError):
    """Raised when the mock LLM server fails to start."""


class TraceConfigError(TraceError):
    """Raised when trace configuration is invalid (e.g. both record and replay)."""