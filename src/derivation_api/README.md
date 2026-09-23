# Derivation HTTP/SSE control plane

This package is the HTTP/SSE boundary for a `DerivationRun`: single-user on
loopback by default, with an authenticated multi-user server mode.  It
contains no model provider and does not mutate Record V1 inside HTTP handlers.  A concrete runtime implements `DerivationService` and is
injected into `create_app(...)`.

The bundled `FakeDerivationService` is deterministic and hermetic.  It exists
only for transport tests and local UI development; production startup must
inject the real orchestrator service.

## Reproducible setup and verification

Run from this directory:

```bash
uv sync --frozen
uv run pytest
uv run ruff check .
uv run python scripts/export_openapi.py
```

`uv.lock` and `.python-version` pin resolution and Python 3.14.  Once `uv sync`
has populated `.venv`, the test commands need no network.  `.venv` and caches
are ignored.

For a local fake server used by the frontend:

```bash
uv run uvicorn derivation_api.testing:app --host 127.0.0.1 --port 8000
```

Never bind this v1 server to a public interface.  Browser origins are limited
to HTTP(S) loopback hosts; requests without an `Origin` header remain available
to local CLI clients.

## Command contract

All scientific and runtime choices are explicit.  Runtime metadata is separate
because `auth_mode`, concurrency, retries, and wall-clock caps are not fields in
Record V1's frozen `configuration` object.

The implemented v1 runtime profile is intentionally narrow:

- `auth_mode` must be `chatgpt`; API-key execution is not implemented.
- `concurrency` must be `1`; the scheduler is serial.
- `max_run_seconds` must be explicit `null`.  The concrete service does not
  yet enforce a wall-clock deadline, so the API rejects numeric values instead
  of accepting and silently ignoring them.

```json
{
  "question": "Derive the low-energy limit under the stated assumptions.",
  "config": {
    "granularity": "one_claim",
    "writer": {"provider": "openai", "model": "gpt-example", "effort": "high"},
    "checker": {"provider": "openai", "model": "gpt-example", "effort": "medium"},
    "judge": {"provider": "openai", "model": "gpt-example", "effort": "high"},
    "backend": {"name": "codex-app-server", "version": "0.147.0"},
    "max_model_calls": 12,
    "max_active_branches": 2,
    "reference_allowed": false,
    "allowed_paths": []
  },
  "runtime": {
    "auth_mode": "chatgpt",
    "concurrency": 1,
    "retries": 0,
    "max_run_seconds": null
  }
}
```

Submit and inspect:

```bash
curl --fail-with-body \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: submit-demo-1' \
  --data @run-request.json \
  http://127.0.0.1:8000/api/runs

curl --fail-with-body http://127.0.0.1:8000/api/runs/run-0001
```

Soft pause, resume, and hard interrupt are deliberately separate commands:

```bash
curl --fail-with-body -X POST \
  -H 'Idempotency-Key: pause-demo-1' \
  http://127.0.0.1:8000/api/runs/run-0001/pause

curl --fail-with-body -X POST \
  -H 'Idempotency-Key: resume-demo-1' \
  http://127.0.0.1:8000/api/runs/run-0001/resume

curl --fail-with-body -X POST \
  -H 'Idempotency-Key: interrupt-demo-1' \
  http://127.0.0.1:8000/api/runs/run-0001/interrupt
```

Command eligibility is exact and is enforced by the injected service, not by
the HTTP handler:

| Command | Eligible state | Ineligible result |
| --- | --- | --- |
| `pause` | Active execution in `submitted`, `autonomous_exploration`, or `human_expansion` | 409 `run_not_pausable` |
| `resume` | `paused` | 409 `run_not_resumable` |
| `interrupt` | At least one authoritative current in-flight call | 409 `run_not_interruptible` |

Phase alone cannot prove interrupt eligibility.  The UI may use a current SSE
`overlay.activeCalls` value as a hint, but the service makes the final decision
because GET deliberately contains only canonical scientific state.  Pause is
resumable and must map to the core resume operation; it is not an alias for a
hard interrupt.  Repeating a command without its original `Idempotency-Key`
is a new command and is checked against the current eligibility state.

A human branch is accepted only in `review_ready` or
`review_ready_due_to_cap`, and only from a sealed StepRevision:

```json
{
  "from_step_revision_id": "revision-run-0001-001",
  "kind": "human_direction",
  "instruction": "Explore the boundary case without changing the old route."
}
```

`human_direction` inherits through the anchor.  `human_revision` has replace
semantics and inherits only the prefix before the anchor.  For
`human_revision`, `instruction` is not a model prompt: it must be a JSON-encoded
object containing exactly `claim`, `why`, `source`, `derivation`, and `scope`.
The service canonicalizes those five fields, records the `revise_step`
HumanAction, and seals them with human provenance.  It must never ask a model to
write a replacement and then label that output as human.  The runtime service,
not the HTTP route, records the matching HumanAction before the branch change.

## SSE replay and reconnect

```bash
curl -N 'http://127.0.0.1:8000/api/runs/run-0001/events'
curl -N -H 'Last-Event-ID: 4' \
  'http://127.0.0.1:8000/api/runs/run-0001/events'
```

Each message contains numeric `id:` and one JSON `data:` object.  Event type is
inside JSON so `EventSource.onmessage` receives it.  `Last-Event-ID` is
exclusive and takes precedence over the optional `after` query.  Heartbeats are
SSE comments.  `?follow=false` replays the available suffix and closes, which is
useful for tests and diagnostics.  Subscriber queues are bounded; a slow client
is closed and reconnects from its last processed id rather than accumulating
unbounded memory.

All event IDs and cursors are bounded to `9_007_199_254_740_991`
(`Number.MAX_SAFE_INTEGER`).  This keeps `canonical_event_id`, SSE `event_id`,
`after`, and `Last-Event-ID` exactly representable by browser JavaScript
`number` values.

## Error and idempotency response contract

For every documented GET/POST operation, applicable 400, 403, 404, 409, 413,
422, 500, and 503 responses use one JSON `ErrorEnvelope`:

```json
{
  "error": {
    "code": "run_not_resumable",
    "message": "Resume is only valid for a soft-paused run.",
    "details": {"phase": "review_ready"}
  },
  "request_id": "request-123"
}
```

The checked OpenAPI contract does not expose FastAPI's default
`HTTPValidationError`.  Successful command responses document and emit the
`Idempotency-Key` response header when the request supplied a valid key.
Browser CORS preflight is middleware behavior rather than a documented command
operation and is not claimed to use this envelope.

## Canonical truth boundary

`GET /api/runs/{id}` must be built from the last complete, strict-replayable
Record V1 head.  It must not attempt a strict replay that ends at
`model_call_started`, and it must not promote streaming text into a sealed step.
The `phase` values `review_ready` and `review_ready_due_to_cap` are backend
control phases, not new Record V1 events.  SSE may carry an `overlay` describing
in-flight state while its nested `run` remains the last complete canonical
snapshot.  The full control phase set is `submitted`,
`autonomous_exploration`, `human_expansion`, `paused`, `recovering`,
`review_ready`, `review_ready_due_to_cap`, `interrupted`, and `error`.

The checked machine-readable contract is `openapi.json`.
