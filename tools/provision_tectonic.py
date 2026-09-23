#!/usr/bin/env python3
"""Provision the pinned Tectonic runtime that PDF reporting uses.

The runtime is the Tectonic 0.17.0 binary plus a TeX resource bundle of 332
files. It is third-party software under several licences, so this repository
does not carry it. This script fetches it, checks every byte against the pins
this repository does carry, and installs it where the application looks for it:

- ``config/reporting/tectonic_runtime.lock.json`` pins the binary (and the
  release archive it comes from) per platform, the bundle's tree hash, and the
  install location. The application reads the same file and refuses to compile
  with anything that does not match it.
- ``config/reporting/tectonic_bundle_manifest.json`` lists every bundle file by
  name, size and SHA-256, and says where each one is fetched from: the Tectonic
  default bundle v33 (through ``tectonic -X bundle cat``, run with the binary
  just verified), or the Fandol fonts package. Fandol is looked for in a fixed
  list of archives, tried in order: the frozen TeX Live 2025 archives first
  (immutable, each pinned by its own SHA-256), then CTAN. Any source that
  fails, for a network, certificate or digest reason, is skipped; every file is
  still checked against its own digest, so no source has to be trusted.

Only ``darwin-arm64`` is pinned today. On any other platform the lock marks the
target ``unprovisioned``; this script says so and exits 0, and PDF export fails
closed with ``tectonic_runtime_unavailable``. Everything else in the application
works without the runtime.

Usage::

    python3 tools/provision_tectonic.py            # download, verify, install
    python3 tools/provision_tectonic.py --check    # verify an existing install, no network
    python3 tools/provision_tectonic.py --from DIR # install from a local copy, no network

``--from DIR`` takes a directory laid out like the installed runtime (the
``managed_root`` in the lock), for machines without network access, or for when
a download host is down. Every file is verified exactly as a download would be.
The simplest way to make such a directory is to run this script on a second
machine with network access and copy its ``managed_root`` over.

Nothing is installed unless every file verifies. Files are assembled in a
staging directory next to the install location and moved into place with a
rename, so a failed or interrupted run leaves the previous state untouched; the
next run removes a staging directory that a killed run left behind.
Standard library only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import lzma
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
LOCK_RELATIVE = Path("config/reporting/tectonic_runtime.lock.json")
MANIFEST_RELATIVE = Path("config/reporting/tectonic_bundle_manifest.json")
LOCK_SCHEMA = "derivationlab-tectonic-runtime-lock-v1"
MANIFEST_SCHEMA = "derivationlab-tectonic-bundle-manifest-v1"
DOWNLOAD_TIMEOUT_SECONDS = 120


class ProvisionError(Exception):
    """A pin did not hold, or an input was unusable. Nothing was installed."""


# ---------------------------------------------------------------------------
# Hashing: the same algorithm as derivation_app.reporting._sha256_directory
# ---------------------------------------------------------------------------


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def tree_hash_of_entries(entries: list[tuple[str, int, str]]) -> str:
    """Tree hash over ``(relative path, size, sha256)`` triples.

    SHA-256 over, for every file in path order, ``path NUL size NUL sha256 LF``.
    This is the algorithm the application uses to check the installed bundle,
    so a manifest can be checked against the lock without any file present.
    """

    digest = hashlib.sha256()
    for relative, size, file_sha in sorted(entries):
        digest.update(relative.encode())
        digest.update(b"\0")
        digest.update(str(size).encode())
        digest.update(b"\0")
        digest.update(file_sha.encode())
        digest.update(b"\n")
    return digest.hexdigest()


def tree_hash(directory: Path) -> str:
    entries = []
    for item in directory.rglob("*"):
        if item.is_symlink():
            raise ProvisionError(f"{item}: the bundle must not contain symlinks")
        if item.is_file():
            entries.append((item.relative_to(directory).as_posix(), item.stat().st_size, sha256_file(item)))
    return tree_hash_of_entries(entries)


# ---------------------------------------------------------------------------
# Pins
# ---------------------------------------------------------------------------


def detect_target() -> str:
    """The lock's target key for this machine, resolved as the application does."""

    machine = platform.machine().casefold()
    machine = {"aarch64": "arm64", "amd64": "x86_64"}.get(machine, machine)
    system = {"Darwin": "darwin", "Linux": "linux", "Windows": "windows"}.get(
        platform.system(), platform.system().casefold()
    )
    return f"{system}-{machine}"


def _safe_relative(value: object, what: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ProvisionError(f"lock: {what} is not a path")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise ProvisionError(f"lock: {what} must be a safe repository-relative path, not {value!r}")
    return path


@dataclass(frozen=True)
class Plan:
    """Everything the install needs, resolved and checked, before any I/O."""

    target: str
    provisioned: bool
    evidence: str
    managed_root: Path
    binary: Path  # relative to managed_root
    binary_sha256: str | None
    archive_sha256: str | None
    bundle: Path  # relative to managed_root
    bundle_sha256: str
    manifest: dict

    @property
    def files(self) -> list[dict]:
        return self.manifest["files"]


def load_plan(root: Path, *, target: str | None = None, lock: Path | None = None, manifest: Path | None = None) -> Plan:
    lock_path = lock or root / LOCK_RELATIVE
    manifest_path = manifest or root / MANIFEST_RELATIVE
    try:
        lock_value = json.loads(lock_path.read_text(encoding="utf-8"))
        manifest_value = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ProvisionError(f"cannot read the pins: {error}") from error

    if lock_value.get("schema_version") != LOCK_SCHEMA:
        raise ProvisionError(f"{lock_path}: unsupported lock schema")
    if manifest_value.get("schema_version") != MANIFEST_SCHEMA:
        raise ProvisionError(f"{manifest_path}: unsupported manifest schema")
    if manifest_value.get("tectonic_version") != lock_value.get("tectonic_version"):
        raise ProvisionError("the manifest and the lock pin different Tectonic versions")

    target = target or detect_target()
    targets = lock_value.get("targets", {})
    if target not in targets:
        raise ProvisionError(f"the lock has no target {target!r}; known targets: {', '.join(sorted(targets))}")
    target_value = targets[target]

    managed_relative = _safe_relative(lock_value.get("managed_root"), "managed_root")
    bundle_relative = _safe_relative(lock_value.get("bundle", {}).get("path"), "bundle.path")
    binary_relative = _safe_relative(target_value.get("binary", {}).get("path"), f"targets.{target}.binary.path")
    for path, what in ((bundle_relative, "bundle.path"), (binary_relative, "binary.path")):
        if not path.is_relative_to(managed_relative):
            raise ProvisionError(f"lock: {what} must be inside managed_root")

    bundle_sha256 = lock_value["bundle"].get("sha256")
    listed = [(entry["name"], entry["size"], entry["sha256"]) for entry in manifest_value.get("files", [])]
    if any("/" in name or name in {"", ".", ".."} for name, _size, _sha in listed):
        raise ProvisionError("manifest: bundle file names must be plain file names")
    if manifest_value.get("bundle_sha256") != bundle_sha256 or tree_hash_of_entries(listed) != bundle_sha256:
        raise ProvisionError("the manifest does not describe the bundle the lock pins (tree hash differs)")

    binary = target_value.get("binary", {})
    return Plan(
        target=target,
        provisioned=target_value.get("status") == "provisioned",
        evidence=str(target_value.get("evidence", "")),
        managed_root=(root / managed_relative),
        binary=binary_relative.relative_to(managed_relative),
        binary_sha256=binary.get("sha256"),
        archive_sha256=binary.get("release_archive_sha256"),
        bundle=bundle_relative.relative_to(managed_relative),
        bundle_sha256=bundle_sha256,
        manifest=manifest_value,
    )


# ---------------------------------------------------------------------------
# Verification of an installed (or staged) runtime
# ---------------------------------------------------------------------------


def problems(plan: Plan, runtime_root: Path) -> list[str]:
    """Every way the runtime under ``runtime_root`` differs from the pins."""

    found: list[str] = []
    binary = runtime_root / plan.binary
    if not binary.is_file() or binary.is_symlink():
        found.append(f"binary missing: {plan.binary}")
    elif sha256_file(binary) != plan.binary_sha256:
        found.append(f"binary sha256 differs from the lock: {plan.binary}")
    elif os.name != "nt" and not os.access(binary, os.X_OK):
        found.append(f"binary is not executable: {plan.binary}")

    bundle = runtime_root / plan.bundle
    if not bundle.is_dir():
        return [*found, f"bundle missing: {plan.bundle}"]
    expected = {entry["name"]: entry for entry in plan.files}
    present = {item.name for item in bundle.iterdir()}
    for name in sorted(present - set(expected)):
        found.append(f"unexpected file in the bundle: {name}")
    for name, entry in sorted(expected.items()):
        path = bundle / name
        if not path.is_file() or path.is_symlink():
            found.append(f"bundle file missing: {name}")
        elif path.stat().st_size != entry["size"] or sha256_file(path) != entry["sha256"]:
            found.append(f"bundle file differs from the manifest: {name}")
    if not found and tree_hash(bundle) != plan.bundle_sha256:
        found.append("bundle tree hash differs from the lock")
    return found


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------


def _download(url: str, destination: Path) -> Path:
    print(f"  fetching {url}", file=sys.stderr)
    request = urllib.request.Request(url, headers={"User-Agent": "derivationlab-provision-tectonic"})
    with urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response, destination.open("wb") as out:
        shutil.copyfileobj(response, out)
    return destination


def _expect(data: bytes, *, sha256: str, what: str, size: int | None = None) -> bytes:
    if size is not None and len(data) != size:
        raise ProvisionError(f"{what}: {len(data)} bytes, the pin says {size}")
    actual = sha256_bytes(data)
    if actual != sha256:
        raise ProvisionError(f"{what}: sha256 {actual} does not match the pinned {sha256}")
    return data


class Source:
    """Where file contents come from: the network, or a local copy."""

    def __init__(self, plan: Plan, scratch: Path, local: Path | None) -> None:
        self.plan = plan
        self.scratch = scratch
        self.local = local
        # url -> {member file name: bytes}, or None when that source failed.
        self._fandol: dict[str, dict[str, bytes] | None] = {}
        self._fandol_errors: list[str] = []

    def close(self) -> None:
        self._fandol.clear()

    def binary(self) -> bytes:
        what = f"Tectonic binary {self.plan.binary}"
        if self.local is not None:
            return _expect((self.local / self.plan.binary).read_bytes(), sha256=self.plan.binary_sha256, what=what)
        release = self.plan.manifest.get("release", {}).get(self.plan.target)
        if not release or not self.plan.archive_sha256:
            raise ProvisionError(f"no pinned release archive for {self.plan.target}")
        archive = _download(release["url"], self.scratch / "release.tar.gz")
        _expect(archive.read_bytes(), sha256=self.plan.archive_sha256, what="release archive")
        with tarfile.open(archive, "r:gz") as tar:
            member = tar.getmember(release["member"])
            if not member.isfile():
                raise ProvisionError(f"release archive member {release['member']} is not a regular file")
            extracted = tar.extractfile(member)
            assert extracted is not None
            return _expect(extracted.read(), sha256=self.plan.binary_sha256, what=what)

    def license(self) -> bytes:
        entry = self.plan.manifest["license"]
        what = f"Tectonic licence {entry['path']}"
        if self.local is not None:
            data = (self.local / entry["path"]).read_bytes()
        else:
            data = _download(entry["url"], self.scratch / "LICENSE").read_bytes()
        return _expect(data, sha256=entry["sha256"], size=entry["size"], what=what)

    def bundle_file(self, entry: dict, tectonic: Path) -> bytes:
        name = entry["name"]
        if self.local is not None:
            data = (self.local / self.plan.bundle / name).read_bytes()
        elif entry["source"] == "fandol":
            data = self._fandol_member(entry)
        elif entry["source"] == "tectonic-bundle":
            data = self._bundle_cat(tectonic, name)
        elif entry["source"] == "bundle-digest":
            data = self._bundle_cat(tectonic, name).strip() + b"\n"
        else:
            raise ProvisionError(f"manifest: unknown source {entry['source']!r} for {name}")
        return _expect(data, sha256=entry["sha256"], size=entry["size"], what=f"bundle file {name}")

    def _fandol_member(self, entry: dict) -> bytes:
        """The first archive that holds this file with the pinned digest wins."""

        wanted = entry["member"].rsplit("/", 1)[-1]
        for archive in self.plan.manifest["sources"]["fandol"]["archives"]:
            members = self._fandol_archive(archive)
            data = members.get(wanted) if members is not None else None
            if data is not None and len(data) == entry["size"] and sha256_bytes(data) == entry["sha256"]:
                return data
        failures = f" ({'; '.join(self._fandol_errors)})" if self._fandol_errors else ""
        raise ProvisionError(
            f"bundle file {entry['name']}: no Fandol source provided it{failures}. Install from a local copy "
            "with --from DIR, or, if the downloads failed on a certificate, set SSL_CERT_FILE to a CA bundle "
            "and retry."
        )

    def _fandol_archive(self, archive: dict) -> dict[str, bytes] | None:
        url = archive["url"]
        if url in self._fandol:
            return self._fandol[url]
        members = None
        destination = self.scratch / f"fandol-{len(self._fandol)}-{url.rsplit('/', 1)[-1]}"
        try:
            data = _download(url, destination).read_bytes()
            if archive.get("sha256"):
                _expect(data, sha256=archive["sha256"], what=f"Fandol archive {url}")
            members = _archive_files(destination)
        except (OSError, EOFError, ProvisionError, tarfile.TarError, zipfile.BadZipFile, lzma.LZMAError) as error:
            # URLError and ssl errors are OSErrors: a mirror with a broken
            # certificate chain is skipped like one that is down.
            self._fandol_errors.append(f"{url}: {error}")
            print(f"  skipped {url}: {error}", file=sys.stderr)
        finally:
            destination.unlink(missing_ok=True)
        self._fandol[url] = members
        return members

    def _bundle_cat(self, tectonic: Path, name: str) -> bytes:
        workspace = self.scratch / "workspace"
        workspace.mkdir(exist_ok=True)
        completed = subprocess.run(
            [str(tectonic), "-X", "bundle", "cat", name],
            cwd=workspace,
            env={**os.environ, "TECTONIC_CACHE_DIR": str(self.scratch / "tectonic-cache")},
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            detail = completed.stderr.decode("utf-8", "replace").strip().splitlines()[-1:]
            raise ProvisionError(f"tectonic -X bundle cat {name} failed: {' '.join(detail)}")
        return completed.stdout


def _archive_files(path: Path) -> dict[str, bytes]:
    """Regular files of a .zip or .tar.xz archive, keyed by file name."""

    if path.name.endswith(".zip"):
        with zipfile.ZipFile(path) as archive:
            return {
                info.filename.rsplit("/", 1)[-1]: archive.read(info) for info in archive.infolist() if not info.is_dir()
            }
    if path.name.endswith(".tar.xz"):
        with tarfile.open(path, "r:xz") as archive:
            files = {}
            for member in archive.getmembers():
                extracted = archive.extractfile(member) if member.isfile() else None
                if extracted is not None:
                    files[member.name.rsplit("/", 1)[-1]] = extracted.read()
            return files
    raise ProvisionError(f"{path.name}: unsupported archive format")


# ---------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------


def _clean_stale(root: Path) -> None:
    """Undo what a run killed before its cleanup (SIGKILL, power loss) left."""

    for stale in root.parent.glob(f".{root.name}.staging-*"):
        shutil.rmtree(stale, ignore_errors=True)
    for previous in root.parent.glob(f".{root.name}.previous-*"):
        if not root.exists():
            previous.rename(root)  # killed between the two renames: restore
        else:
            shutil.rmtree(previous, ignore_errors=True)


def install(plan: Plan, *, local: Path | None = None) -> None:
    root = plan.managed_root
    root.parent.mkdir(parents=True, exist_ok=True)
    _clean_stale(root)
    staging = Path(tempfile.mkdtemp(prefix=f".{root.name}.staging-", dir=root.parent))
    staging.chmod(0o755)
    scratch = Path(tempfile.mkdtemp(prefix="provision-tectonic-"))
    source = Source(plan, scratch, local)
    try:
        binary = staging / plan.binary
        binary.parent.mkdir(parents=True)
        binary.write_bytes(source.binary())
        binary.chmod(0o755)
        license_path = staging / plan.manifest["license"]["path"]
        license_path.parent.mkdir(parents=True, exist_ok=True)
        license_path.write_bytes(source.license())

        bundle = staging / plan.bundle
        bundle.mkdir(parents=True)
        total = len(plan.files)
        for index, entry in enumerate(plan.files, start=1):
            (bundle / entry["name"]).write_bytes(source.bundle_file(entry, binary))
            if index % 25 == 0 or index == total:
                print(f"  bundle: {index}/{total} files verified", file=sys.stderr)

        remaining = problems(plan, staging)
        if remaining:
            raise ProvisionError("; ".join(remaining))

        previous = None
        if root.exists():
            previous = root.with_name(f".{root.name}.previous-{os.getpid()}")
            root.rename(previous)
        try:
            staging.rename(root)
        except OSError:
            if previous is not None:
                previous.rename(root)
            raise
        if previous is not None:
            shutil.rmtree(previous)
    finally:
        source.close()
        shutil.rmtree(scratch, ignore_errors=True)
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Provision the pinned Tectonic runtime for PDF reporting.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="verify an existing install; no network")
    mode.add_argument("--from", dest="local", type=Path, help="install from a local copy of the runtime directory")
    parser.add_argument("--root", type=Path, default=REPO_ROOT, help="repository root (default: this checkout)")
    parser.add_argument("--target", help="lock target to use instead of this machine's (for example darwin-arm64)")
    parser.add_argument("--force", action="store_true", help="reinstall even if the existing install verifies")
    args = parser.parse_args(argv)

    try:
        plan = load_plan(args.root.resolve(), target=args.target)
    except ProvisionError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    if not plan.provisioned:
        print(f"PDF reporting is not available on {plan.target}: the lock pins no runtime for it.")
        print(f"  {plan.evidence}")
        print("  Everything else works; PDF export reports tectonic_runtime_unavailable.")
        return 0

    installed = problems(plan, plan.managed_root) if plan.managed_root.exists() else ["not installed"]
    if args.check:
        if installed:
            for problem in installed:
                print(f"not ok: {problem}")
            return 1
        print(f"ok: Tectonic runtime for {plan.target} matches the lock ({plan.managed_root})")
        return 0
    if not installed and not args.force:
        print(f"ok: already provisioned for {plan.target}; nothing to do (--force reinstalls)")
        return 0

    try:
        install(plan, local=args.local)
    except (ProvisionError, OSError, KeyError, tarfile.TarError, zipfile.BadZipFile) as error:
        print(f"error: {error}", file=sys.stderr)
        print("nothing was installed; the previous state is unchanged", file=sys.stderr)
        return 1
    print(f"ok: installed and verified the Tectonic runtime for {plan.target} at {plan.managed_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
