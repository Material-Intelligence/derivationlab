# Derivation Agent Record — the contract

> Status: **frozen candidate specification**. Version id: `derivation-agent-record-v1`,
> extended but not replaced by Record 1.1 (§17).
>
> This specification defines recording and replay semantics only. It does not
> choose a writer, checker or judge, an effort level, a step size or an
> active-branch cap, and it implements no real model backend. Every run MUST
> write its own configuration into the record explicitly; v1 has no hidden
> defaults.

**About this document.** It is an English translation of the contract
documents the runtime hashes into every record, cross-checked line by line
against the JSON schemas and the replay engine in this repository. The
originals, in Chinese, ship untranslated under `docs/spec/` because their bytes
are what the records pin (see `docs/spec/README.md`). Where the original text and
the code disagreed, the code wins and the disagreement is recorded in §18. This
file is a translation, not the hashed artefact of a Record v1: see §18.1 before
treating any sentence here as the frozen contract.

Normative keywords: **MUST**, **MUST NOT**, **SHOULD**, **MAY**. Unless the text
says otherwise, a MUST in §0–§16 is enforced by the replay engine: a record that
breaks it fails `verify`. The requirements replay cannot check — the
producer-side obligations that the runtime, not the verifier, has to keep — are
collected in §17.8 and §18.17.

---

## 0. Purpose and boundaries

A run of a derivation agent MUST leave behind a record from which any later
reader, working from the event log alone, can answer: what happened, who caused
it, exactly which text was checked, why a candidate was eligible or not, and
which body of text the final selection actually points at.

The single source of truth in v1 is an append-only JSONL event stream. Branches,
step revisions, candidates, judgements, selections, the canonical JSON state and
the HTML page are all replay products of that one stream. They MUST NOT hold a
second, mutable truth in a database or a user interface.

This specification governs audit and state semantics. It does not prove that a
derivation is physically correct. A step-level check can only falsify a
definite defect; the final scientific verdict belongs to an independent
judgement object.

## 1. The eight formal objects

| Object | Immutable identity | Role |
|---|---|---|
| Run | `run_id` | Freezes code, specification, task, source pack, models, backend, granularity, budgets and input boundary |
| Branch | `branch_id` | One derivation lineage: parent, fork point, inherited prefix, working hypothesis and status history |
| StepRevision | `step_revision_id` | One immutable five-field step; a human edit creates a new replacement branch and revision and never overwrites an old object |
| ModelCall | `model_call_id` | The start, streamed fragments and finish/failure/abort of one writer, checker or judge call, including partial output |
| Check | `check_id` | A step-level check bound to an exact step revision and its `output_sha256` |
| Candidate | `candidate_id` | An immutable snapshot of a complete transcript at one moment; not an alias for a branch |
| Judgement | `judgement_id` | A final verdict bound to an exact candidate transcript hash |
| Selection | `selection_id` | The chosen submission: an eligible candidate plus a passing judgement |

HumanAction is stored separately, as a provenance event. It is not a ninth
scientific object, but every human pause, resume, kill, step revision, direction,
hypothesis change, call abort and candidate selection MUST be preceded by an
explicit HumanAction, before the corresponding state change is recorded.

## 2. Event envelope and hash chain

Each line of the log is one complete JSON object, and MUST carry exactly these
fields and no others. A producer MUST NOT write a blank line; a reader tolerates
one, because a stray trailing newline is a transport artefact and not a claim
about the record. `load_events` therefore skips whitespace-only lines and
numbers the rest 1, 2, 3 … as `seq` requires:

```json
{
  "schema_version": "derivation-agent-event-v1",
  "run_id": "run_fixture_001",
  "seq": 1,
  "event_id": "evt_0001",
  "recorded_at": "2026-08-25T18:00:00Z",
  "type": "run_created",
  "actor": {"kind": "system", "id": "record-runtime"},
  "prev_event_sha256": null,
  "event_sha256": "...",
  "payload": {}
}
```

Constraints:

1. `seq` MUST increase contiguously from 1; `event_id` MUST be unique within the
   run. `run_id` MUST NOT change within a record.
2. The first event MUST be `run_created`, with `prev_event_sha256 = null`.
3. Every later event's `prev_event_sha256` MUST equal the previous event's
   `event_sha256`.
4. `event_sha256` is the SHA-256 of the canonical JSON of the event object with
   the `event_sha256` field itself removed.
5. Canonical JSON: UTF-8, keys in lexicographic order, no insignificant
   whitespace (`,` and `:` separators), non-ASCII characters not escaped.
6. Changing an event body, its order, its back-link or its hash MUST make
   `verify` fail.
7. The hash chain detects tampering; it is not a digital signature. A formal
   scientific freeze SHOULD still be anchored by a git commit or tag, or by an
   external signature over the log head hash.

`schema_version` MUST be the same on every event of a record. Mixing
`derivation-agent-event-v1` and `derivation-agent-event-v1.1` within one record
is rejected.

V1 event types:

- `run_created`
- `human_action_recorded`
- `branch_created`, `branch_status_changed`
- `model_call_started`, `model_call_chunk`, `model_call_finished`,
  `model_call_failed`, `model_call_aborted`
- `step_revision_sealed`
- `check_requested`, `check_completed`
- `candidate_declared`
- `judgement_requested`, `judgement_completed`
- `selection_recorded`

Record 1.1 adds six more: `model_revision_applied`, `model_revision_deferred`,
`source_evidence_registered`, `check_retry_authorized`,
`writer_route_completion` and `writer_output_normalized` (§17). A v1 record
carrying any of them is rejected. The full per-type field list is in
[`EVENT_VOCABULARY.md`](./EVENT_VOCABULARY.md).

The shape of a single event is constrained by
`docs/spec/derivation_agent_event_v1.schema.json` (Record 1.1:
`src/derivation_agent_record/schemas/event-v1.1.schema.json`). The cross-event state
machine, the actor rules, the hash bindings and the provenance rules are
constrained by the replay engine in `src/derivation_agent_record/`. The schemas are
the weaker of the two: a record can satisfy the schema and still be rejected by
replay, and that is the intended division of labour.

## 3. Run: every experimental condition is recorded explicitly

`run_created` MUST freeze:

- the path and SHA-256 of this specification, the event schema and the
  canonical schema;
- the 40-character code commit;
- the id and SHA-256 of the task and of the source pack;
- the `granularity` of this run;
- `max_active_branches` and `max_model_calls` for this run;
- the provider, model and effort of the writer, the checker and the judge;
- the backend name and version;
- `reference_allowed` and the allowed input paths.

The actor of `run_created` MUST be `system`, and the event MUST occur exactly
once, as the first event.

V1 supports recording `one_claim` and `one_task` granularity, and **selects
neither automatically** for any run. `max_active_branches` MAY be a positive
integer or an explicit `null`; `null` means this run enforces no count cap under
the record contract, and implies nothing about a recommended runtime value.

Allowed input paths MUST be safe repository-relative paths: not absolute, and
containing no `..` component. When `reference_allowed` is `false`,
`allowed_paths` MUST NOT list any path whose first component is `reference`.
This rule exists so that a run cannot claim in prose that it used no reference
material while listing it in the input boundary.

A different task hash, pack hash, model, effort, backend, input boundary or
granularity means a different Run. Two runs MUST NOT be spliced into one event
stream.

## 4. Branch: a lineage, not a mutable text container

### 4.1 Creation

Root branch:

- `parent_branch_id = null`
- `fork_mode = root`
- inherits no steps (`inherited_step_revision_ids = []`, `anchor_step_revision_id = null`)
- `created_reason = root`, `human_action_id = null`, actor kind `system`

Child branch:

- `fork_mode = after`: inherits the exact parent prefix up to and **including**
  the anchor;
- `fork_mode = replace`: inherits the exact parent prefix up to but **excluding**
  the anchor;
- MUST record the parent, the anchor, the inherited list of step revision ids,
  and the new working hypothesis;
- MUST NOT substitute a summary for the parent transcript, and MUST NOT
  re-copy ancestor steps under new ids.

A new branch MUST start `active` or `paused`.

Permitted child origins: `model_alternative`, `instrument_retry`,
`human_direction`, `human_hypothesis`, `human_revision`. The last three MUST
cite a prior HumanAction, and:

- the actor MUST be `human` and the hypothesis MUST be human-sourced, citing the
  event id of that action;
- `human_direction` requires a `set_direction` action and `human_hypothesis` a
  `set_hypothesis` action; both MUST target the parent branch, and the branch
  hypothesis text MUST equal the hashed content of the action;
- `human_revision` requires a `revise_step` action, `fork_mode = replace`, and
  the action MUST target the step being replaced (the anchor).

`model_alternative` MUST be created by a `model` actor with a model-sourced
hypothesis. `instrument_retry` MUST be created by a `system` or `model` actor,
with a `model` or `inherited` hypothesis, and MUST NOT cite a human action.

### 4.2 The only legal semantics for a human edit

A human edit of an earlier step MUST NOT overwrite it in place.

The legal sequence is:

1. `human_action_recorded(action = revise_step)` naming the old
   `step_revision_id`, carrying the canonical text of the new five fields and
   its SHA-256;
2. create a child branch with `fork_mode = replace` and
   `created_reason = human_revision`;
3. the child inherits the exact prefix before the old step;
4. seal a new StepRevision in the same `step_slot` with
   `revision = old.revision + 1`;
5. the old branch, old revision, old checks and old candidates are retained
   permanently.

The replay engine rejects a duplicate `step_revision_id`, an in-place rewrite,
a wrong inherited prefix, a wrong revision number, and a human-edited step with
no corresponding HumanAction.

### 4.3 Status

Statuses: `active`, `paused`, `parked`, `completed`, `killed`.

- `paused`: a recoverable human or scheduling pause;
- `parked`: an instrument, budget or configuration condition was not met; this
  is not a scientific failure;
- `completed`: the writer declared the task goal reached, so a candidate may be
  frozen;
- `killed`: the route is terminated; its text is retained as an appendix.

Permitted transitions are fixed by the machine validator:

| From | To |
|---|---|
| `active` | `paused`, `parked`, `completed`, `killed` |
| `paused` | `active`, `parked`, `killed` |
| `parked` | `active`, `paused`, `killed` |
| `completed` | `active`, `killed` |
| `killed` | — (terminal) |

A hard defect MAY take a branch from `completed` to `killed`; a zero-body
instrument failure MAY take it from `active` to `parked`. A pause and a kill
MUST NOT share a reason code.

Each `branch_status_changed` carries a `reason_code`, and the reason determines
who may record it and what it MUST cite:

| `reason_code` | Actor | To | Must cite | Must not cite |
|---|---|---|---|---|
| `human_pause` | human | any legal | a `pause_branch` action on this branch | check, model call |
| `human_resume` | human | any legal | a `resume_branch` action on this branch | check, model call |
| `human_kill` | human | any legal | a `kill_branch` action on this branch | check, model call |
| `manual_reopen` | human | `active` | a `resume_branch` action on this branch | check, model call |
| `writer_complete` | model | `completed` | — | action, check, model call |
| `hard_defect` | checker or system | `killed` | a check with verdict `hard_defect` whose target step is in this transcript | action, model call |
| `instrument_failure` | system | `parked` | a failed or aborted writer call on this branch with `body_chars = 0` | action, check |
| `writer_blocked` (1.1) | model | `parked` | a finished writer call on this branch whose control decision is `blocked` | action, check |
| `runtime_failure` (1.1) | system | `paused` | a failed or aborted writer or checker call on this route | action |

`from_status` MUST equal the branch's current status.

If the Run gives a positive integer `max_active_branches`, any creation or
resume that would take the active count above the cap is rejected. If it is
`null`, the contract imposes no count cap. V1 fills in no default number.

## 5. StepRevision: five fields, immutable, exact origin

Every sealed step MUST have five non-empty fields:

1. `claim`: what this step asserts;
2. `why`: why this step is taken now;
3. `source`: what it rests on;
4. `derivation`: the derivation itself;
5. `scope`: the range of validity and the assumptions.

`output_sha256` is the SHA-256 of the canonical JSON of the five-field object.

A step MAY be sealed only on an `active` branch, and only into the next slot
(`step_slot = len(transcript) + 1`). A step that starts a new slot MUST have
`revision = 1` and `replaces_step_revision_id = null`.

Origin:

- `origin.kind = model`: cites a finished writer ModelCall; the call's target
  MUST be the same branch and slot, and the call's output hash MUST equal the
  step hash. The sealing actor MUST be `model`, and the step MUST NOT cite a
  human action.
- `origin.kind = human`: permitted only for a replacement revision; cites a
  `revise_step` HumanAction whose content hash MUST equal the step hash. The
  sealing actor MUST be `human`, and the step MUST NOT cite a model call.

A missing field, an empty field or a wrong hash is a deterministic format
failure, rejected by the program. It MUST NOT be handed to a checker to be
adjudicated as a possible defect.

If an output is truncated but already carries body text, a later runtime MAY use
several ModelCalls to complete the same unsealed step; a StepRevision is
produced only once all five fields are complete. A call with zero body text
produces no StepRevision.

## 6. ModelCall: instrument readings, kept apart from scientific judgement

Lifecycle:

```text
started -> finished
        -> failed
        -> aborted
```

No call may still be `started` when the record ends.

`model_call_chunk` MAY carry the `analysis`, `body` and `raw` channels, with
contiguous indices and a SHA-256 over each fragment's text. If any `body`
chunks are present, their concatenation MUST equal the finished output text.
A finish event stores the complete output text, its hash, the character count,
the finish reason and usage; a failure or abort stores the partial text, its
hash, its character count and the failure class.

Roles: `writer`, `checker`, `judge`; the actor kind MUST be `model`, `checker`
and `judge` respectively, on the start event and on every chunk and terminal
event of that call. The provider, model and effort of each call MUST equal the
frozen configuration for that role in the Run; to change a model or an effort
level, open a new Run rather than deviating on one call.

A writer call MUST target the next slot of an `active` branch. A checker call
MUST target a check that is still `requested`; a judge call MUST target a
judgement that is still `requested`. Two calls with the same role and the same
target MUST NOT be in flight at once. If `max_model_calls` is a positive
integer, the number of calls in the record MUST NOT exceed it.

A failed call with `body_chars = 0` is an instrument reading of the class
`zero_body_instrument_failure`. It MAY park a branch, but it MUST NOT:

- produce a StepRevision;
- produce a Check hard defect;
- count as a failure of the physical route;
- be used to "continue" an empty transcript.

## 7. Check: bound to an exact revision and hash; its only power is to falsify

### 7.1 Request and completion

`check_requested` is recorded by `system` and stores:

- `check_id`
- `target_step_revision_id`
- `target_output_sha256` (which MUST equal that step's current `output_sha256`)
- whether the check is required for a candidate
- the reason for the request

`check_completed` is recorded by `checker`, MUST repeat the same revision and
hash, and MUST cite the exact checker ModelCall. Any mismatch is rejected.
After a human edit creates a new revision, an old check belongs to the old
revision only, and never migrates to the new step.

### 7.2 Verdicts

- `ok`: no actionable objection was found. This is not a certificate of
  correctness.
- `objection`: the reasoning or the route is worth human review, but not enough
  to kill the branch automatically.
- `hard_defect`: satisfies the whitelist below, and MAY automatically terminate
  a route containing that revision.
- `instrument_failure`: the checker call failed; the candidate is blocked, not
  judged wrong. The checker call MUST be in a `failed` or `aborted` state, and
  the evidence list MUST be empty.

For every verdict other than `instrument_failure`, the checker call MUST be
`finished`.

### 7.3 The hard-defect whitelist

A `hard_defect` MUST carry at least one evidence item with a verbatim quotation,
and each item's `kind` MUST be one of:

- `ancestor_quote`: directly contradicts sealed text in the prefix of the route
  under check. `source_id` MUST be a step revision inside the checked
  transcript, up to and including the target step, and the quote MUST be a
  substring of that step's evidence text (§17.6 defines that text for 1.1; in
  v1 it is the canonical JSON of the five fields).
- `hypothesis_quote`: directly contradicts an explicit working hypothesis in the
  lineage of the route under check. `source_id` MUST be a branch in that
  lineage, and the quote MUST be a substring of its hypothesis text.
- `scope_quote`: violates the explicit scope of this step. `source_id` MUST be a
  step revision inside the checked transcript, and the quote MUST be a substring
  of its `scope` field.
- `task_constraint_quote`: violates a hard constraint of the task statement.
  `source_id` MUST equal the run's task id.

Record 1.1 adds `literature_quote` (§17.5).

"I do not like the reasoning", "another route is better", "there may be a
missing step" and "the final answer does not look like the reference" can only
be an `objection`. A checker MUST NOT write the next step, edit the transcript,
issue a final pass certificate, or select a candidate.

### 7.4 Late-arriving results

A Candidate stores a snapshot of its required check ids. If any of them is
still pending at declaration time, the candidate is `provisional`; late results
recompute the status. The replay engine evaluates, in this order:

1. any required check not yet completed → `provisional`
2. (v1 only) any `hard_defect` → `rejected`
3. any `instrument_failure` → `blocked`
4. (1.1 only) any `hard_defect` or `objection` → `conditional`
5. otherwise → `eligible`

The precedence matters when a candidate carries both a hard defect and an
instrument failure: in v1 it is `rejected`, not `blocked` (see §18.2).

A late result on an old revision MUST affect only candidates containing that
revision, and MUST NOT contaminate a replacement candidate.

After a candidate is declared, no further check may be marked required for a
step revision inside its transcript. Otherwise a run could evade checking by
declaring a candidate first.

## 8. Candidate: an immutable full-transcript snapshot

When the writer or a human declares completion, a Candidate is created, storing:

- `branch_id`
- `tip_step_revision_id`
- `transcript_step_revision_ids`, in fixed order
- `transcript_sha256`, the SHA-256 of the canonical JSON of the ordered list of
  `{step_revision_id, output_sha256}` pairs
- `required_check_ids`, which MUST be exactly the set of required checks
  targeting steps in that transcript
- who declared it and why

The branch MUST be `completed` at declaration time, and the transcript MUST
equal the branch's **complete transcript at that moment**. A later resume,
growth, kill or fork of the branch MUST NOT change an existing candidate. A
Selection MUST NOT point at a branch id alone.

`declared_by = writer` requires a `model` actor; `declared_by = human` requires
a `human` actor.

An `objection` does not automatically remove eligibility, but it MUST be shown
in the final-review view; the final judge MAY use it to return `fail` or
`near_pass`.

## 9. Judgement: the final verdict is an independent object

Every reviewable completed transcript goes through `judgement_requested` and
`judgement_completed`, forming an independent Judgement bound to:

- `candidate_id`
- `candidate_transcript_sha256`
- the judge ModelCall
- a verdict: `pass`, `near_pass`, `fail`, `instrument_failure`
- a reason and an optional numeric score

A judgement may be requested and completed only for an `eligible` candidate (in
1.1, also for an explicitly `conditional` one, §17.5). The requester MUST be
`system` or `human` and MUST match the actor kind; the completion actor MUST be
`judge`. A judge instrument failure is not a final `fail`, and its judge call
MUST be `failed` or `aborted`. Step-level checks that are all `ok` do not
automatically produce a `pass` judgement.

## 10. Selection: hash identity, eligibility still valid, final review passed

V1 permits at most one selected submission per run. `selection_recorded` MUST
satisfy all of:

1. the Candidate exists and is currently `eligible` — `conditional` is not
   enough (§17.5);
2. the candidate transcript SHA-256 in the event matches exactly;
3. the cited Judgement targets the same candidate and the same hash;
4. that Judgement is completed with verdict `pass`;
5. a human selection MUST be preceded by a `select_candidate` HumanAction
   targeting that candidate. A `system` actor MAY record the selection instead,
   and then `human_action_id` MUST be `null`.

If a later event makes the selected candidate ineligible, verification of the
complete log fails: the runtime MUST withdraw or redo the selection rather than
keep an ineligible selected submission.

All other completed candidates, failed routes and killed routes are retained as
an appendix, never silently deleted.

## 11. Human provenance and what may be claimed externally

A HumanAction MUST store at least the actor, the action, the exact target, the
reason, the content and the content hash. A pure operation with no content
(pause, resume, kill) MUST record `null` for both content and content hash
explicitly. A `revise_step`, `set_direction` or `set_hypothesis` action MUST
carry content, and the recorded `content_sha256` MUST be the SHA-256 of that
content.

The canonical replay computes, for each Branch, Candidate and Selection:

- `human_touched`
- `content_class = model_only | human_steered | human_edited`
- `steering_action_ids`
- `edit_action_ids`
- `operational_action_ids`

Attribution is computed over the lineage:

- a human gave a direction or a working hypothesis → `human_steered`;
- a human wrote a step directly → `human_edited`, which outranks steered;
- model content only → `model_only`.

Operations such as pause, resume and kill are listed separately and MUST NOT
by themselves reclassify a transcript as human-edited. When a human performs
the Selection, the Selection's `human_touched` is true, but the candidate's own
content provenance MUST NOT be rewritten backwards.

Deriving a human revision from an old branch MUST NOT contaminate that old
branch: the old Branch and Candidate keep computing provenance from their own
ancestors and steps.

External reporting MUST distinguish at least three cases: completed by the model
alone, completed with human direction, completed with human edits to the text.
A single vague `human_touched` flag MUST NOT be used to flatten the difference.

## 12. Canonical state and HTML

The replay engine emits `derivation-agent-canonical-v1` containing: `run`,
`branches`, `step_revisions`, `model_calls`, `checks`, `candidates`,
`judgements`, `human_actions`, `selections`, `summary` and the event-log head
hash. Record 1.1 emits `derivation-agent-canonical-v1.1`, which additionally
contains `source_evidence`.

`summary` carries the object counts, `candidate_status_counts` and
`selected_candidate_id`; `event_log` carries `event_count` and
`head_event_sha256`.

The machine structure is constrained by
`docs/spec/derivation_agent_canonical_v1.schema.json` (1.1:
`src/derivation_agent_record/schemas/canonical-v1.1.schema.json`). Canonical JSON
MUST be byte-stable under the ordering rules: replaying the same event log under
the same specification version MUST produce byte-identical output.

The HTML is a read-only audit page produced by the same verified replay, and
MUST show:

- branch status and content provenance;
- candidate eligibility, exact hashes and required checks;
- judgements and selections;
- the complete event hash chain and payloads.

The HTML is not a control panel and MUST NOT write hidden state outside the
event stream.

## 13. Minimum coverage of the golden fixture

The golden record for this specification MUST cover:

1. a root branch writing two steps normally;
2. the model proposing an alternative branch;
3. a human-directed branch;
4. a writer zero-body failure: the branch is parked and no StepRevision is
   produced;
5. a human revising an earlier step, creating a replace branch and a new
   revision, with the old route retained permanently;
6. the root candidate being `provisional` while checks are pending;
7. a late hard defect making only the root candidate `rejected`;
8. the replacement candidate becoming `eligible` once its checks complete;
9. an independent `pass` judgement;
10. only the replacement candidate being legally selected;
11. `verify` failing when the body, the `seq`, the `prev_event_sha256` or the
    event hash is tampered with;
12. the JSON, the canonical JSON and the HTML rebuilding byte for byte.

Every model call in the fixture is a `mock:*` record. It makes no network
request and constitutes no physical experimental data.

## 14. Files and command interface

Stable files in this repository:

| Role | Path |
|---|---|
| This contract (English translation) | `docs/RECORD_SPEC.md` |
| The contract as pinned by Record v1 (original, Chinese) | `docs/spec/DERIVATION_AGENT_RECORD_V1_cn.md` |
| Record 1.1 runtime contract (original, Chinese) | `docs/spec/DERIVATION_RUNTIME_RECORD_V1_1_cn.md` |
| Event vocabulary table | `docs/EVENT_VOCABULARY.md` |
| Event schema, v1 | `docs/spec/derivation_agent_event_v1.schema.json` |
| Canonical schema, v1 | `docs/spec/derivation_agent_canonical_v1.schema.json` |
| Event schema, 1.1 | `src/derivation_agent_record/schemas/event-v1.1.schema.json` |
| Canonical schema, 1.1 | `src/derivation_agent_record/schemas/canonical-v1.1.schema.json` |
| Standard-library package | `src/derivation_agent_record/` |

The commands below run from the repository root with `PYTHONPATH=src`, or
anywhere once the package is installed.

Offline commands:

```sh
python3 -m derivation_agent_record verify EVENT_LOG.jsonl

python3 -m derivation_agent_record replay EVENT_LOG.jsonl --output CANONICAL.json

python3 -m derivation_agent_record render EVENT_LOG.jsonl --output AUDIT.html

python3 -m derivation_agent_record build EVENT_LOG.jsonl \
  --canonical CANONICAL.json \
  --html AUDIT.html

# from a clone or an unpacked sdist only: it reads the fixtures under tests/
python3 -m unittest derivation_agent_record.selftest
```

`verify` exits 0 on success, 2 on a contract violation — printing the first rule
that failed — and 3 when it cannot read the file it was given, which is not a
verdict about a record. None of these commands needs an API key, calls a
provider, or reads reference material.

## 15. What v1 explicitly does not do

- it does not choose a model, an effort level, a granularity, a step size or an
  active-branch cap;
- it does not implement a streaming provider, concurrency locks, a durable
  database or a run-time user interface;
- it does not build retrieval, a knowledge base, an ontology, symbolic proof or
  automatic branch merging;
- it does not treat a Check as a final review;
- it does not treat an instrument failure as a scientific failure;
- it does not run any particular scientific problem;
- it does not modify a frozen task statement, judge configuration or historical
  tag established before it;
- it does not read reference material as a fixture or as prompt input.

## 16. Change control

Any change to the following semantics MUST raise the specification version and
MUST NOT be applied as a silent compatible change:

- whether a StepRevision is immutable;
- whether a human revision must derive a Branch;
- the revision/hash binding of a Check;
- the event hash-chain algorithm;
- the transcript snapshot of a Candidate;
- eligibility and late hard defects;
- the candidate/hash binding of a Judgement;
- the preconditions for a `pass` before a Selection;
- provenance classification.

Adding an optional field normally also requires a `v1.1` or an explicit
migration note, because the V1 schema rejects unknown fields by default. An old
event is always replayed under the schema version it declares; it is never
auto-upgraded or overwritten.

---

## 17. Record 1.1

Record 1.1 is an explicit version extension. It does not replace frozen v1.

### 17.1 Version selection and compatibility

A producing runtime defaults to record version `1.0`, which keeps the original
event and canonical tags, the original configuration shape, and the bytes,
hashes and reading rules of every historical run. Version 1.1 MUST be chosen
explicitly. Mixing versions within one run is forbidden, and new behaviour MUST
NOT be pushed silently into an old log.

The 1.1 event tag is `derivation-agent-event-v1.1` and the projection tag is
`derivation-agent-canonical-v1.1`. JSON Schema checks shape; replay separately
checks origin, hashes, ordering and cross-event invariants.

### 17.2 Configuration additions

1.1 adds `checker_enabled` (boolean) and `max_local_repairs` (non-negative
integer) to the run configuration; the runtime defaults are `true` and `2`.
`max_model_calls = null` means no global call budget; the historical default of
100 does not change because of this. Disabling the checker issues no checker
call and creates no receipt, and it does **not** mean a scientific check passed.

The 1.1 generation loop does not automatically invoke the older in-task terminal
judge. Forming a candidate ends generation; `review_ready` means only that the
transcript may be sent for independent final review — it is not a pass. An
answer-aware final review runs in an isolated post-run stage with frozen inputs,
configuration and outputs; it MUST NOT be written as a generation-side judgement
and MUST NOT be fed back to the writer. The 1.0 automatic judge behaviour is
unchanged.

### 17.3 Autonomous output and model-initiated revision

The five fields `claim/why/source/derivation/scope` are unchanged. A 1.1 writer
emits a control object alongside its text, as exactly one `raw` chunk containing
canonical JSON with either the keys `{decision, alternatives}` or
`{decision, alternatives, revise_step_revision_id, reason}`:

- `decision` is one of `continue`, `fork`, `complete`, `revise`, `blocked`;
- `alternatives` is a list of non-empty strings, non-empty if and only if
  `decision = fork`;
- `decision = revise` MUST name an exact `revise_step_revision_id` on the
  current route and give a reason; the text is a complete replacement;
- `decision = blocked` MUST give a reason and forms no completed candidate;
- any decision other than `revise` MUST leave `revise_step_revision_id` null.

Both checker modes carry the same autonomous revision rights.

`model_revision_applied` is emitted by a `model` actor with the payload
`parent_branch_id`, `branch_id`, `target_step_revision_id`, `step_revision_id`,
`model_call_id`, `reason`, `content`. One event executes atomically:

1. verify that a finished writer call targeted the parent's next slot and that
   its raw control explicitly requested this revision with this reason;
2. verify that the content hash equals the immutable call output (after
   normalization, if any), that the new ids are unused, and that the old target
   is on the parent route;
3. create a replace child branch inheriting the exact prefix before the old
   target, with the new text in the old slot and `revision` incremented by one;
4. park the parent with the status reason `model_superseded`, keeping the whole
   old suffix; the old suffix does not enter the new candidate.

There is no HumanAction and no fabricated human consent; the origin remains
`model_only`. After a revision, the model session MUST be re-established on the
new valid prefix and MUST NOT reuse provider history containing the old suffix.
If the host crashes after the call finishes but before the atomic revision,
recovery MUST complete one revision from the same persisted output rather than
call the model again.

Consecutive revisions are counted along the lineage of the replaced step, and
are not reset by the checker's wording or verdict. Once the local limit is
reached, that target is temporarily removed from the set of revisable ids; the
model must change sub-target, explore with a stated condition, or declare
`blocked`. A new ordinary derivation segment makes the target processable
again. If the model still returns a forbidden revision,
`model_revision_deferred` records the system-sourced policy disposition, keeps
the original call, and does not dress that text up as a valid step. This
deterministic boundary cannot judge whether a new derivation segment carries
real scientific progress; that judgement still belongs to scientific checking
and final acceptance. The local boundary is not a global call or time budget.

### 17.4 A `hard_defect` does not kill a branch in 1.1

In 1.1 a model's `hard_defect` does not automatically kill the whole branch. It
is an evidenced accusation that must be answered, and it MUST NOT be promoted to
a machine-confirmed counterexample. Genuine deterministic falsification must
come from an interpretable verifier; this version does not automatically
elevate ordinary computational output or an LLM boolean judgement into
scientific truth. No general deterministic counterexample hard gate is
implemented yet; computed results stay in the tool audit, the model must still
judge whether a tool result applies, and it must follow the instruction not to
adopt a known counterexample as a valid premise. Neither that instruction nor
the availability of a computation tool may be claimed as completed
machine-level mathematical counterexample detection.

The record contract still permits a `hard_defect` branch kill to be recorded in
1.1; not doing it automatically is a runtime policy, not a contract prohibition
(§18.3).

### 17.5 Conditional candidates and the evidence registry

A 1.1 candidate stores `unresolved_check_ids`, derived exactly from the
`objection` and `hard_defect` checks on its valid route. When that list is
non-empty the candidate's status is `conditional`. A conditional candidate MAY
be sent to an independent judge, carrying its unresolved checks; it MUST NOT be
selected automatically as an unconditionally passed result. Opinions on a
superseded revision target stay in the history and MUST NOT be mixed into the
replacement route. A tool or platform failure is not a scientific verdict.

`source_evidence_registered` is emitted only by `system`, and only before the
first model call of the run. It registers a method source with `source_id`,
`kind = literature_quote`, the complete snapshot text and its SHA-256.
Re-binding the same id is forbidden. A checker's `literature_quote` evidence
MUST come from that registry and MUST be an exact substring of the snapshot, so
the record can be rechecked against the original independently. The catalogue
given to the model contains only ids, hashes and reading instructions; the full
text is read on demand through a read-only source tool, rather than injecting
every paper into every round. The existence of a quotation proves provenance
only; it does not prove that a physical inference holds. A run given no source
material has an empty registry and MUST NOT accept literature citations from
any other condition or from the answer side.

> Note for publication: this event embeds third-party source text verbatim
> inside the hash chain, where it cannot be removed without destroying the
> record. A record containing `source_evidence_registered` should be treated as
> carrying whatever rights attach to that text.

In 1.1, replay also validates evidence on a non-`hard_defect` verdict whenever
the evidence list is non-empty (§18.4).

### 17.6 Ancestor evidence text

In 1.1 an `ancestor_quote` source is the readable five-field text, in the order
`claim`, `why`, `source`, `derivation`, `scope`, each rendered as
`fieldname:\n<text>` and separated by a blank line. Quotes, backslashes,
newlines and Unicode inside a field keep their original values, matching the
decoded fields a transcript read returns. A catalogue SHA-256 is computed over
this readable text; a model MUST NOT be asked to quote text with the escaping of
canonical JSON. The runtime and replay share the same source-construction
function. Record 1.0 keeps its historical canonical-JSON source representation.
Content hashes and event hashes continue to be computed under the original
canonical JSON rules: changing the readable representation of evidence does not
change those original texts.

### 17.7 Host format normalization

`writer_output_normalized` is emitted by a `system` actor, only in 1.1, after
the `model_call_finished` of a writer call and before whatever that call seals
(`step_revision_sealed`, `model_revision_applied` or `writer_route_completion`).
It is written only when the replacement list is non-empty. The payload is
`model_call_id`, `raw_output_sha256`, `content` (the normalized five fields),
`output_sha256` (the hash of its canonical JSON), `policy`, `normalizer_version`
and `replacements`.

The raw call output is never rewritten. Replay verifies, item by item: that the
call is a finished writer call, not yet consumed and not yet materialized into a
step; that `raw_output_sha256` equals the call's output hash and that the raw
output is canonical JSON of the five fields; that applying each replacement in
list order to the text as it then stands (`start`/`end` are the offsets at the
moment that item is applied, and `original` MUST match verbatim) reproduces
`content` exactly; and that `output_sha256` matches and differs from the raw
hash. Each replacement's kind and structure is constrained:

- `control_char_backslash`: `original` is a single character in
  U+0000–U+0008, U+000B or U+000E–U+001F, and `replacement` is one backslash
  immediately followed in the text by an ASCII letter;
- `control_char_removed`: a single character from the same set is deleted, and
  is immediately followed by a backslash and an ASCII letter;
- `del_removed`: U+007F is deleted and is immediately followed by a backslash;
- `ansi_escape_removed`: a complete ANSI CSI sequence is deleted;
- `macro_expansion`: `original` begins with a backslash and a control word, is
  not in the `source` field, cites a registered `source_id` and a `source_line`
  within that source, where that line really does define that macro name
  (`\newcommand`, `\renewcommand`, `\providecommand`, `\def`, `\gdef` or
  `\DeclareMathOperator`); `original` is exactly the macro name plus the
  arguments TeX would read there, and not one character more; `replacement` has
  balanced braces and introduces no unescaped dollar, percent, hash or backtick
  and none of the delimiters `\(`, `\)`, `\[`, `\]`; and `replacement`
  **equals the expansion of that definition applied to that original** — replay
  recomputes it with an independent implementation inside the record package
  (`src/derivation_agent_record/macro_expansion.py`) and rejects the event if it
  does not get the same result. One trailing space is tolerated when the
  expansion ends in a control word and a letter follows it in the field.

For every control-class replacement, `source_id` and `source_line` MUST be
`null`.

After the event, the call carries `normalized_output` in canonical state (the
event id, the raw and normalized hashes, the policy, the version and the number
of replacements), and step sealing, model revision and route completion compare
against the normalized hash instead of the raw call hash. A call with no such
event keeps exactly the historical comparison rules and canonical bytes.

Whether a replacement is "provably unambiguous" is decided by a runtime rule
(`formula-normalization-v1`, implemented by the runtime in
`src/derivation_runtime/formula_normalization.py`, not by the verifier). Replay proves
that the replacements are reversible, structurally legal, consistent with the
sealed content, and that every `macro_expansion` replacement is what its cited
definition produces.

### 17.8 What replay cannot mechanically verify

1. **Which definition was cited.** The runtime picks one definition by its own
   rules; replay verifies only that the cited line really defines the macro that
   way. It does not recompute whether that was the right line to cite. If the
   same macro name is defined differently in two sources and the host cited one
   of them, replay will not object.
2. **Whether expansion should have happened at all.** The engine whitelist is
   not in the record, so replay cannot tell that a name should have been
   typeset directly by the engine and therefore never expanded; nor can it
   reconstruct multi-part groupings of one document supplied by the runtime.
3. **Nested expansion.** The runtime expands definition bodies and arguments
   recursively. When a single-level substitution leaves no registered macro
   name, replay's single-level check is exact; otherwise replay recomputes with
   a whitelist-free approximation (expand every name uniquely defined across the
   registered sources) and rejects only if neither result matches. A name that
   is both an engine command and redefined by a manuscript could therefore make
   a legitimate record fail — the failure mode is closed, not permissive.
4. **Whether text is a quotation.** Whether a formula is quoting a source is
   decided by the runtime; replay only enforces that the `source` field is never
   macro-expanded.
5. **Whether a task-constraint quotation is real.** A `task_constraint_quote`
   is checked only for citing the run's task id. The task text itself is not in
   the record — only its id and SHA-256 — so replay cannot confirm the quoted
   constraint appears in it.
6. **Whether a run is closed-book in substance.** Replay can tell that no
   source was registered; it cannot tell that no source text reached the model
   by another path.
7. **Whether a quotation is substantial.** Evidence is checked only for being
   non-empty text that occurs in the source it cites (§7.3). Replay imposes no
   minimum length and no word or token boundary, so a single character that
   happens to appear in the step satisfies "a hard defect has to quote the text
   it is objecting to". Whether a quotation is long enough to identify what is
   being objected to is a producer policy, not a contract rule.

### 17.9 Stopping, context and recovery

A `writer_blocked` status event MUST be emitted by a `model` actor citing a
finished writer call on that branch whose control decision is explicitly
`blocked`; the branch is parked, the run shows `paused / model_blocked`, and no
completed candidate is formed. Model completion, model inability to continue,
exhaustion of the old global budget and tool failure are all reported
distinctly.

All original text is kept in the append-only record. A long history is presented
to the model as a bounded visible context built from recent complete segments,
plus a catalogue of ids, claims and scopes for the first and recent segments;
the catalogue is navigation, not evidence. Earlier content is paged through a
transcript catalogue query, and read by step id, field and offset. The model
session is re-established periodically, and a full context MUST NOT be treated
as a permanent wait. After a revision, only the new valid route is readable
through those tools; the original history remains in the audit record.

Every new check is fed back; a current opinion MUST NOT be skipped because an
earlier step already carries a dispute. Having received an opinion on the
current tip, a model that decides to keep the content and deliver a conditional
candidate may repeat the tip text in full and declare `complete`.
`writer_route_completion` verifies the current tip, the finished writer call,
the control decision and the content hash, and atomically completes the existing
route; it is an explicit completion decision and produces no duplicate
scientific node and no new check. It also requires that every required check on
that transcript is completed and is not an instrument failure. Changing the
text instead MUST produce a new step, which MUST be checked.

In 1.1, a known writer or checker call failure pauses the branch with
`runtime_failure`, keeps the original partial output, and MUST NOT be displayed
as a scientific completion. After an explicit user resume, a new attempt is
allowed; a checker `instrument_failure` may leave the required set through
`check_retry_authorized`, which cites a real `resume_branch` HumanAction on that
route and is recorded by `system`. The original receipt and the authorizing
event are both retained, and a new check is then created. That event does not
rewrite a failure as `ok`. On recovery, an in-flight call whose provider state
is missing stays `started`/recovering: a missing handle MUST NOT be read as
remote failure and blindly retried.

Writer-facing check feedback is likewise bounded: recent opinion summaries and a
catalogue are shown, and the full opinion is read back by check id and offset,
with a catalogue mode paging older opinions. The complete feedback and evidence
are stored in this run record; truncated navigation does not change the original
opinion.

### 17.10 Verification boundary of 1.1

The 1.1 reproduction-loop test uses checkable fake outputs to exercise multiple
rounds, the checker switch, self-initiated revision, checker feedback,
conditional candidates, model blocking, recovery and version isolation. The
older runtime and record self-tests are unchanged. These tests demonstrate run
and record behaviour. They do not support any claim that a particular published
result has been reproduced.

---

## 18. Notes: where this document, the schemas and the code differ

Every item was checked against the files in this repository. Where the source
document and the code disagree, the code is authoritative and the body of this
translation follows the code.

**18.1 This file is not the hashed contract — in the Record v1 records.** Each
record's `run_created` pins three things by path and SHA-256: the contract and
both schemas. Those strings are inside the hash chain and cannot be rewritten
without destroying the record.

The runtime in this repository hashes the contract document into every record
it writes (`src/derivation_app/service.py`): a Record v1 pins
`docs/spec/DERIVATION_AGENT_RECORD_V1_cn.md`, a Record 1.1 written by the
runtime pins `docs/spec/DERIVATION_RUNTIME_RECORD_V1_1_cn.md`. Both are in
Chinese and ship untranslated, because a translation is a different byte
sequence and nothing else could hash to the pinned digest. Both are public
editions of their upstream originals, in which clauses that named unpublished
work were made generic (for the first, two items of the §15 list, plus one
sentence of §4.2 reworded; see §18.14),
so their digests differ from the originals' (`docs/spec/README.md`). The first
hashes to `9d6cfcd5…`. The two Record v1 example records cite it by path. The
deterministic fixture was regenerated against the public edition, and its pin
resolves. The real model run, `examples/runs/uniformly_charged_sphere/`, was
written against the upstream original (`290a674f…`); that digest is inside its
hash chain, so its `record_spec` pin does not resolve here and is reported as
withheld. No record in this repository pins the second document. The two v1
JSON schemas that both v1 records cite ship byte-identical —
`docs/spec/derivation_agent_event_v1.schema.json` hashes to `2d23d249…` and
`docs/spec/derivation_agent_canonical_v1.schema.json` to `f4050b21…`. Treat
§0–§16 as a faithful rendering of the first document, and the code as the
binding definition.

The synthetic Record v1.1 record was written from this translation rather than
by the runtime, so its `record_spec` pins this file itself, and its schema pins
name `src/derivation_agent_record/schemas/event-v1.1.schema.json` and
`src/derivation_agent_record/schemas/canonical-v1.1.schema.json`. This file's
own digest is therefore inside that record's hash chain: editing this document
means regenerating `examples/runs/worked_v1_1/`, which the suite forces rather
than merely asks for.

`tools/verify_record_pins.py` is the machine check behind every sentence above.
It resolves all three pins of every record under `examples/runs/` against the
files at the paths they name and fails on any mismatch or any absent file,
except the one withheld original it names by field, path and digest, which it
reports.

**18.2 Candidate status precedence.** The source document lists the late-result
rules as "any pending → provisional; any instrument failure → blocked; any hard
defect → rejected". The code evaluates hard defect **before** instrument failure
in v1, so a v1 candidate holding both is `rejected`, not `blocked`. §7.4 states
the code's order.

**18.3 A 1.1 hard defect may still kill a branch.** The 1.1 source document says
a model `hard_defect` no longer automatically kills a branch. The replay engine
does not gate the `hard_defect` branch-kill reason on the record version: a 1.1
record may legally contain `branch_status_changed(reason_code = hard_defect)` to
`killed`. The change is a producer policy, not a contract restriction.

**18.4 Evidence validation is wider than "hard defect" in 1.1.** The source
document ties the evidence whitelist to `hard_defect`. In 1.1 the replay engine
runs the same validation whenever a completed check carries a non-empty evidence
list, whatever the verdict — so an `objection` carrying a malformed quotation is
rejected too.

**18.5 `created_reason = model_revision` is not usable on `branch_created`.**
The 1.1 event schema lists `model_revision` in the `branch_created`
`created_reason` enum, but the replay engine accepts only `model_alternative`
and `instrument_retry` for a non-human child. The value exists because
`model_revision_applied` synthesizes a branch carrying that reason into canonical
state; a hand-written `branch_created` using it is rejected.

**18.6 `human_action_recorded.target` is looser in the schema than in replay.**
Both schemas allow a target object containing `judgement_id`, and allow any
combination of the five target keys. The replay engine requires exactly one
key, fixed per action: `branch_id` for pause/resume/kill/set_direction/
set_hypothesis, `step_revision_id` for `revise_step`, `model_call_id` for
`abort_model_call`, `candidate_id` for `select_candidate`. No action uses
`judgement_id`.

**18.7 The schemas do not constrain actor kinds.** Both event schemas allow all
five actor kinds on every event type. Every per-event actor rule in this
document is enforced by the replay engine only.

**18.8 `max_model_calls = null`.** The v1 schema requires an integer ≥ 1; the
1.1 schema and the replay engine allow `null` in 1.1 only, meaning no global
budget.

**18.9 `recorded_at` has no format constraint.** The schemas require a non-empty
string and the replay engine requires non-empty text. Nothing enforces RFC 3339,
UTC or monotonicity. Timestamps are recorded, not validated; `seq` and the hash
chain carry the ordering.

**18.10 The command interface has four subcommands, not two.** The source
document lists `verify` and `build`. The CLI also has `replay` (canonical JSON
only) and `render` (HTML only). §14 lists all four.

**18.11 Paths.** The package lives at `src/derivation_agent_record/`, as the
source document says, and the commands in §14 need `PYTHONPATH=src` when run
from a checkout. The 1.1 schemas ship inside the package. §14 gives the layout.

**18.12 A human cannot complete a branch.** Only `writer_complete` (model actor)
and, in 1.1, `writer_route_completion` reach `completed`, and a candidate
requires a completed branch. So `declared_by = human` is reachable only after a
model completed the route. The source document does not say this explicitly.

**18.13 `check_retry_authorized` orders events by `seq`, never by their ids.**
The replay engine establishes that the authorizing resume came after the failure
by comparing the human action's `seq` with the `seq` of the event that completed
the check. An `event_id` is a name, and a producer chooses it freely (§2.2), so
nothing may be inferred from it; there is no constraint on the id format beyond
the one §2.2 states. An earlier revision of this package derived the failure's
position by parsing the numeric tail of `completed_event_id`, which both crashed
on ids with no numeric tail and let a producer misstate the ordering by naming
an event; `tests/records/must_be_rejected/retry_predating_failure/` keeps that
case.

**18.14 Where the public edition differs from the original.** In the upstream
original of the Record v1 document, two items of the §15 list of things v1 does
not do were written in project-local shorthand. The public edition that ships
in `docs/spec/` states them generically, and this translation follows it: "any
particular scientific problem" and "a frozen task statement, judge
configuration or historical tag established before it". No normative content
changes: the clause still says v1 runs no problem of its own and modifies no
earlier frozen artefact. The edition also words the opening sentence of §4.2
as a plain normative statement; the rule it states, that a human edit never
overwrites a historical step, is unchanged and is what §4.2 of this translation
says. These two edits are the only differences between the edition and the
original, and the reason one pin of
`examples/runs/uniformly_charged_sphere/` does not resolve (§18.1).

**18.15 What the shipped golden fixture actually asserts.** §13.12 requires the
JSON, the canonical JSON and the HTML to rebuild byte for byte. In this repository
the self-test compares the rendered HTML byte for byte against
`tests/fixtures/golden_view.html`, and compares the canonical replay against
`tests/fixtures/golden_canonical.json` after parsing, which is structural
equality rather than byte equality. Byte stability of the canonical serializer
still follows from the ordering rules in §2.5 and §12, but the fixture does not
assert it directly.

**18.16 Check the version of the example you are reading.** Each record states
its own generation in every event's `schema_version`, and the two generations
differ in the ways §17 lists. Three records ship: two of generation v1 —
`examples/runs/deterministic_fixture/` from a deterministic runtime and
`examples/runs/uniformly_charged_sphere/` from a real model run — and
`examples/runs/worked_v1_1/`, a Record v1.1 built event by event
from this contract by `tests/v1_1_example.py` (no runtime produced it; see the
caption in `examples/runs/README.md`). Between them they use 21 of the 22 event
types. The exception is `source_evidence_registered`, which carries source text
inside the hash chain and therefore appears in no published record here — so the
macro-expansion replacement kind of §17.7 is stated by this document and by
`src/derivation_agent_record/macro_expansion.py`, but is exercised by no shipped
record.

**18.17 Not checked.** This translation was checked against the verifier, not
against the runtime that produces records (`src/derivation_runtime/`,
`src/derivation_app/`). Every statement in §17 about producer-side behaviour
(context windows, session re-establishment, crash recovery, the
formula-normalization rule, the reproduction-loop test) is reproduced from the
source document and is **not** enforced by the verifier. What the verifier
enforces is exactly what `src/derivation_agent_record/replay.py` checks.
