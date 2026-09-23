"""Versioned runtime capability profiles outside the frozen scientific Record."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


@dataclass(frozen=True)
class CapabilityProfile:
    """Resolved experimental conditions for one derivation runtime.

    The Engine sees only the profile identity and hash. Provider-specific
    translation stays in the runtime adapter.
    """

    profile_id: str
    version: int
    permission_profile: str
    allowed_tools: tuple[str, ...]
    network_access: bool
    include_project_instructions: bool
    include_skills: bool
    web_search: bool
    mcp_servers: tuple[str, ...]
    apps_enabled: bool
    scientific_runtime_id: str | None

    def __post_init__(self) -> None:
        for name, value in (
            ("profile_id", self.profile_id),
            ("permission_profile", self.permission_profile),
        ):
            if not _IDENTIFIER.fullmatch(value):
                raise ValueError(f"{name} must be a lowercase portable identifier")
        if self.version < 1:
            raise ValueError("capability profile version must be positive")
        for name, values in (
            ("allowed_tools", self.allowed_tools),
            ("mcp_servers", self.mcp_servers),
        ):
            if tuple(sorted(set(values))) != values:
                raise ValueError(f"{name} must be sorted and unique")
            if any(not _IDENTIFIER.fullmatch(value) for value in values):
                raise ValueError(f"{name} contains an invalid identifier")
        if self.scientific_runtime_id is not None and not _IDENTIFIER.fullmatch(
            self.scientific_runtime_id
        ):
            raise ValueError("scientific_runtime_id must be a portable identifier")
        if self.web_search and not self.network_access:
            raise ValueError("web_search requires network_access")

    @property
    def identity(self) -> str:
        return f"{self.profile_id}_v{self.version}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "version": self.version,
            "permission_profile": self.permission_profile,
            "allowed_tools": list(self.allowed_tools),
            "network_access": self.network_access,
            "include_project_instructions": self.include_project_instructions,
            "include_skills": self.include_skills,
            "web_search": self.web_search,
            "mcp_servers": list(self.mcp_servers),
            "apps_enabled": self.apps_enabled,
            "scientific_runtime_id": self.scientific_runtime_id,
        }

    @property
    def sha256(self) -> str:
        encoded = json.dumps(
            self.to_dict(), sort_keys=True, separators=(",", ":")
        ).encode()
        return hashlib.sha256(encoded).hexdigest()

    def app_server_config(self) -> dict[str, Any]:
        return {
            # Built-in command/code tools are disabled for every DerivationLab
            # capability. Scientific computation is exposed only through the
            # separately declared, reviewed dynamic tool.
            "features": {
                "code_mode_host": False,
                "js_repl": False,
                "shell_tool": False,
                "unified_exec": False,
            },
            "project_doc_max_bytes": (
                32_768 if self.include_project_instructions else 0
            ),
            "skills": {"include_instructions": self.include_skills},
            "mcp_servers": {server: {"enabled": True} for server in self.mcp_servers},
            "web_search": "live" if self.web_search else "disabled",
            "apps": {
                "_default": {
                    "enabled": self.apps_enabled,
                    "open_world_enabled": self.apps_enabled,
                    "destructive_enabled": False,
                }
            },
        }


BENCHMARK_SYMBOLIC_V1 = CapabilityProfile(
    profile_id="benchmark_symbolic",
    version=1,
    permission_profile="strict_run_workspace",
    allowed_tools=("scientific_compute",),
    network_access=False,
    include_project_instructions=False,
    include_skills=False,
    web_search=False,
    mcp_servers=(),
    apps_enabled=False,
    scientific_runtime_id="sympy_uv",
)

# Internal pre-Run profile.  It is intentionally absent from
# BUILTIN_CAPABILITY_PROFILES because a DerivationRun may not select it.
INTAKE_V1 = CapabilityProfile(
    profile_id="intake",
    version=1,
    permission_profile="strict_run_workspace",
    allowed_tools=(),
    network_access=False,
    include_project_instructions=False,
    include_skills=False,
    web_search=False,
    mcp_servers=(),
    apps_enabled=False,
    scientific_runtime_id=None,
)

BUILTIN_CAPABILITY_PROFILES = {
    BENCHMARK_SYMBOLIC_V1.identity: BENCHMARK_SYMBOLIC_V1,
}

# Both literature conditions use this identical tool declaration. An empty
# SourceLibrary denies all reads/searches and exposes no literature metadata.
SOURCE_READING_V1 = CapabilityProfile(
    profile_id="source_reading",
    version=1,
    permission_profile="strict_run_workspace",
    allowed_tools=(
        "feedback_read",
        "scientific_compute",
        "source_catalog",
        "source_read",
        "source_search",
        "transcript_catalog",
        "transcript_read",
    ),
    network_access=False,
    include_project_instructions=False,
    include_skills=False,
    web_search=False,
    mcp_servers=(),
    apps_enabled=False,
    scientific_runtime_id="sympy_uv",
)
BUILTIN_CAPABILITY_PROFILES[SOURCE_READING_V1.identity] = SOURCE_READING_V1


def resolve_capability_profile(identity: str) -> CapabilityProfile:
    try:
        return BUILTIN_CAPABILITY_PROFILES[identity]
    except KeyError as exc:
        raise ValueError(f"unknown capability profile {identity!r}") from exc


__all__ = [
    "BENCHMARK_SYMBOLIC_V1",
    "BUILTIN_CAPABILITY_PROFILES",
    "INTAKE_V1",
    "SOURCE_READING_V1",
    "CapabilityProfile",
    "resolve_capability_profile",
]
