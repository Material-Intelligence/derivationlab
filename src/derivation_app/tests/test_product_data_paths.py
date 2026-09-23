from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from derivation_app.factory import create_fake_service

ROOT = Path(__file__).resolve().parents[3]


class ProductDataPathTests(unittest.IsolatedAsyncioTestCase):
    async def test_packaged_service_keeps_mutable_data_outside_resources(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="derivationlab-product-data-"
        ) as directory:
            data_root = Path(directory) / "Data"
            service = create_fake_service(
                run_root=data_root / "runs",
                storage_root=data_root,
                archive_root=data_root / "Legacy Examples",
                repo_root=ROOT,
                code_commit="a" * 40,
            )

            await service.start()
            try:
                health = await service.health()
                account = await service.account()
            finally:
                await service.close()

            self.assertEqual(health.status, "ok")
            self.assertEqual(account.status, "signed_in")
            self.assertTrue((data_root / "runs").is_dir())
            self.assertEqual(service.run_root, (data_root / "runs").resolve())
            self.assertTrue(
                service.report_exporter.report_root.is_relative_to(data_root.resolve())
            )


if __name__ == "__main__":
    unittest.main()
