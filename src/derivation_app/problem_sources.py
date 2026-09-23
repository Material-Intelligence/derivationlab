"""Problem sources for runs: problems carry no literature pack in this build.

A run either has no sources (the empty pack) or a verified method-source pack.
Method-source packs bundle third-party literature that is not redistributed, so
this build offers no presets and refuses any request that names a pack.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from tempfile import TemporaryDirectory

from derivation_api.models import CreateRunRequest, ProblemPresetsView, SourcePackRef

from derivation_agent_record import sha256_text
from derivation_runtime.source_material import SourceLibrary
from derivation_runtime.types import ContentRef, RunConfig

EMPTY_PACK_ID = "pack_empty"
PACK_VERSION = "1"
SOURCE_PACKS_UNAVAILABLE = "source packs are not available in this build"


def _empty_manifest_sha256() -> str:
    with TemporaryDirectory(prefix="derivation-empty-catalog-") as temporary:
        return SourceLibrary.empty(
            Path(temporary).resolve() / "sources"
        ).manifest_sha256


def build_problem_presets(repository_root: Path) -> ProblemPresetsView:
    """Return the preset catalogue: no presets and no method references."""
    del repository_root
    return ProblemPresetsView(
        presets=[],
        method_source_pack=SourcePackRef(
            pack_id=EMPTY_PACK_ID,
            version=PACK_VERSION,
            sha256=_empty_manifest_sha256(),
        ),
        method_references=[],
    )


def validate_problem_sources(
    command: CreateRunRequest, repository_root: Path
) -> ContentRef:
    """No user-supplied path ever grants file access to the model."""
    del repository_root
    problem, config = command.problem, command.config
    if config.allowed_paths:
        raise ValueError(
            "source access uses a verified pack, not caller-provided paths"
        )
    if problem.source_pack is not None:
        raise ValueError(SOURCE_PACKS_UNAVAILABLE)
    if config.reference_allowed or problem.allowed_references:
        raise ValueError("reference declarations require a verified source pack")
    if command.runtime.reading_mode != "on_demand":
        raise ValueError(
            "reading modes other than on_demand need a verified source pack"
        )
    if config.record_version == "1.0":
        return ContentRef(EMPTY_PACK_ID, sha256_text(""))
    return ContentRef(EMPTY_PACK_ID, _empty_manifest_sha256())


def prepare_run_sources(
    config: RunConfig,
    repository_root: Path,
    evidence_directory: Path,
    workspace: Path,
) -> SourceLibrary:
    """Archive exact inputs and create an independently verified provider copy.

    Resume uses archived inputs; source edits cannot silently change a run.
    """
    del repository_root
    archive = evidence_directory / "sources"
    if archive.exists():
        library = SourceLibrary.load(archive, config.pack.sha256)
    elif config.pack.id == EMPTY_PACK_ID and not config.input_policy.reference_allowed:
        library = SourceLibrary.empty(archive)
    elif config.input_policy.reference_allowed:
        raise ValueError(SOURCE_PACKS_UNAVAILABLE)
    else:
        raise ValueError("run source identity conflicts with its reference policy")
    if library.manifest_sha256 != config.pack.sha256:
        raise ValueError("run source snapshot differs from its frozen configuration")
    if (library.manifest["mode"] == "methods") != config.input_policy.reference_allowed:
        raise ValueError("run source mode conflicts with its reference policy")
    destination = workspace / "sources"
    if not destination.exists():
        shutil.copytree(archive, destination)
    return SourceLibrary.load(destination, config.pack.sha256)
