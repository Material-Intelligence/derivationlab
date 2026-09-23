"""Validated model catalogs owned by the Codex App Server boundary."""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

CATALOG_CACHE_SCHEMA = "derivationlab-model-catalog-v2"
CatalogSource = Literal["app_server", "last_known_good", "static_fixture"]
DEFAULT_PRODUCT_MODEL = "gpt-5.6-sol"
DEFAULT_PRODUCT_EFFORT = "high"
DEFAULT_PRODUCT_SERVICE_TIER = "fast"
STANDARD_SERVICE_TIER = "standard"
FAST_SERVICE_TIER = "fast"
APP_SERVER_FAST_SERVICE_TIER = "priority"


@dataclass(frozen=True)
class ModelOption:
    model: str
    display_name: str
    default_effort: str
    supported_efforts: tuple[str, ...]
    default_service_tier: str = "standard"
    supported_service_tiers: tuple[str, ...] = ("standard",)
    is_default: bool = False

    def __post_init__(self) -> None:
        for name, value in (
            ("model", self.model),
            ("display_name", self.display_name),
            ("default_effort", self.default_effort),
            ("default_service_tier", self.default_service_tier),
        ):
            if not value or value != value.strip():
                raise ValueError(f"model option {name} must be non-empty and trimmed")
        if not self.supported_efforts:
            raise ValueError("model option supported_efforts must be non-empty")
        if len(self.supported_efforts) != len(set(self.supported_efforts)):
            raise ValueError("model option supported_efforts must be unique")
        if any(not item or item != item.strip() for item in self.supported_efforts):
            raise ValueError("model option efforts must be non-empty and trimmed")
        if self.default_effort not in self.supported_efforts:
            raise ValueError("model option default_effort must be supported")
        if not self.supported_service_tiers:
            raise ValueError("model option supported_service_tiers must be non-empty")
        if len(self.supported_service_tiers) != len(set(self.supported_service_tiers)):
            raise ValueError("model option supported_service_tiers must be unique")
        if any(
            not item or item != item.strip() for item in self.supported_service_tiers
        ):
            raise ValueError("model option service tiers must be non-empty and trimmed")
        if STANDARD_SERVICE_TIER not in self.supported_service_tiers:
            raise ValueError("model option must support the standard service tier")
        if self.default_service_tier not in self.supported_service_tiers:
            raise ValueError("model option default_service_tier must be supported")


@dataclass(frozen=True)
class ModelCatalog:
    options: tuple[ModelOption, ...]
    source: CatalogSource
    refreshed_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.options:
            raise ValueError("model catalog must be non-empty")
        if len(self.models) != len(set(self.models)):
            raise ValueError("model catalog models must be unique")
        if sum(option.is_default for option in self.options) != 1:
            raise ValueError("model catalog must contain exactly one default model")
        if self.refreshed_at is not None and self.refreshed_at.tzinfo is None:
            raise ValueError("catalog refreshed_at must be timezone-aware")

    @property
    def models(self) -> tuple[str, ...]:
        return tuple(option.model for option in self.options)

    @property
    def efforts(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                effort for option in self.options for effort in option.supported_efforts
            )
        )

    @property
    def service_tiers(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                tier
                for option in self.options
                for tier in option.supported_service_tiers
            )
        )

    @property
    def default(self) -> ModelOption:
        return next(option for option in self.options if option.is_default)

    def option(self, model: str) -> ModelOption | None:
        return next((option for option in self.options if option.model == model), None)

    def to_cache_dict(self) -> dict[str, object]:
        refreshed_at = self.refreshed_at or datetime.now(UTC)
        return {
            "schema_version": CATALOG_CACHE_SCHEMA,
            "refreshed_at": refreshed_at.isoformat(),
            "options": [
                {
                    "model": option.model,
                    "display_name": option.display_name,
                    "default_effort": option.default_effort,
                    "supported_efforts": list(option.supported_efforts),
                    "default_service_tier": option.default_service_tier,
                    "supported_service_tiers": list(option.supported_service_tiers),
                    "is_default": option.is_default,
                }
                for option in self.options
            ],
        }

    @classmethod
    def from_cache_dict(cls, value: object) -> ModelCatalog:
        if not isinstance(value, dict) or set(value) != {
            "schema_version",
            "refreshed_at",
            "options",
        }:
            raise ValueError("model catalog cache has an invalid envelope")
        if value.get("schema_version") != CATALOG_CACHE_SCHEMA:
            raise ValueError("model catalog cache schema is unsupported")
        raw_timestamp = value.get("refreshed_at")
        if not isinstance(raw_timestamp, str):
            raise TypeError("model catalog cache timestamp is invalid")
        try:
            refreshed_at = datetime.fromisoformat(raw_timestamp)
        except ValueError as exc:
            raise ValueError("model catalog cache timestamp is invalid") from exc
        raw_options = value.get("options")
        if not isinstance(raw_options, list):
            raise TypeError("model catalog cache options must be a list")
        return cls(
            options=tuple(_option_from_cache(item) for item in raw_options),
            source="last_known_good",
            refreshed_at=refreshed_at,
        )


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"App Server model/list {field} must be non-empty and trimmed")
    return value


def _option_from_cache(value: object) -> ModelOption:
    if not isinstance(value, dict) or set(value) != {
        "model",
        "display_name",
        "default_effort",
        "supported_efforts",
        "default_service_tier",
        "supported_service_tiers",
        "is_default",
    }:
        raise ValueError("model catalog cache option has an invalid shape")
    efforts = value.get("supported_efforts")
    if not isinstance(efforts, list):
        raise TypeError("model catalog cache supported_efforts must be a list")
    service_tiers = value.get("supported_service_tiers")
    if not isinstance(service_tiers, list):
        raise TypeError("model catalog cache supported_service_tiers must be a list")
    is_default = value.get("is_default")
    if not isinstance(is_default, bool):
        raise TypeError("model catalog cache is_default must be boolean")
    return ModelOption(
        model=_required_text(value.get("model"), "model"),
        display_name=_required_text(value.get("display_name"), "display_name"),
        default_effort=_required_text(value.get("default_effort"), "default_effort"),
        supported_efforts=tuple(
            _required_text(item, "supported_efforts") for item in efforts
        ),
        default_service_tier=_required_text(
            value.get("default_service_tier"), "default_service_tier"
        ),
        supported_service_tiers=tuple(
            _required_text(item, "supported_service_tiers") for item in service_tiers
        ),
        is_default=is_default,
    )


def catalog_from_app_server_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    refreshed_at: datetime | None = None,
) -> ModelCatalog:
    options: list[ModelOption] = []
    for row in rows:
        model = _required_text(row.get("model"), "model")
        identifier = _required_text(row.get("id"), "id")
        if identifier != model:
            raise ValueError("App Server model/list id and model must match")
        raw_efforts = row.get("supportedReasoningEfforts")
        if not isinstance(raw_efforts, list):
            raise TypeError(
                "App Server model/list supportedReasoningEfforts must be a list"
            )
        efforts: list[str] = []
        for raw_effort in raw_efforts:
            if not isinstance(raw_effort, dict):
                raise TypeError("App Server reasoning effort must be an object")
            efforts.append(
                _required_text(raw_effort.get("reasoningEffort"), "reasoningEffort")
            )
        is_default = row.get("isDefault")
        if not isinstance(is_default, bool):
            raise TypeError("App Server model/list isDefault must be boolean")
        raw_service_tiers = row.get("serviceTiers", [])
        if not isinstance(raw_service_tiers, list):
            raise TypeError("App Server model/list serviceTiers must be a list")
        supports_fast = False
        for raw_tier in raw_service_tiers:
            if not isinstance(raw_tier, dict):
                raise TypeError("App Server service tier must be an object")
            tier_id = _required_text(raw_tier.get("id"), "serviceTiers.id")
            _required_text(raw_tier.get("name"), "serviceTiers.name")
            _required_text(raw_tier.get("description"), "serviceTiers.description")
            if tier_id == APP_SERVER_FAST_SERVICE_TIER:
                supports_fast = True
        default_service_tier = row.get("defaultServiceTier")
        if default_service_tier is not None and not isinstance(
            default_service_tier, str
        ):
            raise TypeError(
                "App Server model/list defaultServiceTier must be text or null"
            )
        supported_service_tiers = (
            (STANDARD_SERVICE_TIER, FAST_SERVICE_TIER)
            if supports_fast
            else (STANDARD_SERVICE_TIER,)
        )
        options.append(
            ModelOption(
                model=model,
                display_name=_required_text(row.get("displayName"), "displayName"),
                default_effort=_required_text(
                    row.get("defaultReasoningEffort"), "defaultReasoningEffort"
                ),
                supported_efforts=tuple(efforts),
                default_service_tier=(
                    FAST_SERVICE_TIER
                    if default_service_tier == APP_SERVER_FAST_SERVICE_TIER
                    and supports_fast
                    else STANDARD_SERVICE_TIER
                ),
                supported_service_tiers=supported_service_tiers,
                is_default=is_default,
            )
        )
    return ModelCatalog(
        options=tuple(options),
        source="app_server",
        refreshed_at=refreshed_at or datetime.now(UTC),
    )


class ModelCatalogCache:
    """Atomic private cache for one validated App Server model/list result."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).resolve()

    def load(self) -> ModelCatalog:
        if self.path.is_symlink() or not self.path.is_file():
            raise ValueError("last-known-good model catalog is unavailable")
        return ModelCatalog.from_cache_dict(
            json.loads(self.path.read_text(encoding="utf-8"))
        )

    def store(self, catalog: ModelCatalog) -> None:
        if catalog.source != "app_server":
            raise ValueError("only a live App Server catalog may update the cache")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.path.parent.is_symlink():
            raise ValueError("model catalog cache directory must not be a symlink")
        pending = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.pending")
        encoded = json.dumps(
            catalog.to_cache_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        try:
            descriptor = os.open(
                pending,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(encoded)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(pending, self.path)
        finally:
            if pending.exists():
                pending.unlink()


def fixture_catalog() -> ModelCatalog:
    return ModelCatalog(
        options=(
            ModelOption(
                model="gpt-5.6-sol",
                display_name="GPT-5.6-Sol",
                default_effort="low",
                supported_efforts=("low", "medium", "high", "xhigh", "max", "ultra"),
                default_service_tier="fast",
                supported_service_tiers=("standard", "fast"),
                is_default=True,
            ),
            ModelOption(
                model="gpt-5.6-terra",
                display_name="GPT-5.6-Terra",
                default_effort="medium",
                supported_efforts=("low", "medium", "high", "xhigh", "max", "ultra"),
                default_service_tier="fast",
                supported_service_tiers=("standard", "fast"),
            ),
            ModelOption(
                model="gpt-5.6-luna",
                display_name="GPT-5.6-Luna",
                default_effort="medium",
                supported_efforts=("low", "medium", "high", "xhigh", "max"),
                default_service_tier="fast",
                supported_service_tiers=("standard", "fast"),
            ),
            ModelOption(
                model="gpt-5.5",
                display_name="GPT-5.5",
                default_effort="medium",
                supported_efforts=("low", "medium", "high", "xhigh"),
                default_service_tier="fast",
                supported_service_tiers=("standard", "fast"),
            ),
            ModelOption(
                model="gpt-5.4",
                display_name="GPT-5.4",
                default_effort="medium",
                supported_efforts=("low", "medium", "high", "xhigh"),
                default_service_tier="fast",
                supported_service_tiers=("standard", "fast"),
            ),
            ModelOption(
                model="gpt-5.4-mini",
                display_name="GPT-5.4 Mini",
                default_effort="medium",
                supported_efforts=("low", "medium", "high", "xhigh"),
                default_service_tier="standard",
                supported_service_tiers=("standard",),
            ),
            ModelOption(
                model="gpt-5-codex",
                display_name="GPT-5 Codex fixture",
                default_effort="medium",
                supported_efforts=("low", "medium", "high"),
                default_service_tier="standard",
                supported_service_tiers=("standard",),
            ),
            ModelOption(
                model="gpt-5.3-codex-spark",
                display_name="GPT-5.3-Codex-Spark",
                default_effort="high",
                supported_efforts=("low", "medium", "high", "xhigh"),
                default_service_tier="fast",
                supported_service_tiers=("standard", "fast"),
            ),
        ),
        source="static_fixture",
    )


def fake_catalog() -> ModelCatalog:
    product_options = tuple(
        ModelOption(
            model=option.model,
            display_name=option.display_name,
            default_effort=option.default_effort,
            supported_efforts=option.supported_efforts,
            default_service_tier=option.default_service_tier,
            supported_service_tiers=option.supported_service_tiers,
        )
        for option in fixture_catalog().options
    )
    return ModelCatalog(
        options=(
            ModelOption(
                model="deterministic",
                display_name="Deterministic fixture",
                default_effort="none",
                supported_efforts=("none",),
                default_service_tier="standard",
                supported_service_tiers=("standard",),
                is_default=True,
            ),
            *product_options,
        ),
        source="static_fixture",
    )


def product_catalog() -> ModelCatalog:
    """Hermetic catalog for tests; production refreshes from App Server."""

    return fixture_catalog()


def allowed_models() -> tuple[str, ...]:
    return fixture_catalog().models


def allowed_efforts() -> tuple[str, ...]:
    return fixture_catalog().efforts


def product_default_selection(
    catalog: ModelCatalog,
) -> tuple[ModelOption, str, str]:
    """Resolve explicit product defaults with catalog-safe fallbacks."""

    option = catalog.option(DEFAULT_PRODUCT_MODEL) or catalog.default
    effort = (
        DEFAULT_PRODUCT_EFFORT
        if DEFAULT_PRODUCT_EFFORT in option.supported_efforts
        else option.default_effort
    )
    service_tier = (
        DEFAULT_PRODUCT_SERVICE_TIER
        if DEFAULT_PRODUCT_SERVICE_TIER in option.supported_service_tiers
        else option.default_service_tier
    )
    return option, effort, service_tier


def validate_model_effort(
    model: object,
    effort: object,
    *,
    catalog: ModelCatalog | None = None,
) -> tuple[str, str]:
    catalog = catalog or fixture_catalog()
    model_text = model.strip() if isinstance(model, str) else ""
    effort_text = effort.strip() if isinstance(effort, str) else ""
    if not model_text or not effort_text:
        raise ValueError("model and effort are required")
    option = catalog.option(model_text)
    if option is None:
        raise ValueError(
            f"model {model_text!r} is not in the allowed catalog: "
            + ", ".join(catalog.models)
        )
    if effort_text not in option.supported_efforts:
        raise ValueError(
            f"effort {effort_text!r} is not supported by model {model_text!r}: "
            + ", ".join(option.supported_efforts)
        )
    return model_text, effort_text


def validate_model_service_tier(
    model: object,
    service_tier: object,
    *,
    catalog: ModelCatalog | None = None,
) -> tuple[str, str]:
    catalog = catalog or fixture_catalog()
    model_text = model.strip() if isinstance(model, str) else ""
    tier_text = service_tier.strip() if isinstance(service_tier, str) else ""
    if not model_text or not tier_text:
        raise ValueError("model and service tier are required")
    option = catalog.option(model_text)
    if option is None:
        raise ValueError(
            f"model {model_text!r} is not in the allowed catalog: "
            + ", ".join(catalog.models)
        )
    if tier_text not in option.supported_service_tiers:
        raise ValueError(
            f"service tier {tier_text!r} is not supported by model "
            f"{model_text!r}: " + ", ".join(option.supported_service_tiers)
        )
    return model_text, tier_text
