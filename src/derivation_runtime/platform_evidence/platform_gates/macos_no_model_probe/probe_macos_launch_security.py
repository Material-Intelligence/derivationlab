#!/usr/bin/env python3
"""Prove the macOS strict-run symlink boundary without a model or credentials.

All mutable probe state stays below ``state/`` next to this script.  The
checked-in report contains only pass/fail markers, not command output or secret
material.  The script refuses to overwrite its evidence file.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
from typing import Any


ROOT = Path(__file__).resolve().parent
STATE = ROOT / "state"
HOME = STATE / "product_home"
CODEX_HOME = STATE / "codex_home"
WORKSPACE = STATE / "workspace"
OUTSIDE = STATE / "outside"
RUNTIME_TEMP = WORKSPACE / ".runtime_tmp"
REPORT = ROOT / "probe_results.json"
PROFILE = "strict_run_workspace"
RUNTIME = STATE / "runtime"
RUNTIME_PYTHON = RUNTIME / "env" / "bin" / "python"


def send(process: subprocess.Popen[str], message: dict[str, Any]) -> None:
    if process.stdin is None:
        raise RuntimeError("App Server stdin is unavailable")
    process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
    process.stdin.flush()


def receive(
    process: subprocess.Popen[str], request_id: int
) -> tuple[dict[str, Any], list[str]]:
    if process.stdout is None:
        raise RuntimeError("App Server stdout is unavailable")
    notifications: list[str] = []
    for raw in process.stdout:
        message = json.loads(raw)
        if not isinstance(message, dict):
            raise RuntimeError("App Server emitted a non-object message")
        if message.get("id") == request_id:
            return message, notifications
        method = message.get("method")
        if isinstance(method, str) and "id" not in message:
            notifications.append(method)
    raise RuntimeError(f"App Server exited before response {request_id}")


def request(
    process: subprocess.Popen[str],
    request_id: int,
    method: str,
    params: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    send(process, {"method": method, "id": request_id, "params": params})
    response, notifications = receive(process, request_id)
    if "error" in response:
        raise RuntimeError(f"{method} failed: {response['error']}")
    return response, notifications


def exec_command(
    process: subprocess.Popen[str], request_id: int, argv: list[str]
) -> tuple[int | None, str, list[str]]:
    response, notifications = request(
        process,
        request_id,
        "command/exec",
        {
            "command": argv,
            "cwd": str(WORKSPACE),
            "permissionProfile": PROFILE,
            "timeoutMs": 10_000,
        },
    )
    result = response.get("result", {})
    stdout = result.get("stdout") if isinstance(result.get("stdout"), str) else ""
    return result.get("exitCode"), stdout, notifications


def prepare_state() -> Path:
    for directory in (HOME, CODEX_HOME, WORKSPACE, OUTSIDE, RUNTIME_TEMP):
        directory.mkdir(parents=True, exist_ok=True)

    config = CODEX_HOME / "config.toml"
    config.write_text(
        '\n'.join(
            [
                f'default_permissions = "{PROFILE}"',
                "",
                f"[permissions.{PROFILE}]",
                'description = "Read minimal runtime files and write only the run workspace."',
                "",
                f"[permissions.{PROFILE}.filesystem]",
                '":minimal" = "read"',
                '":workspace_roots" = "write"',
                f'{json.dumps(str(RUNTIME))} = "read"',
                "",
                f"[permissions.{PROFILE}.network]",
                "enabled = false",
                "",
            ]
        ),
        encoding="utf-8",
    )
    (WORKSPACE / "input.txt").write_text("workspace fixture\n", encoding="utf-8")
    (OUTSIDE / "sentinel.txt").write_text("outside sentinel\n", encoding="utf-8")

    escape = WORKSPACE / "outside-link"
    if escape.exists() or escape.is_symlink():
        if not escape.is_symlink() or escape.resolve(strict=True) != OUTSIDE.resolve():
            raise RuntimeError(f"unsafe pre-existing escape fixture: {escape}")
    else:
        escape.symlink_to(OUTSIDE, target_is_directory=True)
    return escape


def main() -> None:
    if REPORT.exists():
        raise SystemExit(f"refusing to overwrite evidence: {REPORT}")
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise SystemExit("this evidence probe is pinned to macOS arm64")
    if not RUNTIME_PYTHON.is_file():
        raise SystemExit(f"pinned runtime Python is unavailable: {RUNTIME_PYTHON}")
    codex = shutil.which("codex")
    if codex is None:
        raise SystemExit("codex executable is unavailable")
    codex_path = str(Path(codex).absolute())
    controlled_path = f"{Path(codex_path).parent}:/usr/bin:/bin:/usr/sbin:/sbin"
    version = subprocess.run(
        [codex_path, "--version"],
        check=True,
        capture_output=True,
        text=True,
        env={"PATH": controlled_path},
    ).stdout.strip()
    if version != "codex-cli 0.147.0":
        raise SystemExit(f"unexpected Codex version: {version}")

    escape = prepare_state()
    outside_write = OUTSIDE / "must-not-exist.txt"
    if outside_write.exists():
        raise SystemExit(f"refusing to remove or overwrite fixture: {outside_write}")

    environment = {
        "HOME": str(HOME),
        "CODEX_HOME": str(CODEX_HOME),
        "TMPDIR": str(RUNTIME_TEMP),
        "PATH": controlled_path,
        "LANG": "C.UTF-8",
    }
    process = subprocess.Popen(
        [codex_path, "app-server", "--listen", "stdio://"],
        cwd=WORKSPACE,
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    notification_methods: list[str] = []
    checks: dict[str, str] = {}
    try:
        initialize, notifications = request(
            process,
            0,
            "initialize",
            {
                "clientInfo": {
                    "name": "macos_launch_security_probe",
                    "title": "macOS Launch Security Probe",
                    "version": "0.1.0",
                },
                "capabilities": {"experimentalApi": True},
            },
        )
        notification_methods.extend(notifications)
        send(process, {"method": "initialized", "params": {}})
        initialize_result = initialize.get("result")
        if not isinstance(initialize_result, dict):
            raise RuntimeError("initialize response has no result")
        if initialize_result.get("platformFamily") != "unix":
            raise RuntimeError("initialize reported an unexpected platform family")
        if initialize_result.get("platformOs") != "macos":
            raise RuntimeError("initialize reported an unexpected operating system")
        if "/0.147.0 " not in initialize_result.get("userAgent", ""):
            raise RuntimeError("initialize did not report pinned Codex 0.147.0")
        if Path(initialize_result.get("codexHome", "")) != CODEX_HOME:
            raise RuntimeError("initialize reported an unexpected CODEX_HOME")
        checks["app_server_initialize"] = "passed"

        profiles, notifications = request(
            process, 1, "permissionProfile/list", {"cwd": str(WORKSPACE)}
        )
        notification_methods.extend(notifications)
        profile_data = profiles.get("result", {}).get("data", [])
        allowed = {
            item.get("id")
            for item in profile_data
            if isinstance(item, dict) and item.get("allowed") is True
        }
        if PROFILE not in allowed:
            raise RuntimeError("strict_run_workspace profile is unavailable")

        commands = [
            (
                "workspace_read",
                ["/bin/cat", str(WORKSPACE / "input.txt")],
                lambda code, output: code == 0,
            ),
            (
                "workspace_write",
                ["/usr/bin/touch", str(WORKSPACE / "written.txt")],
                lambda code, output: code == 0,
            ),
            (
                "adjacent_read_denied",
                ["/bin/cat", str(OUTSIDE / "sentinel.txt")],
                lambda code, output: code not in (None, 0),
            ),
            (
                "symlink_escape_read_denied",
                ["/bin/cat", str(escape / "sentinel.txt")],
                lambda code, output: code not in (None, 0),
            ),
            (
                "symlink_escape_write_denied",
                ["/usr/bin/touch", str(escape / outside_write.name)],
                lambda code, output: code not in (None, 0),
            ),
            (
                "network_off",
                [
                    str(RUNTIME_PYTHON),
                    "-I",
                    "-c",
                    "import socket,sys\ntry:\n socket.create_connection(('1.1.1.1',443),1); sys.exit(9)\nexcept PermissionError:\n print('NETWORK_DENIED')",
                ],
                lambda code, output: code == 0 and "NETWORK_DENIED" in output,
            ),
            (
                "python_sympy",
                [
                    str(RUNTIME_PYTHON),
                    "-I",
                    "-c",
                    "import sympy as s; x=s.symbols('x'); print('SYMPY_OK', s.__version__, s.solve(x**2-1,x))",
                ],
                lambda code, output: code == 0
                and "SYMPY_OK 1.14.0 [-1, 1]" in output,
            ),
        ]
        for request_id, (check_id, argv, predicate) in enumerate(commands, 2):
            exit_code, output, notifications = exec_command(process, request_id, argv)
            notification_methods.extend(notifications)
            passed = bool(predicate(exit_code, output))
            checks[check_id] = "passed" if passed else "failed"

        checks["sandbox_activation"] = (
            "passed"
            if checks.get("workspace_read") == "passed"
            and checks.get("workspace_write") == "passed"
            and checks.get("adjacent_read_denied") == "passed"
            and checks.get("symlink_escape_read_denied") == "passed"
            and checks.get("symlink_escape_write_denied") == "passed"
            and checks.get("network_off") == "passed"
            and checks.get("python_sympy") == "passed"
            else "failed"
        )
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)

    forbidden_warnings = sorted(
        method
        for method in notification_methods
        if method in {"warning", "configWarning"}
    )
    required = {
        "app_server_initialize",
        "sandbox_activation",
        "workspace_read",
        "workspace_write",
        "adjacent_read_denied",
        "symlink_escape_read_denied",
        "symlink_escape_write_denied",
        "network_off",
        "python_sympy",
    }
    failures = sorted(check_id for check_id in required if checks.get(check_id) != "passed")
    if not (WORKSPACE / "written.txt").is_file():
        failures.append("workspace_write_missing")
    if outside_write.exists():
        failures.append("outside_write_created")
    if forbidden_warnings:
        failures.append("app_server_warning")

    payload = {
        "schema_version": "macos-launch-security-probe-v1",
        "platform": "macos",
        "architecture": "arm64",
        "codex_version": "0.147.0",
        "permission_profile": PROFILE,
        "status": "passed" if not failures else "failed",
        "checks": checks,
        "warning_methods": forbidden_warnings,
        "failures": failures,
        "outside_write_created": outside_write.exists(),
        "used_model_call": False,
        "read_or_printed_credentials": False,
        "recorded_command_output": False,
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }
    REPORT.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    if failures:
        raise SystemExit("FAIL: " + ", ".join(failures))
    print("PASS: macOS App Server strict-run symlink read/write boundary")
    print("DONE_MACOS_LAUNCH_SECURITY_PROBE")


if __name__ == "__main__":
    main()
