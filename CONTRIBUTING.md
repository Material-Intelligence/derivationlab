# Contributing

## Send us a record we verify wrong

That is the contribution this project wants most. The verifier is the part of
this repository everything else rests on; the only real evidence about it is a
record it judges incorrectly. Two
kinds are equally useful:

- a record that **should verify and does not** — the rules are too strict, or one
  of them is wrong;
- a record that **verifies and should not** — some claim in it is unsupported by
  the events, and no rule caught it.

Either becomes a permanent test case, with attribution, the day it is merged.

### How to send one

Open an issue with:

1. The `events.jsonl` itself, attached or inline.
2. The exact command you ran and its exact output, including the exit code.
3. The verdict you expected, and the rule or contract section you expected to
   produce it.

**One hard rule about the file.** It must contain no `source_evidence_registered`
event. That event carries third-party source text *inside* the hash chain, so it
cannot be removed from a record afterwards without destroying the record — which
means such a file can never be published, and this repository cannot accept one.
`tests/test_examples.py` enforces this for every shipped record.

### Where it lands

Accepted records go into the corpus, which the suite discovers by directory:

```
tests/records/should_verify/<case>/events.jsonl
tests/records/should_verify/<case>/why.md
tests/records/must_be_rejected/<case>/events.jsonl
tests/records/must_be_rejected/<case>/why.md
```

Adding a directory is all it takes — `tests/test_records_corpus.py` walks both
trees. `why.md` is required and is the part worth writing carefully: name the
rule that should decide the case, and the expected message. A record with no
account of why it is there stops being a test and becomes a puzzle.

## Reporting a soundness bug privately

If you would rather not open a public issue — for instance because the record
demonstrates a way to make a false claim verify — see [`SECURITY.md`](SECURITY.md).

## Working on the code

Every command runs from the repository root. The README's "Testing" section has
the same list with context.

```
# Record verifier and repository tooling: standard library plus pytest
PYTHONPATH=src python3 -m unittest derivation_agent_record.selftest
python3 -m pytest
ruff check . && black --check .
python3 tools/verify_record_pins.py

# Runtime, service and API (uv reads src/derivation_api/uv.lock)
PYTHONPATH=src:src/derivation_api uv run --frozen --project src/derivation_api \
    pytest -q --import-mode=importlib src/derivation_app/tests src/derivation_api/tests \
    src/derivation_runtime tools/formula_whitelist/tests
uv run --frozen --project src/derivation_api ruff check src/derivation_app src/derivation_runtime src/derivation_api

# Web UI
cd src/derivation_web && npm ci && npm test && npm run api:check && npm run typecheck && npm run lint && npm run build
```

The record package (`src/derivation_agent_record/`) imports nothing outside the
Python standard library and must stay that way: a record has to remain
verifiable years from now on a machine with no network and no wheels.
Dependencies are welcome in the other packages and in the test and tooling
layers.

A change to the HTTP models changes `src/derivation_api/openapi.json`; regenerate
the web client with `npm run api:generate` in `src/derivation_web`, and CI checks
that the two agree. A change to `docs/RECORD_SPEC.md` changes the digest that
`examples/runs/worked_v1_1/` pins; the suite says so and the builder in
`tests/v1_1_example.py` regenerates it. The Chinese documents under `docs/spec/`
are pinned by every record the runtime writes and are never edited.

## Porting to another platform

Runs and PDF reports are pinned to `darwin-arm64` today. Two pins make a new
platform work, and both are welcome as one pull request:

1. **A Tectonic 0.17.0 binary for it** in
   `config/reporting/tectonic_runtime.lock.json`: the release archive's and the
   binary's SHA-256, a target entry with `"status": "provisioned"`, and the
   release URL under `release` in `config/reporting/tectonic_bundle_manifest.json`.
   The bundle is the same on every platform. `python3 tools/provision_tectonic.py`
   must then install and verify it there.
2. **A formula engine whitelist generated on it**, with
   `tools/formula_whitelist/generate.py` against that provisioned runtime (its
   README has the command). The runtime refuses a whitelist from another
   platform, so this is what lets the service suite and runs start there.

Say in the pull request which machine you generated them on and paste the
output of the provisioning script and the generator.

One convention that is not obvious from the code:

- **Tests state the contract.** A test that mocks the verifier tests nothing.
  Build a real record, break one rule, and require the real replay to reject it —
  `tests/conftest.py` and `tests/v1_1_example.py` are the builders to reach for.
