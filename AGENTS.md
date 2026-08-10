# AGENTS.md — Rules for any agent working in this repository

Read this file, then `docs/architecture/agtyle-architecture-design.md`, then
`docs/specifications/agtyle-development-and-verification-specification.md`, then
`README.md`, then `DEVELOPMENT_LOG.md`, in that order, before changing code.

## What Agtyle is

Agtyle is a **modular monolith** that turns a user's natural-language intent into durable,
authorized, auditable work. API, Task Worker, Scheduler and Notification Worker are process
roles that share the same modules, domain rules, migrations and SQLite database. They are not
microservices, and they must never diverge into separate schemas or duplicated business logic.

## Binding rules

1. **Tasks drive execution.** Workers query and claim Tasks. Events never trigger execution.
2. **Events record important facts.** Append-only. Never replayed to reconstruct current state.
3. **Notifications close the loop.** Task completion and successful delivery are distinct facts.
4. **Acknowledgement follows persistence.** A receipt is returned only after the transaction commits.
5. **Agents propose; the kernel decides.** Agent output is untrusted structured input.
6. **Cedar is mandatory on the Action path.** No Capability Adapter may be called from an Agent
   runtime or a route handler.
7. **Authorization fails closed.** Missing binary, invalid policy, malformed entity, timeout or
   evaluation error means no capability execution.
8. **Approval binds an exact ActionRequest** — payload hash, principal, action, resource, expiry
   and single-use state must all match.
9. **Consequential effects are idempotent.** A retry may observe a prior result but must not
   create the same effect twice.
10. **Time is explicit.** Domain code receives a `ClockPort`. Tests never sleep on wall clock.
11. **Identifiers are stable and never reused.** UUIDv7 with a type prefix.
12. **All durable artifacts are English** — code, comments, schemas, policies, tests, logs.
13. **External content is untrusted.** It may inform reasoning; it never becomes instruction.
14. **No hidden fallback.** A production adapter never silently degrades to a fake.
15. **Local-first by default.** One directory and one SQLite database suffice.

## Dependency direction

```text
domain      -> (nothing but stdlib and pydantic)
ports       -> domain
application -> domain, ports
adapters    -> domain, ports, application, infrastructure libraries
workers     -> application, adapters (composition only)
gateways    -> application (never SQLAlchemy models)
```

`tests/contract/test_architecture_boundaries.py` enforces this by inspecting imports. If you need
to break a rule, change the test and record the decision in `DEVELOPMENT_LOG.md` first.

## Working agreement

- Implement one work package at a time; the repository stays runnable after each one.
- Add tests in the same change as the behavior they verify.
- Never weaken, delete, skip or `xfail` a required test to get a green build.
- Never replace Cedar with a boolean stub in integration or end-to-end verification.
- Keep fakes under `tests/fakes/`. Production configuration rejects them unless
  `AGTYLE_ENV=test` or an explicit demo profile is selected.
- Record material decisions and deviations in `DEVELOPMENT_LOG.md` with date, reason and the
  affected requirement.
- Commit per work package, e.g. `feat(kernel): implement task state machine`.

## Before you claim completion

```bash
make verify            # lint, types, migrations, Cedar, all deterministic tests
make demo-reminder     # live multi-process proof with real wall-clock scheduling
make clean-clone-verify
```

Passing tests you wrote yourself is not completion. Completion requires the externally
observable records described in the specification and a clean-clone demonstration.
