"""Load and verify immutable local-release identity metadata."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from derivation_api.models import BuildInfoView
from pydantic import ValidationError


class BuildInfoError(RuntimeError):
    """A release manifest is missing, malformed, or bound to another API."""


def openapi_sha256(path: str | Path) -> str:
    source = Path(path)
    try:
        return hashlib.sha256(source.read_bytes()).hexdigest()
    except OSError as exc:
        raise BuildInfoError("cannot read the checked-in OpenAPI contract") from exc


def development_build_info(*, commit: str, openapi_path: str | Path) -> BuildInfoView:
    return BuildInfoView(
        schema_version="derivationlab-build-info-v1",
        version="dev",
        build_number="0",
        release_id=f"development-{commit[:7]}",
        commit=commit,
        openapi_sha256=openapi_sha256(openapi_path),
        product_mode="development",
    )


def load_release_build_info(
    manifest_path: str | Path,
    *,
    openapi_path: str | Path,
) -> BuildInfoView:
    path = Path(manifest_path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise TypeError("release manifest must be an object")
        candidate = raw.get("build_info", raw)
        if not isinstance(candidate, dict):
            raise TypeError("release manifest build_info must be an object")
        fields = set(BuildInfoView.model_fields)
        value = BuildInfoView.model_validate({name: candidate[name] for name in fields})
    except (KeyError, OSError, TypeError, json.JSONDecodeError, ValidationError) as exc:
        raise BuildInfoError("release manifest is missing or invalid") from exc
    if value.product_mode != "release":
        raise BuildInfoError("packaged release manifest must declare release mode")
    if value.openapi_sha256 != openapi_sha256(openapi_path):
        raise BuildInfoError(
            "release manifest OpenAPI digest does not match the bundle"
        )
    return value


__all__ = [
    "BuildInfoError",
    "development_build_info",
    "load_release_build_info",
    "openapi_sha256",
]
