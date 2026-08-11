# Cedar authorization

Cedar answers exactly one question: **may this principal perform this action on this resource in
this context?** It does not validate business data. Whether a reminder's due time is in the
future is a domain rule, checked separately.

## Installation

```bash
python scripts/install_cedar.py            # installs to .tools/cedar/4.12.0/cedar
python scripts/install_cedar.py --force    # reinstall
```

The installer detects the host, downloads only the matching official release asset, verifies the
SHA-256 checksum the Cedar project publishes beside it, stages the binary, confirms it reports
the pinned version, and only then moves it into place. There is no `curl | sh` path. If
verification fails the download is discarded and any existing valid installation is untouched.

Unsupported hosts fail with a clear message naming the supported targets rather than downloading
a binary for another platform.

## Files

| File | Purpose |
|---|---|
| `policies/cedar/agtyle.cedarschema` | Entity types, actions and the exact context shape. |
| `policies/cedar/base.cedar` | The policy set. Every policy carries a stable `@id`. |
| `policies/cedar/tests.json` | The P01–P10 matrix, stored as data. |

## The request

Agtyle sends only policy-relevant values:

```json
{
  "origin_user_id": "user_local",
  "has_direct_user_instruction": true,
  "payload_hash": "sha256:...",
  "approval_present": false,
  "approval_valid": false
}
```

Resource ownership is an attribute of the resource entity, so same-user scope is enforced by a
policy rather than by an application check that a refactor could bypass.

## Outcomes

| Cedar answer | Agtyle outcome | Meaning |
|---|---|---|
| `allow` | `ALLOW` | The capability may run. |
| `deny`, capability not approval-eligible | `DENY` | Terminal. `reminder.create` is reversible and directly user-requested, so a denial is a denial. |
| `deny`, capability approval-eligible, no valid Approval | `REQUIRE_APPROVAL` | The Task parks and waits for a human. |
| anything else | `ERROR` | Fail closed. No capability runs. Bounded retry. |

`ERROR` is stored distinctly from `DENY` because they mean different things to an operator: one
is the system working correctly, the other is the system unable to decide.

## Running the matrix

```bash
make test-contract    # runs P01-P10 through the adapter and through Cedar's own runner
```

Two of the ten cases (P07, an action absent from the schema; P08, a wrongly typed context field)
fail *request validation* rather than producing allow or deny, so Cedar's `run-tests` command
cannot express them. They are still stored in `tests.json` and are asserted through the adapter,
where they must produce `ERROR` with reason `cedar_request_invalid`.

## Changing policy

1. Edit `policies/cedar/base.cedar`, giving every new policy a stable `@id`.
2. Add the case to `policies/cedar/tests.json` with its expected outcome.
3. Run `make test-contract`, then `agtyle validate`.

A policy set that fails validation blocks Action execution. That is intentional: an
un-validatable policy set is not a safe basis for authorizing anything.
