from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from .scientific_runtime import (
    RUNTIME_ID,
    ScientificCalculatorConfig,
    ScientificCalculatorTool,
    ScientificRuntimeError,
    ScientificRuntimeValidation,
    provision_scientific_runtime,
    validate_scientific_runtime,
)


class ScientificRuntimeTests(unittest.TestCase):
    def test_missing_runtime_is_optional_only_when_requested(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            self.assertIsNone(validate_scientific_runtime(root, required=False))
            with self.assertRaisesRegex(ScientificRuntimeError, "not provisioned"):
                validate_scientific_runtime(root)

    @unittest.skipIf(os.name == "nt", "fixture paths below are POSIX-specific")
    def test_provisioner_builds_private_runtime_and_publishes_manifest_last(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)

            def fake_run(argv: list[str], _environment: dict[str, str]) -> None:
                if argv[1:3] == ["python", "install"]:
                    managed = root / "python/cpython-3.13.11-test/bin/python3.13"
                    managed.parent.mkdir(parents=True)
                    managed.write_text("python", encoding="utf-8")
                    managed.chmod(0o500)
                elif argv[1] == "venv":
                    env_python = root / "env/bin/python"
                    env_python.parent.mkdir(parents=True)
                    env_python.write_text("python", encoding="utf-8")
                    env_python.chmod(0o500)

            with patch(
                "derivation_runtime.scientific_runtime._run",
                side_effect=fake_run,
            ) as runner:
                value = provision_scientific_runtime(
                    root, uv_executable=Path("/usr/bin/true")
                )

            self.assertEqual(runner.call_count, 4)
            self.assertEqual(value.runtime_id, RUNTIME_ID)
            self.assertEqual(value, validate_scientific_runtime(root))
            self.assertFalse((root / ".provisioning").exists())
            manifest = json.loads((root / "manifest.json").read_text())
            self.assertEqual(manifest["runtime_id"], RUNTIME_ID)
            self.assertEqual(manifest["packages"]["sympy"], "1.14.0")

    @unittest.skipIf(os.name == "nt", "symlink fixture is POSIX-specific")
    def test_python_symlink_cannot_escape_runtime_root(self) -> None:
        with tempfile.TemporaryDirectory() as raw_parent:
            parent = Path(raw_parent)
            root = parent / "runtime"
            root.mkdir()
            external = parent / "python"
            external.write_text("python", encoding="utf-8")
            env_bin = root / "env/bin"
            env_bin.mkdir(parents=True)
            (env_bin / "python").symlink_to(external)
            from .scientific_runtime import _manifest_payload

            (root / "manifest.json").write_text(
                json.dumps(_manifest_payload(), sort_keys=True, separators=(",", ":"))
                + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ScientificRuntimeError, "outside"):
                validate_scientific_runtime(root)


class _FakeScientificProcess:
    def __init__(self, stdout: bytes, *, delay: float = 0.0) -> None:
        self.returncode: int | None = None
        self._stdout = stdout
        self._delay = delay
        self.killed = False

    async def communicate(self, _stdin: bytes) -> tuple[bytes, bytes]:
        if self._delay:
            import asyncio

            await asyncio.sleep(self._delay)
        self.returncode = 0
        return self._stdout, b""

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    async def wait(self) -> int:
        assert self.returncode is not None
        return self.returncode


class ScientificCalculatorTests(unittest.IsolatedAsyncioTestCase):
    def validation(self, root: Path) -> ScientificRuntimeValidation:
        return ScientificRuntimeValidation(
            runtime_id=RUNTIME_ID,
            root=root,
            python=Path(sys.executable),
            bin_directory=Path(sys.executable).parent,
            manifest_path=root / "manifest.json",
            requirements_sha256="0" * 64,
        )

    async def test_valid_call_uses_only_fixed_isolated_worker(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            validation = self.validation(root)
            process = _FakeScientificProcess(
                b'{"ok":true,"operation":"differentiate","result":"3*x**2"}\n'
            )
            with (
                patch(
                    "derivation_runtime.scientific_runtime.validate_scientific_runtime",
                    return_value=validation,
                ),
                patch(
                    "derivation_runtime.scientific_runtime.asyncio.create_subprocess_exec",
                    return_value=process,
                ) as spawn,
            ):
                tool = ScientificCalculatorTool(validation)
                result = await tool.invoke(
                    {
                        "operation": "differentiate",
                        "expression": "x**3",
                        "variable": "x",
                    }
                )

            self.assertTrue(result["success"])
            self.assertEqual(
                result["contentItems"],
                [
                    {
                        "type": "inputText",
                        "text": ('{"operation":"differentiate","result":"3*x**2"}'),
                    }
                ],
            )
            args = spawn.call_args.args
            kwargs = spawn.call_args.kwargs
            self.assertEqual(args[:4], (str(validation.python), "-I", "-B", "-c"))
            self.assertEqual(kwargs["cwd"], root)
            self.assertNotIn("PATH", kwargs["env"])
            self.assertNotIn("HOME", kwargs["env"])
            self.assertNotIn("CODEX_HOME", kwargs["env"])

    async def test_invalid_call_never_starts_python(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            validation = self.validation(Path(raw_root))
            with (
                patch(
                    "derivation_runtime.scientific_runtime.validate_scientific_runtime",
                    return_value=validation,
                ),
                patch(
                    "derivation_runtime.scientific_runtime.asyncio.create_subprocess_exec"
                ) as spawn,
            ):
                tool = ScientificCalculatorTool(validation)
                result = await tool.invoke(
                    {
                        "operation": "differentiate",
                        "expression": "__import__('os').system('id')",
                        "variable": "x",
                        "path": "/etc/passwd",
                    }
                )
            self.assertFalse(result["success"])
            spawn.assert_not_called()

    async def test_timeout_kills_worker_and_returns_bounded_failure(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            validation = self.validation(Path(raw_root))
            process = _FakeScientificProcess(b"", delay=1.0)
            with (
                patch(
                    "derivation_runtime.scientific_runtime.validate_scientific_runtime",
                    return_value=validation,
                ),
                patch(
                    "derivation_runtime.scientific_runtime.asyncio.create_subprocess_exec",
                    return_value=process,
                ),
            ):
                tool = ScientificCalculatorTool(
                    validation,
                    config=ScientificCalculatorConfig(timeout_seconds=0.01),
                )
                result = await tool.invoke(
                    {
                        "operation": "simplify",
                        "expression": "x + x",
                    }
                )
            self.assertTrue(process.killed)
            self.assertEqual(
                result,
                {
                    "contentItems": [
                        {
                            "type": "inputText",
                            "text": '{"error":"scientific_runtime_timeout"}',
                        }
                    ],
                    "success": False,
                },
            )


if __name__ == "__main__":
    unittest.main()
