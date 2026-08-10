# Agtyle

An auditable execution spine for delegated agent work.

Agtyle turns a natural-language request into durable, authorized, explainable work. An Executive
Agent interprets the request and proposes a Task; the kernel persists it; a Worker claims it and
runs the assigned Specialist Agent; the Agent's structured output is validated as untrusted
input; an independent policy engine authorizes the resulting Action; only then does a capability
adapter cause any effect. Every step leaves a record you can read afterwards.

The first vertical slice is a reminder. That is deliberately small: the point is not the
reminder, it is the spine underneath it — natural-language entry, Executive-to-Specialist
routing, durable background work, deterministic authorization, scheduling, notification closure,
restart recovery, concurrency control, idempotency and auditability.

## Design commitments

- **Tasks drive execution.** Workers claim Tasks. Events record facts and never trigger work.
- **Agents propose; the kernel decides.** Agent output is untrusted structured input. The kernel
  owns identifiers, hashes, idempotency keys, state transitions and every adapter call.
- **Cedar is mandatory on the Action path**, and it fails closed. A missing binary, an invalid
  policy set, a malformed request or a timeout all mean no capability runs.
- **Acknowledgement follows persistence.** A receipt is returned only after the transaction that
  created the work has committed.
- **Consequential effects are idempotent.** A retry may observe a prior result; it may not
  create the same effect twice.
- **Local-first.** One directory and one SQLite database are enough to run and back up.

It is a modular monolith. API, Task Worker, Scheduler and Notification Worker are process roles
that share the same modules, domain rules, migrations and database — not microservices.

## Getting started

Requires [`uv`](https://docs.astral.sh/uv/) and `make`. Network access is needed only for the
first bootstrap.

```bash
git clone https://github.com/ialchemist-dev/Agyle.git
cd Agyle
make bootstrap     # locked dependencies and the pinned Cedar 4.12.0 CLI, checksum-verified
make init          # data directory, migrations, registry and policy validation
make verify        # lint, types, migrations, Cedar matrix, every deterministic test
make demo-reminder # four real processes, wall-clock scheduling, restart proof
```

`make demo-reminder` is the honest demonstration. It starts an API, a Task Worker, a Scheduler
and a Notification Worker as separate processes against one database, creates a reminder due a
few seconds later, waits for both notification loops to close, restarts everything, and fails if
anything was delivered twice.

## Try it

```bash
agtyle api --port 8000 &
agtyle worker &

curl -sS -X POST http://127.0.0.1:8000/v1/interactions \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: demo-interaction-001' \
  -d '{"user_id":"user_local","conversation_id":"conv_demo","channel":"api",
       "input":"Remind me to submit the report at 2026-08-10T15:00:00-06:00"}'

agtyle task timeline <task_id_from_the_response>
```

## Documentation

| Document | What it covers |
|---|---|
| [Architecture design](docs/architecture/agtyle-architecture-design.md) | System boundaries and why they are where they are. |
| [Development and verification specification](docs/specifications/agtyle-development-and-verification-specification.md) | The executable contract this implementation satisfies. |
| [Local development](docs/operations/local-development.md) | Running, configuring, inspecting and backing up. |
| [Recovery](docs/operations/recovery.md) | What each kind of crash leaves behind, and how it resolves. |
| [Cedar authorization](docs/operations/cedar.md) | The schema, the policy set, the matrix and how to change policy. |
| [AGENTS.md](AGENTS.md) | Rules for any agent or person changing this repository. |
| [DEVELOPMENT_LOG.md](DEVELOPMENT_LOG.md) | Decisions, deviations and their reasons. |

## License

MIT
