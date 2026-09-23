# Security policy

## What counts as a vulnerability here

**In the verifier** (`src/derivation_agent_record/`): the interesting failure is
not code execution, it is **a record that makes a false claim and verifies
anyway**. If you can construct a record whose events do not support what its
replayed state asserts, and this verifier accepts it, that is the bug this
project cares about most. Also in scope:

- a way to make `verify` accept a record whose hash chain has been altered;
- a rendered `viewer.html` that fetches anything, executes script, or shows
  something the record does not contain;
- a crash that a record can trigger in a way that lets an unsound record pass a
  pipeline gate.

**In the application** (`src/derivation_runtime/`, `src/derivation_app/`,
`src/derivation_api/`, `src/derivation_web/`):

- any way to read the ChatGPT credential (`CODEX_HOME/auth.json` in the product
  profile) from a run, a record, a manifest, a log, the web UI or a model's tool
  call;
- a model tool call that escapes its run workspace, reaches the network, or
  reads files outside what the run's capability profile allows;
- the local server accepting a request from a non-loopback address or a foreign
  origin;
- script injection through model output rendered in the web UI.

Out of scope: denial of service from a deliberately enormous record, and anything
requiring the attacker to already control the machine running DerivationLab.

## Reporting

Email **xiejh.mail@gmail.com** with the record and the command you ran. Please
include the expected verdict. Use GitHub's private vulnerability reporting if you
prefer.

Do not attach a record containing third-party source text: a
`source_evidence_registered` event puts that text inside the hash chain, where it
cannot be removed afterwards. Reconstruct the case without it.

Expect an acknowledgement within a week. Anything that lets an unsound record
verify will be fixed and added to `tests/records/must_be_rejected/`, with
attribution unless you ask otherwise.

## Supported versions

Only the latest release. This is a 0.x snapshot; there is no backport branch.
