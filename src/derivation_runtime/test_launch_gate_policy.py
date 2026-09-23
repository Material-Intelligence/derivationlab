from __future__ import annotations

import json
import unittest
from pathlib import Path

from derivation_runtime.platform_policy import (
    PINNED_CODEX_VERSION,
    CapabilityStatus,
    current_macos_capability,
    failed_colima_linux_capability,
    pending_linux_capability,
    pending_windows_capability,
)

ROOT = Path(__file__).resolve().parents[2]
PLATFORM_GATES = (
    ROOT / "src" / "derivation_runtime" / "platform_evidence" / "platform_gates"
)


class PlatformPolicyTests(unittest.TestCase):
    def test_checked_in_macos_no_model_evidence_is_supported(self) -> None:
        report = current_macos_capability(
            ROOT,
            architecture="arm64",
            codex_version=PINNED_CODEX_VERSION,
        )
        self.assertEqual(report.status, CapabilityStatus.SUPPORTED)
        self.assertTrue(report.authorizes_model_turns)
        self.assertFalse(report.release_conformant)
        self.assertEqual(
            set(report.proven_checks),
            {
                "app_server_initialize",
                "sandbox_activation",
                "workspace_read_write",
                "adjacent_read_denied",
                "symlink_escape_denied",
                "network_off",
                "python_sympy",
            },
        )

    def test_macos_evidence_does_not_generalize_to_other_architecture(self) -> None:
        report = current_macos_capability(
            ROOT,
            architecture="x86_64",
            codex_version=PINNED_CODEX_VERSION,
        )
        self.assertEqual(report.status, CapabilityStatus.FAILED)
        self.assertFalse(report.authorizes_model_turns)
        self.assertTrue(any("architecture" in item for item in report.failures))

    def test_linux_and_windows_are_pending_without_host_evidence(self) -> None:
        linux = pending_linux_capability(
            architecture="x86_64", codex_version=PINNED_CODEX_VERSION
        )
        windows = pending_windows_capability(
            architecture="AMD64", codex_version=PINNED_CODEX_VERSION
        )
        self.assertEqual(linux.status, CapabilityStatus.PENDING)
        self.assertEqual(windows.status, CapabilityStatus.PENDING)
        self.assertFalse(linux.authorizes_model_turns)
        self.assertFalse(windows.authorizes_model_turns)
        self.assertFalse(linux.release_conformant)
        self.assertFalse(windows.release_conformant)

    def test_colima_failure_is_host_specific_and_fail_closed(self) -> None:
        report = failed_colima_linux_capability()
        self.assertEqual(report.status, CapabilityStatus.FAILED)
        self.assertIn("Colima", report.os_description)
        self.assertTrue(any("RTM_NEWADDR" in item for item in report.failures))
        self.assertFalse(report.authorizes_model_turns)

    def test_machine_readable_files_cover_every_semantic_check(self) -> None:
        manifest = json.loads(
            (PLATFORM_GATES / "conformance_manifest.json").read_text(encoding="utf-8")
        )
        report = json.loads(
            (PLATFORM_GATES / "platform_report.current.json").read_text(
                encoding="utf-8"
            )
        )
        required = {item["id"] for item in manifest["checks"] if item["required"]}
        expected = {
            "app_server_initialize",
            "sandbox_activation",
            "workspace_read",
            "workspace_write",
            "adjacent_read_denied",
            "symlink_escape_denied",
            "windows_junction_escape_denied",
            "network_off",
            "python_sympy",
            "account_persistence",
            "restart_resume",
            "fork_completed_node",
        }
        self.assertEqual(required, expected)
        model_turn_prerequisites = {
            item["id"] for item in manifest["checks"] if item["model_turn_prerequisite"]
        }
        self.assertEqual(
            model_turn_prerequisites,
            {
                "app_server_initialize",
                "sandbox_activation",
                "workspace_read",
                "workspace_write",
                "adjacent_read_denied",
                "symlink_escape_denied",
                "windows_junction_escape_denied",
                "network_off",
                "python_sympy",
            },
        )
        self.assertEqual(
            {item["platform"] for item in report["platform_reports"]},
            {"macos", "linux", "windows"},
        )
        linux_hosts = [
            item
            for item in report["host_results"]
            if item["host_id"] == "colima-ubuntu-24.04.4-arm64-20260829"
        ]
        self.assertEqual(len(linux_hosts), 1)
        self.assertEqual(linux_hosts[0]["status"], "failed")
        self.assertFalse(linux_hosts[0]["authorizes_model_turns"])

    def test_every_cited_evidence_file_ships_or_is_marked_unpublished(self) -> None:
        report = json.loads(
            (PLATFORM_GATES / "platform_report.current.json").read_text(
                encoding="utf-8"
            )
        )
        for host in report["host_results"]:
            for item in host["evidence"]:
                with self.subTest(host=host["host_id"], path=item["path"]):
                    if item["path"] is None:
                        # Nothing to open: either no file was kept, or the file
                        # is an unpublished artefact identified by digest only.
                        if item.get("published") is False:
                            self.assertRegex(item["sha256"], r"^[0-9a-f]{64}$")
                            self.assertTrue(item["note"])
                        continue
                    self.assertNotIn("published", item)
                    self.assertTrue((PLATFORM_GATES / item["path"]).is_file())


if __name__ == "__main__":
    unittest.main()
