# Agtyle Development and Verification Specification

**Status:** Implementation specification  
**Date:** 2026-08-09  
**Governing architecture:** `Agtyle Architecture Design`  
**Primary implementation proof:** Create, schedule, deliver, and audit a reminder through the complete Agtyle pipeline

---

## 1. Purpose

This document is the executable development contract for the Agtyle reference implementation. It is written so that a new implementation agent can clone an otherwise uninitialized repository, implement the system without relying on prior conversation context, and prove that the result satisfies the architecture.

The implementation is complete only when another clean environment can:

1. clone the repository;
2. install pinned dependencies through documented commands;
3. initialize the database and validate Cedar policies;
4. start the API, Task Worker, Scheduler, and Notification Worker;
5. submit a natural-language reminder request;
6. receive a persisted Task receipt;
7. observe the Steward Agent produce a structured `reminder.create` ActionRequest;
8. observe schema validation and a real Cedar authorization decision;
9. observe the reminder being stored exactly once;
10. receive a Task-completion notification;
11. advance to the reminder due time and receive one due notification;
12. restart the processes and prove that neither the reminder nor either notification is duplicated; and
13. inspect the Task, AgentRun, ActionRequest, PolicyDecision, ActionResult, Event, Reminder, and Notification records that explain the execution.

This specification uses the following normative terms:

- **MUST / MUST NOT:** required for acceptance;
- **SHOULD / SHOULD NOT:** expected unless a documented technical reason justifies a deviation;
- **MAY:** optional and must not be required by the main verification path.

If this specification and the architecture design appear to conflict, the architecture design governs system boundaries and this document governs implementation detail. The implementing agent MUST record any unresolved contradiction before changing either document.

---

## 2. Product and Engineering Outcome

The first implementation establishes a narrow but complete Agtyle spine:

```text
Interaction
→ Intent preservation
→ Executive routing
→ persisted Task and assignment receipt
→ Worker claim and AgentRun
→ Specialist Agent structured output
→ ActionRequest schema validation
→ Cedar authorization
→ Capability execution
→ transactional state, Event, and Notification persistence
→ Notification delivery
→ scheduled reminder wake-up
→ due Notification delivery
→ restart and idempotency proof
```

The goal is not to build a reminder application. The reminder is the smallest useful vertical slice that exercises natural-language entry, Executive-to-Specialist routing, durable background work, deterministic authorization, scheduling, notification closure, restart recovery, concurrency control, idempotency, and auditability.

The implementation MUST remain a modular monolith. API, Worker, Scheduler, and Notification Worker MAY run as separate operating-system processes, but they MUST use the same application modules, domain rules, ports, migrations, and SQLite database. They are process roles, not microservices.

---

## 3. Scope

### 3.1 Required baseline

The implementation agent MUST deliver:

- a Python 3.12+ package using Pydantic v2;
- a FastAPI Interaction Gateway;
- a CLI that can initialize, validate, run, inspect, and demonstrate Agtyle;
- SQLAlchemy 2 persistence with Alembic migrations;
- a SQLite database using foreign keys, WAL mode, busy timeout, and STRICT tables;
- core domain objects and state machines;
- an Agent Registry with Executive and Steward manifests;
- deterministic Executive and Steward test runtimes;
- an `AgentRuntimePort` that permits a real LLM runtime later without changing the kernel;
- an `AuthorizationPort` implemented with the pinned official Cedar CLI;
- schema and policy validation that fail closed;
- a SQLite Task Worker with leases, retries, and crash recovery;
- a reminder capability and reminder scheduler;
- a reliable Notification Worker and local console/test notification adapters;
- append-only Events that record important facts but never drive execution;
- structured logs carrying domain identifiers;
- deterministic tests using an injected clock and ID generator;
- a live multi-process reminder demonstration;
- developer documentation and a machine-readable verification report.

### 3.2 Required seams without full production adapters

The following ports and models MUST exist and have contract tests, even though their production adapters are not part of the reminder baseline:

- `KnowledgePort`;
- `SecretStorePort`;
- `WorkflowEnginePort`;
- Approval and approval consumption models;
- Artifact storage metadata;
- capability adapter registration;
- notification destination routing;
- interactive-to-delegated conversion.

An in-memory or explicit `NotImplementedAdapter` is acceptable for these seams only when the main reminder path does not invoke them. A missing adapter MUST produce a typed failure, never silent success.

### 3.3 Not in the baseline

The implementation agent MUST NOT add the following to make the initial slice work:

- microservices, Kubernetes, or a required container orchestrator;
- Kafka, Redis, RabbitMQ, or another broker;
- PostgreSQL as a required dependency;
- Temporal as a required dependency;
- full Event Sourcing;
- a vector database;
- a dashboard that writes operational state;
- direct MCP or API credentials in an Agent runtime;
- Gmail, Outlook, Slack, or mobile push as a required test dependency;
- recurring reminders;
- distributed multi-user authorization;
- autonomous recommendation or engagement features.

---

## 4. Binding Design Rules

1. **Tasks drive execution.** Workers query and claim Tasks. Events never trigger execution.
2. **Events record important facts.** They are append-only and are not replayed to reconstruct current state.
3. **Notifications close the loop.** Task completion and successful notification delivery are distinct durable facts.
4. **Acknowledgement follows persistence.** The Executive may say a delegated Task was assigned only after the creation and assignment transaction commits.
5. **Agents propose; the kernel decides.** Agent output is untrusted structured input. The kernel validates it, owns state transitions, authorizes Actions, and calls adapters.
6. **Cedar is mandatory on the Action path.** No Capability Adapter may be called directly from an Agent runtime or route handler.
7. **Authorization fails closed.** Missing Cedar, invalid policies, malformed entities, timeout, or evaluation error results in no capability execution.
8. **Approval binds an exact ActionRequest.** The payload hash, principal, action, resource, expiry, and single-use state must match.
9. **Consequential effects are idempotent.** A retry may observe a prior result but must not create the same effect twice.
10. **Time is explicit.** Domain code receives a `ClockPort`; tests never depend on wall-clock sleeps.
11. **Identifiers are stable and never reused.** Use UUIDv7 or an equivalently sortable unique identifier with a type prefix.
12. **All durable artifacts are English.** This includes code comments, schemas, policies, tests, developer logs, and Agent instructions.
13. **External content is untrusted.** It can inform Agent reasoning but never become policy, system instruction, or credential material.
14. **No hidden fallback.** A production adapter must not silently fall back to a fake adapter.
15. **The default installation is local-first.** One directory and one SQLite database must be sufficient for operation and backup.

---

## 5. Implementation Agent Operating Procedure

### 5.1 Before coding

1. Clone the repository into a clean directory.
2. Read `AGENTS.md`, the architecture design, this specification, `README.md`, and `DEVELOPMENT_LOG.md` in that order.
3. Inspect repository status and existing work. Do not overwrite unrelated user changes.
4. Run the existing verification command. If none exists, record `NO_BASELINE_COMMAND` in the development log before creating one.
5. Create `implement/reminder-vertical-slice` unless the user supplies another branch.
6. Add a work-package checklist to `DEVELOPMENT_LOG.md`.

### 5.2 While coding

- Implement one work package at a time and keep the repository runnable after each one.
- Add tests in the same change as the behavior they verify.
- Do not weaken, delete, skip, or xfail a required test to obtain a green build.
- Do not replace Cedar with a Boolean stub in integration or end-to-end verification.
- Keep fake adapters under `tests/fakes/` or a clearly named test module. Production configuration must reject them unless `AGTYLE_ENV=test` or an explicit demo profile is selected.
- Record material decisions and deviations in `DEVELOPMENT_LOG.md` with date, reason, and affected requirement.
- Commit by work package using messages such as `feat(kernel): implement task state machine`.

### 5.3 Before handoff

The agent MUST:

1. run formatting, linting, type checking, unit, contract, integration, migration, failure-injection, and end-to-end tests;
2. run the clean-clone acceptance flow in a new temporary directory;
3. produce `artifacts/verification/verification-report.json` and `.md`;
4. include commands, exit codes, test counts, Cedar version, migration revision, and Git commit SHA;
5. inspect the final diff for secrets, databases, virtual environments, generated files, and unrelated changes;
6. update `DEVELOPMENT_LOG.md`; and
7. report every unmet MUST requirement explicitly.

An agent MUST NOT claim completion solely because tests written by that same agent pass. Completion requires the specified externally observable records and clean-clone demonstration.

---

## 6. Repository Contract

```text
.
├── AGENTS.md
├── README.md
├── DEVELOPMENT_LOG.md
├── Makefile
├── pyproject.toml
├── uv.lock
├── .env.example
├── docs/
│   ├── architecture/agtyle-architecture-design.md
│   ├── specifications/agtyle-development-and-verification-specification.md
│   └── operations/{local-development,recovery,cedar}.md
├── src/agtyle/
│   ├── {config,cli,bootstrap}.py
│   ├── domain/{common,intents,tasks,agents,actions,approvals,artifacts,reminders,notifications,events}.py
│   ├── application/{interaction_service,dispatch_service,execution_service,policy_service,approval_service,reminder_service,notification_service,recovery_service}.py
│   ├── ports/{agent_runtime,authorization,capability,clock,id_generator,knowledge,notification,repositories,secret_store,workflow}.py
│   ├── adapters/
│   │   ├── agent_runtimes/{deterministic_executive,deterministic_steward}.py
│   │   ├── authorization/cedar_cli.py
│   │   ├── capabilities/local_reminders.py
│   │   ├── gateways/{api,cli_gateway}.py
│   │   ├── notifications/{console,recording}.py
│   │   └── persistence/{database,models,repositories,unit_of_work}.py
│   ├── workers/{task_worker,scheduler,notification_worker}.py
│   └── observability/{logging,tracing}.py
├── agents/{executive,steward}/
├── contracts/
│   ├── schemas/v1/
│   └── fixtures/{valid,invalid}/
├── policies/cedar/{agtyle.cedarschema,base.cedar,tests.json}
├── migrations/{env.py,versions/}
├── scripts/{install_cedar,clean_clone_verify,reminder_demo}.py
├── tests/{unit,contract,persistence,integration,e2e,failure_injection,fakes}/
└── artifacts/verification/.gitkeep
```

Generated runtime data MUST live under `.agtyle/`. Git MUST ignore `.agtyle/`, `.venv/`, `.tools/`, `.env`, SQLite database sidecars, and generated verification reports.

---

## 7. Toolchain and Reproducibility

### 7.1 Required toolchain

The reference implementation MUST pin:

| Tool | Requirement |
|---|---|
| Python | `>=3.12,<3.14` until both versions are verified |
| Package metadata | PEP 621 in `pyproject.toml` |
| Dependency lock | committed `uv.lock` |
| Domain validation | Pydantic v2 |
| API | FastAPI + Uvicorn |
| ORM | SQLAlchemy 2 |
| Migrations | Alembic |
| Tests | pytest |
| Property tests | Hypothesis |
| Lint/format | Ruff |
| Static typing | mypy strict mode for `src/agtyle` |
| Authorization | official Cedar CLI `4.12.0` |

Cedar `4.12.0` is pinned because the official project publishes prebuilt CLI binaries and checksums for Linux, macOS, and Windows. `scripts/install_cedar.py` MUST:

1. detect OS and architecture;
2. download only the matching official release asset;
3. verify the published SHA-256 checksum;
4. install to `.tools/cedar/4.12.0/cedar`;
5. print the installed version;
6. be idempotent; and
7. preserve an existing valid installation if a new download fails verification.

The script MUST NOT execute an unverified `curl | sh` path. The official Cedar project separates schema/policy validation from authorization evaluation. Policy validation therefore MUST run during bootstrap, CI, and application startup. See the [official Cedar implementation](https://github.com/cedar-policy/cedar), [schema documentation](https://docs.cedarpolicy.com/schema/schema.html), and [validation documentation](https://docs.cedarpolicy.com/policies/validation.html).

### 7.2 Canonical commands

The Makefile MUST expose:

```text
make bootstrap        # locked Python dependencies and pinned Cedar
make init             # config directories, migrations, registry and policy validation
make format
make lint
make typecheck
make test-unit
make test-contract
make test-integration
make test-e2e
make test-failures
make test             # all deterministic tests; no live external APIs
make verify           # lint, types, migrations, Cedar, all deterministic tests
make demo-reminder    # live local multi-process reminder proof
make clean-clone-verify
```

`make verify` MUST not require Gmail, a live LLM, Docker, or a cloud service. Network access is allowed only for initial dependency/bootstrap installation. Once dependencies and Cedar are present, verification MUST run offline.

### 7.3 Configuration

Configuration MUST be Pydantic-validated and loaded in this precedence order:

1. explicit CLI flags;
2. environment variables prefixed `AGTYLE_`;
3. `.env` for local development only;
4. defaults.

```text
AGTYLE_ENV=development|test|production
AGTYLE_DATA_DIR=.agtyle
AGTYLE_DATABASE_URL=sqlite:///.agtyle/agtyle.db
AGTYLE_CEDAR_BINARY=.tools/cedar/4.12.0/cedar
AGTYLE_CEDAR_SCHEMA=policies/cedar/agtyle.cedarschema
AGTYLE_CEDAR_POLICIES=policies/cedar/base.cedar
AGTYLE_LOCAL_TIMEZONE=America/Denver
AGTYLE_TASK_LEASE_SECONDS=30
AGTYLE_NOTIFICATION_LEASE_SECONDS=30
AGTYLE_MAX_TASK_ATTEMPTS=3
AGTYLE_WORKER_POLL_MILLISECONDS=250
AGTYLE_LOG_LEVEL=INFO
```

The timezone is a user preference used only when an interaction omits one. Every stored reminder MUST include the IANA timezone used to interpret its due time.

---

## 8. Runtime Topology

| Process role | Responsibility | Writes state? |
|---|---|---|
| API / CLI Gateway | Accept interactions, create Intent and Task, return receipt, inspect state | Through application services |
| Task Worker | Claim assigned Tasks, run Agents, process Actions, finalize Tasks | Yes |
| Scheduler | Claim due Reminders and create due Notifications | Yes |
| Notification Worker | Claim and deliver pending Notifications | Yes |

All roles MUST use the same dependency bootstrap, domain rules, application services, repositories, and database. Route handlers and CLI commands MUST NOT access SQLAlchemy models directly.

The development command MAY start all roles under one supervisor. Production-shaped tests MUST also prove that separate processes can share the database safely.

---

## 9. Domain Contracts

### 9.1 Identifiers

IDs MUST be unique, sortable where practical, opaque to business logic, and never reused:

```text
int_<uuid7>   task_<uuid7>  run_<uuid7>  act_<uuid7>
pol_<uuid7>   apr_<uuid7>   res_<uuid7>  art_<uuid7>
rem_<uuid7>   not_<uuid7>   evt_<uuid7>
```

Tests MUST inject a deterministic ID generator. Production logic MUST not infer type by parsing the prefix.

### 9.2 Time

- Persist timestamps as UTC ISO 8601 with microseconds and `Z`, or as UTC integer microseconds. Select one representation for all tables.
- Pydantic models expose timezone-aware `datetime`.
- Reject naive datetimes at boundaries.
- Store reminder `scheduled_for_utc` and `timezone`.
- Domain and application modules receive `ClockPort`; they do not call `datetime.now()` directly.

### 9.3 Intent

```python
class Intent(BaseModel):
    id: IntentId
    user_id: str
    origin_channel: str
    origin_conversation_id: str
    original_input: str
    interpreted_outcome: str | None
    created_at: AwareDatetime
```

The original input MUST be preserved byte-for-byte after request decoding. It MUST NOT contain credentials. Interpretation may be added but never replaces the original.

### 9.4 Task and state machine

```python
class TaskStatus(StrEnum):
    CREATED = "created"
    ASSIGNED = "assigned"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

class ExecutionMode(StrEnum):
    INTERACTIVE = "interactive"
    DELEGATED = "delegated"
    APPROVAL_GATED = "approval_gated"
```

| From | Allowed next states |
|---|---|
| `created` | `assigned`, `cancelled` |
| `assigned` | `running`, `cancelled` |
| `running` | `waiting_approval`, `completed`, `failed`, `cancelled` |
| `waiting_approval` | `running`, `cancelled`, `failed` |
| `failed` | `assigned` only when retry policy permits |
| `completed` | none |
| `cancelled` | none |

Every transition MUST use a domain method that validates the current state. Direct assignment to `task.status` outside persistence hydration is prohibited.

Required Task lease fields:

```text
lease_owner
lease_expires_at
attempt_count
max_attempts
last_error_code
```

### 9.5 AgentRun

One Task may have multiple AgentRuns due to retry. `(task_id, attempt)` MUST be unique. The Worker creates an AgentRun in the same transaction that claims the Task and transitions it to `running`.

```text
status = running | succeeded | failed | abandoned
```

Expired-lease recovery marks the previous running AgentRun `abandoned` before creating the next attempt.

### 9.6 ActionRequest and hashing

```python
class ActionRequest(BaseModel):
    id: ActionRequestId
    task_id: TaskId
    agent_run_id: AgentRunId
    principal_agent_id: str
    capability: str
    schema_version: Literal[1]
    resource_type: str
    resource_id: str
    payload: dict[str, JsonValue]
    payload_hash: str
    idempotency_key: str
    status: ActionStatus
```

Canonical payload hashing MUST use UTF-8 RFC 8785 JSON Canonicalization Scheme semantics or an explicitly tested equivalent. Hash format: `sha256:<lowercase hex>`.

The protected document is:

```json
{
  "principal_agent_id": "steward",
  "capability": "reminder.create",
  "resource_type": "ReminderCollection",
  "resource_id": "user_local",
  "schema_version": 1,
  "payload": {}
}
```

Database IDs, timestamps, status, and the hash itself are excluded. Contract tests MUST prove that key order and insignificant whitespace do not change the hash, while any protected value change does.

### 9.7 PolicyDecision

```text
decision = allow | require_approval | deny | error
cedar_decision = allow | deny | not_evaluated | error
reason_code
determining_policy_ids_json
request_json
created_at
```

The exact normalized authorization request MUST be retained without secrets. `error` is fail-closed and distinct from a policy `deny`.

### 9.8 Approval

Approval is a required core model even though baseline reminder creation is auto-allowed.

```text
id
action_request_id
payload_hash
principal_agent_id
resource_type
resource_id
approved_by_user_id
status                # granted / rejected / expired / consumed / revoked
expires_at
single_use
created_at
consumed_at
```

Approval consumption and transition of the matching ActionRequest back to executable state MUST be atomic. Used, expired, revoked, mismatched, or modified Approval MUST NOT authorize an Action.

### 9.9 Reminder

```python
class ReminderStatus(StrEnum):
    SCHEDULED = "scheduled"
    FIRING = "firing"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"
    FAILED = "failed"

class Reminder(BaseModel):
    id: ReminderId
    user_id: str
    source_task_id: TaskId
    title: str
    note: str | None
    scheduled_for_utc: AwareDatetime
    timezone: str
    status: ReminderStatus
    idempotency_key: str
    created_at: AwareDatetime
    delivered_at: AwareDatetime | None
```

Rules:

- title after trimming is 1–200 Unicode code points;
- note is optional and at most 4,000 Unicode code points;
- timezone is a valid IANA identifier;
- due time is later than Action evaluation time;
- ambiguous daylight-saving local time requires an offset or explicit disambiguation;
- nonexistent local time requires clarification;
- recurrence is rejected as unsupported;
- `(user_id, idempotency_key)` is unique;
- delivered and cancelled are terminal.

### 9.10 Notification

```text
kind = task_completed | task_failed | approval_required | reminder_due
delivery_status = pending | delivering | delivered | failed
```

Each Notification has a unique `delivery_key`:

```text
reminder_due:<reminder_id>:once
task_terminal:<task_id>:<completed|failed|cancelled>
```

Destination is structured:

```json
{
  "adapter": "console",
  "user_id": "user_local",
  "conversation_id": "conv_demo"
}
```

### 9.11 Event

```json
{
  "id": "evt_...",
  "type": "agtyle.task.completed.v1",
  "subject": "task_...",
  "actor": "agent:steward",
  "time": "2026-08-09T22:32:14.000000Z",
  "data": {}
}
```

The Event repository exposes append and query only. SQLite triggers MUST reject `UPDATE` and `DELETE` on `events`.

---

## 10. Persistence Specification

### 10.1 SQLite connection contract

Every connection MUST execute:

```sql
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA busy_timeout = 5000;
PRAGMA synchronous = NORMAL;
```

Tests MUST assert `foreign_keys=1`. Production startup MUST fail if the database cannot enable required settings. WAL MAY be omitted only for isolated in-memory unit tests.

### 10.2 Atomic operations

Each list item below is one transaction:

1. create Intent, create Task, assign Task, append accepted and assigned Events;
2. claim Task, increment attempt, create AgentRun, transition Task to running;
3. persist ActionRequest and validation result;
4. persist PolicyDecision and update ActionRequest;
5. create Reminder, ActionResult, completed Task, completion Event, and completion Notification;
6. claim due Reminder, transition to firing, create reminder-due Notification;
7. mark Notification delivered and mark its Reminder delivered when applicable;
8. recover expired lease, abandon old run, reassign or fail Task, append recovery Event;
9. create or consume Approval and update its ActionRequest and Task.

No Notification may be delivered before its creation transaction commits.

### 10.3 Required tables

The initial migration MUST create SQLite STRICT tables:

```text
intents
tasks
agent_runs
action_requests
policy_decisions
approvals
action_results
artifacts
reminders
notifications
events
agents
capabilities
```

SQLAlchemy models use explicit column types. JSON is canonical text with `CHECK(json_valid(column))`. Domain-specific payloads are also validated against their versioned JSON Schema before persistence.

### 10.4 Required constraints and indexes

```text
UNIQUE agent_runs(task_id, attempt)
UNIQUE action_requests(idempotency_key)
UNIQUE action_results(action_request_id)
UNIQUE reminders(user_id, idempotency_key)
UNIQUE notifications(delivery_key)
UNIQUE agents(agent_id, version)
UNIQUE capabilities(name, version)

INDEX tasks_claim_idx(status, lease_expires_at, created_at)
INDEX reminders_due_idx(status, scheduled_for_utc)
INDEX notifications_claim_idx(delivery_status, next_attempt_at, created_at)
INDEX events_subject_time_idx(subject, time)
INDEX policy_decisions_action_idx(action_request_id, created_at)
```

Foreign-key deletion defaults to `RESTRICT`. Events, PolicyDecisions, and ActionResults MUST never cascade-delete. The baseline exposes no destructive deletion of audit history.

### 10.5 Migration proof

On a new temporary database, CI MUST prove:

1. `alembic upgrade head`;
2. current revision equals head;
3. `alembic downgrade base`;
4. a second upgrade to head;
5. every required table, index, unique constraint, STRICT declaration, and Event immutability trigger exists;
6. foreign-key violation attempts fail;
7. invalid JSON fails.

### 10.6 Minimum column contract

All tables include `created_at`; mutable operational tables also include `updated_at` and integer `row_version` for optimistic concurrency. IDs are TEXT primary keys. Enum values have `CHECK` constraints.

#### `intents`

```text
id PK
user_id NOT NULL
origin_channel NOT NULL
origin_conversation_id NOT NULL
interaction_idempotency_key NOT NULL
request_hash NOT NULL
original_input NOT NULL
interpreted_outcome NULL
created_at NOT NULL
UNIQUE(user_id, interaction_idempotency_key)
```

#### `tasks`

```text
id PK
intent_id FK intents NOT NULL
task_type NOT NULL
status NOT NULL
execution_mode NOT NULL
assigned_agent_id NOT NULL
objective NOT NULL
payload_json NOT NULL CHECK(json_valid(payload_json))
origin_json NOT NULL CHECK(json_valid(origin_json))
attempt_count NOT NULL DEFAULT 0 CHECK(attempt_count >= 0)
max_attempts NOT NULL CHECK(max_attempts >= 1)
lease_owner NULL
lease_expires_at NULL
next_attempt_at NULL
last_error_code NULL
last_error_message NULL
row_version NOT NULL DEFAULT 1
created_at NOT NULL
updated_at NOT NULL
```

#### `agent_runs`

```text
id PK
task_id FK tasks NOT NULL
agent_id NOT NULL
agent_version NOT NULL
attempt NOT NULL CHECK(attempt >= 1)
status NOT NULL
context_pack_hash NOT NULL
started_at NOT NULL
ended_at NULL
error_code NULL
error_message NULL
UNIQUE(task_id, attempt)
```

#### `action_requests`

```text
id PK
task_id FK tasks NOT NULL
agent_run_id FK agent_runs NOT NULL
principal_agent_id NOT NULL
capability NOT NULL
schema_version NOT NULL
resource_type NOT NULL
resource_id NOT NULL
payload_json NOT NULL CHECK(json_valid(payload_json))
payload_hash NOT NULL
idempotency_key NOT NULL UNIQUE
status NOT NULL
created_at NOT NULL
updated_at NOT NULL
row_version NOT NULL DEFAULT 1
```

#### `policy_decisions`

```text
id PK
action_request_id FK action_requests NOT NULL
decision NOT NULL
cedar_decision NOT NULL
reason_code NOT NULL
determining_policy_ids_json NOT NULL CHECK(json_valid(determining_policy_ids_json))
request_json NOT NULL CHECK(json_valid(request_json))
cedar_version NULL
created_at NOT NULL
```

#### `approvals`

```text
id PK
action_request_id FK action_requests NOT NULL
payload_hash NOT NULL
principal_agent_id NOT NULL
resource_type NOT NULL
resource_id NOT NULL
approved_by_user_id NOT NULL
status NOT NULL
expires_at NOT NULL
single_use NOT NULL CHECK(single_use IN (0,1))
consumed_at NULL
created_at NOT NULL
updated_at NOT NULL
row_version NOT NULL DEFAULT 1
```

#### `action_results`

```text
id PK
action_request_id FK action_requests NOT NULL UNIQUE
status NOT NULL
external_ref NULL
result_json NOT NULL CHECK(json_valid(result_json))
reconciliation_status NOT NULL
started_at NOT NULL
completed_at NOT NULL
created_at NOT NULL
```

#### `artifacts`

```text
id PK
task_id FK tasks NOT NULL
kind NOT NULL
media_type NOT NULL
storage_ref NOT NULL
content_hash NOT NULL
metadata_json NOT NULL CHECK(json_valid(metadata_json))
created_at NOT NULL
```

#### `reminders`

```text
id PK
user_id NOT NULL
source_task_id FK tasks NOT NULL
title NOT NULL
note NULL
scheduled_for_utc NOT NULL
timezone NOT NULL
status NOT NULL
idempotency_key NOT NULL
firing_lease_owner NULL
firing_lease_expires_at NULL
created_at NOT NULL
updated_at NOT NULL
delivered_at NULL
row_version NOT NULL DEFAULT 1
UNIQUE(user_id, idempotency_key)
```

#### `notifications`

```text
id PK
task_id FK tasks NULL
reminder_id FK reminders NULL
kind NOT NULL
destination_json NOT NULL CHECK(json_valid(destination_json))
payload_json NOT NULL CHECK(json_valid(payload_json))
delivery_key NOT NULL UNIQUE
delivery_status NOT NULL
attempt_count NOT NULL DEFAULT 0 CHECK(attempt_count >= 0)
max_attempts NOT NULL CHECK(max_attempts >= 1)
lease_owner NULL
lease_expires_at NULL
next_attempt_at NULL
last_error_code NULL
created_at NOT NULL
updated_at NOT NULL
delivered_at NULL
row_version NOT NULL DEFAULT 1
CHECK(task_id IS NOT NULL OR reminder_id IS NOT NULL)
```

#### `events`

```text
id PK
type NOT NULL
subject NOT NULL
actor NOT NULL
time NOT NULL
data_json NOT NULL CHECK(json_valid(data_json))
created_at NOT NULL
```

#### `agents` and `capabilities`

```text
agents:
  agent_id
  version
  manifest_hash
  enabled
  registered_at
  PRIMARY KEY(agent_id, version)

capabilities:
  name
  version
  action_schema_id
  adapter_name
  enabled
  registered_at
  PRIMARY KEY(name, version)
```

The database stores registry snapshots for audit and startup comparison; filesystem manifests and code registration remain the configured source. Startup MUST fail when an enabled database snapshot disagrees with the current manifest hash until an explicit registry-sync command records the new version.

---

## 11. Port Contracts and Dependency Direction

Domain modules MUST import no FastAPI, SQLAlchemy, Cedar subprocess, filesystem, or LLM SDK code. Application modules may import domain and ports. Adapters implement ports and may import infrastructure libraries.

```python
class AgentRuntimePort(Protocol):
    async def run(
        self,
        assignment: AgentAssignment,
        context: ContextPack,
    ) -> AgentOutput: ...

class AuthorizationPort(Protocol):
    async def validate_policy_set(self) -> PolicyValidationReport: ...
    async def authorize(self, request: AuthorizationRequest) -> CedarResult: ...

class CapabilityPort(Protocol):
    capability_name: str
    async def execute(self, request: ActionRequest) -> ActionExecutionResult: ...
    async def reconcile(self, idempotency_key: str) -> ReconciliationResult: ...

class NotificationPort(Protocol):
    adapter_name: str
    async def deliver(self, notification: Notification) -> DeliveryResult: ...

class ClockPort(Protocol):
    def now(self) -> datetime: ...

class UnitOfWorkPort(Protocol):
    intents: IntentRepository
    tasks: TaskRepository
    agent_runs: AgentRunRepository
    actions: ActionRepository
    approvals: ApprovalRepository
    reminders: ReminderRepository
    notifications: NotificationRepository
    events: EventRepository
    async def __aenter__(self) -> Self: ...
    async def commit(self) -> None: ...
    async def rollback(self) -> None: ...
```

Repository methods MUST operate on domain models, not leak ORM models. Adapters MUST pass the same contract test suite. A future MCP reminder adapter, for example, must satisfy the tests already used by the local reminder adapter.

### 11.1 Application use cases

Application services MUST expose commands and results equivalent to:

| Service | Command | Result |
|---|---|---|
| InteractionService | `HandleInteraction` | direct response or persisted TaskReceipt |
| DispatchService | `CreateAndAssignTask` | TaskReceipt |
| ExecutionService | `ClaimTask`, `ExecuteClaimedTask`, `FinalizeTask` | claim/result |
| PolicyService | `EvaluateAction` | PolicyOutcome |
| ApprovalService | `Grant`, `Reject`, `Consume` | Approval |
| ReminderService | `CreateFromAction`, `ClaimDue`, `MarkDelivered` | Reminder |
| NotificationService | `ClaimPending`, `DeliverClaimed` | DeliveryResult |
| RecoveryService | `RecoverExpiredLeases` | RecoverySummary |

Commands are immutable Pydantic models. Services return domain/application results and raise typed application errors. HTTP codes and console exit codes are mapped only in Gateway adapters.

### 11.2 Stable error taxonomy

```text
AGT-INPUT-001 invalid_request
AGT-INPUT-002 clarification_required
AGT-INPUT-003 idempotency_conflict
AGT-TASK-001 illegal_transition
AGT-TASK-002 lease_lost
AGT-TASK-003 retry_exhausted
AGT-AGENT-001 invalid_agent_output
AGT-AGENT-002 unsupported_assignment
AGT-POLICY-001 denied
AGT-POLICY-002 approval_required
AGT-POLICY-003 engine_error
AGT-ACTION-001 schema_invalid
AGT-ACTION-002 idempotency_conflict
AGT-CAP-001 transient_failure
AGT-CAP-002 permanent_failure
AGT-NOTIFY-001 delivery_failed
AGT-SYSTEM-001 configuration_invalid
```

Public errors include code, safe title, safe detail, and Task ID when available. Internal exception type, stack, command line, policy body, and secrets remain diagnostic only.

---

## 12. Agent Registry and Deterministic Runtimes

### 12.1 Manifest contract

The Steward manifest MUST declare:

```yaml
id: steward
version: 1
display_name: Steward Agent
accepts:
  - reminder_create
produces:
  - reminder_action_request
capabilities:
  requested:
    - reminder.create
context_scopes:
  - timezone
  - reminder_preferences
authority:
  external_publish: forbidden
escalation:
  ambiguity: when_outcome_changes
```

The Executive manifest MUST accept `interaction`, produce `task_proposal` or `direct_response`, and request no external capability.

Startup validation MUST reject:

- duplicate Agent ID/version;
- a manifest that fails schema validation;
- an accepted or produced type that is unregistered;
- a requested Capability that is unregistered;
- missing responsibility, personality, or playbook files.

### 12.2 Agent output is untrusted

Every Agent output MUST be parsed into a Pydantic model with `extra="forbid"`. Invalid output produces a typed AgentRun failure and no Action execution. Repair or retry MAY occur only under explicit retry policy and MUST be recorded as a new AgentRun or model call attempt.

### 12.3 Deterministic Executive runtime

The deterministic runtime exists to verify orchestration without a live LLM. It MUST recognize the exact test fixture family:

```text
Remind me to <title> at <RFC3339 timestamp>
```

It returns:

```json
{
  "kind": "task_proposal",
  "task_type": "reminder_create",
  "assigned_agent_id": "steward",
  "execution_mode": "delegated",
  "objective": "Create a reminder",
  "payload": {
    "title": "submit the report",
    "scheduled_for": "2026-08-10T15:00:00-06:00",
    "timezone": "America/Denver"
  }
}
```

Unknown or ambiguous input returns a typed clarification response. It MUST NOT invent time values.

### 12.4 Deterministic Steward runtime

Given a validated reminder assignment, it returns one `ActionRequestProposal`:

```json
{
  "capability": "reminder.create",
  "schema_version": 1,
  "resource_type": "ReminderCollection",
  "resource_id": "user_local",
  "payload": {
    "title": "submit the report",
    "note": null,
    "scheduled_for_utc": "2026-08-10T21:00:00Z",
    "timezone": "America/Denver"
  }
}
```

The kernel supplies IDs, hash, idempotency key, Task links, and status. An Agent MUST NOT be trusted to select those control values.

### 12.5 Real runtime seam

A real LLM runtime is a later adapter. Its prompt MUST include responsibility, output schema, minimal ContextPack, authority budget, and trust labels. Tests for the kernel MUST not require that adapter. A live smoke test MAY be added under an explicit marker such as `pytest -m live_agent` and MUST not run in default CI.

---

## 13. Public Interaction and Inspection API

### 13.1 Health endpoints

```text
GET /health/live
GET /health/ready
```

`ready` returns non-200 unless the database is migrated, registry is valid, Cedar binary is present, and policies validate. It MUST NOT require a Worker to be currently polling.

### 13.2 Create interaction

```http
POST /v1/interactions
Idempotency-Key: demo-interaction-001
Content-Type: application/json
```

```json
{
  "user_id": "user_local",
  "conversation_id": "conv_demo",
  "channel": "api",
  "input": "Remind me to submit the report at 2026-08-10T15:00:00-06:00",
  "preferred_execution_mode": "delegated"
}
```

Successful delegated response is `202 Accepted`:

```json
{
  "intent_id": "int_...",
  "task_receipt": {
    "task_id": "task_...",
    "status": "assigned",
    "assigned_agent_id": "steward",
    "execution_mode": "delegated",
    "accepted_at": "2026-08-09T22:00:00Z"
  },
  "message": "The reminder task has been assigned to the Steward Agent."
}
```

The handler MUST return only after the Intent, assigned Task, and Events commit. Retrying the same request with the same `Idempotency-Key` and same body returns the same receipt. Reusing the key with a different body returns `409 Conflict`.

Validation errors return RFC 9457-style problem details. No internal traceback, policy text, or secret reference is returned.

### 13.3 Inspection endpoints

```text
GET /v1/tasks/{task_id}
GET /v1/tasks/{task_id}/timeline
GET /v1/reminders/{reminder_id}
GET /v1/notifications?task_id=...&status=...
```

The timeline is a read model assembled from current Task state plus Events, AgentRuns, Actions, PolicyDecisions, Results, and Notifications. It MUST clearly label current state versus historical records.

### 13.4 CLI

```text
agtyle init
agtyle validate
agtyle api
agtyle worker [--once]
agtyle scheduler [--once]
agtyle notifications [--once]
agtyle task show <task_id>
agtyle task timeline <task_id>
agtyle reminder show <reminder_id>
agtyle demo reminder --due-in 5s
```

Every `--once` command returns exit code 0 when it successfully polls, including when there is no work. Typed processing failures return a non-zero code and structured error output.

---

## 14. Reminder Contract and Capability

### 14.1 Versioned Action schema

`contracts/schemas/v1/reminder-create.schema.json` MUST:

- set `additionalProperties: false`;
- require `title`, `scheduled_for_utc`, and `timezone`;
- allow nullable `note`;
- constrain string lengths;
- require RFC 3339 date-time syntax;
- forbid a recurrence field;
- include `$id` and a stable schema version.

Pydantic and JSON Schema validation results MUST agree for the valid and invalid fixtures.

### 14.2 Idempotency key

For the first creation attempt:

```text
sha256(user_id | task_id | capability | payload_hash)
```

The key is computed by the kernel, not the Agent. Retrying the same Task and Action payload produces the same key. A changed protected payload produces a new key and requires a new ActionRequest.

### 14.3 Local reminder adapter

`LocalReminderCapability.execute` MUST:

1. assert capability name and schema version;
2. validate the Action payload;
3. check for an existing Reminder by idempotency key;
4. return the existing Reminder as `already_applied` if its protected fields match;
5. raise an idempotency conflict if the key exists with different protected fields;
6. create one scheduled Reminder otherwise;
7. return a structured result with Reminder ID and status.

The adapter MUST not mark the source Task complete. `ExecutionService` owns ActionResult persistence and Task finalization.

### 14.4 Scheduler

The Scheduler claims due records in a short transaction:

1. select `scheduled` reminders with `scheduled_for_utc <= now`, ordered by due time and ID;
2. conditionally update one row from `scheduled` to `firing`;
3. create a `reminder_due` Notification using the stable delivery key;
4. append `agtyle.reminder.firing.v1`;
5. commit.

SQLite does not provide PostgreSQL-style `SKIP LOCKED`; use a conditional update and verify one affected row. Two Scheduler processes MUST not create two due Notifications.

If a Scheduler crashes after transition to `firing` but before commit, the transaction rolls back. If it crashes after commit, the Notification exists and the Notification Worker continues the flow.

### 14.5 Reminder delivery outcome

On successful due Notification delivery, the same transaction MUST:

- mark Notification `delivered`;
- mark Reminder `delivered`;
- set both delivery timestamps from `ClockPort`;
- append `agtyle.reminder.delivered.v1`.

After maximum delivery attempts, mark Notification `failed`, mark Reminder `failed`, append failure Event, and create no recursive failure notification in the baseline.

---

## 15. Cedar Authorization Specification

### 15.1 Integration choice

The Python reference adapter invokes the pinned official Cedar CLI as a subprocess. It MUST:

- use an argument array, never a shell string;
- set a short configurable timeout;
- pass request data through temporary files with owner-only permissions or supported standard input;
- parse only documented JSON output;
- capture policy IDs and diagnostics;
- delete temporary files in `finally`;
- map missing binary, timeout, invalid output, and non-decision exit codes to `CedarResult.ERROR`;
- never log full unredacted request context at INFO level.

A future in-process Rust or WASM adapter may replace it behind `AuthorizationPort`. No application service may depend on subprocess details.

### 15.2 Cedar entities

Use namespace `Agtyle` and stable, never-reused entity IDs:

```text
Agtyle::Agent::"steward"
Agtyle::Agent::"research"
Agtyle::User::"user_local"
Agtyle::ReminderCollection::"user_local"
Agtyle::Action::"reminder.create"
```

The request context for `reminder.create` MUST include only policy-relevant values:

```json
{
  "origin_user_id": "user_local",
  "has_direct_user_instruction": true,
  "payload_hash": "sha256:...",
  "approval_present": false,
  "approval_valid": false
}
```

Due time validity belongs to domain validation, not Cedar. Cedar decides authority, not business-data correctness.

### 15.3 Baseline business mapping

`PolicyService` evaluates in this order:

1. capability is registered;
2. Agent manifest declares it may request the capability;
3. Action schema is valid;
4. Cedar request is evaluated;
5. if Cedar allows, return `ALLOW`;
6. if Cedar denies and the capability is approval-eligible with no approval, return `REQUIRE_APPROVAL`;
7. otherwise return `DENY`;
8. any infrastructure or evaluation error returns `ERROR` and prevents execution.

`reminder.create` is not approval-eligible in the baseline because it is a reversible local action directly requested by the user. A Cedar deny therefore maps to `DENY`, not `REQUIRE_APPROVAL`.

### 15.4 Required policy matrix

The real Cedar engine MUST pass:

| Case | Principal | Action | Context | Expected |
|---|---|---|---|---|
| P01 | Steward | reminder.create | direct instruction, same user scope | Allow |
| P02 | Steward | reminder.create | no direct instruction | Deny |
| P03 | Steward | reminder.create | resource belongs to another user | Deny |
| P04 | Research | reminder.create | otherwise valid | Deny |
| P05 | Executive | reminder.create | otherwise valid | Deny |
| P06 | Unknown Agent | reminder.create | otherwise valid | Deny |
| P07 | Steward | unknown action | otherwise valid | request validation failure; fail closed |
| P08 | malformed context | reminder.create | wrong field type | fail closed |
| P09 | Steward | future email.send | no approval | Cedar deny; Policy Service may map to require approval |
| P10 | Steward | prohibited finance.transfer | any | Deny |

Policy tests MUST be stored as data in `policies/cedar/tests.json` and run both through a Python contract test and the Cedar CLI test command when available.

### 15.5 Startup behavior

Application startup MUST:

1. check Cedar version exactly matches the pinned supported version;
2. parse schema and policies;
3. validate all policies against the schema;
4. run a minimal deny-by-default self-test;
5. expose the validation result in readiness state.

Invalid Cedar configuration makes the system unready and prevents Workers from executing Actions. It does not delete or mutate queued Tasks.

---

## 16. Task Worker

### 16.1 Claim algorithm

Within a write transaction, the Worker:

1. selects the oldest `assigned` Task whose lease is absent or expired;
2. conditionally updates it to `running` with `lease_owner`, `lease_expires_at`, and incremented `attempt_count`;
3. verifies exactly one row was updated;
4. creates the matching AgentRun;
5. appends `agtyle.task.started.v1`;
6. commits before calling an Agent runtime.

Only the Worker that owns the lease may heartbeat or finalize the attempt. Finalization uses an optimistic condition on Task ID, status, lease owner, and expected version.

### 16.2 Heartbeat

Long Agent calls MUST renew the lease before half the lease duration elapses. The reference runtime may use a background coroutine. Failure to renew causes the Worker to stop processing the result as authoritative; it must reconcile ownership before persisting output.

### 16.3 Execution sequence

```text
claim Task
→ build constrained ContextPack
→ invoke assigned Agent runtime
→ validate AgentOutput
→ persist proposed ActionRequest
→ validate action schema
→ authorize through PolicyService/Cedar
→ execute Capability only on ALLOW
→ persist ActionResult
→ complete Task
→ create Event and Notification atomically
```

A Task with no Action but a valid terminal Artifact may complete. The reminder Task is complete only after the Reminder record and ActionResult exist.

### 16.4 Failure classification

| Failure | Retry? | Example |
|---|---|---|
| `validation_permanent` | No | Agent output violates schema repeatedly |
| `authorization_denied` | No | Research Agent requests reminder.create |
| `authorization_error` | Yes, bounded | Cedar process timeout |
| `capability_transient` | Yes, reconcile first | temporary database busy |
| `capability_permanent` | No | invalid reminder timezone |
| `agent_runtime_transient` | Yes | model timeout |
| `agent_runtime_permanent` | No | unsupported assignment |
| `lease_lost` | No finalization by old owner | another Worker recovered Task |

Retry delay is deterministic exponential backoff with bounded jitter injected through a policy object. Tests use zero jitter.

### 16.5 Exhaustion

After `max_attempts`, transition Task to `failed`, store a stable public error code and redacted message, append failure Event, and create one terminal failure Notification. Internal stack traces remain in diagnostic logs, not Task payloads or user notifications.

---

## 17. Notification Worker

### 17.1 Claim and retry

The Notification Worker uses the same conditional-claim pattern:

```text
pending/failed-retryable
→ delivering with owner and lease
→ adapter call
→ delivered, or pending with next_attempt_at, or terminal failed
```

Delivery adapters MUST accept `delivery_key`. The recording adapter enforces uniqueness. The console adapter prints one JSON line containing Notification ID, kind, delivery key, user-visible message, and timestamp.

### 17.2 Delivery semantics

Exactly-once delivery cannot be guaranteed for every future external channel. Agtyle provides:

- exactly-once Notification record creation through a unique delivery key;
- at-least-once adapter invocation after crashes;
- idempotent or reconcilable adapter contracts;
- exactly-once observed delivery in the local recording adapter used for acceptance.

Future adapters that lack idempotency MUST document their reconciliation strategy and residual duplicate risk.

### 17.3 Completion wording

The Notification payload stores structured facts, not final prose:

```json
{
  "task_id": "task_...",
  "status": "completed",
  "result_ref": "rem_...",
  "summary_code": "reminder_created"
}
```

The console demo renderer may produce a fixed English sentence. A future Executive runtime may render natural prose from the same payload.

---

## 18. Complete Reminder Execution

### 18.1 Normal flow

1. Gateway receives an interaction and idempotency key.
2. It validates the envelope and stores the original input.
3. Executive runtime returns a `reminder_create` TaskProposal.
4. Dispatch validates Agent and Capability registration.
5. One transaction creates Intent, Task, assignment, and Events.
6. Gateway returns `202` with the persisted receipt.
7. Task Worker claims Task and creates AgentRun.
8. Kernel builds a ContextPack with Task, timezone preference, origin, and authority budget `[reminder.create]`.
9. Steward returns an ActionRequestProposal.
10. Kernel validates it and computes protected payload hash and idempotency key.
11. ActionRequest is persisted.
12. Policy Service confirms manifest capability declaration.
13. Cedar evaluates Steward + reminder.create + user ReminderCollection + direct-instruction context.
14. PolicyDecision `allow` is stored.
15. Local Reminder Capability creates one scheduled Reminder.
16. In one transaction, the kernel stores ActionResult, marks AgentRun successful, marks Task completed, appends Events, and creates completion Notification.
17. Notification Worker delivers the completion Notification.
18. At due time, Scheduler claims Reminder and creates one reminder-due Notification.
19. Notification Worker delivers it and marks Reminder delivered.
20. Inspection API shows a coherent timeline from Intent to due delivery.

### 18.2 Required record cardinality after success

For one unique interaction and one one-time reminder:

| Record | Count | Required state |
|---|---:|---|
| Intent | 1 | preserved |
| Task | 1 | completed |
| AgentRun | 1 | succeeded |
| ActionRequest | 1 | succeeded/applied |
| PolicyDecision | 1 | allow |
| ActionResult | 1 | success |
| Reminder | 1 | delivered after due flow |
| Task completion Notification | 1 | delivered |
| Reminder due Notification | 1 | delivered |
| Approval | 0 | not required |
| Events | at least 7 | append-only, ordered by time/ID |

Expected Event types:

```text
agtyle.intent.accepted.v1
agtyle.task.assigned.v1
agtyle.task.started.v1
agtyle.action.authorized.v1
agtyle.reminder.created.v1
agtyle.task.completed.v1
agtyle.notification.delivered.v1
agtyle.reminder.firing.v1
agtyle.reminder.delivered.v1
```

### 18.3 Duplicate submission

Submitting the same interaction idempotency key and identical body:

- returns the original Intent/Task receipt;
- creates no new Task, AgentRun, Action, Reminder, Event, or Notification;
- records an access log but no authoritative Event.

Submitting the same key with a different body returns `409` and creates nothing.

### 18.4 Interactive mode

The same request MAY run interactively when explicitly selected and within the configured time budget. It still creates a Task and uses Cedar. It returns `200` with the completed Reminder result and does not emit a redundant Task-completion Notification to the same active interaction. The scheduled due Notification remains required.

If the interactive deadline is reached before capability execution:

1. atomically leave the same Task in `assigned`;
2. switch execution mode to `delegated`;
3. return a persisted Task receipt;
4. let the Worker continue;
5. create the normal terminal Notification later.

The conversion MUST not create a second Task.

---

## 19. Testing Strategy

### 19.1 Test design rules

- Tests use an isolated temporary data directory.
- Unit and integration tests inject `FrozenClock` and `DeterministicIdGenerator`.
- Default tests make no external network call.
- Integration tests use a real temporary SQLite file, not only `:memory:`.
- Cedar integration tests use the real pinned CLI.
- Every bug fix includes a regression test that fails before the fix.
- Assertions verify domain state and external observations, not private implementation call order unless order is the contract.
- Randomized/stateful tests print reproducible seeds on failure.

### 19.2 Unit tests

At minimum:

```text
test_task_allows_every_declared_transition
test_task_rejects_every_undeclared_transition
test_terminal_task_cannot_reopen
test_failed_task_reassign_requires_retry_budget
test_payload_hash_is_order_independent
test_payload_hash_changes_for_every_protected_field
test_naive_datetime_is_rejected
test_due_time_must_be_future
test_dst_ambiguous_time_requires_disambiguation
test_dst_nonexistent_time_requires_clarification
test_reminder_delivery_key_is_stable
test_task_terminal_delivery_key_is_stable
test_approval_matches_exact_action
test_expired_or_consumed_approval_is_invalid
test_policy_service_maps_allow_deny_approval_and_error
test_retry_classifier_is_deterministic
```

Hypothesis state-machine tests MUST generate Task transitions and prove no illegal terminal escape, attempt overflow, or duplicate terminal notification key.

### 19.3 Contract tests

- every valid JSON fixture passes both JSON Schema and Pydantic;
- every invalid fixture fails both;
- `additionalProperties` is rejected;
- every Agent manifest validates;
- every registered capability has an Action schema and adapter registration;
- every adapter passes its port contract suite;
- Cedar schema and policy set validate;
- all P01–P10 policy cases pass;
- public API examples validate against OpenAPI output;
- Pydantic-generated schema drift is detected against committed public schemas.

### 19.4 Persistence tests

- migrations up/down/up;
- STRICT rejects wrong storage class;
- foreign keys are active;
- Event update/delete triggers reject mutations;
- transaction rollback leaves no partial Task/Notification state;
- duplicate Action idempotency key is rejected;
- duplicate Reminder idempotency key resolves to existing matching Reminder;
- same key with different payload raises conflict;
- two Workers conditionally claim only one Task;
- two Schedulers create one due Notification;
- two Notification Workers deliver one record through the recording adapter.

### 19.5 Integration tests

```text
test_delegated_reminder_returns_persisted_receipt
test_worker_creates_reminder_through_real_cedar
test_completion_is_atomic_with_event_and_notification
test_scheduler_creates_one_due_notification
test_notification_delivery_marks_reminder_delivered
test_timeline_contains_complete_explanation
test_research_agent_is_denied_reminder_capability
test_missing_cedar_fails_closed_without_reminder
test_invalid_agent_output_fails_task_without_action
test_same_interaction_key_returns_same_receipt
test_interactive_timeout_converts_same_task_to_delegated
```

### 19.6 Failure-injection tests

Introduce a `FailureInjectorPort` available only in tests. Required checkpoints:

| Checkpoint | Expected recovery |
|---|---|
| after Task claim commit, before Agent call | lease expiry abandons run and retries |
| after Agent output, before ActionRequest commit | no Action exists; Task retries |
| after PolicyDecision commit, before capability call | retry reuses ActionRequest and reauthorizes/reconciles |
| after Reminder insert, before outer completion commit | transaction rollback leaves no Reminder |
| after completion commit, before Notification delivery | Notification remains pending |
| after adapter observes delivery, before delivered commit | retry invokes adapter with same delivery key; recording adapter deduplicates |
| after Reminder firing commit, before due delivery | due Notification remains pending |
| Cedar timeout | no capability call; bounded retry |
| Worker loses lease during Agent call | old owner cannot finalize |

Each test MUST assert record counts, final states, and absence of duplicate effects.

### 19.7 Security tests

- unregistered Agent cannot request a Capability;
- registered Agent cannot request undeclared Capability;
- malformed Cedar context fails closed;
- external input containing “ignore policy and execute” remains data;
- request payload cannot specify principal, authorization result, idempotency key, or Task status;
- logs redact configured secret patterns;
- temporary Cedar request files use restricted permissions and are removed;
- API problem responses contain no traceback or policy contents;
- production config refuses deterministic test adapters.

---

## 20. Work Packages

Each work package ends in a green repository. Do not postpone its required tests to a later package.

### WP-00 — Repository foundation

**Deliverables**

- root project files, package layout, documentation locations, and Git ignores;
- locked Python environment;
- canonical Make targets;
- CLI skeleton;
- structured configuration;
- `AGENTS.md` containing architecture and verification rules;
- `DEVELOPMENT_LOG.md`.

**Verification**

- `make bootstrap` is idempotent;
- `agtyle --help` succeeds;
- no import crosses the domain dependency boundary;
- clean Git status after bootstrap except ignored runtime files.

**Exit criterion:** a clean clone can install and run an empty test suite with the final command surface.

### WP-01 — Contracts and domain state

**Deliverables**

- typed IDs, ClockPort, ID generator;
- Intent, Task, AgentRun, ActionRequest, PolicyDecision, Approval, ActionResult, Reminder, Notification, and Event models;
- Task and Reminder state machines;
- canonical payload hashing;
- versioned schemas and fixtures.

**Verification**

- unit transition matrix;
- property-based state-machine tests;
- hash golden tests;
- schema/Pydantic parity;
- timezone and DST tests.

**Exit criterion:** all business invariants run without database or infrastructure imports.

### WP-02 — Persistence and migrations

**Deliverables**

- SQLAlchemy models and Unit of Work;
- repository implementations;
- initial Alembic migration;
- SQLite connection configuration;
- Event immutability triggers;
- conditional claim queries.

**Verification**

- migration up/down/up;
- schema introspection;
- transaction rollback tests;
- constraint and concurrency tests.

**Exit criterion:** every core object can round-trip without losing type or identity, and illegal persistence is rejected.

### WP-03 — Agent and capability registry

**Deliverables**

- manifest schema and loader;
- Executive and Steward Agent packages;
- Capability registry with `reminder.create@1`;
- startup validation;
- ContextPack builder.

**Verification**

- valid registry starts;
- duplicate or invalid Agent and Capability configurations fail;
- ContextPack contains only allowed scopes and no secrets.

**Exit criterion:** the kernel can deterministically answer which Agent owns a Task and which Capabilities it may propose.

### WP-04 — Cedar authorization

**Deliverables**

- pinned Cedar installer;
- Cedar schema, policies, and data-driven policy tests;
- subprocess adapter;
- Policy Service three-state/error mapping;
- readiness integration.

**Verification**

- checksum and version checks;
- policy validation;
- P01–P10;
- timeout, missing binary, malformed output, malformed request;
- fail-closed proof that Capability adapter is not called.

**Exit criterion:** no Action can reach a Capability without a stored real Cedar decision.

### WP-05 — Interaction, dispatch, and Task Worker

**Deliverables**

- FastAPI interaction endpoint;
- interaction idempotency;
- deterministic Executive runtime;
- atomic Intent/Task assignment;
- Task receipt;
- Worker claim, heartbeat, retries, lease recovery;
- deterministic Steward runtime.

**Verification**

- receipt exists only after commit;
- duplicate request returns same receipt;
- two Workers claim once;
- lease-loss and exhaustion behavior;
- invalid Agent output behavior.

**Exit criterion:** a natural-language fixture becomes a running Steward AgentRun through durable Task state.

### WP-06 — Reminder Action vertical slice

**Deliverables**

- reminder Action schema;
- payload normalization;
- ActionRequest persistence;
- real Cedar authorization;
- local Reminder Capability;
- ActionResult;
- transactional Task completion and completion Notification.

**Verification**

- successful creation cardinality;
- unauthorized principals denied;
- invalid reminder rejected;
- duplicate retry creates one Reminder;
- completion rollback creates neither false completion nor orphan Notification.

**Exit criterion:** Task completion proves a Reminder was actually stored, not merely proposed.

### WP-07 — Scheduler and Notification closure

**Deliverables**

- due Reminder Scheduler;
- Notification Worker;
- recording and console adapters;
- retries, leases, stable delivery keys;
- Reminder terminal updates.

**Verification**

- due-before/not-due boundary;
- two Schedulers emit once;
- crash after firing commit recovers;
- delivery retry deduplicates;
- one completion and one due delivery are observed.

**Exit criterion:** the user-visible due effect occurs and is durably connected to the original Task.

### WP-08 — Inspection, observability, and recovery

**Deliverables**

- Task and Reminder inspection API/CLI;
- timeline read model;
- structured JSON logging;
- recovery command and startup recovery scan;
- optional no-op OpenTelemetry adapter boundary.

**Verification**

- timeline contains every required record and distinguishes current state from history;
- logs carry Task, AgentRun, and Action IDs;
- stale leases recover;
- no secret or protected full payload appears at INFO level.

**Exit criterion:** an operator can explain and recover a failed execution from durable state.

### WP-09 — Acceptance automation

**Deliverables**

- deterministic end-to-end test;
- live multi-process reminder demo;
- clean-clone verifier;
- verification report generator;
- local operations documentation.

**Verification**

- all canonical Make targets;
- fresh temporary clone;
- process restart and duplicate proof;
- machine-readable record cardinality and IDs.

**Exit criterion:** a second agent can reproduce acceptance without implementation author assistance.

---

## 21. Deterministic End-to-End Scenario

This scenario runs in CI without wall-clock waiting.

### 21.1 Arrange

- temporary SQLite file;
- `FrozenClock = 2026-08-09T22:00:00Z`;
- user `user_local`;
- conversation `conv_e2e`;
- timezone `America/Denver`;
- due time `2026-08-09T22:05:00Z`;
- deterministic Executive and Steward;
- real Cedar CLI;
- local Reminder Capability;
- recording Notification adapter.

### 21.2 Act and assert

1. POST interaction with `Idempotency-Key: e2e-reminder-001`.
2. Assert `202`, Task `assigned`, and receipt query finds committed state.
3. Run Task Worker once.
4. Assert Task completed and exactly one allowed PolicyDecision, ActionResult, Reminder, completion Notification.
5. Run Notification Worker once.
6. Assert completion Notification delivered.
7. Advance FrozenClock to one microsecond before due time.
8. Run Scheduler once; assert no due Notification.
9. Advance to exact due time.
10. Run two Scheduler instances concurrently; assert one due Notification.
11. Run two Notification Workers concurrently; assert one observed due delivery.
12. Restart service containers/objects using the same database.
13. Run all Workers again; assert counts unchanged.
14. Submit duplicate interaction; assert same receipt and counts unchanged.
15. Query timeline; assert all required identifiers link to the original Task.

### 21.3 Required output artifact

The test writes a redacted JSON proof containing:

```json
{
  "scenario": "deterministic_reminder_e2e",
  "status": "passed",
  "task_id": "task_...",
  "reminder_id": "rem_...",
  "cedar": {
    "version": "4.12.0",
    "decision": "allow",
    "policy_ids": ["permit-steward-direct-reminder"]
  },
  "counts": {
    "intents": 1,
    "tasks": 1,
    "agent_runs": 1,
    "action_requests": 1,
    "policy_decisions": 1,
    "action_results": 1,
    "reminders": 1,
    "notifications": 2
  },
  "deliveries": [
    "task_completed",
    "reminder_due"
  ],
  "restart_duplicate_check": "passed"
}
```

---

## 22. Live Multi-Process Reminder Demonstration

`make demo-reminder` MUST prove real process coordination and wall-clock scheduling. It uses a new temporary data directory and performs:

1. database migration and registry/Cedar validation;
2. start API on an available local port;
3. start one Task Worker, one Scheduler, and one Notification Worker;
4. wait for readiness with a bounded timeout;
5. submit “Remind me to verify Agtyle at <now + 5 seconds>”;
6. print the Task receipt;
7. wait for and print Task completion delivery;
8. wait for and print Reminder due delivery;
9. stop all roles cleanly;
10. restart them using the same database;
11. poll for three seconds;
12. verify no duplicate delivery;
13. print the Task timeline and final record-count summary;
14. terminate all child processes in `finally`, including failure paths.

Expected human-readable milestones:

```text
[PASS] readiness
[PASS] task accepted and assigned to steward
[PASS] Cedar allowed reminder.create
[PASS] reminder stored exactly once
[PASS] task completion notification delivered
[PASS] reminder due notification delivered
[PASS] restart produced no duplicate
[PASS] timeline and record cardinality verified
```

The script exits non-zero on timeout, missing milestone, duplicate, leaked process, or count mismatch. It MUST use bounded waits, never an unbounded sleep.

---

## 23. Clean-Clone Verification

`make clean-clone-verify` or `scripts/clean_clone_verify.py` MUST:

1. require a clean source worktree or record its dirty state;
2. create a temporary directory with a safe OS facility;
3. clone the repository at the current commit into that directory;
4. set a new temporary `AGTYLE_DATA_DIR`;
5. run `make bootstrap`, `make init`, and `make verify`;
6. run `make demo-reminder`;
7. collect versions, exit codes, durations, and proof artifact;
8. remove the temporary clone on success;
9. preserve or print its path on failure for diagnosis.

The verification MUST not depend on global editable installs, the source checkout's virtual environment, its database, or undeclared environment variables.

For CI, the same sequence SHOULD run on Linux. A second job SHOULD run the deterministic verification on macOS because the intended personal runtime includes macOS. Windows support may remain unverified but the Cedar installer must fail with a clear message rather than download the wrong binary.

---

## 24. Verification Report

The JSON report MUST conform to a committed schema and include:

```text
schema_version
generated_at
git_commit
git_dirty
os
architecture
python_version
cedar_version
migration_head
commands[] {name, command, exit_code, duration_ms}
tests[] {suite, passed, failed, skipped}
policy_cases[] {id, expected, actual}
e2e {task_id, reminder_id, record_counts, delivery_keys}
restart_duplicate_check
unmet_requirements[]
```

The Markdown report is a renderer of the JSON report, not a separately maintained source of truth. A successful report MUST have:

- zero failed commands;
- zero failed tests;
- zero skipped required tests;
- all policy cases matching expected results;
- exact required E2E cardinality;
- empty `unmet_requirements`.

---

## 25. Quality Gates

### 25.1 Merge gate

Every change MUST pass:

```text
ruff format --check
ruff check
mypy --strict src/agtyle
pytest tests/unit
pytest tests/contract
pytest tests/persistence
pytest tests/integration
pytest tests/failure_injection
pytest tests/e2e
alembic upgrade/downgrade verification
Cedar schema/policy validation and policy matrix
```

Required behavior may not be excluded by markers in CI.

### 25.2 Coverage

Coverage is a diagnostic, not the sole quality proof. Minimums:

- 90% branch coverage for domain and application modules;
- 80% branch coverage overall;
- 100% explicit case coverage for Task and Reminder state transitions;
- 100% of Cedar policy matrix cases;
- every failure-injection checkpoint exercised.

Generated code, migration scripts, and trivial CLI wiring MAY be excluded with documented configuration.

### 25.3 Performance sanity

On a normal developer machine after startup:

- interaction persistence and delegated receipt p95 under 250 ms without an LLM call;
- Task claim transaction p95 under 100 ms;
- local Cedar decision p95 under 250 ms using the CLI adapter;
- reminder due detection within one poll interval plus 250 ms;
- no database lock error under the two-Worker/two-Scheduler acceptance concurrency.

These are sanity targets, not hard real-time guarantees. The verification report records observed values and flags regressions above twice the target.

---

## 26. Security and Privacy Gate

Before handoff:

- run a repository secret scan;
- prove `.env`, database, raw prompts, credentials, and Cedar temporary inputs are not tracked;
- use opaque secret references only;
- ensure API and logs redact authorization context fields classified sensitive;
- ensure production mode requires an explicit real adapter configuration;
- ensure Cedar policy validation failure blocks Action execution;
- ensure no route, CLI, Agent, or repository method bypasses `ExecutionService → PolicyService → CapabilityPort`;
- ensure Event immutability is enforced both in repository API and database triggers.

A static architecture test SHOULD search imports and direct adapter calls to prevent route handlers and Agent runtimes from invoking Capability implementations.

---

## 27. Definition of Done

The reminder baseline is done only when every statement is true:

- [ ] The repository matches the required modular boundaries.
- [ ] A clean clone bootstraps from documented commands.
- [ ] All dependencies and Cedar are pinned and verified.
- [ ] Database migrations are reversible and verified.
- [ ] Every core domain model and required port exists.
- [ ] Executive and Steward manifests validate.
- [ ] A natural-language reminder interaction returns a persisted Task receipt.
- [ ] A Task Worker creates one AgentRun.
- [ ] The Steward output is schema-validated as untrusted data.
- [ ] One ActionRequest with a canonical hash and idempotency key is stored.
- [ ] The real Cedar engine allows the valid case and denies unauthorized cases.
- [ ] The Capability is never called on deny or error.
- [ ] Exactly one Reminder and one ActionResult exist.
- [ ] Task completion, Event, and completion Notification commit atomically.
- [ ] The completion Notification is delivered.
- [ ] The Scheduler creates exactly one due Notification.
- [ ] Due delivery marks the Reminder delivered.
- [ ] Restart and duplicate submission create no duplicate effect.
- [ ] Lease recovery and all required failure checkpoints pass.
- [ ] The timeline explains the execution with linked IDs.
- [ ] No secrets or runtime data are committed.
- [ ] `make verify`, `make demo-reminder`, and `make clean-clone-verify` pass.
- [ ] Verification reports are generated with no unmet requirements.
- [ ] `DEVELOPMENT_LOG.md` describes the implementation and limitations.

“The API returned 200,” “the Agent said it created a reminder,” or “the unit tests pass” is not sufficient.

---

## 28. Subsequent Vertical Slices

These begin only after the reminder Definition of Done passes.

### Slice B — Interactive email read

Proves a read-only external adapter, constrained account scope, minimal ContextPack, and synchronous response. Use a fake/recorded email provider in deterministic CI and an optional live provider smoke test.

### Slice C — Approval-gated email send

Proves draft Artifact creation, `REQUIRE_APPROVAL`, exact payload hash binding, expiry, single use, reauthorization, idempotent send/reconciliation, and final delivery confirmation. This is the required acceptance slice for the Approval model.

### Slice D — Background research and Knowledge proposal

Proves long-running delegated work, raw source registration, trust labels, compiled Wiki proposal, citations, and completion Notification. No independent vector database is introduced.

### Slice E — Read-only projections and operations

Proves projection rebuild, pending approvals, running/failed Tasks, notification delivery failures, and knowledge health without allowing dashboard writes to authoritative state.

Each slice MUST reuse the same Task, AgentRun, Action, Cedar, Notification, Event, and idempotency contracts. A slice that requires changing a core contract must include a migration, compatibility decision, and update to the architecture/specification.

---

## 29. Handoff Prompt for a New Implementation Agent

The following text may be given directly to a new coding agent together with the repository URL:

> Clone the Agtyle repository and implement the reminder vertical slice exactly as specified in `docs/specifications/agtyle-development-and-verification-specification.md`. Read `AGENTS.md` and the architecture design first. Work package by work package, add behavior and its tests together, preserve the modular-monolith and hexagonal boundaries, and do not bypass the real Cedar authorization path. The task is complete only when `make verify`, `make demo-reminder`, and `make clean-clone-verify` pass and the verification reports contain no unmet requirements. Maintain `DEVELOPMENT_LOG.md`, record any deviation before implementing it, and explicitly report every requirement you could not satisfy.

---

## 30. Final Acceptance Principle

Agtyle's first implementation is accepted when a fresh agent can prove, from a fresh clone and without privileged conversational context, that:

> a user's intent became durable assigned work; the correct Specialist proposed a typed Action; an independent policy engine authorized it; the capability created one real reminder; the system closed both the task-completion and due-time notification loops; and crashes, retries, concurrency, or restarts could not silently lose or duplicate the effect.

That proof establishes the execution spine on which email, research, knowledge, approvals, and future Agent organizations can be added safely.
