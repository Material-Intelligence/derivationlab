"""Portable product profile and thin Codex App Server launch boundary.

The profile isolates credentials and writable workspaces.  It deliberately
does not perform platform attestation at startup: sandbox, tool, resume, and
fork conformance are release/benchmark tests.  Runtime startup checks only the
facts needed to avoid accidental misconfiguration.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform as host_platform
import re
import stat
import time
import uuid
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Self

import tomllib

from derivation_runtime.app_server_client import AppServerClient, SpawnedProcessIdentity
from derivation_runtime.app_server_protocol import ProtocolPin
from derivation_runtime.app_server_runtime import CodexAppServerRuntime, LaunchSettings
from derivation_runtime.capabilities import (
    BENCHMARK_SYMBOLIC_V1,
    INTAKE_V1,
    SOURCE_READING_V1,
    CapabilityProfile,
)
from derivation_runtime.launch_gate import (
    AppServerCommand,
    CredentialStore,
    GateResult,
    GateStatus,
    LaunchBlocked,
    LaunchRequest,
    PathState,
    RuntimeObservation,
    RuntimeRoots,
    build_app_server_command,
    evaluate_launch,
)
from derivation_runtime.platform_policy import (
    PINNED_CODEX_VERSION,
    PINNED_V2_SCHEMA_SHA256,
    PlatformFamily,
)
from derivation_runtime.scientific_runtime import (
    RUNTIME_ID as SCIENTIFIC_RUNTIME_ID,
)
from derivation_runtime.scientific_runtime import (
    ScientificRuntimeError,
    ScientificRuntimeValidation,
    validate_scientific_runtime,
)
from derivation_runtime.shared_app_server import SharedAppServerSession
from derivation_runtime.source_material import SourceLibrary
from derivation_runtime.types import (
    CheckOutput,
    CheckRequest,
    FormulaEquivalenceOutput,
    FormulaEquivalenceRequest,
    FormulaRepairOutput,
    FormulaRepairRequest,
    JudgeOutput,
    JudgeRequest,
    ModelRole,
    ReconcileResult,
    RunConfig,
    RuntimeInterruption,
    RuntimeInvariantError,
    RuntimeInvocation,
    RuntimeSession,
    StepSnapshot,
    WriterOutput,
    WriterRequest,
)


class ProfileMode(StrEnum):
    PRODUCTION = "production"
    TEST = "test"


class ProfileConflict(RuntimeError):
    """The requested local profile is inconsistent or incomplete."""


class ProfileLockHeld(RuntimeError):
    """Another backend already owns this local product profile."""


_PRODUCT_AUTHORITY = object()
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


@dataclass(frozen=True)
class ProductProfile:
    root: Path
    home: Path
    codex_home: Path
    runtime: Path
    workspaces: Path
    credential_store: CredentialStore = CredentialStore.FILE
    allow_file_credentials: bool = True
    mode: ProfileMode = ProfileMode.PRODUCTION

    @classmethod
    def below(
        cls,
        root: str | Path,
        *,
        credential_store: CredentialStore = CredentialStore.FILE,
        allow_file_credentials: bool = True,
        mode: ProfileMode = ProfileMode.PRODUCTION,
    ) -> ProductProfile:
        profile_root = Path(root).resolve()
        return cls(
            root=profile_root,
            home=profile_root / "home",
            codex_home=profile_root / "codex",
            runtime=profile_root / "runtime",
            workspaces=profile_root / "workspaces",
            credential_store=credential_store,
            allow_file_credentials=allow_file_credentials,
            mode=mode,
        )


@dataclass(frozen=True)
class ProfileValidation:
    """One observation of the profile's permission contract.

    ``config_sha256`` fingerprints the *contract*, not the config file bytes:
    Codex's native ``[projects]`` trust table is excluded, because the App
    Server appends an entry to it for every new workspace it is handed.  See
    :func:`_permission_contract_sha256`.
    """

    config_path: Path
    config_sha256: str
    credential_store: CredentialStore
    file_credentials_validated: bool
    scientific_runtime: ScientificRuntimeValidation | None


@dataclass(frozen=True)
class RunWorkspaceMapping:
    run_id: str
    evidence_directory: Path
    sandbox_workspace: Path
    runtime_temp: Path


@dataclass(frozen=True)
class IntakeWorkspaceMapping:
    intake_id: str
    sandbox_workspace: Path
    runtime_temp: Path


@dataclass(frozen=True)
class HolderWorkspaceMapping:
    """The child-process directory of a shared, multi-run App Server holder.

    No Run writes here: it only fixes the process cwd and TMPDIR.  Every run
    that shares the child keeps its own workspace and declares it per thread.
    """

    holder_id: str
    sandbox_workspace: Path
    runtime_temp: Path


WorkspaceMapping = RunWorkspaceMapping | IntakeWorkspaceMapping | HolderWorkspaceMapping


@dataclass(frozen=True)
class PreparedProductLaunch:
    profile: ProductProfile
    validation: ProfileValidation
    workspace_mapping: WorkspaceMapping
    request: LaunchRequest
    command: AppServerCommand
    capability_profile: CapabilityProfile
    lock: ProfileInstanceLock
    # Set only for a run that borrows a shared holder child.  ``request.roots``
    # then describes the holder process while this is the run's own workspace.
    session_workspace: Path | None = None

    @property
    def test_only(self) -> bool:
        return self.profile.mode is ProfileMode.TEST

    @property
    def shared(self) -> bool:
        return self.session_workspace is not None


@dataclass(frozen=True)
class SameClientLiveEvidence:
    collected_monotonic_ns: int
    run_config_sha256: str
    product_config_sha256: str
    process_identity: SpawnedProcessIdentity
    server_version: str
    account_type: str


@dataclass(frozen=True)
class SameClientIntakeEvidence:
    collected_monotonic_ns: int
    product_config_sha256: str
    process_identity: SpawnedProcessIdentity
    server_version: str
    account_type: str


@dataclass(frozen=True)
class AuthorizedModelTurn:
    prepared: PreparedProductLaunch
    client: AppServerClient
    gate_result: GateResult
    settings: LaunchSettings
    live_evidence: SameClientLiveEvidence | None
    _authority: object = field(repr=False, compare=False)
    # A shared holder authorizes the exact child once; each run then drives it
    # through its own isolated session lease, which is what the runtime holds.
    session: SharedAppServerSession | None = field(
        default=None, repr=False, compare=False
    )

    @property
    def test_only(self) -> bool:
        return self.prepared.test_only

    @property
    def runtime_client(self) -> AppServerClient | SharedAppServerSession:
        return self.client if self.session is None else self.session


@dataclass(frozen=True)
class AuthorizedIntakeTurn:
    prepared: PreparedProductLaunch
    client: AppServerClient
    settings: LaunchSettings
    live_evidence: SameClientIntakeEvidence
    _authority: object = field(repr=False, compare=False)


def _run_config_sha256(config: RunConfig) -> str:
    encoded = json.dumps(
        asdict(config), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


class RejectedClientCleanupError(RuntimeError):
    def __init__(
        self,
        *,
        original_error: BaseException,
        cleanup_error: BaseException,
        cleanup: RejectedClientCleanup,
    ) -> None:
        self.original_error = original_error
        self.cleanup_error = cleanup_error
        self.cleanup = cleanup
        super().__init__(
            "rejected App Server cleanup is incomplete; the profile lock remains held"
        )


class RejectedClientCleanup:
    def __init__(self, client: AppServerClient, lock: ProfileInstanceLock) -> None:
        self.__client = client
        self.__lock = lock
        self._closed = False
        lock.transfer_to_runtime(self)

    @property
    def closed(self) -> bool:
        return self._closed

    async def close(self) -> None:
        if self._closed:
            return
        await self.__client.close()
        if self.__client.is_running or self.__client.returncode is None:
            raise RuntimeInvariantError("App Server process exit is unconfirmed")
        self.__lock.release_from_runtime(self)
        self._closed = True


class AuthorizedRuntimeLease:
    """Keep one App Server client and one profile lock for a runtime lifetime."""

    def __init__(
        self,
        runtime: CodexAppServerRuntime,
        authorization: AuthorizedModelTurn,
    ) -> None:
        self.__runtime = runtime
        self.__authorization = authorization
        self._closed = False
        self._close_failed = False
        self._shared = authorization.session is not None
        if not self._shared:
            authorization.prepared.lock.transfer_to_runtime(self)

    @property
    def close_failed(self) -> bool:
        return self._close_failed

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeInvariantError("authorized runtime lease is closed")
        if not self.__authorization.prepared.lock.held:
            raise ProfileLockHeld("product profile lock was released during use")
        if self.__runtime.client is not self.__authorization.runtime_client:
            raise RuntimeInvariantError("runtime client differs from authorized client")

    async def start_writer(
        self, request: WriterRequest, session: RuntimeSession | None
    ) -> RuntimeInvocation:
        self._require_open()
        return await self.__runtime.start_writer(request, session)

    async def collect_writer(self, invocation: RuntimeInvocation) -> WriterOutput:
        self._require_open()
        return await self.__runtime.collect_writer(invocation)

    async def start_checker(self, request: CheckRequest) -> RuntimeInvocation:
        self._require_open()
        return await self.__runtime.start_checker(request)

    async def collect_checker(self, invocation: RuntimeInvocation) -> CheckOutput:
        self._require_open()
        return await self.__runtime.collect_checker(invocation)

    async def start_judge(self, request: JudgeRequest) -> RuntimeInvocation:
        self._require_open()
        return await self.__runtime.start_judge(request)

    async def collect_judge(self, invocation: RuntimeInvocation) -> JudgeOutput:
        self._require_open()
        return await self.__runtime.collect_judge(invocation)

    async def start_formula_repair(
        self, request: FormulaRepairRequest
    ) -> RuntimeInvocation:
        self._require_open()
        return await self.__runtime.start_formula_repair(request)

    async def collect_formula_repair(
        self, invocation: RuntimeInvocation
    ) -> FormulaRepairOutput:
        self._require_open()
        return await self.__runtime.collect_formula_repair(invocation)

    async def start_formula_review(
        self, request: FormulaEquivalenceRequest
    ) -> RuntimeInvocation:
        self._require_open()
        return await self.__runtime.start_formula_review(request)

    async def collect_formula_review(
        self, invocation: RuntimeInvocation
    ) -> FormulaEquivalenceOutput:
        self._require_open()
        return await self.__runtime.collect_formula_review(invocation)

    async def fork(
        self, session: RuntimeSession, completed_operation_id: str
    ) -> RuntimeSession:
        self._require_open()
        return await self.__runtime.fork(session, completed_operation_id)

    async def rehydrate(self, transcript: Sequence[StepSnapshot]) -> RuntimeSession:
        self._require_open()
        return await self.__runtime.rehydrate(transcript)

    async def interrupt(self, invocation: RuntimeInvocation) -> RuntimeInterruption:
        self._require_open()
        return await self.__runtime.interrupt(invocation)

    async def reconcile(self, invocation: RuntimeInvocation) -> ReconcileResult:
        self._require_open()
        return await self.__runtime.reconcile(invocation)

    def register_recovered_writer_session(
        self, session: RuntimeSession, branch_id: str
    ) -> None:
        self._require_open()
        self.__runtime.register_recovered_writer_session(session, branch_id)

    def rebind_branch_writer_session(
        self, branch_id: str, session: RuntimeSession
    ) -> None:
        self._require_open()
        self.__runtime.rebind_branch_writer_session(branch_id, session)

    def evidence_sources(self) -> Mapping[str, str]:
        self._require_open()
        return self.__runtime.evidence_sources()

    def evidence_source_documents(self) -> dict[str, str]:
        self._require_open()
        return self.__runtime.evidence_source_documents()

    def writer_preparation(self, *, include_full: bool) -> Mapping[str, Any] | None:
        self._require_open()
        return self.__runtime.writer_preparation(include_full=include_full)

    async def close(self) -> None:
        if self._closed:
            return
        try:
            session = self.__authorization.session
            if session is not None:
                # Release only this run's lease.  The shared child and the
                # profile lock stay with the holder until its last user leaves.
                if self._close_failed:
                    await session.close()
                else:
                    await self.__runtime.close()
                if session.is_running:
                    raise RuntimeInvariantError(
                        "shared App Server session is still open after shutdown"
                    )
            else:
                if self._close_failed:
                    await self.__authorization.client.close()
                else:
                    await self.__runtime.close()
                if self.__authorization.client.is_running:
                    raise RuntimeInvariantError("App Server still runs after shutdown")
                if self.__authorization.client.returncode is None:
                    raise RuntimeInvariantError(
                        "App Server process exit is unconfirmed"
                    )
                self.__authorization.prepared.lock.release_from_runtime(self)
        except BaseException:
            self._close_failed = True
            raise
        self._closed = True
        self._close_failed = False


def _expected_config(profile: ProductProfile) -> dict[str, Any]:
    permission = BENCHMARK_SYMBOLIC_V1.permission_profile
    return {
        "cli_auth_credentials_store": profile.credential_store.value,
        "default_permissions": permission,
        "project_doc_max_bytes": 0,
        "skills": {"include_instructions": False},
        "permissions": {
            permission: {
                "description": (
                    "Read the immutable product runtime and read/write only the active run workspace."
                ),
                "filesystem": {
                    ":minimal": "read",
                    ":workspace_roots": "write",
                    str(profile.runtime.resolve()): "read",
                },
                "network": {"enabled": False},
            }
        },
    }


def _permission_contract_sha256(parsed_config: Mapping[str, Any]) -> str:
    """Fingerprint the permission contract of an already-parsed config.toml.

    The caller passes the parsed config with Codex's native ``[projects]``
    trust table already dropped, exactly as
    :func:`validate_product_profile` compares it.  Hashing the raw file
    instead would make the fingerprint depend on that table, and the App
    Server appends an entry to it for every new run workspace it is handed --
    so a second concurrent run on one shared child would find the first run's
    authorization snapshot stale and be refused.  The contract itself (the
    credential store, the permission profile and its filesystem/network
    grants) is what authorization must pin, and that is what this hashes.

    Serialization is canonical JSON, so the digest is stable against
    whitespace, key order and other formatting churn in the file.
    """

    if "projects" in parsed_config:
        raise RuntimeInvariantError(
            "permission contract digest must exclude native [projects] state"
        )
    encoded = json.dumps(
        parsed_config, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _render_config(profile: ProductProfile) -> str:
    permission = BENCHMARK_SYMBOLIC_V1.permission_profile
    runtime = json.dumps(str(profile.runtime.resolve()), ensure_ascii=False)
    store = json.dumps(profile.credential_store.value)
    return "\n".join(
        (
            f"cli_auth_credentials_store = {store}",
            f'default_permissions = "{permission}"',
            "project_doc_max_bytes = 0",
            "",
            "[skills]",
            "include_instructions = false",
            "",
            f"[permissions.{permission}]",
            (
                'description = "Read the immutable product runtime and read/write '
                'only the active run workspace."'
            ),
            "",
            f"[permissions.{permission}.filesystem]",
            '":minimal" = "read"',
            '":workspace_roots" = "write"',
            f'{runtime} = "read"',
            "",
            f"[permissions.{permission}.network]",
            "enabled = false",
            "",
        )
    )


def _ensure_private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise ProfileConflict(f"profile path is not a real directory: {path}")
    if os.name != "nt":
        path.chmod(0o700)


def _write_all(descriptor: int, content: bytes) -> None:
    offset = 0
    while offset < len(content):
        written = os.write(descriptor, content[offset:])
        if written <= 0:
            raise OSError("short write while publishing product config")
        offset += written


def _publish_config(config_path: Path, content: str) -> None:
    pending = config_path.parent / f".config.pending.{os.getpid()}.{uuid.uuid4().hex}"
    descriptor: int | None = None
    try:
        descriptor = os.open(pending, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        _write_all(descriptor, content.encode())
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        with suppress(FileExistsError):
            os.link(pending, config_path)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        pending.unlink(missing_ok=True)


def _validate_profile_layout(profile: ProductProfile, repo_root: str | Path) -> None:
    if profile.credential_store is not CredentialStore.FILE:
        raise ProfileConflict(
            "v1 product profiles use portable file credentials; keyring is future work"
        )
    if not profile.allow_file_credentials:
        raise ProfileConflict("file credentials require explicit approval")
    expected = {
        "home": profile.root / "home",
        "codex_home": profile.root / "codex",
        "runtime": profile.root / "runtime",
        "workspaces": profile.root / "workspaces",
    }
    if not profile.root.is_absolute() or profile.root != profile.root.resolve():
        raise ProfileConflict("profile root must be an absolute canonical path")
    for name, path in expected.items():
        if getattr(profile, name) != path:
            raise ProfileConflict(f"profile {name} must be the fixed child {path}")
    repository = Path(repo_root).resolve()
    if profile.mode is ProfileMode.PRODUCTION and (
        profile.root.is_relative_to(repository)
        or repository.is_relative_to(profile.root)
    ):
        raise ProfileConflict("production profile must not overlap the repository")


def _validate_auth_file(profile: ProductProfile) -> bool:
    auth_file = profile.codex_home / "auth.json"
    if not os.path.lexists(auth_file):
        return False
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(auth_file, flags)
    except OSError as exc:
        raise ProfileConflict("auth.json must be an accessible regular file") from exc
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode):
            raise ProfileConflict("auth.json must be a regular file")
        if os.name == "nt":
            return True
        if stat.S_IMODE(details.st_mode) != 0o600:
            raise ProfileConflict("auth.json must have mode 0600")
        if details.st_uid != os.getuid():
            raise ProfileConflict("auth.json must be owned by the current user")
        return True
    finally:
        os.close(descriptor)


def validate_product_profile(
    profile: ProductProfile, *, repo_root: str | Path
) -> ProfileValidation:
    _validate_profile_layout(profile, repo_root)
    for path in (
        profile.root,
        profile.home,
        profile.codex_home,
        profile.runtime,
        profile.workspaces,
    ):
        if path.is_symlink() or not path.is_dir():
            raise ProfileConflict(f"missing product profile directory: {path}")
        if os.name != "nt":
            details = path.stat()
            if details.st_uid != os.getuid():
                raise ProfileConflict(
                    f"profile directory must be owned by the current user: {path}"
                )
            if stat.S_IMODE(details.st_mode) != 0o700:
                raise ProfileConflict(f"profile directory must have mode 0700: {path}")
    config_path = profile.codex_home / "config.toml"
    if config_path.is_symlink() or not config_path.is_file():
        raise ProfileConflict("product CODEX_HOME requires config.toml")
    try:
        parsed = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ProfileConflict(f"cannot parse config.toml: {exc}") from exc
    # Codex records trusted working directories in this native section. It is
    # runtime state, not part of DerivationLab's permission contract, and the
    # App Server rewrites it while runs are in flight -- so it is excluded from
    # both the comparison below and the digest this validation carries.
    parsed.pop("projects", None)
    if parsed != _expected_config(profile):
        raise ProfileConflict("existing config.toml is incompatible")
    if os.name != "nt":
        config_details = config_path.stat()
        if config_details.st_uid != os.getuid():
            raise ProfileConflict("config.toml must be owned by the current user")
        if stat.S_IMODE(config_details.st_mode) != 0o600:
            raise ProfileConflict("config.toml must have mode 0600")
    try:
        scientific_runtime = validate_scientific_runtime(
            profile.runtime, required=False
        )
    except ScientificRuntimeError as exc:
        raise ProfileConflict(str(exc)) from exc
    return ProfileValidation(
        config_path=config_path,
        config_sha256=_permission_contract_sha256(parsed),
        credential_store=profile.credential_store,
        file_credentials_validated=_validate_auth_file(profile),
        scientific_runtime=scientific_runtime,
    )


def provision_product_profile(
    profile: ProductProfile, *, repo_root: str | Path
) -> ProfileValidation:
    _validate_profile_layout(profile, repo_root)
    for path in (
        profile.root,
        profile.home,
        profile.codex_home,
        profile.runtime,
        profile.workspaces,
    ):
        _ensure_private_directory(path)
    config_path = profile.codex_home / "config.toml"
    if not config_path.exists():
        _publish_config(config_path, _render_config(profile))
    if os.name != "nt":
        config_path.chmod(0o600)
    return validate_product_profile(profile, repo_root=repo_root)


class ProfileInstanceLock:
    """One backend may own a profile; reclaim only verified dead POSIX owners."""

    def __init__(self, profile: ProductProfile) -> None:
        self.profile = profile
        self.path = profile.root / "derivation-app.instance.lock"
        self._descriptor: int | None = None
        self._identity: tuple[int, int] | None = None
        self._runtime_owner: object | None = None

    @property
    def held(self) -> bool:
        return self._descriptor is not None

    @property
    def runtime_owned(self) -> bool:
        return self._runtime_owner is not None

    def acquire(self) -> ProfileInstanceLock:
        if self.held:
            raise ProfileLockHeld("this lock is already held")
        try:
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as exc:
            if self._reclaim_dead_owner():
                return self.acquire()
            raise ProfileLockHeld(
                f"profile is already owned; inspect stale lock manually: {self.path}"
            ) from exc
        details = os.fstat(descriptor)
        try:
            _write_all(descriptor, f"pid={os.getpid()}\n".encode())
            os.fsync(descriptor)
        except BaseException:
            os.close(descriptor)
            self.path.unlink(missing_ok=True)
            raise
        self._descriptor = descriptor
        self._identity = (details.st_dev, details.st_ino)
        return self

    def _reclaim_dead_owner(self) -> bool:
        # flock serializes reclaimers of this inode. Recheck the pathname under
        # that lock so a second reclaimer cannot unlink a replacement owner.
        if os.name != "posix":
            return False
        import fcntl

        try:
            descriptor = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
        except OSError:
            return False
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            details = os.fstat(descriptor)
            if (
                not stat.S_ISREG(details.st_mode)
                or details.st_uid != os.getuid()
                or stat.S_IMODE(details.st_mode) != 0o600
            ):
                return False
            match = re.fullmatch(rb"pid=([1-9][0-9]*)\n", os.read(descriptor, 64))
            if match is None:
                return False
            try:
                os.kill(int(match[1]), 0)
            except ProcessLookupError:
                pass
            except (OSError, OverflowError):
                return False
            else:
                return False
            current = self.path.lstat()
            if (current.st_dev, current.st_ino) != (details.st_dev, details.st_ino):
                return False
            self.path.unlink()
            return True
        except OSError:
            return False
        finally:
            os.close(descriptor)

    def transfer_to_runtime(self, owner: object) -> None:
        if not self.held or self._runtime_owner is not None:
            raise ProfileLockHeld("profile lock cannot be transferred")
        self._runtime_owner = owner

    def release_from_runtime(self, owner: object) -> None:
        if self._runtime_owner is not owner:
            raise ProfileLockHeld("runtime does not own this profile lock")
        self._release()
        self._runtime_owner = None

    def release(self) -> None:
        if self._runtime_owner is not None:
            raise ProfileLockHeld("profile lock belongs to a live runtime")
        self._release()

    def _release(self) -> None:
        descriptor, identity = self._descriptor, self._identity
        if descriptor is None or identity is None:
            return
        current = self.path.stat()
        if (current.st_dev, current.st_ino) != identity:
            raise ProfileLockHeld("profile lock path changed while held")
        self.path.unlink()
        os.close(descriptor)
        self._descriptor = None
        self._identity = None

    def __enter__(self) -> Self:
        return self.acquire()

    def __exit__(self, *_: object) -> None:
        if self._runtime_owner is None:
            self.release()


def _validate_run_workspace_mapping(
    profile: ProductProfile,
    mapping: RunWorkspaceMapping,
    *,
    evidence_root: str | Path,
) -> RunWorkspaceMapping:
    if not _RUN_ID_RE.fullmatch(mapping.run_id):
        raise ProfileConflict("run_id is not a portable path component")
    allowed_evidence_root = Path(evidence_root).resolve()
    evidence = mapping.evidence_directory.resolve()
    expected_workspace = profile.workspaces / mapping.run_id
    expected_temp = expected_workspace / ".runtime_tmp"
    if (
        not evidence.is_relative_to(allowed_evidence_root)
        or evidence.name != mapping.run_id
        or not evidence.is_dir()
    ):
        raise ProfileConflict("run evidence must be an existing runs/<run_id>")
    if (
        mapping.sandbox_workspace != expected_workspace
        or mapping.sandbox_workspace.is_symlink()
        or not mapping.sandbox_workspace.is_dir()
        or mapping.sandbox_workspace.resolve() != expected_workspace
    ):
        raise ProfileConflict("sandbox workspace is not the exact profile child")
    if (
        mapping.runtime_temp != expected_temp
        or mapping.runtime_temp.is_symlink()
        or not mapping.runtime_temp.is_dir()
        or mapping.runtime_temp.resolve() != expected_temp
    ):
        raise ProfileConflict("runtime temp is not the exact workspace child")
    if expected_workspace.is_relative_to(evidence) or evidence.is_relative_to(
        expected_workspace
    ):
        raise ProfileConflict("sandbox workspace and run evidence overlap")
    return RunWorkspaceMapping(
        run_id=mapping.run_id,
        evidence_directory=evidence,
        sandbox_workspace=expected_workspace,
        runtime_temp=expected_temp,
    )


def _validate_intake_workspace_mapping(
    profile: ProductProfile,
    mapping: IntakeWorkspaceMapping,
) -> IntakeWorkspaceMapping:
    if not _RUN_ID_RE.fullmatch(mapping.intake_id) or not mapping.intake_id.startswith(
        "intake_"
    ):
        raise ProfileConflict("intake_id is not a portable intake workspace id")
    expected_workspace = profile.workspaces / mapping.intake_id
    expected_temp = expected_workspace / ".runtime_tmp"
    if (
        mapping.sandbox_workspace != expected_workspace
        or mapping.sandbox_workspace.is_symlink()
        or not mapping.sandbox_workspace.is_dir()
        or mapping.sandbox_workspace.resolve() != expected_workspace
    ):
        raise ProfileConflict("intake workspace is not the exact profile child")
    if (
        mapping.runtime_temp != expected_temp
        or mapping.runtime_temp.is_symlink()
        or not mapping.runtime_temp.is_dir()
        or mapping.runtime_temp.resolve() != expected_temp
    ):
        raise ProfileConflict("intake runtime temp is not the exact workspace child")
    return IntakeWorkspaceMapping(
        intake_id=mapping.intake_id,
        sandbox_workspace=expected_workspace,
        runtime_temp=expected_temp,
    )


def prepare_run_workspace(
    profile: ProductProfile,
    *,
    run_id: str,
    evidence_directory: str | Path,
    repo_root: str | Path,
    evidence_root: str | Path | None = None,
) -> RunWorkspaceMapping:
    validate_product_profile(profile, repo_root=repo_root)
    if not _RUN_ID_RE.fullmatch(run_id):
        raise ProfileConflict("run_id is not a portable path component")
    evidence = Path(evidence_directory).resolve()
    repository = Path(repo_root).resolve()
    allowed_evidence_root = Path(evidence_root or (repository / "runs")).resolve()
    if (
        not evidence.is_relative_to(allowed_evidence_root)
        or evidence.name != run_id
        or not evidence.is_dir()
    ):
        raise ProfileConflict("run evidence must be an existing runs/<run_id>")
    workspace = profile.workspaces / run_id
    _ensure_private_directory(workspace)
    runtime_temp = workspace / ".runtime_tmp"
    _ensure_private_directory(runtime_temp)
    return _validate_run_workspace_mapping(
        profile,
        RunWorkspaceMapping(run_id, evidence, workspace, runtime_temp),
        evidence_root=allowed_evidence_root,
    )


def prepare_intake_workspace(
    profile: ProductProfile,
    *,
    intake_id: str,
    repo_root: str | Path,
) -> IntakeWorkspaceMapping:
    """Create one empty private workspace without creating a Run or Record."""

    validate_product_profile(profile, repo_root=repo_root)
    if not _RUN_ID_RE.fullmatch(intake_id) or not intake_id.startswith("intake_"):
        raise ProfileConflict("intake_id is not a portable intake workspace id")
    workspace = profile.workspaces / intake_id
    _ensure_private_directory(workspace)
    runtime_temp = workspace / ".runtime_tmp"
    _ensure_private_directory(runtime_temp)
    return _validate_intake_workspace_mapping(
        profile,
        IntakeWorkspaceMapping(intake_id, workspace, runtime_temp),
    )


def _validate_holder_workspace_mapping(
    profile: ProductProfile,
    mapping: HolderWorkspaceMapping,
) -> HolderWorkspaceMapping:
    if not _RUN_ID_RE.fullmatch(mapping.holder_id) or not mapping.holder_id.startswith(
        "holder_"
    ):
        raise ProfileConflict("holder_id is not a portable holder workspace id")
    expected_workspace = profile.workspaces / mapping.holder_id
    expected_temp = expected_workspace / ".runtime_tmp"
    if (
        mapping.sandbox_workspace != expected_workspace
        or mapping.sandbox_workspace.is_symlink()
        or not mapping.sandbox_workspace.is_dir()
        or mapping.sandbox_workspace.resolve() != expected_workspace
    ):
        raise ProfileConflict("holder workspace is not the exact profile child")
    if (
        mapping.runtime_temp != expected_temp
        or mapping.runtime_temp.is_symlink()
        or not mapping.runtime_temp.is_dir()
        or mapping.runtime_temp.resolve() != expected_temp
    ):
        raise ProfileConflict("holder runtime temp is not the exact workspace child")
    return HolderWorkspaceMapping(
        holder_id=mapping.holder_id,
        sandbox_workspace=expected_workspace,
        runtime_temp=expected_temp,
    )


def prepare_holder_workspace(
    profile: ProductProfile,
    *,
    holder_id: str,
    repo_root: str | Path,
) -> HolderWorkspaceMapping:
    """Create the private child-process directory of a shared App Server holder."""

    validate_product_profile(profile, repo_root=repo_root)
    if not _RUN_ID_RE.fullmatch(holder_id) or not holder_id.startswith("holder_"):
        raise ProfileConflict("holder_id is not a portable holder workspace id")
    workspace = profile.workspaces / holder_id
    _ensure_private_directory(workspace)
    runtime_temp = workspace / ".runtime_tmp"
    _ensure_private_directory(runtime_temp)
    return _validate_holder_workspace_mapping(
        profile,
        HolderWorkspaceMapping(holder_id, workspace, runtime_temp),
    )


def _build_launch_request(
    profile: ProductProfile,
    mapping: WorkspaceMapping,
    *,
    platform: PlatformFamily,
    architecture: str,
    app_server_executable: str | Path,
    repository: str | Path,
    user_home: str | Path,
    capability_profile: CapabilityProfile,
    scientific_runtime: ScientificRuntimeValidation | None,
    windows_sandbox_mode: str | None,
) -> LaunchRequest:
    return LaunchRequest(
        platform=platform,
        architecture=architecture,
        app_server_executable=str(app_server_executable),
        roots=RuntimeRoots(
            home=str(profile.home),
            codex_home=str(profile.codex_home),
            workspace=str(mapping.sandbox_workspace),
            runtime=str(profile.runtime),
            repository=str(repository),
            user_home=str(user_home),
        ),
        expected_codex_version=PINNED_CODEX_VERSION,
        expected_schema_sha256=PINNED_V2_SCHEMA_SHA256,
        credential_store=profile.credential_store,
        permission_profile=capability_profile.permission_profile,
        allowed_skills=(),
        allowed_mcp_servers=capability_profile.mcp_servers,
        allowed_apps=(),
        allowed_command_network_destinations=(),
        tool_path_entries=(
            ()
            if scientific_runtime is None
            else (str(scientific_runtime.bin_directory),)
        ),
        windows_sandbox_mode=windows_sandbox_mode,
    )


def prepare_product_launch(
    profile: ProductProfile,
    *,
    lock: ProfileInstanceLock,
    repo_root: str | Path,
    platform: PlatformFamily,
    architecture: str,
    app_server_executable: str | Path,
    workspace_mapping: WorkspaceMapping,
    repository: str | Path,
    user_home: str | Path,
    capability_profile: CapabilityProfile = BENCHMARK_SYMBOLIC_V1,
    evidence_root: str | Path | None = None,
    windows_sandbox_mode: str | None = None,
    path_states: Mapping[str, PathState] | None = None,
) -> PreparedProductLaunch:
    if lock.profile != profile or not lock.held:
        raise ProfileLockHeld("matching profile lock must remain held")
    validation = validate_product_profile(profile, repo_root=repo_root)
    required_runtime = capability_profile.scientific_runtime_id
    if required_runtime is not None:
        if required_runtime != SCIENTIFIC_RUNTIME_ID:
            raise ProfileConflict(
                f"unsupported scientific runtime {required_runtime!r}"
            )
        if validation.scientific_runtime is None:
            raise ProfileConflict(
                "capability profile requires the sympy_uv runtime; provision it before starting a Run"
            )
    repository_path = Path(repository).resolve()
    if repository_path != Path(repo_root).resolve():
        raise ProfileConflict("launch repository must equal repo_root")
    if type(workspace_mapping) is RunWorkspaceMapping:
        mapping: WorkspaceMapping = _validate_run_workspace_mapping(
            profile,
            workspace_mapping,
            evidence_root=evidence_root or (repository_path / "runs"),
        )
    elif type(workspace_mapping) is IntakeWorkspaceMapping:
        mapping = _validate_intake_workspace_mapping(profile, workspace_mapping)
    elif type(workspace_mapping) is HolderWorkspaceMapping:
        mapping = _validate_holder_workspace_mapping(profile, workspace_mapping)
    else:
        raise ProfileConflict("unsupported product workspace mapping")
    executable = Path(app_server_executable)
    if not executable.is_absolute() or not executable.exists():
        raise ProfileConflict("App Server executable must be an existing absolute path")
    request = _build_launch_request(
        profile,
        mapping,
        platform=platform,
        architecture=architecture,
        app_server_executable=executable,
        repository=repository_path,
        user_home=Path(user_home).resolve(),
        capability_profile=capability_profile,
        scientific_runtime=validation.scientific_runtime,
        windows_sandbox_mode=windows_sandbox_mode,
    )
    return PreparedProductLaunch(
        profile=profile,
        validation=validation,
        workspace_mapping=mapping,
        request=request,
        command=build_app_server_command(request, path_states=path_states),
        capability_profile=capability_profile,
        lock=lock,
    )


def prepare_shared_run_launch(
    holder: PreparedProductLaunch,
    *,
    run_mapping: RunWorkspaceMapping,
    repo_root: str | Path,
    evidence_root: str | Path | None = None,
) -> PreparedProductLaunch:
    """Bind one run to an already-prepared shared App Server holder child.

    The command, request and profile lock stay the holder's; only the run
    workspace is per-run, and every thread of this run declares it explicitly.
    """

    if type(holder.workspace_mapping) is not HolderWorkspaceMapping:
        raise ProfileConflict("shared run launch requires a holder workspace")
    if not holder.lock.held:
        raise ProfileLockHeld("shared holder profile lock must remain held")
    repository = Path(repo_root).resolve()
    if Path(holder.request.roots.repository) != repository:
        raise ProfileConflict("shared run repository differs from the holder")
    mapping = _validate_run_workspace_mapping(
        holder.profile,
        run_mapping,
        evidence_root=evidence_root or (repository / "runs"),
    )
    if mapping.sandbox_workspace == holder.workspace_mapping.sandbox_workspace:
        raise ProfileConflict("shared run workspace collides with the holder child")
    return PreparedProductLaunch(
        profile=holder.profile,
        validation=holder.validation,
        workspace_mapping=mapping,
        request=holder.request,
        command=holder.command,
        capability_profile=holder.capability_profile,
        lock=holder.lock,
        session_workspace=mapping.sandbox_workspace,
    )


def _observed_client_command(client: AppServerClient) -> AppServerCommand:
    if not client.is_running or client.returncode is not None:
        raise RuntimeInvariantError("App Server client is not running")
    if client.cwd is None or client.env is None:
        raise RuntimeInvariantError("App Server client lacks cwd/environment")
    return AppServerCommand(
        argv=client.command,
        cwd=str(client.cwd),
        environment=client.env,
    )


def _validate_process_identity(
    prepared: PreparedProductLaunch, client: AppServerClient
) -> SpawnedProcessIdentity:
    identity = client.process_identity
    if type(identity) is not SpawnedProcessIdentity:
        raise RuntimeInvariantError("App Server process identity is unavailable")
    executable = str(Path(prepared.request.app_server_executable).resolve(strict=True))
    executable_state = os.stat(executable)
    if (
        identity.canonical_executable != executable
        or identity.executable_st_dev != executable_state.st_dev
        or identity.executable_st_ino != executable_state.st_ino
        or identity.pid <= 0
        or identity.spawn_started_monotonic_ns <= 0
        or identity.spawn_completed_monotonic_ns < identity.spawn_started_monotonic_ns
        or identity.identity_source != "resolved_argv0_stat"
    ):
        raise RuntimeInvariantError("App Server process identity is inconsistent")
    return identity


def _validate_runtime_config(
    prepared: PreparedProductLaunch, config: RunConfig
) -> None:
    if (
        config.backend_name != "codex-app-server"
        or config.backend_version != PINNED_CODEX_VERSION
        or config.run_id != prepared.workspace_mapping.run_id
    ):
        raise ProfileConflict("RunConfig differs from the prepared App Server run")
    for role in ModelRole:
        model = config.model_for(role)
        if model.provider != "openai":
            raise ProfileConflict(
                f"{role.value} provider is not configured in the v1 product profile"
            )
        if model.effort == "ultra":
            raise ProfileConflict(f"{role.value} uses unsupported effort 'ultra'")


def _require_chatgpt_account(value: object) -> None:
    if not isinstance(value, Mapping):
        raise ProfileConflict("account/read did not return an object")
    account = value.get("account")
    if not isinstance(account, Mapping) or account.get("type") != "chatgpt":
        raise ProfileConflict("App Server is not using a ChatGPT subscription")


def _finish_authorization(
    prepared: PreparedProductLaunch,
    *,
    client: AppServerClient,
    gate: GateResult,
    live_evidence: SameClientLiveEvidence | None,
    session: SharedAppServerSession | None = None,
) -> AuthorizedModelTurn:
    if _observed_client_command(client) != prepared.command:
        raise RuntimeInvariantError("App Server command differs from prepared launch")
    if not gate.allowed:
        raise LaunchBlocked(gate)
    process_workspace: Path | None = None
    workspace = Path(prepared.request.roots.workspace)
    if prepared.session_workspace is not None:
        if session is None:
            raise RuntimeInvariantError(
                "a shared run must be authorized with its own App Server session"
            )
        if session.workspace != prepared.session_workspace.resolve():
            raise RuntimeInvariantError(
                "App Server session workspace differs from the prepared run"
            )
        process_workspace, workspace = workspace, prepared.session_workspace
    elif session is not None:
        raise RuntimeInvariantError(
            "an exclusive run cannot be authorized through a shared session"
        )
    settings = LaunchSettings(
        workspace=workspace,
        gate_result=gate,
        authorized_command=prepared.command,
        authorized_client=client if session is None else session,
        capability_profile=prepared.capability_profile,
        process_workspace=process_workspace,
        # Operational interruption only; a tool-enabled Record 1.1 run has no
        # total time budget.
        turn_timeout=1800.0
        if prepared.capability_profile == SOURCE_READING_V1
        else 300.0,
    )
    return AuthorizedModelTurn(
        prepared=prepared,
        client=client,
        gate_result=gate,
        settings=settings,
        live_evidence=live_evidence,
        _authority=_PRODUCT_AUTHORITY,
        session=session,
    )


def authorize_model_turn(
    prepared: PreparedProductLaunch,
    *,
    client: AppServerClient,
    observation: RuntimeObservation,
    capability: object | None = None,
    path_states: Mapping[str, PathState] | None = None,
    **_: object,
) -> AuthorizedModelTurn:
    """Test-only authorization seam using explicitly supplied observations."""

    del capability
    if not prepared.test_only:
        raise ProfileConflict("production requires same-client live authorization")
    _validate_process_identity(prepared, client)
    gate = evaluate_launch(
        prepared.request,
        observation,
        path_states=path_states,
    )
    return _finish_authorization(
        prepared,
        client=client,
        gate=gate,
        live_evidence=None,
    )


def _assert_host_matches(request: LaunchRequest) -> None:
    actual = {
        "Darwin": PlatformFamily.MACOS,
        "Linux": PlatformFamily.LINUX,
        "Windows": PlatformFamily.WINDOWS,
    }.get(host_platform.system())
    if actual is not request.platform:
        raise ProfileConflict("requested platform differs from the current host")
    aliases = {
        "aarch64": "arm64",
        "amd64": "x86_64",
    }
    requested_arch = aliases.get(
        request.architecture.casefold(), request.architecture.casefold()
    )
    actual_arch = aliases.get(
        host_platform.machine().casefold(), host_platform.machine().casefold()
    )
    if requested_arch != actual_arch:
        raise ProfileConflict("requested architecture differs from the current host")


async def collect_and_authorize_model_turn(
    prepared: PreparedProductLaunch,
    *,
    config: RunConfig,
    client: AppServerClient,
    session: SharedAppServerSession | None = None,
    **_: object,
) -> AuthorizedModelTurn:
    """Check one initialized client, credential, account, and runtime config."""

    if prepared.test_only:
        raise ProfileConflict("live authorization requires production mode")
    if type(client) is not AppServerClient:
        raise ProfileConflict("production requires an exact AppServerClient")
    if session is not None:
        if type(session) is not SharedAppServerSession:
            raise ProfileConflict("a shared run requires an exact shared session")
        if session.broker.client is not client:
            raise ProfileConflict(
                "shared session belongs to a different App Server child"
            )
    _assert_host_matches(prepared.request)
    validation = validate_product_profile(
        prepared.profile, repo_root=prepared.request.roots.repository
    )
    if validation != prepared.validation or not validation.file_credentials_validated:
        raise ProfileConflict("production requires a private reusable auth.json")
    if _observed_client_command(client) != prepared.command:
        raise RuntimeInvariantError("App Server command differs from prepared launch")
    identity = _validate_process_identity(prepared, client)
    if client.server_version != prepared.request.expected_codex_version:
        raise RuntimeInvariantError("App Server version differs from RunConfig")
    if client.protocol_pin != ProtocolPin():
        raise RuntimeInvariantError("App Server protocol pin differs")
    _validate_runtime_config(prepared, config)
    _require_chatgpt_account(await client.account_read(refresh_token=False))
    live = SameClientLiveEvidence(
        collected_monotonic_ns=time.monotonic_ns(),
        run_config_sha256=_run_config_sha256(config),
        product_config_sha256=validation.config_sha256,
        process_identity=identity,
        server_version=client.server_version,
        account_type="chatgpt",
    )
    return _finish_authorization(
        prepared,
        client=client,
        gate=GateResult(
            status=GateStatus.SUPPORTED,
            stage="model_turn",
            issues=(),
        ),
        live_evidence=live,
        session=session,
    )


async def collect_and_authorize_intake_turn(
    prepared: PreparedProductLaunch,
    *,
    model_provider: str,
    model: str,
    effort: str,
    client: AppServerClient,
) -> AuthorizedIntakeTurn:
    """Authorize one non-Record, tool-free Intake turn on the exact client."""

    if prepared.test_only:
        raise ProfileConflict("live Intake authorization requires production mode")
    if type(prepared.workspace_mapping) is not IntakeWorkspaceMapping:
        raise ProfileConflict("Intake authorization requires an Intake workspace")
    if prepared.capability_profile != INTAKE_V1:
        raise ProfileConflict("Intake authorization requires intake_v1 capability")
    if type(client) is not AppServerClient:
        raise ProfileConflict("production requires an exact AppServerClient")
    if model_provider != "openai" or not model.strip() or not effort.strip():
        raise ProfileConflict("Intake model configuration is invalid")
    if effort == "ultra":
        raise ProfileConflict("Intake uses unsupported effort 'ultra'")
    _assert_host_matches(prepared.request)
    validation = validate_product_profile(
        prepared.profile, repo_root=prepared.request.roots.repository
    )
    if validation != prepared.validation or not validation.file_credentials_validated:
        raise ProfileConflict("production requires a private reusable auth.json")
    if _observed_client_command(client) != prepared.command:
        raise RuntimeInvariantError("App Server command differs from prepared launch")
    identity = _validate_process_identity(prepared, client)
    if client.server_version != prepared.request.expected_codex_version:
        raise RuntimeInvariantError("App Server version differs from Intake config")
    if client.protocol_pin != ProtocolPin():
        raise RuntimeInvariantError("App Server protocol pin differs")
    _require_chatgpt_account(await client.account_read(refresh_token=False))
    gate = GateResult(
        status=GateStatus.SUPPORTED,
        stage="intake_turn",
        issues=(),
    )
    settings = LaunchSettings(
        workspace=Path(prepared.request.roots.workspace),
        gate_result=gate,
        authorized_command=prepared.command,
        authorized_client=client,
        capability_profile=prepared.capability_profile,
    )
    return AuthorizedIntakeTurn(
        prepared=prepared,
        client=client,
        settings=settings,
        live_evidence=SameClientIntakeEvidence(
            collected_monotonic_ns=time.monotonic_ns(),
            product_config_sha256=validation.config_sha256,
            process_identity=identity,
            server_version=client.server_version,
            account_type="chatgpt",
        ),
        _authority=_PRODUCT_AUTHORITY,
    )


def create_authorized_runtime(
    *,
    config: RunConfig,
    authorization: AuthorizedModelTurn,
    source_library: SourceLibrary | None = None,
    writer_preparation: Mapping[str, Any] | None = None,
) -> AuthorizedRuntimeLease:
    if (
        type(authorization) is not AuthorizedModelTurn
        or authorization._authority is not _PRODUCT_AUTHORITY
        or authorization.test_only
    ):
        raise RuntimeInvariantError("production runtime authorization is invalid")
    live = authorization.live_evidence
    if (
        type(live) is not SameClientLiveEvidence
        or live.run_config_sha256 != _run_config_sha256(config)
        or live.product_config_sha256 != authorization.prepared.validation.config_sha256
        or live.process_identity != authorization.client.process_identity
        or live.account_type != "chatgpt"
    ):
        raise RuntimeInvariantError("same-client authorization is stale or mismatched")
    _validate_process_identity(authorization.prepared, authorization.client)
    if _observed_client_command(authorization.client) != authorization.prepared.command:
        raise RuntimeInvariantError("App Server command changed after authorization")
    if (authorization.session is None) is not (
        authorization.prepared.session_workspace is None
    ):
        raise RuntimeInvariantError(
            "shared run authorization and prepared launch disagree"
        )
    runtime = CodexAppServerRuntime.from_authorized_client(
        config=config,
        client=authorization.runtime_client,
        settings=authorization.settings,
        **({"source_library": source_library} if source_library is not None else {}),
        **(
            {"writer_preparation": writer_preparation}
            if writer_preparation is not None
            else {}
        ),
    )
    return AuthorizedRuntimeLease(runtime, authorization)


async def create_product_runtime(
    *,
    config: RunConfig,
    prepared: PreparedProductLaunch,
    client: AppServerClient,
    source_library: SourceLibrary | None = None,
    writer_preparation: Mapping[str, Any] | None = None,
    **_: object,
) -> AuthorizedRuntimeLease:
    cleanup: RejectedClientCleanup | None = None
    try:
        authorization = await collect_and_authorize_model_turn(
            prepared, config=config, client=client
        )
        return create_authorized_runtime(
            config=config,
            authorization=authorization,
            **(
                {"source_library": source_library} if source_library is not None else {}
            ),
            **(
                {"writer_preparation": writer_preparation}
                if writer_preparation is not None
                else {}
            ),
        )
    except BaseException as original_error:
        try:
            cleanup = RejectedClientCleanup(client, prepared.lock)
            await cleanup.close()
        except BaseException as cleanup_error:
            if cleanup is None:
                raise
            raise RejectedClientCleanupError(
                original_error=original_error,
                cleanup_error=cleanup_error,
                cleanup=cleanup,
            ) from cleanup_error
        raise


async def create_shared_product_runtime(
    *,
    config: RunConfig,
    prepared: PreparedProductLaunch,
    client: AppServerClient,
    session: SharedAppServerSession,
    source_library: SourceLibrary | None = None,
    writer_preparation: Mapping[str, Any] | None = None,
) -> AuthorizedRuntimeLease:
    """Authorize one run on a shared holder child and bind it to its session.

    A rejected run closes only its own session lease: the shared child, the
    other runs and the profile lock belong to the holder, not to this run.
    """

    if prepared.session_workspace is None:
        raise ProfileConflict("shared runtime creation requires a shared run launch")
    try:
        authorization = await collect_and_authorize_model_turn(
            prepared, config=config, client=client, session=session
        )
        return create_authorized_runtime(
            config=config,
            authorization=authorization,
            **(
                {"source_library": source_library} if source_library is not None else {}
            ),
            **(
                {"writer_preparation": writer_preparation}
                if writer_preparation is not None
                else {}
            ),
        )
    except BaseException:
        await session.close()
        raise


__all__ = [
    "AuthorizedIntakeTurn",
    "AuthorizedModelTurn",
    "AuthorizedRuntimeLease",
    "HolderWorkspaceMapping",
    "IntakeWorkspaceMapping",
    "PreparedProductLaunch",
    "ProductProfile",
    "ProfileConflict",
    "ProfileInstanceLock",
    "ProfileLockHeld",
    "ProfileMode",
    "ProfileValidation",
    "RejectedClientCleanupError",
    "RunWorkspaceMapping",
    "SameClientIntakeEvidence",
    "SameClientLiveEvidence",
    "authorize_model_turn",
    "collect_and_authorize_intake_turn",
    "collect_and_authorize_model_turn",
    "create_authorized_runtime",
    "create_product_runtime",
    "create_shared_product_runtime",
    "prepare_holder_workspace",
    "prepare_intake_workspace",
    "prepare_product_launch",
    "prepare_run_workspace",
    "prepare_shared_run_launch",
    "provision_product_profile",
    "validate_product_profile",
]
