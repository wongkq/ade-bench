# LLM Session Record + Replay

ADE-bench can record every Anthropic API call an agent makes during a trial
and replay the same exact bytes later. This makes benchmark runs **fully
deterministic** for the parts of agent behavior that depend on the model:
identical tool selections, identical SSE streams, identical token usage.

## When to use it

| Scenario | Use |
|---|---|
| Stable baseline for A/B comparisons | `--record-trace` once, `--replay-trace` many |
| Regression test for harness changes | `--replay-trace` against checked-in JSONL |
| CI gating with deterministic pass/fail | `--replay-trace` |
| Producing new recordings | `--record-trace` |
| Production benchmark run | (no flag — live Anthropic API) |

## Quick start

### 1. Record a run

```bash
mkdir -p ./traces
ab run airbnb001 \
  --db duckdb --project-type dbt \
  --agent claude --model claude-opus-4-5-20251101 \
  --record-trace ./traces
```

This produces one JSONL per trial under `./traces/`, e.g.:

```
./traces/airbnb001.base.1-of-1.jsonl
./traces/airbnb001.hard.1-of-1.jsonl
```

### 2. Replay it

```bash
ab run airbnb001 \
  --db duckdb --project-type dbt \
  --agent claude \
  --replay-trace ./traces/airbnb001.base.1-of-1.jsonl
```

The mock LLM server reads the JSONL and short-circuits every Anthropic call.
No network egress; identical agent behavior; deterministic `results.tsv`.

## Flags

| Flag | Mode | Description |
|---|---|---|
| `--record-trace DIR` | record | Write `<session_id>.jsonl` per trial to `DIR` |
| `--replay-trace FILE` | replay | Read JSONL from `FILE` and answer Anthropic calls |
| `--trace-on-mismatch POLICY` | replay | What to do when a request hash has no recording |

The two `--record-trace` and `--replay-trace` flags are **mutually exclusive**.
Supplying both produces a hard error at startup.

### `--trace-on-mismatch` policies

| Value | Behavior |
|---|---|
| `error` (default) | Return Anthropic-style 500 error. Replay cannot proceed. |
| `fallback_seq` | Serve the next unused assistant event in JSONL order. |
| `fallback_hash` | First try legacy (non-normalized) hash, then sequential. |

Use `fallback_seq` when you've added a new `--plugin-set` that changes the
system prompt — request hashes will diverge, but you still want the recording
to play forward.

## JSONL format

Each line is one event. Schema version: `ade-bench-trace/v1`.

### Common fields (every event)

```json
{
  "event_id": "uuid4",
  "session_id": "airbnb001.base.1-of-1",
  "schema_version": "ade-bench-trace/v1",
  "timestamp": "2026-06-15T10:23:45.123Z",
  "type": "user | assistant | system"
}
```

### `assistant` event

```json
{
  "...": "...",
  "type": "assistant",
  "message": {
    "role": "assistant",
    "content": [
      {"type": "text", "text": "..."},
      {"type": "tool_use", "id": "toolu_01ABC", "name": "Bash",
       "input": {"command": "ls -la"}}
    ],
    "stop_reason": "end_turn | tool_use | max_tokens"
  },
  "model": "claude-opus-4-5-20251101",
  "stop_reason": "end_turn",
  "usage": {
    "input_tokens": 1234,
    "output_tokens": 567,
    "cache_creation_input_tokens": 0,
    "cache_read_input_tokens": 8901
  },
  "request_hash": "sha256:abc...",
  "request_hash_legacy": "sha256:def...",
  "elapsed_ms": 4321
}
```

### `user` event

```json
{
  "...": "...",
  "type": "user",
  "message": {
    "role": "user",
    "content": [
      {"type": "text", "text": "The system prompt..."},
      {"type": "tool_result", "tool_use_id": "toolu_01ABC",
       "content": "stdout...", "is_error": false}
    ]
  }
}
```

### `system` event (always the first line of the file)

```json
{
  "...": "...",
  "type": "system",
  "message": {
    "role": "system",
    "content": [{"type": "text", "text": "ADE-bench trace session initialized."}]
  },
  "mode": "record",
  "real_base_url": "https://api.anthropic.com"
}
```

## How it works

```
Host machine                              Container
────────────────────────────────────────────────────────────────
┌─────────────────────────────┐
│ scripts/mock_llm_server.py  │
│  (Flask, port = auto-pick)  │             ┌──────────────────┐
│  bound to 0.0.0.0           │             │ Claude Code CLI  │
│                             │  POST       │                  │
│  --mode=record              │ /v1/messages│ ANTHROPIC_BASE_  │
│   ├─ forward to real API    │◀───────────▶│ URL=http://      │
│   │  (forces stream=true)   │             │ host.docker.     │
│   │  (Accept-Encoding:      │             │ internal:<port>  │
│   │   identity)             │             │                  │
│   └─ tee SSE → JSONL        │             │ ADE_TRACE_       │
│                             │             │ SESSION_ID=      │
│  --mode=replay              │             │ <trial_name>     │
│   └─ serve from JSONL       │             │                  │
└─────────────────────────────┘             └──────────────────┘
```

The mock server binds to `0.0.0.0` (not `127.0.0.1`) because the container
reaches the host through `host.docker.internal:host-gateway`, which resolves
to the host's external IP — connections to `127.0.0.1` from inside the
container get `ConnectionRefused`. The port is auto-picked by the OS
(`--port 0` → ephemeral) and announced on stdout as `MOCK_LLM_SERVER_PORT=…`,
then the harness passes it to the container via
`http://host.docker.internal:<port>`.

The docker-compose templates already expose `ANTHROPIC_BASE_URL` and
`extra_hosts: host.docker.internal:host-gateway` (see
`shared/defaults/docker-compose-*.yaml`). The harness overrides
`ANTHROPIC_BASE_URL` to the mock server's port and adds two new env vars:
`ADE_TRACE_BASE_URL` and `ADE_TRACE_SESSION_ID`.

### Why the mock forces `stream=true`

Some Anthropic-compatible providers (e.g. minimax at
`api.minimaxi.com/anthropic`) ignore the client's `stream` flag and return
a single non-streaming JSON envelope. Claude Code *always* expects SSE, so
it would fail with `Failed to parse JSON`. To make the mock robust across
providers, `_handle_record` rewrites the upstream request body to set
`"stream": true` before forwarding, regardless of what the client asked for.

### Why the mock sets `Accept-Encoding: identity`

`requests.post(..., stream=True)` defaults to `Accept-Encoding: gzip`,
which upstream honors by returning a zstd/gzip-compressed binary blob. The
mock forwards chunks byte-for-byte to the client, so Claude would see the
binary stream and fail with `API returned an empty or malformed response
(HTTP 200)`. The mock explicitly sets `Accept-Encoding: identity` to force
plaintext SSE.

## Trial naming

JSONL files are named after the harness `trial_name`:

```
<record_trace_dir>/<task_id>.<variant_key>.<attempt>-of-<n_attempts>.jsonl
```

Example: `airbnb001.base.1-of-1.jsonl`.

The `ADE_TRACE_SESSION_ID` injected into the container matches the same name
so the mock server can route per-request events to the right file.

## Hash matching (replay determinism)

Replay determinism rests on a **normalized SHA-256** of the request body.
The raw body is normalized via `_strip_runtime_noise` and a couple of
pre-processing helpers to strip fields that vary between record and replay
even when the agent's intent is identical:

| Field | Treatment | Why |
|---|---|---|
| `cache_control` | dropped | Claude SDK adds per-request |
| `cache_creation_input_tokens` / `cache_read_input_tokens` | dropped | sentinels, not conversation state |
| `tool_result.content` | replaced with literal `"<stripped-tool-result-content>"` | bash output varies: timestamps, file paths, duckdb file sizes |
| `tool_use_id` / `tool_call_id` | replaced with literal `"<normalized-tool-id>"` | Claude CLI generates fresh UUIDs per session, so the recorded `call_…` IDs would never match a fresh session's IDs |
| `system` blocks with `cc_version=…` | dropped | runtime version banner |
| `system` blocks that are `<system-reminder>` / `</system-reminder>` | dropped | per-turn dynamic reminders |
| `text` blocks inside message content that start with `<system-reminder>` / `</system-reminder>` | replaced with `"<stripped-system-reminder-text>"` | Claude SDK re-injects CLAUDE.md as a per-turn user text reminder; the embedded content can drift across sessions (dates, paths, runtime banners) and would otherwise cause every turn 2+ to fail to match |
| `role: "system"` messages inside `messages[]` | lifted into the top-level `system` array | some Claude SDK builds inject a `role=system` message inside the messages array (e.g. the "Available agent types" reminder). Treat them like part of the system prompt during normalization. |
| `thinking` content blocks (in `content[]` arrays) | dropped entirely | the mock server's SSE generator skips thinking blocks when replaying, so the agent's reconstructed conversation on replay never sees them. Including them in the hash — even with content collapsed to a placeholder — would still cause a block-count mismatch. Removing them aligns the conversation shape on both sides. |
| `metadata` | dropped | contains a per-session `user_id` with `device_id` / `account_uuid` that Claude SDK generates fresh on every CLI invocation |

The hash input is `(model, system, tools, messages, max_tokens, temperature)`
after normalization. This means the 4th replay turn and beyond will still
match, even though the bash output the agent saw on the original recording
is different from what bash produces today.

A legacy hash (no normalization, full raw body) is also recorded as
`request_hash_legacy` and consulted by `--trace-on-mismatch fallback_hash`.

### Why dropping `thinking` blocks (not just collapsing)

`thinking` is an *extended-thinking* feature that wasn't a part of the
Anthropic public API when this code was first written. The SSE generator
in `assistant_message_to_sse` (around L289) does:

```python
elif btype == "thinking":
    # Skip - thinking blocks are not part of Anthropic public API yet
    continue
```

So when the agent calls Claude API on replay turn 2, the recorded assistant
message had `[thinking, tool_use]` as content blocks, but the replay SSE
stream only emits `tool_use`. The agent's reconstructed conversation history
for turn 3 will therefore have `[tool_use]` (no thinking) in the prior
turn, and a recorded turn-3 request with `[thinking, tool_use]` will not
match. Collapsing the block's `text` and `signature` fields to placeholders
doesn't fix this — the block *count* still differs. Removing the entire
block from both record and replay's hash input makes them align.

## Converting a JSONL back to a readable transcript

```bash
python -c "
import json, sys
with open('traces/airbnb001.base.1-of-1.jsonl') as f:
    for i, line in enumerate(f, 1):
        e = json.loads(line)
        t = e['type']
        role = e['message']['role']
        snippet = e['message']['content']
        if isinstance(snippet, list):
            snippet = ' | '.join(
                f\"{b.get('type','?')}:{(b.get('text') or b.get('content') or b.get('name') or '')!r}\"
                for b in snippet
            )
        print(f'[{i:03d}] {t} ({role}): {snippet[:120]}')"
```

## Validation commands

```bash
# Count events
python -c "
import json
with open('traces/airbnb001.base.1-of-1.jsonl') as f:
    events = [json.loads(l) for l in f if l.strip()]
print(f'total: {len(events)}')
print(f'assistant: {sum(1 for e in events if e[\"type\"]==\"assistant\")}')
print(f'user:      {sum(1 for e in events if e[\"type\"]==\"user\")}')
print(f'tool_uses: {sum(sum(1 for c in e[\"message\"][\"content\"] if c.get(\"type\")==\"tool_use\") for e in events if e[\"type\"]==\"assistant\")}')
"

# Confirm the system init event is on line 1
head -1 traces/airbnb001.base.1-of-1.jsonl | python3 -c "import sys, json; print(json.loads(sys.stdin.read())['type'])"
# expect: system
```

### Stability check

To confirm a recording truly reproduces deterministically, replay it multiple
times in a row and compare the `(result, tests, turns)` columns of each
`results.tsv`:

```bash
# Record once
ab run airbnb001 --db duckdb --project-type dbt \
  --agent claude \
  --record-trace ./traces \
  --run-id record

# Replay N times under different run-ids
for i in 1 2 3 4 5; do
  ab run airbnb001 --db duckdb --project-type dbt \
    --agent claude \
    --replay-trace ./traces/airbnb001.base.1-of-1.jsonl \
    --run-id "replay-$i" >/dev/null
done

# All N result+tests+turns triples should be byte-identical.
awk -F'\t' 'NR>1 {print $2, $6, $7, $14}' \
  experiments/replay-1__none/results.tsv \
  experiments/replay-2__none/results.tsv \
  experiments/replay-3__none/results.tsv \
  experiments/replay-4__none/results.tsv \
  experiments/replay-5__none/results.tsv \
  | md5sum
```

If the line `airbnb001 pass 11 11 14` repeats five times with the same MD5,
replay is deterministic for that recording. A typical full-run validation on
`airbnb001` records ~38s of wall time and replays in ~0.3s — orders of
magnitude faster than re-running the agent live.

## Limitations and known caveats

1. **Concurrency**: in record mode, the mock server writes to one file
   per process. Concurrent trials (`--n-concurrent-trials > 1`) will all be
   recorded under the trial name the mock server was started with.
   **Recommendation**: use `--n-concurrent-trials 1` for record/replay runs.
   Replay mode is naturally single-trial.

2. **bash-vcr**: only the LLM traffic is recorded. Bash command results
   inside the container come from the live shell. If your task depends on
   time-of-day or nondeterministic external state, bash results will still
   vary between record and replay.

3. **Recording size**: a typical 5-turn trial records ~10–50 KB of JSONL.
   Long agent loops can produce MB-scale traces.

4. **Schema evolution**: the `schema_version` field lets us evolve the
   format. Older traces replay fine as long as a reader understands the
   older version.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `mock_llm_server failed to start` | port in use / firewall | Mock uses `--port 0` (auto-pick). On conflict, no easy fix; check `lsof -i` and re-run |
| `replay trace file not found` | wrong path | Verify the file exists; the harness checks at startup |
| `request_hash mismatch` on turn 4+ | hash normalization didn't account for some runtime field | Update `_strip_runtime_noise` to handle the new field, then re-record |
| `request_hash mismatch` on turn 1 | new plugin-set changed the system prompt | Re-record, or pass `--trace-on-mismatch fallback_seq` |
| `no matching recording and recordings exhausted` | recording shorter than agent run | Increase `--max-episodes`, or set `--trace-on-mismatch fallback_seq` |
| `Failed to parse JSON` from Claude (record mode) | upstream returned non-SSE JSON or compressed binary | Both cases are handled by the mock's forced `stream: true` and `Accept-Encoding: identity`; if you see this, check mock stderr for the actual upstream response shape |
| `API returned an empty or malformed response (HTTP 200)` (record mode) | upstream returned compressed binary | Mock sets `Accept-Encoding: identity`; if you see this, your provider may be doing something exotic (e.g. zstd) — see `scripts/mock_llm_server.py` `_handle_record` |
| `ConnectionRefused` from container to mock (record mode) | mock bound to 127.0.0.1 (or port not exposed) | Mock binds `0.0.0.0` and auto-picks port; verify `host.docker.internal:host-gateway` is in `extra_hosts` of the compose file |
| Trial results show `is_resolved=false` but agent succeeded | recorded output differs from live | Compare JSONL to a fresh recording; check `elapsed_ms` for timeouts |

## Implementation pointers

- `scripts/mock_llm_server.py` — Flask app, command-line entrypoint
- `ade_bench/trace/manager.py` — host-side subprocess lifecycle
- `ade_bench/trace/models.py` — `TraceConfig`, `TraceMode`, `TraceEvent`
- `ade_bench/trace/exceptions.py` — typed errors
- `ade_bench/harness.py` — start/stop, env injection
- `ade_bench/cli/ab/main.py` — `--record-trace` / `--replay-trace` flags
- `shared/defaults/docker-compose-*.yaml` — env propagation
- `tests/trace/test_manager.py`, `tests/trace/test_mock_server.py` — unit tests