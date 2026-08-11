# Steward Agent — Playbook

## reminder_create

Given an assignment payload containing `title`, `scheduled_for` and `timezone`:

1. Trim the title. Reject an empty title and one longer than 200 characters.
2. Resolve `scheduled_for` to a UTC instant.
   - An explicit offset or `Z` is authoritative; convert it directly.
   - A bare local time is interpreted in `timezone`. If that local time does not exist, or
     occurs twice, escalate instead of choosing.
3. Confirm the instant is later than now. A past reminder is a rejection, not a silent no-op.
4. Reject any recurrence field.
5. Return exactly one proposal:

```json
{
  "kind": "action_request",
  "capability": "reminder.create",
  "schema_version": 1,
  "resource_type": "ReminderCollection",
  "resource_id": "<user id>",
  "payload": {
    "title": "submit the report",
    "note": null,
    "scheduled_for_utc": "2026-08-10T21:00:00Z",
    "timezone": "America/Denver"
  }
}
```

Never emit an identifier, hash, idempotency key, task link or status. The kernel supplies those.
