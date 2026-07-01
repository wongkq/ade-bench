"""ADE-bench LLM session trace: record & replay.

Public API:
    TraceManager   — start / stop the host-side mock LLM server
    TraceMode      — enum: record | replay | off
    TraceError     — base exception
"""

from ade_bench.trace.exceptions import (
    TraceCorruptedError,
    TraceError,
    TraceMismatchError,
    TraceMissingError,
)
from ade_bench.trace.manager import TraceManager
from ade_bench.trace.models import TraceConfig, TraceEvent, TraceMode

__all__ = [
    "TraceManager",
    "TraceMode",
    "TraceConfig",
    "TraceEvent",
    "TraceError",
    "TraceCorruptedError",
    "TraceMismatchError",
    "TraceMissingError",
]