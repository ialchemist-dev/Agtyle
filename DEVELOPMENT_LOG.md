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
- [ ] WP-02 Persistence and migrations
- [ ] WP-03 Agent and capability registry
- [ ] WP-04 Cedar authorization
- [ ] WP-05 Interaction, dispatch and Task Worker
- [ ] WP-06 Reminder Action vertical slice
- [ ] WP-07 Scheduler and Notification closure
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
