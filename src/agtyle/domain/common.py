"""Shared domain primitives: identifiers, time rules, errors and canonical hashing."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any, Final

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints

type JsonValue = Any
type JsonMapping = dict[str, Any]


class IdPrefix(StrEnum):
    """Type prefixes for opaque identifiers. Business logic must not parse these."""

    INTENT = "int"
    TASK = "task"
    AGENT_RUN = "run"
    ACTION_REQUEST = "act"
    POLICY_DECISION = "pol"
    APPROVAL = "apr"
    ACTION_RESULT = "res"
    ARTIFACT = "art"
    REMINDER = "rem"
    NOTIFICATION = "not"
    EVENT = "evt"


_ID_BODY: Final = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"


def id_pattern(prefix: IdPrefix) -> str:
    """The exact regular expression an identifier of this type must satisfy."""
    return rf"^{prefix.value}_{_ID_BODY}$"


# Identifier aliases are written out rather than generated so that static analysis can see them.
IntentId = Annotated[str, StringConstraints(pattern=rf"^int_{_ID_BODY}$")]
TaskId = Annotated[str, StringConstraints(pattern=rf"^task_{_ID_BODY}$")]
AgentRunId = Annotated[str, StringConstraints(pattern=rf"^run_{_ID_BODY}$")]
ActionRequestId = Annotated[str, StringConstraints(pattern=rf"^act_{_ID_BODY}$")]
PolicyDecisionId = Annotated[str, StringConstraints(pattern=rf"^pol_{_ID_BODY}$")]
ApprovalId = Annotated[str, StringConstraints(pattern=rf"^apr_{_ID_BODY}$")]
ActionResultId = Annotated[str, StringConstraints(pattern=rf"^res_{_ID_BODY}$")]
ArtifactId = Annotated[str, StringConstraints(pattern=rf"^art_{_ID_BODY}$")]
ReminderId = Annotated[str, StringConstraints(pattern=rf"^rem_{_ID_BODY}$")]
NotificationId = Annotated[str, StringConstraints(pattern=rf"^not_{_ID_BODY}$")]
EventId = Annotated[str, StringConstraints(pattern=rf"^evt_{_ID_BODY}$")]

UserId = Annotated[str, StringConstraints(min_length=1, max_length=200, strip_whitespace=True)]
AgentId = Annotated[
    str, StringConstraints(min_length=1, max_length=100, pattern=r"^[a-z][a-z0-9_]*$")
]
CapabilityName = Annotated[
    str,
    StringConstraints(
        min_length=1, max_length=100, pattern=r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$"
    ),
]

PAYLOAD_HASH_PATTERN: Final = r"^sha256:[0-9a-f]{64}$"
PayloadHash = Annotated[str, StringConstraints(pattern=PAYLOAD_HASH_PATTERN)]
IdempotencyKey = Annotated[str, StringConstraints(min_length=1, max_length=200)]


class DomainModel(BaseModel):
    """Base for every domain model: strict, immutable and explicit."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=False)


class ErrorCode(StrEnum):
    """The stable public error taxonomy. Codes are part of the external contract."""

    INVALID_REQUEST = "AGT-INPUT-001"
    CLARIFICATION_REQUIRED = "AGT-INPUT-002"
    INTERACTION_IDEMPOTENCY_CONFLICT = "AGT-INPUT-003"
    ILLEGAL_TRANSITION = "AGT-TASK-001"
    LEASE_LOST = "AGT-TASK-002"
    RETRY_EXHAUSTED = "AGT-TASK-003"
    INVALID_AGENT_OUTPUT = "AGT-AGENT-001"
    UNSUPPORTED_ASSIGNMENT = "AGT-AGENT-002"
    AGENT_RUNTIME_TIMEOUT = "AGT-AGENT-003"
    POLICY_DENIED = "AGT-POLICY-001"
    APPROVAL_REQUIRED = "AGT-POLICY-002"
    POLICY_ENGINE_ERROR = "AGT-POLICY-003"
    ACTION_SCHEMA_INVALID = "AGT-ACTION-001"
    ACTION_IDEMPOTENCY_CONFLICT = "AGT-ACTION-002"
    CAPABILITY_TRANSIENT_FAILURE = "AGT-CAP-001"
    CAPABILITY_PERMANENT_FAILURE = "AGT-CAP-002"
    NOTIFICATION_DELIVERY_FAILED = "AGT-NOTIFY-001"
    CONFIGURATION_INVALID = "AGT-SYSTEM-001"


_PUBLIC_TITLES: Final[dict[ErrorCode, str]] = {
    ErrorCode.INVALID_REQUEST: "Invalid request",
    ErrorCode.CLARIFICATION_REQUIRED: "Clarification required",
    ErrorCode.INTERACTION_IDEMPOTENCY_CONFLICT: "Idempotency key reused with a different body",
    ErrorCode.ILLEGAL_TRANSITION: "Illegal task transition",
    ErrorCode.LEASE_LOST: "Task lease lost",
    ErrorCode.RETRY_EXHAUSTED: "Task retry budget exhausted",
    ErrorCode.INVALID_AGENT_OUTPUT: "Agent produced invalid output",
    ErrorCode.UNSUPPORTED_ASSIGNMENT: "Agent cannot accept this assignment",
    ErrorCode.AGENT_RUNTIME_TIMEOUT: "Agent runtime did not answer in time",
    ErrorCode.POLICY_DENIED: "Action denied by policy",
    ErrorCode.APPROVAL_REQUIRED: "Action requires approval",
    ErrorCode.POLICY_ENGINE_ERROR: "Authorization engine error",
    ErrorCode.ACTION_SCHEMA_INVALID: "Action payload failed schema validation",
    ErrorCode.ACTION_IDEMPOTENCY_CONFLICT: "Action idempotency conflict",
    ErrorCode.CAPABILITY_TRANSIENT_FAILURE: "Capability temporarily unavailable",
    ErrorCode.CAPABILITY_PERMANENT_FAILURE: "Capability rejected the request",
    ErrorCode.NOTIFICATION_DELIVERY_FAILED: "Notification delivery failed",
    ErrorCode.CONFIGURATION_INVALID: "Invalid configuration",
}


class AgtyleError(Exception):
    """Base application error carrying a stable public code and a safe message.

    ``detail`` is safe to return to a caller. Stack traces, policy bodies, command
    lines and secrets stay in diagnostic logs and must never be placed here.
    """

    code: ErrorCode = ErrorCode.INVALID_REQUEST

    def __init__(
        self,
        detail: str,
        *,
        code: ErrorCode | None = None,
        task_id: str | None = None,
    ) -> None:
        self.code = code or type(self).code
        self.detail = detail
        self.task_id = task_id
        super().__init__(f"{self.code.value}: {detail}")

    @property
    def title(self) -> str:
        return _PUBLIC_TITLES[self.code]

    def to_public_dict(self) -> JsonMapping:
        payload: JsonMapping = {
            "code": self.code.value,
            "title": self.title,
            "detail": self.detail,
        }
        if self.task_id is not None:
            payload["task_id"] = self.task_id
        return payload


class InvalidRequestError(AgtyleError):
    code = ErrorCode.INVALID_REQUEST


class ClarificationRequiredError(AgtyleError):
    code = ErrorCode.CLARIFICATION_REQUIRED


class InteractionIdempotencyConflictError(AgtyleError):
    code = ErrorCode.INTERACTION_IDEMPOTENCY_CONFLICT


class IllegalTransitionError(AgtyleError):
    code = ErrorCode.ILLEGAL_TRANSITION


class LeaseLostError(AgtyleError):
    code = ErrorCode.LEASE_LOST


class RetryExhaustedError(AgtyleError):
    code = ErrorCode.RETRY_EXHAUSTED


class InvalidAgentOutputError(AgtyleError):
    code = ErrorCode.INVALID_AGENT_OUTPUT


class UnsupportedAssignmentError(AgtyleError):
    code = ErrorCode.UNSUPPORTED_ASSIGNMENT


class AgentRuntimeTimeoutError(AgtyleError):
    """The Agent runtime exceeded its budget. Transient by nature, so a retry is allowed."""

    code = ErrorCode.AGENT_RUNTIME_TIMEOUT


class PolicyDeniedError(AgtyleError):
    code = ErrorCode.POLICY_DENIED


class ApprovalRequiredError(AgtyleError):
    code = ErrorCode.APPROVAL_REQUIRED


class PolicyEngineError(AgtyleError):
    code = ErrorCode.POLICY_ENGINE_ERROR


class ActionSchemaInvalidError(AgtyleError):
    code = ErrorCode.ACTION_SCHEMA_INVALID


class ActionIdempotencyConflictError(AgtyleError):
    code = ErrorCode.ACTION_IDEMPOTENCY_CONFLICT


class CapabilityTransientError(AgtyleError):
    code = ErrorCode.CAPABILITY_TRANSIENT_FAILURE


class CapabilityPermanentError(AgtyleError):
    code = ErrorCode.CAPABILITY_PERMANENT_FAILURE


class NotificationDeliveryError(AgtyleError):
    code = ErrorCode.NOTIFICATION_DELIVERY_FAILED


class ConfigurationInvalidError(AgtyleError):
    code = ErrorCode.CONFIGURATION_INVALID


# --------------------------------------------------------------------------------------
# Time
# --------------------------------------------------------------------------------------

TIMESTAMP_FORMAT: Final = "%Y-%m-%dT%H:%M:%S.%fZ"


def require_aware(value: datetime, *, field: str) -> datetime:
    """Reject naive datetimes at every boundary and normalize to UTC."""
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise InvalidRequestError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def to_storage(value: datetime) -> str:
    """Render an aware datetime as the single canonical UTC storage representation."""
    return require_aware(value, field="timestamp").strftime(TIMESTAMP_FORMAT)


def from_storage(value: str) -> datetime:
    """Parse the canonical UTC storage representation back into an aware datetime."""
    return datetime.strptime(value, TIMESTAMP_FORMAT).replace(tzinfo=UTC)


UtcDatetime = Annotated[AwareDatetime, Field()]


# --------------------------------------------------------------------------------------
# Canonical JSON and hashing
# --------------------------------------------------------------------------------------


def canonical_json(document: JsonValue) -> str:
    """Serialize a JSON document using RFC 8785 canonicalization semantics.

    Object keys are sorted by UTF-16 code unit, separators carry no insignificant
    whitespace, and non-ASCII characters are emitted literally as UTF-8.
    """
    return json.dumps(
        _canonicalize(document),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _canonicalize(document: JsonValue) -> JsonValue:
    """Reject values that have no canonical JSON form before serialization."""
    if document is None or isinstance(document, (str, bool)):
        return document
    if isinstance(document, int):
        return document
    if isinstance(document, float):
        if document != document or document in (float("inf"), float("-inf")):
            raise InvalidRequestError("payload contains a non-finite number")
        return document
    if isinstance(document, dict):
        canonical: dict[str, JsonValue] = {}
        for key, value in document.items():
            if not isinstance(key, str):
                raise InvalidRequestError("payload object keys must be strings")
            canonical[key] = _canonicalize(value)
        return canonical
    if isinstance(document, (list, tuple)):
        return [_canonicalize(item) for item in document]
    if isinstance(document, datetime):
        raise InvalidRequestError("payload datetimes must be serialized before hashing")
    raise InvalidRequestError(f"payload contains an unsupported type: {type(document).__name__}")


def sha256_hex(data: str) -> str:
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def hash_document(document: JsonValue) -> str:
    """Return ``sha256:<lowercase hex>`` over the canonical JSON form."""
    return f"sha256:{sha256_hex(canonical_json(document))}"


_SECRET_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"(?i)\b(sk-[A-Za-z0-9_-]{16,})"),
    re.compile(r"(?i)\b(gh[pousr]_[A-Za-z0-9]{20,})"),
    re.compile(r"(?i)\b(AKIA[0-9A-Z]{16})"),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/-]{16,}"),
    re.compile(r"(?i)(\"?(?:api[_-]?key|secret|password|token)\"?\s*[:=]\s*\"?)([^\s\",]{8,})"),
)

REDACTED: Final = "[REDACTED]"


def redact_secrets(text: str) -> str:
    """Best-effort removal of credential-shaped substrings from log and error text."""
    redacted = text
    for pattern in _SECRET_PATTERNS:
        if pattern.groups >= 2:
            redacted = pattern.sub(lambda match: f"{match.group(1)}{REDACTED}", redacted)
        else:
            redacted = pattern.sub(REDACTED, redacted)
    return redacted
