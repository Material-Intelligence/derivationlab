from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from derivation_app import __main__ as cli


def _codex_stub(root: Path, version: str) -> Path:
    """An executable that answers ``--version`` the way the Codex CLI does."""

    codex = root / "codex"
    codex.write_text(f"#!/bin/sh\necho 'codex-cli {version}'\n", encoding="utf-8")
    codex.chmod(0o700)
    return codex


class ProductCliTests(unittest.TestCase):
    def test_no_command_is_real_product_with_loopback_defaults(self) -> None:
        args = cli.parse_args([])

        self.assertEqual(args.mode, "product")
        self.assertFalse(args.fake)
        self.assertEqual(args.host, "127.0.0.1")
        self.assertEqual(args.port, 8000)
        self.assertEqual(cli._run_root(args), cli.DEFAULT_PRODUCT_RUN_ROOT.resolve())

    def test_fake_requires_exact_dev_fake_opt_in(self) -> None:
        with self.assertRaises(SystemExit):
            cli.parse_args(["dev"])
        with self.assertRaises(SystemExit):
            cli.parse_args(["--fake"])

        args = cli.parse_args(["dev", "--fake"])

        self.assertEqual(args.mode, "dev")
        self.assertTrue(args.fake)
        self.assertEqual(cli._run_root(args), cli.DEFAULT_DEV_RUN_ROOT.resolve())

    def test_packaged_fake_smoke_writes_only_to_injected_data_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = cli.parse_args(
                [
                    "dev",
                    "--fake",
                    "--resource-root",
                    str(root / "resources"),
                    "--data-root",
                    str(root / "Data"),
                    "--release-manifest",
                    str(root / "release-manifest.json"),
                ]
            )

            run_root = cli._run_root(args)

        self.assertEqual(run_root, (root / "Data" / "runs").resolve())

    def test_non_loopback_binding_is_rejected(self) -> None:
        with self.assertRaises(SystemExit):
            cli.parse_args(["--host", "0.0.0.0"])

    def test_server_requires_explicit_root_origin_and_trusted_proxy(self) -> None:
        with self.assertRaises(SystemExit):
            cli.parse_args(["server"])
        with self.assertRaises(SystemExit):
            cli.parse_args(["server", "--server-root", "/srv/derivationlab"])

        args = cli.parse_args(
            [
                "server",
                "--server-root",
                "/srv/derivationlab",
                "--server-profile-root",
                "/srv/derivationlab-profiles",
                "--host-control-root",
                "/srv/derivationlab-control",
                "--channel",
                "preview",
                "--origin",
                "https://pilot.example.test",
                "--trusted-proxy",
                "127.0.0.1",
            ]
        )

        self.assertEqual(args.mode, "server")
        self.assertEqual(args.host, "127.0.0.1")
        self.assertEqual(args.origin, ["https://pilot.example.test"])

    def test_codex_launcher_symlink_is_preserved_for_its_controlled_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "package" / "bin" / "codex.js"
            target.parent.mkdir(parents=True)
            target.write_text("#!/bin/sh\n", encoding="utf-8")
            target.chmod(0o700)
            launcher = root / "bin" / "codex"
            launcher.parent.mkdir()
            launcher.symlink_to(target)

            resolved = cli._resolve_codex_executable(launcher)

        self.assertEqual(resolved, launcher.absolute())

    def test_pinned_codex_version_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            codex = _codex_stub(Path(directory), cli.PINNED_CODEX_VERSION)

            self.assertEqual(cli._require_pinned_codex(codex), codex)

    def test_version_probe_uses_a_throwaway_codex_home(self) -> None:
        """``codex --version`` writes below its home; that must not be ``~/.codex``."""

        seen: dict[str, str] = {}

        def run(command, **kwargs):  # type: ignore[no-untyped-def]
            env = kwargs["env"]
            seen.update(env)
            # The directory exists while the probe runs and is its own home.
            self.assertTrue(Path(env["CODEX_HOME"]).is_dir())
            return SimpleNamespace(
                stdout=f"codex-cli {cli.PINNED_CODEX_VERSION}\n", stderr=""
            )

        launcher = Path("/opt/codex/bin/codex")
        with patch.object(cli.subprocess, "run", side_effect=run) as mocked:
            self.assertEqual(cli._require_pinned_codex(launcher), launcher)

        mocked.assert_called_once()
        self.assertEqual(mocked.call_args.args[0], [str(launcher), "--version"])
        home = Path(seen["CODEX_HOME"])
        self.assertEqual(seen["HOME"], seen["CODEX_HOME"])
        self.assertTrue(home.is_absolute())
        self.assertNotEqual(home, Path.home() / ".codex")
        self.assertNotEqual(home, Path.home())
        self.assertTrue(
            home.resolve().is_relative_to(Path(tempfile.gettempdir()).resolve())
        )
        self.assertTrue(home.name.startswith("derivationlab-codex-version-"))
        # A throwaway home: removed once the probe has answered.
        self.assertFalse(home.exists())
        self.assertEqual(set(seen), {"PATH", "CODEX_HOME", "HOME"})

    def test_other_codex_version_is_refused_with_install_hint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            codex = _codex_stub(Path(directory), "0.145.0")

            with self.assertRaises(cli.CliConfigurationError) as caught:
                cli._require_pinned_codex(codex)

        message = str(caught.exception)
        self.assertIn(f"Codex {cli.PINNED_CODEX_VERSION} is required", message)
        self.assertIn("found 0.145.0", message)
        self.assertIn(f"npm install -g @openai/codex@{cli.PINNED_CODEX_VERSION}", message)

    def test_product_start_with_other_codex_version_exits_2_before_any_setup(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            codex = _codex_stub(root, "0.145.0")
            with (
                patch("derivation_app.__main__.create_product_app") as product_factory,
                patch("derivation_app.__main__.provision_product_profile") as provision,
                patch("sys.stderr") as stderr,
            ):
                status = cli.main(
                    [
                        "--profile-root",
                        str(root / "profile"),
                        "--codex-executable",
                        str(codex),
                    ]
                )

        self.assertEqual(status, 2)
        written = "".join(call.args[0] for call in stderr.write.call_args_list)
        self.assertIn("DerivationLab startup failed: Codex", written)
        product_factory.assert_not_called()
        provision.assert_not_called()

    def test_doctor_reports_a_wrong_codex_version(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            codex = _codex_stub(root, "0.145.0")
            args = cli.parse_args(
                [
                    "doctor",
                    "--profile-root",
                    str(root / "profile"),
                    "--codex-executable",
                    str(codex),
                ]
            )

            report = cli._doctor_report(args)

        self.assertFalse(report["checks"]["codex_executable"]["ok"])
        self.assertIn("found 0.145.0", report["checks"]["codex_executable"]["detail"])
        self.assertFalse(report["ready"])

    def test_default_server_selects_product_factory_and_provisions_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            codex = _codex_stub(root, cli.PINNED_CODEX_VERSION)
            web_dist = root / "dist"
            web_dist.mkdir()
            args = cli.parse_args(
                [
                    "--profile-root",
                    str(root / "profile"),
                    "--run-root",
                    str(root / "runs"),
                    "--web-dist",
                    str(web_dist),
                    "--codex-executable",
                    str(codex),
                ]
            )
            marker = object()
            with (
                patch(
                    "derivation_app.__main__.provision_product_profile",
                    return_value=SimpleNamespace(scientific_runtime=None),
                ) as provision_profile,
                patch(
                    "derivation_app.__main__.provision_scientific_runtime"
                ) as provision_runtime,
                patch(
                    "derivation_app.__main__.create_product_app",
                    return_value=marker,
                ) as product_factory,
                patch("derivation_app.__main__.create_fake_app") as fake_factory,
                patch("derivation_app.__main__._code_commit", return_value="a" * 40),
            ):
                app = cli.create_server_app(args)

        self.assertIs(app, marker)
        provision_profile.assert_called_once()
        provision_runtime.assert_called_once()
        product_factory.assert_called_once()
        fake_factory.assert_not_called()

    def test_explicit_dev_fake_selects_only_fake_factory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            web_dist = root / "dist"
            web_dist.mkdir()
            args = cli.parse_args(["dev", "--fake", "--web-dist", str(web_dist)])
            marker = object()
            with (
                patch(
                    "derivation_app.__main__.create_fake_app", return_value=marker
                ) as fake_factory,
                patch("derivation_app.__main__.create_product_app") as product_factory,
                patch("derivation_app.__main__._code_commit", return_value="b" * 40),
            ):
                app = cli.create_server_app(args)

        self.assertIs(app, marker)
        fake_factory.assert_called_once()
        product_factory.assert_not_called()

    def test_explicit_server_selects_multiuser_factory_without_provisioning_shared_profile(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            codex = _codex_stub(root, cli.PINNED_CODEX_VERSION)
            web_dist = root / "dist"
            web_dist.mkdir()
            args = cli.parse_args(
                [
                    "server",
                    "--server-root",
                    str(root / "server"),
                    "--server-profile-root",
                    str(root / "profiles"),
                    "--host-control-root",
                    str(root / "control"),
                    "--channel",
                    "preview",
                    "--origin",
                    "https://pilot.example.test",
                    "--trusted-proxy",
                    "127.0.0.1",
                    "--web-dist",
                    str(web_dist),
                    "--codex-executable",
                    str(codex),
                ]
            )
            marker = object()
            with (
                patch(
                    "derivation_app.__main__.create_product_server_app",
                    return_value=marker,
                ) as server_factory,
                patch("derivation_app.__main__.create_product_app") as product_factory,
                patch("derivation_app.__main__.provision_product_profile") as provision,
                patch("derivation_app.__main__._code_commit", return_value="c" * 40),
            ):
                app = cli.create_server_app(args)

        self.assertIs(app, marker)
        server_factory.assert_called_once()
        settings = server_factory.call_args.kwargs["settings"]
        self.assertEqual(settings.server_origins, ("https://pilot.example.test",))
        self.assertEqual(settings.trusted_proxy_hosts, ("127.0.0.1",))
        self.assertEqual(settings.site_channel, "preview")
        self.assertEqual(
            server_factory.call_args.kwargs["server_profile_root"],
            (root / "profiles").resolve(),
        )
        self.assertEqual(
            server_factory.call_args.kwargs["host_control_root"],
            (root / "control").resolve(),
        )
        product_factory.assert_not_called()
        provision.assert_not_called()

    def test_server_tls_arguments_must_be_paired(self) -> None:
        common = [
            "server",
            "--server-root",
            "/srv/state",
            "--server-profile-root",
            "/srv/profiles",
            "--host-control-root",
            "/srv/control",
            "--channel",
            "preview",
            "--origin",
            "https://pilot.example.test",
            "--trusted-proxy",
            "127.0.0.1",
        ]
        with self.assertRaises(SystemExit):
            cli.parse_args([*common, "--ssl-certfile", "/srv/tls/cert.pem"])

        args = cli.parse_args(
            [
                *common,
                "--ssl-certfile",
                "/srv/tls/cert.pem",
                "--ssl-keyfile",
                "/srv/tls/key.pem",
            ]
        )
        self.assertEqual(args.channel, "preview")

    def test_server_admin_reads_password_from_prompt_not_process_arguments(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = cli.parse_args(
                [
                    "server-admin",
                    "--server-root",
                    str(root),
                    "--admin-username",
                    "admin",
                    "--admin-email",
                    "admin@example.test",
                ]
            )
            identity = Mock()
            identity.create_account.return_value = SimpleNamespace(
                username="admin",
                user_id="a" * 32,
            )
            with (
                patch(
                    "derivation_app.__main__.getpass.getpass",
                    side_effect=["private-password-value", "private-password-value"],
                ),
                patch(
                    "derivation_app.__main__.SiteIdentityStore", return_value=identity
                ),
            ):
                status = cli.create_server_administrator(args)

        self.assertEqual(status, 0)
        identity.create_account.assert_called_once_with(
            username="admin",
            email="admin@example.test",
            password="private-password-value",
            role=cli.SiteRole.ADMIN,
            temporary_password=False,
            source="server-admin-cli",
        )

    def test_doctor_does_not_select_either_server_factory(self) -> None:
        args = cli.parse_args(["doctor"])
        with (
            patch(
                "derivation_app.__main__._doctor_report",
                return_value={
                    "schema_version": "derivationlab-doctor-v1",
                    "ready": True,
                    "model_calls": 0,
                    "checks": {},
                },
            ),
            patch("derivation_app.__main__.create_product_app") as product_factory,
            patch("derivation_app.__main__.create_fake_app") as fake_factory,
        ):
            status = cli.run_doctor(args)

        self.assertEqual(status, 0)
        product_factory.assert_not_called()
        fake_factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
