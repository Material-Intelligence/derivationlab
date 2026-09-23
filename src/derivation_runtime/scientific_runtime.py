"""Explicit provisioning for the immutable scientific-compute runtime.

Provisioning is a setup/release action. Model turns never run uv, install
packages, read its cache, or need package-network access.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

RUNTIME_ID = "sympy_uv"
RUNTIME_SCHEMA = "derivation-scientific-runtime-v1"
PYTHON_VERSION = "3.13.11"
PACKAGE_VERSIONS = {"mpmath": "1.3.0", "sympy": "1.14.0"}
REQUIREMENTS_LOCK = Path(__file__).with_name("scientific_runtime_requirements.lock")

SCIENTIFIC_TOOL_NAME = "scientific_compute"
SCIENTIFIC_TOOL_SPEC: dict[str, Any] = {
    "type": "function",
    "name": SCIENTIFIC_TOOL_NAME,
    "description": (
        "Perform one bounded symbolic operation with the pinned SymPy runtime. "
        "This tool accepts mathematical expressions, not Python code."
    ),
    "inputSchema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["operation", "expression"],
        "properties": {
            "operation": {"enum": ["differentiate", "simplify", "expand", "factor"]},
            "expression": {"type": "string", "minLength": 1, "maxLength": 512},
            "variable": {
                "type": "string",
                "pattern": "^[A-Za-z][A-Za-z0-9_]{0,31}$",
            },
            "order": {"type": "integer", "minimum": 1, "maximum": 4},
        },
    },
}

_IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,31}$")
_MAX_ARGUMENT_BYTES = 2_048
_MAX_EXPRESSION_CHARS = 512
_MAX_OUTPUT_CHARS = 4_096

# Fixed worker source. The model supplies only the JSON request on stdin.  The
# worker never evals input and cannot select code, modules, paths, commands, or
# network destinations.
_SCIENTIFIC_WORKER = r"""
import ast
import json
import math
import re
import sys

import sympy

MAX_NODES = 128
MAX_OUTPUT = 4096
IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,31}$")
FUNCTIONS = {
    "sin": sympy.sin,
    "cos": sympy.cos,
    "tan": sympy.tan,
    "asin": sympy.asin,
    "acos": sympy.acos,
    "atan": sympy.atan,
    "sinh": sympy.sinh,
    "cosh": sympy.cosh,
    "tanh": sympy.tanh,
    "exp": sympy.exp,
    "log": sympy.log,
    "sqrt": sympy.sqrt,
    "Abs": sympy.Abs,
}
CONSTANTS = {"pi": sympy.pi, "E": sympy.E, "I": sympy.I}


class InputError(Exception):
    pass


def fail(code):
    print(json.dumps({"ok": False, "error": code}, separators=(",", ":")))
    raise SystemExit(0)


def parse_expression(source):
    try:
        root = ast.parse(source, mode="eval")
    except (SyntaxError, ValueError):
        raise InputError("invalid_expression")
    if sum(1 for _ in ast.walk(root)) > MAX_NODES:
        raise InputError("expression_too_complex")
    symbols = {}

    def convert(node):
        if isinstance(node, ast.Expression):
            return convert(node.body)
        if isinstance(node, ast.Constant):
            value = node.value
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise InputError("unsupported_literal")
            if isinstance(value, int) and len(str(abs(value))) > 32:
                raise InputError("integer_too_large")
            if isinstance(value, float) and not math.isfinite(value):
                raise InputError("non_finite_number")
            return sympy.Integer(value) if isinstance(value, int) else sympy.Float(value)
        if isinstance(node, ast.Name):
            name = node.id
            if name in CONSTANTS:
                return CONSTANTS[name]
            if not IDENTIFIER.fullmatch(name) or name.startswith("_"):
                raise InputError("invalid_symbol")
            if len(symbols) >= 12 and name not in symbols:
                raise InputError("too_many_symbols")
            return symbols.setdefault(name, sympy.Symbol(name))
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            value = convert(node.operand)
            return value if isinstance(node.op, ast.UAdd) else -value
        if isinstance(node, ast.BinOp):
            left = convert(node.left)
            right = convert(node.right)
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            if isinstance(node.op, ast.Mult):
                return left * right
            if isinstance(node.op, ast.Div):
                return left / right
            if isinstance(node.op, ast.Pow):
                if (
                    not isinstance(node.right, ast.Constant)
                    or isinstance(node.right.value, bool)
                    or not isinstance(node.right.value, int)
                    or not -16 <= node.right.value <= 16
                ):
                    raise InputError("unsupported_exponent")
                return left ** right
            raise InputError("unsupported_operator")
        if isinstance(node, ast.Call):
            if (
                not isinstance(node.func, ast.Name)
                or node.func.id not in FUNCTIONS
                or len(node.args) != 1
                or node.keywords
            ):
                raise InputError("unsupported_function")
            return FUNCTIONS[node.func.id](convert(node.args[0]))
        raise InputError("unsupported_syntax")

    return convert(root), symbols


try:
    payload = json.loads(sys.stdin.read())
    if not isinstance(payload, dict) or set(payload) - {
        "operation", "expression", "variable", "order"
    }:
        raise InputError("invalid_arguments")
    operation = payload.get("operation")
    expression = payload.get("expression")
    if operation not in {"differentiate", "simplify", "expand", "factor"}:
        raise InputError("invalid_operation")
    if not isinstance(expression, str) or not expression or len(expression) > 512:
        raise InputError("invalid_expression")
    parsed, symbols = parse_expression(expression)
    variable = payload.get("variable")
    order = payload.get("order", 1)
    if operation == "differentiate":
        if (
            not isinstance(variable, str)
            or not IDENTIFIER.fullmatch(variable)
            or variable.startswith("_")
            or isinstance(order, bool)
            or not isinstance(order, int)
            or not 1 <= order <= 4
        ):
            raise InputError("invalid_differentiation_arguments")
        symbol = symbols.get(variable, sympy.Symbol(variable))
        result = sympy.diff(parsed, symbol, order)
    else:
        if variable is not None or "order" in payload:
            raise InputError("arguments_not_applicable")
        functions = {
            "simplify": sympy.simplify,
            "expand": sympy.expand,
            "factor": sympy.factor,
        }
        result = functions[operation](parsed)
    rendered = str(result)
    if len(rendered) > MAX_OUTPUT:
        raise InputError("output_too_large")
    print(
        json.dumps(
            {"ok": True, "operation": operation, "result": rendered},
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )
except InputError as exc:
    fail(str(exc))
except Exception:
    fail("scientific_runtime_error")
"""


class ScientificRuntimeError(RuntimeError):
    """The private scientific runtime is absent, invalid, or failed setup."""


@dataclass(frozen=True)
class ScientificRuntimeValidation:
    runtime_id: str
    root: Path
    python: Path
    bin_directory: Path
    manifest_path: Path
    requirements_sha256: str


@dataclass(frozen=True)
class ScientificCalculatorConfig:
    timeout_seconds: float = 2.0
    max_argument_bytes: int = _MAX_ARGUMENT_BYTES
    max_output_chars: int = _MAX_OUTPUT_CHARS

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0:
            raise ValueError("scientific tool timeout must be positive")
        if self.max_argument_bytes <= 0 or self.max_output_chars <= 0:
            raise ValueError("scientific tool byte limits must be positive")


_DEFAULT_SCIENTIFIC_CALCULATOR_CONFIG = ScientificCalculatorConfig()


class ScientificCalculatorTool:
    """One fixed, bounded SymPy dynamic tool backed by the pinned runtime."""

    name = SCIENTIFIC_TOOL_NAME
    spec = SCIENTIFIC_TOOL_SPEC

    def __init__(
        self,
        runtime: ScientificRuntimeValidation,
        *,
        config: ScientificCalculatorConfig = _DEFAULT_SCIENTIFIC_CALCULATOR_CONFIG,
    ) -> None:
        validated = validate_scientific_runtime(runtime.root)
        if validated != runtime:
            raise ScientificRuntimeError(
                "scientific calculator runtime differs from its validated pin"
            )
        self._runtime = runtime
        self._config = config

    async def invoke(self, arguments: object) -> dict[str, Any]:
        error = self._validate_arguments(arguments)
        if error is not None:
            return self._failure(error)
        assert isinstance(arguments, Mapping)
        encoded = json.dumps(
            dict(arguments),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        if len(encoded) > self._config.max_argument_bytes:
            return self._failure("arguments_too_large")
        environment = {
            "PYTHONHASHSEED": "0",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        for key in ("SYSTEMROOT", "WINDIR"):
            value = os.environ.get(key)
            if value is not None:
                environment[key] = value
        process = await asyncio.create_subprocess_exec(
            str(self._runtime.python),
            "-I",
            "-B",
            "-c",
            _SCIENTIFIC_WORKER,
            cwd=self._runtime.root,
            env=environment,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, _stderr = await asyncio.wait_for(
                process.communicate(encoded), self._config.timeout_seconds
            )
        except asyncio.CancelledError:
            if process.returncode is None:
                process.kill()
                await process.wait()
            raise
        except TimeoutError:
            process.kill()
            await process.wait()
            return self._failure("scientific_runtime_timeout")
        if process.returncode != 0 or len(stdout) > self._config.max_output_chars + 512:
            return self._failure("scientific_runtime_error")
        try:
            value = json.loads(stdout.decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return self._failure("scientific_runtime_error")
        if not isinstance(value, dict) or value.get("ok") is not True:
            code = value.get("error") if isinstance(value, dict) else None
            return self._failure(
                code
                if isinstance(code, str) and len(code) <= 64
                else "invalid_arguments"
            )
        if set(value) != {"ok", "operation", "result"}:
            return self._failure("scientific_runtime_error")
        operation = value.get("operation")
        result = value.get("result")
        if (
            operation not in {"differentiate", "simplify", "expand", "factor"}
            or not isinstance(result, str)
            or len(result) > self._config.max_output_chars
        ):
            return self._failure("scientific_runtime_error")
        text = json.dumps(
            {"operation": operation, "result": result},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return {"contentItems": [{"type": "inputText", "text": text}], "success": True}

    @staticmethod
    def _validate_arguments(arguments: object) -> str | None:
        if not isinstance(arguments, Mapping):
            return "invalid_arguments"
        keys = set(arguments)
        if keys - {"operation", "expression", "variable", "order"}:
            return "invalid_arguments"
        operation = arguments.get("operation")
        expression = arguments.get("expression")
        if operation not in {"differentiate", "simplify", "expand", "factor"}:
            return "invalid_operation"
        if (
            not isinstance(expression, str)
            or not expression
            or len(expression) > _MAX_EXPRESSION_CHARS
        ):
            return "invalid_expression"
        if operation == "differentiate":
            variable = arguments.get("variable")
            order = arguments.get("order", 1)
            if (
                not isinstance(variable, str)
                or not _IDENTIFIER.fullmatch(variable)
                or variable.startswith("_")
                or isinstance(order, bool)
                or not isinstance(order, int)
                or not 1 <= order <= 4
            ):
                return "invalid_differentiation_arguments"
        elif arguments.get("variable") is not None or "order" in arguments:
            return "arguments_not_applicable"
        return None

    @staticmethod
    def _failure(code: str) -> dict[str, Any]:
        text = json.dumps({"error": code}, sort_keys=True, separators=(",", ":"))
        return {
            "contentItems": [{"type": "inputText", "text": text}],
            "success": False,
        }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _relative_python() -> Path:
    return Path("env/Scripts/python.exe") if os.name == "nt" else Path("env/bin/python")


def _manifest_payload() -> dict[str, Any]:
    return {
        "schema_version": RUNTIME_SCHEMA,
        "runtime_id": RUNTIME_ID,
        "python_version": PYTHON_VERSION,
        "packages": PACKAGE_VERSIONS,
        "python_relative_path": _relative_python().as_posix(),
        "requirements_sha256": _sha256(REQUIREMENTS_LOCK),
    }


def validate_scientific_runtime(
    runtime_root: str | Path,
    *,
    required: bool = True,
) -> ScientificRuntimeValidation | None:
    root = Path(runtime_root).resolve()
    manifest = root / "manifest.json"
    if not manifest.exists():
        if required:
            raise ScientificRuntimeError(
                f"scientific runtime is not provisioned: {manifest}"
            )
        return None
    if manifest.is_symlink() or not manifest.is_file():
        raise ScientificRuntimeError(
            "scientific runtime manifest is not a regular file"
        )
    try:
        value = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ScientificRuntimeError(
            "scientific runtime manifest is unreadable"
        ) from exc
    if value != _manifest_payload():
        raise ScientificRuntimeError("scientific runtime manifest differs from its pin")
    python = root / _relative_python()
    if python.is_symlink():
        try:
            resolved_python = python.resolve(strict=True)
        except OSError as exc:
            raise ScientificRuntimeError("scientific Python symlink is broken") from exc
        if not resolved_python.is_relative_to(root):
            raise ScientificRuntimeError(
                "scientific Python resolves outside runtime root"
            )
    elif not python.is_file():
        raise ScientificRuntimeError("scientific Python executable is missing")
    if os.name != "nt" and not os.access(python, os.X_OK):
        raise ScientificRuntimeError("scientific Python is not executable")
    return ScientificRuntimeValidation(
        runtime_id=RUNTIME_ID,
        root=root,
        python=python,
        bin_directory=python.parent,
        manifest_path=manifest,
        requirements_sha256=_sha256(REQUIREMENTS_LOCK),
    )


def scientific_calculator_from_environment(
    environment: Mapping[str, str],
    *,
    config: ScientificCalculatorConfig = _DEFAULT_SCIENTIFIC_CALCULATOR_CONFIG,
) -> ScientificCalculatorTool:
    """Find exactly one pinned runtime advertised by the controlled PATH."""

    raw_path = environment.get("PATH")
    if not isinstance(raw_path, str) or not raw_path:
        raise ScientificRuntimeError(
            "controlled App Server environment has no scientific runtime PATH"
        )
    validations: list[ScientificRuntimeValidation] = []
    executable_name = "python.exe" if os.name == "nt" else "python"
    for raw_entry in raw_path.split(os.pathsep):
        if not raw_entry:
            continue
        entry = Path(raw_entry)
        candidate = entry / executable_name
        if not candidate.exists():
            continue
        if entry.name not in {"bin", "Scripts"} or entry.parent.name != "env":
            continue
        root = entry.parent.parent
        try:
            validation = validate_scientific_runtime(root)
        except ScientificRuntimeError:
            continue
        try:
            same_python = validation.python.resolve(strict=True) == candidate.resolve(
                strict=True
            )
        except OSError:
            same_python = False
        if same_python and validation not in validations:
            validations.append(validation)
    if len(validations) != 1:
        raise ScientificRuntimeError(
            "controlled PATH must expose exactly one pinned scientific runtime"
        )
    return ScientificCalculatorTool(validations[0], config=config)


def _controlled_environment(runtime_root: Path, uv_path: Path) -> dict[str, str]:
    build_root = runtime_root / ".provisioning"
    home = build_root / "home"
    cache = build_root / "uv-cache"
    temporary = build_root / "tmp"
    for directory in (home, cache, temporary):
        directory.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            directory.chmod(0o700)
    if os.name == "nt":
        return {
            "HOME": str(home),
            "USERPROFILE": str(home),
            "UV_CACHE_DIR": str(cache),
            "TEMP": str(temporary),
            "TMP": str(temporary),
            "PATH": f"{uv_path.parent};C:\\Windows\\System32;C:\\Windows",
        }
    return {
        "HOME": str(home),
        "UV_CACHE_DIR": str(cache),
        "TMPDIR": str(temporary),
        "PATH": f"{uv_path.parent}:/usr/bin:/bin:/usr/sbin:/sbin",
        "LANG": "C.UTF-8",
    }


def _run(argv: list[str], environment: dict[str, str]) -> None:
    try:
        subprocess.run(argv, check=True, env=environment)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ScientificRuntimeError(
            f"scientific runtime command failed: {argv[0]}"
        ) from exc


def _managed_python(runtime_root: Path) -> Path:
    install_root = runtime_root / "python"
    names = ("python.exe",) if os.name == "nt" else ("python3.13",)
    candidates = sorted(
        path
        for name in names
        for path in install_root.rglob(name)
        if path.is_file() and path.resolve().is_relative_to(runtime_root)
    )
    if len(candidates) != 1:
        raise ScientificRuntimeError(
            "uv did not produce exactly one managed Python 3.13 executable"
        )
    return candidates[0]


def _publish_manifest(runtime_root: Path) -> None:
    manifest = runtime_root / "manifest.json"
    pending = runtime_root / f".manifest.pending.{uuid.uuid4().hex}"
    pending.write_text(
        json.dumps(_manifest_payload(), sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    if os.name != "nt":
        pending.chmod(0o600)
    pending.replace(manifest)


def provision_scientific_runtime(
    runtime_root: str | Path,
    *,
    uv_executable: str | Path = "uv",
) -> ScientificRuntimeValidation:
    """Provision once with uv, then return the validated immutable interface."""

    root = Path(runtime_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    existing = validate_scientific_runtime(root, required=False)
    if existing is not None:
        return existing
    raw_uv = os.fspath(uv_executable)
    found_uv = shutil.which(raw_uv) if not os.path.isabs(raw_uv) else raw_uv
    if found_uv is None:
        raise ScientificRuntimeError("uv executable is unavailable")
    uv_path = Path(found_uv).resolve(strict=True)
    environment = _controlled_environment(root, uv_path)
    _run(
        [
            str(uv_path),
            "python",
            "install",
            PYTHON_VERSION,
            "--install-dir",
            str(root / "python"),
        ],
        environment,
    )
    managed_python = _managed_python(root)
    _run(
        [
            str(uv_path),
            "venv",
            "--python",
            str(managed_python),
            str(root / "env"),
        ],
        environment,
    )
    env_python = root / _relative_python()
    _run(
        [
            str(uv_path),
            "pip",
            "install",
            "--python",
            str(env_python),
            "--require-hashes",
            "--no-deps",
            "--only-binary",
            ":all:",
            "-r",
            str(REQUIREMENTS_LOCK),
        ],
        environment,
    )
    _run(
        [
            str(env_python),
            "-I",
            "-c",
            (
                "import mpmath,sympy,sys;"
                "sys.exit(0 if (mpmath.__version__,sympy.__version__)"
                "==('1.3.0','1.14.0') else 1)"
            ),
        ],
        environment,
    )
    _publish_manifest(root)
    validation = validate_scientific_runtime(root)
    assert validation is not None
    provisioning = root / ".provisioning"
    if provisioning.exists():
        # Cache cleanup is best-effort; it is not part of the runtime contract.
        shutil.rmtree(provisioning)
    if os.name != "nt":
        for path in root.rglob("*"):
            if path.is_file() and not path.is_symlink():
                mode = stat.S_IMODE(path.stat().st_mode)
                path.chmod(mode & ~0o222)
    return validation


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--uv", default="uv")
    args = parser.parse_args()
    value = provision_scientific_runtime(args.root, uv_executable=args.uv)
    print(
        json.dumps(
            {
                "runtime_id": value.runtime_id,
                "root": str(value.root),
                "python": str(value.python),
                "requirements_sha256": value.requirements_sha256,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
