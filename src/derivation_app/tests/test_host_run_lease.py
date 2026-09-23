from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from derivation_app.host_run_lease import (
    LEASE_SCHEMA,
    HostLeasedRuntime,
    HostRunLeaseBusy,
    HostScientificRunLease,
)


class HostScientificRunLeaseTests(unittest.IsolatedAsyncioTestCase):
    def test_capacity_is_one_with_auditable_owner(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "control" / "scientific-run.lock"
            first = HostScientificRunLease.acquire(
                path,
                channel="preview",
                run_id="run-preview",
            )
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["schema_version"], LEASE_SCHEMA)
            self.assertEqual(payload["channel"], "preview")
            self.assertEqual(payload["run_id"], "run-preview")
            with self.assertRaises(HostRunLeaseBusy):
                HostScientificRunLease.acquire(
                    path,
                    channel="stable",
                    run_id="run-stable",
                )

            first.release()
            second = HostScientificRunLease.acquire(
                path,
                channel="stable",
                run_id="run-stable",
            )
            second.release()
            self.assertEqual(path.read_text(encoding="utf-8"), "")

    async def test_runtime_releases_capacity_only_after_clean_close(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "scientific-run.lock"
            lease = HostScientificRunLease.acquire(
                path,
                channel="preview",
                run_id="run-preview",
            )
            runtime = AsyncMock()
            wrapped = HostLeasedRuntime(runtime, lease)

            await wrapped.close()

            runtime.close.assert_awaited_once()
            self.assertFalse(lease.held)

    async def test_failed_runtime_close_keeps_capacity_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "scientific-run.lock"
            lease = HostScientificRunLease.acquire(
                path,
                channel="preview",
                run_id="run-preview",
            )
            runtime = AsyncMock()
            runtime.close.side_effect = RuntimeError("child still running")
            wrapped = HostLeasedRuntime(runtime, lease)

            with self.assertRaisesRegex(RuntimeError, "still running"):
                await wrapped.close()
            self.assertTrue(lease.held)
            lease.release()


if __name__ == "__main__":
    unittest.main()
