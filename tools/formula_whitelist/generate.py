"""Generate the engine-derived math command whitelist.

Every candidate in ``candidates.json`` (never the live static whitelist, so a
rerun is deterministic) is probed with the production ``TectonicRunner`` at the
locked Tectonic runtime and ``REPORT_TEX_PREAMBLE``, using the production
formula document layout (``derivation_app.formula_compiler.formula_document``:
each probe inside ``\\[ { ... } \\]``). A command is supported when at least one
probe template compiles; extra templates are tried only for defined commands
whose basic templates all fail. Unsafe names are never compiled.

Output: ``src/derivation_runtime/formula_engine_whitelist.json`` (schema
``formula-engine-whitelist-v1``), bound to the lock file and preamble hashes.

Run from the repository root (see README.md):
    export PATH=/opt/homebrew/bin:$PATH
    PYTHONPATH=src:src/derivation_api uv run --project src/derivation_api \\
        python tools/formula_whitelist/generate.py --runtime-root <repo with runtime>
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
for _path in (REPO / "src", REPO / "src" / "derivation_api"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from derivation_app.formula_compiler import (
    FragmentCompileResult,
    compile_fragments,
    formula_document,
)
from derivation_app.reporting import (
    REPORT_TEX_PREAMBLE,
    TectonicRunner,
    TectonicRuntimeSpec,
)
from derivation_runtime import formula_validation as fv

SCHEMA_VERSION = "formula-engine-whitelist-v1"
CANDIDATES_SCHEMA_VERSION = "formula-whitelist-candidates-v1"
CANDIDATES = HERE / "candidates.json"
DEFAULT_OUT = REPO / "src" / "derivation_runtime" / "formula_engine_whitelist.json"
DEFAULT_LOCK = REPO / "config" / "reporting" / "tectonic_runtime.lock.json"

# Template names are the values stored under ``supported``; ``{c}`` is the name.
BASIC_TEMPLATES = {
    "bare": "\\{c}",
    "arg1": "\\{c}{{x}}",
    "arg2": "\\{c}{{x}}{{y}}",
    "space_arg": "\\{c} x",
    "sub": "\\{c}_{{x}}",
}
EXTRA_TEMPLATES = {
    "opt_arg": "\\{c}[x]{{y}}",
    "length_arg": "\\{c}{{1em}}",
    "delim": "\\{c}(",
    "left_pair": "\\{c}( x \\right)",
    "right_pair": "\\left( x \\{c})",
    "middle_pair": "\\left( x \\{c}| y \\right)",
    "relation_prefix": "\\{c}=",
    "limits": "\\sum\\{c}_{{x}}",
    "subscript_arg": "\\sum_{{\\{c}{{a\\\\b}}}}",
    "in_array_row": "\\begin{{matrix}} x \\{c} y \\end{{matrix}}",
    "begin_env": "\\{c}{{matrix}} x \\end{{matrix}}",
    "end_env": "\\begin{{matrix}} x \\{c}{{matrix}}",
}
ENV_TEMPLATES = {
    "env": "\\begin{{{c}}} x & y \\\\ z & w \\end{{{c}}}",
    "env_arg": "\\begin{{{c}}}{{2}} x & y \\\\ z & w \\end{{{c}}}",
    "env_cols": "\\begin{{{c}}}{{cc}} x & y \\\\ z & w \\end{{{c}}}",
    "env_rows": "\\begin{{{c}}} x \\\\ y \\end{{{c}}}",
}
UNSAFE_PREFIXES = ("pdf", "xetex", "luatex")


class InfrastructureError(RuntimeError):
    pass


def render(template: str, name: str) -> str:
    return template.format(c=name)


def is_control_symbol(name: str) -> bool:
    return len(name) == 1 and not name.isalpha()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def preamble_sha256() -> str:
    return sha256_bytes(REPORT_TEX_PREAMBLE.encode("utf-8"))


def load_candidates(path: Path = CANDIDATES) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("schema_version") != CANDIDATES_SCHEMA_VERSION:
        raise ValueError(f"unsupported candidates schema in {path}")
    for key in ("commands", "environments", "unsafe"):
        items = value.get(key)
        if not isinstance(items, list) or not all(isinstance(i, str) and i for i in items):
            raise ValueError(f"candidates.{key} must be a list of names")
    return value


def unsafe_names(names: Iterable[str], listed: Iterable[str]) -> set[str]:
    """Names that must never be compiled or marked supported.

    Union of the committed unsafe list, the validator's current unsafe list,
    engine primitive prefixes, and anything ``validate_math`` flags unsafe, so
    a safety change on either side can only widen the exclusion.
    """
    listed_set = set(listed) | set(getattr(fv, "_UNSAFE_COMMANDS", ()))
    unsafe = set()
    for name in set(names) | listed_set:
        if (
            name in listed_set
            or name.lower().startswith(UNSAFE_PREFIXES)
            or any(d.code == "unsafe_command" for d in fv.validate_math("\\" + name))
        ):
            unsafe.add(name)
    return unsafe


@dataclass
class Probe:
    key: str  # "cmd:<name>" or "env:<name>"
    name: str
    template: str
    tex: str
    status: str = "pending"  # passed | failed
    evidence: str = ""
    message: str = ""


def failure_value_line(
    result: FragmentCompileResult, index: int, line: int | None, values: Sequence[str]
) -> bool:
    """Whether a failure line is the probe's own (single) content line."""
    failure = next((f for f in result.failures if f.index == index), None)
    if failure is None or failure.compile_number is None or line is None:
        return False
    compiled = result.compiles[failure.compile_number - 1].fragments
    _tex, regions = formula_document([values[i] for i in compiled])
    first, _last = regions[compiled.index(index)]
    return line == first + 2


class Prober:
    def __init__(
        self,
        runner: TectonicRunner,
        workspace: Path,
        *,
        workers: int,
        group_size: int,
        retries: int = 2,
        compile_fn: Callable[..., FragmentCompileResult] = compile_fragments,
    ) -> None:
        self.runner = runner
        self.workspace = workspace
        workspace.mkdir(parents=True, exist_ok=True)
        self.workers = workers
        self.group_size = group_size
        self.retries = retries
        self.compile_fn = compile_fn
        self.compiles = 0
        self.compile_seconds = 0.0
        self._lock = threading.Lock()

    def compile(self, values: Sequence[str]) -> FragmentCompileResult:
        for _attempt in range(self.retries + 1):
            directory = Path(tempfile.mkdtemp(prefix="probe-", dir=self.workspace))
            result = self.compile_fn(
                list(values), self.runner, directory, label="formula-engine-whitelist"
            )
            with self._lock:
                self.compiles += len(result.compiles)
                self.compile_seconds += sum(result.durations)
            if result.infrastructure_error is None:
                return result
        raise InfrastructureError(result.infrastructure_error)

    def run_group(self, group: list[Probe]) -> list[Probe]:
        """Mark passing/attributed probes; return probes needing isolation."""
        values = [probe.tex for probe in group]
        result = self.compile(values)
        failures = {failure.index: failure for failure in result.failures}
        isolate: list[Probe] = []
        for index, probe in enumerate(group):
            if index in result.accepted:
                probe.status, probe.evidence = "passed", "group_document_success"
                continue
            failure = failures.get(index)
            if (
                failure is not None
                and failure.kind == "tex_error"
                and failure.message.startswith("Undefined control sequence")
                and failure_value_line(result, index, failure.line, values)
            ):
                # The probe line holds only the candidate plus letters and
                # braces, so an undefined control sequence there is that name.
                probe.status = "failed"
                probe.evidence = "group_undefined_on_probe_line"
                probe.message = failure.message[:240]
            else:
                isolate.append(probe)
        return isolate

    def isolate(self, probe: Probe) -> None:
        result = self.compile([probe.tex])
        if result.passed:
            probe.status, probe.evidence = "passed", "isolation_document_success"
            return
        probe.status = "failed"
        probe.evidence = "isolation_failure"
        probe.message = result.failures[0].message[:240] if result.failures else ""

    def run(self, probes: list[Probe]) -> None:
        groups = [
            probes[i : i + self.group_size]
            for i in range(0, len(probes), self.group_size)
        ]
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            pending = [p for items in pool.map(self.run_group, groups) for p in items]
            list(pool.map(self.isolate, pending))


def build_whitelist(
    *,
    candidates: dict[str, Any],
    spec: TectonicRuntimeSpec,
    lock_bytes: bytes,
    prober: Prober,
    limit: int = 0,
) -> tuple[dict[str, Any], dict[str, Any]]:
    unsafe = unsafe_names(candidates["commands"], candidates["unsafe"])
    names = sorted(set(candidates["commands"]) - unsafe)
    if limit:
        names = names[:limit]
    environments = sorted(set(candidates["environments"]))

    probes: list[Probe] = [
        Probe(f"cmd:{name}", name, template_name, render(template, name))
        for template_name, template in BASIC_TEMPLATES.items()
        for name in names
    ] + [
        Probe(f"env:{name}", name, template_name, render(template, name))
        for template_name, template in ENV_TEMPLATES.items()
        for name in environments
    ]
    prober.run(probes)
    passed_keys = {p.key for p in probes if p.status == "passed"}
    undefined = {
        name
        for name in names
        if f"cmd:{name}" not in passed_keys
        and all(
            p.message.startswith("Undefined control sequence")
            for p in probes
            if p.key == f"cmd:{name}"
        )
    }
    # Every extra template contains the bare \name token, so an undefined name
    # cannot pass any of them.
    extra_names = [
        n for n in names if f"cmd:{n}" not in passed_keys and n not in undefined
    ]
    extra = [
        Probe(f"cmd:{name}", name, template_name, render(template, name))
        for template_name, template in EXTRA_TEMPLATES.items()
        for name in extra_names
    ]
    prober.run(extra)
    all_probes = probes + extra
    if any(p.status not in {"passed", "failed"} for p in all_probes):
        raise InfrastructureError("probe left undecided")

    order = {name: i for i, name in enumerate([*BASIC_TEMPLATES, *EXTRA_TEMPLATES])}
    templates: dict[str, list[str]] = {}
    for probe in all_probes:
        if probe.status == "passed" and probe.key.startswith("cmd:"):
            templates.setdefault(probe.name, []).append(probe.template)
    supported = {
        name: sorted(templates[name], key=order.__getitem__)
        for name in sorted(templates)
        if not is_control_symbol(name)
    }
    control_symbols = sorted(name for name in templates if is_control_symbol(name))
    env_supported = sorted(
        {p.name for p in all_probes if p.status == "passed" and p.key.startswith("env:")}
    )
    if (set(supported) | set(control_symbols)) & unsafe:
        raise AssertionError("unsafe command marked supported")
    output = {
        "schema_version": SCHEMA_VERSION,
        "engine": {
            "version": spec.version,
            "target": spec.target,
            "binary_sha256": spec.binary_sha256,
            "bundle_sha256": spec.bundle_sha256,
            "lock_sha256": sha256_bytes(lock_bytes),
        },
        "preamble_sha256": preamble_sha256(),
        "supported": supported,
        "control_symbols": control_symbols,
        "environments": env_supported,
        "unsafe_excluded": sorted(unsafe),
    }
    unsupported = sorted(set(names) - set(templates))
    report = {
        "candidates": len(names),
        "environments_probed": len(environments),
        "supported_commands": len(supported),
        "control_symbols": len(control_symbols),
        "environments_supported": len(env_supported),
        "unsupported": {
            name: (
                "undefined_control_sequence"
                if name in undefined
                else "defined_but_no_template_compiles"
            )
            for name in unsupported
        },
        "unsafe_excluded": len(unsafe),
        "probes": len(all_probes),
        "compiles": prober.compiles,
        "compile_seconds": round(prober.compile_seconds, 1),
    }
    return output, report


def serialize(output: dict[str, Any]) -> str:
    return json.dumps(output, indent=2, ensure_ascii=False) + "\n"


def update_candidates(path: Path = CANDIDATES) -> dict[str, Any]:
    """Union the live static whitelist into the committed candidate list."""
    value = load_candidates(path)
    commands = set(value["commands"]) | set(fv._ALLOWED_MATH_COMMANDS)
    commands |= set(fv._ALLOWED_MATH_CONTROL_SYMBOLS)
    environments = set(value["environments"]) | set(fv._SUPPORTED_MATH_ENVIRONMENTS)
    unsafe = set(value["unsafe"]) | set(getattr(fv, "_UNSAFE_COMMANDS", ()))
    value.update(
        commands=sorted(commands - unsafe),
        environments=sorted(environments),
        unsafe=sorted(unsafe),
    )
    path.write_text(serialize(value), encoding="utf-8")
    return value


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--runtime-root",
        type=Path,
        default=REPO,
        help="repository root whose provisioned runtime the lock paths resolve in",
    )
    parser.add_argument("--lock", type=Path, default=DEFAULT_LOCK)
    parser.add_argument("--candidates", type=Path, default=CANDIDATES)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--group-size", type=int, default=24)
    parser.add_argument("--limit", type=int, default=0, help="debug: first N commands")
    parser.add_argument(
        "--check",
        action="store_true",
        help="regenerate in memory; exit 1 if --out differs",
    )
    parser.add_argument(
        "--update-candidates",
        action="store_true",
        help="merge the live static whitelist into candidates.json and exit",
    )
    parser.add_argument("--keep-evidence", type=Path, default=None)
    args = parser.parse_args(argv)

    if args.update_candidates:
        value = update_candidates(args.candidates)
        print(f"candidates: {len(value['commands'])} commands, "
              f"{len(value['environments'])} environments")
        return 0

    candidates = load_candidates(args.candidates)
    lock_bytes = args.lock.read_bytes()
    spec = TectonicRuntimeSpec.from_lock(args.runtime_root, lock_path=args.lock)
    runner = TectonicRunner(spec)
    artifact_error = runner._artifact_error()
    if artifact_error:
        print(f"runtime unavailable: {artifact_error}", file=sys.stderr)
        return 2
    workspace = Path(tempfile.mkdtemp(prefix="formula-whitelist-"))
    started = time.monotonic()
    try:
        prober = Prober(
            runner, workspace, workers=args.workers, group_size=args.group_size
        )
        baseline = prober.compile(["x"])
        if not baseline.passed:
            print("baseline document failed to compile", file=sys.stderr)
            return 3
        output, report = build_whitelist(
            candidates=candidates,
            spec=spec,
            lock_bytes=lock_bytes,
            prober=prober,
            limit=args.limit,
        )
    except InfrastructureError as exc:
        print(f"infrastructure failure: {exc}", file=sys.stderr)
        return 4
    finally:
        if args.keep_evidence:
            shutil.copytree(workspace, args.keep_evidence, dirs_exist_ok=True)
        shutil.rmtree(workspace, ignore_errors=True)
    report["wall_seconds"] = round(time.monotonic() - started, 1)
    text = serialize(output)
    if args.check:
        current = args.out.read_text(encoding="utf-8") if args.out.exists() else ""
        print(json.dumps(report, indent=2), file=sys.stderr)
        if current != text:
            print(f"{args.out} is stale; rerun without --check", file=sys.stderr)
            return 1
        return 0
    args.out.write_text(text, encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
