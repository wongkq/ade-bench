"""
VCR-style mock Anthropic API server for ADE-bench.

Captures or replays Claude Code CLI's traffic to the Anthropic API, storing
each session as a JSONL trace aligned with the Claude session format:

    {"event_id": "...", "session_id": "...", "timestamp": "...",
     "type": "user|assistant|system", "message": {...},
     "model": "...", "stop_reason": "...", "usage": {...},
     "request_hash": "sha256:...", "elapsed_ms": 1234}

Usage:
    # Record
    python scripts/mock_llm_server.py \\
        --mode record \\
        --out-dir /tmp/traces \\
        --session-id airbnb001.base.1-of-1 \\
        --real-base-url https://api.anthropic.com \\
        --port 8765

    # Replay
    python scripts/mock_llm_server.py \\
        --mode replay \\
        --trace-file /tmp/traces/airbnb001.base.1-of-1.jsonl \\
        --port 8765 \\
        --on-mismatch error
"""

import argparse
import hashlib
import json
import os
import socket
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterator

import requests
from flask import Flask, Response, jsonify, request


# ============================================================
# JSONL event schema constants
# ============================================================

SCHEMA_VERSION = "ade-bench-trace/v1"
SUPPORTED_EVENT_TYPES = ("user", "assistant", "system")


class OnMismatchPolicy(str, Enum):
    """Strategy when a replay request's hash has no matching recording."""

    ERROR = "error"              # Return 500 immediately
    FALLBACK_SEQ = "fallback_seq"  # Serve next unconsumed assistant event
    FALLBACK_HASH = "fallback_hash"  # Try legacy hash, then seq


# ============================================================
# Request normalization & hashing
# ============================================================


def _strip_runtime_noise(value: Any) -> Any:
    """Recursively strip cache_control and other runtime-only fields.

    Also collapses:
      - tool_result.content → constant placeholder (bash output varies between
        recording and replay: timestamps, file paths, db file sizes, etc.).
        Anthropic API allows content to be either a string OR a list of blocks.
      - tool_use_id / tool_call_id → deterministic key derived from the
        tool's name + input. Claude CLI generates fresh UUIDs per session, so
        recorded IDs won't match replay IDs even when the conversation shape
        is identical.
      - message id / stop_reason in tool_use blocks where redundant
    """
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        # Detect tool_result content-block
        block_type = value.get("type")
        for k, v in value.items():
            if k in ("cache_control", "cache_creation_input_tokens", "cache_read_input_tokens"):
                continue
            if k == "content" and block_type == "tool_result":
                # Both str and list variants exist — collapse to placeholder
                cleaned[k] = "<stripped-tool-result-content>"
                continue
            if k in ("tool_use_id", "tool_call_id") and isinstance(v, str):
                # Replace with a hashable placeholder. The conversation shape
                # (position in messages array, sibling tool names) is preserved.
                cleaned[k] = "<normalized-tool-id>"
                continue
            cleaned[k] = _strip_runtime_noise(v)
        return cleaned
    if isinstance(value, list):
        return [_strip_runtime_noise(v) for v in value]
    return value


def _normalize_system(system: Any) -> Any:
    """Strip dynamic Claude Code system reminders and cc_version markers."""
    if not isinstance(system, list):
        return system
    cleaned = []
    for block in system:
        if not isinstance(block, dict):
            cleaned.append(block)
            continue
        text = block.get("text", "")
        # Drop runtime-only Claude Code headers/footers
        if "cc_version=" in text:
            continue
        if text.strip().startswith("<system-reminder>") or text.strip().startswith("</system-reminder>"):
            continue
        cleaned.append({k: v for k, v in block.items() if k != "cache_control"})
    return cleaned


def normalize_request(req: dict) -> dict:
    """Build a hashable canonical view of the request, stripping runtime noise."""
    norm = {
        "model": req.get("model"),
        "system": _normalize_system(req.get("system", [])),
        "tools": _strip_runtime_noise(req.get("tools", [])),
        "messages": _strip_runtime_noise(req.get("messages", [])),
        "max_tokens": req.get("max_tokens"),
        "temperature": req.get("temperature"),
    }
    return norm


def compute_request_hash(req: dict, normalized: bool = True) -> str:
    """Compute SHA-256 of the canonical request body."""
    if normalized:
        payload = normalize_request(req)
    else:
        payload = req
    canonical = json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


# ============================================================
# SSE event builders (replay side)
# ============================================================


def assistant_message_to_sse(message: dict, model: str | None) -> Iterator[str]:
    """Convert a recorded assistant message into Anthropic SSE stream events."""
    msg_id = f"msg_{uuid.uuid4().hex[:24]}"
    # 1. message_start
    yield "event: message_start\n"
    yield "data: " + json.dumps({
        "type": "message_start",
        "message": {
            "id": msg_id,
            "type": "message",
            "role": "assistant",
            "model": model or "claude-unknown",
            "content": [],
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
        },
    }) + "\n\n"

    # 2. content_block_start / delta / stop per block
    for idx, block in enumerate(message.get("content", [])):
        btype = block.get("type")
        if btype == "text":
            yield "event: content_block_start\n"
            yield "data: " + json.dumps({
                "type": "content_block_start",
                "index": idx,
                "content_block": {"type": "text", "text": ""},
            }) + "\n\n"

            yield "event: content_block_delta\n"
            yield "data: " + json.dumps({
                "type": "content_block_delta",
                "index": idx,
                "delta": {"type": "text_delta", "text": block.get("text", "")},
            }) + "\n\n"

            yield "event: content_block_stop\n"
            yield "data: " + json.dumps({
                "type": "content_block_stop",
                "index": idx,
            }) + "\n\n"

        elif btype == "tool_use":
            yield "event: content_block_start\n"
            yield "data: " + json.dumps({
                "type": "content_block_start",
                "index": idx,
                "content_block": {
                    "type": "tool_use",
                    "id": block.get("id"),
                    "name": block.get("name"),
                    "input": {},
                },
            }) + "\n\n"

            input_json = json.dumps(block.get("input", {}))
            yield "event: content_block_delta\n"
            yield "data: " + json.dumps({
                "type": "content_block_delta",
                "index": idx,
                "delta": {"type": "input_json_delta", "partial_json": input_json},
            }) + "\n\n"

            yield "event: content_block_stop\n"
            yield "data: " + json.dumps({
                "type": "content_block_stop",
                "index": idx,
            }) + "\n\n"

        elif btype == "thinking":
            # Skip - thinking blocks are not part of Anthropic public API yet
            continue
        else:
            # Unknown block: emit as text
            yield "event: content_block_start\n"
            yield "data: " + json.dumps({
                "type": "content_block_start",
                "index": idx,
                "content_block": {"type": "text", "text": ""},
            }) + "\n\n"
            yield "event: content_block_delta\n"
            yield "data: " + json.dumps({
                "type": "content_block_delta",
                "index": idx,
                "delta": {"type": "text_delta", "text": json.dumps(block)},
            }) + "\n\n"
            yield "event: content_block_stop\n"
            yield "data: " + json.dumps({
                "type": "content_block_stop",
                "index": idx,
            }) + "\n\n"

    # 3. message_delta with stop_reason
    yield "event: message_delta\n"
    yield "data: " + json.dumps({
        "type": "message_delta",
        "delta": {
            "stop_reason": message.get("stop_reason", "end_turn"),
            "stop_sequence": None,
        },
        "usage": {"output_tokens": 0},
    }) + "\n\n"

    # 4. message_stop
    yield "event: message_stop\n"
    yield "data: " + json.dumps({"type": "message_stop"}) + "\n\n"


# ============================================================
# JSONL writer (thread-safe, atomic-per-line)
# ============================================================


class JSONLWriter:
    """Append-only JSONL writer with fsync per line."""

    def __init__(self, path: Path):
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        # Open in append mode; existing file is preserved.
        self._fh = open(self._path, "a", encoding="utf-8", buffering=1)

    def append(self, event: dict) -> None:
        line = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            self._fh.write(line + "\n")
            self._fh.flush()
            try:
                os.fsync(self._fh.fileno())
            except OSError:
                pass  # some FS (NFS) may not support fsync

    def close(self) -> None:
        with self._lock:
            if not self._fh.closed:
                self._fh.flush()
                try:
                    os.fsync(self._fh.fileno())
                except OSError:
                    pass
                self._fh.close()


# ============================================================
# Replay store
# ============================================================


class ReplayStore:
    """Loads JSONL trace file and serves recorded events by request hash."""

    def __init__(self, path: Path, on_mismatch: OnMismatchPolicy):
        self._path = path
        self._on_mismatch = on_mismatch
        self._lock = threading.Lock()
        self._by_normalized_hash: dict[str, dict] = {}
        self._by_legacy_hash: dict[str, dict] = {}
        self._seq: list[dict] = []  # ordered assistant events for fallback_seq
        self._seq_cursor = 0
        self._corrupted_lines = 0
        self._total_lines = 0
        self._hit_count = 0
        self._fallback_count = 0
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            raise FileNotFoundError(f"Trace file not found: {self._path}")
        with open(self._path, "r", encoding="utf-8") as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line:
                    continue
                self._total_lines += 1
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    self._corrupted_lines += 1
                    continue
                if not isinstance(event, dict):
                    self._corrupted_lines += 1
                    continue
                etype = event.get("type")
                if etype == "assistant":
                    nh = event.get("request_hash")
                    lh = event.get("request_hash_legacy")
                    if nh:
                        self._by_normalized_hash[nh] = event
                    if lh:
                        self._by_legacy_hash[lh] = event
                    self._seq.append(event)

        corrupt_ratio = (
            self._corrupted_lines / self._total_lines if self._total_lines else 0.0
        )
        if self._total_lines > 0 and corrupt_ratio > 0.5:
            raise ValueError(
                f"Trace file {self._path} has >50% corrupted lines "
                f"({self._corrupted_lines}/{self._total_lines}). Refusing to load."
            )

    def lookup(self, request: dict) -> tuple[dict | None, str]:
        """Look up a recording by request hash.

        Returns (event, match_type) where match_type is one of:
          "normalized" | "legacy" | "fallback_seq" | "miss"
        """
        with self._lock:
            nh = compute_request_hash(request, normalized=True)
            lh = compute_request_hash(request, normalized=False)
            if nh in self._by_normalized_hash:
                self._hit_count += 1
                return self._by_normalized_hash[nh], "normalized"
            sys.stderr.write(
                f"[mock_llm_server] replay miss: model={request.get('model')} "
                f"msgs={len(request.get('messages',[]))} nh={nh[:16]}.. lh={lh[:16]}.. "
                f"indexed_norm={len(self._by_normalized_hash)}\n"
            )
            sys.stderr.flush()
            if self._on_mismatch == OnMismatchPolicy.FALLBACK_HASH and lh in self._by_legacy_hash:
                self._hit_count += 1
                self._fallback_count += 1
                return self._by_legacy_hash[lh], "legacy"
            if self._on_mismatch in (OnMismatchPolicy.FALLBACK_SEQ, OnMismatchPolicy.FALLBACK_HASH):
                if self._seq_cursor < len(self._seq):
                    event = self._seq[self._seq_cursor]
                    self._seq_cursor += 1
                    self._fallback_count += 1
                    return event, "fallback_seq"
            return None, "miss"

    def reset(self) -> None:
        with self._lock:
            self._seq_cursor = 0

    def stats(self) -> dict:
        with self._lock:
            return {
                "total_lines": self._total_lines,
                "corrupted_lines": self._corrupted_lines,
                "assistant_events": len(self._seq),
                "hit_count": self._hit_count,
                "fallback_count": self._fallback_count,
                "seq_cursor": self._seq_cursor,
            }


# ============================================================
# Flask app factory
# ============================================================


def create_app(
    mode: str,
    out_dir: Path | None = None,
    session_id: str = "default",
    real_base_url: str = "https://api.anthropic.com",
    trace_file: Path | None = None,
    on_mismatch: OnMismatchPolicy = OnMismatchPolicy.ERROR,
) -> Flask:
    """Build the Flask app. mode in {'record', 'replay'}."""
    app = Flask(__name__)
    app.config["MODE"] = mode
    app.config["SESSION_ID"] = session_id
    app.config["REAL_BASE_URL"] = real_base_url.rstrip("/")
    app.config["ON_MISMATCH"] = on_mismatch
    app.config["FORWARD_COUNT"] = 0
    app.config["WRITE_COUNT"] = 0
    app.config["REPLAY_MISS_COUNT"] = 0

    writer: JSONLWriter | None = None
    store: ReplayStore | None = None

    if mode == "record":
        if out_dir is None:
            raise ValueError("record mode requires --out-dir")
        writer = JSONLWriter(out_dir / f"{session_id}.jsonl")
        # Always emit a system init event as the first line.
        writer.append({
            "event_id": str(uuid.uuid4()),
            "session_id": session_id,
            "schema_version": SCHEMA_VERSION,
            "timestamp": _utc_now_iso(),
            "type": "system",
            "message": {
                "role": "system",
                "content": [
                    {"type": "text", "text": "ADE-bench trace session initialized."}
                ],
            },
            "mode": "record",
            "real_base_url": real_base_url,
        })
        app.config["WRITE_COUNT"] = 1
    elif mode == "replay":
        if trace_file is None:
            raise ValueError("replay mode requires --trace-file")
        store = ReplayStore(trace_file, on_mismatch)
        app.config["REPLAY_STORE"] = store
    else:
        raise ValueError(f"Unknown mode: {mode}")

    # ========================
    # Health & admin endpoints
    # ========================

    @app.get("/health")
    def health():
        body = {"status": "ok", "mode": mode, "session_id": session_id}
        if store is not None:
            body["trace_file"] = str(trace_file)
            body.update(store.stats())
        elif writer is not None:
            body["out_dir"] = str(out_dir)
            body["write_count"] = app.config["WRITE_COUNT"]
        return jsonify(body), 200

    @app.post("/_reset")
    def reset():
        if store is not None:
            store.reset()
            return jsonify({"reset": True, "cursor": store._seq_cursor}), 200
        return jsonify({"reset": False, "reason": "not in replay mode"}), 400

    @app.get("/_stats")
    def stats():
        body = {
            "mode": mode,
            "session_id": session_id,
            "forward_count": app.config["FORWARD_COUNT"],
            "write_count": app.config["WRITE_COUNT"],
            "replay_miss_count": app.config["REPLAY_MISS_COUNT"],
        }
        if store is not None:
            body["replay"] = store.stats()
        return jsonify(body), 200

    @app.get("/v1/models")
    def list_models():
        if mode == "record":
            # Forward to real API
            try:
                r = requests.get(
                    f"{app.config['REAL_BASE_URL']}/v1/models",
                    headers=_forward_headers(),
                    timeout=30,
                )
                app.config["FORWARD_COUNT"] += 1
                return Response(r.content, status=r.status_code, content_type=r.headers.get("content-type", "application/json"))
            except requests.RequestException as e:
                return jsonify({"error": str(e)}), 502
        # Replay: return empty list
        return jsonify({"data": [], "has_more": False, "first_id": None, "last_id": None}), 200

    # ========================
    # Main endpoint: /v1/messages
    # ========================

    @app.post("/v1/messages")
    def messages():
        if mode == "record":
            return _handle_record()
        else:
            return _handle_replay()

    def _handle_record() -> Response:
        # Read raw body so we can both parse it and forward it
        raw_body = request.get_data()
        try:
            req_dict = json.loads(raw_body)
        except json.JSONDecodeError:
            return jsonify({"type": "error", "error": {"type": "invalid_request_error", "message": "invalid JSON"}}), 400

        # Write the user event(s) — find the latest user message(s) with tool_result
        # In a streaming conversation, the client always sends the latest user turn,
        # which may include tool_result blocks if the previous turn was a tool_use.
        if req_dict.get("messages"):
            user_event = _build_user_event(req_dict, session_id)
            if user_event:
                writer.append(user_event)
                app.config["WRITE_COUNT"] += 1

        # Force stream=true on the upstream call so we always get SSE back.
        # Some Anthropic-compatible providers (e.g. minimax) return a non-streaming
        # JSON envelope when the client doesn't request streaming — but Claude CLI
        # *always* expects SSE and would otherwise fail with "Failed to parse JSON".
        # Mutating the body here is safe because we already parsed a copy above.
        forward_body = raw_body
        if not req_dict.get("stream"):
            try:
                mutated = json.dumps({**req_dict, "stream": True})
                forward_body = mutated.encode("utf-8")
            except (TypeError, ValueError):
                pass  # fall back to raw_body if dict isn't JSON-serializable

        # Forward to real Anthropic API, streaming
        forward_headers = _forward_headers()
        forward_headers["content-type"] = "application/json"
        # Disable upstream compression — we forward chunks byte-for-byte to the
        # Claude CLI, which would see a binary zstd/gzip blob and choke with
        # "API returned an empty or malformed response (HTTP 200)".
        forward_headers["accept-encoding"] = "identity"
        try:
            upstream = requests.post(
                f"{app.config['REAL_BASE_URL']}/v1/messages",
                headers=forward_headers,
                data=forward_body,
                stream=True,
                timeout=300,
            )
        except requests.RequestException as e:
            return jsonify({"type": "error", "error": {"type": "api_error", "message": f"upstream error: {e}"}}), 502

        if upstream.status_code >= 400:
            # Forward error verbatim
            body = upstream.content
            try:
                upstream.close()
            except Exception:
                pass
            return Response(body, status=upstream.status_code, content_type=upstream.headers.get("content-type", "application/json"))

        # Stream the SSE response, collect the assistant message, and tee bytes to client
        return Response(
            _stream_and_capture(upstream, req_dict, writer, app, session_id),
            status=upstream.status_code,
            content_type=upstream.headers.get("content-type", "text/event-stream"),
        )

    def _handle_replay() -> Response:
        try:
            req_dict = request.get_json(force=True, silent=False)
        except Exception:
            return jsonify({"type": "error", "error": {"type": "invalid_request_error", "message": "invalid JSON"}}), 400

        if not isinstance(req_dict, dict):
            return jsonify({"type": "error", "error": {"type": "invalid_request_error", "message": "expected object body"}}), 400

        # Optional user-event recording for inspection (write-through disabled by default)
        event, match_type = store.lookup(req_dict)
        if event is None:
            app.config["REPLAY_MISS_COUNT"] += 1
            if on_mismatch == OnMismatchPolicy.ERROR:
                return jsonify({
                    "type": "error",
                    "error": {
                        "type": "api_error",
                        "message": (
                            "no matching recording and recordings exhausted. "
                            "Use --on-mismatch fallback_seq or fallback_hash to override."
                        ),
                    },
                }), 500
            # Should not reach here given store.lookup semantics
            return jsonify({"type": "error", "error": {"type": "api_error", "message": "no matching recording"}}), 500

        message = event.get("message", {})
        model = event.get("model")
        sys.stderr.write(
            f"[mock_llm_server] replay hit: match={match_type} session={session_id}\n"
        )
        sys.stderr.flush()

        return Response(
            assistant_message_to_sse(message, model),
            status=200,
            content_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @app.teardown_appcontext
    def _cleanup(_exc):
        pass

    # Stash writer/store on app for stop hook
    app.config["_WRITER"] = writer
    app.config["_STORE"] = store
    return app


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _forward_headers() -> dict:
    """Headers to forward to upstream Anthropic API."""
    drop = {"host", "content-length", "transfer-encoding", "connection"}
    fwd = {}
    for k, v in request.headers.items():
        if k.lower() not in drop:
            fwd[k] = v
    fwd.setdefault("anthropic-version", "2023-06-01")
    return fwd


def _build_user_event(req_dict: dict, session_id: str) -> dict | None:
    """Build a 'user' event from the latest user message in the request."""
    messages = req_dict.get("messages", [])
    if not messages:
        return None
    # Find last user-role message
    last_user = None
    for m in reversed(messages):
        if m.get("role") == "user":
            last_user = m
            break
    if last_user is None:
        return None
    content = last_user.get("content", [])
    # content can be a string or a list of blocks
    if isinstance(content, str):
        content_blocks = [{"type": "text", "text": content}]
    else:
        content_blocks = content
    return {
        "event_id": str(uuid.uuid4()),
        "session_id": session_id,
        "schema_version": SCHEMA_VERSION,
        "timestamp": _utc_now_iso(),
        "type": "user",
        "message": {"role": "user", "content": content_blocks},
        "request_hash": compute_request_hash(req_dict, normalized=True),
        "request_hash_legacy": compute_request_hash(req_dict, normalized=False),
    }


def _stream_and_capture(upstream, req_dict: dict, writer: JSONLWriter, app: Flask, session_id: str):
    """Generator that streams SSE bytes from upstream to client and captures the full assistant message."""
    app.config["FORWARD_COUNT"] += 1

    started_at = time.time()
    # Anthropic SSE format: lines beginning with "event:" or "data:", separated by "\n\n"
    # We buffer until we see "message_stop".
    event_blocks: list[tuple[str, str]] = []  # list of (event_name, data_json)
    current_event = None
    current_data_parts: list[str] = []
    raw_buffer = ""  # for forwarding verbatim

    def _flush_block():
        if current_event is None and not current_data_parts:
            return None
        data_text = "\n".join(current_data_parts)
        return (current_event or "message", data_text)

    try:
        for chunk in upstream.iter_content(chunk_size=4096, decode_unicode=True):
            if not chunk:
                continue
            # Forward verbatim
            yield chunk

            raw_buffer += chunk
            # Parse SSE events
            # Each event ends with blank line (\n\n); split and process
            # We use a simple state machine
            for line in chunk.split("\n"):
                if line.startswith("event:"):
                    current_event = line[len("event:"):].strip()
                elif line.startswith("data:"):
                    current_data_parts.append(line[len("data:"):].strip())
                elif line.strip() == "":
                    # end of event
                    block = _flush_block()
                    if block is not None:
                        event_blocks.append(block)
                        if block[0] == "message_stop":
                            # build assistant event and write it
                            assistant_event = _build_assistant_event(
                                event_blocks, req_dict, session_id, started_at
                            )
                            if assistant_event is not None:
                                writer.append(assistant_event)
                                app.config["WRITE_COUNT"] += 1
                            # reset for next message
                            event_blocks = []
                    current_event = None
                    current_data_parts = []

        # Flush any trailing block
        block = _flush_block()
        if block is not None:
            event_blocks.append(block)
            if block[0] == "message_stop":
                assistant_event = _build_assistant_event(
                    event_blocks, req_dict, session_id, started_at
                )
                if assistant_event is not None:
                    writer.append(assistant_event)
                    app.config["WRITE_COUNT"] += 1
    finally:
        try:
            upstream.close()
        except Exception:
            pass


def _build_assistant_event(
    event_blocks: list[tuple[str, str]],
    req_dict: dict,
    session_id: str,
    started_at: float,
) -> dict | None:
    """Reassemble SSE blocks into a single assistant message event."""
    if not event_blocks:
        return None
    # Parse each data: payload as JSON
    parsed: list[tuple[str, dict]] = []
    for name, data_text in event_blocks:
        if not data_text:
            continue
        try:
            parsed.append((name, json.loads(data_text)))
        except json.JSONDecodeError:
            continue

    # Find message_start to get model and initial usage
    model = req_dict.get("model")
    usage_in = {"input_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
    stop_reason = None
    content_blocks: dict[int, dict] = {}

    for name, payload in parsed:
        etype = payload.get("type")
        if etype == "message_start":
            msg = payload.get("message", {})
            model = msg.get("model", model)
            u = msg.get("usage", {})
            usage_in = {
                "input_tokens": u.get("input_tokens", 0),
                "cache_creation_input_tokens": u.get("cache_creation_input_tokens", 0),
                "cache_read_input_tokens": u.get("cache_read_input_tokens", 0),
            }
        elif etype == "content_block_start":
            idx = payload.get("index")
            block = payload.get("content_block", {})
            content_blocks[idx] = dict(block)
            # text blocks start with empty text
            if block.get("type") == "text":
                content_blocks[idx]["text"] = block.get("text", "")
            elif block.get("type") == "tool_use":
                content_blocks[idx]["input"] = block.get("input", {})
        elif etype == "content_block_delta":
            idx = payload.get("index")
            delta = payload.get("delta", {})
            block = content_blocks.get(idx)
            if block is None:
                continue
            if delta.get("type") == "text_delta":
                block["text"] = block.get("text", "") + delta.get("text", "")
            elif delta.get("type") == "input_json_delta":
                # Collect partial JSON and parse at the end. Simplification: store as string.
                existing = block.get("_partial_input", "")
                block["_partial_input"] = existing + delta.get("partial_json", "")
            elif delta.get("type") == "thinking_delta":
                block.setdefault("thinking", "")
                block["thinking"] += delta.get("thinking", "")
        elif etype == "message_delta":
            delta = payload.get("delta", {})
            stop_reason = delta.get("stop_reason", stop_reason)
        elif etype == "message_stop":
            pass

    # Materialize partial input JSON
    final_blocks: list[dict] = []
    for idx in sorted(content_blocks.keys()):
        b = dict(content_blocks[idx])
        if "_partial_input" in b:
            try:
                b["input"] = json.loads(b["_partial_input"])
            except json.JSONDecodeError:
                b["input"] = {}
            b.pop("_partial_input", None)
        # Drop synthetic-only fields
        b.pop("text", None) if b.get("type") != "text" else None
        final_blocks.append(b)

    # Compute elapsed
    elapsed_ms = int((time.time() - started_at) * 1000)

    return {
        "event_id": str(uuid.uuid4()),
        "session_id": session_id,
        "schema_version": SCHEMA_VERSION,
        "timestamp": _utc_now_iso(),
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": final_blocks,
            "stop_reason": stop_reason,
        },
        "model": model,
        "stop_reason": stop_reason,
        "usage": {
            "input_tokens": usage_in["input_tokens"],
            "output_tokens": 0,  # not always surfaced in stream; left 0 for downstream
            "cache_creation_input_tokens": usage_in["cache_creation_input_tokens"],
            "cache_read_input_tokens": usage_in["cache_read_input_tokens"],
        },
        "request_hash": compute_request_hash(req_dict, normalized=True),
        "request_hash_legacy": compute_request_hash(req_dict, normalized=False),
        "elapsed_ms": elapsed_ms,
    }


# ============================================================
# CLI entrypoint
# ============================================================


def _pick_free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "mock_llm_server")
    parser.add_argument("--mode", choices=("record", "replay"), required=True)
    parser.add_argument("--port", type=int, default=0, help="Bind port (0 = auto-pick)")
    parser.add_argument(
        "--host",
        default="0.0.0.0",
        help=(
            "Bind address. Default 0.0.0.0 because the container reaches "
            "us via host.docker.internal:host-gateway (host's external IP), "
            "not 127.0.0.1."
        ),
    )
    parser.add_argument("--session-id", default="default")
    parser.add_argument("--out-dir", type=Path, default=None, help="Record mode: output directory for JSONL")
    parser.add_argument("--trace-file", type=Path, default=None, help="Replay mode: input JSONL trace file")
    parser.add_argument("--real-base-url", default=os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com"))
    parser.add_argument("--real-api-key", default=os.environ.get("ANTHROPIC_API_KEY", ""))
    parser.add_argument("--on-mismatch", choices=[p.value for p in OnMismatchPolicy], default=OnMismatchPolicy.ERROR.value)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    port = args.port or _pick_free_port()
    on_mismatch = OnMismatchPolicy(args.on_mismatch)

    # Echo selected port to stdout so the parent process can parse it
    print(f"MOCK_LLM_SERVER_PORT={port}", flush=True)
    print(f"MOCK_LLM_SERVER_MODE={args.mode}", flush=True)
    print(f"MOCK_LLM_SERVER_SESSION={args.session_id}", flush=True)

    if args.real_api_key:
        os.environ["ANTHROPIC_API_KEY"] = args.real_api_key

    app = create_app(
        mode=args.mode,
        out_dir=args.out_dir,
        session_id=args.session_id,
        real_base_url=args.real_base_url,
        trace_file=args.trace_file,
        on_mismatch=on_mismatch,
    )

    try:
        # use_reloader=False to avoid double-spawn
        # threaded=True so concurrent /v1/messages requests can be served
        app.run(host=args.host, port=port, debug=False, use_reloader=False, threaded=True)
    finally:
        writer = app.config.get("_WRITER")
        if writer is not None:
            writer.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())