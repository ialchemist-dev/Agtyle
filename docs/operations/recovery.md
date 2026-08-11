# Recovery

Agtyle assumes processes die. Nothing in the system depends on a Worker shutting down cleanly.

## What a crash leaves behind

| Moment a process dies | Durable state afterwards | How it resolves |
|---|---|---|
| After claiming a Task, before the Agent answers | Task `running` with an expiring lease; AgentRun `running` | Lease expires, recovery marks the run `abandoned` and returns the Task to `assigned` |
| After the Agent answers, before the ActionRequest commits | No ActionRequest | The retry proposes again; the same payload produces the same idempotency key |
| After the PolicyDecision commits, before the capability runs | ActionRequest and PolicyDecision exist; no effect | The retry reuses the same ActionRequest and reauthorizes it |
| After the capability applied its effect, before completion commits | The Reminder exists; no ActionResult, no Notification, Task not complete | The retry observes the existing Reminder by idempotency key and completes without duplicating it |
| After completion commits, before delivery | Task `completed`, Notification `pending` | The Notification Worker delivers it |
| After the adapter observed a delivery, before the commit that records it | Notification `delivering` with an expiring lease | The lease expires, the retry calls the adapter with the same delivery key, and an idempotent adapter suppresses the duplicate |
| After a Reminder fired, before the due delivery | Reminder `firing`, due Notification `pending` | The Notification Worker delivers it; the Scheduler will not fire it again |

## Reclaiming abandoned work

```bash
agtyle recover
```

This scans for Tasks left `running` past their lease and Notifications left `delivering` past
theirs. Each Task's in-flight AgentRun is marked `abandoned`, then the Task returns to `assigned`
if retry budget remains or fails terminally if it does not. A terminal failure appends a failure
Event and creates one terminal Notification.

The Task Worker performs the same scan at the start of every iteration, so a running system
recovers by itself; the command exists for operators who want it to happen now.

Recovery never deletes or rewrites history. Events are append-only in the repository API and are
additionally protected by database triggers that reject `UPDATE` and `DELETE`.

## Diagnosing a failed Task

```bash
agtyle task timeline <task_id>
```

Read it in this order:

1. `current_state.last_error_code` — the stable public code (see §11.2 of the specification).
2. The `agent_run` entries — how many attempts happened, and how each ended.
3. The `policy_decision` entries — whether Cedar allowed, denied, or could not decide. A
   `decision=error` means authorization failed closed; it is not the same as a denial.
4. The `action_request` entry — the payload hash and idempotency key, which tell you whether a
   retry would be the same logical Action.
5. The `action_result` entry — whether the effect actually happened.

If there is a PolicyDecision but no ActionResult, no effect occurred. That is the invariant the
whole design protects.

## Why a role refuses to start

Any role that accepts or executes work — API, Task Worker, Scheduler, Notification Worker —
runs one startup gate before it does anything. It refuses to start when:

- the database is not migrated to head;
- an enabled registry snapshot disagrees with the configured manifests (§10.6);
- the Cedar binary is missing, the wrong version, or the policy set does not validate (§15.5).

The failure is an `AGT-SYSTEM-001` document naming the problem. Readiness renders the same
report, so `/health/ready` and startup can never disagree.

Refusing to start, rather than only reporting `503`, is deliberate: a local-first deployment has
no load balancer to honour an unready signal, so a process that merely reported itself unready
would still be serving requests.

Operator tooling deliberately stays outside the gate, because its whole purpose is to diagnose
and repair a system too broken to start:

```bash
agtyle validate          # report every startup check without enforcing it
agtyle registry check    # is the snapshot still in agreement?
agtyle recover           # reclaim abandoned work
agtyle task timeline ... # explain what happened
```

## When Cedar is misconfigured

Invalid Cedar configuration makes `/health/ready` return non-200 and prevents Workers from
executing Actions. It does not delete or mutate queued Tasks: they simply wait. Fix the policy
set, re-run `agtyle validate`, and the queue drains.

```bash
agtyle validate       # reports the engine version, policy ids and any validation error
```
