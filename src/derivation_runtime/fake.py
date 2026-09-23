"""Deterministic provider-neutral runtime used for orchestration tests."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from .types import (
    CheckOutput,
    CheckRequest,
    FormulaEquivalenceOutput,
    FormulaEquivalenceRequest,
    FormulaRepairOutput,
    FormulaRepairRequest,
    JudgeOutput,
    JudgeRequest,
    ModelRole,
    ProviderLineage,
    ReconcileResult,
    ReconcileStatus,
    RuntimeInterruption,
    RuntimeInvariantError,
    RuntimeInvocation,
    RuntimeInvocationError,
    RuntimeOutput,
    RuntimeSession,
    StepSnapshot,
    WriterOutput,
    WriterRequest,
)


@dataclass(frozen=True)
class FakeFailure:
    failure_kind: str
    message: str
    partial_output: str
    retryable: bool


_FakeRequest = (
    WriterRequest
    | CheckRequest
    | JudgeRequest
    | FormulaRepairRequest
    | FormulaEquivalenceRequest
)


@dataclass
class _InvocationState:
    invocation: RuntimeInvocation
    request: _FakeRequest
    outcome: RuntimeOutput | FakeFailure
    state: str


class DeterministicFakeRuntime:
    """Scripted runtime with inspectable session reuse and fork history."""

    def __init__(
        self,
        *,
        writer_outputs: Mapping[tuple[str, int], WriterOutput | FakeFailure],
        check_factory: Callable[[CheckRequest], CheckOutput | FakeFailure],
        judge_factory: Callable[[JudgeRequest], JudgeOutput | FakeFailure],
        formula_repair_factory: Callable[
            [FormulaRepairRequest], FormulaRepairOutput | FakeFailure
        ]
        | None = None,
        formula_review_factory: Callable[
            [FormulaEquivalenceRequest], FormulaEquivalenceOutput | FakeFailure
        ]
        | None = None,
    ) -> None:
        self.writer_outputs = dict(writer_outputs)
        self.check_factory = check_factory
        self.judge_factory = judge_factory
        # Typeset-layer calls: a runtime without a script refuses them the way a
        # provider without that capability would, instead of inventing a repair.
        self.formula_repair_factory = formula_repair_factory
        self.formula_review_factory = formula_review_factory
        self.formula_repair_requests: list[FormulaRepairRequest] = []
        self.formula_review_requests: list[FormulaEquivalenceRequest] = []
        self._session_counter = 0
        self._operation_counter = 0
        self._invocations: dict[str, _InvocationState] = {}
        self.writer_sessions_by_branch: dict[str, list[str]] = {}
        self.branch_writer_sessions: dict[str, str] = {}
        self.forks: list[tuple[str, str, str]] = []
        self.rehydrations: list[tuple[str, tuple[str, ...]]] = []
        self.rebinds: list[tuple[str, str]] = []

    def _new_session(self, lineage: ProviderLineage) -> RuntimeSession:
        self._session_counter += 1
        return RuntimeSession(f"fake_session_{self._session_counter:04d}", lineage)

    def _new_invocation(
        self,
        role: ModelRole,
        session: RuntimeSession,
        request: _FakeRequest,
        outcome: RuntimeOutput | FakeFailure,
    ) -> RuntimeInvocation:
        self._operation_counter += 1
        invocation = RuntimeInvocation(
            session=session,
            operation_id=f"fake_operation_{self._operation_counter:04d}",
            role=role,
        )
        self._invocations[invocation.operation_id] = _InvocationState(
            invocation=invocation,
            request=request,
            outcome=outcome,
            state="running",
        )
        return invocation

    async def start_writer(
        self, request: WriterRequest, session: RuntimeSession | None
    ) -> RuntimeInvocation:
        outcome = self.writer_outputs.get((request.branch_id, request.step_slot))
        if outcome is None:
            raise RuntimeInvariantError(
                f"fake writer has no output for {(request.branch_id, request.step_slot)!r}"
            )
        actual_session = session or self._new_session(ProviderLineage.NATIVE)
        history = self.writer_sessions_by_branch.setdefault(request.branch_id, [])
        bound = self.branch_writer_sessions.get(request.branch_id)
        if bound is not None and bound != actual_session.session_id:
            # The provider runtime binds a Branch to one writer session and
            # raises this exact invariant when a turn arrives on another one.
            # A rotated or restart-rehydrated Branch is legitimate, but only
            # once the move has been announced, so the fake refuses the same
            # unannounced switch the real App Server runtime refuses.
            raise RuntimeInvariantError(
                "writer session differs from the Branch binding"
            )
        if history and history[-1] != actual_session.session_id:
            rehydrated_prefix = next(
                (
                    prefix
                    for session_id, prefix in self.rehydrations
                    if session_id == actual_session.session_id
                ),
                None,
            )
            request_prefix = tuple(item.step_revision_id for item in request.transcript)
            if (
                actual_session.lineage is not ProviderLineage.REHYDRATED
                or rehydrated_prefix is None
                or rehydrated_prefix != request_prefix
            ):
                raise RuntimeInvariantError(
                    "same writer branch attempted to switch runtime session"
                )
        history.append(actual_session.session_id)
        self.branch_writer_sessions[request.branch_id] = actual_session.session_id
        return self._new_invocation(ModelRole.WRITER, actual_session, request, outcome)

    async def collect_writer(self, invocation: RuntimeInvocation) -> WriterOutput:
        outcome = self._collect(invocation, ModelRole.WRITER)
        if not isinstance(outcome, WriterOutput):
            raise RuntimeInvariantError("fake writer produced a non-writer output")
        return outcome

    async def start_checker(self, request: CheckRequest) -> RuntimeInvocation:
        return self._new_invocation(
            ModelRole.CHECKER,
            self._new_session(ProviderLineage.NATIVE),
            request,
            self.check_factory(request),
        )

    async def collect_checker(self, invocation: RuntimeInvocation) -> CheckOutput:
        outcome = self._collect(invocation, ModelRole.CHECKER)
        if not isinstance(outcome, CheckOutput):
            raise RuntimeInvariantError("fake checker produced a non-checker output")
        return outcome

    async def start_judge(self, request: JudgeRequest) -> RuntimeInvocation:
        return self._new_invocation(
            ModelRole.JUDGE,
            self._new_session(ProviderLineage.NATIVE),
            request,
            self.judge_factory(request),
        )

    async def collect_judge(self, invocation: RuntimeInvocation) -> JudgeOutput:
        outcome = self._collect(invocation, ModelRole.JUDGE)
        if not isinstance(outcome, JudgeOutput):
            raise RuntimeInvariantError("fake judge produced a non-judge output")
        return outcome

    async def start_formula_repair(
        self, request: FormulaRepairRequest
    ) -> RuntimeInvocation:
        if self.formula_repair_factory is None:
            raise RuntimeInvocationError(
                "unsupported_request",
                "fake runtime has no formula repair script",
                partial_output="",
                retryable=False,
            )
        self.formula_repair_requests.append(request)
        return self._new_invocation(
            ModelRole.WRITER,
            self._new_session(ProviderLineage.NATIVE),
            request,
            self.formula_repair_factory(request),
        )

    async def collect_formula_repair(
        self, invocation: RuntimeInvocation
    ) -> FormulaRepairOutput:
        outcome = self._collect(invocation, ModelRole.WRITER)
        if not isinstance(outcome, FormulaRepairOutput):
            raise RuntimeInvariantError("fake formula repair produced another output")
        return outcome

    async def start_formula_review(
        self, request: FormulaEquivalenceRequest
    ) -> RuntimeInvocation:
        if self.formula_review_factory is None:
            raise RuntimeInvocationError(
                "unsupported_request",
                "fake runtime has no formula review script",
                partial_output="",
                retryable=False,
            )
        self.formula_review_requests.append(request)
        return self._new_invocation(
            ModelRole.CHECKER,
            self._new_session(ProviderLineage.NATIVE),
            request,
            self.formula_review_factory(request),
        )

    async def collect_formula_review(
        self, invocation: RuntimeInvocation
    ) -> FormulaEquivalenceOutput:
        outcome = self._collect(invocation, ModelRole.CHECKER)
        if not isinstance(outcome, FormulaEquivalenceOutput):
            raise RuntimeInvariantError("fake formula review produced another output")
        return outcome

    def _collect(self, invocation: RuntimeInvocation, role: ModelRole) -> RuntimeOutput:
        state = self._invocations.get(invocation.operation_id)
        if state is None or state.invocation != invocation:
            raise RuntimeInvariantError("unknown fake invocation")
        if invocation.role is not role:
            raise RuntimeInvariantError("invocation role mismatch")
        if state.state == "interrupted":
            raise RuntimeInvocationError(
                "provider_interrupted",
                "fake invocation was interrupted",
                partial_output="",
                retryable=True,
            )
        if state.state == "completed":
            if isinstance(state.outcome, FakeFailure):
                raise RuntimeInvariantError(
                    "failed fake invocation cannot be collected as completed"
                )
            return state.outcome
        if isinstance(state.outcome, FakeFailure):
            state.state = "failed"
            raise RuntimeInvocationError(
                state.outcome.failure_kind,
                state.outcome.message,
                partial_output=state.outcome.partial_output,
                retryable=state.outcome.retryable,
            )
        state.state = "completed"
        return state.outcome

    async def fork(
        self, session: RuntimeSession, completed_operation_id: str
    ) -> RuntimeSession:
        state = self._invocations.get(completed_operation_id)
        if state is None:
            raise RuntimeInvariantError("cannot fork an unknown operation")
        if state.invocation.role is not ModelRole.WRITER:
            raise RuntimeInvariantError("fork anchor must be a writer operation")
        if state.invocation.session.session_id != session.session_id:
            raise RuntimeInvariantError(
                "fork session does not own the anchor operation"
            )
        if state.state != "completed":
            raise RuntimeInvariantError("fork anchor operation is not completed")
        child = self._new_session(ProviderLineage.FORKED)
        self.forks.append(
            (session.session_id, completed_operation_id, child.session_id)
        )
        return child

    def rebind_branch_writer_session(
        self, branch_id: str, session: RuntimeSession
    ) -> None:
        previous = self.branch_writer_sessions.get(branch_id)
        if previous == session.session_id:
            return
        owner_branch = next(
            (
                other
                for other, bound in self.branch_writer_sessions.items()
                if bound == session.session_id
            ),
            None,
        )
        if owner_branch is not None:
            raise RuntimeInvariantError(
                "rebound Branch session is already owned by another Branch"
            )
        self.rebinds.append((branch_id, session.session_id))
        self.branch_writer_sessions[branch_id] = session.session_id

    async def rehydrate(self, transcript: Sequence[StepSnapshot]) -> RuntimeSession:
        session = self._new_session(ProviderLineage.REHYDRATED)
        self.rehydrations.append(
            (session.session_id, tuple(item.step_revision_id for item in transcript))
        )
        return session

    async def interrupt(self, invocation: RuntimeInvocation) -> RuntimeInterruption:
        state = self._invocations.get(invocation.operation_id)
        if state is None:
            raise RuntimeInvariantError("cannot interrupt an unknown invocation")
        if state.state != "running":
            raise RuntimeInvariantError("only a running invocation can be interrupted")
        state.state = "interrupted"
        partial = (
            state.outcome.partial_output
            if isinstance(state.outcome, FakeFailure)
            else ""
        )
        return RuntimeInterruption(partial_output=partial)

    async def reconcile(self, invocation: RuntimeInvocation) -> ReconcileResult:
        state = self._invocations.get(invocation.operation_id)
        if state is None:
            return ReconcileResult(
                status=ReconcileStatus.MISSING,
                output=None,
                partial_output="",
                failure_kind="unknown_provider_state",
                message="fake provider no longer has the operation",
                retryable=True,
            )
        if state.state == "running":
            return ReconcileResult(
                status=ReconcileStatus.RUNNING,
                output=None,
                partial_output="",
                failure_kind=None,
                message=None,
                retryable=False,
            )
        if state.state == "completed":
            if isinstance(state.outcome, FakeFailure):
                raise RuntimeInvariantError("completed fake state contains a failure")
            return ReconcileResult(
                status=ReconcileStatus.COMPLETED,
                output=state.outcome,
                partial_output="",
                failure_kind=None,
                message=None,
                retryable=False,
            )
        if state.state == "interrupted":
            return ReconcileResult(
                status=ReconcileStatus.INTERRUPTED,
                output=None,
                partial_output="",
                failure_kind="provider_interrupted",
                message="fake operation was interrupted",
                retryable=True,
            )
        failure = state.outcome
        if not isinstance(failure, FakeFailure):
            raise RuntimeInvariantError(
                "failed fake state contains a successful output"
            )
        return ReconcileResult(
            status=ReconcileStatus.FAILED,
            output=None,
            partial_output=failure.partial_output,
            failure_kind=failure.failure_kind,
            message=failure.message,
            retryable=failure.retryable,
        )

    def complete_without_collect(self, operation_id: str) -> None:
        state = self._invocations[operation_id]
        if isinstance(state.outcome, FakeFailure):
            state.state = "failed"
        else:
            state.state = "completed"

    def forget_operation(self, operation_id: str) -> None:
        self._invocations.pop(operation_id, None)


__all__ = ["DeterministicFakeRuntime", "FakeFailure"]
