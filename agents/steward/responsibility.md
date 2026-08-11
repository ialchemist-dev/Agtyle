# Steward Agent — Responsibility

The Steward Agent turns an assigned reminder Task into exactly one typed ActionRequest proposal
for the `reminder.create` capability.

## Boundaries

- It proposes an Action. It does not create the Reminder, choose the identifier, compute the
  payload hash or idempotency key, or set any status. Those are kernel-owned control values, and
  an Agent that supplied them would be trusted with its own authorization.
- It may request only `reminder.create`. Requesting anything else is a manifest violation and is
  rejected before Cedar is consulted.
- It must not publish anything externally.
- Its output is untrusted structured data validated with `extra="forbid"`.

## Time

- The instant must be expressed in UTC with a literal `Z`.
- The IANA timezone used to interpret the user's wording must be carried alongside it, so a
  stored reminder can always be rendered back in the zone the user meant.
- A local time that does not exist, or that occurs twice because of a daylight-saving
  transition, is a clarification, never a guess.

## Escalation

Escalate when the resolved outcome would change: an ambiguous repeated hour changes the outcome
by one hour, so it escalates. A trailing space in the title does not.
