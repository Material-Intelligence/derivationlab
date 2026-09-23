"""Service binding and restart configuration for directly specified problems.

This build ships no method-source pack, so a direct problem runs with the empty
pack, and a request that names a pack is refused before a run is created.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from derivation_api.models import CreateRunRequest
from derivation_api.service import DerivationServiceError, ErrorKind

from derivation_app.factory import deterministic_runtime, fake_create_run_defaults
from derivation_app.problem_sources import (
    SOURCE_PACKS_UNAVAILABLE,
    build_problem_presets,
)
from derivation_app.service import (
    RuntimeDerivationService,
    _run_config_dict,
    _run_config_from_dict,
)
from derivation_app.tests.test_vertical_integration import ROOT, run_request

#: A textbook problem stated directly, without an Intake interview.
DIRECT_PROBLEM = {
    "problem_id": "harmonic_oscillator_partition_function",
    "version": 1,
    "origin": "direct_spec",
    "confirmed_by_user": False,
    "objective": (
        "Derive the canonical partition function, mean energy and heat capacity "
        "of a one-dimensional quantum harmonic oscillator of angular frequency omega."
    ),
    "givens": ["The energy levels E_n = hbar omega (n + 1/2), n = 0, 1, 2, ..."],
    "assumptions": ["Thermal equilibrium at temperature T in the canonical ensemble."],
    "scope": "A single distinguishable oscillator; no anharmonic corrections.",
    "deliverable": "Closed-form Z(T), U(T) and C(T) with their low- and high-T limits.",
    "allowed_tools": ["scientific_compute"],
    "allowed_references": [],
    "success_criteria": [
        "The high-temperature limit of C(T) reproduces the classical value k_B."
    ],
}


def direct_request(*, checker: bool = False) -> dict:
    request = run_request()
    request["problem"] = dict(DIRECT_PROBLEM)
    request["config"].update(
        record_version="1.1",
        max_model_calls=None,
        checker_enabled=checker,
        max_local_repairs=3,
        reference_allowed=False,
    )
    request["runtime"]["capability_profile"] = "source_reading_v1"
    return request


class DirectProblemServiceConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.service = RuntimeDerivationService(
            run_root=Path(self.temporary.name),
            storage_root=Path(self.temporary.name),
            repo_root=ROOT,
            code_commit="0" * 40,
            credential_profile_id="fake-profile",
            create_run_defaults=fake_create_run_defaults(),
            runtime_factory=lambda config, _: deterministic_runtime(config),
        )

    def test_empty_pack_binds_and_roundtrips_runtime_choices(self) -> None:
        empty = build_problem_presets(ROOT).method_source_pack
        for checker in (False, True):
            with self.subTest(checker=checker):
                command = CreateRunRequest.model_validate(
                    direct_request(checker=checker)
                )
                config = self.service._config_for_command("run_test", command)
                restored = _run_config_from_dict(_run_config_dict(config))
                self.assertEqual(config, restored)
                self.assertIsNone(restored.max_model_calls)
                self.assertEqual(restored.checker_enabled, checker)
                self.assertEqual(restored.max_local_repairs, 3)
                self.assertEqual(restored.record_version, "1.1")
                self.assertTrue(
                    restored.event_schema.path.endswith("event-v1.1.schema.json")
                )
                self.assertEqual(restored.pack.id, "pack_empty")
                self.assertEqual(restored.pack.sha256, empty.sha256)

    def test_a_named_source_pack_is_refused(self) -> None:
        request = direct_request()
        request["problem"]["source_pack"] = {
            "pack_id": "independent",
            "version": "1",
            "sha256": "0" * 64,
        }
        request["problem"]["allowed_references"] = ["A method reference"]
        request["config"]["reference_allowed"] = True
        with self.assertRaises(DerivationServiceError) as raised:
            self.service._config_for_command(
                "run_test", CreateRunRequest.model_validate(request)
            )
        self.assertEqual(raised.exception.code, "problem_sources_invalid")
        self.assertIn(SOURCE_PACKS_UNAVAILABLE, str(raised.exception))

    def _refused(self, request: dict, message: str) -> None:
        with self.assertRaises(DerivationServiceError) as raised:
            self.service._config_for_command(
                "run_test", CreateRunRequest.model_validate(request)
            )
        self.assertEqual(raised.exception.code, "problem_sources_invalid")
        self.assertIn(message, str(raised.exception))

    def test_caller_provided_paths_are_refused(self) -> None:
        request = direct_request()
        request["config"]["allowed_paths"] = ["notes/method.tex"]
        self._refused(request, "not caller-provided paths")

    def test_reference_declarations_without_a_pack_are_refused(self) -> None:
        declared = direct_request()
        declared["problem"]["allowed_references"] = ["A method reference"]
        self._refused(declared, "require a verified source pack")
        allowed = direct_request()
        allowed["config"]["reference_allowed"] = True
        self._refused(allowed, "require a verified source pack")

    def test_reading_modes_that_need_a_pack_are_refused(self) -> None:
        for mode in ("direct_full", "reader_assisted"):
            with self.subTest(mode=mode):
                request = direct_request()
                request["runtime"]["reading_mode"] = mode
                request["runtime"]["generation_context_sha256"] = "a" * 64
                self._refused(request, "need a verified source pack")

    def test_direct_problem_rejects_legacy_capability_before_run_creation(self) -> None:
        command = CreateRunRequest.model_validate(direct_request())
        command.runtime.capability_profile = "benchmark_symbolic_v1"
        with self.assertRaises(DerivationServiceError) as raised:
            self.service._config_for_command("run_test", command)
        self.assertEqual(raised.exception.kind, ErrorKind.INVALID_STATE)
        self.assertEqual(raised.exception.code, "direct_problem_profile_invalid")

    def test_legacy_manifest_missing_new_fields_keeps_v1_shape(self) -> None:
        request = run_request()
        # A manifest written before these fields existed reopens under the
        # legacy fallbacks, not under today's product default of 3 repairs.
        request["config"]["max_local_repairs"] = 2
        config = self.service._config_for_command(
            "run_legacy", CreateRunRequest.model_validate(request)
        )
        persisted = _run_config_dict(config)
        for key in ("record_version", "checker_enabled", "max_local_repairs"):
            persisted.pop(key)
        restored = _run_config_from_dict(persisted)
        self.assertEqual(restored.record_version, "1.0")
        self.assertNotIn("checker_enabled", restored.record_configuration())
        self.assertNotIn("max_local_repairs", restored.record_configuration())
        self.assertEqual(config, restored)


class DirectProblemRestartTests(unittest.IsolatedAsyncioTestCase):
    async def test_unchecked_direct_run_reopens_without_becoming_checked(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = {
                "run_root": root,
                "storage_root": root,
                "repo_root": ROOT,
                "code_commit": "0" * 40,
                "credential_profile_id": "fake-profile",
                "create_run_defaults": fake_create_run_defaults(),
                "runtime_factory": lambda config, _: deterministic_runtime(config),
            }
            request = CreateRunRequest.model_validate(direct_request(checker=False))
            service = RuntimeDerivationService(**settings)
            await service.start()
            try:
                created = await service.create_run(request, idempotency_key=None)
                completed = await service.wait_for_phase(
                    created.id, {"review_ready", "error"}, timeout=5
                )
                self.assertEqual(completed.phase, "review_ready")
                self.assertTrue(completed.steps)
                self.assertTrue(
                    all(
                        step.checks.physics == "not_requested"
                        for step in completed.steps
                    )
                )
                before = service.event_log_path(created.id).read_bytes()
            finally:
                await service.close()
            reopened = RuntimeDerivationService(**settings)
            await reopened.start()
            try:
                restored = await reopened.get_run(created.id)
                self.assertEqual(restored.config.record_version, "1.1")
                self.assertFalse(restored.config.checker_enabled)
                self.assertIsNone(restored.config.max_model_calls)
                self.assertEqual(restored.phase, "review_ready")
                self.assertTrue(
                    all(
                        step.checks.physics == "not_requested"
                        for step in restored.steps
                    )
                )
                self.assertEqual(
                    reopened.event_log_path(created.id).read_bytes(), before
                )
            finally:
                await reopened.close()
