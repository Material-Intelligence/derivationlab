"""Ephemeral HTTPS server used only by the real browser authentication smoke."""

from __future__ import annotations

import argparse
from pathlib import Path

import uvicorn
from argon2 import PasswordHasher
from derivation_api.application import ApiSettings
from derivation_api.fake_service import FakeDerivationService

from derivation_app.factory import create_http_app
from derivation_app.site_identity import SiteIdentityStore, SiteRole
from derivation_app.tenant_runtime import (
    TenantRequestServiceResolver,
    TenantRuntimeRegistry,
)


class EmptyAdminContentReader:
    async def list_runs(self, _user_id):
        return []

    async def get_run(self, _user_id, _run_id):
        raise RuntimeError("smoke fixture has no runs")

    async def list_intakes(self, _user_id):
        return []

    async def get_intake(self, _user_id, _session_id):
        raise RuntimeError("smoke fixture has no intake sessions")


ADMIN_PASSWORD = "admin-server-smoke-private"
BOB_PASSWORD = "bob-server-smoke-private"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--server-root", type=Path, required=True)
    parser.add_argument("--web-dist", type=Path, required=True)
    parser.add_argument("--cert", type=Path, required=True)
    parser.add_argument("--key", type=Path, required=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    origin = f"https://127.0.0.1:{args.port}"
    identity = SiteIdentityStore(
        args.server_root / "identity.sqlite",
        password_hasher=PasswordHasher(time_cost=1, memory_cost=8192, parallelism=1),
    )
    identity.create_account(
        username="admin",
        email="admin@example.test",
        password=ADMIN_PASSWORD,
        role=SiteRole.ADMIN,
        temporary_password=False,
    )
    identity.create_account(
        username="bob",
        email="bob@example.test",
        password=BOB_PASSWORD,
        temporary_password=False,
    )

    async def factory(_paths):
        return FakeDerivationService()

    registry = TenantRuntimeRegistry(args.server_root / "tenants", factory)
    app = create_http_app(
        None,
        settings=ApiSettings(server_origins=(origin,)),
        web_dist=args.web_dist,
        site_identity=identity,
        service_resolver=TenantRequestServiceResolver(registry),
        admin_content_reader=EmptyAdminContentReader(),
    )
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=args.port,
        ssl_certfile=str(args.cert),
        ssl_keyfile=str(args.key),
        log_level="warning",
    )


if __name__ == "__main__":
    main()
