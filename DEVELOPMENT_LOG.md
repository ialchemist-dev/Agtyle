# Development Log

Chronological record of material decisions, deviations and their reasons. Newest first.

---

## 2026-08-09 — WP-00 Repository foundation

**Baseline command check:** `NO_BASELINE_COMMAND`. The repository contained only `README.md`,
`.gitignore` and the two `docs/` documents at `3f0569d`. There was no test suite, no build
configuration and no verification command to run before starting, so the canonical Make targets
in this log's work packages establish the first baseline.

**Branch:** `implement/reminder-vertical-slice`, created from `main` at `3f0569d`.

### Work package checklist

- [x] WP-00 Repository foundation
- [x] WP-01 Contracts and domain state
- [x] WP-02 Persistence and migrations
- [x] WP-03 Agent and capability registry
- [x] WP-04 Cedar authorization
- [x] WP-05 Interaction, dispatch and Task Worker
- [x] WP-06 Reminder Action vertical slice
- [x] WP-07 Scheduler and Notification closure
- [ ] WP-08 Inspection, observability and recovery
- [ ] WP-09 Acceptance automation

### Decisions

**D-001 — `uv` is the dependency manager, with a committed `uv.lock`.**
Specification §7.1 requires a committed lock file and PEP 621 metadata. `uv` provides both plus
reproducible interpreter provisioning, which the clean-clone verifier depends on.
*Affected requirement:* §7.1.

**D-002 — The verified interpreter is pinned to CPython 3.12 via `.python-version`.**
`requires-python` stays `>=3.12,<3.14` as specified, but the lock file, CI and the clean-clone
verifier all resolve 3.12 so that verification is reproducible. 3.13 is permitted by metadata and
is **not yet verified**; §7.1 explicitly allows this until both versions are proven.
*Affected requirement:* §7.1.

**D-003 — UUIDv7 is generated in-process rather than through a third-party package.**
`agtyle.ports.id_generator.Uuid7Generator` implements RFC 9562 version 7 with a 12-bit monotonic
counter in `rand_a`, so identifiers sort by creation time even within the same millisecond. This
removes a dependency whose maintenance status would otherwise have to be tracked, and it keeps the
deterministic test generator in the same module and behind the same port.
*Affected requirement:* §9.1.

**D-004 — Ports are declared `async`, but the SQLite adapters execute synchronously.**
The specification's port signatures are `async`. SQLite in the local-first baseline is a
file-backed, single-writer store; an async driver would add a dependency without removing any
blocking work. The persistence adapter therefore implements the `async` protocol methods over
synchronous SQLAlchemy 2 calls. The seam is unchanged, so a future networked store can be made
genuinely concurrent without touching application code.
*Affected requirement:* §11.

**D-005 — Canonical hashing uses RFC 8785 semantics implemented over `json.dumps`.**
`sort_keys=True`, `separators=(",", ":")`, `ensure_ascii=False` and `allow_nan=False` reproduce
JCS for the value space Agtyle allows in a protected payload (objects, arrays, strings, booleans,
integers, finite floats, null). Values outside that space are rejected rather than silently
coerced. Contract tests in WP-01 prove order- and whitespace-independence and per-field
sensitivity, as §9.6 requires.
*Affected requirement:* §9.6.

---

## 2026-08-09 — WP-01 Contracts and domain state

**D-006 — A distinct `ActionSchemaPort` was added for versioned Action payload validation.**
The specification requires schema validation to be a fail-closed gate that runs before Cedar
(§15.3 step 3) but does not name a port for it. Rather than let an application service read
`contracts/` from disk, `agtyle.ports.schema_registry.ActionSchemaPort` declares the seam and
`agtyle.adapters.validation.json_schema.JsonSchemaRegistry` implements it. This keeps filesystem
access inside an adapter, as §11 requires.
*Affected requirement:* §11, §14.1, §15.3.

**D-007 — `timezone` uses a custom `iana-time-zone` JSON Schema format.**
§14.1 requires JSON Schema and Pydantic to agree on every fixture. A pattern cannot express "this
is a resolvable IANA zone", so the committed schema declares
`"format": "iana-time-zone"` and the validator registers a checker backed by `zoneinfo`. The
`reminder-create.unknown-timezone` fixture is therefore rejected by both validators for the same
reason, instead of only by Pydantic.
*Affected requirement:* §14.1.

**D-008 — `scheduled_for_utc` accepts only RFC 3339 UTC with a literal `Z`.**
§9.2 requires one storage representation. Allowing `-06:00` on the wire would mean two spellings
of the same instant hash differently in a protected payload, so the schema pattern and
`parse_utc_instant` both require `Z`. The local wall-clock time the user actually said is
preserved separately in `timezone`, and `resolve_local_time` converts it.
*Affected requirement:* §9.2, §9.6, §14.1.

**D-009 — `wait_for_approval` releases the Worker lease.**
A Task parked for a human decision may wait far longer than a lease. Holding the lease would make
every such Task look like a crashed Worker to the recovery scan. The property-based state machine
found this: the "only running tasks hold a lease" invariant failed. `resume` acquires a fresh
lease when work continues.
*Affected requirement:* §9.4, §16.1.

**D-010 — Retry jitter is deterministic, not random.**
§16.4 requires "bounded jitter injected through a policy object" and zero jitter in tests.
`RetryPolicy` derives jitter from a SHA-256 of the seed identifier, so production still spreads
retries across Tasks while the same input always produces the same schedule.
*Affected requirement:* §16.4.

---

## 2026-08-09 — WP-02 Persistence and migrations

**D-011 — The initial migration snapshots `models.metadata`; later revisions must not.**
Declaring the schema twice (once for the ORM, once in migration DDL) is the usual source of
drift, and §10.5 demands introspection proof rather than trust. The single initial revision
therefore calls `metadata.create_all`, and `scripts/migration_check.py` verifies the *result* on
a throwaway database: up, revision equals head, down to base, up again, then required tables,
indexes, unique constraints, STRICT declarations, Event triggers, foreign-key enforcement and
JSON validity. Any later revision must use explicit Alembic operations so it stays frozen.
*Affected requirement:* §10.3, §10.5.

**D-012 — `STRICT` is emitted by a SQLAlchemy compiler hook.**
SQLAlchemy 2 has no dialect flag for SQLite STRICT tables. `models._compile_create_table`
appends `STRICT` for tables carrying `info={"sqlite_strict": True}`, so the declaration lives with
the table rather than in hand-written DDL.
*Affected requirement:* §10.3.

**D-013 — Booleans and timestamps have one storage form each.**
STRICT tables have no BOOLEAN type, so `single_use` and `enabled` are INTEGER with
`CHECK(col IN (0,1))`. All timestamps are TEXT in the canonical `%Y-%m-%dT%H:%M:%S.%fZ` UTC form,
which sorts lexicographically, so due-time and lease comparisons work directly in SQL.
*Affected requirement:* §9.2, §10.6.

**D-014 — Claims use `BEGIN IMMEDIATE` plus a conditional update on `row_version`.**
SQLite defers the write lock until the first write, which turns read-then-write claiming into a
late upgrade that can fail with `SQLITE_BUSY`. The Unit of Work opens `BEGIN IMMEDIATE`, so
competing processes queue on the busy timeout instead. Exclusion itself comes from the
conditional `UPDATE ... WHERE status = ? AND row_version = ?`: the winner is whichever process
changes exactly one row. Threaded tests prove two Workers claim one Task, two Schedulers create
one due Notification, and two Notification Workers claim one delivery.
*Affected requirement:* §10.2, §14.4, §16.1, §19.4.

**D-015 — A STRICT TEXT column still accepts numeric literals.**
SQLite applies TEXT affinity before the STRICT check, so `INSERT ... VALUES (12.5)` into a TEXT
column stores `'12.5'` rather than failing. The storage-class test therefore uses a BLOB into
TEXT and a non-numeric string into INTEGER, which STRICT does reject. Worth knowing: STRICT
prevents type confusion, it does not prevent lossless coercion.
*Affected requirement:* §19.4.

---

## 2026-08-09 — WP-03 Agent and capability registry

**D-016 — An Agent's directory name must equal its manifest id.**
§12.1 requires startup to reject duplicate Agent id/version pairs. Binding the directory name to
the id makes a duplicate structurally impossible rather than merely detected, and it means the
filesystem layout is itself part of the checked contract. The registry test asserts the failure
mode explicitly.
*Affected requirement:* §12.1.

**D-017 — Accepted and produced types are drawn from a closed kernel-owned set.**
`REGISTERED_ASSIGNMENT_TYPES` and `REGISTERED_OUTPUT_TYPES` in `adapters/registry.py` list what
the kernel can actually route. A manifest naming anything else fails startup, which is what
"an accepted or produced type that is unregistered" means in §12.1. Adding a slice means adding
its types here deliberately.
*Affected requirement:* §12.1.

**D-018 — ContextPack filtering is allow-list by declared scope.**
`SCOPE_PREFERENCES` maps each declared `context_scopes` entry to the exact preference keys it
unlocks; everything else is dropped, and credential-shaped keys are dropped even if a scope
would have admitted them. A deny-list would silently leak the next preference key someone adds.
*Affected requirement:* §9.2 of the architecture, §12.1.

---

## 2026-08-09 — WP-04 Cedar authorization

**D-019 — The Cedar CLI reports decisions as text and exit codes, not JSON.**
§15.1 asks the adapter to "parse only documented JSON output". Cedar CLI 4.12.0 has no JSON
decision format: `cedar authorize` prints `ALLOW`/`DENY` and exits 0 / 2, and reserves JSON for
diagnostics under `-f json`. The adapter therefore keys the decision off the documented exit
code plus an exact token match, and parses only Cedar's JSON diagnostics for error text. Any
other exit code, any missing token, and any unparsable output map to `CedarDecision.ERROR`,
which is fail-closed. This is a deviation from the letter of §15.1 and is recorded here rather
than silently implemented.
*Affected requirement:* §15.1.

**D-020 — Resource ownership is a Cedar entity attribute, not a caller assertion.**
`ReminderCollection::"<user_id>"` carries `owner`, and `permit-steward-direct-reminder` requires
`resource.owner == context.origin_user_id`. Case P03 (a collection belonging to another user)
is therefore denied by policy rather than by an application-level check that a future refactor
could bypass.
*Affected requirement:* §15.2, §15.4.

**D-021 — P07 and P08 are stored in `tests.json` but cannot run through `cedar run-tests`.**
Cedar's own test runner expresses only `allow`/`deny`. An undeclared action (P07) and a
wrongly-typed context field (P08) fail *request validation*, which is a third outcome. All ten
cases live in `policies/cedar/tests.json` as required, each tagged with
`agtyle.cedar_evaluable`; the Python contract test runs all ten through the real adapter and
asserts P07/P08 produce `ERROR` with reason `cedar_request_invalid`, and separately feeds the
eight evaluable cases to `cedar run-tests` so the data is validated by Cedar itself too.
*Affected requirement:* §15.4.

**D-022 — Policy validation includes a deny-by-default self-test.**
§15.5 step 4 requires a minimal self-test at startup. `validate_policy_set` authorizes a
synthetic principal that no policy mentions and requires the answer to be `DENY`. A policy set
that validates but somehow permits an unknown principal makes the deployment unready.
*Affected requirement:* §15.5.

**D-023 — Cedar diagnostics have temporary paths stripped before storage.**
Request data is written to `0600` temporary files removed in a `finally` block, but Cedar names
that path in its error text. `_strip_temp_paths` rewrites it to `<request>` so stored
PolicyDecisions and API problem responses are stable and disclose nothing about the filesystem.
*Affected requirement:* §15.1, §19.7.

---

## 2026-08-09 — WP-05 to WP-07: interaction, execution, reminders and notifications

**D-024 — The error taxonomy gained one code: `AGT-AGENT-003 agent_runtime_timeout`.**
§16.4 requires an `agent_runtime_transient` class whose example is "model timeout", but the
§11.2 taxonomy has no code for it: `AGT-AGENT-001` is invalid output and `AGT-AGENT-002` is an
unsupported assignment. Rather than mislabel a timeout as a capability failure, one code was
added. No existing code changed meaning, so the taxonomy remains stable for consumers.
*Affected requirement:* §11.2, §16.4.

**D-025 — Interactive conversion of an already-claimed Task uses `running -> failed -> assigned`.**
§18.4 requires the interactive deadline to "atomically leave the same Task in `assigned`" without
creating a second Task. By then the interactive runner has already claimed the Task, so it is
`running`. Adding a `running -> assigned` edge would weaken the §9.4 transition matrix, so
`convert_to_delegated` instead fails the attempt with `AGT-AGENT-003` and reassigns it through the
declared `failed -> assigned` retry edge. The Task id, Intent and receipt are unchanged; the
abandoned AgentRun is marked `abandoned`; and the audit trail states plainly that one attempt was
made and did not finish in budget.
*Affected requirement:* §9.4, §18.4.

**D-026 — A Cedar engine error requeues the Task rather than failing it.**
§16.4 classifies `authorization_error` as "Yes, bounded" for retry. So a missing binary or a
timeout stores a `PolicyDecision` with `decision=error`, leaves no Reminder and no ActionResult,
and returns the Task to `assigned` within its attempt budget. This is visibly different from a
policy `deny`, which is terminal. The integration test asserts both the stored decision and the
absence of any capability invocation.
*Affected requirement:* §15.3, §16.4.

**D-027 — The due Notification is routed to the originating conversation.**
A Reminder does not store an origin channel, so `ReminderService` reads
`task.origin.conversation_id` from the source Task inside the same claim transaction. The due
notification therefore reaches the same conversation that asked for it, and the Notification is
linked to both the Reminder and the Task for the timeline.
*Affected requirement:* §9.10, §14.4.

**D-028 — `RecordingNotificationAdapter` deduplicates by delivery key.**
§17.2 is explicit that exactly-once delivery cannot be promised for an arbitrary channel. Agtyle
guarantees exactly-once Notification *creation* through the unique delivery key and at-least-once
adapter invocation. The recording adapter closes the last gap locally by suppressing a repeated
delivery key, which is what makes "exactly-once observed delivery" assertable in acceptance.
*Affected requirement:* §17.2.
