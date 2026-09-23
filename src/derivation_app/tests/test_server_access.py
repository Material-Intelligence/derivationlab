from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from argon2 import PasswordHasher
from derivation_api.application import ApiSettings, create_app
from derivation_api.fake_service import FakeDerivationService
from derivation_api.models import (
    CreateIntakeSessionRequest,
    CreateRunRequest,
    IntakeRevisionRequest,
)
from derivation_api.service import DerivationServiceError, ErrorKind
from fastapi.testclient import TestClient

from derivation_app.factory import create_fake_service
from derivation_app.site_identity import SiteIdentityStore, SiteRole
from derivation_app.tenant_content import FilesystemTenantContentReader
from derivation_app.tenant_runtime import (
    TenantRequestServiceResolver,
    TenantRuntimeRegistry,
)

ORIGIN = "https://pilot.example.test"
ROOT = Path(__file__).resolve().parents[3]
ALICE_PASSWORD = "alice-correct-private-password"
BOB_PASSWORD = "bob-correct-private-password"
ADMIN_PASSWORD = "admin-correct-private-password"


class FakeTenantContentReader:
    """Expose only services already started by their owning test client."""

    def __init__(self, services: dict[str, FakeDerivationService]) -> None:
        self.services = services
        self.fail_user_ids: set[str] = set()

    async def list_runs(self, user_id: str):
        if user_id in self.fail_user_ids:
            raise DerivationServiceError(
                ErrorKind.UNAVAILABLE,
                "fixture_tenant_content_unavailable",
                "Fixture tenant content is unavailable.",
            )
        service = self.services.get(user_id)
        return [] if service is None else await service.list_runs()

    async def get_run(self, user_id: str, run_id: str):
        service = self.services.get(user_id)
        if service is None:
            raise self._not_found()
        return await service.get_run(run_id)

    async def list_intakes(self, user_id: str):
        service = self.services.get(user_id)
        if service is None:
            return []
        rows = []
        for status in (
            "active",
            "convergence_required",
            "candidate_ready",
            "confirmed",
            "cancelled",
        ):
            rows.extend(await service.list_intake_sessions(status=status))
        return rows

    async def get_intake(self, user_id: str, session_id: str):
        service = self.services.get(user_id)
        if service is None:
            raise self._not_found()
        return await service.get_intake_session(session_id)

    @staticmethod
    def _not_found() -> DerivationServiceError:
        return DerivationServiceError(
            ErrorKind.NOT_FOUND,
            "tenant_content_not_found",
            "Tenant content does not exist.",
        )


def run_request(objective: str) -> dict[str, object]:
    role = {"provider": "openai", "model": "test-model", "effort": "low"}
    return {
        "problem": {
            "problem_id": "server-access-test",
            "version": 1,
            "supersedes_version": None,
            "objective": objective,
            "givens": ["The fixture is deterministic."],
            "assumptions": [],
            "scope": "Server access integration only.",
            "deliverable": "A checked fixture derivation.",
            "allowed_tools": ["scientific_compute"],
            "allowed_references": [],
            "success_criteria": ["The fixture run is created."],
            "source_pack": None,
            "confirmed_by_user": True,
        },
        "config": {
            "granularity": "one_task",
            "writer": role,
            "checker": role,
            "judge": role,
            "backend": {"name": "fixture", "version": "1"},
            "max_model_calls": 3,
            "max_active_branches": 1,
            "reference_allowed": False,
            "allowed_paths": [],
        },
        "runtime": {
            "auth_mode": "chatgpt",
            "concurrency": 1,
            "retries": 0,
            "max_run_seconds": None,
        },
    }


class ServerAccessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        root = Path(self.temporary_directory.name)
        self.root = root
        self.identity = SiteIdentityStore(
            root / "identity.sqlite",
            password_hasher=PasswordHasher(
                time_cost=1,
                memory_cost=8192,
                parallelism=1,
            ),
        )
        self.alice = self.identity.create_account(
            username="alice",
            email="alice@example.test",
            password=ALICE_PASSWORD,
            temporary_password=False,
        )
        self.bob = self.identity.create_account(
            username="bob",
            email="bob@example.test",
            password=BOB_PASSWORD,
            temporary_password=False,
        )
        self.admin = self.identity.create_account(
            username="admin",
            email="admin@example.test",
            password=ADMIN_PASSWORD,
            role=SiteRole.ADMIN,
            temporary_password=False,
        )
        self.services: dict[str, FakeDerivationService] = {}

        async def factory(paths):
            service = FakeDerivationService()
            self.services[paths.user_id] = service
            return service

        registry = TenantRuntimeRegistry(root / "tenant-data", factory)
        resolver = TenantRequestServiceResolver(registry)
        app = create_app(
            None,
            settings=ApiSettings(server_origins=(ORIGIN,)),
            site_identity=self.identity,
            service_resolver=resolver,
            admin_content_reader=FakeTenantContentReader(self.services),
        )
        self.client_context = TestClient(app, base_url=ORIGIN)
        self.client = self.client_context.__enter__()
        self.addCleanup(self.client_context.__exit__, None, None, None)

    def test_server_control_plane_needs_no_shared_product_service(self) -> None:
        health = self.client.get("/healthz")
        build = self.client.get("/api/build-info")

        self.assertEqual(
            health.json(),
            {
                "status": "ok",
                "service": "derivation-tenant-server",
            },
        )
        self.assertEqual(build.status_code, 200)
        self.assertEqual(
            self.client.get("/api/site/mode").json(),
            {"mode": "server", "channel": "development"},
        )
        self.assertEqual(self.services, {})

    def test_filesystem_admin_reader_never_changes_persisted_evidence(self) -> None:
        user_id = "1" * 32
        tenant_root = self.root / "persisted-tenants"
        run_root = tenant_root / "users" / user_id / "runs"
        run_root.mkdir(parents=True)

        async def exercise() -> None:
            service = create_fake_service(
                run_root=run_root,
                storage_root=tenant_root,
                repo_root=ROOT,
            )
            await service.start()
            try:
                request = run_request("Read-only evidence.")
                role = {
                    "provider": "fake",
                    "model": "fake:deterministic",
                    "effort": "deterministic",
                }
                request["config"].update(  # type: ignore[union-attr]
                    {
                        "writer": role,
                        "checker": role,
                        "judge": role,
                        "backend": {
                            "name": "deterministic-fake-runtime",
                            "version": "1",
                        },
                    }
                )
                run = await service.create_run(
                    CreateRunRequest.model_validate(request),
                    idempotency_key="reader-run",
                )
                intake = await service.create_intake_session(
                    CreateIntakeSessionRequest(
                        initial_message="Read-only intake.",
                        model="gpt-5.4",
                        effort="low",
                    ),
                    idempotency_key="reader-intake",
                )
                intake = await service.cancel_intake_session(
                    intake.session_id,
                    IntakeRevisionRequest(base_revision=intake.revision),
                    idempotency_key="reader-intake-cancel",
                )
            finally:
                await service.close()

            before = {
                path.relative_to(tenant_root): (
                    path.stat().st_mtime_ns,
                    path.read_bytes(),
                )
                for path in tenant_root.rglob("*")
                if path.is_file() and not path.name.endswith(("-wal", "-shm"))
            }
            reader = FilesystemTenantContentReader(tenant_root)
            self.assertEqual(
                [item.id for item in await reader.list_runs(user_id)], [run.id]
            )
            self.assertTrue((await reader.get_run(user_id, run.id)).read_only)
            self.assertEqual(
                [item.session_id for item in await reader.list_intakes(user_id)],
                [intake.session_id],
            )
            self.assertEqual(
                (await reader.get_intake(user_id, intake.session_id)).session_id,
                intake.session_id,
            )
            after = {
                path.relative_to(tenant_root): (
                    path.stat().st_mtime_ns,
                    path.read_bytes(),
                )
                for path in tenant_root.rglob("*")
                if path.is_file() and not path.name.endswith(("-wal", "-shm"))
            }
            self.assertEqual(after, before)

        asyncio.run(exercise())

    def login(self, identifier: str, password: str, *, client=None):
        target = client or self.client
        return target.post(
            "/api/site/session",
            headers={"Origin": ORIGIN},
            json={"identifier": identifier, "password": password},
        )

    def test_login_cookie_is_secure_and_session_can_be_revoked(self) -> None:
        without_origin = self.client.post(
            "/api/site/session",
            json={"identifier": "alice", "password": ALICE_PASSWORD},
        )
        self.assertEqual(without_origin.status_code, 403)
        self.assertEqual(without_origin.json()["error"]["code"], "origin_required")

        response = self.login("Alice@Example.Test", ALICE_PASSWORD)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["account"]["user_id"], self.alice.user_id)
        cookie = response.headers["set-cookie"].lower()
        self.assertIn("httponly", cookie)
        self.assertIn("secure", cookie)
        self.assertIn("samesite=strict", cookie)

        current = self.client.get("/api/site/session")
        self.assertEqual(current.status_code, 200)
        self.assertEqual(current.json()["account"]["username"], "alice")

        logged_out = self.client.delete("/api/site/session", headers={"Origin": ORIGIN})
        self.assertEqual(logged_out.status_code, 204)
        self.assertEqual(self.client.get("/api/site/session").status_code, 401)

    def test_untrusted_origin_and_unauthenticated_api_are_rejected(self) -> None:
        unauthenticated = self.client.get("/api/runs")
        self.assertEqual(unauthenticated.status_code, 401)
        self.assertEqual(unauthenticated.headers["x-frame-options"], "DENY")
        self.assertIn(
            "frame-ancestors 'none'",
            unauthenticated.headers["content-security-policy"],
        )
        self.assertEqual(self.client.get("/docs").status_code, 404)
        rejected = self.client.post(
            "/api/site/session",
            headers={"Origin": "https://attacker.example"},
            json={"identifier": "alice", "password": ALICE_PASSWORD},
        )
        self.assertEqual(rejected.status_code, 403)
        self.assertEqual(rejected.json()["error"]["code"], "origin_not_allowed")

    def test_server_configuration_requires_canonical_https_origin(self) -> None:
        with self.assertRaisesRegex(ValueError, "canonical HTTPS origins"):
            ApiSettings(server_origins=(f"{ORIGIN}/",))
        with self.assertRaisesRegex(ValueError, "canonical HTTPS origins"):
            ApiSettings(server_origins=("http://pilot.example.test",))
        with self.assertRaisesRegex(ValueError, "literal IP addresses"):
            ApiSettings(
                server_origins=(ORIGIN,),
                trusted_proxy_hosts=("nas-proxy",),
            )

    def test_each_user_is_routed_to_an_independent_service(self) -> None:
        self.assertEqual(self.login("alice", ALICE_PASSWORD).status_code, 200)
        created = self.client.post(
            "/api/runs",
            headers={"Origin": ORIGIN, "Idempotency-Key": "alice-create"},
            json=run_request("Alice owns this run."),
        )
        self.assertEqual(created.status_code, 201, created.text)
        run_id = created.json()["id"]

        bob_client = TestClient(self.client.app, base_url=ORIGIN)
        try:
            self.assertEqual(
                self.login("bob", BOB_PASSWORD, client=bob_client).status_code,
                200,
            )
            self.assertEqual(bob_client.get(f"/api/runs/{run_id}").status_code, 404)
            self.assertEqual(bob_client.get("/api/runs").json(), [])
        finally:
            bob_client.close()

        alice_runs = self.client.get("/api/runs").json()
        self.assertEqual([item["id"] for item in alice_runs], [run_id])
        self.assertIsNot(
            self.services[self.alice.user_id], self.services[self.bob.user_id]
        )

    def test_admin_can_read_tenant_content_and_every_view_is_audited(self) -> None:
        self.assertEqual(self.login("alice", ALICE_PASSWORD).status_code, 200)
        created_run = self.client.post(
            "/api/runs",
            headers={"Origin": ORIGIN, "Idempotency-Key": "alice-admin-view-run"},
            json=run_request("Alice administrator-readable run."),
        )
        self.assertEqual(created_run.status_code, 201, created_run.text)
        run_id = created_run.json()["id"]
        created_intake = self.client.post(
            "/api/intake/sessions",
            headers={"Origin": ORIGIN, "Idempotency-Key": "alice-admin-view-intake"},
            json={
                "initial_message": "Alice administrator-readable intake.",
                "model": "deterministic",
                "effort": "none",
            },
        )
        self.assertEqual(created_intake.status_code, 201, created_intake.text)
        intake_id = created_intake.json()["session_id"]

        admin_client = TestClient(self.client.app, base_url=ORIGIN)
        bob_client = TestClient(self.client.app, base_url=ORIGIN)
        try:
            self.assertEqual(
                self.login("admin", ADMIN_PASSWORD, client=admin_client).status_code,
                200,
            )
            self.assertEqual(
                self.login("bob", BOB_PASSWORD, client=bob_client).status_code,
                200,
            )
            bob_run_id = None
            bob_intake_id = None
            for index in range(2):
                bob_run = bob_client.post(
                    "/api/runs",
                    headers={"Origin": ORIGIN, "Idempotency-Key": f"bob-run-{index}"},
                    json=run_request(f"Bob-only run {index}."),
                )
                self.assertEqual(bob_run.status_code, 201, bob_run.text)
                bob_run_id = bob_run.json()["id"]
                bob_intake = bob_client.post(
                    "/api/intake/sessions",
                    headers={
                        "Origin": ORIGIN,
                        "Idempotency-Key": f"bob-intake-{index}",
                    },
                    json={
                        "initial_message": f"Bob-only intake {index}.",
                        "model": "deterministic",
                        "effort": "none",
                    },
                )
                self.assertEqual(bob_intake.status_code, 201, bob_intake.text)
                bob_intake_id = bob_intake.json()["session_id"]
            assert bob_run_id is not None
            assert bob_intake_id is not None
            admin_root = f"/api/site/admin/accounts/{self.alice.user_id}"
            admin_runs_response = admin_client.get(f"{admin_root}/runs")
            self.assertEqual(admin_runs_response.headers["cache-control"], "no-store")
            admin_runs = admin_runs_response.json()
            self.assertEqual([item["id"] for item in admin_runs], [run_id])
            self.assertTrue(admin_runs[0]["read_only"])
            admin_run = admin_client.get(f"{admin_root}/runs/{run_id}").json()
            self.assertEqual(admin_run["id"], run_id)
            self.assertTrue(admin_run["read_only"])
            self.assertEqual(
                admin_run["commands"],
                {
                    "can_pause": False,
                    "can_resume": False,
                    "can_interrupt": False,
                    "branchable_step_revision_ids": [],
                },
            )
            self.assertEqual(
                [
                    item["session_id"]
                    for item in admin_client.get(f"{admin_root}/intake/sessions").json()
                ],
                [intake_id],
            )
            self.assertEqual(
                admin_client.get(f"{admin_root}/intake/sessions/{intake_id}").json()[
                    "session_id"
                ],
                intake_id,
            )
            self.assertEqual(bob_client.get(f"{admin_root}/runs").status_code, 403)

            self.assertEqual(
                admin_client.get(f"{admin_root}/runs/{bob_run_id}").status_code,
                404,
            )
            self.assertEqual(
                admin_client.get(
                    f"{admin_root}/intake/sessions/{bob_intake_id}"
                ).status_code,
                404,
            )

            reader = self.client.app.state.admin_content_reader
            reader.fail_user_ids.add(self.alice.user_id)
            self.assertEqual(admin_client.get(f"{admin_root}/runs").status_code, 503)
            reader.fail_user_ids.clear()

            unknown_user_id = "f" * 32
            self.assertEqual(
                admin_client.get(
                    f"/api/site/admin/accounts/{unknown_user_id}/runs"
                ).status_code,
                404,
            )
            self.assertNotIn(unknown_user_id, self.services)
        finally:
            admin_client.close()
            bob_client.close()

        view_events = [
            event
            for event in self.identity.list_audit_events()
            if event.action == "tenant.content_view"
        ]
        self.assertEqual(
            [event.target_type for event in view_events],
            [
                "run_catalog",
                "run",
                "intake_catalog",
                "intake_session",
                "run",
                "intake_session",
                "run_catalog",
                "run_catalog",
            ],
        )
        self.assertEqual(
            [event.outcome for event in view_events],
            [
                "success",
                "success",
                "success",
                "success",
                "not_found",
                "not_found",
                "failure",
                "not_found",
            ],
        )
        self.assertTrue(
            all(event.actor_user_id == self.admin.user_id for event in view_events)
        )
        self.assertEqual(
            [event.details["owner_user_id"] for event in view_events],
            [self.alice.user_id] * 7 + ["f" * 32],
        )

    def test_temporary_password_blocks_product_until_changed(self) -> None:
        temporary = "temporary-private-password"
        replacement = "replacement-private-password"
        self.identity.create_account(
            username="carol",
            email="carol@example.test",
            password=temporary,
            temporary_password=True,
        )
        self.assertEqual(self.login("carol", temporary).status_code, 200)

        blocked = self.client.get("/api/runs")
        self.assertEqual(blocked.status_code, 403)
        self.assertEqual(blocked.json()["error"]["code"], "password_change_required")
        changed = self.client.post(
            "/api/site/password",
            headers={"Origin": ORIGIN},
            json={
                "current_password": temporary,
                "new_password": replacement,
            },
        )
        self.assertEqual(changed.status_code, 204, changed.text)
        self.assertEqual(self.client.get("/api/site/session").status_code, 401)
        self.assertEqual(self.login("carol", replacement).status_code, 200)
        self.assertEqual(self.client.get("/api/runs").status_code, 200)

    def test_only_admin_can_create_reset_and_disable_accounts(self) -> None:
        self.assertEqual(self.login("alice", ALICE_PASSWORD).status_code, 200)
        denied = self.client.post(
            "/api/site/admin/accounts",
            headers={"Origin": ORIGIN},
            json={
                "username": "dave",
                "email": "dave@example.test",
                "password": "dave-initial-private-password",
            },
        )
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(denied.json()["error"]["code"], "site_permission_denied")

        self.assertEqual(self.login("admin", ADMIN_PASSWORD).status_code, 200)
        rejected_legacy_policy = self.client.post(
            "/api/site/admin/accounts",
            headers={"Origin": ORIGIN},
            json={
                "username": "dave",
                "email": "dave@example.test",
                "password": "dave-initial-private-password",
                "temporary_password": True,
            },
        )
        self.assertEqual(rejected_legacy_policy.status_code, 422)

        created = self.client.post(
            "/api/site/admin/accounts",
            headers={"Origin": ORIGIN},
            json={
                "username": "dave",
                "email": "dave@example.test",
                "password": "dave-initial-private-password",
            },
        )
        self.assertEqual(created.status_code, 201, created.text)
        self.assertFalse(created.json()["must_change_password"])
        dave_id = created.json()["user_id"]
        self.assertIn(
            "dave",
            [
                item["username"]
                for item in self.client.get("/api/site/admin/accounts").json()
            ],
        )

        dave_client = TestClient(self.client.app, base_url=ORIGIN)
        try:
            self.assertEqual(
                self.login(
                    "dave", "dave-initial-private-password", client=dave_client
                ).status_code,
                200,
            )
            reset = self.client.post(
                f"/api/site/admin/accounts/{dave_id}/password-reset",
                headers={"Origin": ORIGIN},
                json={"new_password": "dave-reset-private-password"},
            )
            self.assertEqual(reset.status_code, 204, reset.text)
            self.assertEqual(dave_client.get("/api/site/session").status_code, 401)
            self.assertEqual(
                self.login(
                    "dave", "dave-reset-private-password", client=dave_client
                ).status_code,
                200,
            )
            self.assertFalse(
                dave_client.get("/api/site/session").json()["account"][
                    "must_change_password"
                ]
            )
        finally:
            dave_client.close()

        disabled = self.client.post(
            f"/api/site/admin/accounts/{dave_id}/status",
            headers={"Origin": ORIGIN},
            json={"status": "disabled"},
        )
        self.assertEqual(disabled.status_code, 200)
        self.assertEqual(disabled.json()["status"], "disabled")

        last_admin = self.client.post(
            f"/api/site/admin/accounts/{self.admin.user_id}/status",
            headers={"Origin": ORIGIN},
            json={"status": "disabled"},
        )
        self.assertEqual(last_admin.status_code, 403)
        self.assertEqual(last_admin.json()["error"]["code"], "site_permission_denied")


if __name__ == "__main__":
    unittest.main()
