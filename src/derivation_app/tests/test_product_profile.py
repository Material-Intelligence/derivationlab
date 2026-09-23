from __future__ import annotations

import hashlib
import json
import os
import platform
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import tomllib

from derivation_app.product_profile import (
    ProductProfile,
    ProfileConflict,
    ProfileInstanceLock,
    ProfileLockHeld,
    ProfileMode,
    _permission_contract_sha256,
    authorize_model_turn,
    prepare_holder_workspace,
    prepare_intake_workspace,
    prepare_product_launch,
    prepare_run_workspace,
    prepare_shared_run_launch,
    provision_product_profile,
    validate_product_profile,
)
from derivation_runtime.app_server_client import SpawnedProcessIdentity
from derivation_runtime.capabilities import INTAKE_V1
from derivation_runtime.launch_gate import CredentialStore, RuntimeObservation
from derivation_runtime.platform_policy import (
    PINNED_CODEX_VERSION,
    PINNED_V2_SCHEMA_SHA256,
    PlatformFamily,
    expected_sandbox_backend,
)
from derivation_runtime.scientific_runtime import (
    PACKAGE_VERSIONS,
    PYTHON_VERSION,
    REQUIREMENTS_LOCK,
    RUNTIME_ID,
    RUNTIME_SCHEMA,
)
from derivation_runtime.types import RuntimeInvariantError


def host_platform() -> PlatformFamily:
    return {
        "Darwin": PlatformFamily.MACOS,
        "Linux": PlatformFamily.LINUX,
        "Windows": PlatformFamily.WINDOWS,
    }[platform.system()]


def provision_fake_scientific_runtime(profile: ProductProfile) -> None:
    relative_python = (
        Path("env/Scripts/python.exe") if os.name == "nt" else Path("env/bin/python")
    )
    python = profile.runtime / relative_python
    python.parent.mkdir(parents=True, exist_ok=True)
    python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    if os.name != "nt":
        python.chmod(0o500)
    digest = hashlib.sha256(REQUIREMENTS_LOCK.read_bytes()).hexdigest()
    (profile.runtime / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": RUNTIME_SCHEMA,
                "runtime_id": RUNTIME_ID,
                "python_version": PYTHON_VERSION,
                "packages": PACKAGE_VERSIONS,
                "python_relative_path": relative_python.as_posix(),
                "requirements_sha256": digest,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )


class ProductProfileTests(unittest.TestCase):
    def layout(self, *, mode: ProfileMode = ProfileMode.PRODUCTION):
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        repository = root / "repo"
        repository.mkdir()
        (repository / "runs").mkdir()
        profile = ProductProfile.below(root / "profile", mode=mode)
        return temporary, repository, profile

    def test_provision_is_idempotent_and_auth_file_is_reusable(self) -> None:
        temporary, repository, profile = self.layout()
        self.addCleanup(temporary.cleanup)

        initial = provision_product_profile(profile, repo_root=repository)
        self.assertFalse(initial.file_credentials_validated)
        self.assertIsNone(initial.scientific_runtime)
        auth = profile.codex_home / "auth.json"
        auth.write_text(json.dumps({"auth_mode": "chatgpt"}), encoding="utf-8")
        if os.name != "nt":
            auth.chmod(0o600)

        validated = provision_product_profile(profile, repo_root=repository)
        self.assertTrue(validated.file_credentials_validated)
        self.assertEqual(
            validated, validate_product_profile(profile, repo_root=repository)
        )
        self.assertNotIn("auth.json", validated.config_path.read_text(encoding="utf-8"))

    def test_native_project_trust_entries_do_not_invalidate_profile(self) -> None:
        temporary, repository, profile = self.layout()
        self.addCleanup(temporary.cleanup)
        initial = provision_product_profile(profile, repo_root=repository)
        with initial.config_path.open("a", encoding="utf-8") as handle:
            handle.write(
                '\n[projects."/native/codex/workspace"]\ntrust_level = "trusted"\n'
            )

        validated = validate_product_profile(profile, repo_root=repository)

        self.assertEqual(validated.credential_store, CredentialStore.FILE)

    def test_a_new_project_trust_entry_keeps_the_authorization_snapshot_valid(
        self,
    ) -> None:
        """The App Server rewrite that a second concurrent run causes.

        Every ``create_run`` on a shared child hands the App Server a new
        workspace, and the child appends a ``[projects]`` trust entry for it.
        Authorization compares a whole :class:`ProfileValidation` for equality,
        so if that rewrite moved ``config_sha256`` the second run would be
        refused as if the permission contract had changed.
        """

        temporary, repository, profile = self.layout()
        self.addCleanup(temporary.cleanup)
        provision_product_profile(profile, repo_root=repository)
        auth = profile.codex_home / "auth.json"
        auth.write_text(json.dumps({"auth_mode": "chatgpt"}), encoding="utf-8")
        if os.name != "nt":
            auth.chmod(0o600)

        before = validate_product_profile(profile, repo_root=repository)
        raw_before = hashlib.sha256(before.config_path.read_bytes()).hexdigest()
        workspace = profile.workspaces / "run_second"
        with before.config_path.open("a", encoding="utf-8") as handle:
            handle.write(f'\n[projects."{workspace}"]\ntrust_level = "trusted"\n')
        after = validate_product_profile(profile, repo_root=repository)
        raw_after = hashlib.sha256(after.config_path.read_bytes()).hexdigest()

        self.assertNotEqual(raw_before, raw_after)
        self.assertEqual(before.config_sha256, after.config_sha256)
        self.assertEqual(before, after)

    def test_a_widened_permission_block_is_still_refused(self) -> None:
        temporary, repository, profile = self.layout()
        self.addCleanup(temporary.cleanup)
        validated = provision_product_profile(profile, repo_root=repository)
        original = validated.config_path.read_text(encoding="utf-8")
        widened = original.replace(
            "[permissions.strict_run_workspace.network]\nenabled = false",
            "[permissions.strict_run_workspace.network]\nenabled = true",
        )
        self.assertNotEqual(original, widened)
        validated.config_path.write_text(widened, encoding="utf-8")
        if os.name != "nt":
            validated.config_path.chmod(0o600)

        with self.assertRaisesRegex(ProfileConflict, "incompatible"):
            validate_product_profile(profile, repo_root=repository)

    def test_the_config_digest_separates_permission_contracts_only(self) -> None:
        temporary, repository, profile = self.layout()
        self.addCleanup(temporary.cleanup)
        validated = provision_product_profile(profile, repo_root=repository)
        contract = tomllib.loads(validated.config_path.read_text(encoding="utf-8"))
        contract.pop("projects", None)
        widened = json.loads(json.dumps(contract))
        widened["permissions"]["strict_run_workspace"]["network"]["enabled"] = True
        reordered = dict(reversed(list(contract.items())))

        self.assertEqual(validated.config_sha256, _permission_contract_sha256(contract))
        self.assertNotEqual(
            _permission_contract_sha256(contract),
            _permission_contract_sha256(widened),
        )
        self.assertEqual(
            _permission_contract_sha256(contract),
            _permission_contract_sha256(reordered),
        )
        with self.assertRaisesRegex(RuntimeInvariantError, r"\[projects\]"):
            _permission_contract_sha256(
                {**contract, "projects": {"/somewhere": {"trust_level": "trusted"}}}
            )

    def test_two_shared_runs_stay_authorized_across_a_trust_table_rewrite(
        self,
    ) -> None:
        """Reproduce the concurrency-4 blocker at the authorization predicate.

        ``prepare_shared_run_launch`` hands the holder's snapshot to every run,
        and ``collect_and_authorize_model_turn`` refuses the run unless a fresh
        validation still equals it.  The App Server appends its trust entry
        between the two launches, which is the only thing that changed.
        """

        temporary, repository, profile = self.layout()
        self.addCleanup(temporary.cleanup)
        provision_product_profile(profile, repo_root=repository)
        provision_fake_scientific_runtime(profile)
        auth = profile.codex_home / "auth.json"
        auth.write_text(json.dumps({"auth_mode": "chatgpt"}), encoding="utf-8")
        if os.name != "nt":
            auth.chmod(0o600)
        lock = ProfileInstanceLock(profile).acquire()
        self.addCleanup(lambda: lock.release() if lock.held else None)
        family = host_platform()
        holder = prepare_product_launch(
            profile,
            lock=lock,
            repo_root=repository,
            platform=family,
            architecture=platform.machine(),
            app_server_executable=Path(sys.executable),
            workspace_mapping=prepare_holder_workspace(
                profile, holder_id="holder_fixture", repo_root=repository
            ),
            repository=repository,
            user_home=Path.home(),
            windows_sandbox_mode="elevated"
            if family is PlatformFamily.WINDOWS
            else None,
        )

        def launch(run_id: str):
            evidence = repository / "runs" / run_id
            evidence.mkdir()
            return prepare_shared_run_launch(
                holder,
                run_mapping=prepare_run_workspace(
                    profile,
                    run_id=run_id,
                    evidence_directory=evidence,
                    repo_root=repository,
                ),
                repo_root=repository,
            )

        first = launch("run_alpha")
        # What the child does the moment it is handed the first run workspace.
        config_path = profile.codex_home / "config.toml"
        with config_path.open("a", encoding="utf-8") as handle:
            handle.write(
                f'\n[projects."{first.workspace_mapping.sandbox_workspace}"]\n'
                'trust_level = "trusted"\n'
            )
        second = launch("run_bravo")
        live = validate_product_profile(profile, repo_root=repository)

        self.assertEqual(live, first.validation)
        self.assertEqual(live, second.validation)
        self.assertTrue(live.file_credentials_validated)

    def test_keyring_is_not_a_hidden_platform_specific_v1_branch(self) -> None:
        temporary, repository, _ = self.layout()
        self.addCleanup(temporary.cleanup)
        profile = ProductProfile.below(
            Path(temporary.name) / "keyring",
            credential_store=CredentialStore.KEYRING,
        )
        with self.assertRaisesRegex(ProfileConflict, "future work"):
            provision_product_profile(profile, repo_root=repository)

    def test_auth_json_requires_private_posix_mode(self) -> None:
        if os.name == "nt":
            self.skipTest("POSIX modes do not apply on Windows")
        temporary, repository, profile = self.layout()
        self.addCleanup(temporary.cleanup)
        provision_product_profile(profile, repo_root=repository)
        auth = profile.codex_home / "auth.json"
        auth.write_text("{}", encoding="utf-8")
        auth.chmod(0o644)
        with self.assertRaisesRegex(ProfileConflict, "0600"):
            validate_product_profile(profile, repo_root=repository)

    def test_instance_lock_is_single_backend_and_recoverable(self) -> None:
        temporary, repository, profile = self.layout()
        self.addCleanup(temporary.cleanup)
        provision_product_profile(profile, repo_root=repository)
        first = ProfileInstanceLock(profile).acquire()
        self.addCleanup(lambda: first.release() if first.held else None)
        with self.assertRaises(ProfileLockHeld):
            ProfileInstanceLock(profile).acquire()
        first.release()
        second = ProfileInstanceLock(profile).acquire()
        second.release()

    @unittest.skipUnless(os.name == "posix", "POSIX dead owner recovery")
    def test_instance_lock_recovers_verified_dead_process(self) -> None:
        temporary, repository, profile = self.layout()
        self.addCleanup(temporary.cleanup)
        provision_product_profile(profile, repo_root=repository)
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        child.wait(timeout=10)
        lock = ProfileInstanceLock(profile)
        lock.path.write_text(f"pid={child.pid}\n")
        lock.path.chmod(0o600)
        lock.acquire()
        self.assertEqual(lock.path.read_text(), f"pid={os.getpid()}\n")
        lock.release()

    @unittest.skipUnless(os.name == "posix", "POSIX dead owner recovery")
    def test_instance_lock_never_reclaims_uncertain_owner(self) -> None:
        temporary, repository, profile = self.layout()
        self.addCleanup(temporary.cleanup)
        provision_product_profile(profile, repo_root=repository)
        lock = ProfileInstanceLock(profile)
        for content in (
            "",
            "pid=0\n",
            "pid=-1\n",
            "pid=unknown\n",
            f"pid={os.getpid()}\n",
        ):
            with self.subTest(content=content):
                lock.path.write_text(content)
                lock.path.chmod(0o600)
                with self.assertRaises(ProfileLockHeld):
                    lock.acquire()
                self.assertEqual(lock.path.read_text(), content)
        with (
            patch(
                "derivation_app.product_profile.os.kill", side_effect=PermissionError
            ),
            self.assertRaises(ProfileLockHeld),
        ):
            lock.acquire()

    @unittest.skipUnless(os.name == "posix", "POSIX dead owner recovery")
    def test_instance_lock_reclaimer_does_not_delete_replacement_owner(self) -> None:
        temporary, repository, profile = self.layout()
        self.addCleanup(temporary.cleanup)
        provision_product_profile(profile, repo_root=repository)
        lock = ProfileInstanceLock(profile)
        lock.path.write_text("pid=12345\n")
        lock.path.chmod(0o600)
        replacement = lock.path.with_suffix(".replacement")
        replacement.write_text(f"pid={os.getpid()}\n")
        replacement.chmod(0o600)

        def replaced_owner(*_args):
            os.replace(replacement, lock.path)
            raise ProcessLookupError

        with (
            patch("derivation_app.product_profile.os.kill", side_effect=replaced_owner),
            self.assertRaises(ProfileLockHeld),
        ):
            lock.acquire()
        self.assertEqual(lock.path.read_text(), f"pid={os.getpid()}\n")

    def test_workspace_launch_and_record_evidence_stay_separate(self) -> None:
        temporary, repository, profile = self.layout(mode=ProfileMode.TEST)
        self.addCleanup(temporary.cleanup)
        provision_product_profile(profile, repo_root=repository)
        provision_fake_scientific_runtime(profile)
        run_id = "run_portable"
        evidence = repository / "runs" / run_id
        evidence.mkdir()
        mapping = prepare_run_workspace(
            profile,
            run_id=run_id,
            evidence_directory=evidence,
            repo_root=repository,
        )
        lock = ProfileInstanceLock(profile).acquire()
        self.addCleanup(lambda: lock.release() if lock.held else None)
        family = host_platform()
        prepared = prepare_product_launch(
            profile,
            lock=lock,
            repo_root=repository,
            platform=family,
            architecture=platform.machine(),
            app_server_executable=Path(sys.executable),
            workspace_mapping=mapping,
            repository=repository,
            user_home=Path.home(),
            windows_sandbox_mode="elevated"
            if family is PlatformFamily.WINDOWS
            else None,
        )
        self.assertEqual(Path(prepared.command.cwd), mapping.sandbox_workspace)
        self.assertNotEqual(Path(prepared.command.cwd), evidence)
        self.assertEqual(prepared.capability_profile.identity, "benchmark_symbolic_v1")

        executable = str(Path(sys.executable).resolve(strict=True))
        details = os.stat(executable)
        client = SimpleNamespace(
            is_running=True,
            returncode=None,
            cwd=Path(prepared.command.cwd),
            env=dict(prepared.command.environment),
            command=prepared.command.argv,
            process_identity=SpawnedProcessIdentity(
                canonical_executable=executable,
                executable_st_dev=details.st_dev,
                executable_st_ino=details.st_ino,
                pid=1,
                spawn_started_monotonic_ns=1,
                spawn_completed_monotonic_ns=2,
                identity_source="resolved_argv0_stat",
            ),
        )
        observation = RuntimeObservation(
            platform=family,
            architecture=platform.machine(),
            codex_version=PINNED_CODEX_VERSION,
            schema_sha256=PINNED_V2_SCHEMA_SHA256,
            sandbox_backend=expected_sandbox_backend(family),
            active_permission_profile="strict_run_workspace",
            available_permission_profiles=("strict_run_workspace",),
            instruction_sources=(),
            skills=(),
            mcp_servers=(),
            apps=(),
            command_network_enabled=False,
            command_network_proxy_active=False,
            command_network_destinations=(),
            effective_credential_store=CredentialStore.FILE,
            host_home=str(profile.home),
            tool_home=str(profile.home),
            tool_codex_home=None,
            web_search_enabled=False,
            windows_sandbox_mode=(
                "elevated" if family is PlatformFamily.WINDOWS else None
            ),
        )
        authorized = authorize_model_turn(
            prepared,
            client=client,  # type: ignore[arg-type]
            observation=observation,
        )
        self.assertTrue(authorized.gate_result.allowed)
        # _finish_authorization builds the settings of every product run,
        # shared App Server holder included, and keeps the runtime's event
        # limits; the operation buffer must hold a 100,000-character turn.
        self.assertEqual(authorized.settings.max_events_per_operation, 131072)
        self.assertIn(
            str(profile.runtime / "env" / "bin"), prepared.command.environment["PATH"]
        )

    def test_packaged_product_may_keep_run_evidence_outside_repository(self) -> None:
        temporary, repository, profile = self.layout(mode=ProfileMode.TEST)
        self.addCleanup(temporary.cleanup)
        provision_product_profile(profile, repo_root=repository)
        evidence_root = Path(temporary.name) / "Application Support" / "Data" / "runs"
        evidence = evidence_root / "run_external"
        evidence.mkdir(parents=True)

        mapping = prepare_run_workspace(
            profile,
            run_id="run_external",
            evidence_directory=evidence,
            repo_root=repository,
            evidence_root=evidence_root,
        )

        self.assertEqual(mapping.evidence_directory, evidence.resolve())
        self.assertFalse(mapping.evidence_directory.is_relative_to(repository))

    def test_launch_requires_the_runtime_declared_by_capability_profile(self) -> None:
        temporary, repository, profile = self.layout(mode=ProfileMode.TEST)
        self.addCleanup(temporary.cleanup)
        provision_product_profile(profile, repo_root=repository)
        run_id = "run_missing_runtime"
        evidence = repository / "runs" / run_id
        evidence.mkdir()
        mapping = prepare_run_workspace(
            profile,
            run_id=run_id,
            evidence_directory=evidence,
            repo_root=repository,
        )
        lock = ProfileInstanceLock(profile).acquire()
        self.addCleanup(lambda: lock.release() if lock.held else None)
        family = host_platform()
        with self.assertRaisesRegex(ProfileConflict, "provision it"):
            prepare_product_launch(
                profile,
                lock=lock,
                repo_root=repository,
                platform=family,
                architecture=platform.machine(),
                app_server_executable=Path(sys.executable),
                workspace_mapping=mapping,
                repository=repository,
                user_home=Path.home(),
                windows_sandbox_mode=(
                    "elevated" if family is PlatformFamily.WINDOWS else None
                ),
            )

    def test_intake_launch_uses_private_workspace_without_scientific_runtime(
        self,
    ) -> None:
        temporary, repository, profile = self.layout(mode=ProfileMode.TEST)
        self.addCleanup(temporary.cleanup)
        provision_product_profile(profile, repo_root=repository)
        mapping = prepare_intake_workspace(
            profile,
            intake_id="intake_fixture",
            repo_root=repository,
        )
        lock = ProfileInstanceLock(profile).acquire()
        self.addCleanup(lambda: lock.release() if lock.held else None)
        family = host_platform()

        prepared = prepare_product_launch(
            profile,
            lock=lock,
            repo_root=repository,
            platform=family,
            architecture=platform.machine(),
            app_server_executable=Path(sys.executable),
            workspace_mapping=mapping,
            repository=repository,
            user_home=Path.home(),
            capability_profile=INTAKE_V1,
            windows_sandbox_mode=(
                "elevated" if family is PlatformFamily.WINDOWS else None
            ),
        )

        self.assertEqual(prepared.workspace_mapping, mapping)
        self.assertEqual(prepared.capability_profile, INTAKE_V1)
        self.assertIsNone(prepared.validation.scientific_runtime)
        self.assertEqual(list((repository / "runs").iterdir()), [])

    def test_config_file_mode_is_private(self) -> None:
        if os.name == "nt":
            self.skipTest("POSIX modes do not apply on Windows")
        temporary, repository, profile = self.layout()
        self.addCleanup(temporary.cleanup)
        result = provision_product_profile(profile, repo_root=repository)
        self.assertEqual(stat.S_IMODE(result.config_path.stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
