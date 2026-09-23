# Normative originals

Read [`../RECORD_SPEC.md`](../RECORD_SPEC.md). It is the English translation of
the two Chinese documents in this directory, cross-checked against the JSON
schemas and the replay engine, and it records every place where the original
text and the code disagree (§18).

The files here are the documents records pin, kept byte for byte:

| File | Language | SHA-256 | Pinned by |
|---|---|---|---|
| `DERIVATION_AGENT_RECORD_V1_cn.md` | Chinese | `9d6cfcd525ac2fe60921c01523bf29e1e81e8dd4cb290fb7e282101fc5a58a1b` | `run_created.record_spec` of every Record v1 the runtime in this repository writes, including `examples/runs/deterministic_fixture/`; `examples/runs/uniformly_charged_sphere/` pins an earlier wording instead (see below) |
| `DERIVATION_RUNTIME_RECORD_V1_1_cn.md` | Chinese | `7a1518659dc603e12d5e3a7d1c20fe5b1f1095bb2fd33d124a90ff69074865fc` | `run_created.record_spec` of every Record 1.1 the runtime in this repository writes; no record under `examples/` pins it (see below) |
| `derivation_agent_event_v1.schema.json` | JSON Schema | `2d23d2490f6bc274e6f65d48a7e06df2ab144429ab05bdc9cd6a70e2ec868d24` | `run_created.event_schema` of every Record v1 |
| `derivation_agent_canonical_v1.schema.json` | JSON Schema | `f4050b21035309adb07b5a544132223984a941c697b1d2e24cab05fbc0753625` | `run_created.canonical_schema` of every Record v1 |

The 1.1 schemas live inside the package, at
`src/derivation_agent_record/schemas/`, and are pinned the same way.

## Why the Chinese documents ship

When the runtime creates a run, `src/derivation_app/service.py` reads the
contract document by path, hashes its bytes and writes path and digest into the
first event of the record. The digest is inside the hash chain, so it can never
be changed afterwards. A reader who wants to check what contract a record was
written against has to be able to hash the same bytes, and a translation is a
different byte sequence. So the Chinese documents stay, and the translation
sits next to them.

## Earlier wordings

Both contract documents were reworded in a few clauses without changing what
they require. Their digests therefore differ from those of the earlier
wordings.

`DERIVATION_AGENT_RECORD_V1_cn.md` differs from its earlier wording in two
items of the list in §15 (lines 355 and 356), which now read "runs no
particular scientific problem" and "modifies no task statement, judge
configuration or historical tag frozen before it". It also differs in the
opening sentence of §4.2 (line 114), which now states the rule that a human
edit never overwrites a historical step as a plain normative sentence. The
earlier wording hashes to
`290a674f2f39fcd6509e5a31aa7ec8a7d069722ea4c104530335dc2d09251cfb`. A Record v1
written by the runtime in this repository pins the shipped document, and it
resolves here; so does `examples/runs/deterministic_fixture/`.
`examples/runs/uniformly_charged_sphere/` is a real model run written against
the earlier wording, and its digest is inside its hash chain, so it cannot be
re-pinned: its `record_spec` pin names this path at `290a674f…` and **does not
resolve here**. `tools/verify_record_pins.py` reports that one pin as withheld
(its `WITHHELD_ORIGINALS` table names exactly that field, path and digest) and
fails on any other mismatch. Its two schema pins resolve.

`DERIVATION_RUNTIME_RECORD_V1_1_cn.md` differs from its earlier wording in
two clauses (lines 22 and 169); the earlier wording hashes to `963acffe…`.
No record in this repository pins either digest: the Record 1.1 example,
`examples/runs/worked_v1_1/`, pins `docs/RECORD_SPEC.md`. A Record 1.1 written
by the runtime in this repository pins the shipped document, and it resolves
here.

Do not edit these files. Changing one byte changes the digest every new record
pins, and makes the pin of every existing record that cites it fail
(`python3 tools/verify_record_pins.py`). A change to the contract is a new
document with a new path, under the change control of RECORD_SPEC §16.
