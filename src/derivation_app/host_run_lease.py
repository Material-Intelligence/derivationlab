"""Cross-process lease limiting scientific runs across Server channels."""

from __future__ import annotations

import fcntl
import json
import os
import stat
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from derivation_runtime.types import ModelRuntime

LEASE_SCHEMA = "derivationlab-host-scientific-run-lease-v1"
_ACTIVE_PATHS: set[Path] = set()
_FAILED_LEASES: list[HostScientificRunLease] = []
_PROCESS_GUARD = threading.Lock()


class HostRunLeaseError(RuntimeError):
    """The host-wide scientific-run capacity is unavailable or unsafe."""


class HostRunLeaseBusy(HostRunLeaseError):
    """Another channel or tenant already owns the host lease."""


class HostScientificRunLease:
    """One non-blocking, process-aware file lease with auditable ownership."""

    def __init__(self, path: Path, descriptor: int) -> None:
        self.path = path
        self._descriptor = descriptor
        self._held = True

    @classmethod
    def acquire(
        cls, path: str | Path, *, channel: str, run_id: str
    ) -> HostScientificRunLease:
        target = Path(path).expanduser().resolve()
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if target.parent.is_symlink() or not target.parent.is_dir():
            raise HostRunLeaseError("host control root must be a real directory")
        target.parent.chmod(0o700)
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(target, flags, 0o600)
        registered = False
        try:
            details = os.fstat(descriptor)
            if details.st_uid != os.getuid() or not stat.S_ISREG(details.st_mode):
                raise HostRunLeaseError(
                    "host lease must be a current-user regular file"
                )
            os.fchmod(descriptor, 0o600)
            with _PROCESS_GUARD:
                if target in _ACTIVE_PATHS:
                    raise HostRunLeaseBusy(
                        "another scientific run owns the host lease in this process"
                    )
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise HostRunLeaseBusy(
                        "another process owns the host scientific-run lease"
                    ) from exc
                _ACTIVE_PATHS.add(target)
                registered = True
            payload = {
                "schema_version": LEASE_SCHEMA,
                "channel": channel,
                "run_id": run_id,
                "pid": os.getpid(),
                "acquired_at": datetime.now(UTC).isoformat(),
            }
            encoded = (json.dumps(payload, sort_keys=True) + "\n").encode()
            os.ftruncate(descriptor, 0)
            os.lseek(descriptor, 0, os.SEEK_SET)
            os.write(descriptor, encoded)
            os.fsync(descriptor)
            return cls(target, descriptor)
        except BaseException:
            if registered:
                with _PROCESS_GUARD:
                    _ACTIVE_PATHS.discard(target)
            os.close(descriptor)
            raise

    @property
    def held(self) -> bool:
        return self._held

    def release(self) -> None:
        if not self._held:
            return
        os.ftruncate(self._descriptor, 0)
        os.fsync(self._descriptor)
        fcntl.flock(self._descriptor, fcntl.LOCK_UN)
        os.close(self._descriptor)
        with _PROCESS_GUARD:
            _ACTIVE_PATHS.discard(self.path)
        self._held = False

    def retain_after_cleanup_failure(self) -> None:
        """Keep capacity fail-closed when the provider child may still exist."""

        if self._held:
            _FAILED_LEASES.append(self)


class HostLeasedRuntime:
    """Release the host lease only after the exact provider runtime closes."""

    def __init__(self, runtime: ModelRuntime, lease: HostScientificRunLease) -> None:
        self._runtime = runtime
        self._lease = lease

    def __getattr__(self, name: str) -> Any:
        return getattr(self._runtime, name)

    async def close(self) -> None:
        close = getattr(self._runtime, "close", None)
        if not callable(close):
            raise HostRunLeaseError("leased runtime has no close method")
        await close()
        self._lease.release()


__all__ = [
    "LEASE_SCHEMA",
    "HostLeasedRuntime",
    "HostRunLeaseBusy",
    "HostRunLeaseError",
    "HostScientificRunLease",
]
