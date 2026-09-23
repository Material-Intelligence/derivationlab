"""FastAPI application factory.  No concrete runtime is hidden in handlers."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from ipaddress import ip_address
from typing import Annotated, Literal, TypeVar

from fastapi import FastAPI, Header, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from .constants import MAX_EVENT_ID, MAX_EVENT_ID_DIGITS
from .middleware import (
    LocalOriginMiddleware,
    RequestBodyLimitMiddleware,
    RequestIdMiddleware,
    ServerOriginMiddleware,
    ServerSecurityHeadersMiddleware,
    SiteSessionMiddleware,
    canonical_https_origin,
    error_payload,
)
from .models import (
    AccountRateLimitsView,
    AccountView,
    AdminCreateSiteAccountRequest,
    AdminResetSitePasswordRequest,
    AdminSetSiteAccountStatusRequest,
    BuildInfoView,
    CapabilitiesView,
    CreateBranchRequest,
    CreateIntakeSessionRequest,
    CreateRunRequest,
    DeviceLoginCancelView,
    DeviceLoginStartView,
    DeviceLoginStatusView,
    ErrorEnvelope,
    ExportReportRequest,
    FinalizeIntakeSessionRequest,
    HealthView,
    Identifier,
    ImportExistingAccountRequest,
    IntakeRevisionRequest,
    IntakeSessionStatusValue,
    IntakeSessionView,
    ProblemPresetsView,
    QuitReadinessView,
    ReportBundleView,
    RunCommandCapabilities,
    RunEvent,
    RunSummary,
    RunView,
    SiteAccountView,
    SiteLoginRequest,
    SiteModeView,
    SitePasswordChangeRequest,
    SiteSessionView,
    SubmitIntakeRoundRequest,
)
from .service import DerivationService, DerivationServiceError, ErrorKind
from .site_access import (
    AccountConflict,
    AccountLocked,
    AccountNotFound,
    AuthenticatedSession,
    IdentityError,
    InvalidCredentials,
    PasswordPolicyError,
    RequestServiceResolver,
    SessionInvalid,
    SiteAccount,
    SiteAccountStatus,
    SiteIdentityAuthority,
    SitePermissionDenied,
    SiteRole,
    SourceRateLimited,
    TemporaryPasswordExpired,
    TenantContentReader,
)
from .sse import stream_sse

LOCAL_ORIGIN_REGEX = r"https?://(?:localhost|127\.0\.0\.1|\[::1\])(?::\d{1,5})?"
IDEMPOTENCY_KEY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
BASE_ERROR_STATUS_CODES = (400, 403, 413, 500, 503)
VALIDATION_ERROR_STATUS_CODES = (*BASE_ERROR_STATUS_CODES, 422)
RUN_ERROR_STATUS_CODES = (*VALIDATION_ERROR_STATUS_CODES, 404)
COMMAND_ERROR_STATUS_CODES = (*RUN_ERROR_STATUS_CODES, 409)
CREATE_ERROR_STATUS_CODES = (*VALIDATION_ERROR_STATUS_CODES, 409)
INTAKE_ERROR_STATUS_CODES = COMMAND_ERROR_STATUS_CODES
ERROR_DESCRIPTIONS = {
    400: "Malformed request metadata or event cursor.",
    401: "A valid site session is required.",
    403: "The request origin or authenticated action is not allowed.",
    404: "Requested run or sealed StepRevision does not exist.",
    409: "Command is not eligible in the authoritative runtime state or conflicts with idempotency.",
    413: "Request body exceeds the configured local API limit.",
    422: "Path, header, query, or JSON body validation failed.",
    423: "The site account is temporarily locked.",
    429: "The client source has sent too many requests.",
    500: "Unexpected control-plane failure.",
    503: "Injected derivation service is unavailable.",
}
IDEMPOTENCY_RESPONSE_HEADERS = {
    "Idempotency-Key": {
        "description": "Present when the valid request Idempotency-Key is echoed by the response.",
        "schema": {"type": "string"},
    }
}


@dataclass(frozen=True)
class ApiSettings:
    max_body_bytes: int = 1_048_576
    sse_heartbeat_seconds: float = 15.0
    sse_retry_milliseconds: int = 2_000
    server_origins: tuple[str, ...] = ()
    trusted_proxy_hosts: tuple[str, ...] = ()
    session_cookie_name: str = "derivationlab_session"
    site_channel: Literal["development", "preview", "stable", "staging"] = "development"

    def __post_init__(self) -> None:
        if self.max_body_bytes < 1:
            raise ValueError("max_body_bytes must be positive")
        if self.sse_heartbeat_seconds <= 0:
            raise ValueError("sse_heartbeat_seconds must be positive")
        if self.sse_retry_milliseconds < 100:
            raise ValueError("sse_retry_milliseconds must be at least 100")
        if not self.session_cookie_name.isascii() or not self.session_cookie_name.replace("_", "").isalnum():
            raise ValueError("session_cookie_name must be an ASCII token")
        if any(canonical_https_origin(origin) != origin for origin in self.server_origins):
            raise ValueError("server_origins must contain canonical HTTPS origins")
        if not self.server_origins and self.site_channel != "development":
            raise ValueError("non-development site_channel requires server mode")
        for host in self.trusted_proxy_hosts:
            try:
                ip_address(host)
            except ValueError as exc:
                raise ValueError("trusted_proxy_hosts must contain literal IP addresses") from exc


IdempotencyKey = Annotated[
    str | None,
    Header(alias="Idempotency-Key", pattern=IDEMPOTENCY_KEY_PATTERN),
]
RequiredIdempotencyKey = Annotated[
    str,
    Header(alias="Idempotency-Key", pattern=IDEMPOTENCY_KEY_PATTERN),
]
AdminContentResult = TypeVar("AdminContentResult")
LastEventId = Annotated[
    str | None,
    Header(
        alias="Last-Event-ID",
        description=(
            "Exclusive non-negative decimal SSE cursor. When supplied, this header takes precedence "
            "over the after query parameter."
        ),
        json_schema_extra={"pattern": r"^[0-9]+$", "maxLength": MAX_EVENT_ID_DIGITS},
    ),
]


class EventStreamResponse(StreamingResponse):
    """Streaming response class whose OpenAPI media type is SSE, not JSON."""

    media_type = "text/event-stream"


def _error_responses(status_codes: tuple[int, ...]) -> dict[int, dict[str, object]]:
    return {
        status_code: {
            "description": ERROR_DESCRIPTIONS[status_code],
            "content": {
                "application/json": {
                    "schema": {"$ref": "#/components/schemas/ErrorEnvelope"},
                }
            },
        }
        for status_code in status_codes
    }


def _command_responses(
    success_status: int,
    *,
    error_status_codes: tuple[int, ...] = COMMAND_ERROR_STATUS_CODES,
) -> dict[int, dict[str, object]]:
    responses = _error_responses(error_status_codes)
    responses[success_status] = {
        "description": "Command accepted.",
        "headers": IDEMPOTENCY_RESPONSE_HEADERS,
    }
    return responses


def _set_idempotency_echo(response: Response, key: str | None) -> None:
    if key is not None:
        response.headers["Idempotency-Key"] = key


def _cursor(last_event_id: str | None, after: int | None) -> int:
    header = last_event_id
    if header is None:
        return 0 if after is None else after
    if not header.isascii() or not header.isdecimal():
        raise DerivationServiceError(
            ErrorKind.CONFLICT,
            "invalid_event_cursor",
            "Last-Event-ID must be a non-negative decimal integer.",
        )
    cursor = int(header)
    if len(header) > MAX_EVENT_ID_DIGITS or cursor > MAX_EVENT_ID:
        raise DerivationServiceError(
            ErrorKind.CONFLICT,
            "invalid_event_cursor",
            "Last-Event-ID exceeds the supported integer range.",
        )
    return cursor


def _site_account_view(account: SiteAccount) -> SiteAccountView:
    return SiteAccountView(
        user_id=account.user_id,
        username=account.username,
        email=account.email,
        role=account.role.value,
        status=account.status.value,
        must_change_password=account.must_change_password,
    )


def _site_session_view(session: AuthenticatedSession) -> SiteSessionView:
    return SiteSessionView(
        account=_site_account_view(session.account),
        idle_expires_at=session.idle_expires_at,
        absolute_expires_at=session.absolute_expires_at,
    )


def _admin_run_view(run: RunView) -> RunView:
    return run.model_copy(
        deep=True,
        update={
            "read_only": True,
            "commands": RunCommandCapabilities(),
        },
    )


def _admin_run_summary(run: RunSummary) -> RunSummary:
    return run.model_copy(deep=True, update={"read_only": True})


def _request_source(request: Request, *, trusted_proxy_hosts: tuple[str, ...]) -> str:
    host = request.client.host if request.client is not None else "unknown"
    if host in trusted_proxy_hosts:
        forwarded = request.headers.get("x-forwarded-for", "").partition(",")[0].strip()
        with suppress(ValueError):
            host = str(ip_address(forwarded))
    return f"http:{host}"


def create_app(
    service: DerivationService | None,
    *,
    settings: ApiSettings | None = None,
    site_identity: SiteIdentityAuthority | None = None,
    service_resolver: RequestServiceResolver | None = None,
    admin_content_reader: TenantContentReader | None = None,
) -> FastAPI:
    """Build a desktop API or an explicitly authenticated server API."""

    api_settings = settings or ApiSettings()
    server_parts = (site_identity, service_resolver, admin_content_reader)
    server_mode = any(part is not None for part in server_parts)
    if server_mode and any(part is None for part in server_parts):
        raise ValueError("server mode requires site_identity, service_resolver, and admin_content_reader")
    if not server_mode and service is None:
        raise ValueError("desktop mode requires a derivation service")
    if server_mode and not api_settings.server_origins:
        raise ValueError("server mode requires at least one configured HTTPS origin")

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if service is not None:
            await service.start()
        if service_resolver is not None:
            await service_resolver.start()
        try:
            yield
        finally:
            try:
                if service_resolver is not None:
                    await service_resolver.close()
            finally:
                if service is not None:
                    await service.close()

    app = FastAPI(
        title="DerivationLab API",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None if server_mode else "/docs",
        redoc_url=None if server_mode else "/redoc",
        openapi_url=None if server_mode else "/openapi.json",
    )
    app.state.derivation_service = service
    app.state.site_identity = site_identity
    app.state.service_resolver = service_resolver
    app.state.admin_content_reader = admin_content_reader

    async def request_service(request: Request) -> DerivationService:
        if service_resolver is None:
            assert service is not None
            return service
        return await service_resolver.resolve(request)

    app.add_middleware(RequestBodyLimitMiddleware, max_bytes=api_settings.max_body_bytes)
    if server_mode:
        assert site_identity is not None
        app.add_middleware(
            SiteSessionMiddleware,
            identity=site_identity,
            cookie_name=api_settings.session_cookie_name,
        )
        app.add_middleware(
            ServerOriginMiddleware,
            allowed_origins=api_settings.server_origins,
        )
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(api_settings.server_origins),
            allow_credentials=True,
            allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
            allow_headers=[
                "Content-Type",
                "Idempotency-Key",
                "Last-Event-ID",
                "X-Request-ID",
            ],
            expose_headers=["Idempotency-Key", "X-Request-ID"],
        )
        app.add_middleware(ServerSecurityHeadersMiddleware)
    else:
        app.add_middleware(LocalOriginMiddleware)
        app.add_middleware(
            CORSMiddleware,
            allow_origin_regex=LOCAL_ORIGIN_REGEX,
            allow_credentials=False,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=[
                "Content-Type",
                "Idempotency-Key",
                "Last-Event-ID",
                "X-Request-ID",
            ],
            expose_headers=["Idempotency-Key", "X-Request-ID"],
        )
    app.add_middleware(RequestIdMiddleware)

    @app.exception_handler(DerivationServiceError)
    async def service_error_handler(request: Request, exc: DerivationServiceError) -> JSONResponse:
        status_by_kind = {
            ErrorKind.NOT_FOUND: 404,
            ErrorKind.INVALID_STATE: 409,
            ErrorKind.CONFLICT: 400,
            ErrorKind.IDEMPOTENCY_CONFLICT: 409,
            ErrorKind.UNAVAILABLE: 503,
        }
        return JSONResponse(
            error_payload(request.scope, exc.code, exc.message, exc.details),
            status_code=status_by_kind[exc.kind],
            headers={"Cache-Control": "no-store"},
        )

    @app.exception_handler(IdentityError)
    async def identity_error_handler(request: Request, exc: IdentityError) -> JSONResponse:
        if isinstance(exc, (InvalidCredentials, SessionInvalid)):
            status_code, code = 401, "invalid_credentials"
        elif isinstance(exc, AccountLocked):
            status_code, code = 423, "account_locked"
        elif isinstance(exc, SourceRateLimited):
            status_code, code = 429, "source_rate_limited"
        elif isinstance(exc, (TemporaryPasswordExpired, SitePermissionDenied)):
            status_code, code = 403, "temporary_password_expired"
            if isinstance(exc, SitePermissionDenied):
                code = "site_permission_denied"
        elif isinstance(exc, PasswordPolicyError):
            status_code, code = 422, "password_policy_failed"
        elif isinstance(exc, AccountConflict):
            status_code, code = 409, "site_account_conflict"
        elif isinstance(exc, AccountNotFound):
            status_code, code = 404, "site_account_not_found"
        else:
            status_code, code = 400, "identity_command_rejected"
        return JSONResponse(
            error_payload(request.scope, code, str(exc)),
            status_code=status_code,
            headers={"Cache-Control": "no-store"},
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        details = [
            {"location": list(error["loc"]), "message": error["msg"], "type": error["type"]}
            for error in exc.errors()
        ]
        return JSONResponse(
            error_payload(
                request.scope,
                "validation_failed",
                "Request validation failed.",
                details,
            ),
            status_code=422,
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = "route_not_found" if exc.status_code == 404 else "http_error"
        return JSONResponse(
            error_payload(request.scope, code, str(exc.detail)),
            status_code=exc.status_code,
        )

    @app.exception_handler(Exception)
    async def unexpected_error_handler(request: Request, _: Exception) -> JSONResponse:
        return JSONResponse(
            error_payload(
                request.scope,
                "internal_error",
                "The derivation service encountered an unexpected error.",
            ),
            status_code=500,
        )

    @app.get(
        "/healthz",
        response_model=HealthView,
        responses=_error_responses(BASE_ERROR_STATUS_CODES),
    )
    async def health() -> HealthView:
        if service_resolver is not None:
            return await service_resolver.health()
        assert service is not None
        return await service.health()

    @app.get(
        "/api/build-info",
        response_model=BuildInfoView,
        responses=_error_responses(BASE_ERROR_STATUS_CODES),
    )
    async def build_info() -> BuildInfoView:
        if service_resolver is not None:
            return await service_resolver.build_info()
        assert service is not None
        return await service.build_info()

    @app.get(
        "/api/site/mode",
        response_model=SiteModeView,
        responses=_error_responses(BASE_ERROR_STATUS_CODES),
    )
    async def site_mode() -> SiteModeView:
        """Declare the access mode explicitly so the Web gate can fail closed."""

        return SiteModeView(
            mode="server" if server_mode else "desktop",
            channel=api_settings.site_channel,
        )

    def identity_authority() -> SiteIdentityAuthority:
        if site_identity is None:
            raise StarletteHTTPException(404, "Site authentication is not enabled.")
        return site_identity

    def admin_session(request: Request) -> AuthenticatedSession:
        identity_authority()
        session: AuthenticatedSession = request.state.site_session
        if session.account.role is not SiteRole.ADMIN:
            raise SitePermissionDenied("administrator access is required")
        return session

    async def admin_content_read(
        *,
        user_id: str,
        request: Request,
        content_type: str,
        content_id: str | None,
        operation: Callable[[TenantContentReader], Awaitable[AdminContentResult]],
    ) -> AdminContentResult:
        administrator = admin_session(request)
        identity = identity_authority()
        source = _request_source(
            request,
            trusted_proxy_hosts=api_settings.trusted_proxy_hosts,
        )

        def audit(outcome: str) -> None:
            identity.record_admin_content_access(
                actor_user_id=administrator.account.user_id,
                owner_user_id=user_id,
                content_type=content_type,
                content_id=content_id,
                outcome=outcome,
                source=source,
            )

        try:
            identity.get_account(user_id)
            if admin_content_reader is None:
                raise StarletteHTTPException(404, "Server tenant content access is not enabled.")
            result = await operation(admin_content_reader)
        except AccountNotFound:
            audit("not_found")
            raise
        except DerivationServiceError as exc:
            audit("not_found" if exc.kind is ErrorKind.NOT_FOUND else "failure")
            raise
        except Exception:
            audit("failure")
            raise
        audit("success")
        return result

    @app.post(
        "/api/site/session",
        response_model=SiteSessionView,
        responses=_error_responses((400, 403, 413, 422, 423, 429, 500, 503)),
    )
    async def create_site_session(
        command: SiteLoginRequest,
        request: Request,
        response: Response,
    ) -> SiteSessionView:
        grant = identity_authority().login(
            command.identifier,
            command.password,
            source=_request_source(
                request,
                trusted_proxy_hosts=api_settings.trusted_proxy_hosts,
            ),
        )
        response.set_cookie(
            key=api_settings.session_cookie_name,
            value=grant.token,
            max_age=7 * 24 * 60 * 60,
            secure=True,
            httponly=True,
            samesite="strict",
            path="/",
        )
        response.headers["Cache-Control"] = "no-store"
        return SiteSessionView(
            account=_site_account_view(grant.account),
            idle_expires_at=grant.idle_expires_at,
            absolute_expires_at=grant.absolute_expires_at,
        )

    @app.get(
        "/api/site/session",
        response_model=SiteSessionView,
        responses=_error_responses((400, 401, 403, 413, 500, 503)),
    )
    async def get_site_session(request: Request) -> SiteSessionView:
        identity_authority()
        session: AuthenticatedSession = request.state.site_session
        return _site_session_view(session)

    @app.delete(
        "/api/site/session",
        status_code=204,
        responses=_error_responses((400, 401, 403, 413, 500, 503)),
    )
    async def delete_site_session(request: Request, response: Response) -> None:
        identity = identity_authority()
        session: AuthenticatedSession = request.state.site_session
        identity.logout(
            request.state.site_session_token,
            actor_user_id=session.account.user_id,
            source=_request_source(
                request,
                trusted_proxy_hosts=api_settings.trusted_proxy_hosts,
            ),
        )
        response.delete_cookie(
            api_settings.session_cookie_name,
            path="/",
            secure=True,
            httponly=True,
            samesite="strict",
        )
        response.headers["Cache-Control"] = "no-store"

    @app.post(
        "/api/site/password",
        status_code=204,
        responses=_error_responses((400, 401, 403, 413, 422, 500, 503)),
    )
    async def change_site_password(
        command: SitePasswordChangeRequest,
        request: Request,
        response: Response,
    ) -> None:
        identity = identity_authority()
        session: AuthenticatedSession = request.state.site_session
        identity.change_password(
            session.account.user_id,
            current_password=command.current_password,
            new_password=command.new_password,
            source=_request_source(
                request,
                trusted_proxy_hosts=api_settings.trusted_proxy_hosts,
            ),
        )
        response.delete_cookie(
            api_settings.session_cookie_name,
            path="/",
            secure=True,
            httponly=True,
            samesite="strict",
        )
        response.headers["Cache-Control"] = "no-store"

    @app.get(
        "/api/site/admin/accounts",
        response_model=list[SiteAccountView],
        responses=_error_responses((400, 401, 403, 413, 500, 503)),
    )
    async def list_site_accounts(request: Request) -> list[SiteAccountView]:
        admin_session(request)
        return [_site_account_view(account) for account in identity_authority().list_accounts()]

    @app.post(
        "/api/site/admin/accounts",
        response_model=SiteAccountView,
        status_code=201,
        responses=_error_responses((400, 401, 403, 409, 413, 422, 500, 503)),
    )
    async def create_site_account(
        command: AdminCreateSiteAccountRequest,
        request: Request,
    ) -> SiteAccountView:
        administrator = admin_session(request)
        account = identity_authority().create_account(
            username=command.username,
            email=command.email,
            password=command.password,
            role=SiteRole(command.role),
            temporary_password=False,
            actor_user_id=administrator.account.user_id,
            source=_request_source(
                request,
                trusted_proxy_hosts=api_settings.trusted_proxy_hosts,
            ),
        )
        return _site_account_view(account)

    @app.post(
        "/api/site/admin/accounts/{user_id}/password-reset",
        status_code=204,
        responses=_error_responses((400, 401, 403, 404, 413, 422, 500, 503)),
    )
    async def reset_site_account_password(
        user_id: Identifier,
        command: AdminResetSitePasswordRequest,
        request: Request,
    ) -> None:
        administrator = admin_session(request)
        identity_authority().reset_password(
            user_id,
            new_password=command.new_password,
            actor_user_id=administrator.account.user_id,
            source=_request_source(
                request,
                trusted_proxy_hosts=api_settings.trusted_proxy_hosts,
            ),
        )

    @app.post(
        "/api/site/admin/accounts/{user_id}/status",
        response_model=SiteAccountView,
        responses=_error_responses((400, 401, 403, 404, 413, 422, 500, 503)),
    )
    async def set_site_account_status(
        user_id: Identifier,
        command: AdminSetSiteAccountStatusRequest,
        request: Request,
    ) -> SiteAccountView:
        administrator = admin_session(request)
        account = identity_authority().set_account_status(
            user_id,
            SiteAccountStatus(command.status),
            actor_user_id=administrator.account.user_id,
            source=_request_source(
                request,
                trusted_proxy_hosts=api_settings.trusted_proxy_hosts,
            ),
        )
        return _site_account_view(account)

    @app.get(
        "/api/site/admin/accounts/{user_id}/runs",
        response_model=list[RunSummary],
        responses=_error_responses((400, 401, 403, 404, 413, 422, 500, 503)),
    )
    async def list_admin_site_account_runs(
        user_id: Identifier,
        request: Request,
        response: Response,
    ) -> list[RunSummary]:
        async def read_runs(reader: TenantContentReader) -> list[RunSummary]:
            return [_admin_run_summary(run) for run in await reader.list_runs(user_id)]

        runs = await admin_content_read(
            user_id=user_id,
            request=request,
            content_type="run_catalog",
            content_id=None,
            operation=read_runs,
        )
        response.headers["Cache-Control"] = "no-store"
        return runs

    @app.get(
        "/api/site/admin/accounts/{user_id}/runs/{run_id}",
        response_model=RunView,
        response_model_by_alias=True,
        responses=_error_responses((400, 401, 403, 404, 413, 422, 500, 503)),
    )
    async def get_admin_site_account_run(
        user_id: Identifier,
        run_id: Identifier,
        request: Request,
        response: Response,
    ) -> RunView:
        async def read_run(reader: TenantContentReader) -> RunView:
            return _admin_run_view(await reader.get_run(user_id, run_id))

        run = await admin_content_read(
            user_id=user_id,
            request=request,
            content_type="run",
            content_id=run_id,
            operation=read_run,
        )
        response.headers["Cache-Control"] = "no-store"
        return run

    @app.get(
        "/api/site/admin/accounts/{user_id}/intake/sessions",
        response_model=list[IntakeSessionView],
        responses=_error_responses((400, 401, 403, 404, 413, 422, 500, 503)),
    )
    async def list_admin_site_account_intakes(
        user_id: Identifier,
        request: Request,
        response: Response,
    ) -> list[IntakeSessionView]:
        intakes = await admin_content_read(
            user_id=user_id,
            request=request,
            content_type="intake_catalog",
            content_id=None,
            operation=lambda reader: reader.list_intakes(user_id),
        )
        response.headers["Cache-Control"] = "no-store"
        return intakes

    @app.get(
        "/api/site/admin/accounts/{user_id}/intake/sessions/{session_id}",
        response_model=IntakeSessionView,
        responses=_error_responses((400, 401, 403, 404, 413, 422, 500, 503)),
    )
    async def get_admin_site_account_intake(
        user_id: Identifier,
        session_id: Identifier,
        request: Request,
        response: Response,
    ) -> IntakeSessionView:
        intake = await admin_content_read(
            user_id=user_id,
            request=request,
            content_type="intake_session",
            content_id=session_id,
            operation=lambda reader: reader.get_intake(user_id, session_id),
        )
        response.headers["Cache-Control"] = "no-store"
        return intake

    @app.get(
        "/api/desktop/quit-readiness",
        response_model=QuitReadinessView,
        responses=_error_responses(BASE_ERROR_STATUS_CODES),
    )
    async def quit_readiness(request: Request) -> QuitReadinessView:
        return await (await request_service(request)).quit_readiness()

    @app.get(
        "/api/account",
        response_model=AccountView,
        responses=_error_responses(BASE_ERROR_STATUS_CODES),
    )
    async def account(request: Request) -> AccountView:
        return await (await request_service(request)).account()

    @app.get(
        "/api/account/rate-limits",
        response_model=AccountRateLimitsView,
        responses=_error_responses(BASE_ERROR_STATUS_CODES),
    )
    async def account_rate_limits(request: Request, response: Response) -> AccountRateLimitsView:
        response.headers["Cache-Control"] = "no-store"
        return await (await request_service(request)).account_rate_limits()

    @app.post(
        "/api/account/import-existing",
        response_model=AccountView,
        responses=_command_responses(200),
    )
    async def import_existing_account(
        command: ImportExistingAccountRequest,
        request: Request,
        response: Response,
        idempotency_key: RequiredIdempotencyKey,
    ) -> AccountView:
        _set_idempotency_echo(response, idempotency_key)
        runtime = await request_service(request)
        return await runtime.import_existing_account(
            command,
            idempotency_key=idempotency_key,
        )

    @app.post(
        "/api/account/device-login/start",
        response_model=DeviceLoginStartView,
        responses=_error_responses(COMMAND_ERROR_STATUS_CODES),
    )
    async def start_device_login(request: Request) -> DeviceLoginStartView:
        return await (await request_service(request)).start_device_login()

    @app.get(
        "/api/account/device-login/{login_id}",
        response_model=DeviceLoginStatusView,
        responses=_error_responses(RUN_ERROR_STATUS_CODES),
    )
    async def get_device_login(login_id: Identifier, request: Request) -> DeviceLoginStatusView:
        return await (await request_service(request)).get_device_login(login_id)

    @app.post(
        "/api/account/device-login/{login_id}/cancel",
        response_model=DeviceLoginCancelView,
        responses=_error_responses(COMMAND_ERROR_STATUS_CODES),
    )
    async def cancel_device_login(login_id: Identifier, request: Request) -> DeviceLoginCancelView:
        return await (await request_service(request)).cancel_device_login(login_id)

    @app.get(
        "/api/capabilities",
        response_model=CapabilitiesView,
        responses=_error_responses(BASE_ERROR_STATUS_CODES),
    )
    async def capabilities(request: Request) -> CapabilitiesView:
        return await (await request_service(request)).capabilities()

    @app.get(
        "/api/problem-presets",
        response_model=ProblemPresetsView,
        responses=_error_responses(BASE_ERROR_STATUS_CODES),
    )
    async def problem_presets(request: Request) -> ProblemPresetsView:
        return await (await request_service(request)).get_problem_presets()

    @app.post(
        "/api/intake/sessions",
        response_model=IntakeSessionView,
        responses=_command_responses(
            201,
            error_status_codes=INTAKE_ERROR_STATUS_CODES,
        ),
        status_code=201,
    )
    async def create_intake_session(
        command: CreateIntakeSessionRequest,
        request: Request,
        response: Response,
        idempotency_key: RequiredIdempotencyKey,
    ) -> IntakeSessionView:
        _set_idempotency_echo(response, idempotency_key)
        runtime = await request_service(request)
        return await runtime.create_intake_session(
            command,
            idempotency_key=idempotency_key,
        )

    @app.get(
        "/api/intake/sessions",
        response_model=list[IntakeSessionView],
        responses=_error_responses(INTAKE_ERROR_STATUS_CODES),
    )
    async def list_intake_sessions(
        request: Request,
        status: IntakeSessionStatusValue = "active",
    ) -> list[IntakeSessionView]:
        return await (await request_service(request)).list_intake_sessions(status=status)

    @app.get(
        "/api/intake/sessions/{session_id}",
        response_model=IntakeSessionView,
        responses=_error_responses(INTAKE_ERROR_STATUS_CODES),
    )
    async def get_intake_session(session_id: Identifier, request: Request) -> IntakeSessionView:
        return await (await request_service(request)).get_intake_session(session_id)

    @app.post(
        "/api/intake/sessions/{session_id}/rounds",
        response_model=IntakeSessionView,
        responses=_command_responses(
            200,
            error_status_codes=INTAKE_ERROR_STATUS_CODES,
        ),
    )
    async def submit_intake_round(
        session_id: Identifier,
        command: SubmitIntakeRoundRequest,
        request: Request,
        response: Response,
        idempotency_key: RequiredIdempotencyKey,
    ) -> IntakeSessionView:
        _set_idempotency_echo(response, idempotency_key)
        runtime = await request_service(request)
        return await runtime.submit_intake_round(
            session_id,
            command,
            idempotency_key=idempotency_key,
        )

    @app.post(
        "/api/intake/sessions/{session_id}/finalize",
        response_model=IntakeSessionView,
        responses=_command_responses(
            200,
            error_status_codes=INTAKE_ERROR_STATUS_CODES,
        ),
        description=(
            "Start with what is known. Valid only while the session is "
            "convergence_required: answer every pending problem question, and "
            "everything still open becomes a declared default."
        ),
    )
    async def finalize_intake_session(
        session_id: Identifier,
        command: FinalizeIntakeSessionRequest,
        request: Request,
        response: Response,
        idempotency_key: RequiredIdempotencyKey,
    ) -> IntakeSessionView:
        _set_idempotency_echo(response, idempotency_key)
        runtime = await request_service(request)
        return await runtime.finalize_intake_session(
            session_id,
            command,
            idempotency_key=idempotency_key,
        )

    @app.post(
        "/api/intake/sessions/{session_id}/confirm",
        response_model=IntakeSessionView,
        responses=_command_responses(
            200,
            error_status_codes=INTAKE_ERROR_STATUS_CODES,
        ),
    )
    async def confirm_intake_session(
        session_id: Identifier,
        command: IntakeRevisionRequest,
        request: Request,
        response: Response,
        idempotency_key: RequiredIdempotencyKey,
    ) -> IntakeSessionView:
        _set_idempotency_echo(response, idempotency_key)
        runtime = await request_service(request)
        return await runtime.confirm_intake_session(
            session_id,
            command,
            idempotency_key=idempotency_key,
        )

    @app.post(
        "/api/intake/sessions/{session_id}/cancel",
        response_model=IntakeSessionView,
        responses=_command_responses(
            200,
            error_status_codes=INTAKE_ERROR_STATUS_CODES,
        ),
    )
    async def cancel_intake_session(
        session_id: Identifier,
        command: IntakeRevisionRequest,
        request: Request,
        response: Response,
        idempotency_key: RequiredIdempotencyKey,
    ) -> IntakeSessionView:
        _set_idempotency_echo(response, idempotency_key)
        runtime = await request_service(request)
        return await runtime.cancel_intake_session(
            session_id,
            command,
            idempotency_key=idempotency_key,
        )

    @app.post(
        "/api/runs",
        response_model=RunView,
        response_model_by_alias=True,
        responses=_command_responses(201, error_status_codes=CREATE_ERROR_STATUS_CODES),
        status_code=201,
    )
    async def create_run(
        command: CreateRunRequest,
        request: Request,
        response: Response,
        idempotency_key: IdempotencyKey = None,
    ) -> RunView:
        _set_idempotency_echo(response, idempotency_key)
        runtime = await request_service(request)
        return await runtime.create_run(command, idempotency_key=idempotency_key)

    @app.get(
        "/api/runs",
        response_model=list[RunSummary],
        responses=_error_responses(BASE_ERROR_STATUS_CODES),
    )
    async def list_runs(request: Request) -> list[RunSummary]:
        return await (await request_service(request)).list_runs()

    @app.get(
        "/api/runs/{run_id}",
        response_model=RunView,
        response_model_by_alias=True,
        responses=_error_responses(RUN_ERROR_STATUS_CODES),
    )
    async def get_run(run_id: Identifier, request: Request) -> RunView:
        return await (await request_service(request)).get_run(run_id)

    @app.post(
        "/api/runs/{run_id}/reports",
        response_model=ReportBundleView,
        responses=_error_responses(COMMAND_ERROR_STATUS_CODES),
        status_code=201,
    )
    async def export_report(
        run_id: Identifier,
        command: ExportReportRequest,
        request: Request,
    ) -> ReportBundleView:
        return await (await request_service(request)).export_report(run_id, command)

    @app.get(
        "/api/runs/{run_id}/reports/{export_id}/report.pdf",
        response_class=Response,
        responses=_error_responses(RUN_ERROR_STATUS_CODES),
    )
    async def download_report_pdf(
        run_id: Identifier,
        export_id: Identifier,
        request: Request,
    ) -> Response:
        content = await (await request_service(request)).read_report_pdf(run_id, export_id)
        filename = f"DerivationLab-{run_id}-{export_id}.pdf"
        return Response(
            content=content,
            media_type="application/pdf",
            headers={
                "Cache-Control": "no-store",
                "Content-Disposition": f'attachment; filename="{filename}"',
            },
        )

    @app.get(
        "/api/runs/{run_id}/events",
        response_class=EventStreamResponse,
        responses={
            200: {
                "description": "Replayable Server-Sent Events stream.",
                "content": {
                    "text/event-stream": {
                        "schema": {
                            "type": "string",
                            "format": "event-stream",
                            "x-sse-data-schema": {"$ref": "#/components/schemas/RunEvent"},
                        }
                    }
                },
            },
            **_error_responses(RUN_ERROR_STATUS_CODES),
        },
    )
    async def events(
        run_id: Identifier,
        request: Request,
        after: Annotated[int | None, Query(ge=0, le=MAX_EVENT_ID)] = None,
        follow: bool = True,
        last_event_id: LastEventId = None,
    ) -> EventStreamResponse:
        # Resolve errors before response headers start.  The service guarantees
        # GET is a complete canonical snapshot, never an in-flight strict replay.
        runtime = await request_service(request)
        await runtime.get_run(run_id)
        cursor = _cursor(last_event_id, after)
        source = runtime.stream_events(run_id, after_event_id=cursor, follow=follow)

        async def session_is_active() -> bool:
            if site_identity is None:
                return True
            token = getattr(request.state, "site_session_token", "")
            try:
                site_identity.authenticate_session(token, touch=False)
            except SessionInvalid:
                return False
            return True

        body = stream_sse(
            request,
            source,
            heartbeat_seconds=api_settings.sse_heartbeat_seconds,
            retry_milliseconds=api_settings.sse_retry_milliseconds,
            authorization_check=session_is_active if server_mode else None,
        )
        return EventStreamResponse(
            body,
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @app.post(
        "/api/runs/{run_id}/pause",
        response_model=RunView,
        response_model_by_alias=True,
        responses=_command_responses(200),
        description=(
            "Request a resumable soft pause. Eligible only during active execution in phase "
            "submitted, autonomous_exploration, or human_expansion; the service is authoritative."
        ),
    )
    async def pause_run(
        run_id: Identifier,
        request: Request,
        response: Response,
        idempotency_key: IdempotencyKey = None,
    ) -> RunView:
        _set_idempotency_echo(response, idempotency_key)
        runtime = await request_service(request)
        return await runtime.pause_run(run_id, idempotency_key=idempotency_key)

    @app.post(
        "/api/runs/{run_id}/resume",
        response_model=RunView,
        response_model_by_alias=True,
        responses=_command_responses(200),
        description=(
            "Resume scheduling from a resumable soft pause. Eligible only when phase is paused; "
            "the service maps this command to the core resume operation."
        ),
    )
    async def resume_run(
        run_id: Identifier,
        request: Request,
        response: Response,
        idempotency_key: IdempotencyKey = None,
    ) -> RunView:
        _set_idempotency_echo(response, idempotency_key)
        runtime = await request_service(request)
        return await runtime.resume_run(run_id, idempotency_key=idempotency_key)

    @app.post(
        "/api/runs/{run_id}/interrupt",
        response_model=RunView,
        response_model_by_alias=True,
        responses=_command_responses(200),
        description=(
            "Request a hard interrupt. Eligible only while the service has at least one current "
            "in-flight call; phase alone is insufficient and the service is authoritative."
        ),
    )
    async def interrupt_run(
        run_id: Identifier,
        request: Request,
        response: Response,
        idempotency_key: IdempotencyKey = None,
    ) -> RunView:
        _set_idempotency_echo(response, idempotency_key)
        runtime = await request_service(request)
        return await runtime.interrupt_run(run_id, idempotency_key=idempotency_key)

    @app.post(
        "/api/runs/{run_id}/branches",
        response_model=RunView,
        response_model_by_alias=True,
        responses=_command_responses(201),
        status_code=201,
    )
    async def create_branch(
        run_id: Identifier,
        command: CreateBranchRequest,
        request: Request,
        response: Response,
        idempotency_key: IdempotencyKey = None,
    ) -> RunView:
        _set_idempotency_echo(response, idempotency_key)
        runtime = await request_service(request)
        return await runtime.create_branch(run_id, command, idempotency_key=idempotency_key)

    def custom_openapi() -> dict[str, object]:
        if app.openapi_schema is not None:
            return app.openapi_schema
        schema = get_openapi(title=app.title, version=app.version, routes=app.routes)
        event_schema = RunEvent.model_json_schema(
            by_alias=True,
            mode="serialization",
            ref_template="#/components/schemas/{model}",
        )
        definitions = event_schema.pop("$defs", {})
        schemas = schema.setdefault("components", {}).setdefault("schemas", {})
        for name, definition in definitions.items():
            schemas.setdefault(name, definition)
        schemas["RunEvent"] = event_schema

        error_schema = ErrorEnvelope.model_json_schema(
            by_alias=True,
            mode="serialization",
            ref_template="#/components/schemas/{model}",
        )
        error_definitions = error_schema.pop("$defs", {})
        for name, definition in error_definitions.items():
            schemas.setdefault(name, definition)
        schemas["ErrorEnvelope"] = error_schema
        schemas.pop("HTTPValidationError", None)
        schemas.pop("ValidationError", None)
        app.openapi_schema = schema
        return schema

    app.openapi = custom_openapi  # type: ignore[method-assign]
    return app
