"""Platform capability policy for the derivation runtime.

The policy separates a platform family from evidence for one concrete host.
An untested platform is ``pending`` and a host with a known sandbox failure is
``failed``.  Neither state is silently converted to a weaker sandbox.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Any

PINNED_CODEX_VERSION = "0.147.0"
PINNED_V2_SCHEMA_SHA256 = (
    "ff10829cd75b67297019b39ab508ac699198574663579aa18336b7dc55ea178f"
)
MACOS_PROBE_SHA256 = "d98178d61584428d3f6959985d9d21bce1c6d4a689dff0d703da2af83c7fc2e6"
MACOS_PROBE_RELATIVE_PATH = Path(
    "src/derivation_runtime/platform_evidence/"
    "app_server_probe/uv_runtime_probe/probe_results.json"
)
MACOS_LAUNCH_SECURITY_SHA256 = (
    "5a62fa6f38a6721a2549f3bd03b226ec6315377d7434a8e9e8e3ec4e629fbcda"
)
MACOS_LAUNCH_SECURITY_RELATIVE_PATH = Path(
    "src/derivation_runtime/platform_evidence/platform_gates/"
    "macos_no_model_probe/probe_results.json"
)
MACOS_LAUNCH_SECURITY_SCRIPT_RELATIVE_PATH = Path(
    "src/derivation_runtime/platform_evidence/platform_gates/"
    "macos_no_model_probe/probe_macos_launch_security.py"
)


class PlatformFamily(str, Enum):
    MACOS = "macos"
    LINUX = "linux"
    WINDOWS = "windows"


class CapabilityStatus(str, Enum):
    SUPPORTED = "supported"
    PENDING = "pending"
    FAILED = "failed"


class SandboxBackend(str, Enum):
    SEATBELT = "seatbelt"
    BUBBLEWRAP_SECCOMP = "bubblewrap_seccomp"
    WINDOWS_ELEVATED = "windows_elevated"
    WINDOWS_UNELEVATED = "windows_unelevated"
    WSL2 = "wsl2"


@dataclass(frozen=True)
class EvidenceReference:
    path: str | None
    sha256: str | None
    evidence_kind: str
    summary: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PlatformCapabilityReport:
    host_id: str
    platform: PlatformFamily
    architecture: str
    os_description: str
    codex_version: str
    sandbox_backend: SandboxBackend
    scope: str
    status: CapabilityStatus
    proven_checks: tuple[str, ...] = ()
    pending_checks: tuple[str, ...] = ()
    failures: tuple[str, ...] = ()
    evidence: tuple[EvidenceReference, ...] = ()

    @property
    def authorizes_model_turns(self) -> bool:
        required = {
            "app_server_initialize",
            "sandbox_activation",
            "workspace_read_write",
            "adjacent_read_denied",
            "symlink_escape_denied",
            "network_off",
            "python_sympy",
        }
        if self.platform is PlatformFamily.WINDOWS:
            required.add("windows_junction_escape_denied")
        return (
            self.scope == "launch_security"
            and self.status is CapabilityStatus.SUPPORTED
            and required <= set(self.proven_checks)
        )

    @property
    def release_conformant(self) -> bool:
        return (
            self.status is CapabilityStatus.SUPPORTED
            and not self.pending_checks
            and not self.failures
        )

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["platform"] = self.platform.value
        payload["sandbox_backend"] = self.sandbox_backend.value
        payload["status"] = self.status.value
        payload["evidence"] = [item.to_dict() for item in self.evidence]
        payload["authorizes_model_turns"] = self.authorizes_model_turns
        payload["release_conformant"] = self.release_conformant
        return payload


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _probe_result(payload: Mapping[str, Any], key: str) -> tuple[int | None, str, str]:
    entry = payload.get(key)
    if not isinstance(entry, Mapping):
        return None, "", ""
    result = entry.get("result")
    if not isinstance(result, Mapping):
        return None, "", ""
    exit_code = result.get("exitCode")
    return (
        exit_code if isinstance(exit_code, int) else None,
        result.get("stdout") if isinstance(result.get("stdout"), str) else "",
        result.get("stderr") if isinstance(result.get("stderr"), str) else "",
    )


def current_macos_capability(
    repo_root: Path,
    *,
    architecture: str,
    codex_version: str,
) -> PlatformCapabilityReport:
    """Verify the checked-in, no-model macOS App Server evidence."""

    evidence_path = repo_root / MACOS_PROBE_RELATIVE_PATH
    launch_security_path = repo_root / MACOS_LAUNCH_SECURITY_RELATIVE_PATH
    launch_security_script = repo_root / MACOS_LAUNCH_SECURITY_SCRIPT_RELATIVE_PATH
    failures: list[str] = []
    if architecture != "arm64":
        failures.append(
            f"checked-in evidence is arm64, observed architecture is {architecture!r}"
        )
    if codex_version != PINNED_CODEX_VERSION:
        failures.append(
            f"checked-in evidence pins Codex {PINNED_CODEX_VERSION}, "
            f"observed {codex_version!r}"
        )
    if not evidence_path.is_file():
        failures.append(f"missing evidence file: {evidence_path}")
        digest = None
        payload: Mapping[str, Any] = {}
    else:
        digest = sha256_file(evidence_path)
        if digest != MACOS_PROBE_SHA256:
            failures.append(
                f"macOS evidence sha256 mismatch: {digest} != {MACOS_PROBE_SHA256}"
            )
        try:
            loaded = json.loads(evidence_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            failures.append(f"cannot parse macOS evidence: {exc}")
            payload = {}
        else:
            payload = loaded if isinstance(loaded, Mapping) else {}
            if not isinstance(loaded, Mapping):
                failures.append("macOS evidence root must be a JSON object")

    if not launch_security_path.is_file():
        failures.append(f"missing launch-security evidence: {launch_security_path}")
        launch_security_digest = None
        launch_security: Mapping[str, Any] = {}
    else:
        launch_security_digest = sha256_file(launch_security_path)
        if launch_security_digest != MACOS_LAUNCH_SECURITY_SHA256:
            failures.append(
                "macOS launch-security evidence sha256 mismatch: "
                f"{launch_security_digest} != {MACOS_LAUNCH_SECURITY_SHA256}"
            )
        try:
            launch_loaded = json.loads(launch_security_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            failures.append(f"cannot parse macOS launch-security evidence: {exc}")
            launch_security = {}
        else:
            launch_security = (
                launch_loaded if isinstance(launch_loaded, Mapping) else {}
            )
            if not isinstance(launch_loaded, Mapping):
                failures.append("macOS launch-security evidence must be a JSON object")

    workspace = _probe_result(payload, "workspace_read_write")
    outside = _probe_result(payload, "outside_read_denied")
    network = _probe_result(payload, "network_denied")
    sympy = _probe_result(payload, "sympy_calculation")
    if workspace[0] != 0 or "WORKSPACE_OK" not in workspace[1]:
        failures.append("workspace read/write proof is missing or failed")
    if outside[0] in (None, 0) or "Operation not permitted" not in outside[2]:
        failures.append("adjacent read denial proof is missing or failed")
    if network[0] != 0 or "NETWORK_DENIED" not in network[1]:
        failures.append("network denial proof is missing or failed")
    if sympy[0] != 0 or "SYMPY_OK" not in sympy[1]:
        failures.append("Python/SymPy proof is missing or failed")

    expected_metadata = {
        "schema_version": "macos-launch-security-probe-v1",
        "platform": "macos",
        "architecture": "arm64",
        "codex_version": PINNED_CODEX_VERSION,
        "permission_profile": "strict_run_workspace",
        "status": "passed",
        "outside_write_created": False,
        "used_model_call": False,
        "read_or_printed_credentials": False,
        "recorded_command_output": False,
    }
    for key, expected in expected_metadata.items():
        if launch_security.get(key) != expected:
            failures.append(
                f"macOS launch-security metadata {key!r} does not equal {expected!r}"
            )
    launch_checks = launch_security.get("checks")
    if not isinstance(launch_checks, Mapping):
        failures.append("macOS launch-security checks are missing")
        launch_checks = {}
    for check_id in (
        "app_server_initialize",
        "sandbox_activation",
        "workspace_read",
        "workspace_write",
        "adjacent_read_denied",
        "symlink_escape_read_denied",
        "symlink_escape_write_denied",
        "network_off",
        "python_sympy",
    ):
        if launch_checks.get(check_id) != "passed":
            failures.append(f"macOS launch-security check failed: {check_id}")
    if launch_security.get("failures") != []:
        failures.append("macOS launch-security evidence reports failures")
    if launch_security.get("warning_methods") != []:
        failures.append("macOS launch-security evidence reports warnings")
    if not launch_security_script.is_file():
        failures.append(
            f"missing launch-security probe script: {launch_security_script}"
        )
    else:
        script_digest = sha256_file(launch_security_script)
        if launch_security.get("script_sha256") != script_digest:
            failures.append(
                "macOS launch-security script does not match the evidence report"
            )

    status = CapabilityStatus.FAILED if failures else CapabilityStatus.SUPPORTED
    return PlatformCapabilityReport(
        host_id="macos-arm64-local-20260829",
        platform=PlatformFamily.MACOS,
        architecture=architecture,
        os_description="macOS arm64 local host",
        codex_version=codex_version,
        sandbox_backend=SandboxBackend.SEATBELT,
        scope="launch_security",
        status=status,
        proven_checks=(
            "app_server_initialize",
            "sandbox_activation",
            "workspace_read_write",
            "adjacent_read_denied",
            "symlink_escape_denied",
            "network_off",
            "python_sympy",
        )
        if not failures
        else (),
        pending_checks=(
            "account_persistence",
            "restart_resume",
            "fork_completed_node",
        ),
        failures=tuple(failures),
        evidence=(
            EvidenceReference(
                path=str(MACOS_PROBE_RELATIVE_PATH),
                sha256=digest,
                evidence_kind="checked_in_no_model_probe",
                summary="App Server workspace, adjacent-read, network, and SymPy probe",
            ),
            EvidenceReference(
                path=str(MACOS_LAUNCH_SECURITY_RELATIVE_PATH),
                sha256=launch_security_digest,
                evidence_kind="checked_in_no_model_probe",
                summary=(
                    "strict_run_workspace initialize, sandbox, workspace, adjacent, "
                    "symlink read/write, network, and SymPy probe"
                ),
            ),
        ),
    )


def pending_linux_capability(
    *, architecture: str, codex_version: str
) -> PlatformCapabilityReport:
    return PlatformCapabilityReport(
        host_id=f"linux-{architecture}-unverified",
        platform=PlatformFamily.LINUX,
        architecture=architecture,
        os_description="Linux host without a passing conformance report",
        codex_version=codex_version,
        sandbox_backend=SandboxBackend.BUBBLEWRAP_SECCOMP,
        scope="launch_security",
        status=CapabilityStatus.PENDING,
        pending_checks=("all_required_conformance_checks",),
        evidence=(),
    )


def failed_colima_linux_capability() -> PlatformCapabilityReport:
    """Return the observed 2026-08-29 Colima failure without generalizing it."""

    return PlatformCapabilityReport(
        host_id="colima-ubuntu-24.04.4-arm64-20260829",
        platform=PlatformFamily.LINUX,
        architecture="arm64",
        os_description="Ubuntu 24.04.4 LTS in Colima",
        codex_version=PINNED_CODEX_VERSION,
        sandbox_backend=SandboxBackend.BUBBLEWRAP_SECCOMP,
        scope="launch_security",
        status=CapabilityStatus.FAILED,
        pending_checks=("remaining_linux_conformance_checks",),
        failures=(
            "bwrap loopback setup failed with RTM_NEWADDR Operation not permitted",
            "unshare user namespace failed while writing /proc/self/uid_map",
        ),
        evidence=(
            EvidenceReference(
                path=None,
                sha256=None,
                evidence_kind="live_no_model_observation",
                summary=(
                    "bubblewrap 0.9.0 entered the sandbox but the Colima host "
                    "could not provide the required network/user-namespace operations"
                ),
            ),
        ),
    )


def pending_windows_capability(
    *, architecture: str, codex_version: str
) -> PlatformCapabilityReport:
    return PlatformCapabilityReport(
        host_id=f"windows-{architecture}-unverified",
        platform=PlatformFamily.WINDOWS,
        architecture=architecture,
        os_description="Windows host without a passing elevated-sandbox report",
        codex_version=codex_version,
        sandbox_backend=SandboxBackend.WINDOWS_ELEVATED,
        scope="launch_security",
        status=CapabilityStatus.PENDING,
        pending_checks=("all_required_conformance_checks",),
        evidence=(),
    )


def expected_sandbox_backend(platform: PlatformFamily) -> SandboxBackend:
    if platform is PlatformFamily.MACOS:
        return SandboxBackend.SEATBELT
    if platform is PlatformFamily.LINUX:
        return SandboxBackend.BUBBLEWRAP_SECCOMP
    if platform is PlatformFamily.WINDOWS:
        return SandboxBackend.WINDOWS_ELEVATED
    raise ValueError(f"unsupported platform family: {platform!r}")
