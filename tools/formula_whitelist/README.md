# Engine-derived formula whitelist

`generate.py` decides which TeX math commands, control symbols and math
environments the locked report compiler actually typesets, and writes
`src/derivation_runtime/formula_engine_whitelist.json`. The static formula gate
loads that file; nothing in it is hand-edited.

## Inputs (all committed, so a rerun is deterministic)

- `candidates.json`: the candidate universe. Seeded from the static whitelist
  of `derivation_runtime.formula_validation` plus the commands seen in an
  offline replay of archived Writer math and formula audits, less a few
  document-specific macro names that no engine defines (dropping them cannot
  change the output). `unsafe` lists names that are never compiled.
- `config/reporting/tectonic_runtime.lock.json`: Tectonic version, binary and
  bundle hashes.
- `REPORT_TEX_PREAMBLE` in `src/derivation_app/reporting.py`.

## Method

Each candidate is rendered into probe templates and compiled with the
production `TectonicRunner` through
`derivation_app.formula_compiler.compile_fragments`, which uses the production
document layout (each fragment inside `\[`, `{`, fragment, `}`, `\]`, so an
argument-taking command cannot swallow the closing delimiter).

| Template | Probe for `name` |
|---|---|
| `bare` | `\name` |
| `arg1` | `\name{x}` |
| `arg2` | `\name{x}{y}` |
| `space_arg` | `\name x` |
| `sub` | `\name_{x}` |
| extra (only when no basic template compiles and the name is defined) | `opt_arg` `\name[x]{y}`, `length_arg` `\name{1em}`, `delim` `\name(`, `left_pair` `\name( x \right)`, `right_pair` `\left( x \name)`, `middle_pair` `\left( x \name| y \right)`, `relation_prefix` `\name=`, `limits` `\sum\name_{x}`, `subscript_arg` `\sum_{\name{a\\b}}`, `in_array_row` `\begin{matrix} x \name y \end{matrix}`, `begin_env` `\name{matrix} x \end{matrix}`, `end_env` `\begin{matrix} x \name{matrix}` |
| environment | `env`, `env_arg` (`{2}`), `env_cols` (`{cc}`), `env_rows` |

Probes are compiled in groups; a probe passes only inside a successful
document. A probe failing in a group is recompiled alone, except an
`Undefined control sequence` reported on the probe's own line (that line holds
only the candidate). Missing glyphs count as failure (production runner rule).
Unsafe names (the committed list, the validator's `_UNSAFE_COMMANDS`,
`pdf*`/`xetex*`/`luatex*`, anything `validate_math` flags unsafe) are never
compiled and are listed in `unsafe_excluded`. Infrastructure failures are
retried twice, then the run aborts without writing.

## Output schema `formula-engine-whitelist-v1`

```json
{
  "schema_version": "formula-engine-whitelist-v1",
  "engine": {"version", "target", "binary_sha256", "bundle_sha256", "lock_sha256"},
  "preamble_sha256": "...",
  "supported": {"frac": ["arg2", "..."]},
  "control_symbols": [",", "..."],
  "environments": ["aligned", "..."],
  "unsafe_excluded": ["def", "..."]
}
```

`supported` holds letter commands (and single-letter commands) with the
template names that compiled; `control_symbols` holds single non-letter names.
`src/derivation_app/tests/test_formula_engine_whitelist.py` fails when the lock
file or the preamble no longer match the recorded hashes, so any engine or
preamble change forces regeneration.

## Run

From the repository root, pointing `--runtime-root` at a checkout whose
untracked runtime resources are provisioned (`python3 tools/provision_tectonic.py`;
the lock paths resolve there; the hashes are re-verified before every compile):

```bash
PYTHONPATH=src:src/derivation_api uv run --project src/derivation_api \
  python tools/formula_whitelist/generate.py \
  --runtime-root ../other-checkout     # or omit to use this checkout
# --check            regenerate in memory, exit 1 if the committed JSON differs
# --update-candidates merge the live static whitelist into candidates.json
# --limit N           debug on the first N commands (never commit that output)
```

A full run compiles roughly 2,000 documents (about 10 minutes with 8–9
workers on an M-series Mac).

Tests without a runtime: `tools/formula_whitelist/tests/test_generate.py`.
