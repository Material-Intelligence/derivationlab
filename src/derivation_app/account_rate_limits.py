"""Tenant-private, non-authoritative ChatGPT usage cache."""

from __future__ import annotations

import copy
import threading
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from derivation_api.models import (
    AccountRateLimitsView,
    AccountRateLimitWindowView,
)

JsonObject = dict[str, Any]
_PROVIDER_REFRESH_SECONDS = 5 * 60
_STALE_AFTER_SECONDS = 10 * 60


def _utc_iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=UTC).isoformat().replace("+00:00", "Z")


class AccountRateLimitStore:
    """Merge App Server snapshots without exposing provider account metadata."""

    def __init__(
        self,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        wall_time: Callable[[], float] = time.time,
    ) -> None:
        self._monotonic = monotonic
        self._wall_time = wall_time
        self._lock = threading.Lock()
        self._snapshot: JsonObject | None = None
        self._observed_monotonic: float | None = None
        self._observed_wall_time: float | None = None
        self._last_refresh_attempt: float | None = None

    def observe(self, snapshot: JsonObject, sparse: bool) -> None:
        incoming = copy.deepcopy(snapshot)
        now = self._monotonic()
        wall = self._wall_time()
        with self._lock:
            if not sparse or self._snapshot is None:
                self._snapshot = incoming
            else:
                merged = copy.deepcopy(self._snapshot)
                for key, value in incoming.items():
                    previous = merged.get(key)
                    if (
                        key in {"primary", "secondary"}
                        and isinstance(previous, Mapping)
                        and isinstance(value, Mapping)
                    ):
                        merged[key] = {**previous, **copy.deepcopy(value)}
                    else:
                        merged[key] = copy.deepcopy(value)
                self._snapshot = merged
            self._observed_monotonic = now
            self._observed_wall_time = wall
            if not sparse:
                self._last_refresh_attempt = now

    def clear(self) -> None:
        with self._lock:
            self._snapshot = None
            self._observed_monotonic = None
            self._observed_wall_time = None
            self._last_refresh_attempt = None

    def claim_provider_refresh(self) -> bool:
        """Atomically throttle full App Server reads to at most once per 5 min."""

        now = self._monotonic()
        with self._lock:
            if (
                self._last_refresh_attempt is not None
                and now - self._last_refresh_attempt < _PROVIDER_REFRESH_SECONDS
            ):
                return False
            self._last_refresh_attempt = now
            return True

    def view(self, *, signed_out: bool = False) -> AccountRateLimitsView:
        if signed_out:
            self.clear()
            return AccountRateLimitsView(
                status="signed_out",
                windows=[],
                diagnostic="product_auth_missing",
            )
        with self._lock:
            snapshot = copy.deepcopy(self._snapshot)
            observed_monotonic = self._observed_monotonic
            observed_wall_time = self._observed_wall_time
        if snapshot is None or observed_monotonic is None or observed_wall_time is None:
            return AccountRateLimitsView(
                status="temporarily_unavailable",
                windows=[],
                diagnostic="rate_limits_unavailable",
            )
        windows: list[AccountRateLimitWindowView] = []
        for field in ("primary", "secondary"):
            raw = snapshot.get(field)
            if not isinstance(raw, Mapping):
                continue
            duration = raw.get("windowDurationMins")
            used = raw.get("usedPercent")
            if isinstance(used, bool) or not isinstance(used, int):
                continue
            kind = (
                "five_hour"
                if duration == 300
                else "weekly"
                if duration == 10_080
                else "other"
            )
            resets_at = raw.get("resetsAt")
            windows.append(
                AccountRateLimitWindowView(
                    kind=kind,
                    used_percent=used,
                    remaining_percent=100 - used,
                    window_duration_mins=(
                        duration if isinstance(duration, int) else None
                    ),
                    resets_at=(
                        _utc_iso(resets_at)
                        if isinstance(resets_at, int)
                        and not isinstance(resets_at, bool)
                        else None
                    ),
                )
            )
        age = max(0.0, self._monotonic() - observed_monotonic)
        return AccountRateLimitsView(
            status="available",
            plan_type=(
                snapshot.get("planType")
                if isinstance(snapshot.get("planType"), str)
                else None
            ),
            windows=windows,
            observed_at=_utc_iso(observed_wall_time),
            stale=age >= _STALE_AFTER_SECONDS,
            diagnostic="ready",
        )


__all__ = ["AccountRateLimitStore"]
