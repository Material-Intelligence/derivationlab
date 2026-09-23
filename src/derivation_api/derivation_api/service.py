"""Provider-neutral seam between HTTP transport and derivation runtime."""

from __future__ import annotations

from collections.abc import AsyncIterator
from enum import StrEnum
from typing import Protocol, runtime_checkable

from .models import (
    AccountRateLimitsView,
    AccountView,
    BuildInfoView,
    CapabilitiesView,
    CreateBranchRequest,
    CreateIntakeSessionRequest,
    CreateRunRequest,
    DeviceLoginCancelView,
    DeviceLoginStartView,
    DeviceLoginStatusView,
    ExportReportRequest,
    FinalizeIntakeSessionRequest,
    HealthView,
    ImportExistingAccountRequest,
    IntakeRevisionRequest,
    IntakeSessionStatusValue,
    IntakeSessionView,
    ProblemPresetsView,
    QuitReadinessView,
    ReportBundleView,
    RunEvent,
    RunSummary,
    RunView,
    SubmitIntakeRoundRequest,
)


class ErrorKind(StrEnum):
    NOT_FOUND = "not_found"
    INVALID_STATE = "invalid_state"
    CONFLICT = "conflict"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    UNAVAILABLE = "unavailable"


class DerivationServiceError(RuntimeError):
    """Expected service-layer failure safe to map into a structured response."""

    def __init__(
        self,
        kind: ErrorKind,
        code: str,
        message: str,
        *,
        details: object | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.code = code
        self.message = message
        self.details = details


@runtime_checkable
class DerivationService(Protocol):
    """Small integration boundary implemented by fake and real runtimes.

    Command methods own all state validation and idempotency.  HTTP handlers do
    not mutate scientific state or infer branch or control-command eligibility.
    In particular, ``interrupt_run`` must consult the runtime's authoritative
    in-flight call state instead of inferring eligibility from ``RunView.phase``.
    Event streams
    must use bounded subscriber buffers and release them when the iterator is
    closed.  For ``human_revision``, implementations must record
    ``command.canonical_instruction()`` as direct human content; they must not
    obtain replacement prose from a model.
    """

    async def start(self) -> None: ...

    async def close(self) -> None: ...

    async def health(self) -> HealthView: ...

    async def build_info(self) -> BuildInfoView: ...

    async def get_problem_presets(self) -> ProblemPresetsView: ...

    async def quit_readiness(self) -> QuitReadinessView: ...

    async def account(self) -> AccountView: ...

    async def account_rate_limits(self) -> AccountRateLimitsView: ...

    async def import_existing_account(
        self,
        command: ImportExistingAccountRequest,
        *,
        idempotency_key: str | None,
    ) -> AccountView: ...

    async def start_device_login(self) -> DeviceLoginStartView: ...

    async def get_device_login(self, login_id: str) -> DeviceLoginStatusView: ...

    async def cancel_device_login(self, login_id: str) -> DeviceLoginCancelView: ...

    async def capabilities(self) -> CapabilitiesView: ...

    async def create_intake_session(
        self,
        command: CreateIntakeSessionRequest,
        *,
        idempotency_key: str,
    ) -> IntakeSessionView: ...

    async def get_intake_session(self, session_id: str) -> IntakeSessionView: ...

    async def list_intake_sessions(self, *, status: IntakeSessionStatusValue) -> list[IntakeSessionView]: ...

    async def submit_intake_round(
        self,
        session_id: str,
        command: SubmitIntakeRoundRequest,
        *,
        idempotency_key: str,
    ) -> IntakeSessionView: ...

    async def finalize_intake_session(
        self,
        session_id: str,
        command: FinalizeIntakeSessionRequest,
        *,
        idempotency_key: str,
    ) -> IntakeSessionView: ...

    async def confirm_intake_session(
        self,
        session_id: str,
        command: IntakeRevisionRequest,
        *,
        idempotency_key: str,
    ) -> IntakeSessionView: ...

    async def cancel_intake_session(
        self,
        session_id: str,
        command: IntakeRevisionRequest,
        *,
        idempotency_key: str,
    ) -> IntakeSessionView: ...

    async def create_run(
        self,
        command: CreateRunRequest,
        *,
        idempotency_key: str | None,
    ) -> RunView: ...

    async def get_run(self, run_id: str) -> RunView: ...

    async def list_runs(self) -> list[RunSummary]: ...

    async def export_report(
        self,
        run_id: str,
        command: ExportReportRequest,
    ) -> ReportBundleView: ...

    async def read_report_pdf(self, run_id: str, export_id: str) -> bytes: ...

    def stream_events(
        self,
        run_id: str,
        *,
        after_event_id: int,
        follow: bool,
    ) -> AsyncIterator[RunEvent]: ...

    async def pause_run(self, run_id: str, *, idempotency_key: str | None) -> RunView: ...

    async def resume_run(self, run_id: str, *, idempotency_key: str | None) -> RunView: ...

    async def interrupt_run(self, run_id: str, *, idempotency_key: str | None) -> RunView: ...

    async def create_branch(
        self,
        run_id: str,
        command: CreateBranchRequest,
        *,
        idempotency_key: str | None,
    ) -> RunView: ...
