# Local development

Agtyle is local-first: one directory and one SQLite database are enough to run, back up and
inspect the whole system.

## Prerequisites

- [`uv`](https://docs.astral.sh/uv/) — provisions the pinned CPython 3.12 and the locked
  dependency set.
- `make`.
- Network access for the first `make bootstrap` only. Everything after that runs offline.

## First run

```bash
make bootstrap     # locked Python dependencies plus the pinned Cedar 4.12.0 CLI
make init          # data directory, migrations, registry and Cedar policy validation
make verify        # lint, types, migrations, Cedar matrix, every deterministic test
make demo-reminder # four real processes, wall-clock scheduling, restart proof
```

`make bootstrap` is idempotent. Re-running it re-verifies the Cedar checksum and leaves an
existing valid installation untouched if a download ever fails verification.

## Running the roles

Each role is a separate operating-system process sharing the same modules and database:

```bash
agtyle api --port 8000     # Interaction Gateway
agtyle worker              # Task Worker
agtyle scheduler           # due Reminder Scheduler
agtyle notifications       # Notification Worker
```

Add `--once` to `worker`, `scheduler` or `notifications` to process at most one item and exit.
A `--once` command exits 0 whenever it polled successfully, including when there was no work,
so it composes cleanly with `cron`, a supervisor, or a test.

## Creating a reminder

The Executive Agent is deterministic in this baseline and recognizes exactly one phrasing:

```text
Remind me to <what> at <RFC 3339 timestamp>
```

The timestamp must be in the future, so generate it rather than pasting a fixed date:

```bash
DUE=$(python -c "import datetime as d; print((d.datetime.now(d.UTC) + d.timedelta(minutes=2)).strftime('%Y-%m-%dT%H:%M:%SZ'))")

curl -sS -X POST http://127.0.0.1:8000/v1/interactions \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: demo-interaction-001' \
  -d "{
        \"user_id\": \"user_local\",
        \"conversation_id\": \"conv_demo\",
        \"channel\": \"api\",
        \"input\": \"Remind me to submit the report at $DUE\",
        \"preferred_execution_mode\": \"delegated\"
      }"
```

A due time in the past is still accepted as an interaction — the Executive has no opinion about
it — but the Steward's Action fails domain validation, the Task ends `failed` with
`AGT-INPUT-001`, and you are told so through a `task_failed` notification. That asymmetry is
deliberate: the interaction was well-formed, the Action was not.

The response is `202 Accepted` with a Task receipt. The receipt is returned only after the
Intent, Task and assignment transaction has committed, so the identifiers in it are already
queryable.

## Inspecting what happened

```bash
agtyle task show <task_id>
agtyle task timeline <task_id>   # current state and full history, clearly separated
agtyle reminder show <reminder_id>
```

The timeline is assembled from the Task plus Events, AgentRuns, ActionRequests, PolicyDecisions,
ActionResults, Reminders and Notifications. Current state comes from the Task; everything under
`history` is a record of something that already happened.

## Changing an Agent manifest

A manifest declares which Capabilities an Agent may even propose, so editing one changes the
system's authority surface. The database keeps a snapshot of the registry, and startup compares
against it:

```bash
agtyle registry check   # does the stored snapshot still agree with the configured registry?
agtyle registry sync    # record the current registry as the enabled snapshot
```

If a manifest changes without a sync, every role refuses to start with `AGT-SYSTEM-001` naming
the Agent and both hashes. `agtyle init` syncs as part of initialization. Old snapshot rows are
disabled, never deleted, so a removed Agent can still explain the AgentRuns it produced.

## Configuration

Precedence: explicit CLI flags, then `AGTYLE_` environment variables, then `.env` (development
only), then defaults. Copy `.env.example` to `.env` to start. Every value is validated at
startup; an invalid value fails the process rather than being silently coerced.

The most useful knobs:

| Variable | Meaning |
|---|---|
| `AGTYLE_DATA_DIR` | Where the database and runtime files live. |
| `AGTYLE_LOCAL_TIMEZONE` | Fallback zone when an interaction omits one. |
| `AGTYLE_TASK_LEASE_SECONDS` | How long a Worker owns a claimed Task before recovery may reclaim it. |
| `AGTYLE_MAX_TASK_ATTEMPTS` | Retry budget per Task. |
| `AGTYLE_WORKER_POLL_MILLISECONDS` | Poll interval for all three worker loops. |

## Backup

Stop the writers, then copy the data directory:

```bash
cp -R .agtyle /path/to/backup
```

SQLite runs in WAL mode, so copy the `-wal` and `-shm` sidecars along with the database, or run
`sqlite3 .agtyle/agtyle.db ".backup /path/to/backup/agtyle.db"` while the system is running.
