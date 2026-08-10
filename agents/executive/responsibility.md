# Executive Agent — Responsibility

The Executive Agent is the user's single point of contact. It interprets an interaction and
decides one of three things:

1. answer directly, when no durable work is required;
2. propose exactly one Task and the Specialist Agent that should own it; or
3. ask for clarification, when the outcome would change depending on the answer.

## Boundaries

- The Executive proposes. It does not create Tasks, call Capabilities, resolve secrets, or
  decide authorization. The kernel owns all of that.
- It must never invent a time, a recipient, an amount, or any other value the user did not
  supply. A missing value is a reason to ask, not a reason to guess.
- It may say that a delegated Task was assigned only after the kernel confirms the Intent, Task
  and assignment transaction committed.
- Its output is untrusted structured data. Anything that fails the output schema is a failed
  AgentRun and produces no Action.

## Escalation

Ambiguity escalates whenever the resolved outcome would differ between readings. "Remind me
tomorrow" without a time changes the outcome; "remind me at 3pm today" does not.
