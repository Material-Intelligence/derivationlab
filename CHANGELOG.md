# Changelog

This file records what changes between released versions of this repository.
The **Record contract** has its own change policy and its own compatibility
rules: see `docs/RECORD_SPEC.md` §16.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
versions follow [semantic versioning](https://semver.org/). For the
`derivationlab-record` distribution (the verifier under
`src/derivation_agent_record/`), the versioned surface is its four subcommands
and their exit codes, four names in its Python API (`load_events`,
`replay_events`, `render_html`, `ContractError`) and the canonical JSON shape.
The application packages are not published as distributions and have no
stability promise yet.

## 0.1.0 — 2026-09

First public snapshot, exported from the private development repository at
commit `f47623cb72a8f82414ead0987b361774959ec14d`. Exported files were changed
only to remove references to unpublished material, relocate evidence files,
replace fixtures from unpublished work with synthetic ones, and make refusals
explicit where this build lacks a component. The digest of every file the
shipped records pin is unchanged; the Record 1.1 contract original is a public
edition with two clauses made generic, and no shipped record pins it
(`docs/spec/README.md`). The verifier comes from a later revision than that commit (see
`docs/RECORD_SPEC.md` §18.13).

### Added

- `src/derivation_agent_record/`: a standard-library-only replay engine that
  re-derives the whole state of a run from its events and refuses any record
  whose chain or cross-event rules do not hold; a renderer that turns a
  verified record into one self-contained HTML page; a CLI with `verify`,
  `replay`, `render` and `build`. Both Record generations, v1 and v1.1.
  Supported on Python 3.9 to 3.14.
- `src/derivation_runtime/`: the derivation orchestrator, the Record writer,
  and the adapter to the Codex app-server (Codex CLI 0.147.0 exactly).
- `src/derivation_api/`: the FastAPI and SSE control plane and its
  `openapi.json`.
- `src/derivation_app/`: the product service, Problem Intake, the ChatGPT
  sign-in, the typeset layer, ReportBundle export, and the command line
  (`python -m derivation_app`, `doctor`, `dev --fake`).
- `src/derivation_web/`: the React UI, in English and Simplified Chinese.
- `docs/RECORD_SPEC.md` (the contract in English), `docs/EVENT_VOCABULARY.md`,
  `docs/ARCHITECTURE.md`, and under `docs/spec/` the two Chinese documents of
  the contract that the runtime hashes into every record, shipped untranslated
  as public editions (`docs/spec/README.md`).
- Three example records under `examples/runs/`, each with a caption saying
  what it is and is not evidence of.
- `tools/provision_tectonic.py`, which downloads and verifies the Tectonic
  runtime for PDF reports against `config/reporting/`;
  `tools/formula_whitelist/`, which regenerates the formula engine whitelist
  against a provisioned runtime; `tools/verify_record_pins.py`;
  `tools/release_check.py`, the scanner that gates what this repository may
  contain.

### Not included

The internal evaluation harness, the desktop packaging shell, literature
source packs and the vendored TeX runtime of the development repository. Runs
are closed-book; PDF export needs the provisioning step and is pinned for
`darwin-arm64` only; there is no API-key model adapter.
