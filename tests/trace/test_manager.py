"""Unit tests for ade_bench.trace.manager and ade_bench.trace.models.

These tests do NOT spin up the mock LLM server (the subprocess boundary is
exercised separately in test_mock_server.py). They verify the deterministic
behavior of the manager: env dict shape, session_id mapping, trace_path_for
mapping, mutual-exclusion validation, and OFF-mode no-op semantics.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ade_bench.trace.exceptions import TraceConfigError, TraceMissingError
from ade_bench.trace.manager import TraceManager
from ade_bench.trace.models import TraceConfig, TraceMode


# ============================================================
# TraceConfig validation
# ============================================================


def test_record_mode_requires_dir():
    cfg = TraceConfig(mode=TraceMode.RECORD)
    with pytest.raises(TraceConfigError):
        cfg.validate_()


def test_replay_mode_requires_file():
    cfg = TraceConfig(mode=TraceMode.REPLAY)
    with pytest.raises(TraceConfigError):
        cfg.validate_()


def test_replay_mode_requires_existing_file(tmp_path: Path):
    cfg = TraceConfig(
        mode=TraceMode.REPLAY,
        replay_trace_file=tmp_path / "does-not-exist.jsonl",
    )
    # The lightweight validate_() only checks field presence (no I/O).
    cfg.validate_()
    # The file-existence check is performed by TraceManager.start() while
    # building the subprocess command — so calling start() on a non-existent
    # file should raise TraceMissingError before any subprocess is spawned.
    tm = TraceManager(cfg)
    with pytest.raises(TraceMissingError):
        tm.start()


def test_off_mode_passes_validation():
    cfg = TraceConfig(mode=TraceMode.OFF)
    cfg.validate_()  # no exception


# ============================================================
# Session-id & trace-path mapping
# ============================================================


def test_session_id_is_trial_name():
    assert TraceManager.session_id_for("airbnb001.base.1-of-1") == "airbnb001.base.1-of-1"


def test_trace_path_off_mode_is_none(tmp_path: Path):
    cfg = TraceConfig(mode=TraceMode.OFF)
    tm = TraceManager(cfg)
    assert tm.trace_path_for("anything") is None


def test_trace_path_record_mode_uses_session_id(tmp_path: Path):
    cfg = TraceConfig(mode=TraceMode.RECORD, record_trace_dir=tmp_path)
    tm = TraceManager(cfg)
    expected = tmp_path / "airbnb001.base.1-of-1.jsonl"
    assert tm.trace_path_for("airbnb001.base.1-of-1") == expected


def test_trace_path_replay_mode_returns_input_file(tmp_path: Path):
    trace_file = tmp_path / "trial.jsonl"
    trace_file.write_text("{}\n")
    cfg = TraceConfig(mode=TraceMode.REPLAY, replay_trace_file=trace_file)
    tm = TraceManager(cfg)
    assert tm.trace_path_for("whatever") == trace_file


# ============================================================
# env injection
# ============================================================


def test_inject_env_when_disabled_returns_empty():
    cfg = TraceConfig(mode=TraceMode.OFF)
    tm = TraceManager(cfg)
    assert tm.inject_env("anything") == {}


def test_inject_env_when_not_started_lazily_starts_mock(tmp_path: Path):
    """inject_env() before explicit start() must lazily spawn the mock server.

    This is the key contract that makes per-trial session_id work: the harness
    only knows the trial_name at the moment of env injection, so the mock
    server has to be (re)started on the fly with that session_id. We assert
    that after one inject_env call the manager reports is_enabled=True and
    the mock server process is alive.
    """
    cfg = TraceConfig(mode=TraceMode.RECORD, record_trace_dir=tmp_path)
    tm = TraceManager(cfg)
    try:
        env = tm.inject_env("trial.x.1-of-1")
        # env must carry both the trace-specific markers and ANTHROPIC_BASE_URL
        assert env["ADE_TRACE_SESSION_ID"] == "trial.x.1-of-1"
        assert env["ADE_TRACE_BASE_URL"].startswith("http://host.docker.internal:")
        assert env["ANTHROPIC_BASE_URL"].startswith("http://host.docker.internal:")
        # mock server must now be alive
        assert tm.is_enabled is True
        assert tm._process is not None
        assert tm._process.poll() is None  # still running
        assert tm.port is not None
    finally:
        tm.stop()


def test_base_url_is_none_when_disabled():
    cfg = TraceConfig(mode=TraceMode.OFF)
    tm = TraceManager(cfg)
    assert tm.base_url is None


def test_base_url_uses_host_docker_internal_after_start(tmp_path: Path):
    cfg = TraceConfig(mode=TraceMode.RECORD, record_trace_dir=tmp_path, port=18765)
    tm = TraceManager(cfg)
    # Don't actually start (would spawn subprocess) — patch _process instead
    # to exercise the property.
    import subprocess as _sp
    from unittest.mock import MagicMock
    tm._process = MagicMock(spec=_sp.Popen)
    tm._port = 18765
    assert tm.base_url == "http://host.docker.internal:18765"