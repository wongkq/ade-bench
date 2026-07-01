"""Smoke tests for scripts/mock_llm_server.py.

We boot the Flask app via create_app() in-process (no subprocess) so the
tests stay fast and hermetic. The tests verify:

- /health responds 200
- record mode creates an init system event + records request/response pairs
- replay mode round-trips the recorded SSE stream for matching requests
- replay mode returns 500 on hash mismatch (when --on-mismatch=error)

We DO NOT make real network calls in record mode here — the record path
is exercised end-to-end in a manual integration run.
"""

from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest

from scripts.mock_llm_server import (
    OnMismatchPolicy,
    SCHEMA_VERSION,
    compute_request_hash,
    create_app,
)


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


# ============================================================
# Schema & hashing
# ============================================================


def test_request_hash_stable_across_cache_control():
    """Removing/adding cache_control must not change the normalized hash."""
    req = {
        "model": "claude-opus-4-5-20251101",
        "system": [{"type": "text", "text": "You are helpful."}],
        "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
        "tools": [
            {
                "name": "Bash",
                "description": "run bash",
                "input_schema": {"type": "object"},
                "cache_control": {"type": "ephemeral"},
            }
        ],
    }
    h1 = compute_request_hash(req, normalized=True)
    req2 = json.loads(json.dumps(req))
    req2["tools"][0].pop("cache_control")
    h2 = compute_request_hash(req2, normalized=True)
    assert h1 == h2
    assert h1.startswith("sha256:")


def test_request_hash_changes_on_message_content():
    req_a = {"messages": [{"role": "user", "content": [{"type": "text", "text": "A"}]}]}
    req_b = {"messages": [{"role": "user", "content": [{"type": "text", "text": "B"}]}]}
    assert compute_request_hash(req_a) != compute_request_hash(req_b)


# ============================================================
# /health endpoint
# ============================================================


def test_health_record_mode(tmp_path: Path):
    app = create_app(
        mode="record", out_dir=tmp_path, session_id="smoke", on_mismatch=OnMismatchPolicy.ERROR
    )
    client = app.test_client()
    r = client.get("/health")
    assert r.status_code == 200
    body = r.get_json()
    assert body["status"] == "ok"
    assert body["mode"] == "record"
    assert body["session_id"] == "smoke"


def test_health_replay_mode(tmp_path: Path):
    trace_file = tmp_path / "empty.jsonl"
    trace_file.write_text("")
    app = create_app(mode="replay", trace_file=trace_file)
    client = app.test_client()
    r = client.get("/health")
    assert r.status_code == 200
    body = r.get_json()
    assert body["mode"] == "replay"


# ============================================================
# Record mode: writes system init event
# ============================================================


def test_record_mode_writes_system_init_event(tmp_path: Path):
    session_id = "smoke.1-of-1"
    app = create_app(mode="record", out_dir=tmp_path, session_id=session_id)
    jsonl_path = tmp_path / f"{session_id}.jsonl"
    # The system init event is written eagerly in create_app()
    assert jsonl_path.exists()
    lines = jsonl_path.read_text().strip().split("\n")
    assert len(lines) == 1
    init = json.loads(lines[0])
    assert init["type"] == "system"
    assert init["session_id"] == session_id
    assert init["schema_version"] == SCHEMA_VERSION


# ============================================================
# Replay mode: 500 on miss, success on hit
# ============================================================


def test_replay_miss_returns_500(tmp_path: Path):
    """An empty trace file → any request should be a mismatch and return 500."""
    trace_file = tmp_path / "empty.jsonl"
    trace_file.write_text("")
    app = create_app(
        mode="replay",
        trace_file=trace_file,
        on_mismatch=OnMismatchPolicy.ERROR,
    )
    client = app.test_client()
    r = client.post(
        "/v1/messages",
        json={
            "model": "claude-opus-4-5-20251101",
            "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
            "max_tokens": 16,
        },
    )
    assert r.status_code == 500
    body = r.get_json()
    assert body["type"] == "error"
    assert "no matching recording" in body["error"]["message"]


def test_replay_hit_returns_sse_stream(tmp_path: Path):
    """Pre-populate a JSONL with one matching assistant event and verify
    replay returns a streamable SSE response with status 200."""
    import uuid

    # Construct an assistant event manually using compute_request_hash
    req = {
        "model": "claude-opus-4-5-20251101",
        "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
    }
    h = compute_request_hash(req, normalized=True)
    h_legacy = compute_request_hash(req, normalized=False)
    event = {
        "event_id": str(uuid.uuid4()),
        "session_id": "smoke.1-of-1",
        "schema_version": SCHEMA_VERSION,
        "timestamp": "2026-06-30T00:00:00.000Z",
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": "hello back"}],
            "stop_reason": "end_turn",
        },
        "model": req["model"],
        "stop_reason": "end_turn",
        "usage": {
            "input_tokens": 5,
            "output_tokens": 2,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        },
        "request_hash": h,
        "request_hash_legacy": h_legacy,
        "elapsed_ms": 100,
    }
    trace_file = tmp_path / "smoke.1-of-1.jsonl"
    trace_file.write_text(json.dumps(event) + "\n")

    app = create_app(
        mode="replay", trace_file=trace_file, on_mismatch=OnMismatchPolicy.ERROR
    )
    client = app.test_client()
    r = client.post("/v1/messages", json=req)
    assert r.status_code == 200
    assert "text/event-stream" in r.content_type
    body = r.get_data(as_text=True)
    # Should contain an Anthropic-style SSE event block
    assert "event: message_start" in body
    assert "event: content_block_start" in body
    assert "event: message_stop" in body
    assert "hello back" in body