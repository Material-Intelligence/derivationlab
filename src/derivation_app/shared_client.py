"""One profile holder that lends an App Server child to concurrent runs.

The exclusive :class:`ProfileInstanceLock` protects a *profile*, not a run, so
running two derivations at once must not mean two children or two locks.  This
holder therefore owns exactly one lock and one authorized App Server child per
process, and hands every run an isolated
:class:`~derivation_runtime.shared_app_server.SharedAppServerSession`: its own
provider threads, its own dynamic-tool objects (and therefore its own source
library), its own tool-call audit file and its own event queue.

Nothing here copies credentials, weakens the lock or widens what a run may
read.  A run that ends or is cancelled releases only its own lease; the child
survives until the holder itself is closed.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from derivation_runtime.app_server_client import (
    DEFAULT_PER_RUN_PROTOCOL_BYTES_TOTAL,
    AppServerClient,
    ClientTimeouts,
)
from derivation_runtime.app_server_protocol import ProviderEvent
from derivation_runtime.capabilities import CapabilityProfile
from derivation_runtime.platform_policy import PlatformFamily
from derivation_runtime.shared_app_server import (
    SharedAppServerBroker,
    SharedAppServerSession,
)
from derivation_runtime.types import RuntimeInvariantError

from .product_profile import (
    PreparedProductLaunch,
    ProductProfile,
    ProfileInstanceLock,
    RunWorkspaceMapping,
    _require_chatgpt_account,
    prepare_holder_workspace,
    prepare_product_launch,
    prepare_shared_run_launch,
)


class SharedProfileClientHolder:
    """Own one profile lock and one App Server child for many concurrent runs."""

    def __init__(
        self,
        *,
        profile: ProductProfile,
        repo_root: Path,
        platform: PlatformFamily,
        architecture: str,
        app_server_executable: Path,
        user_home: Path,
        capability_profile: CapabilityProfile,
        evidence_root: Path | None = None,
        client_timeouts: ClientTimeouts | None = None,
        rate_limit_observer: Callable[[dict[str, Any], bool], None] | None = None,
        expected_runs: int = 1,
    ) -> None:
        if isinstance(expected_runs, bool) or not isinstance(expected_runs, int):
            raise TypeError("expected_runs must be an int")
        if expected_runs < 1:
            raise ValueError("expected_runs must be a positive number of runs")
        for name, path in (
            ("repo_root", repo_root),
            ("app_server_executable", app_server_executable),
            ("user_home", user_home),
        ):
            if not Path(path).is_absolute():
                raise ValueError(f"{name} must be absolute")
        self.profile = profile
        self.repo_root = Path(repo_root).resolve()
        self.platform = platform
        self.architecture = architecture
        self.app_server_executable = Path(app_server_executable)
        self.user_home = Path(user_home).resolve()
        self.capability_profile = capability_profile
        self.evidence_root = (
            self.repo_root / "runs" if evidence_root is None else Path(evidence_root)
        )
        self.client_timeouts = client_timeouts or ClientTimeouts()
        self.rate_limit_observer = rate_limit_observer
        # The child's stdout budget is cumulative across every run that borrows
        # it, so a holder serving a whole round must be sized for the round.
        self.expected_runs = expected_runs
        self.protocol_bytes_budget = (
            DEFAULT_PER_RUN_PROTOCOL_BYTES_TOTAL * expected_runs
        )
        self.holder_id = f"holder_{uuid.uuid4().hex}"
        self._lock: ProfileInstanceLock | None = None
        self._client: AppServerClient | None = None
        self._broker: SharedAppServerBroker | None = None
        self._keepalive: SharedAppServerSession | None = None
        self._prepared: PreparedProductLaunch | None = None
        self._leases = 0
        self._closed = False

    @property
    def client(self) -> AppServerClient:
        if self._client is None:
            raise RuntimeInvariantError("shared App Server holder is not started")
        return self._client

    @property
    def prepared(self) -> PreparedProductLaunch:
        if self._prepared is None:
            raise RuntimeInvariantError("shared App Server holder is not started")
        return self._prepared

    @property
    def is_running(self) -> bool:
        return self._client is not None and self._client.is_running

    @property
    def leases(self) -> int:
        """Run sessions currently borrowing the child, excluding the keepalive."""

        return self._leases

    @property
    def process_events(self) -> tuple[ProviderEvent, ...]:
        return () if self._broker is None else self._broker.process_events

    async def start(self) -> None:
        """Acquire the profile lock and start the single authorized child."""

        if self._closed:
            raise RuntimeInvariantError("shared App Server holder is closed")
        if self._client is not None:
            return
        lock = ProfileInstanceLock(self.profile).acquire()
        self._lock = lock
        client: AppServerClient | None = None
        try:
            mapping = prepare_holder_workspace(
                self.profile, holder_id=self.holder_id, repo_root=self.repo_root
            )
            prepared = prepare_product_launch(
                self.profile,
                lock=lock,
                repo_root=self.repo_root,
                platform=self.platform,
                architecture=self.architecture,
                app_server_executable=self.app_server_executable,
                workspace_mapping=mapping,
                repository=self.repo_root,
                user_home=self.user_home,
                capability_profile=self.capability_profile,
            )
            client = AppServerClient(
                prepared.command.argv,
                cwd=prepared.command.cwd,
                env=prepared.command.environment,
                timeouts=self.client_timeouts,
                max_protocol_bytes_total=self.protocol_bytes_budget,
                **(
                    {"rate_limit_observer": self.rate_limit_observer}
                    if self.rate_limit_observer is not None
                    else {}
                ),
            )
            await client.start()
            # The account check is the same live one every product run makes;
            # per-run authorization still re-checks command, identity and pins.
            _require_chatgpt_account(await client.account_read(refresh_token=False))
            broker = SharedAppServerBroker(client)
            # A holder-owned lease keeps the child alive between waves of runs,
            # so a finished run never closes a client another run still needs.
            keepalive = await broker.acquire_session(
                workspace=mapping.sandbox_workspace
            )
            self._client = client
            self._broker = broker
            self._keepalive = keepalive
            self._prepared = prepared
        except BaseException:
            if client is not None and client.is_running:
                await client.close()
            if lock.held and not lock.runtime_owned:
                lock.release()
            self._lock = None
            raise

    async def acquire_run(
        self,
        *,
        run_mapping: RunWorkspaceMapping,
        dynamic_tool_audit_path: Path,
    ) -> tuple[PreparedProductLaunch, SharedAppServerSession]:
        """Lend one isolated session plus this run's own prepared launch."""

        if self._broker is None or self._client is None or self._prepared is None:
            raise RuntimeInvariantError("shared App Server holder is not started")
        if self._closed:
            raise RuntimeInvariantError("shared App Server holder is closed")
        prepared = prepare_shared_run_launch(
            self._prepared,
            run_mapping=run_mapping,
            repo_root=self.repo_root,
            evidence_root=self.evidence_root,
        )
        session = await self._broker.acquire_session(
            workspace=run_mapping.sandbox_workspace,
            dynamic_tool_audit_path=dynamic_tool_audit_path,
            on_release=self._release,
        )
        self._leases += 1
        return prepared, session

    def _release(self) -> None:
        self._leases = max(0, self._leases - 1)

    async def close(self) -> None:
        """Release the keepalive lease, then the lock once the child is gone."""

        if self._closed:
            return
        self._closed = True
        keepalive, client, lock = self._keepalive, self._client, self._lock
        try:
            if keepalive is not None:
                await keepalive.close()
            if client is not None and client.is_running:
                # Outstanding run leases keep the broker open; closing the
                # holder must still guarantee the child is gone.
                await client.close()
            if client is not None and (client.is_running or client.returncode is None):
                raise RuntimeInvariantError(
                    "shared App Server process exit is unconfirmed; profile lock retained"
                )
        finally:
            if (
                lock is not None
                and lock.held
                and not lock.runtime_owned
                and client is not None
                and not client.is_running
                and client.returncode is not None
            ):
                lock.release()
                self._lock = None


def holder_identity(holder: SharedProfileClientHolder) -> Mapping[str, Any]:
    """A small receipt describing the shared child, for run evidence."""

    identity = holder.client.process_identity
    return {
        "holder_id": holder.holder_id,
        "workspace": str(holder.prepared.workspace_mapping.sandbox_workspace),
        "pid": getattr(identity, "pid", None),
        "server_version": holder.client.server_version,
        "capability_profile": holder.capability_profile.identity,
        "expected_runs": holder.expected_runs,
        "protocol_bytes_budget": holder.protocol_bytes_budget,
    }


__all__ = ["SharedProfileClientHolder", "holder_identity"]
