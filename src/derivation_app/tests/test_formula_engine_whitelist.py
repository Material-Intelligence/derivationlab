"""The committed engine whitelist must match the locked engine and preamble."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from derivation_app.reporting import REPORT_TEX_PREAMBLE, TectonicRuntimeSpec
from derivation_runtime.formula_validation import validate_math

ROOT = Path(__file__).resolve().parents[3]
WHITELIST = ROOT / "src" / "derivation_runtime" / "formula_engine_whitelist.json"
LOCK = ROOT / "config" / "reporting" / "tectonic_runtime.lock.json"
REGENERATE = (
    "engine or preamble changed: regenerate with tools/formula_whitelist/generate.py "
    "(see tools/formula_whitelist/README.md)"
)
# Never allowed, whatever the engine accepts: file/stream I/O, definitions,
# catcode or expansion tricks, engine primitives, and job control.
NEVER_SUPPORTED = {
    "input", "include", "openin", "openout", "read", "write", "immediate",
    "special", "catcode", "csname", "endcsname", "def", "edef", "gdef", "xdef",
    "let", "futurelet", "newcommand", "renewcommand", "providecommand",
    "newenvironment", "renewenvironment", "usepackage", "documentclass", "font",
    "hbox", "vbox", "shipout", "loop", "repeat", "directlua", "luaexec",
    "scantokens", "everyjob", "everymath", "everydisplay", "global",
    "expandafter", "noexpand", "stop", "endinput", "enddocument", "dump",
    "batchmode", "nonstopmode", "scrollmode", "errorstopmode",
}  # fmt: skip


def whitelist() -> dict:
    return json.loads(WHITELIST.read_text(encoding="utf-8"))


def test_schema_is_exact():
    value = whitelist()
    assert list(value) == [
        "schema_version",
        "engine",
        "preamble_sha256",
        "supported",
        "control_symbols",
        "environments",
        "unsafe_excluded",
    ]
    assert value["schema_version"] == "formula-engine-whitelist-v1"
    assert set(value["engine"]) == {
        "version",
        "target",
        "binary_sha256",
        "bundle_sha256",
        "lock_sha256",
    }
    assert all(
        isinstance(templates, list) and templates
        for templates in value["supported"].values()
    )
    assert all(len(name) == 1 and not name.isalpha() for name in value["control_symbols"])
    assert value["supported"] and value["environments"]


def test_whitelist_is_bound_to_current_lock_and_preamble():
    value = whitelist()
    assert value["engine"]["lock_sha256"] == hashlib.sha256(LOCK.read_bytes()).hexdigest(), REGENERATE
    assert (
        value["preamble_sha256"]
        == hashlib.sha256(REPORT_TEX_PREAMBLE.encode("utf-8")).hexdigest()
    ), REGENERATE
    lock = json.loads(LOCK.read_text(encoding="utf-8"))
    assert value["engine"]["version"] == lock["tectonic_version"], REGENERATE
    assert value["engine"]["bundle_sha256"] == lock["bundle"]["sha256"], REGENERATE
    target = lock["targets"][value["engine"]["target"]]
    assert value["engine"]["binary_sha256"] == target["binary"]["sha256"], REGENERATE
    spec = TectonicRuntimeSpec.from_lock(ROOT)
    assert spec.version == value["engine"]["version"]


def test_no_unsafe_command_is_supported():
    value = whitelist()
    names = set(value["supported"]) | set(value["control_symbols"])
    unsafe = set(value["unsafe_excluded"])
    assert unsafe >= NEVER_SUPPORTED
    assert not names & unsafe
    assert not names & NEVER_SUPPORTED
    assert not any(n.lower().startswith(("pdf", "xetex", "luatex")) for n in names)
    for name in names:
        assert not [
            d for d in validate_math("\\" + name) if d.code == "unsafe_command"
        ], name
