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
- [x] WP-08 Inspection, observability and recovery
- [x] WP-09 Acceptance automation

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

---

## 2026-08-09 — WP-08 and WP-09: inspection, acceptance and the required seams

**D-029 — Log files in the live demo are per run.**
The first version of `scripts/reminder_demo.py` reused one log file per role, so restarting the
roles truncated the evidence of the first run and the duplicate check compared against nothing.
Logs are now `<role>.run<n>.log`. Worth stating plainly because the bug made the demo *pass*
its duplicate check for the wrong reason before the restart step, and only failed afterwards.
*Affected requirement:* §22.

**D-030 — `NotImplementedAdapter`s are wired into the container, not merely defined.**
§3.2 requires `KnowledgePort`, `SecretStorePort` and `WorkflowEnginePort` to exist with contract
tests even though the reminder path never calls them. Each has an adapter in
`agtyle.adapters.unimplemented` that raises a typed `ConfigurationInvalidError` naming the port
and the slice that will implement it, and each is placed on the container. If the reminder path
ever grows a dependency on one, it fails loudly at that call instead of silently succeeding.
*Affected requirement:* §3.2, rule 14.

**D-031 — Port contract suites are written against the port, not the implementation.**
`tests/contract/test_port_contracts.py` parametrizes the NotificationPort suite over both
adapters and exercises CapabilityPort through its protocol. A future MCP reminder capability or
Slack notifier is verified by exactly these tests, which is what §11 means by "adapters MUST pass
the same contract test suite".
*Affected requirement:* §11, §19.3.

**D-032 — Drift detection compares Pydantic models to the committed schemas directly.**
`tests/contract/test_schema_drift.py` asserts field-set and required-set equality between
`ReminderCreatePayload` and its JSON Schema, between `ActionRequestProposal` and the committed
envelope, and pins the error taxonomy and every persisted enum value. Those enum strings live in
`CHECK` constraints in a STRICT database, so a rename is a migration, not a refactor.
*Affected requirement:* §19.3.

**D-033 — Coverage floors are enforced by a script, not by a single global threshold.**
§25.2 sets different floors for different layers. `scripts/check_coverage.py` reads the coverage
JSON and enforces 90% branch coverage across `domain` and `application` separately from the 80%
overall floor. Observed on this machine: 91.86% core, 85.8% overall.
*Affected requirement:* §25.2.

**D-034 — Performance sanity is measured, not asserted.**
The report generator measures the §25.3 targets on the machine running it and flags anything
above twice its target. Observed p95 on an Apple M-series machine: interaction receipt 0.70 ms,
task claim 0.55 ms, Cedar decision 7.94 ms, due detection 0.18 ms.
*Affected requirement:* §25.3.

---

## 2026-08-10 — Registry snapshot gate (§10.6)

**D-035 — The registry snapshot MUST from §10.6 was initially missed and is now implemented.**
The `agents` and `capabilities` tables were created by the initial migration but nothing wrote
to them and nothing compared against them, so the sentence "Startup MUST fail when an enabled
database snapshot disagrees with the current manifest hash until an explicit registry-sync
command records the new version" was unmet. Recording it here because it was a genuine gap, not
a deviation.

`RegistrySyncService` now provides `check`, `require_consistent` and `sync`;
`agtyle registry check` and `agtyle registry sync` expose them; `agtyle init` syncs as part of
initialization; and `open_container` gates every role that will execute work. An empty snapshot
is deliberately *not* a disagreement — a fresh database has simply never been synced — while a
changed manifest hash, a removed Agent, or a changed capability registration all stop startup
with `AGT-SYSTEM-001` naming the subject and both hashes. Readiness reports the same state.

Snapshots are disabled rather than deleted, because a removed Agent still has to explain the
AgentRuns it produced. Verified by hand as well as by test: editing `display_name` in the
Steward manifest makes `agtyle worker --once` refuse to start, and `agtyle registry sync`
unblocks it.
*Affected requirement:* §10.6.

**D-036 — A malformed manifest is now a typed configuration error.**
Found while testing the gate: a manifest with invalid YAML escaped as a raw `yaml.YAMLError`
traceback instead of `AGT-SYSTEM-001`. `_load_agent` now converts parser failures, so every
registry failure mode reaches the operator through the same error taxonomy.
*Affected requirement:* §11.2, §12.1.

---

## 2026-08-10 — Handoff state

### What was verified, and how

| Gate | Result |
|---|---|
| `ruff format --check`, `ruff check` | pass, 121 files |
| `mypy --strict src/agtyle` | pass, 73 source files |
| `pytest tests` | 481 passed, 0 failed, 0 skipped |
| Branch coverage | 91.8% across `domain` and `application`, 88.2% overall |
| `scripts/migration_check.py` | up / down / up on a throwaway database, plus schema introspection |
| Cedar policy matrix | P01–P10 all matched, through the adapter and through `cedar run-tests` |
| `make verify` | pass, `unmet_requirements: []` |
| `make demo-reminder` | all eight milestones, four real processes, ~9 s |
| `make clean-clone-verify` | pass from a fresh clone at HEAD, ~37 s |
| Secret scan | 172 tracked files; the only matches are deliberate fake credentials in the redaction tests |

Nothing generated is tracked: no database, virtual environment, `.tools/`, `.env`, or
verification artifact other than `artifacts/verification/.gitkeep`. Only `.gitignore` and
`README.md` were modified among pre-existing files; the architecture design and the
specification are untouched.

### Unmet requirements

None of the specification's MUST requirements are known to be unmet. Every deviation is recorded
as a numbered decision above, and the two that change observable behaviour are D-019 (the Cedar
CLI reports decisions by exit code and token rather than JSON) and D-024/D-025 (one added error
code, and the legal `running -> failed -> assigned` path for interactive conversion).

### Known limitations

These are boundaries of the baseline, not defects. Each is explicitly out of scope in §3.3 or is
deferred to a later slice in §28.

1. **The Agent runtimes are deterministic, not model-backed.** `AgentRuntimePort` exists and the
   kernel is indifferent to what implements it, but no LLM adapter ships here. The deterministic
   Executive recognizes exactly one fixture family and refuses everything else rather than
   guessing. A live adapter belongs behind the `live_agent` pytest marker, which is declared and
   unused.
2. **`REQUIRE_APPROVAL` is modelled and tested, but nothing in the reminder path triggers it.**
   `reminder.create` is deliberately not approval-eligible. The Approval model, service, atomic
   consumption and Cedar mapping all exist and are covered, but Slice C (approval-gated email
   send) is the acceptance slice that exercises the whole path end to end.
3. **`KnowledgePort`, `SecretStorePort` and `WorkflowEnginePort` have no production adapters.**
   Each has a `NotImplementedAdapter` wired into the container that raises a typed error naming
   the port and the slice that will implement it.
4. **Windows is unverified.** The Cedar installer recognizes `x86_64-pc-windows-msvc` and would
   install the official asset, but no Windows CI job runs. Unsupported hosts fail with a clear
   message rather than downloading the wrong binary.
5. **Python 3.13 is permitted by metadata but not verified.** `requires-python` is
   `>=3.12,<3.14` as specified; the lock file, CI and the clean-clone verifier all resolve 3.12.
6. **Exactly-once delivery is guaranteed only for the local recording adapter.** Agtyle
   guarantees exactly-once Notification creation and at-least-once adapter invocation. Any future
   channel without an idempotent write must document its reconciliation strategy and residual
   duplicate risk, as §17.2 requires.
7. **The database is SQLite and the deployment is single-node.** Claim exclusion relies on
   `BEGIN IMMEDIATE` plus conditional updates on `row_version`. That is correct for processes
   sharing one filesystem; it is not a distributed lock.
