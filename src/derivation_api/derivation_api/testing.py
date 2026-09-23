"""Explicit fake application entry point for hermetic tests and UI development."""

from __future__ import annotations

from .application import create_app
from .fake_service import FakeDerivationService

service = FakeDerivationService()
app = create_app(service)
