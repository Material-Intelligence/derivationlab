"""Start the real DerivationLab product or an explicit fake development server."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

import uvicorn
from derivation_api.application import ApiSettings
from derivation_api.site_access import IdentityError, SiteRole

from derivation_runtime.platform_policy import PINNED_CODEX_VERSION, PlatformFamily
from derivation_runtime.scientific_runtime import (
    ScientificRuntimeError,
    provision_scientific_runtime,
)

from .build_info import (
    BuildInfoError,
    development_build_info,
    load_release_build_info,
)
from .factory import create_fake_app, create_product_app, create_product_server_app
from .product_profile import (
    ProductProfile,
    ProfileConflict,
    provision_product_profile,
    validate_product_profile,
)
from .site_identity import SiteIdentityStore

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DEV_RUN_ROOT = REPO_ROOT / "runs" / "derivation-app-dev"
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


class CliConfigurationError(RuntimeError):
    """The local product cannot be assembled from the supplied CLI settings."""


def _default_profile_root() -> Path:
    system = platform.system()
    if system == "Darwin":
        base = Path.home() / "Library" / "Application Support"
    elif system == "Windows":
        local_app_data = os.environ.get("LOCALAPPDATA")
        base = (
            Path(local_app_data)
            if local_app_data
            else Path.home() / "AppData" / "Local"
        )
    else:
        xdg_data_home = os.environ.get("XDG_DATA_HOME")
        base = (
            Path(xdg_data_home) if xdg_data_home else Path.home() / ".local" / "share"
        )
    return base / "DerivationLab" / "product-profile-v1"


def _default_data_root() -> Path:
    return _default_profile_root().parent / "Data"


DEFAULT_PRODUCT_RUN_ROOT = _default_data_root() / "runs"
DEFAULT_WEB_DIST = REPO_ROOT / "src" / "derivation_web" / "dist"


def _host_platform() -> PlatformFamily:
    try:
        return {
            "Darwin": PlatformFamily.MACOS,
            "Linux": PlatformFamily.LINUX,
            "Windows": PlatformFamily.WINDOWS,
        }[platform.system()]
    except KeyError as exc:
        raise CliConfigurationError(
            f"unsupported host platform: {platform.system()!r}"
        ) from exc


def _code_commit(repo_root: Path = REPO_ROOT) -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise CliConfigurationError(
            "cannot resolve the repository code commit; run from a git clone "
            "(runs record `git rev-parse HEAD` as their code commit)"
        ) from exc
    return result.stdout.strip()


def _resolve_codex_executable(value: Path | None) -> Path:
    configured = value
    if configured is None:
        from_environment = os.environ.get("DERIVATIONLAB_CODEX_EXECUTABLE")
        discovered = from_environment or shutil.which("codex")
        if discovered is None:
            raise CliConfigurationError(
                "Codex is not on PATH; pass --codex-executable or set DERIVATIONLAB_CODEX_EXECUTABLE"
            )
        configured = Path(discovered)
    launcher = configured.expanduser().absolute()
    try:
        executable = launcher.resolve(strict=True)
    except OSError as exc:
        raise CliConfigurationError(
            f"Codex executable does not exist: {configured}"
        ) from exc
    if not executable.is_file():
        raise CliConfigurationError(f"Codex executable is not a file: {executable}")
    if os.name != "nt" and not os.access(executable, os.X_OK):
        raise CliConfigurationError(f"Codex executable is not executable: {executable}")
    # Preserve a launcher symlink (for example /opt/homebrew/bin/codex). Its
    # parent can contain a required interpreter such as node; process identity
    # validation still resolves and checks the canonical target at spawn time.
    return launcher


_CODEX_VERSION = re.compile(r"codex-cli (\d+\.\d+\.\d+\S*)")


def _require_pinned_codex(launcher: Path) -> Path:
    """Refuse a Codex CLI other than the pinned one before anything starts.

    An older CLI rejects the app-server configuration this product passes and
    exits before it answers, which would otherwise surface as an unrelated
    startup traceback. ``--version`` runs with a minimal environment: the
    launcher's own directory (npm installs put ``node`` there), the system
    directories, and a throwaway ``CODEX_HOME`` and ``HOME``. Even
    ``--version`` writes scratch files below the Codex home, and the probe
    must not write into the user's own ``~/.codex``.
    """

    try:
        with tempfile.TemporaryDirectory(prefix="derivationlab-codex-version-") as home:
            completed = subprocess.run(
                [str(launcher), "--version"],
                env={
                    "PATH": os.pathsep.join((str(launcher.parent), "/usr/bin", "/bin")),
                    "CODEX_HOME": home,
                    "HOME": home,
                },
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CliConfigurationError(
            f"cannot run `{launcher} --version`: {exc}"
        ) from exc
    found = _CODEX_VERSION.search(completed.stdout)
    version = found[1] if found else None
    if version != PINNED_CODEX_VERSION:
        seen = version or (completed.stdout.strip() or completed.stderr.strip() or "no version")
        raise CliConfigurationError(
            f"Codex {PINNED_CODEX_VERSION} is required; found {seen} at {launcher}. "
            f"Install it with `npm install -g @openai/codex@{PINNED_CODEX_VERSION}` "
            "or pass --codex-executable"
        )
    return launcher


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument(
        "command",
        nargs="?",
        choices=("doctor", "dev", "server", "server-admin"),
        help=(
            "Omit for the real product. Use 'doctor' for zero-model local checks "
            "or 'dev --fake' for the deterministic fixture."
        ),
    )
    value.add_argument(
        "--fake",
        action="store_true",
        help="Required with 'dev'; unavailable for the normal product entry.",
    )
    value.add_argument("--profile-root", type=Path, default=_default_profile_root())
    value.add_argument("--data-root", type=Path, default=_default_data_root())
    value.add_argument("--resource-root", type=Path, default=REPO_ROOT)
    value.add_argument("--release-manifest", type=Path, default=None)
    value.add_argument("--run-root", type=Path, default=None)
    value.add_argument("--web-dist", type=Path, default=None)
    value.add_argument("--codex-executable", type=Path, default=None)
    value.add_argument("--uv-executable", default="uv")
    value.add_argument("--credential-profile-id", default="derivationlab-product-file")
    value.add_argument("--server-root", type=Path, default=None)
    value.add_argument("--server-profile-root", type=Path, default=None)
    value.add_argument("--host-control-root", type=Path, default=None)
    value.add_argument(
        "--channel",
        choices=("preview", "stable", "staging"),
        default=None,
    )
    value.add_argument("--origin", action="append", default=[])
    value.add_argument("--trusted-proxy", action="append", default=[])
    value.add_argument("--session-cookie-name", default="derivationlab_session")
    value.add_argument("--admin-username", default=None)
    value.add_argument("--admin-email", default=None)
    value.add_argument("--host", default="127.0.0.1")
    value.add_argument("--port", type=int, default=8000)
    value.add_argument("--ssl-certfile", type=Path, default=None)
    value.add_argument("--ssl-keyfile", type=Path, default=None)
    return value


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    cli = parser()
    args = cli.parse_args(argv)
    args.mode = args.command or "product"
    if args.mode == "dev" and not args.fake:
        cli.error("the development fixture must be requested as 'dev --fake'")
    if args.mode != "dev" and args.fake:
        cli.error("--fake is only valid with the 'dev' command")
    if args.host not in LOOPBACK_HOSTS:
        cli.error("DerivationLab is local-only; --host must be a loopback address")
    if not 1 <= args.port <= 65535:
        cli.error("--port must be between 1 and 65535")
    if args.mode in {"server", "server-admin"} and args.server_root is None:
        cli.error(f"the {args.mode} command requires --server-root")
    if args.mode == "server":
        if not args.origin:
            cli.error("the server command requires at least one --origin")
        if not args.trusted_proxy:
            cli.error("the server command requires at least one --trusted-proxy")
        if args.server_profile_root is None:
            cli.error("the server command requires --server-profile-root")
        if args.host_control_root is None:
            cli.error("the server command requires --host-control-root")
        if args.channel is None:
            cli.error("the server command requires --channel")
        if (args.ssl_certfile is None) != (args.ssl_keyfile is None):
            cli.error("--ssl-certfile and --ssl-keyfile must be supplied together")
    if args.mode == "server-admin" and (
        args.admin_username is None or args.admin_email is None
    ):
        cli.error(
            "the server-admin command requires --admin-username and --admin-email"
        )
    return args


def _run_root(args: argparse.Namespace) -> Path:
    if args.run_root is not None:
        return args.run_root.resolve()
    default = (
        _resource_root(args) / "runs" / "derivation-app-dev"
        if args.mode == "dev" and args.release_manifest is None
        else _data_root(args) / "runs"
    )
    return default.resolve()


def _resource_root(args: argparse.Namespace) -> Path:
    return args.resource_root.expanduser().resolve()


def _data_root(args: argparse.Namespace) -> Path:
    return args.data_root.expanduser().resolve()


def _web_dist(args: argparse.Namespace) -> Path:
    configured = args.web_dist or (
        _resource_root(args) / "src" / "derivation_web" / "dist"
    )
    directory = configured.expanduser().resolve()
    if not directory.is_dir():
        raise CliConfigurationError(
            f"web bundle is missing: {directory}; build src/derivation_web first"
        )
    return directory


def _build_info(args: argparse.Namespace):
    resource_root = _resource_root(args)
    openapi_path = resource_root / "src" / "derivation_api" / "openapi.json"
    if args.release_manifest is not None:
        return load_release_build_info(
            args.release_manifest.expanduser().resolve(),
            openapi_path=openapi_path,
        )
    return development_build_info(
        commit=_code_commit(resource_root),
        openapi_path=openapi_path,
    )


def _create_product_server(args: argparse.Namespace):
    executable = _require_pinned_codex(_resolve_codex_executable(args.codex_executable))
    web_dist = _web_dist(args)
    host = _host_platform()
    resource_root = _resource_root(args)
    build_info = _build_info(args)
    code_commit = build_info.commit
    profile = ProductProfile.below(args.profile_root.expanduser())
    validation = provision_product_profile(profile, repo_root=resource_root)
    if validation.scientific_runtime is None:
        provision_scientific_runtime(
            profile.runtime,
            uv_executable=args.uv_executable,
        )
    return create_product_app(
        run_root=_run_root(args),
        storage_root=_data_root(args),
        archive_root=_data_root(args) / "Legacy Examples",
        repo_root=resource_root,
        profile=profile,
        platform=host,
        architecture=platform.machine(),
        app_server_executable=executable,
        user_home=Path.home().resolve(),
        credential_profile_id=args.credential_profile_id,
        code_commit=code_commit,
        build_info=build_info,
        web_dist=web_dist,
    )


def _create_fake_server(args: argparse.Namespace):
    resource_root = _resource_root(args)
    build_info = _build_info(args)
    return create_fake_app(
        run_root=_run_root(args),
        storage_root=(
            _data_root(args)
            if args.release_manifest is not None
            else resource_root / "runs"
        ),
        repo_root=resource_root,
        code_commit=build_info.commit,
        build_info=build_info,
        web_dist=_web_dist(args),
    )


def _create_multiuser_server(args: argparse.Namespace):
    executable = _require_pinned_codex(_resolve_codex_executable(args.codex_executable))
    resource_root = _resource_root(args)
    build_info = _build_info(args)
    assert args.server_root is not None
    assert args.server_profile_root is not None
    assert args.host_control_root is not None
    assert args.channel is not None
    return create_product_server_app(
        server_root=args.server_root.expanduser().resolve(),
        server_profile_root=args.server_profile_root.expanduser().resolve(),
        host_control_root=args.host_control_root.expanduser().resolve(),
        channel=args.channel,
        repo_root=resource_root,
        platform=_host_platform(),
        architecture=platform.machine(),
        app_server_executable=executable,
        user_home=Path.home().resolve(),
        code_commit=build_info.commit,
        build_info=build_info,
        settings=ApiSettings(
            server_origins=tuple(args.origin),
            trusted_proxy_hosts=tuple(args.trusted_proxy),
            session_cookie_name=args.session_cookie_name,
            site_channel=args.channel,
        ),
        web_dist=_web_dist(args),
        uv_executable=args.uv_executable,
    )


def create_server_app(args: argparse.Namespace):
    """Select one composition root; fake is reachable only via ``dev --fake``."""

    if args.mode == "product":
        return _create_product_server(args)
    if args.mode == "dev" and args.fake:
        return _create_fake_server(args)
    if args.mode == "server":
        return _create_multiuser_server(args)
    raise CliConfigurationError(f"command {args.mode!r} does not start a server")


def create_server_administrator(args: argparse.Namespace) -> int:
    """Create one permanent-password administrator without exposing it in argv."""

    assert args.server_root is not None
    password = getpass.getpass("Initial administrator password: ")
    confirmation = getpass.getpass("Confirm administrator password: ")
    if password != confirmation:
        raise CliConfigurationError("administrator passwords do not match")
    identity = SiteIdentityStore(
        args.server_root.expanduser().resolve() / "identity" / "site-identity.sqlite"
    )
    account = identity.create_account(
        username=args.admin_username,
        email=args.admin_email,
        password=password,
        role=SiteRole.ADMIN,
        temporary_password=False,
        source="server-admin-cli",
    )
    print(f"Created administrator {account.username} ({account.user_id}).")
    return 0


def _doctor_report(args: argparse.Namespace) -> dict[str, object]:
    resource_root = _resource_root(args)
    profile = ProductProfile.below(args.profile_root.expanduser())
    checks: dict[str, dict[str, object]] = {}
    try:
        validation = validate_product_profile(profile, repo_root=resource_root)
    except (ProfileConflict, ScientificRuntimeError) as exc:
        checks["product_profile"] = {"ok": False, "detail": str(exc)}
        checks["scientific_runtime"] = {
            "ok": False,
            "detail": "unavailable until the product profile is valid",
        }
        checks["reusable_chatgpt_credential"] = {
            "ok": False,
            "detail": "unavailable until the product profile is valid",
        }
    else:
        checks["product_profile"] = {"ok": True, "detail": "valid"}
        checks["scientific_runtime"] = {
            "ok": validation.scientific_runtime is not None,
            "detail": (
                "valid"
                if validation.scientific_runtime is not None
                else "not provisioned; normal product startup provisions it"
            ),
        }
        checks["reusable_chatgpt_credential"] = {
            "ok": validation.file_credentials_validated,
            "detail": (
                "private auth.json is present"
                if validation.file_credentials_validated
                else "private auth.json is missing"
            ),
        }
    try:
        codex = _require_pinned_codex(_resolve_codex_executable(args.codex_executable))
    except CliConfigurationError as exc:
        checks["codex_executable"] = {"ok": False, "detail": str(exc)}
    else:
        checks["codex_executable"] = {
            "ok": True,
            "detail": f"{codex} (codex-cli {PINNED_CODEX_VERSION})",
        }
    try:
        web_dist = _web_dist(args)
    except CliConfigurationError as exc:
        checks["web_bundle"] = {"ok": False, "detail": str(exc)}
    else:
        checks["web_bundle"] = {"ok": True, "detail": str(web_dist)}
    ready = all(bool(check["ok"]) for check in checks.values())
    return {
        "schema_version": "derivationlab-doctor-v1",
        "ready": ready,
        "model_calls": 0,
        "checks": checks,
    }


def run_doctor(args: argparse.Namespace) -> int:
    report = _doctor_report(args)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["ready"] else 1


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.mode == "doctor":
        return run_doctor(args)
    if args.mode == "server-admin":
        try:
            return create_server_administrator(args)
        except (CliConfigurationError, IdentityError, ValueError) as exc:
            print(f"DerivationLab administrator setup failed: {exc}", file=sys.stderr)
            return 2
    try:
        app = create_server_app(args)
    except (
        CliConfigurationError,
        BuildInfoError,
        ProfileConflict,
        ScientificRuntimeError,
        IdentityError,
        ValueError,
    ) as exc:
        print(f"DerivationLab startup failed: {exc}", file=sys.stderr)
        return 2
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_level="info",
        ssl_certfile=(
            str(args.ssl_certfile.expanduser().resolve())
            if args.ssl_certfile is not None
            else None
        ),
        ssl_keyfile=(
            str(args.ssl_keyfile.expanduser().resolve())
            if args.ssl_keyfile is not None
            else None
        ),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
