from __future__ import annotations

import unittest

from derivation_app.account_rate_limits import AccountRateLimitStore


class AccountRateLimitStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.monotonic = [100.0]
        self.wall = [1_788_545_600.0]
        self.store = AccountRateLimitStore(
            monotonic=lambda: self.monotonic[0],
            wall_time=lambda: self.wall[0],
        )

    def test_classifies_windows_by_duration_and_computes_remaining(self) -> None:
        self.store.observe(
            {
                "planType": "plus",
                "primary": {
                    "usedPercent": 31,
                    "windowDurationMins": 10_080,
                    "resetsAt": 1_788_123_456,
                },
                "secondary": {
                    "usedPercent": 18,
                    "windowDurationMins": 300,
                    "resetsAt": 1_788_100_000,
                },
            },
            False,
        )

        view = self.store.view()

        self.assertEqual(view.status, "available")
        self.assertEqual(view.plan_type, "plus")
        self.assertEqual(
            [(window.kind, window.remaining_percent) for window in view.windows],
            [("weekly", 69), ("five_hour", 82)],
        )
        self.assertFalse(view.stale)

    def test_sparse_update_merges_nested_window_and_preserves_duration(self) -> None:
        self.store.observe(
            {
                "primary": {
                    "usedPercent": 31,
                    "windowDurationMins": 10_080,
                    "resetsAt": 1_788_123_456,
                }
            },
            False,
        )

        self.store.observe({"primary": {"usedPercent": 32}}, True)

        window = self.store.view().windows[0]
        self.assertEqual(window.kind, "weekly")
        self.assertEqual(window.remaining_percent, 68)
        self.assertEqual(window.window_duration_mins, 10_080)

    def test_refresh_claim_is_throttled_and_full_observation_resets_clock(self) -> None:
        self.assertTrue(self.store.claim_provider_refresh())
        self.assertFalse(self.store.claim_provider_refresh())
        self.monotonic[0] += 300
        self.assertTrue(self.store.claim_provider_refresh())
        self.monotonic[0] += 300
        self.store.observe({"primary": {"usedPercent": 20}}, False)
        self.assertFalse(self.store.claim_provider_refresh())

    def test_stale_and_signed_out_views_are_explicit(self) -> None:
        self.store.observe({"primary": {"usedPercent": 20}}, False)
        self.monotonic[0] += 600
        self.assertTrue(self.store.view().stale)

        signed_out = self.store.view(signed_out=True)

        self.assertEqual(signed_out.status, "signed_out")
        self.assertEqual(self.store.view().status, "temporarily_unavailable")


if __name__ == "__main__":
    unittest.main()
