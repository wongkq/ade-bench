"""Pydantic data models for trace events and configuration."""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field


class TraceMode(str, Enum):
    """Operating mode of the trace subsystem."""

    OFF = "off"
    RECORD = "record"
    REPLAY = "replay"


class TraceConfig(BaseModel):
    """Configuration consumed by TraceManager.

    Either `record_trace_dir` (record mode) or `replay_trace_file` (replay
    mode) must be set. Setting both raises TraceConfigError.
    """

    mode: TraceMode = TraceMode.OFF
    record_trace_dir: Optional[Path] = None
    replay_trace_file: Optional[Path] = None
    on_mismatch: str = "error"  # error | fallback_seq | fallback_hash
    health_timeout_sec: float = 30.0
    port: Optional[int] = None  # None = auto-pick

    def validate_(self) -> None:
        """Raise TraceConfigError on invalid combinations. Called by TraceManager."""
        from ade_bench.trace.exceptions import TraceConfigError

        if self.mode == TraceMode.OFF:
            return
        if self.mode == TraceMode.RECORD:
            if self.record_trace_dir is None:
                raise TraceConfigError(
                    "RECORD mode requires record_trace_dir to be set"
                )
        elif self.mode == TraceMode.REPLAY:
            if self.replay_trace_file is None:
                raise TraceConfigError(
                    "REPLAY mode requires replay_trace_file to be set"
                )


class TraceEvent(BaseModel):
    """A single JSONL row in the trace file.

    Mirrors the Claude CLI session log schema (event_id, session_id,
    timestamp, type, message) with extra ADE-bench-only fields for
    replay matching and instrumentation.
    """

    event_id: str
    session_id: str
    schema_version: str = "ade-bench-trace/v1"
    timestamp: str
    type: str  # user | assistant | system

    # Standard message envelope (matches Claude session format).
    message: dict[str, Any] = Field(default_factory=dict)

    # Assistant-event-only fields.
    model: Optional[str] = None
    stop_reason: Optional[str] = None
    usage: Optional[dict[str, int]] = None

    # ADE-bench-only fields (used for replay matching).
    request_hash: Optional[str] = None
    request_hash_legacy: Optional[str] = None
    elapsed_ms: Optional[int] = None

    # Misc metadata preserved on system init rows.
    mode: Optional[str] = None
    real_base_url: Optional[str] = None