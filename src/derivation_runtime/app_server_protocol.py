"""Strict protocol boundary for Codex App Server 0.147.0.

This module deliberately keeps provider JSON opaque after validating the
parts the runtime depends on.  A known notification is always surfaced as a
``ProviderEvent``; an unknown notification or state poisons the connection
instead of being silently ignored.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any, TypeAlias
from urllib.parse import urlsplit

JsonObject: TypeAlias = dict[str, Any]

PINNED_CODEX_VERSION = "0.147.0"
PINNED_PROTOCOL_SCHEMA = "codex-app-server-v2-0.147.0"


class AppServerError(RuntimeError):
    """Base class for App Server adapter failures."""


class AppServerStateError(AppServerError):
    """The client operation is not valid in its current lifecycle state."""


class AppServerStartupError(AppServerError):
    """The App Server child could not be started or initialized."""


class AppServerTimeoutError(AppServerError):
    """A bounded App Server operation exceeded its deadline."""

    def __init__(self, operation: str, timeout_seconds: float) -> None:
        self.operation = operation
        self.timeout_seconds = timeout_seconds
        super().__init__(
            f"App Server operation {operation!r} timed out after {timeout_seconds:.3f}s"
        )


class AppServerProtocolError(AppServerError):
    """The child violated the pinned JSONL protocol contract."""


class AppServerForkError(AppServerProtocolError):
    """One ``thread/fork`` response was rejected, and only that response.

    The failure is confined to the forked thread the client refused to adopt:
    every other thread, turn and pending request the child is serving is
    untouched, so the caller may abandon this fork and keep using the same App
    Server child.  Raised instead of failing the whole client, which would
    take every concurrent run down with it.
    """


class MalformedProtocolMessage(AppServerProtocolError):
    """A stdout line was not a valid App Server protocol message."""

    def __init__(self, reason: str, line_preview: str | None = None) -> None:
        self.reason = reason
        self.line_preview = line_preview
        suffix = "" if line_preview is None else f"; line={line_preview!r}"
        super().__init__(f"Malformed App Server message: {reason}{suffix}")


class DuplicateResponseError(AppServerProtocolError):
    """The child sent more than one response for a request id."""

    def __init__(self, request_id: int) -> None:
        self.request_id = request_id
        super().__init__(f"Duplicate response for request id {request_id}")


class UnexpectedResponseError(AppServerProtocolError):
    """The child responded with an id the client cannot correlate."""

    def __init__(self, request_id: object) -> None:
        self.request_id = request_id
        super().__init__(f"Unexpected response id {request_id!r}")


class UnknownNotificationError(AppServerProtocolError):
    """A notification is outside the pinned schema."""

    def __init__(self, method: str) -> None:
        self.method = method
        super().__init__(f"Unknown App Server notification {method!r}")


class UnknownProtocolStateError(AppServerProtocolError):
    """A known event carried a state the adapter cannot map safely."""

    def __init__(self, field: str, value: object, method: str) -> None:
        self.field = field
        self.value = value
        self.method = method
        super().__init__(
            f"Unknown state {value!r} for {field!r} in notification {method!r}"
        )


class UnexpectedServerRequestError(AppServerProtocolError):
    """The unattended runtime received an interactive server request."""

    def __init__(self, method: str, request_id: object) -> None:
        self.method = method
        self.request_id = request_id
        super().__init__(
            f"Unexpected server request {method!r} with id {request_id!r}; "
            "unattended runtime fails closed"
        )


class ProtocolVersionError(AppServerProtocolError):
    """The child version does not match the caller-selected protocol pin."""

    def __init__(self, expected: str, actual: str | None, user_agent: str) -> None:
        self.expected = expected
        self.actual = actual
        self.user_agent = user_agent
        super().__init__(
            f"Codex version mismatch: expected {expected!r}, "
            f"observed {actual!r} in userAgent {user_agent!r}"
        )


class ProtocolIdentityError(AppServerProtocolError):
    """The initialize userAgent does not identify the configured client."""

    def __init__(self, expected: str, actual: str | None, user_agent: str) -> None:
        self.expected = expected
        self.actual = actual
        self.user_agent = user_agent
        super().__init__(
            f"App Server client identity mismatch: expected {expected!r}, "
            f"observed {actual!r} in userAgent {user_agent!r}"
        )


class AppServerRemoteError(AppServerError):
    """A JSON-RPC request completed with a structured remote error."""

    def __init__(
        self,
        *,
        method: str,
        request_id: int,
        code: int | None,
        message: str,
        data: Any = None,
    ) -> None:
        self.method = method
        self.request_id = request_id
        self.code = code
        self.remote_message = message
        self.data = data
        super().__init__(
            f"App Server request {method!r} ({request_id}) failed "
            f"with code {code!r}: {message}"
        )


class AppServerEOFError(AppServerProtocolError):
    """The child closed stdout before a deliberate client shutdown."""


class AppServerProcessError(AppServerError):
    """The child process exited unexpectedly."""

    def __init__(self, returncode: int | None, stderr_tail: str) -> None:
        self.returncode = returncode
        self.stderr_tail = stderr_tail
        detail = f"; stderr tail: {stderr_tail}" if stderr_tail else ""
        super().__init__(f"App Server exited unexpectedly ({returncode}){detail}")


class EventBufferOverflowError(AppServerProtocolError):
    """The consumer stopped draining the bounded provider event queue."""


class TurnNotCompletedError(AppServerStateError):
    """A fork was requested from a turn not known to have completed."""

    def __init__(self, turn_id: str, status: str | None) -> None:
        self.turn_id = turn_id
        self.status = status
        super().__init__(
            f"Cannot fork from turn {turn_id!r}: known status is {status!r}, "
            "expected 'completed'"
        )


class FinalAgentMessageError(AppServerProtocolError):
    """A completed turn does not have one unambiguous final answer item."""


_USER_AGENT = re.compile(r"^(?P<client>[^/ ()]+)/(?P<version>[^ ()]+)(?:[ (]|$)")


@dataclass(frozen=True)
class ProtocolPin:
    """Caller-visible compatibility assertion for the generated schema pin."""

    cli_version: str = PINNED_CODEX_VERSION
    schema_revision: str = PINNED_PROTOCOL_SCHEMA

    def assert_initialize_response(
        self,
        result: Mapping[str, Any],
        *,
        expected_client_name: str,
    ) -> str:
        user_agent = result.get("userAgent")
        if not isinstance(user_agent, str):
            raise MalformedProtocolMessage(
                "initialize result is missing string userAgent"
            )
        match = _USER_AGENT.match(user_agent)
        actual_client = match.group("client") if match else None
        if actual_client != expected_client_name:
            raise ProtocolIdentityError(expected_client_name, actual_client, user_agent)
        actual = match.group("version") if match else None
        if actual != self.cli_version:
            raise ProtocolVersionError(self.cli_version, actual, user_agent)
        return actual


@dataclass(frozen=True, repr=False)
class ChatgptDeviceCodeLogin:
    """Opaque handle for one App Server ChatGPT device-code login.

    ``login_id`` and ``user_code`` are deliberately absent from ``repr`` so
    routine diagnostics cannot copy either transient secret into a log.  The
    client-private owner token prevents a handle created by one client from
    being waited on or cancelled by another client.
    """

    login_id: str
    user_code: str
    verification_url: str
    _owner_token: object

    def __repr__(self) -> str:
        return (
            "ChatgptDeviceCodeLogin("
            "login_id=<redacted>, user_code=<redacted>, "
            f"verification_url={self.verification_url!r})"
        )


@dataclass(frozen=True, repr=False)
class AccountLoginCompletion:
    """Terminal outcome for a login, without retaining its login id."""

    success: bool
    error: str | None

    def __repr__(self) -> str:
        error = "None" if self.error is None else "<redacted>"
        return f"AccountLoginCompletion(success={self.success!r}, error={error})"


def validate_rate_limit_snapshot(value: Any, *, method: str) -> JsonObject:
    """Validate the account quota fields consumed by DerivationLab.

    The provider snapshot contains additional account and credit metadata that
    this product deliberately keeps opaque. Only the fields needed by the
    informational usage surface are validated and copied here.
    """

    snapshot = _require_object(value, "rateLimits", method)
    for field in ("limitId", "limitName"):
        item = snapshot.get(field)
        if item is not None and (not isinstance(item, str) or not item.strip()):
            raise MalformedProtocolMessage(
                f"{method} rateLimits.{field} must be non-empty text or null"
            )
    plan_type = snapshot.get("planType")
    if plan_type is not None and plan_type not in KNOWN_ACCOUNT_PLAN_TYPES:
        raise UnknownProtocolStateError("rateLimits.planType", plan_type, method)
    for field in ("primary", "secondary"):
        window = snapshot.get(field)
        if window is None:
            continue
        window = _require_object(window, f"rateLimits.{field}", method)
        used_percent = window.get("usedPercent")
        if (
            isinstance(used_percent, bool)
            or not isinstance(used_percent, int)
            or not 0 <= used_percent <= 100
        ):
            raise MalformedProtocolMessage(
                f"{method} rateLimits.{field}.usedPercent must be an integer from 0 to 100"
            )
        duration = window.get("windowDurationMins")
        if duration is not None and (
            isinstance(duration, bool) or not isinstance(duration, int) or duration <= 0
        ):
            raise MalformedProtocolMessage(
                f"{method} rateLimits.{field}.windowDurationMins must be a positive integer or null"
            )
        resets_at = window.get("resetsAt")
        if resets_at is not None and (
            isinstance(resets_at, bool)
            or not isinstance(resets_at, int)
            or resets_at < 0
        ):
            raise MalformedProtocolMessage(
                f"{method} rateLimits.{field}.resetsAt must be a non-negative integer or null"
            )
    return copy.deepcopy(snapshot)


def validate_account_rate_limits_response(value: Any) -> JsonObject:
    response = _require_object(value, "result", "account/rateLimits/read")
    if "rateLimits" not in response:
        raise MalformedProtocolMessage(
            "account/rateLimits/read result requires rateLimits"
        )
    validate_rate_limit_snapshot(
        response["rateLimits"], method="account/rateLimits/read"
    )
    by_id = response.get("rateLimitsByLimitId")
    if by_id is not None:
        by_id = _require_object(by_id, "rateLimitsByLimitId", "account/rateLimits/read")
        for limit_id, snapshot in by_id.items():
            if not isinstance(limit_id, str) or not limit_id.strip():
                raise MalformedProtocolMessage(
                    "account/rateLimits/read rateLimitsByLimitId keys must be non-empty text"
                )
            validate_rate_limit_snapshot(snapshot, method="account/rateLimits/read")
    return copy.deepcopy(response)


class ProviderEventKind(str, Enum):
    THREAD = "thread"
    TURN = "turn"
    ITEM = "item"
    STREAM = "stream"
    TOOL = "tool"
    PROCESS = "process"
    ACCOUNT = "account"
    SYSTEM = "system"


@dataclass(frozen=True)
class ProviderEvent:
    """Provider-neutral envelope that retains the complete provider payload."""

    sequence: int
    kind: ProviderEventKind
    method: str
    params: Mapping[str, Any]
    emitted_at_ms: int | None
    raw: Mapping[str, Any]


# Generated from the checked-in 0.147.0 experimental v2 schema.  Keeping this
# exhaustive is intentional: upgrades must update the pin, schema snapshot,
# this set, and contract tests together.
KNOWN_NOTIFICATION_METHODS = frozenset(
    {
        "error",
        "thread/started",
        "thread/status/changed",
        "thread/archived",
        "thread/deleted",
        "thread/unarchived",
        "thread/closed",
        "skills/changed",
        "thread/name/updated",
        "thread/goal/updated",
        "thread/goal/cleared",
        "thread/environment/connected",
        "thread/environment/disconnected",
        "thread/settings/updated",
        "thread/tokenUsage/updated",
        "turn/started",
        "hook/started",
        "turn/completed",
        "hook/completed",
        "turn/diff/updated",
        "turn/plan/updated",
        "item/started",
        "item/autoApprovalReview/started",
        "item/autoApprovalReview/completed",
        "item/completed",
        "item/agentMessage/delta",
        "item/plan/delta",
        "command/exec/outputDelta",
        "process/outputDelta",
        "process/exited",
        "item/commandExecution/outputDelta",
        "item/commandExecution/terminalInteraction",
        "item/fileChange/outputDelta",
        "item/fileChange/patchUpdated",
        "serverRequest/resolved",
        "item/mcpToolCall/progress",
        "mcpServer/oauthLogin/completed",
        "mcpServer/startupStatus/updated",
        "account/updated",
        "account/rateLimits/updated",
        "app/list/updated",
        "remoteControl/status/changed",
        "externalAgentConfig/import/progress",
        "externalAgentConfig/import/completed",
        "fs/changed",
        "item/reasoning/summaryTextDelta",
        "item/reasoning/summaryPartAdded",
        "item/reasoning/textDelta",
        "thread/compacted",
        "model/rerouted",
        "model/verification",
        "turn/moderationMetadata",
        "model/safetyBuffering/updated",
        "warning",
        "guardianWarning",
        "deprecationNotice",
        "configWarning",
        "fuzzyFileSearch/sessionUpdated",
        "fuzzyFileSearch/sessionCompleted",
        "thread/realtime/started",
        "thread/realtime/itemAdded",
        "thread/realtime/transcript/delta",
        "thread/realtime/transcript/done",
        "thread/realtime/outputAudio/delta",
        "thread/realtime/sdp",
        "thread/realtime/error",
        "thread/realtime/closed",
        "windows/worldWritableWarning",
        "windowsSandbox/setupCompleted",
        "account/login/completed",
    }
)


# Every server-initiated request is rejected by the unattended v1 adapter.
# The first five are interactive approval/input surfaces; the remainder require
# client capabilities or policies this adapter does not claim to implement.
KNOWN_SERVER_REQUEST_METHODS = frozenset(
    {
        "item/commandExecution/requestApproval",
        "item/fileChange/requestApproval",
        "item/tool/requestUserInput",
        "mcpServer/elicitation/request",
        "item/permissions/requestApproval",
        "item/tool/call",
        "account/chatgptAuthTokens/refresh",
        "attestation/generate",
        "currentTime/read",
        "applyPatchApproval",
        "execCommandApproval",
    }
)


KNOWN_THREAD_STATUSES = frozenset({"notLoaded", "idle", "systemError", "active"})
KNOWN_TURN_STATUSES = frozenset({"inProgress", "completed", "interrupted", "failed"})
KNOWN_ITEM_TYPES = frozenset(
    {
        "userMessage",
        "hookPrompt",
        "agentMessage",
        "plan",
        "reasoning",
        "commandExecution",
        "fileChange",
        "mcpToolCall",
        "dynamicToolCall",
        "collabAgentToolCall",
        "subAgentActivity",
        "webSearch",
        "imageView",
        "sleep",
        "imageGeneration",
        "enteredReviewMode",
        "exitedReviewMode",
        "contextCompaction",
    }
)
KNOWN_AGENT_MESSAGE_PHASES = frozenset({"commentary", "final_answer"})
KNOWN_ACCOUNT_AUTH_MODES = frozenset(
    {
        "apikey",
        "chatgpt",
        "chatgptAuthTokens",
        "headers",
        "agentIdentity",
        "personalAccessToken",
        "bedrockApiKey",
    }
)
KNOWN_ACCOUNT_PLAN_TYPES = frozenset(
    {
        "free",
        "go",
        "plus",
        "pro",
        "prolite",
        "team",
        "self_serve_business_prolite",
        "self_serve_business_usage_based",
        "business",
        "ent26",
        "enterprise_cbp_automation",
        "enterprise_cbp_usage_based",
        "enterprise",
        "edu",
        "unknown",
    }
)

_SUPPORTED_NOTIFICATION_FIELDS: Mapping[str, tuple[frozenset[str], frozenset[str]]] = {
    "error": (frozenset({"error", "threadId", "turnId", "willRetry"}), frozenset()),
    "thread/started": (frozenset({"thread"}), frozenset()),
    "thread/status/changed": (frozenset({"status", "threadId"}), frozenset()),
    "thread/tokenUsage/updated": (
        frozenset({"threadId", "turnId", "tokenUsage"}),
        frozenset(),
    ),
    "turn/started": (frozenset({"threadId", "turn"}), frozenset({"turnId"})),
    "turn/completed": (frozenset({"threadId", "turn"}), frozenset({"turnId"})),
    "item/started": (
        frozenset({"item", "startedAtMs", "threadId", "turnId"}),
        frozenset(),
    ),
    "item/completed": (
        frozenset({"completedAtMs", "item", "threadId", "turnId"}),
        frozenset(),
    ),
    "item/agentMessage/delta": (
        frozenset({"delta", "itemId", "threadId", "turnId"}),
        frozenset(),
    ),
    "item/plan/delta": (
        frozenset({"delta", "itemId", "threadId", "turnId"}),
        frozenset(),
    ),
    "item/reasoning/summaryTextDelta": (
        frozenset({"delta", "itemId", "summaryIndex", "threadId", "turnId"}),
        frozenset(),
    ),
    "item/reasoning/summaryPartAdded": (
        frozenset({"itemId", "summaryIndex", "threadId", "turnId"}),
        frozenset(),
    ),
    "item/reasoning/textDelta": (
        frozenset({"contentIndex", "delta", "itemId", "threadId", "turnId"}),
        frozenset(),
    ),
    "model/rerouted": (
        frozenset({"fromModel", "reason", "threadId", "toModel", "turnId"}),
        frozenset(),
    ),
    "command/exec/outputDelta": (
        frozenset({"capReached", "deltaBase64", "processId", "stream"}),
        frozenset(),
    ),
    "process/outputDelta": (
        frozenset({"capReached", "deltaBase64", "processHandle", "stream"}),
        frozenset(),
    ),
    "process/exited": (
        frozenset(
            {
                "exitCode",
                "processHandle",
                "stderr",
                "stderrCapReached",
                "stdout",
                "stdoutCapReached",
            }
        ),
        frozenset(),
    ),
    "warning": (frozenset({"message"}), frozenset({"threadId"})),
    "guardianWarning": (frozenset({"message", "threadId"}), frozenset()),
    "remoteControl/status/changed": (
        frozenset({"installationId", "serverName", "status"}),
        frozenset({"environmentId"}),
    ),
    "deprecationNotice": (frozenset({"summary"}), frozenset({"details"})),
    "configWarning": (
        frozenset({"summary"}),
        frozenset({"details", "path", "range"}),
    ),
    "account/login/completed": (
        frozenset({"success"}),
        frozenset({"error", "loginId", "onboardingEntrypoint"}),
    ),
    "account/updated": (
        frozenset(),
        frozenset({"authMode", "planType"}),
    ),
    "account/rateLimits/updated": (
        frozenset({"rateLimits"}),
        frozenset(),
    ),
}


def _require_object(value: Any, field: str, method: str) -> JsonObject:
    if not isinstance(value, dict):
        raise MalformedProtocolMessage(
            f"{method} requires object field {field!r}, got {type(value).__name__}"
        )
    return value


def _validate_turn_items(turn: JsonObject, method: str) -> None:
    items = turn.get("items")
    if not isinstance(items, list):
        raise MalformedProtocolMessage(f"{method} turn requires an items array")
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise MalformedProtocolMessage(
                f"{method} turn item {index} must be an object"
            )
        item_type = item.get("type")
        if item_type not in KNOWN_ITEM_TYPES:
            raise UnknownProtocolStateError(
                f"turn.items[{index}].type", item_type, method
            )
        item_id = item.get("id")
        if not isinstance(item_id, str) or not item_id.strip():
            raise MalformedProtocolMessage(
                f"{method} turn item {index} requires a non-empty string id"
            )
        if item_type == "agentMessage":
            if not isinstance(item.get("text"), str):
                raise MalformedProtocolMessage(
                    f"{method} agentMessage item {index} requires string text"
                )
            phase = item.get("phase")
            if phase is not None and phase not in KNOWN_AGENT_MESSAGE_PHASES:
                raise UnknownProtocolStateError(
                    f"turn.items[{index}].phase", phase, method
                )


def validate_thread_snapshot(thread: Mapping[str, Any], method: str) -> None:
    """Validate state and item discriminators used by persisted thread reads."""

    if not isinstance(thread.get("id"), str) or not thread["id"].strip():
        raise MalformedProtocolMessage(
            f"{method} thread requires a non-empty string id"
        )
    if not isinstance(thread.get("sessionId"), str) or not thread["sessionId"].strip():
        raise MalformedProtocolMessage(
            f"{method} thread requires a non-empty string sessionId"
        )
    forked_from_id = thread.get("forkedFromId")
    if forked_from_id is not None and (
        not isinstance(forked_from_id, str)
        or not forked_from_id.strip()
        or forked_from_id == thread["id"]
    ):
        raise MalformedProtocolMessage(f"{method} thread has invalid forkedFromId")
    if not isinstance(thread.get("ephemeral"), bool):
        raise MalformedProtocolMessage(f"{method} thread requires boolean ephemeral")
    turns = thread.get("turns")
    if not isinstance(turns, list):
        raise MalformedProtocolMessage(f"{method} thread requires a turns array")
    for index, turn_value in enumerate(turns):
        if not isinstance(turn_value, dict):
            raise MalformedProtocolMessage(f"{method} turn {index} must be an object")
        turn_id = turn_value.get("id")
        if not isinstance(turn_id, str) or not turn_id.strip():
            raise MalformedProtocolMessage(
                f"{method} turn {index} requires a non-empty string id"
            )
        status = turn_value.get("status")
        if status not in KNOWN_TURN_STATUSES:
            raise UnknownProtocolStateError(
                f"thread.turns[{index}].status", status, method
            )
        _validate_turn_items(turn_value, method)


def _validate_known_states(method: str, params: JsonObject) -> None:
    if method == "model/rerouted":
        required = {"fromModel", "toModel", "reason", "threadId", "turnId"}
        if set(params) != required:
            raise MalformedProtocolMessage(
                "model/rerouted params must contain exactly "
                "fromModel, toModel, reason, threadId, and turnId"
            )
        for field in ("fromModel", "toModel", "threadId", "turnId"):
            value = params.get(field)
            if not isinstance(value, str) or not value.strip():
                raise MalformedProtocolMessage(
                    f"model/rerouted requires non-empty string {field}"
                )
        reason = params.get("reason")
        if reason != "highRiskCyberActivity":
            raise UnknownProtocolStateError("reason", reason, "model/rerouted")

    if method == "thread/started":
        thread = _require_object(params.get("thread"), "thread", method)
        validate_thread_snapshot(thread, method)

    if method == "thread/status/changed":
        status = _require_object(params.get("status"), "status", method)
        status_type = status.get("type")
        if status_type not in KNOWN_THREAD_STATUSES:
            raise UnknownProtocolStateError("status.type", status_type, method)

    if method in {"turn/started", "turn/completed"}:
        thread_id = params.get("threadId")
        if not isinstance(thread_id, str) or not thread_id:
            raise MalformedProtocolMessage(
                f"{method} requires non-empty string threadId"
            )
        turn = _require_object(params.get("turn"), "turn", method)
        embedded_turn_id = turn.get("id")
        if not isinstance(embedded_turn_id, str) or not embedded_turn_id:
            raise MalformedProtocolMessage(
                f"{method} turn requires a non-empty string id"
            )
        top_level_turn_id = params.get("turnId")
        if top_level_turn_id is not None:
            if not isinstance(top_level_turn_id, str):
                raise MalformedProtocolMessage(
                    f"{method} turnId must be a string when present"
                )
            if top_level_turn_id != embedded_turn_id:
                raise AppServerProtocolError(
                    f"{method} turnId conflicts with embedded turn.id"
                )
        status = turn.get("status")
        if status not in KNOWN_TURN_STATUSES:
            raise UnknownProtocolStateError("turn.status", status, method)
        if method == "turn/started" and status != "inProgress":
            raise UnknownProtocolStateError("turn.status", status, method)
        if method == "turn/completed" and status == "inProgress":
            raise UnknownProtocolStateError("turn.status", status, method)
        _validate_turn_items(turn, method)

    if method == "account/login/completed":
        success = params.get("success")
        if not isinstance(success, bool):
            raise MalformedProtocolMessage(
                "account/login/completed requires boolean success"
            )
        login_id = params.get("loginId")
        if login_id is not None and (
            not isinstance(login_id, str) or not login_id.strip()
        ):
            raise MalformedProtocolMessage(
                "account/login/completed loginId must be non-empty text or null"
            )
        error = params.get("error")
        if error is not None and (not isinstance(error, str) or not error.strip()):
            raise MalformedProtocolMessage(
                "account/login/completed error must be non-empty text or null"
            )
        if success and error is not None:
            raise AppServerProtocolError(
                "successful account/login/completed cannot include an error"
            )
        onboarding_entrypoint = params.get("onboardingEntrypoint")
        if onboarding_entrypoint not in {None, "life_sciences"}:
            raise UnknownProtocolStateError(
                "onboardingEntrypoint",
                onboarding_entrypoint,
                "account/login/completed",
            )

    if method == "account/updated":
        auth_mode = params.get("authMode")
        if auth_mode is not None and auth_mode not in KNOWN_ACCOUNT_AUTH_MODES:
            raise UnknownProtocolStateError("authMode", auth_mode, method)
        plan_type = params.get("planType")
        if plan_type is not None and plan_type not in KNOWN_ACCOUNT_PLAN_TYPES:
            raise UnknownProtocolStateError("planType", plan_type, method)

    if method == "account/rateLimits/updated":
        validate_rate_limit_snapshot(params["rateLimits"], method=method)


def _require_nonempty_text(
    params: Mapping[str, Any], method: str, *fields: str
) -> None:
    for field in fields:
        value = params.get(field)
        if not isinstance(value, str) or not value.strip():
            raise MalformedProtocolMessage(
                f"{method} requires non-empty string {field}"
            )


def _validate_token_usage(value: Any, method: str) -> None:
    if not isinstance(value, dict) or set(value) - {
        "last",
        "total",
        "modelContextWindow",
    }:
        raise MalformedProtocolMessage(f"{method} tokenUsage has invalid shape")
    if not {"last", "total"}.issubset(value):
        raise MalformedProtocolMessage(f"{method} tokenUsage is incomplete")
    required = {
        "cachedInputTokens",
        "inputTokens",
        "outputTokens",
        "reasoningOutputTokens",
        "totalTokens",
    }
    allowed = required | {"cacheWriteInputTokens"}
    for name in ("last", "total"):
        item = value.get(name)
        if (
            not isinstance(item, dict)
            or set(item) - allowed
            or not required.issubset(item)
        ):
            raise MalformedProtocolMessage(
                f"{method} tokenUsage.{name} has invalid shape"
            )
        if any(
            isinstance(item[key], bool)
            or not isinstance(item[key], int)
            or item[key] < 0
            for key in item
        ):
            raise MalformedProtocolMessage(
                f"{method} tokenUsage.{name} requires non-negative integers"
            )
    window = value.get("modelContextWindow")
    if window is not None and (
        isinstance(window, bool) or not isinstance(window, int) or window < 0
    ):
        raise MalformedProtocolMessage(
            f"{method} modelContextWindow must be a non-negative integer or null"
        )


def _validate_notification_shape(method: str, params: JsonObject) -> None:
    shape = _SUPPORTED_NOTIFICATION_FIELDS.get(method)
    if shape is None:
        # The method itself is still pinned by KNOWN_NOTIFICATION_METHODS.
        # Only notifications that affect runtime control or product authority
        # receive a second, handwritten semantic validator here.
        return
    required, optional = shape
    if not required.issubset(params) or set(params) - required - optional:
        raise MalformedProtocolMessage(
            f"{method} params differ from the supported pinned shape"
        )
    identity_fields = tuple(
        field
        for field in ("threadId", "turnId", "itemId", "processId", "processHandle")
        if field in params
    )
    _require_nonempty_text(params, method, *identity_fields)
    if method in {
        "item/agentMessage/delta",
        "item/plan/delta",
        "item/reasoning/summaryTextDelta",
        "item/reasoning/textDelta",
    } and not isinstance(params.get("delta"), str):
        raise MalformedProtocolMessage(f"{method} delta must be text")
    for field in ("summaryIndex", "contentIndex"):
        if field in params and (
            isinstance(params[field], bool)
            or not isinstance(params[field], int)
            or params[field] < 0
        ):
            raise MalformedProtocolMessage(
                f"{method} {field} must be a non-negative integer"
            )
    if method in {"item/started", "item/completed"}:
        timestamp = params.get(
            "startedAtMs" if method == "item/started" else "completedAtMs"
        )
        if (
            isinstance(timestamp, bool)
            or not isinstance(timestamp, int)
            or timestamp < 0
        ):
            raise MalformedProtocolMessage(f"{method} timestamp must be non-negative")
        item = _require_object(params.get("item"), "item", method)
        _validate_turn_items({"items": [item]}, method)
    if method == "thread/tokenUsage/updated":
        _validate_token_usage(params.get("tokenUsage"), method)
    if method == "error" and (
        not isinstance(params.get("error"), dict)
        or not isinstance(params.get("willRetry"), bool)
    ):
        raise MalformedProtocolMessage(f"{method} has invalid error fields")
    if method in {"command/exec/outputDelta", "process/outputDelta"} and (
        not isinstance(params.get("capReached"), bool)
        or not isinstance(params.get("deltaBase64"), str)
        or params.get("stream") not in {"stdout", "stderr"}
    ):
        raise MalformedProtocolMessage(f"{method} has invalid output fields")
    if method == "process/exited" and (
        isinstance(params.get("exitCode"), bool)
        or not isinstance(params.get("exitCode"), int)
        or not isinstance(params.get("stdout"), str)
        or not isinstance(params.get("stderr"), str)
        or not isinstance(params.get("stdoutCapReached"), bool)
        or not isinstance(params.get("stderrCapReached"), bool)
    ):
        raise MalformedProtocolMessage(f"{method} has invalid result fields")
    if method in {"warning", "guardianWarning"} and not isinstance(
        params.get("message"), str
    ):
        raise MalformedProtocolMessage(f"{method} message must be text")
    if method in {"configWarning", "deprecationNotice"} and (
        not isinstance(params.get("summary"), str)
        or (
            params.get("details") is not None
            and not isinstance(params.get("details"), str)
        )
    ):
        raise MalformedProtocolMessage(f"{method} has invalid warning text")
    if (
        method == "configWarning"
        and params.get("path") is not None
        and not isinstance(params.get("path"), str)
    ):
        raise MalformedProtocolMessage("configWarning path must be text or null")
    if method == "remoteControl/status/changed":
        _require_nonempty_text(params, method, "installationId", "serverName")
        if params.get("environmentId") is not None:
            raise AppServerProtocolError(
                "remote control environment must remain unbound"
            )
        if params.get("status") != "disabled":
            raise AppServerProtocolError(
                "remote control must remain disabled for the isolated runtime"
            )
    if method in {"item/started", "item/completed"}:
        item = _require_object(params.get("item"), "item", method)
        item_type = item.get("type")
        if item_type not in KNOWN_ITEM_TYPES:
            raise UnknownProtocolStateError("item.type", item_type, method)


def _event_kind(method: str) -> ProviderEventKind:
    if method.startswith("thread/"):
        return ProviderEventKind.THREAD
    if method.startswith("turn/"):
        return ProviderEventKind.TURN
    if method.startswith("account/"):
        return ProviderEventKind.ACCOUNT
    if method.startswith(("process/", "command/exec/")):
        return ProviderEventKind.PROCESS
    if method.startswith("item/"):
        if method.endswith(("/delta", "/outputDelta")):
            return ProviderEventKind.STREAM
        if method.endswith("/progress") or "terminalInteraction" in method:
            return ProviderEventKind.TOOL
        return ProviderEventKind.ITEM
    if method.startswith(("mcpServer/", "hook/")):
        return ProviderEventKind.TOOL
    return ProviderEventKind.SYSTEM


def normalize_notification(message: Mapping[str, Any], sequence: int) -> ProviderEvent:
    """Validate and normalize one pinned App Server notification."""

    method = message.get("method")
    if not isinstance(method, str):
        raise MalformedProtocolMessage("notification method must be a string")
    known = method in KNOWN_NOTIFICATION_METHODS
    if known and set(message) - {"method", "params", "emittedAtMs"}:
        raise MalformedProtocolMessage(
            f"{method} notification has unknown top-level fields"
        )
    params = _require_object(message.get("params"), "params", method)
    emitted_at_ms = message.get("emittedAtMs")
    if emitted_at_ms is not None and (
        isinstance(emitted_at_ms, bool) or not isinstance(emitted_at_ms, int)
    ):
        raise MalformedProtocolMessage(
            f"{method} emittedAtMs must be an integer or null"
        )
    if known:
        _validate_notification_shape(method, params)
        _validate_known_states(method, params)
    return ProviderEvent(
        sequence=sequence,
        kind=_event_kind(method),
        method=method,
        params=copy.deepcopy(params),
        emitted_at_ms=emitted_at_ms,
        raw=copy.deepcopy(dict(message)),
    )


def validate_chatgpt_device_code_login_start(
    result: Mapping[str, Any], *, owner_token: object
) -> ChatgptDeviceCodeLogin:
    """Validate the exact pinned ``chatgptDeviceCode`` start response."""

    method = "account/login/start"
    expected_fields = {"type", "loginId", "userCode", "verificationUrl"}
    if set(result) != expected_fields:
        raise MalformedProtocolMessage(
            f"{method} chatgptDeviceCode result differs from the pinned shape"
        )
    if result.get("type") != "chatgptDeviceCode":
        raise UnknownProtocolStateError("type", result.get("type"), method)
    for field in ("loginId", "userCode", "verificationUrl"):
        value = result.get(field)
        if not isinstance(value, str) or not value.strip() or value != value.strip():
            raise MalformedProtocolMessage(
                f"{method} requires canonical non-empty string {field}"
            )
    verification_url = result["verificationUrl"]
    try:
        parsed = urlsplit(verification_url)
        parsed_port = parsed.port
    except ValueError as exc:
        raise MalformedProtocolMessage(
            f"{method} verificationUrl is malformed"
        ) from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or parsed_port is not None
    ):
        raise AppServerProtocolError(
            f"{method} verificationUrl must be a fragment-free HTTPS origin URL"
        )
    return ChatgptDeviceCodeLogin(
        login_id=result["loginId"],
        user_code=result["userCode"],
        verification_url=verification_url,
        _owner_token=owner_token,
    )


def validate_account_login_completion(
    params: Mapping[str, Any], *, expected_login_id: str
) -> AccountLoginCompletion:
    """Validate one terminal notification for an exact device-code login id."""

    method = "account/login/completed"
    params_object = dict(params)
    _validate_notification_shape(method, params_object)
    _validate_known_states(method, params_object)
    if params_object.get("loginId") != expected_login_id:
        raise AppServerProtocolError(
            "account/login/completed does not match the awaited login id"
        )
    success = params_object["success"]
    error = params_object.get("error")
    if not success and error is None:
        raise MalformedProtocolMessage(
            "failed account/login/completed requires a non-empty error"
        )
    return AccountLoginCompletion(success=success, error=error)


def extract_single_final_agent_message(turn: Mapping[str, Any]) -> JsonObject:
    """Return the only completed ``final_answer`` item, or fail closed.

    This is the App Server side of the completion gate.  The caller still has
    to parse ``text`` and validate it against the product's five-column
    ``StepOutputV1`` schema before committing a successful ModelCall.
    """

    status = turn.get("status")
    if status != "completed":
        raise FinalAgentMessageError(f"turn status must be 'completed', got {status!r}")
    items = turn.get("items")
    if not isinstance(items, list):
        raise FinalAgentMessageError("completed turn is missing an items array")
    final_items = [
        item
        for item in items
        if isinstance(item, dict)
        and item.get("type") == "agentMessage"
        and item.get("phase") == "final_answer"
    ]
    if len(final_items) != 1:
        raise FinalAgentMessageError(
            "completed turn must contain exactly one final_answer agentMessage; "
            f"found {len(final_items)}"
        )
    final_item = final_items[0]
    if not isinstance(final_item.get("id"), str):
        raise FinalAgentMessageError("final agentMessage requires a string id")
    if not isinstance(final_item.get("text"), str):
        raise FinalAgentMessageError("final agentMessage requires string text")
    return copy.deepcopy(final_item)
