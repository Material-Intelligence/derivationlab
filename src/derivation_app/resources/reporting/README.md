# Product-managed reporting runtime

PDF reporting (ReportBundle V1) and the formula compiler use one pinned
Tectonic runtime, described by `config/reporting/tectonic_runtime.lock.json`.
Product code never discovers a compiler through `PATH`, never falls back to a
system TeX installation, and never fetches TeX resources at compile time.

## The runtime is not in the repository

The Tectonic binary and its TeX bundle are third-party software with their own
licences, so they are not committed. Install them with

```bash
python3 tools/provision_tectonic.py
```

The script uses only the Python standard library. It downloads the official
Tectonic release archive, checks the archive and the binary against the lock,
reads each bundle file with the verified binary (`tectonic -X bundle cat`) or,
for the Fandol CJK fonts, from CTAN, and checks every file against
`config/reporting/tectonic_bundle_manifest.json`. It then recomputes the bundle
tree hash the way the product does and moves the verified runtime into
`src/derivation_app/resources/reporting/runtime/`, which is gitignored. It
changes nothing unless every pin matches.

- `--check` verifies an existing install without network access.
- `--from DIR` installs from a local copy laid out like the `runtime/` directory,
  for example one provisioned on another machine. It is verified the same way.

## Pinned target

Only `darwin-arm64` is pinned today:

- Tectonic `0.17.0`, release
  <https://github.com/tectonic-typesetting/tectonic/releases/tag/tectonic%400.17.0>
- release archive SHA-256 `a3f1cac7c5678f01661a92212f58480ae3b0634115d880dbc59e2953ded45667`
- binary SHA-256 `b52b5a730e2b0b33087304f7720f649603953f270a6b1c88bb031e1ae01f7f9c`
- bundle tree SHA-256 `29c5072c80211e5928d67d7a188e345d9c4b248e144cd049816a83b4d1b891ae`
  (332 files: 327 from the Tectonic default bundle v33, including the Libertinus
  fonts, and 5 Fandol files)

On macOS the compiler also runs under `/usr/bin/sandbox-exec` with network
access denied.

## When the runtime is missing

Runs, checks and the Record do not need the runtime. When the runtime is not
installed, fails verification, or the platform has no pinned target, PDF export
and formula compilation fail closed with `tectonic_runtime_unavailable`, and
the tests that need a real compiler are skipped with that reason.

Adding another platform requires a reviewed release artifact, its hashes, an
offline compile from an empty cache, and a lock-file update.

## Licences

Tectonic is MIT-licensed. The bundle files come from TeX Live packages under
their own licences; the Fandol fonts are GPL with a font exception. None of
them is redistributed by this repository.
