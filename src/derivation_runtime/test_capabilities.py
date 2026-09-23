from __future__ import annotations

import unittest

from .capabilities import (
    BENCHMARK_SYMBOLIC_V1,
    CapabilityProfile,
    resolve_capability_profile,
)


class CapabilityProfileTests(unittest.TestCase):
    def test_builtin_profile_has_stable_closed_configuration(self) -> None:
        profile = resolve_capability_profile("benchmark_symbolic_v1")
        self.assertIs(profile, BENCHMARK_SYMBOLIC_V1)
        self.assertEqual(profile.allowed_tools, ("scientific_compute",))
        self.assertFalse(profile.network_access)
        self.assertRegex(profile.sha256, r"^[0-9a-f]{64}$")
        self.assertEqual(profile.app_server_config()["web_search"], "disabled")
        self.assertEqual(profile.app_server_config()["mcp_servers"], {})
        self.assertEqual(
            profile.app_server_config()["features"],
            {
                "code_mode_host": False,
                "js_repl": False,
                "shell_tool": False,
                "unified_exec": False,
            },
        )

    def test_profile_hash_changes_with_experimental_condition(self) -> None:
        research = CapabilityProfile(
            profile_id="research_open",
            version=1,
            permission_profile="research_workspace",
            allowed_tools=("scientific_compute", "web_search"),
            network_access=True,
            include_project_instructions=True,
            include_skills=True,
            web_search=True,
            mcp_servers=(),
            apps_enabled=False,
            scientific_runtime_id="sympy_uv",
        )
        self.assertNotEqual(research.sha256, BENCHMARK_SYMBOLIC_V1.sha256)
        self.assertEqual(research.app_server_config()["web_search"], "live")

    def test_profile_rejects_ambiguous_tool_order(self) -> None:
        with self.assertRaisesRegex(ValueError, "sorted and unique"):
            CapabilityProfile(
                profile_id="invalid_profile",
                version=1,
                permission_profile="strict_run_workspace",
                allowed_tools=("web_search", "scientific_compute"),
                network_access=True,
                include_project_instructions=False,
                include_skills=False,
                web_search=False,
                mcp_servers=(),
                apps_enabled=False,
                scientific_runtime_id=None,
            )


if __name__ == "__main__":
    unittest.main()
