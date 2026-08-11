"""Reminder domain rules, including timezone and daylight-saving correctness."""

from __future__ import annotations

import re
import zoneinfo
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Final

from pydantic import Field, StringConstraints, field_validator

from agtyle.domain.common import (
    ClarificationRequiredError,
    DomainModel,
    IdempotencyKey,
    InvalidRequestError,
    ReminderId,
    TaskId,
    UserId,
    UtcDatetime,
    require_aware,
)

MAX_TITLE_LENGTH: Final = 200
MAX_NOTE_LENGTH: Final = 4_000

RECURRENCE_FIELDS: Final[frozenset[str]] = frozenset(
    {"recurrence", "rrule", "repeat", "recurring", "frequency"}
)


class ReminderStatus(StrEnum):
    SCHEDULED = "scheduled"
    FIRING = "firing"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"
    FAILED = "failed"


TERMINAL_REMINDER_STATUSES: Final[frozenset[ReminderStatus]] = frozenset(
    {ReminderStatus.DELIVERED, ReminderStatus.CANCELLED}
)

ALLOWED_REMINDER_TRANSITIONS: Final[dict[ReminderStatus, frozenset[ReminderStatus]]] = {
    ReminderStatus.SCHEDULED: frozenset(
        {ReminderStatus.FIRING, ReminderStatus.CANCELLED, ReminderStatus.FAILED}
    ),
    ReminderStatus.FIRING: frozenset(
        {ReminderStatus.DELIVERED, ReminderStatus.FAILED, ReminderStatus.SCHEDULED}
    ),
    ReminderStatus.DELIVERED: frozenset(),
    ReminderStatus.CANCELLED: frozenset(),
    ReminderStatus.FAILED: frozenset(),
}


def validate_timezone(name: str) -> zoneinfo.ZoneInfo:
    try:
        return zoneinfo.ZoneInfo(name)
    except Exception as exc:
        raise InvalidRequestError(f"unknown IANA timezone: {name!r}") from exc


def is_ambiguous_local_time(local: datetime) -> bool:
    """True when the wall-clock time occurs twice, as at the end of daylight-saving time."""
    if local.tzinfo is None:
        raise InvalidRequestError("ambiguity can only be checked on a zone-attached datetime")
    return local.replace(fold=0).utcoffset() != local.replace(fold=1).utcoffset()


def is_nonexistent_local_time(local: datetime) -> bool:
    """True when the wall-clock time is skipped, as at the start of daylight-saving time."""
    if local.tzinfo is None:
        raise InvalidRequestError("existence can only be checked on a zone-attached datetime")
    normalized = local.astimezone(UTC).astimezone(local.tzinfo)
    return normalized.replace(fold=0) != local.replace(fold=0)


def resolve_local_time(
    *,
    local_naive: datetime,
    timezone_name: str,
    disambiguation: str | None = None,
) -> datetime:
    """Convert a naive local wall-clock time to UTC, refusing to guess across DST edges.

    ``disambiguation`` accepts ``"earlier"`` or ``"later"`` to resolve a repeated hour.
    A nonexistent local time always requires clarification because no instant matches it.
    """
    if local_naive.tzinfo is not None:
        raise InvalidRequestError("resolve_local_time expects a naive local wall-clock time")
    zone = validate_timezone(timezone_name)
    attached = local_naive.replace(tzinfo=zone)

    if is_nonexistent_local_time(attached):
        raise ClarificationRequiredError(
            f"{local_naive.isoformat()} does not exist in {timezone_name} because of a "
            "daylight-saving transition; ask the user for a different time"
        )
    if is_ambiguous_local_time(attached):
        if disambiguation == "earlier":
            attached = attached.replace(fold=0)
        elif disambiguation == "later":
            attached = attached.replace(fold=1)
        else:
            raise ClarificationRequiredError(
                f"{local_naive.isoformat()} occurs twice in {timezone_name}; supply a UTC offset "
                "or choose 'earlier' or 'later'"
            )
    return attached.astimezone(UTC)


def normalize_title(raw: str) -> str:
    title = raw.strip()
    if not title:
        raise InvalidRequestError("reminder title must not be empty after trimming")
    if len(title) > MAX_TITLE_LENGTH:
        raise InvalidRequestError(f"reminder title must be at most {MAX_TITLE_LENGTH} characters")
    return title


def normalize_note(raw: str | None) -> str | None:
    if raw is None:
        return None
    note = raw.strip()
    if not note:
        return None
    if len(note) > MAX_NOTE_LENGTH:
        raise InvalidRequestError(f"reminder note must be at most {MAX_NOTE_LENGTH} characters")
    return note


def reject_recurrence(payload: dict[str, object]) -> None:
    """Recurring reminders are explicitly out of the baseline and must not be silently dropped."""
    present = sorted(RECURRENCE_FIELDS.intersection(payload))
    if present:
        raise InvalidRequestError(
            f"recurring reminders are not supported; remove {', '.join(present)}"
        )


def require_future_due_time(scheduled_for_utc: datetime, *, now: datetime) -> datetime:
    due = require_aware(scheduled_for_utc, field="scheduled_for_utc")
    if due <= require_aware(now, field="now"):
        raise InvalidRequestError("reminder due time must be later than the evaluation time")
    return due


class Reminder(DomainModel):
    """A one-time reminder owned by a user and traceable to the Task that created it."""

    id: ReminderId
    user_id: UserId
    source_task_id: TaskId
    title: Annotated[str, StringConstraints(min_length=1, max_length=MAX_TITLE_LENGTH)]
    note: Annotated[str, StringConstraints(max_length=MAX_NOTE_LENGTH)] | None = None
    scheduled_for_utc: UtcDatetime
    timezone: str
    status: ReminderStatus
    idempotency_key: IdempotencyKey
    firing_lease_owner: str | None = None
    firing_lease_expires_at: UtcDatetime | None = None
    created_at: UtcDatetime
    updated_at: UtcDatetime
    delivered_at: UtcDatetime | None = None
    row_version: Annotated[int, Field(ge=1)] = 1

    @field_validator("timezone")
    @classmethod
    def _validate_timezone(cls, value: str) -> str:
        validate_timezone(value)
        return value

    @field_validator("title")
    @classmethod
    def _validate_title(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("reminder title must already be trimmed")
        return value

    def _transition(self, target: ReminderStatus, *, now: datetime, **changes: object) -> Reminder:
        if target not in ALLOWED_REMINDER_TRANSITIONS[self.status]:
            raise InvalidRequestError(
                f"reminder cannot move from {self.status.value} to {target.value}"
            )
        return self.model_copy(
            update={
                "status": target,
                "updated_at": require_aware(now, field="now"),
                "row_version": self.row_version + 1,
                **changes,
            }
        )

    def begin_firing(
        self, *, now: datetime, lease_owner: str, lease_expires_at: datetime
    ) -> Reminder:
        return self._transition(
            ReminderStatus.FIRING,
            now=now,
            firing_lease_owner=lease_owner,
            firing_lease_expires_at=require_aware(lease_expires_at, field="lease_expires_at"),
        )

    def mark_delivered(self, *, now: datetime) -> Reminder:
        moment = require_aware(now, field="now")
        return self._transition(
            ReminderStatus.DELIVERED,
            now=moment,
            delivered_at=moment,
            firing_lease_owner=None,
            firing_lease_expires_at=None,
        )

    def mark_failed(self, *, now: datetime) -> Reminder:
        return self._transition(
            ReminderStatus.FAILED,
            now=now,
            firing_lease_owner=None,
            firing_lease_expires_at=None,
        )

    def cancel(self, *, now: datetime) -> Reminder:
        return self._transition(ReminderStatus.CANCELLED, now=now)

    def is_due(self, *, now: datetime) -> bool:
        return self.status is ReminderStatus.SCHEDULED and self.scheduled_for_utc <= require_aware(
            now, field="now"
        )

    @property
    def is_terminal(self) -> bool:
        return not ALLOWED_REMINDER_TRANSITIONS[self.status]

    def local_time(self) -> datetime:
        return self.scheduled_for_utc.astimezone(validate_timezone(self.timezone))

    def matches_protected_fields(self, other: Reminder) -> bool:
        """Two reminders sharing an idempotency key must agree on every user-visible field."""
        return (
            self.user_id == other.user_id
            and self.title == other.title
            and self.note == other.note
            and self.scheduled_for_utc == other.scheduled_for_utc
            and self.timezone == other.timezone
        )


UTC_INSTANT_PATTERN: Final = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,6})?Z$"
)


def parse_utc_instant(raw: str) -> datetime:
    """Parse the single accepted instant form: RFC 3339 UTC with a literal ``Z``.

    Offsets such as ``-06:00`` are rejected on the wire so that the stored value and the
    hashed payload have exactly one representation.
    """
    if not UTC_INSTANT_PATTERN.match(raw):
        raise InvalidRequestError(
            "scheduled_for_utc must be an RFC 3339 UTC instant ending in 'Z', "
            "for example 2026-08-10T21:00:00Z"
        )
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


class ReminderCreatePayload(DomainModel):
    """The protected payload of a ``reminder.create`` ActionRequest.

    This model and ``contracts/schemas/v1/reminder-create.schema.json`` must accept and
    reject exactly the same documents; a contract test proves it over every fixture.
    """

    title: Annotated[str, StringConstraints(min_length=1, max_length=MAX_TITLE_LENGTH)]
    note: Annotated[str, StringConstraints(max_length=MAX_NOTE_LENGTH)] | None = None
    scheduled_for_utc: str
    timezone: str

    @field_validator("title")
    @classmethod
    def _title_is_trimmed(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("title must be trimmed")
        return value

    @field_validator("scheduled_for_utc")
    @classmethod
    def _instant_is_utc_z(cls, value: str) -> str:
        parse_utc_instant(value)
        return value

    @field_validator("timezone")
    @classmethod
    def _timezone_is_iana(cls, value: str) -> str:
        validate_timezone(value)
        return value

    @property
    def instant(self) -> datetime:
        return parse_utc_instant(self.scheduled_for_utc)
