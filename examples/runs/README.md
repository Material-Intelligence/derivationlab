# Example runs

Each directory here is one complete record: the append-only `events.jsonl` that
carries the hash chain, the runtime's `manifest.json` sidecar, and a
`viewer.html` rendered from the events by this repository's own renderer.

Verify and re-render any of them yourself, from the repository root:

```
export PYTHONPATH=src
python3 -m derivation_agent_record verify examples/runs/deterministic_fixture/events.jsonl
python3 -m derivation_agent_record render examples/runs/deterministic_fixture/events.jsonl \
    --output /tmp/viewer.html
```

`verify` replays the whole record and fails if any event hash, chain link,
sequence number or cross-event rule does not hold. `render` writes a single
self-contained HTML file: no script tag, no stylesheet link, no web font, no
external URL of any kind. Open it from `file://` with the network off.

---

## `deterministic_fixture/`

**Deterministic runtime, no model was called.** A stand-in runtime in place of a
language model, recorded exactly as a real run would be. It exercises 12 of the
16 Record v1 event types — `run_created`, `branch_created`,
`branch_status_changed`, `model_call_started`/`_chunk`/`_finished`,
`step_revision_sealed`, `check_requested`/`check_completed`,
`candidate_declared`, `judgement_requested`/`judgement_completed`. It contains
no human intervention, no failed or aborted call, and no selection: the viewer
says *"No selected submission"*, because this run stops at two judgements
without settling on one. The four types it never reaches —
`human_action_recorded`, `model_call_failed`, `model_call_aborted`,
`selection_recorded` — and the Record v1.1 vocabulary are what the second
record below is for.

| | |
|---|---|
| Run id | `run_ab141dc1abe94a3591b8e35a6e76340e` |
| Record version | `derivation-agent-event-v1` |
| Events | 51 |
| Branches / step revisions | 2 / 4 |
| Model calls | 10 |
| Checks requested / completed | 4 / 4 |
| Candidates / judgements | 2 / 2 |
| Backend | `deterministic-fake-runtime` v1 |
| Writer / checker / judge | provider `fake` for all three |
| Problem | differentiate `x^3 + sin(x)` with respect to `x` |
| Network access / web search / MCP servers | false / false / none |

What it is good for: reading the contract by example, and having a record that
`verify` must accept on every machine, forever, with no credentials and no
network. What it is **not**: evidence that a model can derive physics. Nothing
in this run was produced by a language model, and the problem is a first-year
calculus exercise chosen because its answer is not in dispute.

Two things in the files are worth explaining rather than hiding:

- `manifest.json` has `runtime_config.auth_mode: "chatgpt"`. That is the
  product's only auth mode and it is recorded unconditionally; it does not mean
  a subscription was used here. The fields that say what actually ran are
  `api_config.backend` and the three provider entries, and all four name the
  deterministic fake.
- The `run_created` event cites the contract document and the two JSON schemas
  by path and SHA-256. Those strings are inside the hash chain, so they cannot
  be rewritten without destroying the record. All three files ship here,
  byte-identical, under `docs/spec/`. The contract it cites is the Chinese
  document `docs/spec/DERIVATION_AGENT_RECORD_V1_cn.md`, which the runtime
  hashes into every Record v1 it writes; it ships untranslated next to the
  English translation (`docs/RECORD_SPEC.md`) because only those bytes hash to
  the pinned digest. It is the public edition of the upstream original, and
  this record was regenerated against it (`docs/spec/README.md`).
  `tools/verify_record_pins.py` checks all three pins.
- `code_commit` is forty zeros, the placeholder the fake service writes when it
  is not given a commit. The record was produced by this repository's own
  deterministic runtime (`derivation_app.factory.create_fake_app`, with the
  problem in `manifest.json`), and it is evidence about the record format, not
  about a particular build.

---

## `worked_v1_1/`

**Synthetic. No runtime produced this record and no model was called.** It was
built event by event from the Record v1.1 contract by
[`tests/v1_1_example.py`](../../tests/v1_1_example.py), and the test suite pins
the file below against that builder byte for byte, so the record cannot drift
away from the code that describes it.

It is written rather than captured for one reason: Record v1.1 added
`source_evidence_registered`, which puts third-party source text *inside* the
hash chain, where it can never be removed again. A real v1.1 run from this
project therefore cannot be published as it stands. Rather than ship no v1.1
record at all, this one states the vocabulary directly.

| | |
|---|---|
| Run id | `run-v1-1-worked-example` |
| Record version | `derivation-agent-event-v1.1` |
| Events | 48 |
| Distinct event types | 21 of the 22 the contract defines |
| Branches / step revisions | 2 / 2 |
| Model calls | 9 (7 finished, 1 failed, 1 aborted by a human) |
| Checks requested / completed | 3 / 3 (`instrument_failure`, `objection`, `ok`) |
| Candidates / judgements / selections | 1 / 1 / 1 |
| Backend | `synthetic-record-builder` v1 |
| Writer / checker / judge | provider `fake` for all three |
| Problem | differentiate `x^3 + sin(x)` with respect to `x` |

What happens in it, in order: a Writer call a human aborts; a second Writer call
whose raw output arrives with a LaTeX control word damaged in transport, which a
recorded `writer_output_normalized` repairs before anything is sealed; a check
whose instrument times out, which a human clears with `resume_branch` so that
`check_retry_authorized` can allow the retry; a retry that returns an objection
quoting the step; a revision the runtime records but declines to apply
(`model_revision_deferred`) and then one it applies (`model_revision_applied`);
an explicit `writer_route_completion` of the unchanged checked route; and a
candidate, judgement and selection.

The one type it does not contain is `source_evidence_registered`, for the reason
above. The macro-expansion normaliser that type gates is therefore not
exercised by any shipped record either; the suite builds a record that does
exercise it, in memory, in `tests/v1_1_coverage.py`.

Because this record was written here rather than captured elsewhere, all three
of its `run_created` pins name files that ship in this repository and hash to
exactly what it records: `docs/RECORD_SPEC.md` and the two 1.1 schemas under
`src/derivation_agent_record/schemas/`. Its `code_commit` is a placeholder
digest computed by the builder, not a commit. Run `shasum -a 256` on any of the three and
compare, or run `python3 tools/verify_record_pins.py`, which does it for every
record here. One consequence: editing the contract document changes this
record, and the suite refuses to let the two drift apart.

What it is good for: seeing what Record v1.1 added, and having a v1.1 record
that `verify` must accept. What it is **not**: evidence about any model, or
about anything at all outside the record format — the derivation in it is a
first-year calculus exercise, written by hand.

---

## `uniformly_charged_sphere/`

**A real model run, closed book.** `run_69b24aef161f4053b69559dfe338eb5a`: the
electrostatic field of a uniformly charged solid sphere, inside and outside,
derived by `gpt-5.6-sol` through `codex-app-server` 0.147.0. The contract runs
end to end — an intent ledger and two derivation steps, all three sealed and
each independently checked, then a candidate and a judgement with verdict
`pass` — in 7 model calls out of a budget of 24.

| | |
|---|---|
| Run id | `run_69b24aef161f4053b69559dfe338eb5a` |
| Record version | `derivation-agent-event-v1` |
| Events / branches / step revisions / model calls | 35 / 1 / 3 / 7 |
| Checks / candidates / judgements | 3 (all `ok`) / 1 / 1 (`pass`) |
| Backend | `codex-app-server` 0.147.0 |
| Writer / checker / judge | provider `openai`, `gpt-5.6-sol`, medium effort, all three |
| Network access / web search / MCP servers | false / false / none |
| Source pack / references / `source_evidence_registered` | none / none / 0 |

Closed book: `reference_allowed` is false, the source pack is null and the
record registers no external source text. `runtime_config.auth_mode` in
`manifest.json` is there for the same reason as in the fixture above — it is
the product's only auth mode and is written unconditionally; what actually ran
is `api_config.backend` and the three provider entries.

What it is **not**:

- **Not a research result.** A first-year exercise whose answer is in the
  model's weights, so it tests the machinery — sealing, checking, judgement,
  replay — against an independently known answer.
- **Not an independent verdict.** Writer, checker and judge are the same model:
  the judgement is an independent *call*, not an independent *model*.
- **Not a blind cold run.** `problem.accepted_decisions` admits Maxwell's
  electrostatic equations, the divergence theorem and the uniqueness theorem as
  external primitives, to be recorded as assumed rather than derived; only
  spherical symmetry of the field is withheld. It is a scoped exercise.
- **Not the first attempt.** Two earlier attempts failed — one killed by the
  checker with `hard_defect`, one by an `invalid_model_output` inside the
  checker's evidence protocol — and are not shipped.
- **Not a reproducible tree.** `code_commit` `f47623cb` is the real upstream
  commit this repository was exported from; the tree that ran was a subset of
  that commit's files. Read the field as provenance, not as a build recipe.
- **One of its pins does not resolve here.** Its two schema pins resolve like
  the fixture's. Its `record_spec` pin names
  `docs/spec/DERIVATION_AGENT_RECORD_V1_cn.md` at `290a674f…`, the digest of
  the upstream original of that document; this repository ships a public
  edition in which two items of its §15 list, written in project-local
  shorthand, were made generic, so the file at that path hashes to something
  else. The digest is inside the hash chain and cannot be changed without
  destroying the record. `tools/verify_record_pins.py` reports this one pin as
  withheld rather than resolved (`docs/spec/README.md`); `verify` is
  unaffected, because replay does not read the pinned file.

---

## License

The code in this repository is Apache-2.0 (see `LICENSE`). The run records in
this directory — `events.jsonl`, `manifest.json` and the rendered
`viewer.html` — are data, and are released under
[Creative Commons Attribution 4.0 International (CC-BY 4.0)](https://creativecommons.org/licenses/by/4.0/).
Attribute them to Jiahao Xie.
