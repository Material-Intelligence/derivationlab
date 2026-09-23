"""Provider adapter from ModelRuntime to the pinned Codex App Server client."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import math
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from .app_server_client import (
    AppServerClient,
    ClientTimeouts,
    SpawnedProcessIdentity,
    same_terminal_turn,
)
from .app_server_protocol import (
    AppServerError,
    AppServerForkError,
    AppServerProtocolError,
    AppServerRemoteError,
    JsonObject,
    ProtocolPin,
    ProviderEvent,
    ProviderEventKind,
    TurnNotCompletedError,
    extract_single_final_agent_message,
)
from .capabilities import BENCHMARK_SYMBOLIC_V1, CapabilityProfile
from .launch_gate import (
    AppServerCommand,
    GateResult,
    LaunchBlocked,
)
from .platform_policy import PINNED_CODEX_VERSION
from .prompts import (
    FORMULA_REPAIR_DEVELOPER_INSTRUCTIONS,
    FORMULA_REVIEW_DEVELOPER_INSTRUCTIONS,
    JUDGE_DEVELOPER_INSTRUCTIONS,
    JUDGE_OUTPUT_SCHEMA,
    checker_developer_instructions,
    checker_output_schema,
    checker_user_prompt,
    formula_repair_output_schema,
    formula_repair_user_prompt,
    formula_review_output_schema,
    formula_review_user_prompt,
    judge_user_prompt,
    rehydrated_writer_instructions,
    writer_developer_instructions,
    writer_output_schema,
    writer_user_prompt,
)
from .scientific_runtime import (
    SCIENTIFIC_TOOL_NAME,
    SCIENTIFIC_TOOL_SPEC,
    ScientificRuntimeError,
    scientific_calculator_from_environment,
)
from .shared_app_server import SharedAppServerSession
from .source_material import (
    SOURCE_TOOL_NAMES,
    SOURCE_TOOL_SPECS,
    FeedbackReadTool,
    SourceLibrary,
    TranscriptCatalogTool,
    TranscriptReadTool,
)
from .types import (
    BranchAlternative,
    CheckEvidence,
    CheckOutput,
    CheckRequest,
    EvidenceSource,
    FormulaEquivalenceOutput,
    FormulaEquivalenceRequest,
    FormulaRepairOutput,
    FormulaRepairRequest,
    JudgeOutput,
    JudgeRequest,
    ModelRole,
    ModelSpec,
    ProviderForkError,
    ProviderLineage,
    ReconcileResult,
    ReconcileStatus,
    RunConfig,
    RuntimeInterruption,
    RuntimeInvariantError,
    RuntimeInvocation,
    RuntimeInvocationError,
    RuntimeOutput,
    RuntimeSession,
    StepContent,
    StepSnapshot,
    Usage,
    WriterControl,
    WriterDecision,
    WriterOutput,
    WriterRequest,
)

_Request = (
    WriterRequest
    | CheckRequest
    | JudgeRequest
    | FormulaRepairRequest
    | FormulaEquivalenceRequest
)

# Client failures a fork attempt can raise that say nothing about the
# connection.  ``AppServerForkError`` is a fork response this client refused to
# adopt, after rolling back everything it had recorded for it;
# ``TurnNotCompletedError`` is refused before any request leaves, from the
# client's own turn bookkeeping.  Both leave the parent thread, every other
# thread and every in-flight request of a shared child untouched, so they are
# handed to the orchestrator as one failed fork.  Anything that puts the client
# into its fatal state is deliberately absent: that is a statement about the
# process and must keep ending the run.
_FORK_CONFINED_CLIENT_ERRORS = (AppServerForkError, TurnNotCompletedError)

_PRODUCTION_CONSTRUCTION_AUTHORITY = object()
_TEST_CONSTRUCTION_AUTHORITY = object()

_RuntimeClient = AppServerClient | SharedAppServerSession


def _restore_json_escaped_tex_controls(value: str) -> str:
    """Restore TeX commands that strict JSON decoded as C0 controls.

    A provider can emit ``\bf`` or ``\frac`` with only one JSON backslash.
    Those sequences are valid JSON escapes, but decoding turns them into a
    backspace or form-feed followed by text.  Keeping that hidden control byte
    in a scientific step makes an otherwise literal Checker quote impossible.
    The two replacements below are lossless for ordinary prose because these
    control bytes are otherwise forbidden from persisted readable fields.
    """

    return value.replace("\b", r"\b").replace("\f", r"\f")


def _preparation_documents(preparation: Mapping[str, Any]) -> list[Any]:
    """Return full documents, treating the contract's null as no redelivery."""

    documents = preparation.get("documents")
    if documents is None:
        return []
    if not isinstance(documents, list):
        raise RuntimeInvariantError("Writer preparation documents are invalid")
    return documents


def _app_server_service_tier(service_tier: str) -> str | None:
    if service_tier == "standard":
        return None
    if service_tier == "fast":
        return "priority"
    raise RuntimeInvariantError("unsupported product service tier")


@dataclass(frozen=True)
class LaunchSettings:
    """Model-turn settings produced only after the authoritative launch gate."""

    workspace: Path
    gate_result: GateResult
    authorized_command: AppServerCommand
    authorized_client: _RuntimeClient | None = field(
        default=None, repr=False, compare=False
    )
    # Set only when this run shares one App Server child with other runs.  The
    # child then runs in its own holder directory and every thread declares the
    # per-run ``workspace`` instead, so the two paths must stay disjoint.
    process_workspace: Path | None = None
    capability_profile: CapabilityProfile = BENCHMARK_SYMBOLIC_V1
    turn_timeout: float = 300.0
    max_unscoped_events: int = 2048
    # Every routed event of one turn is retained, and the App Server streams a
    # structured answer roughly one delta per token besides reasoning,
    # tool-call and lifecycle events.  A limit of 8192 is too small for a
    # Writer turn of about 30,000 answer characters (roughly 3.5 characters
    # per routed event once reasoning and tool calls are counted).  A
    # 100,000-character turn needs about 29,000 events at that density and
    # about 50,000 at a pessimistic two characters per event; 131072 covers
    # either with room for reasoning deltas and stays a finite guard (about
    # 1.35 KB retained per delta event, so under 180 MB for one turn at the
    # cap).
    max_events_per_operation: int = 131072
    # Not scaled with turn size: the early buffers hold only the events that
    # reach the router before their turn/start response registers the turn,
    # and the unscoped buffer holds thread notices, which grow with the
    # number of calls rather than with the length of one answer.
    max_early_turn_ids: int = 128
    max_early_events: int = 1024
    _authorized_command_sha256: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if not self.gate_result.allowed:
            raise LaunchBlocked(self.gate_result)
        if self.gate_result.stage not in {"model_turn", "intake_turn"}:
            raise RuntimeInvariantError(
                "LaunchSettings requires a successful model or intake turn gate"
            )
        if not self.workspace.is_absolute():
            raise RuntimeInvariantError("runtime workspace must be absolute")
        if self.authorized_command.shell:
            raise RuntimeInvariantError("authorized App Server command uses a shell")
        if self.authorized_command.inherit_parent_environment:
            raise RuntimeInvariantError(
                "authorized App Server command inherits the parent environment"
            )
        if self.process_workspace is None:
            if Path(self.authorized_command.cwd) != self.workspace:
                raise RuntimeInvariantError(
                    "authorized App Server command cwd differs from workspace"
                )
        else:
            if not self.process_workspace.is_absolute():
                raise RuntimeInvariantError(
                    "shared App Server process workspace must be absolute"
                )
            if Path(self.authorized_command.cwd) != self.process_workspace:
                raise RuntimeInvariantError(
                    "authorized App Server command cwd differs from the shared "
                    "process workspace"
                )
            if self.process_workspace == self.workspace:
                raise RuntimeInvariantError(
                    "shared App Server process workspace must not be a run workspace"
                )
            if self.workspace.is_relative_to(
                self.process_workspace
            ) or self.process_workspace.is_relative_to(self.workspace):
                raise RuntimeInvariantError(
                    "shared App Server process workspace must not contain a run "
                    "workspace"
                )
        if not isinstance(self.capability_profile, CapabilityProfile):
            raise RuntimeInvariantError("runtime capability profile is invalid")
        if self.turn_timeout <= 0:
            raise RuntimeInvariantError("turn timeout must be positive")
        if (
            self.max_unscoped_events <= 0
            or self.max_events_per_operation <= 0
            or self.max_early_turn_ids <= 0
            or self.max_early_events <= 0
        ):
            raise RuntimeInvariantError("event limits must be positive")
        object.__setattr__(
            self,
            "_authorized_command_sha256",
            self._command_sha256(self.authorized_command),
        )

    @property
    def thread_config(self) -> Mapping[str, Any]:
        """Translate the resolved capability profile for App Server."""

        return copy.deepcopy(self.capability_profile.app_server_config())

    @property
    def permission_profile(self) -> str:
        return self.capability_profile.permission_profile

    def assert_authorized_command(self, command: AppServerCommand) -> None:
        if self._command_sha256(command) != self._authorized_command_sha256:
            raise RuntimeInvariantError(
                "App Server command differs from the launch-gate authorization"
            )

    @staticmethod
    def _command_sha256(command: AppServerCommand) -> str:
        if not command.argv or not all(
            isinstance(part, str) and part for part in command.argv
        ):
            raise RuntimeInvariantError("App Server argv is invalid")
        if not isinstance(command.cwd, str) or not command.cwd:
            raise RuntimeInvariantError("App Server cwd is invalid")
        environment = dict(command.environment)
        if not all(
            isinstance(key, str) and key and isinstance(value, str)
            for key, value in environment.items()
        ):
            raise RuntimeInvariantError("App Server environment is invalid")
        payload = json.dumps(
            {
                "argv": list(command.argv),
                "cwd": command.cwd,
                "environment": environment,
                "inherit_parent_environment": command.inherit_parent_environment,
                "shell": command.shell,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


@dataclass
class _OperationState:
    invocation: RuntimeInvocation
    request: _Request | None
    started_after_sequence: int = 0
    completion: asyncio.Event = field(default_factory=asyncio.Event)
    events: list[ProviderEvent] = field(default_factory=list)
    partial_chunks: list[str] = field(default_factory=list)
    terminal_turn: JsonObject | None = None
    usage: JsonObject = field(default_factory=dict)
    output: RuntimeOutput | None = None
    failure: RuntimeInvocationError | None = None
    interrupted: bool = False

    @property
    def partial_output(self) -> str:
        return "".join(self.partial_chunks)


class CodexAppServerRuntime:
    """A strict ModelRuntime implementation over one App Server connection."""

    def __init__(
        self,
        *,
        config: RunConfig,
        client: _RuntimeClient,
        settings: LaunchSettings,
        source_library: SourceLibrary | None = None,
        writer_preparation: Mapping[str, Any] | None = None,
        _construction_authority: object | None = None,
    ) -> None:
        if _construction_authority is _PRODUCTION_CONSTRUCTION_AUTHORITY:
            self._assert_authorized_client(client, settings)
        elif _construction_authority is not _TEST_CONSTRUCTION_AUTHORITY:
            raise RuntimeInvariantError(
                "Codex runtime must be constructed by from_authorized_client()"
            )
        if config.backend_name != "codex-app-server":
            raise RuntimeInvariantError(
                "Codex runtime requires backend_name='codex-app-server'"
            )
        if config.backend_version != PINNED_CODEX_VERSION:
            raise RuntimeInvariantError(
                f"Codex runtime requires backend_version={PINNED_CODEX_VERSION!r}"
            )
        for role in ModelRole:
            spec = config.model_for(role)
            if spec.provider != "openai":
                raise RuntimeInvariantError(
                    f"Codex runtime requires provider='openai' for {role.value}"
                )
            if spec.effort == "ultra":
                raise RuntimeInvariantError(
                    "Codex runtime does not permit ultra effort because it can "
                    "activate proactive multi-agent behavior"
                )
        if client.server_version != PINNED_CODEX_VERSION:
            raise RuntimeInvariantError(
                "initialized App Server version differs from the Record backend"
            )
        protocol_pin = getattr(client, "protocol_pin", None)
        if (
            not isinstance(protocol_pin, ProtocolPin)
            or protocol_pin.cli_version != PINNED_CODEX_VERSION
        ):
            raise RuntimeInvariantError(
                "App Server client protocol pin differs from the reviewed version"
            )
        self.config = config
        self.client = client
        self.settings = settings
        self._operations: dict[str, _OperationState] = {}
        self._max_tracked_threads = (
            (config.max_model_calls if config.max_model_calls is not None else math.inf)
            + (
                2 * config.max_active_branches
                if config.max_active_branches is not None
                else math.inf
            )
            + 4
        )
        self._thread_roles: dict[str, ModelRole] = {}
        self._thread_owners: dict[str, str] = {}
        self._branch_writer_sessions: dict[str, str] = {}
        self._active_by_thread: dict[str, str] = {}
        # child provider thread -> the thread it was forked from.  A fork
        # copies the anchor's history into the child under the *original*
        # turn ids, and the App Server restates those turns' terminal events
        # on the child thread, so an event can legitimately carry one thread
        # id and a turn id owned by another.
        self._forked_parents: dict[str, str] = {}
        self._pending_forks: dict[str, int] = {}
        self._pending_fork_events: list[ProviderEvent] = []
        self._starting_threads: set[str] = set()
        self._registration_events: dict[str, asyncio.Event] = {}
        self._early_events: dict[str, list[ProviderEvent]] = {}
        self._early_event_count = 0
        self._unscoped_events: list[ProviderEvent] = []
        self._recovered_writer_turns: dict[
            tuple[str, str], tuple[JsonObject, WriterOutput]
        ] = {}
        self._router_task: asyncio.Task[None] | None = None
        self._router_error: AppServerError | None = None
        self._routed_sequence = 0
        self._closed = False
        self._scientific_tool_configured = False
        self._source_library = source_library
        self._writer_preparation = (
            copy.deepcopy(dict(writer_preparation))
            if writer_preparation is not None
            else None
        )
        if (config.generation_context_sha256 is None) != (
            self._writer_preparation is None
        ):
            raise RuntimeInvariantError(
                "generation context binding and Writer preparation differ"
            )
        if self._writer_preparation is not None and (
            self._writer_preparation.get("generation_context_sha256")
            != config.generation_context_sha256
            or self._writer_preparation.get("mode") != config.reading_mode
        ):
            raise RuntimeInvariantError(
                "Writer preparation differs from the frozen generation context"
            )
        dynamic_tool_audit_path = getattr(client, "dynamic_tool_audit_path", None)
        self._writer_preparation_audit_path = (
            None
            if dynamic_tool_audit_path is None
            else Path(dynamic_tool_audit_path).parent
            / "writer_preparation_deliveries.jsonl"
        )
        if (
            self._writer_preparation is not None
            and _construction_authority is _PRODUCTION_CONSTRUCTION_AUTHORITY
            and self._writer_preparation_audit_path is None
        ):
            raise RuntimeInvariantError(
                "Writer preparation requires a durable delivery audit path"
            )
        self._transcript_tool = TranscriptReadTool()
        self._feedback_tool = FeedbackReadTool()
        self._transcript_catalog_tool = TranscriptCatalogTool(self._transcript_tool)
        unsupported_tools = set(settings.capability_profile.allowed_tools) - {
            SCIENTIFIC_TOOL_NAME,
            *SOURCE_TOOL_NAMES,
            "transcript_read",
            "transcript_catalog",
            "feedback_read",
        }
        if unsupported_tools:
            raise RuntimeInvariantError(
                f"runtime capability contains unsupported tools: {sorted(unsupported_tools)}"
            )
        if set(SOURCE_TOOL_NAMES) & set(settings.capability_profile.allowed_tools):
            if source_library is None:
                raise RuntimeInvariantError(
                    "literature tools require an explicit SourceLibrary, including none conditions"
                )
            if (
                _construction_authority is _PRODUCTION_CONSTRUCTION_AUTHORITY
                and not source_library.snapshot_dir.is_relative_to(
                    settings.workspace.resolve()
                )
            ):
                raise RuntimeInvariantError(
                    "source snapshot must be inside the isolated provider workspace"
                )
            if (
                _construction_authority is _PRODUCTION_CONSTRUCTION_AUTHORITY
                and self.client.dynamic_tool_audit_path is None
            ):
                raise RuntimeInvariantError(
                    "literature tools require a durable tool audit path"
                )
        if (
            isinstance(self.client, (AppServerClient, SharedAppServerSession))
            and SCIENTIFIC_TOOL_NAME in settings.capability_profile.allowed_tools
        ):
            self._configure_scientific_tool()

    @staticmethod
    def _assert_authorized_client(
        client: _RuntimeClient, settings: LaunchSettings
    ) -> None:
        if (
            settings.authorized_client is not None
            and settings.authorized_client is not client
        ):
            raise RuntimeInvariantError(
                "App Server client differs from the authorized product client"
            )
        if not client.is_running:
            raise RuntimeInvariantError("authorized App Server client is not running")
        if client.cwd is None or client.env is None:
            raise RuntimeInvariantError(
                "authorized App Server client lacks an isolated cwd/environment"
            )
        settings.assert_authorized_command(
            AppServerCommand(
                argv=client.command,
                cwd=str(client.cwd),
                environment=client.env,
            )
        )
        identity = client.process_identity
        if not isinstance(identity, SpawnedProcessIdentity):
            raise RuntimeInvariantError(
                "authorized App Server client lacks spawned-process identity"
            )
        try:
            expected_executable = str(Path(client.command[0]).resolve(strict=True))
        except OSError as exc:
            raise RuntimeInvariantError(
                "authorized App Server executable is no longer resolvable"
            ) from exc
        if (
            identity.canonical_executable != expected_executable
            or identity.pid <= 0
            or identity.executable_st_dev < 0
            or identity.executable_st_ino < 0
            or identity.spawn_started_monotonic_ns <= 0
            or identity.spawn_completed_monotonic_ns
            < identity.spawn_started_monotonic_ns
            or identity.identity_source != "resolved_argv0_stat"
        ):
            raise RuntimeInvariantError(
                "authorized App Server spawned-process identity is invalid"
            )

    @classmethod
    def _from_test_client(
        cls,
        *,
        config: RunConfig,
        client: _RuntimeClient,
        settings: LaunchSettings,
        source_library: SourceLibrary | None = None,
        writer_preparation: Mapping[str, Any] | None = None,
    ) -> CodexAppServerRuntime:
        """Construct around a scripted client; never use in product code."""

        return cls(
            config=config,
            client=client,
            settings=settings,
            source_library=source_library,
            writer_preparation=writer_preparation,
            _construction_authority=_TEST_CONSTRUCTION_AUTHORITY,
        )

    @classmethod
    def from_authorized_client(
        cls,
        *,
        config: RunConfig,
        client: _RuntimeClient,
        settings: LaunchSettings,
        source_library: SourceLibrary | None = None,
        writer_preparation: Mapping[str, Any] | None = None,
    ) -> CodexAppServerRuntime:
        """Attach to the initialized client authorized by the product boundary."""

        return cls(
            config=config,
            client=client,
            settings=settings,
            source_library=source_library,
            writer_preparation=writer_preparation,
            _construction_authority=_PRODUCTION_CONSTRUCTION_AUTHORITY,
        )

    def writer_preparation(self, *, include_full: bool) -> Mapping[str, Any] | None:
        if self._writer_preparation is None:
            return None
        if include_full:
            return copy.deepcopy(self._writer_preparation)
        # The compact view comes from a reader-assisted preparation layer,
        # which this build does not include; without a method-source pack no
        # writer preparation can be constructed, so this is unreachable here.
        raise RuntimeInvariantError(
            "reader-assisted writer preparation is not available in this build"
        )

    @property
    def provider_events(self) -> tuple[ProviderEvent, ...]:
        events = list(self._unscoped_events)
        for state in self._operations.values():
            events.extend(state.events)
        return tuple(sorted(events, key=lambda event: event.sequence))

    def evidence_sources(self) -> tuple[EvidenceSource, ...]:
        """Snapshot text for Record validation; the prompt uses tool references."""
        return (
            ()
            if self._source_library is None
            else self._source_library.evidence_sources()
        )

    def evidence_source_documents(self) -> dict[str, str]:
        """Document grouping of the evidence sources (formula normalization)."""
        return (
            {}
            if self._source_library is None
            else self._source_library.evidence_source_documents()
        )

    def register_recovered_writer_session(
        self, session: RuntimeSession, branch_id: str
    ) -> None:
        """Bind a persisted provider handle restored from the product ControlStore."""

        if not branch_id.strip():
            raise RuntimeInvariantError("recovered writer branch id must be non-empty")
        if len(self._thread_owners) >= self._max_tracked_threads:
            raise RuntimeInvariantError("provider thread tracking limit exceeded")
        if session.session_id in self._thread_roles:
            raise RuntimeInvariantError(
                "cannot register ownership after a provider session is active"
            )
        expected = f"branch:{branch_id}"
        existing_session = self._branch_writer_sessions.get(branch_id)
        if existing_session is not None and existing_session != session.session_id:
            raise RuntimeInvariantError(
                "Branch already has a different persisted writer session"
            )
        existing = self._thread_owners.get(session.session_id)
        if existing is not None and existing != expected:
            raise RuntimeInvariantError(
                "recovered writer session has conflicting Branch ownership"
            )
        self._thread_owners[session.session_id] = expected
        self._branch_writer_sessions[branch_id] = session.session_id

    def rebind_branch_writer_session(
        self, branch_id: str, session: RuntimeSession
    ) -> None:
        """Move a Branch onto a writer session that was just minted for it.

        A Branch is bound to exactly one writer session, and
        ``_ensure_writer_session`` refuses to advance it with any other - that
        is what stops one Branch's turns from landing on another's thread.  But
        a Branch does legitimately change session without changing identity:
        the orchestrator rotates its provider context by rehydrating the
        bounded transcript into a fresh thread, and it rehydrates again when a
        restart leaves the Branch without a live bookmark.  The Branch is the
        same Branch; only the thread carrying it is new, so the binding has to
        move with it here, at the one point where the move is deliberate.
        Nothing is relaxed downstream: the session must be a writer thread this
        runtime just created and no Branch has claimed, so a live session can
        never be reassigned, and the Branch's previous session keeps its
        ownership - it stays a valid fork anchor for the steps it sealed, and
        handing it back to advance the Branch still fails the binding check.
        """

        if not branch_id.strip():
            raise RuntimeInvariantError("rebound writer branch id must be non-empty")
        expected = f"branch:{branch_id}"
        if self._thread_roles.get(session.session_id) is not ModelRole.WRITER:
            raise RuntimeInvariantError(
                "rebound Branch session is not a live writer thread"
            )
        owner = self._thread_owners.get(session.session_id)
        if owner == expected:
            return
        if owner not in {"unclaimed:fork", "unclaimed:rehydrate"}:
            raise RuntimeInvariantError(
                "rebound Branch session is already owned by another Branch"
            )
        self._thread_owners[session.session_id] = expected
        self._branch_writer_sessions[branch_id] = session.session_id

    async def close(self) -> None:
        if self._closed:
            return
        health_error: BaseException | None = None
        if getattr(self.client, "is_running", False):
            try:
                await self.assert_healthy()
            except BaseException as exc:  # noqa: BLE001 - preserve cleanup on cancellation
                health_error = exc
        if self._router_task is not None:
            self._router_task.cancel()
        await self.client.close()
        if self._router_task is not None and not self._router_task.done():
            done, _pending = await asyncio.wait(
                {self._router_task}, timeout=ClientTimeouts().close
            )
            if not done:
                raise RuntimeInvariantError("runtime event router did not close")
        self._closed = True
        if health_error is not None:
            raise health_error

    async def assert_healthy(self) -> None:
        """Flush prior provider frames and fail on any late protocol error."""

        await self._ensure_router()
        target_sequence = await self.client.protocol_barrier()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.settings.turn_timeout
        while self._routed_sequence < target_sequence:
            if self._router_error is not None:
                break
            if loop.time() >= deadline:
                raise RuntimeInvocationError(
                    "provider_protocol_error",
                    "provider event router did not reach the protocol barrier",
                    partial_output="",
                    retryable=False,
                )
            await asyncio.sleep(0)
        if self._router_error is not None:
            raise RuntimeInvocationError(
                "provider_protocol_error",
                str(self._router_error),
                partial_output="",
                retryable=False,
            )

    async def start_writer(
        self, request: WriterRequest, session: RuntimeSession | None
    ) -> RuntimeInvocation:
        try:
            self._require_run(request.run_id)
            self._require_intent_ledger_first(request.intent_ledger_first)
            self._require_dimension_check(request.dimension_check)
            self._require_fork_availability(request.fork_available)
            if "transcript_read" in self.settings.capability_profile.allowed_tools:
                if any(
                    self._thread_roles.get(thread_id)
                    in {ModelRole.WRITER, ModelRole.CHECKER}
                    for thread_id in self._active_by_thread
                ):
                    raise RuntimeInvariantError(
                        "transcript reader requires serialized writer/checker turns"
                    )
                self._transcript_tool.bind(
                    getattr(request, "full_transcript", ()) or request.transcript
                )
                self._feedback_tool.bind(
                    getattr(request, "full_checker_feedback", ())
                    or request.checker_feedback
                )
            await self._ensure_router()
            if session is None:
                if request.branch_id in self._branch_writer_sessions:
                    raise RuntimeInvariantError("Branch already owns a writer session")
                actual_session = await self._start_thread(
                    ModelRole.WRITER,
                    ProviderLineage.NATIVE,
                    writer_developer_instructions(
                        intent_ledger_first=self.config.intent_ledger_first,
                        dimension_check=self.config.dimension_check,
                        fork_available=self._fork_available,
                    ),
                    owner=f"branch:{request.branch_id}",
                )
            else:
                actual_session = await self._ensure_writer_session(
                    session, request.branch_id
                )
            prompt = writer_user_prompt(request)
            invocation = await self._start_turn(
                role=ModelRole.WRITER,
                request=request,
                session=actual_session,
                prompt=prompt,
                output_schema=writer_output_schema(request),
            )
            self._append_writer_preparation_audit(
                request=request,
                invocation=invocation,
                prompt=prompt,
            )
            return invocation
        except RuntimeInvocationError:
            raise
        except AppServerError as exc:
            raise self._client_failure(exc) from exc

    def _append_writer_preparation_audit(
        self,
        *,
        request: WriterRequest,
        invocation: RuntimeInvocation,
        prompt: str,
    ) -> None:
        preparation = request.preparation_context
        path = self._writer_preparation_audit_path
        if preparation is None or path is None:
            return
        documents = _preparation_documents(preparation)
        record = {
            "schema_version": "writer-preparation-delivery-v1",
            "run_id": request.run_id,
            "branch_id": request.branch_id,
            "step_slot": request.step_slot,
            "thread_id": invocation.session.session_id,
            "turn_id": invocation.operation_id,
            "mode": preparation.get("mode"),
            "generation_context_sha256": preparation.get("generation_context_sha256"),
            "preparation_payload_sha256": preparation.get("payload_sha256"),
            "delivered_preparation_sha256": hashlib.sha256(
                json.dumps(
                    preparation,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
            ).hexdigest(),
            "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            "full_document_delivery": bool(documents),
            "delivered_source_ids": [
                document.get("source_id")
                for document in documents
                if isinstance(document, Mapping)
            ],
            "delivered_char_count": sum(
                len(document.get("text", ""))
                for document in documents
                if isinstance(document, Mapping)
                and isinstance(document.get("text"), str)
            ),
        }
        encoded = (
            json.dumps(
                record,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            if os.write(descriptor, encoded) != len(encoded):
                raise OSError("Writer preparation audit append was incomplete")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    async def collect_writer(self, invocation: RuntimeInvocation) -> WriterOutput:
        output = await self._collect(invocation, ModelRole.WRITER)
        if not isinstance(output, WriterOutput):
            raise RuntimeInvariantError(
                "writer invocation produced another role output"
            )
        return output

    async def start_checker(self, request: CheckRequest) -> RuntimeInvocation:
        try:
            self._require_run(request.run_id)
            self._require_intent_ledger_first(request.intent_ledger_first)
            self._require_dimension_check(request.dimension_check)
            if "transcript_read" in self.settings.capability_profile.allowed_tools:
                if any(
                    self._thread_roles.get(thread_id)
                    in {ModelRole.WRITER, ModelRole.CHECKER}
                    for thread_id in self._active_by_thread
                ):
                    raise RuntimeInvariantError(
                        "transcript reader requires serialized writer/checker turns"
                    )
                steps = list(
                    getattr(request, "full_transcript", ()) or request.transcript
                )
                if request.target.step_revision_id not in {
                    step.step_revision_id for step in steps
                }:
                    steps.append(request.target)
                self._transcript_tool.bind(steps)
            await self._ensure_router()
            session = await self._start_thread(
                ModelRole.CHECKER,
                ProviderLineage.NATIVE,
                checker_developer_instructions(
                    intent_ledger_first=self.config.intent_ledger_first,
                    dimension_check=self.config.dimension_check,
                ),
                owner=f"check:{request.check_id}",
            )
            return await self._start_turn(
                role=ModelRole.CHECKER,
                request=request,
                session=session,
                prompt=checker_user_prompt(request),
                output_schema=checker_output_schema(request),
            )
        except RuntimeInvocationError:
            raise
        except AppServerError as exc:
            raise self._client_failure(exc) from exc

    async def collect_checker(self, invocation: RuntimeInvocation) -> CheckOutput:
        output = await self._collect(invocation, ModelRole.CHECKER)
        if not isinstance(output, CheckOutput):
            raise RuntimeInvariantError(
                "checker invocation produced another role output"
            )
        return output

    async def start_judge(self, request: JudgeRequest) -> RuntimeInvocation:
        try:
            self._require_run(request.run_id)
            await self._ensure_router()
            session = await self._start_thread(
                ModelRole.JUDGE,
                ProviderLineage.NATIVE,
                JUDGE_DEVELOPER_INSTRUCTIONS,
                owner=f"judgement:{request.judgement_id}",
            )
            return await self._start_turn(
                role=ModelRole.JUDGE,
                request=request,
                session=session,
                prompt=judge_user_prompt(request),
                output_schema=JUDGE_OUTPUT_SCHEMA,
            )
        except RuntimeInvocationError:
            raise
        except AppServerError as exc:
            raise self._client_failure(exc) from exc

    async def collect_judge(self, invocation: RuntimeInvocation) -> JudgeOutput:
        output = await self._collect(invocation, ModelRole.JUDGE)
        if not isinstance(output, JudgeOutput):
            raise RuntimeInvariantError("judge invocation produced another role output")
        return output

    async def start_formula_repair(
        self, request: FormulaRepairRequest
    ) -> RuntimeInvocation:
        """One typeset-layer repair turn on a fresh, tool-free Writer thread.

        It carries the run's Writer model, effort and service tier, and no
        literature, transcript or computation tool: the turn sees only the
        recorded step fields and the failing formulas it is asked about.
        """
        try:
            self._require_run(request.run_id)
            await self._ensure_router()
            session = await self._start_thread(
                ModelRole.WRITER,
                ProviderLineage.NATIVE,
                FORMULA_REPAIR_DEVELOPER_INSTRUCTIONS,
                owner=f"formula_repair:{request.repair_id}",
                ephemeral=True,
                tools=False,
            )
            return await self._start_turn(
                role=ModelRole.WRITER,
                request=request,
                session=session,
                prompt=formula_repair_user_prompt(request),
                output_schema=formula_repair_output_schema(request),
            )
        except RuntimeInvocationError:
            raise
        except AppServerError as exc:
            raise self._client_failure(exc) from exc

    async def collect_formula_repair(
        self, invocation: RuntimeInvocation
    ) -> FormulaRepairOutput:
        output = await self._collect(invocation, ModelRole.WRITER)
        if not isinstance(output, FormulaRepairOutput):
            raise RuntimeInvariantError(
                "formula repair invocation produced another output"
            )
        return output

    async def start_formula_review(
        self, request: FormulaEquivalenceRequest
    ) -> RuntimeInvocation:
        """One typeset-layer equivalence review on a fresh Checker thread."""
        try:
            self._require_run(request.run_id)
            await self._ensure_router()
            session = await self._start_thread(
                ModelRole.CHECKER,
                ProviderLineage.NATIVE,
                FORMULA_REVIEW_DEVELOPER_INSTRUCTIONS,
                owner=f"formula_review:{request.review_id}",
                ephemeral=True,
                tools=False,
            )
            return await self._start_turn(
                role=ModelRole.CHECKER,
                request=request,
                session=session,
                prompt=formula_review_user_prompt(request),
                output_schema=formula_review_output_schema(request),
            )
        except RuntimeInvocationError:
            raise
        except AppServerError as exc:
            raise self._client_failure(exc) from exc

    async def collect_formula_review(
        self, invocation: RuntimeInvocation
    ) -> FormulaEquivalenceOutput:
        output = await self._collect(invocation, ModelRole.CHECKER)
        if not isinstance(output, FormulaEquivalenceOutput):
            raise RuntimeInvariantError(
                "formula review invocation produced another output"
            )
        return output

    async def fork(
        self, session: RuntimeSession, completed_operation_id: str
    ) -> RuntimeSession:
        pending_parent: str | None = None
        try:
            await self._ensure_router()
            snapshot = await self.client.thread_read(
                session.session_id, include_turns=True
            )
            thread = snapshot["thread"]
            turn = self._find_turn(thread, completed_operation_id)
            if turn is None or turn.get("status") != "completed":
                raise ProviderForkError(
                    "fork anchor is missing or not a completed provider turn"
                )
            state = self._operations.get(completed_operation_id)
            recovered = self._recovered_writer_turns.get(
                (session.session_id, completed_operation_id)
            )
            if state is not None:
                self._validate_invocation(
                    state,
                    RuntimeInvocation(
                        session,
                        completed_operation_id,
                        ModelRole.WRITER,
                    ),
                    ModelRole.WRITER,
                )
                if not isinstance(state.output, WriterOutput):
                    raise RuntimeInvariantError(
                        "fork anchor has not completed live collection"
                    )
                request = state.request
                if (
                    not isinstance(request, WriterRequest)
                    or self._thread_owners.get(session.session_id)
                    != f"branch:{request.branch_id}"
                ):
                    raise RuntimeInvariantError(
                        "fork anchor session is not owned by its writer branch"
                    )
                # The App Server may re-render a sealed turn in a wider
                # ``itemsView`` - the user message prepended, item ids made
                # positional - without changing anything the turn did.  That is
                # the same rendering the client tolerates, so the anchor is
                # compared under the client's own rule rather than a second
                # copy of it that could drift.  ``_writer_output_from_turn``
                # reads only the single final agent message's text, so the
                # equality below still proves the answer itself is unchanged.
                if not same_terminal_turn(state.terminal_turn, turn):
                    raise ProviderForkError("fork anchor changed after live collection")
                verified_output = self._writer_output_from_turn(turn, usage=state.usage)
                if verified_output != state.output:
                    raise ProviderForkError(
                        "fork anchor output differs from the sealed live output"
                    )
            elif recovered is not None:
                owner = self._thread_owners.get(session.session_id)
                if owner is None or not owner.startswith("branch:"):
                    raise RuntimeInvariantError(
                        "recovered fork anchor lacks restored Branch ownership"
                    )
                recovered_turn, recovered_output = recovered
                if not same_terminal_turn(recovered_turn, turn):
                    raise ProviderForkError(
                        "fork anchor changed after restart reconciliation"
                    )
                if self._writer_output_from_turn(turn) != recovered_output:
                    raise ProviderForkError(
                        "fork anchor output differs from restart reconciliation"
                    )
            else:
                raise RuntimeInvariantError(
                    "fork anchor lacks live or reconciled writer output"
                )
            spec = self.config.model_for(ModelRole.WRITER)
            pending_parent = session.session_id
            self._pending_forks[pending_parent] = (
                self._pending_forks.get(pending_parent, 0) + 1
            )
            result = await self.client.thread_fork(
                session.session_id,
                completed_operation_id,
                overrides=self._thread_overrides(
                    spec,
                    role=ModelRole.WRITER,
                    ephemeral=False,
                    set_reasoning_effort=True,
                ),
                expected_reasoning_effort=spec.effort,
                require_persistent=True,
                require_completed_turn=True,
            )
            self._validate_reasoning_effort(result, spec, "thread/fork")
            thread = result["thread"]
            if thread.get("ephemeral") is not False:
                raise RuntimeInvariantError(
                    "thread/fork response returned a non-persistent writer"
                )
            thread_id = thread["id"]
            if thread_id in self._thread_roles or thread_id in self._thread_owners:
                raise RuntimeInvariantError(
                    "thread/fork returned an already known provider thread id"
                )
            child = RuntimeSession(thread_id, ProviderLineage.FORKED)
            self._thread_roles[thread_id] = ModelRole.WRITER
            self._thread_owners[thread_id] = "unclaimed:fork"
            self._forked_parents[thread_id] = session.session_id
            return child
        except RuntimeInvocationError:
            raise
        except _FORK_CONFINED_CLIENT_ERRORS as exc:
            # One refused fork, not a broken provider: the parent branch and its
            # session are still live, so this is handed back as a fork-specific
            # failure the orchestrator can trace and continue past.
            raise ProviderForkError(str(exc)) from exc
        except AppServerError as exc:
            raise self._client_failure(exc) from exc
        finally:
            if pending_parent is not None:
                remaining = self._pending_forks[pending_parent] - 1
                if remaining:
                    self._pending_forks[pending_parent] = remaining
                else:
                    del self._pending_forks[pending_parent]
                self._drain_pending_fork_events()

    async def rehydrate(self, transcript: Sequence[StepSnapshot]) -> RuntimeSession:
        try:
            await self._ensure_router()
            return await self._start_thread(
                ModelRole.WRITER,
                ProviderLineage.REHYDRATED,
                rehydrated_writer_instructions(
                    transcript,
                    intent_ledger_first=self.config.intent_ledger_first,
                    dimension_check=self.config.dimension_check,
                    fork_available=self._fork_available,
                ),
                owner="unclaimed:rehydrate",
            )
        except RuntimeInvocationError:
            raise
        except AppServerError as exc:
            raise self._client_failure(exc) from exc

    async def interrupt(self, invocation: RuntimeInvocation) -> RuntimeInterruption:
        state = self._operations.get(invocation.operation_id)
        if state is not None:
            self._validate_invocation(state, invocation, invocation.role)
        try:
            await self.client.turn_interrupt(
                invocation.session.session_id, invocation.operation_id
            )
        except AppServerError as exc:
            raise self._client_failure(
                exc, partial_output="" if state is None else state.partial_output
            ) from exc
        partial = "" if state is None else state.partial_output
        if state is not None:
            state.interrupted = True
            self._active_by_thread.pop(invocation.session.session_id, None)
        return RuntimeInterruption(partial_output=partial)

    async def reconcile(self, invocation: RuntimeInvocation) -> ReconcileResult:
        try:
            await self._ensure_router()
        except RuntimeInvocationError as exc:
            return self._reconcile_failure(exc)
        state = self._operations.get(invocation.operation_id)
        if state is not None:
            self._validate_invocation(state, invocation, invocation.role)
            if state.output is not None:
                return self._reconcile_completed(state.output)
            if state.failure is not None:
                return self._reconcile_failure(state.failure)
        if state is None:
            try:
                await self._resume_session_for_reconcile(invocation)
            except AppServerError as exc:
                return self._reconcile_failure(self._client_failure(exc))
            except RuntimeInvariantError as exc:
                return self._reconcile_failure(
                    RuntimeInvocationError(
                        "provider_protocol_error",
                        str(exc),
                        partial_output="",
                        retryable=False,
                    )
                )
        try:
            snapshot = await self.client.thread_read(
                invocation.session.session_id, include_turns=True
            )
        except AppServerRemoteError as exc:
            return ReconcileResult(
                status=ReconcileStatus.MISSING,
                output=None,
                partial_output="" if state is None else state.partial_output,
                failure_kind="unknown_provider_state",
                message=str(exc),
                retryable=True,
            )
        except AppServerError as exc:
            return self._reconcile_failure(
                self._client_failure(
                    exc,
                    partial_output="" if state is None else state.partial_output,
                )
            )
        thread = snapshot["thread"]
        try:
            self._validate_reconcile_thread(invocation.role, thread)
        except RuntimeInvariantError as exc:
            failure = RuntimeInvocationError(
                "provider_protocol_error",
                str(exc),
                partial_output="" if state is None else state.partial_output,
                retryable=False,
            )
            if state is not None:
                state.failure = failure
            return self._reconcile_failure(failure)
        turn = self._find_turn(thread, invocation.operation_id)
        if turn is None:
            return ReconcileResult(
                status=ReconcileStatus.MISSING,
                output=None,
                partial_output="" if state is None else state.partial_output,
                failure_kind="unknown_provider_state",
                message="provider thread does not contain the operation",
                retryable=True,
            )
        partial = self._partial_from_turn(turn)
        status = turn["status"]
        if status == "inProgress":
            return ReconcileResult(
                status=ReconcileStatus.RUNNING,
                output=None,
                partial_output=partial,
                failure_kind=None,
                message=None,
                retryable=False,
            )
        try:
            self._audit_terminal_items(invocation.role, turn)
        except ValueError as exc:
            failure = RuntimeInvocationError(
                "invalid_model_output",
                str(exc),
                partial_output=partial,
                retryable=False,
            )
            if state is not None:
                state.failure = failure
            return self._reconcile_failure(failure)
        self._active_by_thread.pop(invocation.session.session_id, None)
        if status == "interrupted":
            outcome = ReconcileResult(
                status=ReconcileStatus.INTERRUPTED,
                output=None,
                partial_output=partial,
                failure_kind="provider_interrupted",
                message="provider turn was interrupted",
                retryable=True,
            )
            if state is not None:
                state.failure = RuntimeInvocationError(
                    "provider_interrupted",
                    outcome.message or "provider turn was interrupted",
                    partial_output=partial,
                    retryable=True,
                )
            return outcome
        if status == "failed":
            message = self._turn_error_message(turn)
            outcome = ReconcileResult(
                status=ReconcileStatus.FAILED,
                output=None,
                partial_output=partial,
                failure_kind="provider_failed",
                message=message,
                retryable=True,
            )
            if state is not None:
                state.failure = RuntimeInvocationError(
                    "provider_failed",
                    message,
                    partial_output=partial,
                    retryable=True,
                )
            return outcome
        try:
            if (
                state is not None
                and state.terminal_turn is not None
                and not same_terminal_turn(state.terminal_turn, turn)
            ):
                # Same read, same tolerance as the fork anchor: ``turn`` came
                # from ``thread/read``, which may render a sealed turn in a
                # wider ``itemsView`` than the ``turn/completed`` event did.
                raise AppServerProtocolError(
                    "reconcile terminal snapshot differs from the live event"
                )
            output = self._output_from_live_turn(
                invocation.role,
                turn,
                {},
                request=state.request if state is not None else None,
            )
        except AppServerProtocolError as exc:
            failure = RuntimeInvocationError(
                "provider_protocol_error",
                str(exc),
                partial_output=partial,
                retryable=False,
            )
            if state is not None:
                state.failure = failure
            return self._reconcile_failure(failure)
        except ValueError as exc:
            return ReconcileResult(
                status=ReconcileStatus.FAILED,
                output=None,
                partial_output=partial,
                failure_kind="invalid_model_output",
                message=str(exc),
                retryable=True,
            )
        if state is not None:
            state.output = output
        elif invocation.role is ModelRole.WRITER:
            assert isinstance(output, WriterOutput)
            self._recovered_writer_turns[
                (invocation.session.session_id, invocation.operation_id)
            ] = (copy.deepcopy(turn), output)
        return self._reconcile_completed(output)

    async def _ensure_router(self) -> None:
        if self._closed:
            raise RuntimeInvariantError("runtime is closed")
        if self._router_task is None:
            self._router_task = asyncio.create_task(
                self._route_events(), name="codex-runtime-event-router"
            )
        if self._router_error is not None:
            raise RuntimeInvocationError(
                "provider_protocol_error",
                str(self._router_error),
                partial_output="",
                retryable=False,
            )

    async def _route_events(self) -> None:
        try:
            while True:
                event = await self.client.next_event()
                self._route_event(event)
        except asyncio.CancelledError:
            raise
        except AppServerError as exc:
            self._router_error = exc
        except BaseException as exc:  # noqa: BLE001 - fatal async task boundary
            self._router_error = AppServerProtocolError(
                f"runtime event router failed: {exc}"
            )
        if self._router_error is not None:
            for state in self._operations.values():
                state.completion.set()

    def _route_event(self, event: ProviderEvent) -> None:
        self._routed_sequence = max(self._routed_sequence, event.sequence)
        turn_id = self._event_turn_id(event)
        if turn_id is None:
            # This is retained audit history, not an in-flight event buffer. An
            # unlimited run must not acquire a lifetime cap via thread notices.
            if (
                self.config.max_model_calls is not None
                and len(self._unscoped_events) >= self.settings.max_unscoped_events
            ):
                raise AppServerProtocolError("runtime unscoped event buffer is full")
            self._unscoped_events.append(event)
            return
        state = self._operations.get(turn_id)
        if state is None:
            if (
                turn_id not in self._early_events
                and len(self._early_events) >= self.settings.max_early_turn_ids
            ):
                raise AppServerProtocolError(
                    "runtime early-event turn-id buffer is full"
                )
            if self._early_event_count >= self.settings.max_early_events:
                raise AppServerProtocolError(
                    "runtime global early-event buffer is full"
                )
            early = self._early_events.setdefault(turn_id, [])
            if len(early) >= self.settings.max_events_per_operation:
                raise AppServerProtocolError("runtime early event buffer is full")
            early.append(event)
            self._early_event_count += 1
            return
        self._apply_event(state, event)

    @staticmethod
    def _event_turn_id(event: ProviderEvent) -> str | None:
        turn = event.params.get("turn")
        if isinstance(turn, Mapping) and isinstance(turn.get("id"), str):
            return turn["id"]
        turn_id = event.params.get("turnId")
        if isinstance(turn_id, str):
            return turn_id
        return None

    def _is_forked_from(self, thread_id: str, ancestor: str) -> bool:
        """Was ``thread_id`` produced by forking ``ancestor``, directly or not?"""

        seen: set[str] = set()
        current = self._forked_parents.get(thread_id)
        while current is not None and current not in seen:
            if current == ancestor:
                return True
            seen.add(current)
            current = self._forked_parents.get(current)
        return False

    def _drain_pending_fork_events(self) -> None:
        # A thread/fork notification can beat its response. Revisit only after
        # the response has been validated and lineage registered (or rejected).
        # Concurrent forks may still explain an event; _apply_event re-buffers
        # it until those responses also settle. Unknown threads never acquire
        # ownership merely by appearing in a notification.
        pending, self._pending_fork_events = self._pending_fork_events, []
        try:
            for event in pending:
                self._route_event(event)
        except AppServerError as exc:
            self._router_error = exc
            for state in self._operations.values():
                state.completion.set()
            raise self._client_failure(exc) from exc

    def _apply_event(self, state: _OperationState, event: ProviderEvent) -> None:
        if len(state.events) >= self.settings.max_events_per_operation:
            raise AppServerProtocolError("operation event buffer is full")
        thread_id = event.params.get("threadId")
        if thread_id is not None and thread_id != state.invocation.session.session_id:
            # A fork copies the anchor's history into the child thread under the
            # original turn ids, and the App Server restates those turns'
            # terminal events on the child.  A live fork probe met that as
            # "provider event threadId does not match its turn", which killed the
            # event router: every later turn then completed at the provider and
            # was never observed, the run burned its whole turn timeout, and it
            # ended on the unrelated invariant that the stale in-flight entry
            # tripped next.  Copied history is history - it says nothing new
            # about the sealed operation, so it is dropped rather than applied.
            # A *live* turn's event on the wrong thread is still a protocol
            # error, which is what the terminal check preserves.
            if state.terminal_turn is not None and self._is_forked_from(
                thread_id, state.invocation.session.session_id
            ):
                return
            if state.terminal_turn is not None and any(
                parent == state.invocation.session.session_id
                or self._is_forked_from(parent, state.invocation.session.session_id)
                for parent in self._pending_forks
            ):
                if len(self._pending_fork_events) >= self.settings.max_early_events:
                    raise AppServerProtocolError(
                        "runtime pending fork event buffer is full"
                    )
                self._pending_fork_events.append(event)
                return
            raise AppServerProtocolError(
                "provider event threadId does not match its turn"
            )
        if state.terminal_turn is not None and event.kind in {
            ProviderEventKind.ITEM,
            ProviderEventKind.STREAM,
        }:
            raise AppServerProtocolError(
                "provider emitted model-content mutation after turn completion"
            )
        state.events.append(event)
        if event.method == "item/agentMessage/delta":
            delta = event.params.get("delta")
            if not isinstance(delta, str):
                raise AppServerProtocolError("agent message delta must be text")
            state.partial_chunks.append(delta)
        elif event.method == "thread/tokenUsage/updated":
            usage = event.params.get("tokenUsage")
            if not isinstance(usage, dict):
                raise AppServerProtocolError("token usage must be an object")
            state.usage = copy.deepcopy(usage)
        elif event.method == "turn/completed":
            if not isinstance(thread_id, str):
                raise AppServerProtocolError(
                    "turn/completed is missing string threadId"
                )
            turn = event.params.get("turn")
            if not isinstance(turn, dict):
                raise AppServerProtocolError("turn/completed is missing turn")
            if turn.get("id") != state.invocation.operation_id:
                raise AppServerProtocolError(
                    "terminal turn id does not match the routed invocation"
                )
            top_level_turn_id = event.params.get("turnId")
            if (
                top_level_turn_id is not None
                and top_level_turn_id != state.invocation.operation_id
            ):
                raise AppServerProtocolError(
                    "turn/completed turnId conflicts with embedded turn.id"
                )
            if state.terminal_turn is not None:
                if state.terminal_turn == turn:
                    return
                raise AppServerProtocolError(
                    "provider emitted divergent terminal snapshots for one turn"
                )
            state.terminal_turn = copy.deepcopy(turn)
            state.completion.set()

    async def _start_thread(
        self,
        role: ModelRole,
        lineage: ProviderLineage,
        developer_instructions: str,
        *,
        owner: str,
        ephemeral: bool | None = None,
        tools: bool = True,
    ) -> RuntimeSession:
        """Open one provider thread.

        ``ephemeral`` defaults to the role's own persistence (only a Writer
        branch thread is persistent) and ``tools=False`` opens a thread with no
        dynamic tools at all, which is what a typeset-layer turn gets.
        """
        if len(self._thread_roles) >= self._max_tracked_threads:
            raise RuntimeInvariantError("provider thread tracking limit exceeded")
        spec = self.config.model_for(role)
        ephemeral = role is not ModelRole.WRITER if ephemeral is None else ephemeral
        params = self._thread_overrides(
            spec,
            role=role,
            ephemeral=ephemeral,
            set_reasoning_effort=True,
            tools=tools,
        )
        params.update(
            {
                "allowProviderModelFallback": False,
                "developerInstructions": developer_instructions,
                "multiAgentMode": "explicitRequestOnly",
            }
        )
        result = await self.client.thread_start(
            params, require_persistent=not ephemeral
        )
        self._validate_reasoning_effort(result, spec, "thread/start")
        thread = result["thread"]
        if thread.get("ephemeral") is not ephemeral:
            raise RuntimeInvariantError(
                "thread/start response changed the requested persistence"
            )
        if thread.get("turns") != []:
            raise RuntimeInvariantError(
                "thread/start returned a thread with hidden prior turns"
            )
        thread_id = thread["id"]
        if not isinstance(thread_id, str):
            raise RuntimeInvariantError("thread/start returned an invalid id")
        if (
            thread_id in self._thread_roles
            or thread_id in self._thread_owners
            or thread_id in self._branch_writer_sessions.values()
        ):
            raise RuntimeInvariantError("thread/start reused an existing id")
        self._thread_roles[thread_id] = role
        self._thread_owners[thread_id] = owner
        if role is ModelRole.WRITER and owner.startswith("branch:"):
            branch_id = owner.removeprefix("branch:")
            existing = self._branch_writer_sessions.get(branch_id)
            if existing is not None and existing != thread_id:
                raise RuntimeInvariantError(
                    "Branch already owns a different writer session"
                )
            self._branch_writer_sessions[branch_id] = thread_id
        return RuntimeSession(thread_id, lineage)

    def _thread_overrides(
        self,
        spec: ModelSpec,
        *,
        role: ModelRole,
        ephemeral: bool,
        set_reasoning_effort: bool,
        tools: bool = True,
    ) -> JsonObject:
        thread_config = copy.deepcopy(dict(self.settings.thread_config))
        if set_reasoning_effort:
            thread_config["model_reasoning_effort"] = spec.effort
        params: JsonObject = {
            "cwd": str(self.settings.workspace),
            "model": spec.model,
            "modelProvider": spec.provider,
            "approvalPolicy": "never",
            "approvalsReviewer": "user",
            "permissions": self.settings.permission_profile,
            "ephemeral": ephemeral,
            "runtimeWorkspaceRoots": [str(self.settings.workspace)],
            "selectedCapabilityRoots": [],
            "dynamicTools": self._dynamic_tools_for_role(role) if tools else [],
            "config": thread_config,
        }
        service_tier = _app_server_service_tier(self.config.service_tier)
        if service_tier is not None:
            params["serviceTier"] = service_tier
        return params

    def _dynamic_tools_for_role(self, role: ModelRole) -> list[JsonObject]:
        allowed = self.settings.capability_profile.allowed_tools
        has_sources = bool(set(SOURCE_TOOL_NAMES) & set(allowed))
        names = [
            name
            for name in allowed
            if (
                name not in {"transcript_read", "transcript_catalog"}
                or role in {ModelRole.WRITER, ModelRole.CHECKER}
            )
            and (
                name != SCIENTIFIC_TOOL_NAME or role is ModelRole.WRITER or has_sources
            )
            and (name != "feedback_read" or role is ModelRole.WRITER)
        ]
        if isinstance(self.client, (AppServerClient, SharedAppServerSession)):
            self._configure_scientific_tool()
            return self.client.dynamic_tool_specs(names)
        # Scripted runtime clients consume the same declaration in hermetic tests;
        # only AppServerClient can execute client-initiated tool requests.
        specs = {
            SCIENTIFIC_TOOL_NAME: SCIENTIFIC_TOOL_SPEC,
            **SOURCE_TOOL_SPECS,
            "transcript_read": TranscriptReadTool.spec,
            "transcript_catalog": TranscriptCatalogTool.spec,
            "feedback_read": FeedbackReadTool.spec,
        }
        return [copy.deepcopy(specs[name]) for name in names]

    def _configure_scientific_tool(self) -> None:
        if self._scientific_tool_configured:
            return
        if not isinstance(self.client, (AppServerClient, SharedAppServerSession)):
            raise RuntimeInvariantError(
                "scientific tool execution requires AppServerClient"
            )
        environment = self.client.env
        if environment is None:
            raise RuntimeInvariantError(
                "scientific tool requires a controlled client environment"
            )
        try:
            allowed = self.settings.capability_profile.allowed_tools
            tools = []
            if SCIENTIFIC_TOOL_NAME in allowed:
                tools.append(scientific_calculator_from_environment(environment))
            if self._source_library is not None:
                tools.extend(
                    tool
                    for tool in self._source_library.tools()
                    if tool.name in allowed
                )
            if "transcript_read" in allowed:
                tools.append(self._transcript_tool)
            if "transcript_catalog" in allowed:
                tools.append(self._transcript_catalog_tool)
            if "feedback_read" in allowed:
                tools.append(self._feedback_tool)
            self.client.configure_dynamic_tools(tools)
        except ScientificRuntimeError as exc:
            raise RuntimeInvariantError(
                f"scientific tool runtime is unavailable: {exc}"
            ) from exc
        self._scientific_tool_configured = True

    async def _ensure_writer_session(
        self, session: RuntimeSession, branch_id: str
    ) -> RuntimeSession:
        role = self._thread_roles.get(session.session_id)
        expected_owner = f"branch:{branch_id}"
        bound_session = self._branch_writer_sessions.get(branch_id)
        if bound_session is not None and bound_session != session.session_id:
            raise RuntimeInvariantError(
                "writer session differs from the Branch binding"
            )
        if role is None:
            if (
                bound_session != session.session_id
                or self._thread_owners.get(session.session_id) != expected_owner
            ):
                raise RuntimeInvariantError(
                    "persisted writer session lacks exact restored Branch ownership"
                )
            spec = self.config.model_for(ModelRole.WRITER)
            overrides = self._thread_overrides(
                spec,
                role=ModelRole.WRITER,
                ephemeral=False,
                set_reasoning_effort=True,
            )
            overrides.pop("ephemeral")
            result = await self.client.thread_resume(
                session.session_id,
                overrides=overrides,
                expected_reasoning_effort=spec.effort,
                require_persistent=True,
            )
            self._validate_reasoning_effort(result, spec, "thread/resume")
            if result["thread"].get("ephemeral") is not False:
                raise RuntimeInvariantError(
                    "thread/resume response changed writer persistence"
                )
            if any(
                isinstance(turn, Mapping) and turn.get("status") == "inProgress"
                for turn in result["thread"].get("turns", [])
            ):
                raise RuntimeInvariantError(
                    "resumed writer has an in-progress turn; reconcile it before reuse"
                )
            self._thread_roles[session.session_id] = ModelRole.WRITER
        elif role is not ModelRole.WRITER:
            raise RuntimeInvariantError(
                "checker or judge session cannot become a writer session"
            )
        else:
            owner = self._thread_owners.get(session.session_id)
            if owner in {"unclaimed:fork", "unclaimed:rehydrate"}:
                if bound_session is not None:
                    raise RuntimeInvariantError(
                        "Branch already owns another writer session"
                    )
                self._thread_owners[session.session_id] = expected_owner
                self._branch_writer_sessions[branch_id] = session.session_id
            elif owner != expected_owner:
                raise RuntimeInvariantError("writer session belongs to another Branch")
        return session

    async def _resume_session_for_reconcile(
        self, invocation: RuntimeInvocation
    ) -> None:
        spec = self.config.model_for(invocation.role)
        expected_ephemeral = invocation.role is not ModelRole.WRITER
        overrides = self._thread_overrides(
            spec,
            role=invocation.role,
            ephemeral=expected_ephemeral,
            set_reasoning_effort=True,
        )
        overrides.pop("ephemeral")
        result = await self.client.thread_resume(
            invocation.session.session_id,
            overrides=overrides,
            expected_reasoning_effort=spec.effort,
            require_persistent=invocation.role is ModelRole.WRITER,
        )
        self._validate_reasoning_effort(result, spec, "thread/resume")
        thread = result["thread"]
        if thread.get("ephemeral") is not expected_ephemeral:
            raise RuntimeInvariantError(
                "thread/resume response changed role-specific persistence"
            )
        self._thread_roles[invocation.session.session_id] = invocation.role

    @staticmethod
    def _validate_reasoning_effort(
        result: Mapping[str, Any], spec: ModelSpec, method: str
    ) -> None:
        if result.get("reasoningEffort") != spec.effort:
            raise RuntimeInvariantError(
                f"{method} reasoningEffort differs from the Run configuration"
            )

    def _validate_reconcile_thread(
        self, role: ModelRole, thread: Mapping[str, Any]
    ) -> None:
        spec = self.config.model_for(role)
        expected_ephemeral = role is not ModelRole.WRITER
        if thread.get("ephemeral") is not expected_ephemeral:
            raise RuntimeInvariantError(
                "thread/read persistence differs from the invocation role"
            )
        if thread.get("cwd") != str(self.settings.workspace):
            raise RuntimeInvariantError(
                "thread/read cwd differs from the authorized workspace"
            )
        if thread.get("modelProvider") != spec.provider:
            raise RuntimeInvariantError(
                "thread/read model provider differs from the Run configuration"
            )

    async def _start_turn(
        self,
        *,
        role: ModelRole,
        request: _Request,
        session: RuntimeSession,
        prompt: str,
        output_schema: Mapping[str, Any],
    ) -> RuntimeInvocation:
        if (
            self.config.max_model_calls is not None
            and len(self._operations) >= self.config.max_model_calls
        ):
            raise RuntimeInvariantError("Run max_model_calls tracking limit exceeded")
        thread_id = session.session_id
        if thread_id in self._active_by_thread or thread_id in self._starting_threads:
            raise RuntimeInvariantError("provider thread already has an active turn")
        expected_role = self._thread_roles.get(thread_id)
        if expected_role is not role:
            raise RuntimeInvariantError("provider thread role mismatch")
        spec = self.config.model_for(role)
        self._starting_threads.add(thread_id)
        started_after_sequence = self._routed_sequence
        registration = asyncio.Event()
        self._registration_events[thread_id] = registration
        turn_overrides: JsonObject = {
            "model": spec.model,
            "effort": spec.effort,
            "approvalPolicy": "never",
            "approvalsReviewer": "user",
            "permissions": self.settings.permission_profile,
            "outputSchema": copy.deepcopy(dict(output_schema)),
        }
        try:
            result = await self.client.turn_start(
                thread_id,
                [{"type": "text", "text": prompt}],
                overrides=turn_overrides,
            )
        except BaseException:
            self._starting_threads.discard(thread_id)
            self._registration_events.pop(thread_id, None)
            registration.set()
            raise
        turn_id = result["turn"]["id"]
        if result["turn"].get("items") != []:
            raise RuntimeInvariantError(
                "turn/start returned a turn with hidden prior items"
            )
        invocation = RuntimeInvocation(session, turn_id, role)
        state = _OperationState(
            invocation=invocation,
            request=request,
            started_after_sequence=started_after_sequence,
        )
        if turn_id in self._operations:
            raise RuntimeInvariantError("turn/start returned a duplicate operation id")
        self._operations[turn_id] = state
        self._active_by_thread[thread_id] = turn_id
        try:
            for event in self._early_events.pop(turn_id, []):
                self._early_event_count -= 1
                self._apply_event(state, event)
        finally:
            self._starting_threads.discard(thread_id)
            self._registration_events.pop(thread_id, None)
            registration.set()
        return invocation

    async def _collect(
        self, invocation: RuntimeInvocation, role: ModelRole
    ) -> RuntimeOutput:
        state = self._operations.get(invocation.operation_id)
        if state is None:
            raise RuntimeInvariantError("unknown provider invocation")
        self._validate_invocation(state, invocation, role)
        if state.output is not None:
            return state.output
        if state.failure is not None:
            raise state.failure
        try:
            await asyncio.wait_for(
                state.completion.wait(), timeout=self.settings.turn_timeout
            )
        except TimeoutError as exc:
            try:
                await self.client.turn_interrupt(
                    invocation.session.session_id, invocation.operation_id
                )
            except AppServerError as interrupt_error:
                state.failure = RuntimeInvocationError(
                    "provider_timeout_interrupt_failed",
                    str(interrupt_error),
                    partial_output=state.partial_output,
                    retryable=False,
                )
                raise state.failure from interrupt_error
            try:
                snapshot = await self.client.thread_read(
                    invocation.session.session_id, include_turns=True
                )
                self._validate_reconcile_thread(role, snapshot["thread"])
                interrupted_turn = self._find_turn(
                    snapshot["thread"], invocation.operation_id
                )
                if (
                    interrupted_turn is None
                    or interrupted_turn.get("status") != "interrupted"
                ):
                    raise AppServerProtocolError(
                        "turn/interrupt acknowledgement lacked terminal interrupted proof"
                    )
            except (AppServerError, RuntimeInvariantError) as reconcile_error:
                state.failure = RuntimeInvocationError(
                    "provider_timeout_requires_reconcile",
                    str(reconcile_error),
                    partial_output=state.partial_output,
                    retryable=False,
                )
                raise state.failure from reconcile_error
            state.interrupted = True
            self._active_by_thread.pop(invocation.session.session_id, None)
            state.failure = RuntimeInvocationError(
                "provider_timeout",
                f"provider turn {invocation.operation_id!r} timed out and was interrupted",
                partial_output=state.partial_output,
                retryable=True,
            )
            raise state.failure from exc
        if self._router_error is not None:
            state.failure = RuntimeInvocationError(
                "provider_protocol_error",
                str(self._router_error),
                partial_output=state.partial_output,
                retryable=False,
            )
            raise state.failure
        turn = state.terminal_turn
        if turn is None:
            state.failure = RuntimeInvocationError(
                "unknown_provider_state",
                "provider event stream ended without a terminal turn",
                partial_output=state.partial_output,
                retryable=True,
            )
            raise state.failure
        status = turn.get("status")
        self._active_by_thread.pop(invocation.session.session_id, None)
        try:
            self._audit_terminal_items(role, turn)
        except ValueError as exc:
            state.failure = RuntimeInvocationError(
                "invalid_model_output",
                str(exc),
                partial_output=state.partial_output,
                retryable=False,
            )
            raise state.failure from exc
        if status == "interrupted":
            state.failure = RuntimeInvocationError(
                "provider_interrupted",
                "provider turn was interrupted",
                partial_output=state.partial_output,
                retryable=True,
            )
            raise state.failure
        if status == "failed":
            state.failure = RuntimeInvocationError(
                "provider_failed",
                self._turn_error_message(turn),
                partial_output=state.partial_output,
                retryable=True,
            )
            raise state.failure
        if status != "completed":
            state.failure = RuntimeInvocationError(
                "unknown_provider_state",
                f"unexpected terminal status {status!r}",
                partial_output=state.partial_output,
                retryable=False,
            )
            raise state.failure
        try:
            state.output = self._output_from_live_turn(
                role,
                turn,
                state.usage,
                request=state.request,
            )
        except (AppServerProtocolError, ValueError) as exc:
            state.failure = RuntimeInvocationError(
                "invalid_model_output",
                str(exc),
                partial_output=state.partial_output,
                retryable=True,
            )
            raise state.failure from exc
        return state.output

    @staticmethod
    def _validate_invocation(
        state: _OperationState,
        invocation: RuntimeInvocation,
        role: ModelRole,
    ) -> None:
        if state.invocation != invocation:
            raise RuntimeInvariantError("provider invocation handle mismatch")
        if invocation.role is not role:
            raise RuntimeInvariantError("provider invocation role mismatch")

    def _output_from_live_turn(
        self,
        role: ModelRole,
        turn: Mapping[str, Any],
        usage: Mapping[str, Any],
        *,
        request: _Request | None = None,
    ) -> RuntimeOutput:
        self._audit_terminal_items(role, turn)
        final = extract_single_final_agent_message(turn)
        payload = self._parse_json_object(final["text"])
        # Typeset-layer turns reuse the Writer/Checker thread configuration, so
        # the request, not the role, decides how their output is read.
        if isinstance(request, FormulaRepairRequest):
            return self._parse_formula_repair_output(
                payload, usage, request, raw=final["text"]
            )
        if isinstance(request, FormulaEquivalenceRequest):
            return self._parse_formula_review_output(
                payload, usage, request, raw=final["text"]
            )
        if role is ModelRole.WRITER:
            return self._parse_writer_output(payload, usage)
        if role is ModelRole.CHECKER:
            return replace(
                self._parse_check_output(
                    payload,
                    usage,
                    request=request if isinstance(request, CheckRequest) else None,
                ),
                raw_output=final["text"],
            )
        if role is ModelRole.JUDGE:
            return self._parse_judge_output(payload, usage)
        raise RuntimeInvariantError(f"unsupported runtime role {role!r}")

    def _writer_output_from_turn(
        self,
        turn: Mapping[str, Any],
        usage: Mapping[str, Any] | None = None,
    ) -> WriterOutput:
        self._audit_terminal_items(ModelRole.WRITER, turn)
        final = extract_single_final_agent_message(turn)
        payload = self._parse_json_object(final["text"])
        return self._parse_writer_output(payload, usage or {})

    @staticmethod
    def _audit_terminal_items(role: ModelRole, turn: Mapping[str, Any]) -> None:
        items = turn.get("items")
        if not isinstance(items, list):
            raise ValueError("terminal turn lacks an items array")  # noqa: TRY004
        for index, item in enumerate(items):
            if not isinstance(item, Mapping):
                raise ValueError(  # noqa: TRY004
                    f"terminal turn item {index} is malformed"
                )
            item_type = item.get("type")
            if not isinstance(item_type, str) or not item_type:
                raise ValueError(f"{role.value} turn item {index} has no valid type")

    @staticmethod
    def _parse_json_object(text: str) -> JsonObject:
        def object_pairs(pairs: list[tuple[str, Any]]) -> JsonObject:
            result: JsonObject = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError(f"duplicate JSON field {key!r}")
                result[key] = value
            return result

        try:
            parsed = json.loads(
                text,
                object_pairs_hook=object_pairs,
                parse_constant=lambda value: (_ for _ in ()).throw(
                    ValueError(f"non-finite JSON number {value}")
                ),
            )
        except (json.JSONDecodeError, ValueError) as exc:
            raise ValueError(f"final agent message is not strict JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise ValueError(  # noqa: TRY004
                "final agent message must be a JSON object"
            )
        return parsed

    @staticmethod
    def _parse_step_content(payload: Mapping[str, Any]) -> StepContent:
        fields = {"claim", "why", "source", "derivation", "scope"}
        if set(payload) != fields:
            raise ValueError("writer final JSON must contain exactly five step fields")
        if any(
            not isinstance(payload[field], str) or not payload[field].strip()
            for field in fields
        ):
            raise ValueError("all five writer step fields must be non-empty text")
        return StepContent(
            **{
                field: _restore_json_escaped_tex_controls(payload[field])
                for field in fields
            }
        )

    @classmethod
    def _parse_writer_output(
        cls, payload: Mapping[str, Any], usage: Mapping[str, Any]
    ) -> WriterOutput:
        if set(payload) != {"content", "control"}:
            raise ValueError(
                "writer final JSON must contain exactly content and control"
            )
        content_raw = payload.get("content")
        control_raw = payload.get("control")
        if not isinstance(content_raw, Mapping):
            raise ValueError("writer content must be an object")  # noqa: TRY004
        content = cls._parse_step_content(content_raw)
        control = cls._parse_writer_control(control_raw)
        return WriterOutput(
            content=content,
            control=control,
            finish_reason="stop",
            usage=Usage(copy.deepcopy(dict(usage))),
        )

    @staticmethod
    def _parse_writer_control(arguments: Any) -> WriterControl:
        if not isinstance(arguments, Mapping):
            raise ValueError(  # noqa: TRY004
                "writer control arguments must be an object"
            )
        if not {"decision", "alternatives"} <= set(arguments) or set(arguments) - {
            "decision",
            "alternatives",
            "revise_step_revision_id",
            "reason",
        }:
            raise ValueError("writer control has missing or extra fields")
        decision_raw = arguments.get("decision")
        alternatives_raw = arguments.get("alternatives")
        try:
            decision = WriterDecision(decision_raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"unknown writer control decision {decision_raw!r}"
            ) from exc
        if not isinstance(alternatives_raw, list) or any(
            not isinstance(item, str) or not item.strip() for item in alternatives_raw
        ):
            raise ValueError("writer control alternatives must be non-empty strings")
        if decision is WriterDecision.FORK and not alternatives_raw:
            raise ValueError("fork writer control requires alternatives")
        if decision is not WriterDecision.FORK and alternatives_raw:
            raise ValueError("writer control alternatives are permitted only for fork")
        alternatives = tuple(BranchAlternative(item) for item in alternatives_raw)
        revision_id, reason = (
            arguments.get("revise_step_revision_id"),
            arguments.get("reason"),
        )
        for field_name, value in (
            ("revise_step_revision_id", revision_id),
            ("reason", reason),
        ):
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(
                    f"writer control {field_name} must be non-empty text or null"
                )
        if decision.value == "revise" and (revision_id is None or reason is None):
            raise ValueError("revise writer control requires a revision id and reason")
        if decision.value != "revise" and revision_id is not None:
            raise ValueError("revision id is permitted only for revise")
        return WriterControl(
            decision=decision,
            alternatives=alternatives,
            revise_step_revision_id=revision_id,
            reason=reason,
        )

    def _parse_check_output(
        self,
        payload: Mapping[str, Any],
        usage: Mapping[str, Any],
        *,
        request: CheckRequest | None = None,
    ) -> CheckOutput:
        if set(payload) != {"verdict", "reason", "evidence"}:
            raise ValueError("checker JSON has missing or extra fields")
        verdict = payload.get("verdict")
        if verdict not in {"ok", "objection", "hard_defect", "instrument_failure"}:
            raise ValueError("checker verdict is invalid")
        reason = payload.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("checker reason must be non-empty text")
        evidence_raw = payload.get("evidence")
        if not isinstance(evidence_raw, list):
            raise ValueError("checker evidence must be an array")  # noqa: TRY004
        allowed_kinds = {
            "ancestor_quote",
            "hypothesis_quote",
            "scope_quote",
            "task_constraint_quote",
        }
        allowed_pairs: set[tuple[str, str]] | None = None
        literature_ids: set[str] = set()
        if self.config.record_version == "1.1":
            if request is not None:
                # Use the same request catalog which constrained this turn's
                # schema, and retain the kind/ID pairing rather than accepting
                # the schema's Cartesian product of separately listed enums.
                schema = checker_output_schema(request)
                allowed_kinds &= set(
                    schema["properties"]["evidence"]["items"]["properties"]["kind"][
                        "enum"
                    ]
                )
                allowed_pairs = {
                    (source.kind, source.source_id)
                    for source in request.evidence_sources
                }
                literature_ids = {
                    source.source_id
                    for source in request.evidence_sources
                    if source.kind == "literature_quote"
                }
            elif self._source_library is not None:
                # Reconciled provider turns can outlive their in-memory request.
                # The run's frozen library remains the authority for literature.
                literature_ids = {
                    source.source_id
                    for source in self._source_library.evidence_sources()
                }
            if literature_ids:
                allowed_kinds.add("literature_quote")
        evidence: list[CheckEvidence] = []
        for item in evidence_raw:
            if not isinstance(item, Mapping) or set(item) != {
                "kind",
                "source_id",
                "quote",
            }:
                raise ValueError("checker evidence item is malformed")
            if item.get("kind") not in allowed_kinds:
                raise ValueError("checker evidence kind is not allowlisted")
            if any(
                not isinstance(item.get(field), str) or not item[field].strip()
                for field in ("source_id", "quote")
            ):
                raise ValueError("checker evidence source and quote must be text")
            if (
                allowed_pairs is not None
                and (item["kind"], item["source_id"]) not in allowed_pairs
            ):
                raise ValueError(
                    "checker evidence kind/source is outside the request catalog"
                )
            if (
                item["kind"] == "literature_quote"
                and item["source_id"] not in literature_ids
            ):
                raise ValueError(
                    "checker literature evidence is outside the frozen sources"
                )
            evidence.append(
                CheckEvidence(
                    kind=item["kind"],
                    source_id=item["source_id"],
                    quote=item["quote"],
                )
            )
        if verdict == "hard_defect" and not evidence:
            raise ValueError("hard_defect requires direct quoted evidence")
        return CheckOutput(
            verdict=verdict,
            reason=reason,
            evidence=tuple(evidence),
            finish_reason="stop",
            usage=Usage(copy.deepcopy(dict(usage))),
        )

    @staticmethod
    def _parse_formula_repair_output(
        payload: Mapping[str, Any],
        usage: Mapping[str, Any],
        request: FormulaRepairRequest,
        *,
        raw: str,
    ) -> FormulaRepairOutput:
        if set(payload) != {"corrections"}:
            raise ValueError("formula repair JSON has missing or extra fields")
        items = payload["corrections"]
        if not isinstance(items, list):
            raise ValueError("formula repair corrections must be an array")  # noqa: TRY004
        corrections: list[tuple[str, str]] = []
        seen: set[str] = set()
        for item in items:
            if not isinstance(item, Mapping) or set(item) != {"formula_id", "latex"}:
                raise ValueError("formula repair correction is malformed")
            formula_id = item["formula_id"]
            latex = item["latex"]
            if not isinstance(formula_id, str) or formula_id in seen:
                raise ValueError("formula repair ids must be unique request ids")
            if not isinstance(latex, str) or not latex.strip():
                raise ValueError("formula repair latex must be non-empty text")
            seen.add(formula_id)
            corrections.append(
                (formula_id, _restore_json_escaped_tex_controls(latex))
            )
        if seen != set(request.formula_ids):
            raise ValueError("formula repair must answer exactly the requested ids")
        return FormulaRepairOutput(
            corrections=tuple(corrections),
            finish_reason="stop",
            usage=Usage(copy.deepcopy(dict(usage))),
            raw_output=raw,
        )

    @staticmethod
    def _parse_formula_review_output(
        payload: Mapping[str, Any],
        usage: Mapping[str, Any],
        request: FormulaEquivalenceRequest,
        *,
        raw: str,
    ) -> FormulaEquivalenceOutput:
        if set(payload) != {"reviews"}:
            raise ValueError("formula review JSON has missing or extra fields")
        items = payload["reviews"]
        if not isinstance(items, list):
            raise ValueError("formula review reviews must be an array")  # noqa: TRY004
        verdicts: list[tuple[str, str, str]] = []
        seen: set[str] = set()
        for item in items:
            if not isinstance(item, Mapping) or set(item) != {
                "formula_id",
                "verdict",
                "reason",
            }:
                raise ValueError("formula review item is malformed")
            formula_id = item["formula_id"]
            verdict = item["verdict"]
            reason = item["reason"]
            if not isinstance(formula_id, str) or formula_id in seen:
                raise ValueError("formula review ids must be unique request ids")
            if verdict not in {"equivalent", "not_equivalent"}:
                raise ValueError("formula review verdict is invalid")
            if not isinstance(reason, str) or not reason.strip():
                raise ValueError("formula review reason must be non-empty text")
            seen.add(formula_id)
            verdicts.append((formula_id, verdict, reason))
        if seen != {item.formula_id for item in request.items}:
            raise ValueError("formula review must answer exactly the requested ids")
        return FormulaEquivalenceOutput(
            verdicts=tuple(verdicts),
            finish_reason="stop",
            usage=Usage(copy.deepcopy(dict(usage))),
            raw_output=raw,
        )

    @staticmethod
    def _parse_judge_output(
        payload: Mapping[str, Any], usage: Mapping[str, Any]
    ) -> JudgeOutput:
        if set(payload) != {"verdict", "reason", "score"}:
            raise ValueError("judge JSON has missing or extra fields")
        verdict = payload.get("verdict")
        if verdict not in {"pass", "near_pass", "fail"}:
            raise ValueError("judge verdict is invalid")
        reason = payload.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("judge reason must be non-empty text")
        score = payload.get("score")
        if score is not None and (
            isinstance(score, bool)
            or not isinstance(score, (int, float))
            or not math.isfinite(float(score))
        ):
            raise ValueError("judge score must be a finite number or null")
        return JudgeOutput(
            verdict=verdict,
            reason=reason,
            score=None if score is None else float(score),
            finish_reason="stop",
            usage=Usage(copy.deepcopy(dict(usage))),
        )

    def _require_run(self, run_id: str) -> None:
        if run_id != self.config.run_id:
            raise RuntimeInvariantError("runtime request belongs to another run")

    def _require_intent_ledger_first(self, requested: bool) -> None:
        """Refuse a request whose obligation differs from the frozen run input.

        The developer text comes from the run configuration while the rendered
        user prompt comes from the request.  If those two ever disagreed the run
        would silently mix two configurations, so the disagreement is an
        invariant failure rather than a precedence rule.
        """

        if requested != self.config.intent_ledger_first:
            raise RuntimeInvariantError(
                "request intent-ledger obligation differs from the frozen run "
                "configuration"
            )

    @property
    def _fork_available(self) -> bool:
        """Whether this run can hold the child branch a fork would need."""

        return self.config.max_active_branches != 1

    def _require_fork_availability(self, requested: bool) -> None:
        """Refuse a request that disagrees with the frozen branch cap.

        The developer text and the output schema are rendered from two different
        objects - the run configuration and the request - so a disagreement would
        offer the Writer a decision the run cannot honour, or withhold one it can.
        """

        if requested != self._fork_available:
            raise RuntimeInvariantError(
                "request fork availability differs from the frozen run configuration"
            )

    def _require_dimension_check(self, requested: bool) -> None:
        """Refuse a request whose obligation differs from the frozen run input.

        Same reasoning as the obligation above: the developer text comes from
        the run configuration and the rendered user prompt from the request, so
        a disagreement would silently mix two configurations.
        """

        if requested != self.config.dimension_check:
            raise RuntimeInvariantError(
                "request dimensional-closure obligation differs from the frozen "
                "run configuration"
            )

    @staticmethod
    def _find_turn(thread: Mapping[str, Any], operation_id: str) -> JsonObject | None:
        turns = thread.get("turns")
        if not isinstance(turns, list):
            raise RuntimeInvariantError("provider thread has no turns array")
        matches = [
            turn
            for turn in turns
            if isinstance(turn, dict) and turn.get("id") == operation_id
        ]
        if len(matches) > 1:
            raise RuntimeInvariantError("provider thread contains duplicate turn ids")
        return None if not matches else copy.deepcopy(matches[0])

    @staticmethod
    def _partial_from_turn(turn: Mapping[str, Any]) -> str:
        items = turn.get("items")
        if not isinstance(items, list):
            return ""
        return "".join(
            item.get("text", "")
            for item in items
            if isinstance(item, Mapping)
            and item.get("type") == "agentMessage"
            and isinstance(item.get("text"), str)
        )

    @staticmethod
    def _turn_error_message(turn: Mapping[str, Any]) -> str:
        error = turn.get("error")
        if isinstance(error, Mapping):
            message = error.get("message")
            if isinstance(message, str) and message.strip():
                return message
        return "provider turn failed"

    @staticmethod
    def _reconcile_completed(output: RuntimeOutput) -> ReconcileResult:
        return ReconcileResult(
            status=ReconcileStatus.COMPLETED,
            output=output,
            partial_output="",
            failure_kind=None,
            message=None,
            retryable=False,
        )

    @staticmethod
    def _reconcile_failure(error: RuntimeInvocationError) -> ReconcileResult:
        status = (
            ReconcileStatus.INTERRUPTED
            if error.failure_kind == "provider_interrupted"
            else ReconcileStatus.FAILED
        )
        return ReconcileResult(
            status=status,
            output=None,
            partial_output=error.partial_output,
            failure_kind=error.failure_kind,
            message=str(error),
            retryable=error.retryable,
        )

    @staticmethod
    def _client_failure(
        error: AppServerError, *, partial_output: str = ""
    ) -> RuntimeInvocationError:
        protocol_failure = isinstance(error, AppServerProtocolError)
        return RuntimeInvocationError(
            ("provider_protocol_error" if protocol_failure else "provider_error"),
            str(error),
            partial_output=partial_output,
            retryable=not protocol_failure,
        )


__all__ = ["CodexAppServerRuntime", "LaunchSettings"]
