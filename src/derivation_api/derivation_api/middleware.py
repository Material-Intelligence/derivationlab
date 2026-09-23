"""Small ASGI middleware for the local-only API boundary."""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable
from urllib.parse import urlsplit

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .site_access import SessionInvalid, SiteIdentityAuthority

REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def request_id_from_scope(scope: Scope) -> str:
    state = scope.setdefault("state", {})
    existing = state.get("request_id")
    if isinstance(existing, str):
        return existing
    generated = uuid.uuid4().hex
    state["request_id"] = generated
    return generated


def error_payload(scope: Scope, code: str, message: str, details: object | None = None) -> dict[str, object]:
    return {
        "error": {"code": code, "message": message, "details": details},
        "request_id": request_id_from_scope(scope),
    }


async def send_json_error(
    scope: Scope,
    receive: Receive,
    send: Send,
    *,
    status_code: int,
    code: str,
    message: str,
    details: object | None = None,
) -> None:
    response = JSONResponse(
        error_payload(scope, code, message, details),
        status_code=status_code,
        headers={"Cache-Control": "no-store"},
    )
    await response(scope, receive, send)


class RequestIdMiddleware:
    """Attach a safe request id to state and every HTTP response."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        supplied = headers.get(b"x-request-id", b"").decode("ascii", errors="ignore")
        request_id = supplied if REQUEST_ID_RE.fullmatch(supplied) else uuid.uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id

        async def add_request_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                response_headers = list(message.get("headers", []))
                response_headers.append((b"x-request-id", request_id.encode("ascii")))
                message["headers"] = response_headers
            await send(message)

        await self.app(scope, receive, add_request_id)


class RequestBodyLimitMiddleware:
    """Reject oversized declared or streamed request bodies before parsing."""

    def __init__(self, app: ASGIApp, *, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        raw_length = headers.get(b"content-length")
        if raw_length is not None:
            try:
                declared_length = int(raw_length)
            except ValueError:
                await send_json_error(
                    scope,
                    receive,
                    send,
                    status_code=400,
                    code="invalid_content_length",
                    message="Content-Length must be an integer.",
                )
                return
            if declared_length < 0:
                await send_json_error(
                    scope,
                    receive,
                    send,
                    status_code=400,
                    code="invalid_content_length",
                    message="Content-Length must not be negative.",
                )
                return
            if declared_length > self.max_bytes:
                await send_json_error(
                    scope,
                    receive,
                    send,
                    status_code=413,
                    code="request_too_large",
                    message="Request body exceeds the configured limit.",
                    details={"max_bytes": self.max_bytes},
                )
                return

        received = 0
        response_started = False

        class BodyTooLarge(Exception):
            pass

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise BodyTooLarge
            return message

        async def track_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, track_send)
        except BodyTooLarge:
            if response_started:
                raise
            await send_json_error(
                scope,
                receive,
                send,
                status_code=413,
                code="request_too_large",
                message="Request body exceeds the configured limit.",
                details={"max_bytes": self.max_bytes},
            )


def is_local_origin(origin: str) -> bool:
    try:
        parsed = urlsplit(origin)
        _ = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme in {"http", "https"}
        and parsed.hostname in LOCAL_HOSTS
        and not parsed.username
        and parsed.path in {"", "/"}
        and not parsed.query
        and not parsed.fragment
    )


class LocalOriginMiddleware:
    """Make CORS an authorization check, not merely a browser response hint."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        raw_origin = headers.get(b"origin")
        if raw_origin is not None:
            origin = raw_origin.decode("latin-1")
            if not is_local_origin(origin):
                await send_json_error(
                    scope,
                    receive,
                    send,
                    status_code=403,
                    code="origin_not_allowed",
                    message="Only local browser origins may access this API.",
                )
                return
        await self.app(scope, receive, send)


def canonical_https_origin(origin: str) -> str | None:
    try:
        parsed = urlsplit(origin)
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        return None
    authority = parsed.hostname.lower()
    if ":" in authority:
        authority = f"[{authority}]"
    if port is not None:
        authority = f"{authority}:{port}"
    return f"https://{authority}"


class ServerOriginMiddleware:
    """Require an exact configured HTTPS Origin for browser mutations."""

    def __init__(self, app: ASGIApp, *, allowed_origins: Iterable[str]) -> None:
        self.app = app
        canonical = {canonical_https_origin(origin) for origin in allowed_origins}
        if None in canonical or not canonical:
            raise ValueError("server mode requires valid HTTPS origins")
        self.allowed_origins = frozenset(canonical)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        raw_origin = headers.get(b"origin")
        origin = canonical_https_origin(raw_origin.decode("latin-1")) if raw_origin is not None else None
        if raw_origin is not None and origin not in self.allowed_origins:
            await send_json_error(
                scope,
                receive,
                send,
                status_code=403,
                code="origin_not_allowed",
                message="This browser origin may not access the server.",
            )
            return
        method = str(scope.get("method", "GET")).upper()
        path = str(scope.get("path", ""))
        if method in {"POST", "PUT", "PATCH", "DELETE"} and path.startswith("/api/") and origin is None:
            await send_json_error(
                scope,
                receive,
                send,
                status_code=403,
                code="origin_required",
                message="An allowed HTTPS Origin is required for this command.",
            )
            return
        await self.app(scope, receive, send)


class SiteSessionMiddleware:
    """Attach an authenticated site Session to protected API requests."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        identity: SiteIdentityAuthority,
        cookie_name: str,
    ) -> None:
        self.app = app
        self.identity = identity
        self.cookie_name = cookie_name

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not self._protected(scope):
            await self.app(scope, receive, send)
            return
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        token = self._cookie(headers.get(b"cookie", b""), self.cookie_name)
        if token is None:
            await send_json_error(
                scope,
                receive,
                send,
                status_code=401,
                code="site_session_required",
                message="Sign in to continue.",
            )
            return
        try:
            session = self.identity.authenticate_session(token)
        except SessionInvalid:
            await send_json_error(
                scope,
                receive,
                send,
                status_code=401,
                code="site_session_invalid",
                message="The site session is unavailable or expired.",
            )
            return
        state = scope.setdefault("state", {})
        state["site_session"] = session
        state["site_session_token"] = token
        path = str(scope.get("path", ""))
        if session.account.must_change_password and path not in {
            "/api/site/session",
            "/api/site/password",
        }:
            await send_json_error(
                scope,
                receive,
                send,
                status_code=403,
                code="password_change_required",
                message="Change the temporary password before continuing.",
            )
            return
        await self.app(scope, receive, send)

    @staticmethod
    def _protected(scope: Scope) -> bool:
        if str(scope.get("method", "GET")).upper() == "OPTIONS":
            return False
        path = str(scope.get("path", ""))
        if not path.startswith("/api/"):
            return False
        return not (
            path in {"/api/build-info", "/api/site/mode"}
            or (path == "/api/site/session" and str(scope.get("method", "GET")).upper() == "POST")
        )

    @staticmethod
    def _cookie(raw: bytes, name: str) -> str | None:
        try:
            value = raw.decode("latin-1")
        except UnicodeDecodeError:
            return None
        for item in value.split(";"):
            key, separator, candidate = item.strip().partition("=")
            if separator and key == name and candidate:
                return candidate
        return None


class ServerSecurityHeadersMiddleware:
    """Add browser hardening headers to every authenticated-server response."""

    _HEADERS = (
        (b"strict-transport-security", b"max-age=31536000"),
        (b"x-content-type-options", b"nosniff"),
        (b"x-frame-options", b"DENY"),
        (b"referrer-policy", b"no-referrer"),
        (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
        (
            b"content-security-policy",
            b"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            b"img-src 'self' data:; font-src 'self'; connect-src 'self'; "
            b"frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
        ),
    )

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def add_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                present = {key.lower() for key, _ in headers}
                headers.extend((key, value) for key, value in self._HEADERS if key not in present)
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, add_headers)
