"""Deterministic failure classification and backoff.

Retry behavior is a policy object rather than scattered conditionals so that the Worker's
decisions are testable without a database, a clock or an external service.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from enum import StrEnum

from agtyle.domain.common import (
    DomainModel,
    ErrorCode,
    require_aware,
)


class FailureClass(StrEnum):
    """Why an attempt failed, which determines whether another attempt is allowed."""

    VALIDATION_PERMANENT = "validation_permanent"
    AUTHORIZATION_DENIED = "authorization_denied"
    AUTHORIZATION_ERROR = "authorization_error"
    CAPABILITY_TRANSIENT = "capability_transient"
    CAPABILITY_PERMANENT = "capability_permanent"
    AGENT_RUNTIME_TRANSIENT = "agent_runtime_transient"
    AGENT_RUNTIME_PERMANENT = "agent_runtime_permanent"
    LEASE_LOST = "lease_lost"


RETRYABLE: frozenset[FailureClass] = frozenset(
    {
        FailureClass.AUTHORIZATION_ERROR,
        FailureClass.CAPABILITY_TRANSIENT,
        FailureClass.AGENT_RUNTIME_TRANSIENT,
    }
)

RECONCILE_BEFORE_RETRY: frozenset[FailureClass] = frozenset({FailureClass.CAPABILITY_TRANSIENT})

_ERROR_CODE_TO_CLASS: dict[ErrorCode, FailureClass] = {
    ErrorCode.INVALID_REQUEST: FailureClass.VALIDATION_PERMANENT,
    ErrorCode.CLARIFICATION_REQUIRED: FailureClass.VALIDATION_PERMANENT,
    ErrorCode.ACTION_SCHEMA_INVALID: FailureClass.VALIDATION_PERMANENT,
    ErrorCode.ACTION_IDEMPOTENCY_CONFLICT: FailureClass.VALIDATION_PERMANENT,
    ErrorCode.INVALID_AGENT_OUTPUT: FailureClass.VALIDATION_PERMANENT,
    ErrorCode.UNSUPPORTED_ASSIGNMENT: FailureClass.AGENT_RUNTIME_PERMANENT,
    ErrorCode.AGENT_RUNTIME_TIMEOUT: FailureClass.AGENT_RUNTIME_TRANSIENT,
    ErrorCode.POLICY_DENIED: FailureClass.AUTHORIZATION_DENIED,
    ErrorCode.APPROVAL_REQUIRED: FailureClass.AUTHORIZATION_DENIED,
    ErrorCode.POLICY_ENGINE_ERROR: FailureClass.AUTHORIZATION_ERROR,
    ErrorCode.CAPABILITY_TRANSIENT_FAILURE: FailureClass.CAPABILITY_TRANSIENT,
    ErrorCode.CAPABILITY_PERMANENT_FAILURE: FailureClass.CAPABILITY_PERMANENT,
    ErrorCode.LEASE_LOST: FailureClass.LEASE_LOST,
    ErrorCode.RETRY_EXHAUSTED: FailureClass.VALIDATION_PERMANENT,
    ErrorCode.ILLEGAL_TRANSITION: FailureClass.VALIDATION_PERMANENT,
    ErrorCode.CONFIGURATION_INVALID: FailureClass.AUTHORIZATION_ERROR,
    ErrorCode.NOTIFICATION_DELIVERY_FAILED: FailureClass.CAPABILITY_TRANSIENT,
    ErrorCode.INTERACTION_IDEMPOTENCY_CONFLICT: FailureClass.VALIDATION_PERMANENT,
}


def classify(error_code: ErrorCode) -> FailureClass:
    """Map a stable error code to its failure class. Unknown codes fail closed as permanent."""
    return _ERROR_CODE_TO_CLASS.get(error_code, FailureClass.VALIDATION_PERMANENT)


class RetryPolicy(DomainModel):
    """Bounded exponential backoff with deterministic, injectable jitter.

    ``jitter_ratio`` is zero in tests, so the schedule is exactly reproducible. In production a
    non-zero ratio spreads retries deterministically per identifier rather than randomly, which
    keeps the same input producing the same schedule while still avoiding a thundering herd.
    """

    base_delay_seconds: float = 1.0
    max_delay_seconds: float = 60.0
    jitter_ratio: float = 0.0

    def should_retry(self, failure: FailureClass, *, attempt_count: int, max_attempts: int) -> bool:
        if failure not in RETRYABLE:
            return False
        return attempt_count < max_attempts

    def requires_reconciliation(self, failure: FailureClass) -> bool:
        return failure in RECONCILE_BEFORE_RETRY

    def delay_for(self, attempt_count: int, *, seed: str = "") -> timedelta:
        """Return the wait before the next attempt. ``attempt_count`` is 1 after one failure."""
        if attempt_count < 1:
            raise ValueError("attempt_count must be at least 1")
        raw = self.base_delay_seconds * (2 ** (attempt_count - 1))
        bounded = min(raw, self.max_delay_seconds)
        return timedelta(seconds=bounded + self._jitter(bounded, seed))

    def next_attempt_at(self, *, now: datetime, attempt_count: int, seed: str = "") -> datetime:
        return require_aware(now, field="now") + self.delay_for(attempt_count, seed=seed)

    def _jitter(self, bounded: float, seed: str) -> float:
        if self.jitter_ratio <= 0 or not seed:
            return 0.0
        digest = hashlib.sha256(f"{seed}:{bounded}".encode()).digest()
        fraction = int.from_bytes(digest[:4], "big") / 0xFFFFFFFF
        return bounded * self.jitter_ratio * fraction
