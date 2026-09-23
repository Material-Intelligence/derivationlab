# Derivation application integration

This package connects the provider-neutral derivation engine to a local
FastAPI/SSE control plane. Record V1 remains the scientific source of truth;
in-flight UI state stays in `RuntimeOverlay`.

## Runtime layers

```text
DerivationEngine
    -> ModelRuntime
        -> CodexAppServerRuntime
            -> official Codex App Server
                -> configured Responses-compatible provider
```

The engine owns scientific nodes, checks, branches, pause/resume, and Record
events. The App Server adapter owns thread/turn calls and protocol-event
translation. A future provider adapter can implement `ModelRuntime` without
changing the engine, but v1 ships only the App Server adapter.

## Capability profiles

Every run resolves a versioned `CapabilityProfile`. The profile is stored in
the run manifest with its SHA-256 but stays outside frozen Record V1. It defines
the experiment's permissions, tools, network, project instructions, Skills,
MCP servers, apps, and scientific runtime.

`benchmark_symbolic_v1` is the only v1 product profile:

- isolated writable run workspace;
- no project instructions or Skills;
- no web, MCP, apps, or command network;
- the reviewed `sympy_uv` scientific runtime.

The adapter translates that profile into App Server thread configuration. The
engine sees only its stable identity and hash.

## Product profile and subscription login

`ProductProfile` owns a private `HOME`, `CODEX_HOME`, runtime directory, and
per-run workspaces outside repository evidence. The reusable ChatGPT
subscription credential is `CODEX_HOME/auth.json`; it is never copied into a
Run, Record, manifest, log, or child-tool environment. On POSIX systems the
file must be owned by the current user with mode `0600`.

V1 deliberately uses the file credential store on macOS, Linux, and Windows.
The earlier macOS-only Keychain IPC and executable-attestation path was removed:
it made ordinary startup platform-specific without improving the scientific
state machine. OS keyring integration can return later behind a credential
store interface if it proves useful.

Normal startup checks only what the product needs:

- isolated profile/workspace paths and exact config;
- the launched command and live child are consistent;
- the initialized App Server version/protocol match the run;
- `account/read` identifies a ChatGPT subscription;
- the run's capability profile is applied.

Sandbox escapes, tool behavior, network denial, provider resume/fork semantics,
and installation signatures belong to conformance tests run during development,
after meaningful Codex/provider changes, and before formal benchmarks. They are
not re-attested on every turn.

Provision the pinned scientific runtime once during product setup:

```bash
PYTHONPATH=src:src/derivation_api \
python -m derivation_runtime.scientific_runtime \
  --root "/absolute/path/to/product-profile/runtime"
```

Normal runs validate and reuse that environment; they do not install packages.
When the npm Codex package is used with a controlled PATH, launch its packaged
native binary rather than the JavaScript wrapper, which depends on `node` being
discoverable through the host PATH.

Provider warning, plugin, Skill-discovery, and tool/process lifecycle events are
diagnostics. They do not fail a scientific run merely by existing. The adapter
still validates the final structured model output and terminal thread/turn
lineage strictly.

## Run storage and recovery

Each run uses two distinct locations:

- `runs/<run_id>/` stores Record V1, manifest, and control bookmarks;
- `<product-profile>/workspaces/<run_id>/` is App Server cwd and writable space.

`control.sqlite` stores only operational bookmarks needed for resume/fork. It
does not store scientific prose or provider transcripts. Restart reconciliation
accepts a completed provider snapshot and maps it back to the existing model
call; Record strict replay remains the final scientific integrity check.

The backend keeps one portable profile lock for the App Server process lifetime.
V1 intentionally does not implement distributed leases or multiple backends
writing the same session.

## Typeset layer of a finished route

A run under `formula-v2` builds one typeset layer per completed route, before
the service presents the run as review-ready, because a consumer that renders
the candidate the moment it sees that phase has to find the layer already
there. The layer lives beside the Record and is not part of it:

- `runs/<run_id>/typeset/<route_id>.json` is the layer, bound to the route's
  step revision ids and their Record `output_sha256`, with one entry per
  recorded math fragment (`ok`, `repaired`, `repaired_reviewed`,
  `quotation_expanded`, `quotation_verbatim`, `failed`, `not_compiled`), the
  host guard result, any review verdict, the compiler errors and the audit of
  every repair or review call;
- `runs/<run_id>/typeset/<route_id>.evidence/attempt-NN/round-RR/` holds the
  compile evidence of each whole-route compile;
- `runs/<run_id>/typeset/model_calls.jsonl` is the append-only journal of the
  typeset calls this run has already spent.

Recorded content never changes: a repair or a quotation expansion produces a
typeset copy, and a consumer uses it only while the layer's hashes still match
the Record. Nothing in this layer can pause a run - a compiler that is
unavailable, a provider that refuses, an exhausted budget and a formula that
still fails are all flags inside the layer.

Consumers of the layer are the ReportBundle exporter and the run views the
HTTP API serves: `GET /api/runs/{run_id}`, the
command responses, the SSE payload and the administrator's read of an account's
run all show the verified typeset math instead of a formula the compiler
rejected. That substitution is declared, never silent - the steps it touched
carry `StepView.typeset`, and `RunView.typeset_layers` names each layer with its
own content hash - and it changes only the math fragments: ids, prose and every
Record hash, `output_sha256` included, stay what the Record sealed. A run whose
layer is absent or no longer matches its Record is served byte for byte as
before. `RuntimeDerivationService.sealed_run` is the accessor for evidence that
must be the recorded text whatever a display layer would render.

Repair and review turns run on fresh threads with the run's Writer and Checker
model, effort and service tier, and without tools. They are counted against
`max_model_calls` together with the Record's own calls: a repair round starts
only while `Record model calls + journalled typeset calls < max_model_calls`.
The orchestrator's own capacity check stays Record-only, because the Record
status (`review_ready_due_to_cap`) is replayed from the Record alone, so a run
that is later expanded by a human can exceed the cap by the typeset calls it
already spent.

## Product entry

The normal CLI has no subcommand: it idempotently provisions the private
product profile and pinned scientific runtime, mounts the built Web UI, and
starts the real App Server-backed product on loopback. It never falls back to
the deterministic fixture.

```bash
PYTHONDONTWRITEBYTECODE=1 \
PYTHONPATH=src:src/derivation_api \
uv run --project src/derivation_api python -m derivation_app
```

Use `--codex-executable PATH` when the pinned Codex executable is not available
as `codex` on `PATH`. The default profile is outside the repository in the
platform's application-data directory. The reusable `auth.json` must already
have been created by the official ChatGPT login flow; this entry does not
implement a second login protocol.

`doctor` performs local, zero-model-call diagnostics. It does not provision,
log in, start App Server, or claim that the stored account is a valid ChatGPT
subscription; the live same-client authorization still checks that when a Run
starts.

```bash
PYTHONPATH=src:src/derivation_api \
uv run --project src/derivation_api python -m derivation_app doctor
```

The server accepts only `127.0.0.1`, `::1`, or `localhost` bindings.

## Problem Intake boundary

`intake_session.py` and `intake_session_service.py` own the persistent,
provider-neutral IntakeSession state machine. SQLite is authoritative for the
versioned Problem Specification, Decision Log, append-only Conversation
Archive, optimistic revision, idempotency receipts, and App Server thread
generation lineage. A healthy session resumes one persistent Intake thread
across model rounds while each App Server client and product-profile lock lives
only for that round.

Each model round returns the complete dependency-ready decision frontier. The
Web client renders all independent questions together, including public
recommendations and an Other/custom answer, and submits the round atomically.
Candidate specifications pass an independent ephemeral, tool-free audit before
one explicit user confirmation. Corrections create new specification versions;
thread loss creates a visible replacement generation from the canonical stored
state.

The clean-break HTTP surface is `/api/intake/sessions` plus the per-session
`rounds`, `confirm`, and `cancel` commands. The old stateless
`/api/intake/turn` adapter was removed. A confirmed IntakeSession can create a
Run only when the Record V1 compatibility projection exactly matches the
confirmed specification. Run evidence then contains immutable
`intake/problem_specification.json`, `decision_log.json`,
`conversation.jsonl`, and `handoff_manifest.json` with content hashes.

## Run catalog

`GET /api/runs` combines the current writable product run root with compatible
Record V1 archives under repository `runs/`. An archive is listed only when its
manifest is compatible and strict replay succeeds. Archived validation runs are
read-only: their tree and route content are visible, while pause, resume,
interrupt, and branch commands return `archive_run_read_only`.

## Development server

Run the deterministic server from the repository root:

```bash
PYTHONDONTWRITEBYTECODE=1 \
PYTHONPATH=src:src/derivation_api \
uv run --project src/derivation_api python -m derivation_app \
  dev --fake --run-root runs/derivation-app-dev
```

The deterministic factory accepts only explicit fake providers and
`deterministic-fake-runtime`, so fixture runs cannot be mislabeled as App Server
evidence. Pass `--web-dist PATH` to serve a built frontend at `/`; FastAPI keeps
ownership of `/api/*` and `/healthz`.
