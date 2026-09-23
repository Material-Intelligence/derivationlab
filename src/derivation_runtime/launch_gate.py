"""Configuration and runtime-boundary checks for Codex App Server.

Startup validates isolated paths and explicit configuration. A supplied
platform capability report adds release/benchmark conformance evidence, but
normal product startup does not require that heavyweight report.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
from typing import Any

from .platform_policy import (
    PINNED_CODEX_VERSION,
    PINNED_V2_SCHEMA_SHA256,
    CapabilityStatus,
    PlatformCapabilityReport,
    PlatformFamily,
    SandboxBackend,
    expected_sandbox_backend,
)

STRICT_PERMISSION_PROFILE = "strict_run_workspace"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class CredentialStore(str, Enum):
    KEYRING = "keyring"
    FILE = "file"
    AUTO = "auto"


class GateStatus(str, Enum):
    SUPPORTED = "supported"
    PENDING = "pending"
    FAILED = "failed"


@dataclass(frozen=True)
class RuntimeRoots:
    home: str
    codex_home: str
    workspace: str
    runtime: str
    repository: str
    user_home: str

    def isolated_items(self) -> tuple[tuple[str, str], ...]:
        return (
            ("home", self.home),
            ("codex_home", self.codex_home),
            ("workspace", self.workspace),
            ("runtime", self.runtime),
        )

    def all_items(self) -> tuple[tuple[str, str], ...]:
        return self.isolated_items() + (
            ("repository", self.repository),
            ("user_home", self.user_home),
        )


@dataclass(frozen=True)
class PathState:
    requested: str
    resolved: str
    exists: bool
    is_directory: bool


@dataclass(frozen=True)
class LaunchRequest:
    platform: PlatformFamily
    architecture: str
    app_server_executable: str
    roots: RuntimeRoots
    expected_codex_version: str
    expected_schema_sha256: str
    credential_store: CredentialStore
    permission_profile: str = STRICT_PERMISSION_PROFILE
    allowed_skills: tuple[str, ...] = ()
    allowed_mcp_servers: tuple[str, ...] = ()
    allowed_apps: tuple[str, ...] = ()
    allowed_command_network_destinations: tuple[str, ...] = ()
    tool_path_entries: tuple[str, ...] = ()
    windows_sandbox_mode: str | None = None


@dataclass(frozen=True)
class RuntimeObservation:
    platform: PlatformFamily
    architecture: str
    codex_version: str
    schema_sha256: str
    sandbox_backend: SandboxBackend
    active_permission_profile: str | None
    available_permission_profiles: tuple[str, ...]
    instruction_sources: tuple[str, ...]
    skills: tuple[str, ...]
    mcp_servers: tuple[str, ...]
    apps: tuple[str, ...]
    command_network_enabled: bool
    command_network_proxy_active: bool
    command_network_destinations: tuple[str, ...]
    effective_credential_store: CredentialStore
    host_home: str
    tool_home: str
    tool_codex_home: str | None
    web_search_enabled: bool
    windows_sandbox_mode: str | None = None


@dataclass(frozen=True)
class GateIssue:
    code: str
    status: GateStatus
    message: str

    def to_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "status": self.status.value,
            "message": self.message,
        }


@dataclass(frozen=True)
class GateResult:
    status: GateStatus
    stage: str
    issues: tuple[GateIssue, ...]

    @property
    def allowed(self) -> bool:
        return self.status is GateStatus.SUPPORTED

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "stage": self.stage,
            "allowed": self.allowed,
            "issues": [issue.to_dict() for issue in self.issues],
        }


@dataclass(frozen=True)
class AppServerCommand:
    argv: tuple[str, ...]
    cwd: str
    environment: Mapping[str, str]
    inherit_parent_environment: bool = False
    shell: bool = False

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["argv"] = list(self.argv)
        payload["environment"] = dict(self.environment)
        return payload


class LaunchBlocked(RuntimeError):
    def __init__(self, result: GateResult) -> None:
        self.result = result
        details = "; ".join(f"{item.code}: {item.message}" for item in result.issues)
        super().__init__(f"launch gate {result.status.value}: {details}")


def _pure_path(platform: PlatformFamily, value: str) -> PurePath:
    if platform is PlatformFamily.WINDOWS:
        return PureWindowsPath(value)
    return PurePosixPath(value)


def _normal_parts(platform: PlatformFamily, value: str) -> tuple[str, ...]:
    path = _pure_path(platform, value)
    parts = path.parts
    if platform is PlatformFamily.WINDOWS:
        return tuple(part.casefold() for part in parts)
    return parts


def _contains(platform: PlatformFamily, parent: str, child: str) -> bool:
    parent_parts = _normal_parts(platform, parent)
    child_parts = _normal_parts(platform, child)
    return len(parent_parts) <= len(child_parts) and (
        child_parts[: len(parent_parts)] == parent_parts
    )


def _overlaps(platform: PlatformFamily, left: str, right: str) -> bool:
    return _contains(platform, left, right) or _contains(platform, right, left)


def _join(platform: PlatformFamily, root: str, name: str) -> str:
    return str(_pure_path(platform, root) / name)


def inspect_path_states(roots: RuntimeRoots) -> dict[str, PathState]:
    """Inspect real paths on the current host without reading file contents."""

    states: dict[str, PathState] = {}
    for name, requested in roots.all_items():
        path = Path(requested)
        try:
            resolved = str(path.resolve(strict=True))
        except (FileNotFoundError, OSError):
            resolved = str(path.resolve(strict=False))
        states[name] = PathState(
            requested=requested,
            resolved=resolved,
            exists=path.exists(),
            is_directory=path.is_dir(),
        )
    return states


def _result(stage: str, issues: Iterable[GateIssue]) -> GateResult:
    materialized = tuple(issues)
    if any(issue.status is GateStatus.FAILED for issue in materialized):
        status = GateStatus.FAILED
    elif any(issue.status is GateStatus.PENDING for issue in materialized):
        status = GateStatus.PENDING
    else:
        status = GateStatus.SUPPORTED
    return GateResult(status=status, stage=stage, issues=materialized)


def _failed(code: str, message: str) -> GateIssue:
    return GateIssue(code=code, status=GateStatus.FAILED, message=message)


def _pending(code: str, message: str) -> GateIssue:
    return GateIssue(code=code, status=GateStatus.PENDING, message=message)


def _validate_root_paths(
    request: LaunchRequest,
    states: Mapping[str, PathState],
) -> list[GateIssue]:
    issues: list[GateIssue] = []
    required_names = {name for name, _ in request.roots.all_items()}
    missing_states = sorted(required_names - set(states))
    if missing_states:
        issues.append(
            _failed("path_state_missing", f"missing path states: {missing_states}")
        )
        return issues

    for name, requested in request.roots.all_items():
        pure = _pure_path(request.platform, requested)
        state = states[name]
        if not pure.is_absolute():
            issues.append(_failed("path_not_absolute", f"{name} is not absolute"))
        if state.requested != requested:
            issues.append(
                _failed(
                    "path_state_mismatch", f"{name} path state does not match request"
                )
            )
        if not state.exists:
            issues.append(_failed("path_missing", f"{name} does not exist"))
        elif not state.is_directory:
            issues.append(_failed("path_not_directory", f"{name} is not a directory"))
        if not _pure_path(request.platform, state.resolved).is_absolute():
            issues.append(
                _failed(
                    "resolved_path_not_absolute",
                    f"{name} resolved path is not absolute",
                )
            )

    isolated = request.roots.isolated_items()
    for index, (left_name, _) in enumerate(isolated):
        for right_name, _ in isolated[index + 1 :]:
            left = states[left_name].resolved
            right = states[right_name].resolved
            if _overlaps(request.platform, left, right):
                issues.append(
                    _failed(
                        "isolated_roots_overlap",
                        f"{left_name} overlaps {right_name}",
                    )
                )

    repository = states["repository"].resolved
    user_home = states["user_home"].resolved
    for name, _ in isolated:
        resolved = states[name].resolved
        if _overlaps(request.platform, resolved, repository):
            issues.append(
                _failed("repository_exposed", f"{name} overlaps the repository")
            )
        if _contains(request.platform, resolved, user_home):
            issues.append(
                _failed(
                    "user_home_exposed",
                    f"{name} equals or contains the real user home",
                )
            )
    return issues


def evaluate_process_start(
    request: LaunchRequest,
    *,
    path_states: Mapping[str, PathState] | None = None,
) -> GateResult:
    """Validate the minimum configuration permitted to start a probe process."""

    issues: list[GateIssue] = []
    executable = _pure_path(request.platform, request.app_server_executable)
    if not executable.is_absolute():
        issues.append(
            _failed("executable_not_absolute", "App Server executable must be absolute")
        )
    if request.expected_codex_version != PINNED_CODEX_VERSION:
        issues.append(
            _failed(
                "version_pin_mismatch",
                f"required Codex version is {PINNED_CODEX_VERSION}",
            )
        )
    if not SHA256_RE.fullmatch(request.expected_schema_sha256):
        issues.append(_failed("schema_pin_invalid", "schema sha256 pin is invalid"))
    elif request.expected_schema_sha256 != PINNED_V2_SCHEMA_SHA256:
        issues.append(
            _failed(
                "schema_pin_mismatch",
                "requested App Server schema does not match the reviewed schema",
            )
        )
    if request.permission_profile != STRICT_PERMISSION_PROFILE:
        issues.append(
            _failed(
                "permission_profile_invalid",
                f"required profile is {STRICT_PERMISSION_PROFILE!r}",
            )
        )
    if request.credential_store is CredentialStore.AUTO:
        issues.append(
            _failed(
                "credential_auto_forbidden",
                "auto credential fallback may silently create plaintext auth.json",
            )
        )
    for entry in request.tool_path_entries:
        path = _pure_path(request.platform, entry)
        if not path.is_absolute() or not _contains(
            request.platform, request.roots.runtime, entry
        ):
            issues.append(
                _failed(
                    "tool_path_outside_runtime",
                    "tool PATH entries must be absolute children of runtime root",
                )
            )
    if request.platform is PlatformFamily.WINDOWS:
        if request.windows_sandbox_mode != "elevated":
            issues.append(
                _failed(
                    "unsafe_windows_fallback",
                    "native Windows support requires the elevated sandbox",
                )
            )
    elif request.windows_sandbox_mode is not None:
        issues.append(
            _failed(
                "windows_mode_on_non_windows",
                "windows_sandbox_mode must be unset outside native Windows",
            )
        )

    actual_states = (
        path_states if path_states is not None else inspect_path_states(request.roots)
    )
    issues.extend(_validate_root_paths(request, actual_states))
    return _result("process_start", issues)


def _unexpected(observed: Iterable[str], allowed: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted(set(observed) - set(allowed)))


def evaluate_launch(
    request: LaunchRequest,
    observation: RuntimeObservation,
    capability: PlatformCapabilityReport | None = None,
    *,
    path_states: Mapping[str, PathState] | None = None,
) -> GateResult:
    """Validate one launch against its requested runtime boundary.

    A platform capability report is optional because it belongs to release and
    benchmark conformance, not every product startup.  When supplied, it is
    still validated in full.
    """

    issues = list(evaluate_process_start(request, path_states=path_states).issues)
    if capability is not None and capability.platform is not request.platform:
        issues.append(
            _failed("capability_platform_mismatch", "capability platform differs")
        )
    if capability is not None and capability.architecture != request.architecture:
        issues.append(
            _failed(
                "capability_architecture_mismatch", "capability architecture differs"
            )
        )
    if (
        capability is not None
        and capability.codex_version != request.expected_codex_version
    ):
        issues.append(
            _failed(
                "capability_version_mismatch", "capability version differs from pin"
            )
        )
    expected_backend = expected_sandbox_backend(request.platform)
    if capability is not None and capability.sandbox_backend is not expected_backend:
        issues.append(
            _failed(
                "capability_sandbox_mismatch",
                f"expected {expected_backend.value}, got {capability.sandbox_backend.value}",
            )
        )
    if capability is not None and capability.status is CapabilityStatus.PENDING:
        issues.append(
            _pending(
                "platform_capability_pending",
                "this concrete host has no passing conformance evidence",
            )
        )
    elif capability is not None and capability.status is CapabilityStatus.FAILED:
        issues.append(
            _failed(
                "platform_capability_failed",
                "; ".join(capability.failures) or "platform capability failed",
            )
        )
    elif capability is not None and not capability.authorizes_model_turns:
        issues.append(
            _failed(
                "platform_capability_incomplete",
                "supported capability report lacks required launch-security proofs",
            )
        )

    if observation.platform is not request.platform:
        issues.append(
            _failed("observed_platform_mismatch", "observed platform differs")
        )
    if observation.architecture != request.architecture:
        issues.append(
            _failed("observed_architecture_mismatch", "observed architecture differs")
        )
    if observation.codex_version != request.expected_codex_version:
        issues.append(
            _failed(
                "codex_version_mismatch",
                f"expected {request.expected_codex_version!r}, got {observation.codex_version!r}",
            )
        )
    if observation.schema_sha256 != request.expected_schema_sha256:
        issues.append(_failed("schema_mismatch", "App Server schema sha256 differs"))
    if observation.sandbox_backend is not expected_backend:
        issues.append(
            _failed(
                "sandbox_backend_mismatch",
                f"expected {expected_backend.value}, got {observation.sandbox_backend.value}",
            )
        )
    if request.permission_profile not in observation.available_permission_profiles:
        issues.append(
            _failed(
                "permission_profile_missing", "strict permission profile is unavailable"
            )
        )
    if observation.active_permission_profile != request.permission_profile:
        issues.append(
            _failed(
                "permission_profile_inactive", "strict permission profile is not active"
            )
        )
    if observation.instruction_sources:
        issues.append(
            _failed(
                "instruction_sources_nonempty",
                f"unexpected instruction sources: {sorted(observation.instruction_sources)}",
            )
        )

    for code, observed, allowed in (
        ("unexpected_skills", observation.skills, request.allowed_skills),
        (
            "unexpected_mcp_servers",
            observation.mcp_servers,
            request.allowed_mcp_servers,
        ),
        ("unexpected_apps", observation.apps, request.allowed_apps),
    ):
        extras = _unexpected(observed, allowed)
        if extras:
            issues.append(_failed(code, f"unexpected entries: {list(extras)}"))

    allowed_destinations = set(request.allowed_command_network_destinations)
    observed_destinations = set(observation.command_network_destinations)
    if observation.command_network_enabled:
        if not allowed_destinations:
            issues.append(_failed("network_on", "sandboxed command network is enabled"))
        if not observation.command_network_proxy_active:
            issues.append(
                _failed(
                    "network_proxy_missing",
                    "enabled command network lacks proxy enforcement",
                )
            )
        extras = sorted(observed_destinations - allowed_destinations)
        if extras:
            issues.append(
                _failed(
                    "network_destination_unexpected",
                    f"unexpected destinations: {extras}",
                )
            )
    elif observed_destinations:
        issues.append(
            _failed(
                "network_destination_without_network",
                "network destinations were reported while command network is disabled",
            )
        )

    if observation.effective_credential_store is CredentialStore.AUTO:
        issues.append(
            _failed(
                "effective_credential_auto", "effective credential store is still auto"
            )
        )
    if observation.effective_credential_store is not request.credential_store:
        issues.append(
            _failed("credential_store_mismatch", "effective credential store differs")
        )

    expected_host_home = (
        request.roots.user_home
        if request.platform is PlatformFamily.MACOS
        and request.credential_store is CredentialStore.KEYRING
        else request.roots.home
    )
    if observation.host_home != expected_host_home:
        issues.append(
            _failed(
                "host_home_mismatch",
                "App Server host HOME differs from the authorized auth HOME",
            )
        )
    if observation.tool_home != request.roots.home:
        issues.append(
            _failed(
                "tool_home_mismatch",
                "tool subprocess HOME differs from the isolated product HOME",
            )
        )
    if observation.tool_codex_home is not None:
        issues.append(
            _failed(
                "tool_codex_home_exposed",
                "tool subprocess environment exposed CODEX_HOME",
            )
        )
    if observation.web_search_enabled:
        issues.append(
            _failed(
                "model_web_search_enabled",
                "model-side web search is enabled outside the v1 tool policy",
            )
        )
    if request.platform is PlatformFamily.WINDOWS:
        if observation.windows_sandbox_mode != "elevated":
            issues.append(
                _failed(
                    "unsafe_windows_observation",
                    "native Windows did not report the elevated sandbox",
                )
            )
    elif observation.windows_sandbox_mode is not None:
        issues.append(
            _failed(
                "unexpected_windows_observation",
                "non-Windows host reported a Windows sandbox mode",
            )
        )
    return _result("model_turn", issues)


def build_app_server_command(
    request: LaunchRequest,
    *,
    path_states: Mapping[str, PathState] | None = None,
) -> AppServerCommand:
    """Build an argv/env specification; never invoke a shell or a subprocess.

    This command starts App Server only so the caller can gather observations.
    It does not authorize ``turn/start``; callers must separately require a
    successful :func:`evaluate_launch` result.
    """

    result = evaluate_process_start(request, path_states=path_states)
    if not result.allowed:
        raise LaunchBlocked(result)
    workspace_temp = _join(request.platform, request.roots.workspace, ".runtime_tmp")
    executable_parent = str(
        _pure_path(request.platform, request.app_server_executable).parent
    )
    host_home = (
        request.roots.user_home
        if request.platform is PlatformFamily.MACOS
        and request.credential_store is CredentialStore.KEYRING
        else request.roots.home
    )
    if request.platform is PlatformFamily.WINDOWS:
        controlled_path = ";".join(
            (
                executable_parent,
                *request.tool_path_entries,
                r"C:\Windows\System32",
                r"C:\Windows",
            )
        )
        environment = {
            "HOME": host_home,
            "USERPROFILE": host_home,
            "CODEX_HOME": request.roots.codex_home,
            "TEMP": workspace_temp,
            "TMP": workspace_temp,
            "PATH": controlled_path,
        }
    else:
        controlled_path = ":".join(
            (
                executable_parent,
                *request.tool_path_entries,
                "/usr/bin",
                "/bin",
                "/usr/sbin",
                "/sbin",
            )
        )
        environment = {
            "HOME": host_home,
            "CODEX_HOME": request.roots.codex_home,
            "TMPDIR": workspace_temp,
            "PATH": controlled_path,
            "LANG": "C.UTF-8",
        }
    tool_environment = {
        "HOME": request.roots.home,
        (
            "TEMP" if request.platform is PlatformFamily.WINDOWS else "TMPDIR"
        ): workspace_temp,
    }
    if request.platform is PlatformFamily.WINDOWS:
        tool_environment["USERPROFILE"] = request.roots.home
        tool_environment["TMP"] = workspace_temp
    config_overrides = [
        'shell_environment_policy.inherit="core"',
        "shell_environment_policy.ignore_default_excludes=false",
        'shell_environment_policy.filters.CODEX_HOME="exclude"',
        "shell_environment_policy.experimental_use_profile=false",
        ("cli_auth_credentials_store=" + json.dumps(request.credential_store.value)),
        "default_permissions=" + json.dumps(request.permission_profile),
    ]
    config_overrides.extend(
        "shell_environment_policy.set." + key + "=" + json.dumps(value)
        for key, value in sorted(tool_environment.items())
    )
    argv = [
        request.app_server_executable,
        "app-server",
        "--strict-config",
    ]
    for override in config_overrides:
        argv.extend(("-c", override))
    argv.extend(("--listen", "stdio://"))
    return AppServerCommand(
        argv=tuple(argv),
        cwd=request.roots.workspace,
        environment=environment,
    )
