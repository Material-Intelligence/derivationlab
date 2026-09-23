from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from derivation_app.build_info import (
    BuildInfoError,
    development_build_info,
    load_release_build_info,
)


class BuildInfoTests(unittest.TestCase):
    def test_release_manifest_must_match_bundled_openapi(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            openapi = root / "openapi.json"
            openapi.write_text('{"openapi":"3.1.0"}\n', encoding="utf-8")
            digest = hashlib.sha256(openapi.read_bytes()).hexdigest()
            manifest = root / "release.json"
            manifest.write_text(
                json.dumps(
                    {
                        "schema_version": "derivationlab-build-info-v1",
                        "version": "2026.8.31",
                        "build_number": "20260831.170102",
                        "release_id": "2026.8.31-20260831.170102-abcdef0",
                        "commit": "a" * 40,
                        "openapi_sha256": digest,
                        "product_mode": "release",
                    }
                ),
                encoding="utf-8",
            )

            loaded = load_release_build_info(manifest, openapi_path=openapi)
            openapi.write_text('{"openapi":"changed"}\n', encoding="utf-8")

            self.assertEqual(loaded.commit, "a" * 40)
            with self.assertRaisesRegex(BuildInfoError, "digest"):
                load_release_build_info(manifest, openapi_path=openapi)

    def test_development_identity_hashes_real_contract_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            openapi = Path(directory) / "openapi.json"
            openapi.write_bytes(b"contract")

            value = development_build_info(commit="b" * 40, openapi_path=openapi)

            self.assertEqual(value.product_mode, "development")
            self.assertEqual(value.release_id, "development-bbbbbbb")
            self.assertEqual(
                value.openapi_sha256,
                hashlib.sha256(b"contract").hexdigest(),
            )


if __name__ == "__main__":
    unittest.main()
