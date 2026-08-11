"""Time correctness: aware datetimes only, future due times, and honest DST handling."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from agtyle.domain.common import (
    ClarificationRequiredError,
    InvalidRequestError,
    from_storage,
    require_aware,
    to_storage,
)
from agtyle.domain.reminders import (
    MAX_NOTE_LENGTH,
    MAX_TITLE_LENGTH,
    Reminder,
    ReminderStatus,
    is_ambiguous_local_time,
    is_nonexistent_local_time,
    normalize_note,
    normalize_title,
    parse_utc_instant,
    reject_recurrence,
    require_future_due_time,
    resolve_local_time,
    validate_timezone,
)

NOW = datetime(2026, 8, 9, 22, 0, tzinfo=UTC)
DUE = datetime(2026, 8, 10, 21, 0, tzinfo=UTC)


def make_reminder(**overrides: object) -> Reminder:
    base: dict[str, object] = {
        "id": "rem_00000000-0000-7000-8000-000000000001",
        "user_id": "user_local",
        "source_task_id": "task_00000000-0000-7000-8000-000000000001",
        "title": "submit the report",
        "note": None,
        "scheduled_for_utc": DUE,
        "timezone": "America/Denver",
        "status": ReminderStatus.SCHEDULED,
        "idempotency_key": "k" * 64,
        "created_at": NOW,
        "updated_at": NOW,
    }
    base.update(overrides)
    return Reminder(**base)  # type: ignore[arg-type]


def test_naive_datetime_is_rejected() -> None:
    with pytest.raises(InvalidRequestError):
        require_aware(datetime(2026, 8, 9, 22, 0), field="scheduled_for_utc")


def test_storage_round_trip_preserves_microseconds_in_utc() -> None:
    value = datetime(2026, 8, 9, 22, 0, 0, 123456, tzinfo=UTC)
    assert to_storage(value) == "2026-08-09T22:00:00.123456Z"
    assert from_storage(to_storage(value)) == value


def test_storage_normalizes_a_non_utc_offset_to_utc() -> None:
    denver = datetime(2026, 8, 10, 15, 0, tzinfo=validate_timezone("America/Denver"))
    assert to_storage(denver) == "2026-08-10T21:00:00.000000Z"


def test_due_time_must_be_future() -> None:
    with pytest.raises(InvalidRequestError):
        require_future_due_time(NOW, now=NOW)
    with pytest.raises(InvalidRequestError):
        require_future_due_time(NOW - timedelta(microseconds=1), now=NOW)
    assert require_future_due_time(NOW + timedelta(microseconds=1), now=NOW)


def test_dst_ambiguous_time_requires_disambiguation() -> None:
    # 2026-11-01 01:30 occurs twice in America/Denver.
    ambiguous = datetime(2026, 11, 1, 1, 30)
    assert is_ambiguous_local_time(ambiguous.replace(tzinfo=validate_timezone("America/Denver")))
    with pytest.raises(ClarificationRequiredError):
        resolve_local_time(local_naive=ambiguous, timezone_name="America/Denver")

    earlier = resolve_local_time(
        local_naive=ambiguous, timezone_name="America/Denver", disambiguation="earlier"
    )
    later = resolve_local_time(
        local_naive=ambiguous, timezone_name="America/Denver", disambiguation="later"
    )
    assert earlier == datetime(2026, 11, 1, 7, 30, tzinfo=UTC)
    assert later == datetime(2026, 11, 1, 8, 30, tzinfo=UTC)
    assert later - earlier == timedelta(hours=1)


def test_dst_nonexistent_time_requires_clarification() -> None:
    # 2026-03-08 02:30 is skipped in America/Denver.
    skipped = datetime(2026, 3, 8, 2, 30)
    assert is_nonexistent_local_time(skipped.replace(tzinfo=validate_timezone("America/Denver")))
    with pytest.raises(ClarificationRequiredError):
        resolve_local_time(local_naive=skipped, timezone_name="America/Denver")
    with pytest.raises(ClarificationRequiredError):
        resolve_local_time(
            local_naive=skipped, timezone_name="America/Denver", disambiguation="later"
        )


def test_unambiguous_local_time_resolves_without_disambiguation() -> None:
    assert resolve_local_time(
        local_naive=datetime(2026, 8, 10, 15, 0), timezone_name="America/Denver"
    ) == datetime(2026, 8, 10, 21, 0, tzinfo=UTC)


def test_resolve_local_time_rejects_an_aware_input() -> None:
    with pytest.raises(InvalidRequestError):
        resolve_local_time(local_naive=NOW, timezone_name="America/Denver")


def test_unknown_timezone_is_rejected() -> None:
    with pytest.raises(InvalidRequestError):
        validate_timezone("Mars/Olympus_Mons")


def test_title_is_trimmed_and_bounded() -> None:
    assert normalize_title("  submit the report \n") == "submit the report"
    with pytest.raises(InvalidRequestError):
        normalize_title("   ")
    with pytest.raises(InvalidRequestError):
        normalize_title("a" * (MAX_TITLE_LENGTH + 1))
    assert len(normalize_title("a" * MAX_TITLE_LENGTH)) == MAX_TITLE_LENGTH


def test_note_is_optional_and_bounded() -> None:
    assert normalize_note(None) is None
    assert normalize_note("   ") is None
    assert normalize_note(" keep ") == "keep"
    with pytest.raises(InvalidRequestError):
        normalize_note("n" * (MAX_NOTE_LENGTH + 1))


@pytest.mark.parametrize("field", ["recurrence", "rrule", "repeat", "recurring", "frequency"])
def test_recurrence_is_rejected_as_unsupported(field: str) -> None:
    with pytest.raises(InvalidRequestError, match="recurring"):
        reject_recurrence({"title": "x", field: "FREQ=DAILY"})


def test_non_recurring_payload_passes_the_recurrence_gate() -> None:
    reject_recurrence({"title": "x", "timezone": "UTC"})


@pytest.mark.parametrize(
    "raw",
    [
        "2026-08-10T15:00:00-06:00",
        "2026-08-10T21:00:00",
        "2026-08-10 21:00:00Z",
        "2026-08-10T21:00:00.1234567Z",
        "not-a-time",
    ],
)
def test_only_rfc3339_utc_instants_are_accepted(raw: str) -> None:
    with pytest.raises(InvalidRequestError):
        parse_utc_instant(raw)


def test_utc_instant_accepts_optional_microseconds() -> None:
    assert parse_utc_instant("2026-08-10T21:00:00Z") == DUE
    assert parse_utc_instant("2026-08-10T21:00:00.500000Z") == DUE + timedelta(microseconds=500000)


def test_reminder_stores_the_timezone_used_to_interpret_it() -> None:
    reminder = make_reminder()
    assert reminder.timezone == "America/Denver"
    assert reminder.local_time().isoformat() == "2026-08-10T15:00:00-06:00"


def test_reminder_delivery_lifecycle_is_terminal() -> None:
    scheduled = make_reminder()
    assert scheduled.is_due(now=DUE)
    assert not scheduled.is_due(now=DUE - timedelta(microseconds=1))

    firing = scheduled.begin_firing(
        now=DUE, lease_owner="scheduler-1", lease_expires_at=DUE + timedelta(seconds=30)
    )
    delivered = firing.mark_delivered(now=DUE + timedelta(seconds=1))
    assert delivered.status is ReminderStatus.DELIVERED
    assert delivered.delivered_at == DUE + timedelta(seconds=1)
    assert delivered.is_terminal
    with pytest.raises(InvalidRequestError):
        delivered.cancel(now=DUE)


def test_cancelled_reminder_is_terminal() -> None:
    cancelled = make_reminder().cancel(now=NOW)
    assert cancelled.is_terminal
    with pytest.raises(InvalidRequestError):
        cancelled.begin_firing(
            now=DUE, lease_owner="s", lease_expires_at=DUE + timedelta(seconds=30)
        )


def test_reminder_rejects_an_untrimmed_title() -> None:
    with pytest.raises(Exception):  # noqa: B017 - pydantic ValidationError
        make_reminder(title="  padded  ")


def test_protected_field_comparison_detects_a_changed_payload() -> None:
    original = make_reminder()
    assert original.matches_protected_fields(make_reminder())
    assert not original.matches_protected_fields(make_reminder(title="different"))
    assert not original.matches_protected_fields(make_reminder(timezone="UTC"))
