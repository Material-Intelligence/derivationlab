"""The Tectonic provisioning script, offline.

The real install downloads about 70 MB and runs the Tectonic binary, so it is
not a unit test. What can be checked without a network is everything the
install relies on: that the shipped manifest describes exactly the bundle the
lock pins, that the tree hash is the application's, and that the install
refuses anything that does not verify and leaves the previous state alone when
it does.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest
from conftest import REPO_ROOT

from tools.provision_tectonic import (
    LOCK_RELATIVE,
    MANIFEST_RELATIVE,
    ProvisionError,
    load_plan,
    main,
    tree_hash,
    tree_hash_of_entries,
)

TARGET = "darwin-arm64"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# The shipped pins
# ---------------------------------------------------------------------------


def test_the_shipped_manifest_describes_the_bundle_the_lock_pins() -> None:
    plan = load_plan(REPO_ROOT, target=TARGET)

    assert plan.provisioned
    assert len(plan.files) == 332
    assert len({entry["name"] for entry in plan.files}) == 332
    listed = [(entry["name"], entry["size"], entry["sha256"]) for entry in plan.files]
    assert tree_hash_of_entries(listed) == plan.bundle_sha256


def test_every_bundle_file_names_a_known_source() -> None:
    manifest = json.loads((REPO_ROOT / MANIFEST_RELATIVE).read_text(encoding="utf-8"))
    sources = {entry["source"] for entry in manifest["files"]}
    assert sources <= set(manifest["sources"])
    assert all(entry["member"] for entry in manifest["files"] if entry["source"] == "fandol")


@pytest.mark.parametrize("target", ["darwin-x86_64", "linux-x86_64", "windows-x86_64"])
def test_an_unpinned_platform_is_reported_not_attempted(
    tmp_path: Path, target: str, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _copy_pins(tmp_path)

    assert main(["--root", str(root), "--target", target]) == 0
    assert "not available" in capsys.readouterr().out
    assert not (root / "src").exists(), "nothing may be written for an unpinned platform"


def test_the_tree_hash_is_the_application_s() -> None:
    """The application checks the installed bundle with its own function; the
    script must compute the same digest, or it would install bundles the
    application then refuses. Runs where the application imports (the app CI
    job); skipped in the standard-library-only record job."""

    try:
        from derivation_app.reporting import _sha256_directory
    except ImportError as error:  # the app's dependencies are not installed here
        pytest.skip(f"derivation_app is not importable here: {error}")

    bundle = Path(__file__).parent / "fixtures"
    assert tree_hash(bundle) == _sha256_directory(bundle)


# ---------------------------------------------------------------------------
# The install, on a synthetic runtime
# ---------------------------------------------------------------------------

BINARY = b"#!/bin/sh\necho synthetic tectonic\n"
LICENSE = b"synthetic licence text\n"
BUNDLE = {"article.cls": b"% synthetic class\n", "SHA256SUM": b"0" * 64 + b"\n"}


def _copy_pins(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    for relative in (LOCK_RELATIVE, MANIFEST_RELATIVE):
        (root / relative).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO_ROOT / relative, root / relative)
    return root


def _synthetic_repo(tmp_path: Path) -> tuple[Path, Path]:
    """A repository whose pins describe a tiny runtime, plus a copy of it."""

    root = tmp_path / "repo"
    managed = "runtime"
    entries = [
        {"name": name, "size": len(data), "sha256": _sha(data), "source": "tectonic-bundle"}
        for name, data in sorted(BUNDLE.items())
    ]
    bundle_sha = tree_hash_of_entries([(e["name"], e["size"], e["sha256"]) for e in entries])
    lock = {
        "schema_version": "derivationlab-tectonic-runtime-lock-v1",
        "tectonic_version": "0.17.0",
        "managed_root": managed,
        "bundle": {"path": f"{managed}/bundle", "sha256": bundle_sha},
        "targets": {
            TARGET: {
                "status": "provisioned",
                "binary": {"path": f"{managed}/bin/tectonic", "sha256": _sha(BINARY)},
                "evidence": "synthetic",
            }
        },
    }
    manifest = {
        "schema_version": "derivationlab-tectonic-bundle-manifest-v1",
        "tectonic_version": "0.17.0",
        "bundle_sha256": bundle_sha,
        "sources": {"tectonic-bundle": {"how": "synthetic"}},
        "release": {},
        "license": {
            "path": "LICENSE",
            "url": "https://invalid.invalid/",
            "size": len(LICENSE),
            "sha256": _sha(LICENSE),
        },
        "files": entries,
    }
    for relative, value in ((LOCK_RELATIVE, lock), (MANIFEST_RELATIVE, manifest)):
        (root / relative).parent.mkdir(parents=True, exist_ok=True)
        (root / relative).write_text(json.dumps(value), encoding="utf-8")

    copy = tmp_path / "copy"
    (copy / "bin").mkdir(parents=True)
    (copy / "bin" / "tectonic").write_bytes(BINARY)
    (copy / "LICENSE").write_bytes(LICENSE)
    (copy / "bundle").mkdir()
    for name, data in BUNDLE.items():
        (copy / "bundle" / name).write_bytes(data)
    return root, copy


def test_install_from_a_local_copy_then_check(tmp_path: Path) -> None:
    root, copy = _synthetic_repo(tmp_path)

    assert main(["--root", str(root), "--target", TARGET, "--check"]) == 1
    assert main(["--root", str(root), "--target", TARGET, "--from", str(copy)]) == 0
    assert main(["--root", str(root), "--target", TARGET, "--check"]) == 0
    assert (root / "runtime" / "bin" / "tectonic").read_bytes() == BINARY
    # Idempotent: a verified install is left alone.
    assert main(["--root", str(root), "--target", TARGET, "--from", str(copy)]) == 0


def test_check_names_the_file_that_changed(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root, copy = _synthetic_repo(tmp_path)
    assert main(["--root", str(root), "--target", TARGET, "--from", str(copy)]) == 0
    capsys.readouterr()

    (root / "runtime" / "bundle" / "article.cls").write_bytes(b"% edited\n")

    assert main(["--root", str(root), "--target", TARGET, "--check"]) == 1
    assert "article.cls" in capsys.readouterr().out


def test_a_copy_that_does_not_verify_installs_nothing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root, copy = _synthetic_repo(tmp_path)
    assert main(["--root", str(root), "--target", TARGET, "--from", str(copy)]) == 0
    capsys.readouterr()

    (copy / "bundle" / "article.cls").write_bytes(b"% tampered\n")

    assert main(["--root", str(root), "--target", TARGET, "--from", str(copy), "--force"]) == 1
    assert "article.cls" in capsys.readouterr().err
    # The previous, verified install is untouched, and no staging is left behind.
    assert main(["--root", str(root), "--target", TARGET, "--check"]) == 0
    assert sorted(path.name for path in root.iterdir()) == ["config", "runtime"]


def test_a_manifest_that_does_not_match_the_lock_is_refused(tmp_path: Path) -> None:
    root, _copy = _synthetic_repo(tmp_path)
    manifest_path = root / MANIFEST_RELATIVE
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"][0]["size"] += 1
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ProvisionError, match="tree hash"):
        load_plan(root, target=TARGET)


@pytest.mark.parametrize("bad", ["../outside", "/absolute/path"])
def test_lock_paths_must_stay_inside_the_repository(tmp_path: Path, bad: str) -> None:
    root, _copy = _synthetic_repo(tmp_path)
    lock_path = root / LOCK_RELATIVE
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    lock["managed_root"] = bad
    lock_path.write_text(json.dumps(lock), encoding="utf-8")

    with pytest.raises(ProvisionError, match="managed_root"):
        load_plan(root, target=TARGET)


# ---------------------------------------------------------------------------
# Fandol sources: a failing mirror is skipped, not fatal
# ---------------------------------------------------------------------------

FONT = b"synthetic font bytes\n"


def _repo_with_a_fandol_file(tmp_path: Path, archives: list[dict]) -> Path:
    """The synthetic runtime plus one bundle file that comes from Fandol."""

    root, _copy = _synthetic_repo(tmp_path)
    manifest_path = root / MANIFEST_RELATIVE
    lock_path = root / LOCK_RELATIVE
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    manifest["files"].append(
        {"name": "Font.otf", "size": len(FONT), "sha256": _sha(FONT), "source": "fandol", "member": "fandol/Font.otf"}
    )
    manifest["sources"]["fandol"] = {"how": "synthetic", "archives": archives}
    bundle_sha = tree_hash_of_entries([(e["name"], e["size"], e["sha256"]) for e in manifest["files"]])
    manifest["bundle_sha256"] = bundle_sha
    lock["bundle"]["sha256"] = bundle_sha
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    lock_path.write_text(json.dumps(lock), encoding="utf-8")
    return root


def _fake_network(monkeypatch: pytest.MonkeyPatch, broken: set[str]) -> list[str]:
    """Serve BINARY/LICENSE/BUNDLE offline, and a zip holding FONT for any
    Fandol URL except those in ``broken``, which fail like a mirror with an
    incomplete certificate chain."""

    import io
    import ssl
    import urllib.error
    import zipfile

    import tools.provision_tectonic as provision

    fetched: list[str] = []

    def download(url: str, destination: Path) -> Path:
        fetched.append(url)
        if url in broken:
            raise urllib.error.URLError(ssl.SSLCertVerificationError("unable to get local issuer certificate"))
        if url.endswith(".zip"):
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w") as archive:
                archive.writestr("fandol/Font.otf", FONT)
            destination.write_bytes(buffer.getvalue())
        else:
            destination.write_bytes(LICENSE)
        return destination

    monkeypatch.setattr(provision, "_download", download)
    monkeypatch.setattr(provision.Source, "binary", lambda self: BINARY)
    monkeypatch.setattr(provision.Source, "_bundle_cat", lambda self, tectonic, name: BUNDLE[name])
    return fetched


def test_a_mirror_that_fails_is_skipped_and_the_next_one_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    first, second = "https://broken.invalid/fandol.zip", "https://working.invalid/fandol.zip"
    root = _repo_with_a_fandol_file(tmp_path, [{"url": first, "sha256": None}, {"url": second, "sha256": None}])
    fetched = _fake_network(monkeypatch, broken={first})

    assert main(["--root", str(root), "--target", TARGET]) == 0
    assert (root / "runtime" / "bundle" / "Font.otf").read_bytes() == FONT
    assert fetched.index(first) < fetched.index(second)
    assert "skipped https://broken.invalid/fandol.zip" in capsys.readouterr().err
    assert main(["--root", str(root), "--target", TARGET, "--check"]) == 0


def test_an_archive_with_the_wrong_digest_is_skipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pinned, loose = "https://pinned.invalid/fandol.zip", "https://loose.invalid/fandol.zip"
    root = _repo_with_a_fandol_file(tmp_path, [{"url": pinned, "sha256": "0" * 64}, {"url": loose, "sha256": None}])
    _fake_network(monkeypatch, broken=set())

    assert main(["--root", str(root), "--target", TARGET]) == 0


def test_when_every_source_fails_the_error_names_the_remedies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    only = "https://broken.invalid/fandol.zip"
    root = _repo_with_a_fandol_file(tmp_path, [{"url": only, "sha256": None}])
    _fake_network(monkeypatch, broken={only})

    assert main(["--root", str(root), "--target", TARGET]) == 1
    err = capsys.readouterr().err
    assert "--from DIR" in err and "SSL_CERT_FILE" in err
    assert not (root / "runtime").exists()


def test_a_staging_directory_left_by_a_killed_run_is_removed(tmp_path: Path) -> None:
    root, copy = _synthetic_repo(tmp_path)
    leftover = root / ".runtime.staging-killed"
    (leftover / "bin").mkdir(parents=True)
    (leftover / "bin" / "tectonic").write_bytes(BINARY)

    assert main(["--root", str(root), "--target", TARGET, "--from", str(copy)]) == 0
    assert sorted(path.name for path in root.iterdir()) == ["config", "runtime"]


def test_the_shipped_fandol_sources_try_pinned_frozen_archives_first() -> None:
    manifest = json.loads((REPO_ROOT / MANIFEST_RELATIVE).read_text(encoding="utf-8"))
    archives = manifest["sources"]["fandol"]["archives"]
    assert archives[0]["sha256"] and "tlnet-final" in archives[0]["url"]
    assert all(archive["url"].startswith("https://") for archive in archives)
    assert len({archive["url"] for archive in archives}) == len(archives)
