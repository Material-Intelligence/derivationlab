# Event vocabulary

Every event type the replay engine accepts, the payload fields its schema
requires, the actor kind the replay engine requires, and what the event means.

The **Required payload fields** column is generated from
`docs/spec/derivation_agent_event_v1.schema.json` and
`src/derivation_agent_record/schemas/event-v1.1.schema.json`. Both schemas set
`additionalProperties: false` on every payload, so the required list is also the
complete list: a payload with any other key is rejected. A field marked *(1.1)*
is required in Record 1.1 and absent in Record v1.

The **Actor** column is not in the JSON schemas. Both schemas allow any of the
five actor kinds on any event; the per-event constraint is enforced by the replay
engine (`src/derivation_agent_record/replay.py`). Full rules are in
[`RECORD_SPEC.md`](./RECORD_SPEC.md).

Sixteen types are common to both versions. Six exist only in Record 1.1, and a
v1 record carrying one of them is rejected.

| Event type | Versions | Actor | Required payload fields | Meaning |
|---|---|---|---|---|
| `run_created` | v1, v1.1 | system | `canonical_schema`, `code_commit`, `configuration`, `event_schema`, `input_policy`, `pack`, `record_spec`, `task` | Opens the record and freezes the contract documents, code commit, task, pack, model configuration, backend and input boundary. |
| `human_action_recorded` | v1, v1.1 | human | `action`, `action_id`, `content`, `content_sha256`, `reason`, `target` | Records a human intent with its exact target and hashed content, before the state change it authorises. |
| `branch_created` | v1, v1.1 | system / model / human (by created_reason) | `anchor_step_revision_id`, `branch_id`, `created_reason`, `fork_mode`, `human_action_id`, `hypothesis`, `inherited_step_revision_ids`, `initial_status`, `parent_branch_id` | Starts a lineage: a root, or a child inheriting an exact prefix of its parent transcript. |
| `branch_status_changed` | v1, v1.1 | by reason_code | `branch_id`, `check_id`, `from_status`, `human_action_id`, `model_call_id`, `reason_code`, `to_status` | Moves one branch between active, paused, parked, completed and killed, citing what caused the move. |
| `model_call_started` | v1, v1.1 | model / checker / judge (by role) | `effort`, `model`, `model_call_id`, `prompt_sha256`, `provider`, `role`, `target` | Opens one writer, checker or judge call against the run's frozen configuration for that role. |
| `model_call_chunk` | v1, v1.1 | same kind as the call | `channel`, `index`, `model_call_id`, `text`, `text_sha256` | Appends one hashed streamed fragment on the analysis, body or raw channel. |
| `model_call_finished` | v1, v1.1 | same kind as the call | `body_chars`, `finish_reason`, `model_call_id`, `output_sha256`, `output_text`, `usage` | Closes a call with its complete output text, output hash, character count, finish reason and usage. |
| `model_call_failed` | v1, v1.1 | same kind as the call | `body_chars`, `failure_kind`, `message`, `model_call_id`, `partial_output_sha256`, `partial_output_text`, `retryable` | Closes a call that failed, keeping the partial output, its hash, the failure kind and whether it is retryable. |
| `model_call_aborted` | v1, v1.1 | same kind as the call | `body_chars`, `human_action_id`, `model_call_id`, `partial_output_sha256`, `partial_output_text` | Closes a call a human stopped, citing the abort_model_call action and keeping the partial output. |
| `step_revision_sealed` | v1, v1.1 | model or human (by origin.kind) | `branch_id`, `content`, `origin`, `output_sha256`, `replaces_step_revision_id`, `revision`, `step_revision_id`, `step_slot` | Seals one immutable five-field step into the next slot of an active branch. |
| `check_requested` | v1, v1.1 | system | `check_id`, `reason`, `required_for_candidate`, `target_output_sha256`, `target_step_revision_id` | Opens a step-level check bound to one step revision and its exact output hash. |
| `check_completed` | v1, v1.1 | checker | `check_id`, `checker_call_id`, `evidence`, `reason`, `target_output_sha256`, `target_step_revision_id`, `verdict` | Closes a check with a verdict, a reason, the checker call, and quoted evidence for any hard defect. |
| `candidate_declared` | v1, v1.1 | model (writer) or human | `branch_id`, `candidate_id`, `declared_by`, `reason`, `required_check_ids`, `tip_step_revision_id`, `transcript_sha256`, `transcript_step_revision_ids`, `unresolved_check_ids` *(1.1)* | Freezes the complete transcript of a completed branch as an immutable candidate with its required checks. |
| `judgement_requested` | v1, v1.1 | system or human | `candidate_id`, `candidate_transcript_sha256`, `judgement_id`, `reason`, `requested_by` | Opens a final judgement bound to one candidate and its transcript hash. |
| `judgement_completed` | v1, v1.1 | judge | `candidate_id`, `candidate_transcript_sha256`, `judge_call_id`, `judgement_id`, `reason`, `score`, `verdict` | Closes a judgement with pass, near_pass, fail or instrument_failure, a reason and an optional score. |
| `selection_recorded` | v1, v1.1 | human or system | `candidate_id`, `candidate_transcript_sha256`, `human_action_id`, `judgement_id`, `reason`, `selection_id` | Records the one selected submission of the run: an eligible candidate with a completed passing judgement. |
| `model_revision_applied` | v1.1 | model | `branch_id`, `content`, `model_call_id`, `parent_branch_id`, `reason`, `step_revision_id`, `target_step_revision_id` | Atomically replaces one earlier step: forks a replace branch, seals the new revision and parks the parent. |
| `model_revision_deferred` | v1.1 | system | `model_call_id`, `reason`, `target_step_revision_id` | Records that a requested model revision was refused by policy; the call is kept and no step is sealed. |
| `source_evidence_registered` | v1.1 | system | `kind`, `sha256`, `source_id`, `text` | Freezes one literature snapshot with its hash before any model call, so later quotations can be rechecked. |
| `check_retry_authorized` | v1.1 | system | `check_id`, `human_action_id` | Drops an instrument-failed check from the required set, citing the human resume that authorised the retry. |
| `writer_route_completion` | v1.1 | model | `branch_id`, `model_call_id`, `tip_step_revision_id` | Completes the current route unchanged, on the writer's explicit decision, without creating a new step. |
| `writer_output_normalized` | v1.1 | system | `content`, `model_call_id`, `normalizer_version`, `output_sha256`, `policy`, `raw_output_sha256`, `replacements` | Records a deterministic host-format repair of one writer output; the raw call stays immutable. |

## Envelope

Every event, of every type, carries exactly these ten keys and no others:

| Field | Meaning |
|---|---|
| `schema_version` | `derivation-agent-event-v1` or `derivation-agent-event-v1.1`; constant within a run |
| `run_id` | identity of the run; constant within a record |
| `seq` | 1-based position; contiguous, no gaps |
| `event_id` | unique within the run |
| `recorded_at` | timestamp string |
| `type` | one of the types above |
| `actor` | `{kind, id}`; kind is `system`, `model`, `checker`, `judge` or `human` |
| `prev_event_sha256` | `event_sha256` of the previous event; `null` only on the first |
| `event_sha256` | SHA-256 of the canonical JSON of this event with `event_sha256` removed |
| `payload` | the type-specific object in the table above |

Generated from the schemas; regenerate rather than edit by hand.
