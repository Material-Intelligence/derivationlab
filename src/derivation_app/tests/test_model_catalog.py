from __future__ import annotations

import json
import unittest
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from derivation_app.model_catalog import (
    DEFAULT_PRODUCT_EFFORT,
    DEFAULT_PRODUCT_MODEL,
    ModelCatalogCache,
    catalog_from_app_server_rows,
    fake_catalog,
    fixture_catalog,
    product_default_selection,
    validate_model_effort,
    validate_model_service_tier,
)


def live_rows() -> list[dict[str, object]]:
    return [
        {
            "id": "gpt-5.6-sol",
            "model": "gpt-5.6-sol",
            "displayName": "GPT-5.6-Sol",
            "isDefault": True,
            "defaultReasoningEffort": "medium",
            "supportedReasoningEfforts": [
                {"reasoningEffort": item}
                for item in ("low", "medium", "high", "xhigh", "max", "ultra")
            ],
            "defaultServiceTier": "priority",
            "serviceTiers": [
                {
                    "id": "priority",
                    "name": "Fast",
                    "description": "1.5x speed, increased usage",
                }
            ],
        },
        {
            "id": "gpt-5.5",
            "model": "gpt-5.5",
            "displayName": "GPT-5.5",
            "isDefault": False,
            "defaultReasoningEffort": "xhigh",
            "supportedReasoningEfforts": [
                {"reasoningEffort": item} for item in ("low", "medium", "high", "xhigh")
            ],
            "defaultServiceTier": "priority",
            "serviceTiers": [
                {
                    "id": "priority",
                    "name": "Fast",
                    "description": "1.5x speed, increased usage",
                }
            ],
        },
    ]


class ModelCatalogTests(unittest.TestCase):
    def test_fixture_contains_latest_models_and_effort_ceiling(self) -> None:
        catalog = fixture_catalog()
        self.assertEqual(catalog.default.model, DEFAULT_PRODUCT_MODEL)
        option, effort, service_tier = product_default_selection(catalog)
        self.assertEqual(option.model, DEFAULT_PRODUCT_MODEL)
        self.assertEqual(effort, DEFAULT_PRODUCT_EFFORT)
        self.assertEqual(service_tier, "fast")
        self.assertIn("gpt-5.6-sol", catalog.models)
        self.assertIn("gpt-5.5", catalog.models)
        self.assertIn("ultra", catalog.option("gpt-5.6-sol").supported_efforts)
        self.assertNotIn("ultra", catalog.option("gpt-5.5").supported_efforts)

    def test_live_rows_preserve_per_model_efforts_and_default(self) -> None:
        refreshed = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
        catalog = catalog_from_app_server_rows(live_rows(), refreshed_at=refreshed)
        self.assertEqual(catalog.source, "app_server")
        self.assertEqual(catalog.refreshed_at, refreshed)
        self.assertEqual(catalog.models, ("gpt-5.6-sol", "gpt-5.5"))
        self.assertEqual(catalog.default.model, "gpt-5.6-sol")
        self.assertEqual(catalog.option("gpt-5.5").default_effort, "xhigh")
        self.assertEqual(
            catalog.option("gpt-5.6-sol").supported_service_tiers,
            ("standard", "fast"),
        )
        self.assertEqual(
            catalog.option("gpt-5.6-sol").default_service_tier,
            "fast",
        )

    def test_fake_catalog_keeps_deterministic_default(self) -> None:
        catalog = fake_catalog()
        self.assertEqual(catalog.default.model, "deterministic")
        self.assertEqual(catalog.option("deterministic").supported_efforts, ("none",))

    def test_validate_model_effort_is_model_specific(self) -> None:
        catalog = catalog_from_app_server_rows(live_rows())
        self.assertEqual(
            validate_model_effort("gpt-5.6-sol", "ultra", catalog=catalog),
            ("gpt-5.6-sol", "ultra"),
        )
        with self.assertRaisesRegex(ValueError, "not supported"):
            validate_model_effort("gpt-5.5", "ultra", catalog=catalog)
        with self.assertRaisesRegex(ValueError, "not in the allowed catalog"):
            validate_model_effort("not-a-model", "low", catalog=catalog)

    def test_validate_service_tier_is_model_specific(self) -> None:
        catalog = fixture_catalog()
        self.assertEqual(
            validate_model_service_tier("gpt-5.6-sol", "fast", catalog=catalog),
            ("gpt-5.6-sol", "fast"),
        )
        with self.assertRaisesRegex(ValueError, "not supported"):
            validate_model_service_tier("gpt-5.4-mini", "fast", catalog=catalog)

    def test_cache_round_trip_becomes_last_known_good(self) -> None:
        with TemporaryDirectory() as directory:
            cache = ModelCatalogCache(Path(directory) / "catalog.json")
            cache.store(catalog_from_app_server_rows(live_rows()))
            loaded = cache.load()
            self.assertEqual(loaded.source, "last_known_good")
            self.assertEqual(loaded.models, ("gpt-5.6-sol", "gpt-5.5"))
            self.assertEqual(
                loaded.option("gpt-5.6-sol").supported_service_tiers,
                ("standard", "fast"),
            )
            self.assertEqual(cache.path.stat().st_mode & 0o777, 0o600)

    def test_cache_rejects_corrupt_or_non_live_input(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "catalog.json"
            cache = ModelCatalogCache(path)
            with self.assertRaisesRegex(ValueError, "only a live"):
                cache.store(fixture_catalog())
            path.write_text(json.dumps({"schema_version": "wrong"}), encoding="utf-8")
            with self.assertRaises(ValueError):
                cache.load()


if __name__ == "__main__":
    unittest.main()
