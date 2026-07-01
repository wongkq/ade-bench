"""TraceManager: host-side lifecycle for the mock LLM server.

Spawns `scripts/mock_llm_server.py` as a subprocess, waits for /health to
return 200, exposes:
  - base_url:        where to point ANTHROPIC_BASE_URL inside the container
  - session_id_for:  maps a trial_name to a stable per-trial session_id
  - trace_path_for:  maps a trial_name to its expected JSONL file
  - inject_env:      container-side env vars to expose
"""

from __future__ import annotations

import logging
import os
import re
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

import requests

from ade_bench.trace.exceptions import (
    TraceConfigError,
    TraceError,
    TraceMissingError,
    TraceStartupError,
)
from ade_bench.trace.models import TraceConfig, TraceMode

# Hostname the in-container Claude CLI uses to reach the host.
CONTAINER_TO_HOST_HOSTNAME = "host.docker.internal"


class TraceManager:
    """Spawn and supervise a host-side mock LLM server.

    Lifecycle:
        tm = TraceManager(TraceConfig(mode=RECORD, record_trace_dir=/tmp/traces))
        tm.start()
        # ... trials run, each calling tm.inject_env(trial_name) ...
        tm.stop()
    """

    def __init__(self, config: TraceConfig):
        config.validate_()
        self._config = config
        self._process: Optional[subprocess.Popen] = None
        self._port: Optional[int] = None
        self._stderr_lines: list[str] = []
        self._stderr_thread: Optional[threading.Thread] = None
        self._stopped = threading.Event()
        self._lock = threading.Lock()
        self._current_session_id: Optional[str] = None  # set on start/inject_env
        self._logger = logging.getLogger(__name__)

    # ---- public properties ----

    @property
    def mode(self) -> TraceMode:
        return self._config.mode

    @property
    def is_enabled(self) -> bool:
        return self._config.mode != TraceMode.OFF and self._process is not None

    @property
    def port(self) -> Optional[int]:
        return self._port

    @property
    def base_url(self) -> Optional[str]:
        """URL the container should use for ANTHROPIC_BASE_URL.

        Returns None when trace is disabled.
        """
        if not self.is_enabled or self._port is None:
            return None
        return f"http://{CONTAINER_TO_HOST_HOSTNAME}:{self._port}"

    # ---- session/file naming ----

    @staticmethod
    def session_id_for(trial_name: str) -> str:
        """Map a harness trial_name to a stable session_id used inside JSONL."""
        # trial_name is already filesystem-safe; no further munging needed.
        return trial_name

    def trace_path_for(self, trial_name: str) -> Optional[Path]:
        """Where the JSONL for this trial will be written (record mode) or
        was sourced from (replay mode). Returns None when disabled."""
        if self._config.mode == TraceMode.OFF:
            return None
        if self._config.mode == TraceMode.RECORD:
            return self._config.record_trace_dir / f"{self.session_id_for(trial_name)}.jsonl"
        # replay: trace was read from a single file; record mode would write to per-trial
        return self._config.replay_trace_file

    def inject_env(self, trial_name: str) -> dict[str, str]:
        """Compute the env-var dict to pass into the trial's container.

        Returns an empty dict when trace mode is OFF.

        Side effect: ensures the mock server is running with the right
        session_id for this trial. If the mock server was already started
        for a different session_id, it is restarted (record mode only —
        replay mode has one fixed trace file regardless of session).
        """
        if self._config.mode == TraceMode.OFF:
            return {}
        session_id = self.session_id_for(trial_name)
        self._ensure_session(session_id)
        env = {
            "ADE_TRACE_SESSION_ID": session_id,
            "ADE_TRACE_BASE_URL": self.base_url,
        }
        # Critical: this overrides ANTHROPIC_BASE_URL only inside the container.
        env["ANTHROPIC_BASE_URL"] = self.base_url
        return env

    def _ensure_session(self, session_id: str) -> None:
        """Start (or restart) the mock server so it uses the given session_id.

        In record mode the mock server writes to a single JSONL file whose
        name is bound to --session-id. To produce per-trial JSONL files
        (matching harness_results.trace_file naming), we restart the server
        whenever the trial's session_id differs from the current one.

        In replay mode the session_id is irrelevant (only one trace file is
        read), so we never restart for session mismatch.
        """
        with self._lock:
            if self._process is None:
                # First trial — start fresh.
                self._current_session_id = session_id
                self._start_locked()
                return
            if self._config.mode == TraceMode.REPLAY:
                return  # replay doesn't care about session_id
            if self._current_session_id == session_id:
                return  # same session — keep running
            # Record mode + different session → restart
            self._logger.debug(
                f"Trace: restarting mock server for session_id={session_id} "
                f"(was {self._current_session_id})"
            )
            self._terminate_locked()
            self._current_session_id = session_id
            self._start_locked()

    # ---- lifecycle ----

    def start(self) -> None:
        """Eagerly spawn the mock LLM server with a placeholder session_id.

        Most callers should NOT invoke start() directly — instead, let
        inject_env(trial_name) drive lazy startup with the correct session.
        start() is preserved for explicit lifecycle control (e.g. tests)
        and writes to ``<out_dir>/default.jsonl`` until inject_env restarts.
        """
        with self._lock:
            if self._process is not None:
                return  # already started
            if self._config.mode == TraceMode.OFF:
                return
            self._current_session_id = self._current_session_id or "default"
            self._start_locked()

    def _start_locked(self) -> None:
        """Spawn the mock server. Caller must hold self._lock."""
        if self._config.mode == TraceMode.OFF:
            return
        cmd = self._build_command()
        self._stderr_lines = []
        try:
            self._process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            raise TraceStartupError(
                f"Failed to spawn mock_llm_server: {exc}"
            ) from exc

        # Drain stdout (we print MOCK_LLM_SERVER_PORT=... lines for our own use)
        self._stdout_thread = threading.Thread(
            target=self._drain_stdout, daemon=True, name="trace-stdout"
        )
        self._stdout_thread.start()

        self._stderr_thread = threading.Thread(
            target=self._drain_stderr, daemon=True, name="trace-stderr"
        )
        self._stderr_thread.start()

        self._wait_for_start()

    def _terminate_locked(self) -> None:
        """Kill the mock server subprocess. Caller must hold self._lock."""
        if self._process is None:
            return
        try:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
                try:
                    self._process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
        finally:
            self._process = None

    def _build_command(self) -> list[str]:
        repo_root = Path(__file__).resolve().parents[2]  # ade-bench2/
        script_path = repo_root / "scripts" / "mock_llm_server.py"
        if not script_path.exists():
            raise TraceStartupError(f"mock_llm_server.py not found at {script_path}")

        cmd = [
            sys.executable,
            str(script_path),
            "--mode", self._config.mode.value,
            "--host", "0.0.0.0",
            "--session-id", self._current_session_id or "default",
        ]
        if self._config.port is not None:
            cmd += ["--port", str(self._config.port)]
        else:
            cmd += ["--port", "0"]
        if self._config.mode == TraceMode.RECORD:
            if self._config.record_trace_dir is None:
                raise TraceConfigError("record_trace_dir must be set in RECORD mode")
            self._config.record_trace_dir.mkdir(parents=True, exist_ok=True)
            self._check_writable(self._config.record_trace_dir)
            cmd += ["--out-dir", str(self._config.record_trace_dir)]
        elif self._config.mode == TraceMode.REPLAY:
            if self._config.replay_trace_file is None:
                raise TraceConfigError("replay_trace_file must be set in REPLAY mode")
            if not self._config.replay_trace_file.exists():
                raise TraceMissingError(
                    f"Replay trace file not found: {self._config.replay_trace_file}"
                )
            cmd += ["--trace-file", str(self._config.replay_trace_file)]
        cmd += ["--on-mismatch", self._config.on_mismatch]
        return cmd

    def _check_writable(self, path: Path) -> None:
        """Verify we can create a tiny file in the directory."""
        probe = path / ".ade-bench-write-probe"
        try:
            probe.write_text("ok")
            probe.unlink()
        except OSError as exc:
            raise TraceStartupError(f"trace_dir not writable: {path}: {exc}") from exc

    def _drain_stdout(self) -> None:
        """Read child stdout looking for the MOCK_LLM_SERVER_PORT line."""
        assert self._process and self._process.stdout
        for line in self._process.stdout:
            line = line.rstrip("\n")
            if line.startswith("MOCK_LLM_SERVER_PORT=") and self._port is None:
                try:
                    self._port = int(line.split("=", 1)[1])
                except ValueError:
                    pass
            # Always echo to our stderr for operator visibility
            sys.stderr.write(f"[trace mock] {line}\n")
            sys.stderr.flush()

    def _drain_stderr(self) -> None:
        assert self._process and self._process.stderr
        for line in self._process.stderr:
            self._stderr_lines.append(line.rstrip("\n"))
            sys.stderr.write(f"[trace mock] {line}")
            sys.stderr.flush()

    def _wait_for_start(self) -> None:
        """Poll /health on the chosen port until 200 or timeout."""
        deadline = time.time() + self._config.health_timeout_sec
        # First wait for port announcement
        while self._port is None and time.time() < deadline:
            if self._process and self._process.poll() is not None:
                self._raise_startup_error("mock_llm_server exited before announcing port")
            time.sleep(0.05)

        if self._port is None:
            self._terminate()
            self._raise_startup_error(
                f"mock_llm_server did not announce port within {self._config.health_timeout_sec}s"
            )

        # Then wait for /health
        while time.time() < deadline:
            try:
                r = requests.get(f"http://127.0.0.1:{self._port}/health", timeout=1.0)
                if r.status_code == 200:
                    return
            except requests.RequestException:
                pass
            if self._process and self._process.poll() is not None:
                self._raise_startup_error("mock_llm_server exited during startup")
            time.sleep(0.2)

        self._terminate()
        self._raise_startup_error(
            f"mock_llm_server did not pass /health check within {self._config.health_timeout_sec}s"
        )

    def _raise_startup_error(self, message: str) -> None:
        stderr_tail = "\n".join(self._stderr_lines[-20:])
        raise TraceStartupError(f"{message}\nstderr tail:\n{stderr_tail}")

    def stop(self) -> None:
        """Terminate the mock server (idempotent)."""
        with self._lock:
            if self._process is None:
                return
            if self._stopped.is_set():
                return
            self._stopped.set()
            try:
                self._terminate_locked()
            except Exception as exc:
                raise TraceError(f"Failed to stop mock_llm_server: {exc}") from exc

    def __enter__(self) -> "TraceManager":
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.stop()