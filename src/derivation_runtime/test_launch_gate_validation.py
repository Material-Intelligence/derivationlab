from __future__ import annotations

import unittest
from dataclasses import replace
from pathlib import Path

from derivation_runtime.launch_gate import (
    AppServerCommand,
    CredentialStore,
    GateStatus,
    LaunchBlocked,
    LaunchRequest,
    PathState,
    RuntimeObservation,
    RuntimeRoots,
    build_app_server_command,
    evaluate_launch,
    evaluate_process_start,
)
from derivation_runtime.platform_policy import (
    PINNED_CODEX_VERSION,
    PINNED_V2_SCHEMA_SHA256,
    PlatformFamily,
    SandboxBackend,
    current_macos_capability,
    failed_colima_linux_capability,
    pending_linux_capability,
)

ROOT = Path(__file__).resolve().parents[2]


def mac_request() -> LaunchRequest:
    return LaunchRequest(
        platform=PlatformFamily.MACOS,
        architecture="arm64",
        app_server_executable="/opt/derivation-lab/bin/codex",
        roots=RuntimeRoots(
            home="/opt/derivation-lab/app-home",
            codex_home="/opt/derivation-lab/codex-home",
            workspace="/opt/derivation-lab/runs/run-1",
            runtime="/opt/derivation-lab/runtime",
            repository=str(ROOT),
            user_home="/Users/researcher",
        ),
        expected_codex_version=PINNED_CODEX_VERSION,
        expected_schema_sha256=PINNED_V2_SCHEMA_SHA256,
        credential_store=CredentialStore.KEYRING,
    )


def states_for(request: LaunchRequest) -> dict[str, PathState]:
    return {
        name: PathState(
            requested=value,
            resolved=value,
            exists=True,
            is_directory=True,
        )
        for name, value in request.roots.all_items()
    }


def mac_observation() -> RuntimeObservation:
    return RuntimeObservation(
        platform=PlatformFamily.MACOS,
        architecture="arm64",
        codex_version=PINNED_CODEX_VERSION,
        schema_sha256=PINNED_V2_SCHEMA_SHA256,
        sandbox_backend=SandboxBackend.SEATBELT,
        active_permission_profile="strict_run_workspace",
        available_permission_profiles=("strict_run_workspace",),
        instruction_sources=(),
        skills=(),
        mcp_servers=(),
        apps=(),
        command_network_enabled=False,
        command_network_proxy_active=False,
        command_network_destinations=(),
        effective_credential_store=CredentialStore.KEYRING,
        host_home="/Users/researcher",
        tool_home="/opt/derivation-lab/app-home",
        tool_codex_home=None,
        web_search_enabled=False,
    )


def issue_codes(result) -> set[str]:
    return {issue.code for issue in result.issues}


class LaunchGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.request = mac_request()
        self.states = states_for(self.request)
        self.observation = mac_observation()
        self.capability = current_macos_capability(
            ROOT,
            architecture="arm64",
            codex_version=PINNED_CODEX_VERSION,
        )

    def evaluate(self, *, request=None, observation=None, capability=None, states=None):
        return evaluate_launch(
            request or self.request,
            observation or self.observation,
            capability or self.capability,
            path_states=states or self.states,
        )

    def test_positive_current_macos_configuration_is_authorized(self) -> None:
        result = self.evaluate()
        self.assertEqual(result.status, GateStatus.SUPPORTED)
        self.assertTrue(result.allowed)
        self.assertEqual(result.issues, ())

    def test_rejects_relative_and_missing_paths(self) -> None:
        request = replace(
            self.request,
            roots=replace(self.request.roots, workspace="relative/run-1"),
        )
        states = states_for(request)
        states["runtime"] = replace(states["runtime"], exists=False)
        result = self.evaluate(request=request, states=states)
        self.assertEqual(result.status, GateStatus.FAILED)
        self.assertIn("path_not_absolute", issue_codes(result))
        self.assertIn("path_missing", issue_codes(result))

    def test_rejects_overlapping_and_repository_exposing_roots(self) -> None:
        request = replace(
            self.request,
            roots=replace(
                self.request.roots,
                codex_home="/opt/derivation-lab/app-home/codex",
                workspace=str(ROOT / "unsafe-run"),
            ),
        )
        observation = replace(
            self.observation,
            tool_home=request.roots.home,
        )
        result = self.evaluate(
            request=request,
            observation=observation,
            states=states_for(request),
        )
        self.assertIn("isolated_roots_overlap", issue_codes(result))
        self.assertIn("repository_exposed", issue_codes(result))

    def test_rejects_resolved_symlink_escape_into_repository(self) -> None:
        states = states_for(self.request)
        states["workspace"] = replace(
            states["workspace"], resolved=str(ROOT / "escaped-workspace")
        )
        result = self.evaluate(states=states)
        self.assertIn("repository_exposed", issue_codes(result))

    def test_rejects_broad_real_user_home_exposure(self) -> None:
        request = replace(
            self.request,
            roots=replace(self.request.roots, home="/Users"),
        )
        result = self.evaluate(request=request, states=states_for(request))
        self.assertIn("user_home_exposed", issue_codes(result))

    def test_allows_dedicated_directory_below_real_user_home(self) -> None:
        request = replace(
            self.request,
            roots=replace(
                self.request.roots,
                home="/Users/researcher/Library/Application Support/Derivation/home",
            ),
        )
        result = self.evaluate(
            request=request,
            observation=replace(
                self.observation,
                tool_home=request.roots.home,
            ),
            states=states_for(request),
        )
        self.assertEqual(result.status, GateStatus.SUPPORTED)

    def test_rejects_auto_credentials_and_context_or_tool_leaks(self) -> None:
        request = replace(self.request, credential_store=CredentialStore.AUTO)
        observation = replace(
            self.observation,
            effective_credential_store=CredentialStore.AUTO,
            instruction_sources=("AGENTS.md",),
            skills=("skill-creator",),
            mcp_servers=("filesystem",),
            apps=("google-drive",),
        )
        result = self.evaluate(request=request, observation=observation)
        self.assertTrue(
            {
                "credential_auto_forbidden",
                "effective_credential_auto",
                "instruction_sources_nonempty",
                "unexpected_skills",
                "unexpected_mcp_servers",
                "unexpected_apps",
            }.issubset(issue_codes(result))
        )

    def test_rejects_network_on_or_unproxied_allowlist(self) -> None:
        observation = replace(
            self.observation,
            command_network_enabled=True,
            command_network_proxy_active=False,
            command_network_destinations=("api.openai.com",),
        )
        result = self.evaluate(observation=observation)
        self.assertTrue(
            {
                "network_on",
                "network_proxy_missing",
                "network_destination_unexpected",
            }.issubset(issue_codes(result))
        )

    def test_rejects_host_tool_home_and_model_web_search_drift(self) -> None:
        observation = replace(
            self.observation,
            host_home=self.request.roots.home,
            tool_home=self.request.roots.user_home,
            tool_codex_home=self.request.roots.codex_home,
            web_search_enabled=True,
        )
        result = self.evaluate(observation=observation)
        self.assertTrue(
            {
                "host_home_mismatch",
                "tool_home_mismatch",
                "tool_codex_home_exposed",
                "model_web_search_enabled",
            }.issubset(issue_codes(result))
        )

    def test_rejects_missing_profile_version_and_schema_drift(self) -> None:
        observation = replace(
            self.observation,
            codex_version="0.148.0",
            schema_sha256="0" * 64,
            active_permission_profile=None,
            available_permission_profiles=(),
        )
        result = self.evaluate(observation=observation)
        self.assertTrue(
            {
                "codex_version_mismatch",
                "schema_mismatch",
                "permission_profile_missing",
                "permission_profile_inactive",
            }.issubset(issue_codes(result))
        )

    def test_rejects_request_side_version_and_schema_repinning(self) -> None:
        request = replace(
            self.request,
            expected_codex_version="0.148.0",
            expected_schema_sha256="0" * 64,
        )
        result = evaluate_process_start(request, path_states=states_for(request))
        self.assertEqual(result.status, GateStatus.FAILED)
        self.assertTrue(
            {"version_pin_mismatch", "schema_pin_mismatch"}.issubset(
                issue_codes(result)
            )
        )

    def test_pending_and_failed_linux_hosts_never_authorize_turns(self) -> None:
        request = replace(
            self.request,
            platform=PlatformFamily.LINUX,
            app_server_executable="/opt/derivation-lab/bin/codex",
        )
        observation = replace(
            self.observation,
            platform=PlatformFamily.LINUX,
            sandbox_backend=SandboxBackend.BUBBLEWRAP_SECCOMP,
            host_home=request.roots.home,
        )
        pending = self.evaluate(
            request=request,
            observation=observation,
            capability=pending_linux_capability(
                architecture="arm64", codex_version=PINNED_CODEX_VERSION
            ),
            states=states_for(request),
        )
        failed = self.evaluate(
            request=request,
            observation=observation,
            capability=failed_colima_linux_capability(),
            states=states_for(request),
        )
        self.assertEqual(pending.status, GateStatus.PENDING)
        self.assertEqual(failed.status, GateStatus.FAILED)
        self.assertFalse(pending.allowed)
        self.assertFalse(failed.allowed)

    def test_supported_label_without_required_proofs_fails_closed(self) -> None:
        incomplete = replace(self.capability, proven_checks=())
        result = self.evaluate(capability=incomplete)
        self.assertEqual(result.status, GateStatus.FAILED)
        self.assertIn("platform_capability_incomplete", issue_codes(result))

    def test_windows_unelevated_claim_is_rejected(self) -> None:
        request = replace(
            self.request,
            platform=PlatformFamily.WINDOWS,
            architecture="AMD64",
            app_server_executable=r"C:\Program Files\Codex\codex.exe",
            roots=RuntimeRoots(
                home=r"C:\ProgramData\Derivation\home",
                codex_home=r"C:\ProgramData\Derivation\codex-home",
                workspace=r"C:\ProgramData\Derivation\runs\run-1",
                runtime=r"C:\ProgramData\Derivation\runtime",
                repository=r"D:\work\derivationlab",
                user_home=r"C:\Users\Scientist",
            ),
            windows_sandbox_mode="unelevated",
        )
        result = evaluate_process_start(request, path_states=states_for(request))
        self.assertEqual(result.status, GateStatus.FAILED)
        self.assertIn("unsafe_windows_fallback", issue_codes(result))

    def test_command_builder_binds_keyring_host_and_isolated_tool_home(self) -> None:
        command = build_app_server_command(self.request, path_states=self.states)
        self.assertIsInstance(command, AppServerCommand)
        self.assertFalse(command.shell)
        self.assertFalse(command.inherit_parent_environment)
        self.assertEqual(
            command.argv,
            (
                "/opt/derivation-lab/bin/codex",
                "app-server",
                "--strict-config",
                "-c",
                'shell_environment_policy.inherit="core"',
                "-c",
                "shell_environment_policy.ignore_default_excludes=false",
                "-c",
                'shell_environment_policy.filters.CODEX_HOME="exclude"',
                "-c",
                "shell_environment_policy.experimental_use_profile=false",
                "-c",
                'cli_auth_credentials_store="keyring"',
                "-c",
                'default_permissions="strict_run_workspace"',
                "-c",
                'shell_environment_policy.set.HOME="/opt/derivation-lab/app-home"',
                "-c",
                (
                    "shell_environment_policy.set.TMPDIR="
                    '"/opt/derivation-lab/runs/run-1/.runtime_tmp"'
                ),
                "--listen",
                "stdio://",
            ),
        )
        self.assertEqual(command.environment["HOME"], self.request.roots.user_home)
        self.assertEqual(
            command.environment["CODEX_HOME"], self.request.roots.codex_home
        )
        self.assertTrue(
            command.environment["PATH"].startswith("/opt/derivation-lab/bin:")
        )
        self.assertNotIn("OPENAI_API_KEY", command.environment)

    def test_file_credentials_keep_isolated_host_home(self) -> None:
        request = replace(
            self.request,
            credential_store=CredentialStore.FILE,
        )
        command = build_app_server_command(request, path_states=self.states)
        self.assertEqual(command.environment["HOME"], request.roots.home)
        self.assertIn('cli_auth_credentials_store="file"', command.argv)
        self.assertIn(
            'shell_environment_policy.set.HOME="/opt/derivation-lab/app-home"',
            command.argv,
        )
        self.assertNotIn(
            "CODEX_HOME",
            "".join(
                part
                for index, part in enumerate(command.argv)
                if index > 1 and "filters.CODEX_HOME" not in part
            ),
        )

    def test_command_builder_rejects_unsafe_request(self) -> None:
        request = replace(self.request, credential_store=CredentialStore.AUTO)
        with self.assertRaises(LaunchBlocked):
            build_app_server_command(request, path_states=states_for(request))

    def test_windows_command_builder_uses_native_paths_without_shell(self) -> None:
        request = LaunchRequest(
            platform=PlatformFamily.WINDOWS,
            architecture="AMD64",
            app_server_executable=r"C:\Program Files\Codex\codex.exe",
            roots=RuntimeRoots(
                home=r"C:\ProgramData\Derivation\home",
                codex_home=r"C:\ProgramData\Derivation\codex-home",
                workspace=r"C:\ProgramData\Derivation\runs\run-1",
                runtime=r"C:\ProgramData\Derivation\runtime",
                repository=r"D:\work\derivationlab",
                user_home=r"C:\Users\Scientist",
            ),
            expected_codex_version=PINNED_CODEX_VERSION,
            expected_schema_sha256=PINNED_V2_SCHEMA_SHA256,
            credential_store=CredentialStore.KEYRING,
            windows_sandbox_mode="elevated",
        )
        command = build_app_server_command(request, path_states=states_for(request))
        self.assertFalse(command.shell)
        self.assertEqual(command.argv[0], r"C:\Program Files\Codex\codex.exe")
        self.assertEqual(command.environment["USERPROFILE"], request.roots.home)
        self.assertTrue(
            command.environment["PATH"].startswith(r"C:\Program Files\Codex;")
        )
        self.assertTrue(command.environment["TEMP"].endswith(".runtime_tmp"))


if __name__ == "__main__":
    unittest.main()
