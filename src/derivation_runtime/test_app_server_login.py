"""Focused offline tests for the typed App Server device-login boundary."""

from __future__ import annotations

import json
import math
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

from .app_server_client import AppServerClient
from .app_server_protocol import (
    KNOWN_ACCOUNT_AUTH_MODES,
    KNOWN_ACCOUNT_PLAN_TYPES,
    AppServerProtocolError,
    AppServerStateError,
    AppServerTimeoutError,
    MalformedProtocolMessage,
    UnknownProtocolStateError,
    normalize_notification,
)

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
PINNED_SCHEMA = (
    HERE / "protocol_schema" / "codex_app_server_protocol.v2.schemas.json"
)


class AppServerLoginTests(unittest.IsolatedAsyncioTestCase):
    def make_running_client(self) -> AppServerClient:
        client = AppServerClient(("/fixture/codex",))
        client._state = type(client._state).RUNNING
        return client

    @staticmethod
    def start_result(**overrides: Any) -> dict[str, Any]:
        result: dict[str, Any] = {
            "type": "chatgptDeviceCode",
            "loginId": "fixture-login-id",
            "userCode": "FIXTURE-CODE",
            "verificationUrl": "https://auth.openai.com/codex/device",
        }
        result.update(overrides)
        return result

    async def start_login(self, client: AppServerClient):  # type: ignore[no-untyped-def]
        request = AsyncMock(return_value=self.start_result())
        with patch.object(client, "request", request):
            login = await client.account_login_start_chatgpt_device_code()
        request.assert_awaited_once_with(
            "account/login/start", {"type": "chatgptDeviceCode"}
        )
        return login

    async def test_start_wait_and_event_queue_are_correlated_without_secret_repr(
        self,
    ) -> None:
        client = self.make_running_client()
        login = await self.start_login(client)

        representation = repr(login)
        self.assertNotIn(login.login_id, representation)
        self.assertNotIn(login.user_code, representation)
        self.assertIn(login.verification_url, representation)

        client._dispatch_message(
            {
                "method": "account/login/completed",
                "params": {
                    "success": True,
                    "error": None,
                    "loginId": login.login_id,
                    "onboardingEntrypoint": None,
                },
            }
        )
        completion = await client.wait_account_login(login, timeout=0.2)
        self.assertTrue(completion.success)
        self.assertIsNone(completion.error)
        self.assertNotIn(login.login_id, repr(completion))

        queued = await client.next_event(timeout=0.2)
        self.assertEqual(queued.method, "account/login/completed")

    async def test_completion_can_arrive_before_start_response_is_consumed(
        self,
    ) -> None:
        client = self.make_running_client()
        client._dispatch_message(
            {
                "method": "account/login/completed",
                "params": {
                    "success": True,
                    "loginId": "fixture-login-id",
                },
            }
        )
        login = await self.start_login(client)
        completion = await client.wait_account_login(login, timeout=0.2)
        self.assertTrue(completion.success)

    async def test_waiter_is_finite_and_handle_is_client_bound(self) -> None:
        owner = self.make_running_client()
        login = await self.start_login(owner)

        for timeout in (0.0, -1.0, math.inf, math.nan):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                await owner.wait_account_login(login, timeout=timeout)
        with self.assertRaises(AppServerTimeoutError) as caught:
            await owner.wait_account_login(login, timeout=0.01)
        self.assertEqual(caught.exception.operation, "account/login/completed")

        other = self.make_running_client()
        with self.assertRaises(AppServerStateError):
            await other.wait_account_login(login, timeout=0.2)

    async def test_cancel_uses_handle_id_and_accepts_only_pinned_statuses(self) -> None:
        client = self.make_running_client()
        login = await self.start_login(client)
        cancel = AsyncMock(return_value={"status": "canceled"})
        with patch.object(client, "request", cancel):
            status = await client.account_login_cancel(login)
        self.assertEqual(status, "canceled")
        cancel.assert_awaited_once_with(
            "account/login/cancel", {"loginId": login.login_id}
        )

        malformed = self.make_running_client()
        malformed_login = await self.start_login(malformed)
        with (
            patch.object(
                malformed,
                "request",
                AsyncMock(return_value={"status": "futureStatus"}),
            ),
            self.assertRaises(UnknownProtocolStateError),
        ):
            await malformed.account_login_cancel(malformed_login)
        self.assertFalse(malformed.is_running)

    async def test_start_response_requires_exact_device_code_shape(self) -> None:
        malformed_results = (
            {**self.start_result(), "unexpected": True},
            self.start_result(type="chatgpt"),
            self.start_result(userCode=""),
            self.start_result(loginId=" fixture-login-id"),
            self.start_result(verificationUrl="http://auth.openai.com/codex/device"),
            self.start_result(
                verificationUrl="https://user@auth.openai.com/codex/device"
            ),
            self.start_result(
                verificationUrl="https://auth.openai.com/codex/device#fragment"
            ),
        )
        for result in malformed_results:
            with self.subTest(fields=tuple(result)):
                client = self.make_running_client()
                with (
                    patch.object(client, "request", AsyncMock(return_value=result)),
                    self.assertRaises(AppServerProtocolError),
                ):
                    await client.account_login_start_chatgpt_device_code()
                self.assertFalse(client.is_running)

    def test_login_and_account_notifications_follow_pinned_shapes(self) -> None:
        completed = normalize_notification(
            {
                "method": "account/login/completed",
                "params": {
                    "success": False,
                    "error": "fixture failure",
                    "loginId": "fixture-login-id",
                    "onboardingEntrypoint": "life_sciences",
                },
            },
            1,
        )
        self.assertEqual(completed.method, "account/login/completed")
        account = normalize_notification(
            {
                "method": "account/updated",
                "params": {"authMode": "chatgpt", "planType": "pro"},
            },
            2,
        )
        self.assertEqual(account.method, "account/updated")

        malformed = (
            (
                "account/login/completed",
                {"success": True, "unexpected": True},
                MalformedProtocolMessage,
            ),
            (
                "account/login/completed",
                {"success": "true"},
                MalformedProtocolMessage,
            ),
            (
                "account/login/completed",
                {"success": True, "onboardingEntrypoint": "future"},
                UnknownProtocolStateError,
            ),
            (
                "account/updated",
                {"authMode": "future"},
                UnknownProtocolStateError,
            ),
            (
                "account/updated",
                {"planType": "future"},
                UnknownProtocolStateError,
            ),
        )
        for method, params, error_type in malformed:
            with (
                self.subTest(method=method, params=params),
                self.assertRaises(error_type),
            ):
                normalize_notification({"method": method, "params": params}, 3)

    def test_login_contract_constants_match_checked_in_schema(self) -> None:
        definitions = json.loads(PINNED_SCHEMA.read_text(encoding="utf-8"))[
            "definitions"
        ]
        start_variant = next(
            variant
            for variant in definitions["LoginAccountResponse"]["oneOf"]
            if variant["properties"]["type"].get("enum") == ["chatgptDeviceCode"]
        )
        self.assertEqual(
            set(start_variant["properties"]),
            {"type", "loginId", "userCode", "verificationUrl"},
        )
        self.assertEqual(
            set(start_variant["required"]),
            {"type", "loginId", "userCode", "verificationUrl"},
        )

        completed = definitions["AccountLoginCompletedNotification"]
        self.assertEqual(
            set(completed["properties"]),
            {"success", "error", "loginId", "onboardingEntrypoint"},
        )
        self.assertEqual(completed["required"], ["success"])
        updated = definitions["AccountUpdatedNotification"]
        self.assertEqual(set(updated["properties"]), {"authMode", "planType"})
        self.assertNotIn("required", updated)

        auth_modes = {
            value
            for variant in definitions["AuthMode"]["oneOf"]
            for value in variant["enum"]
        }
        self.assertEqual(auth_modes, KNOWN_ACCOUNT_AUTH_MODES)
        self.assertEqual(set(definitions["PlanType"]["enum"]), KNOWN_ACCOUNT_PLAN_TYPES)
        self.assertEqual(
            definitions["CancelLoginAccountStatus"]["enum"],
            ["canceled", "notFound"],
        )

    async def test_uncorrelated_or_duplicate_terminal_events_fail_closed(self) -> None:
        missing_id = self.make_running_client()
        await self.start_login(missing_id)
        with self.assertRaises(AppServerProtocolError):
            missing_id._dispatch_message(
                {
                    "method": "account/login/completed",
                    "params": {"success": True},
                }
            )

        duplicate = self.make_running_client()
        login = await self.start_login(duplicate)
        terminal = {
            "method": "account/login/completed",
            "params": {"success": True, "loginId": login.login_id},
        }
        duplicate._dispatch_message(terminal)
        with self.assertRaises(AppServerProtocolError):
            duplicate._dispatch_message(terminal)

        missing_error = self.make_running_client()
        missing_error_login = await self.start_login(missing_error)
        with self.assertRaises(MalformedProtocolMessage):
            missing_error._dispatch_message(
                {
                    "method": "account/login/completed",
                    "params": {
                        "success": False,
                        "loginId": missing_error_login.login_id,
                    },
                }
            )


if __name__ == "__main__":
    unittest.main()
