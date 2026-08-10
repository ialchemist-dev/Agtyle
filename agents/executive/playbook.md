# Executive Agent — Playbook

## Reminder requests

Recognized shape:

```text
Remind me to <title> at <RFC 3339 timestamp>
```

Produce:

```json
{
  "kind": "task_proposal",
  "task_type": "reminder_create",
  "assigned_agent_id": "steward",
  "execution_mode": "delegated",
  "objective": "Create a reminder",
  "payload": {
    "title": "<title>",
    "scheduled_for": "<timestamp as written>",
    "timezone": "<IANA zone>"
  }
}
```

Rules:

- Copy the title exactly as the user phrased it, trimmed.
- Pass the timestamp through unchanged. Do not normalize, shift, or infer an offset.
- If the user supplied no zone and the timestamp has no offset, use the configured user
  timezone and say which zone was used.
- If the request implies recurrence, ask for a single occurrence instead: recurring reminders
  are not supported.

## Anything else

Return `clarification_required` with one specific question, or `direct_response` when the
answer needs no durable work.
