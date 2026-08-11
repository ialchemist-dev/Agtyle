"""Retry classification and backoff must be deterministic and bounded."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from agtyle.application.retry_policy import (
    RETRYABLE,
    FailureClass,
    RetryPolicy,
    classify,
)
from agtyle.domain.common import ErrorCode

NOW = datetime(2026, 8, 9, 22, 0, tzinfo=UTC)


@pytest.mark.parametrize("code", list(ErrorCode))
def test_retry_classifier_is_deterministic(code: ErrorCode) -> None:
    assert classify(code) is classify(code)


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (ErrorCode.INVALID_AGENT_OUTPUT, FailureClass.VALIDATION_PERMANENT),
        (ErrorCode.POLICY_DENIED, FailureClass.AUTHORIZATION_DENIED),
        (ErrorCode.POLICY_ENGINE_ERROR, FailureClass.AUTHORIZATION_ERROR),
        (ErrorCode.CAPABILITY_TRANSIENT_FAILURE, FailureClass.CAPABILITY_TRANSIENT),
        (ErrorCode.CAPABILITY_PERMANENT_FAILURE, FailureClass.CAPABILITY_PERMANENT),
        (ErrorCode.UNSUPPORTED_ASSIGNMENT, FailureClass.AGENT_RUNTIME_PERMANENT),
        (ErrorCode.LEASE_LOST, FailureClass.LEASE_LOST),
    ],
)
def test_specification_failure_table_is_implemented(
    code: ErrorCode, expected: FailureClass
) -> None:
    assert classify(code) is expected


@pytest.mark.parametrize("failure", list(FailureClass))
def test_only_transient_classes_are_retryable(failure: FailureClass) -> None:
    policy = RetryPolicy()
    retryable = policy.should_retry(failure, attempt_count=1, max_attempts=3)
    assert retryable is (failure in RETRYABLE)


def test_denied_and_lease_lost_never_retry_even_with_budget() -> None:
    policy = RetryPolicy()
    for failure in (FailureClass.AUTHORIZATION_DENIED, FailureClass.LEASE_LOST):
        assert not policy.should_retry(failure, attempt_count=0, max_attempts=10)


def test_retry_stops_at_the_attempt_budget() -> None:
    policy = RetryPolicy()
    assert policy.should_retry(FailureClass.CAPABILITY_TRANSIENT, attempt_count=2, max_attempts=3)
    assert not policy.should_retry(
        FailureClass.CAPABILITY_TRANSIENT, attempt_count=3, max_attempts=3
    )


def test_transient_capability_failure_requires_reconciliation_first() -> None:
    policy = RetryPolicy()
    assert policy.requires_reconciliation(FailureClass.CAPABILITY_TRANSIENT)
    assert not policy.requires_reconciliation(FailureClass.AGENT_RUNTIME_TRANSIENT)


def test_backoff_is_exponential_bounded_and_jitter_free_in_tests() -> None:
    policy = RetryPolicy(base_delay_seconds=1.0, max_delay_seconds=8.0, jitter_ratio=0.0)
    delays = [policy.delay_for(attempt, seed="task_1") for attempt in range(1, 6)]
    assert delays == [
        timedelta(seconds=1),
        timedelta(seconds=2),
        timedelta(seconds=4),
        timedelta(seconds=8),
        timedelta(seconds=8),
    ]


def test_jitter_is_reproducible_for_the_same_seed() -> None:
    policy = RetryPolicy(jitter_ratio=0.5)
    first = policy.delay_for(2, seed="task_1")
    assert first == policy.delay_for(2, seed="task_1")
    assert first != policy.delay_for(2, seed="task_2")
    assert timedelta(seconds=2) <= first <= timedelta(seconds=3)


def test_next_attempt_time_comes_from_the_injected_clock() -> None:
    policy = RetryPolicy(base_delay_seconds=2.0, jitter_ratio=0.0)
    assert policy.next_attempt_at(now=NOW, attempt_count=1) == NOW + timedelta(seconds=2)


def test_delay_requires_at_least_one_attempt() -> None:
    with pytest.raises(ValueError, match="attempt_count"):
        RetryPolicy().delay_for(0)
