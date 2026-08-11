"""Cedar authorization through the pinned official CLI.

Design notes that matter for safety:

* The subprocess is invoked with an argument array, never a shell string, so nothing in a
  request can be interpreted as a command.
* Request and entity data are written to owner-only temporary files that are removed in a
  ``finally`` block, including on timeout.
* Every failure mode — missing binary, wrong version, timeout, unparsable output, request
  validation failure — maps to ``CedarDecision.ERROR``, which the Policy Service treats as
  fail-closed. An error is never confused with a policy ``deny``.
* Full request context is logged at DEBUG only; INFO carries identifiers and the decision.

A future in-process Rust or WASM adapter can replace this class behind ``AuthorizationPort``
without any application service noticing.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from agtyle.domain.actions import CedarDecision
from agtyle.domain.common import JsonMapping, redact_secrets
from agtyle.observability.logging import get_logger
from agtyle.ports.authorization import (
    AuthorizationRequest,
    CedarResult,
    PolicyValidationReport,
)

logger = get_logger(__name__)

ALLOW_TOKEN: Final = "ALLOW"  # noqa: S105 - a Cedar decision token, not a credential
DENY_TOKEN: Final = "DENY"  # noqa: S105 - a Cedar decision token, not a credential
_POLICY_NOTE: Final = "note: this decision was due to the following policies:"
_VERSION_PATTERN: Final = re.compile(r"(\d+\.\d+\.\d+)")

#: Reason codes are stable strings stored on every PolicyDecision.
REASON_ALLOW: Final = "cedar_allow"
REASON_DENY: Final = "cedar_deny"
REASON_BINARY_MISSING: Final = "cedar_binary_missing"
REASON_VERSION_MISMATCH: Final = "cedar_version_mismatch"
REASON_TIMEOUT: Final = "cedar_timeout"
REASON_INVALID_OUTPUT: Final = "cedar_invalid_output"
REASON_REQUEST_INVALID: Final = "cedar_request_invalid"
REASON_CONFIG_MISSING: Final = "cedar_configuration_missing"


@dataclass(frozen=True)
class CedarEntity:
    """One entity in the store passed to Cedar for a single authorization question."""

    entity_type: str
    entity_id: str
    attributes: JsonMapping

    def to_document(self) -> JsonMapping:
        return {
            "uid": {"type": f"Agtyle::{self.entity_type}", "id": self.entity_id},
            "attrs": self.attributes,
            "parents": [],
        }


class CedarCliAuthorization:
    """``AuthorizationPort`` implemented with the pinned Cedar CLI."""

    def __init__(
        self,
        *,
        binary: Path,
        schema: Path,
        policies: Path,
        pinned_version: str,
        timeout_seconds: float = 5.0,
    ) -> None:
        self._binary = binary
        self._schema = schema
        self._policies = policies
        self._pinned_version = pinned_version
        self._timeout = timeout_seconds

    # -- configuration ---------------------------------------------------------------

    def configuration_problem(self) -> str | None:
        """Return why Cedar cannot be used, or ``None`` when everything is present."""
        if not self._binary.is_file():
            return f"cedar binary not found at {self._binary}"
        if not os.access(self._binary, os.X_OK):
            return f"cedar binary at {self._binary} is not executable"
        if not self._schema.is_file():
            return f"cedar schema not found at {self._schema}"
        if not self._policies.is_file():
            return f"cedar policy set not found at {self._policies}"
        return None

    def engine_version(self) -> str | None:
        if not self._binary.is_file():
            return None
        completed = self._run(["--version"], timeout=self._timeout)
        if completed is None or completed.returncode != 0:
            return None
        match = _VERSION_PATTERN.search(completed.stdout)
        return match.group(1) if match else None

    # -- port ------------------------------------------------------------------------

    async def validate_policy_set(self) -> PolicyValidationReport:
        """Parse and validate schema and policies, then run a deny-by-default self-test."""
        problem = self.configuration_problem()
        if problem is not None:
            return PolicyValidationReport(valid=False, errors=[problem])

        version = self.engine_version()
        if version != self._pinned_version:
            return PolicyValidationReport(
                valid=False,
                engine_version=version,
                errors=[
                    f"cedar version {version!r} does not match the pinned {self._pinned_version!r}"
                ],
            )

        completed = self._run(
            [
                "-f",
                "json",
                "validate",
                "--schema",
                str(self._schema),
                "--policies",
                str(self._policies),
            ],
            timeout=self._timeout,
        )
        if completed is None:
            return PolicyValidationReport(
                valid=False, engine_version=version, errors=["cedar validate timed out"]
            )
        if completed.returncode != 0:
            return PolicyValidationReport(
                valid=False,
                engine_version=version,
                errors=_json_messages(completed.stdout, completed.stderr),
            )

        self_test = await self._deny_by_default_self_test()
        if self_test is not None:
            return PolicyValidationReport(valid=False, engine_version=version, errors=[self_test])

        return PolicyValidationReport(
            valid=True, engine_version=version, policy_ids=self.policy_ids()
        )

    async def authorize(self, request: AuthorizationRequest) -> CedarResult:
        problem = self.configuration_problem()
        if problem is not None:
            return CedarResult.error(REASON_CONFIG_MISSING, detail=problem)

        version = self.engine_version()
        if version != self._pinned_version:
            return CedarResult.error(
                REASON_VERSION_MISMATCH,
                detail=f"cedar version {version!r} is not the pinned {self._pinned_version!r}",
                engine_version=version,
            )

        return self._authorize_with_entities(request, entities_for(request), version)

    # -- internals -------------------------------------------------------------------

    def policy_ids(self) -> list[str]:
        """Extract the declared ``@id`` annotations so a report can list the policy set."""
        try:
            text = self._policies.read_text(encoding="utf-8")
        except OSError:  # pragma: no cover - configuration_problem covers the usual cases
            return []
        return sorted(set(re.findall(r'@id\("([^"]+)"\)', text)))

    async def _deny_by_default_self_test(self) -> str | None:
        """A principal with no permit must be denied. If it is not, the deployment is unsafe."""
        probe = AuthorizationRequest(
            principal_type="Agent",
            principal_id="agtyle_self_test_principal",
            action_id="reminder.create",
            resource_type="ReminderCollection",
            resource_id="agtyle_self_test_resource",
            context={
                "origin_user_id": "agtyle_self_test_user",
                "has_direct_user_instruction": False,
                "payload_hash": "sha256:" + "0" * 64,
                "approval_present": False,
                "approval_valid": False,
            },
        )
        result = self._authorize_with_entities(probe, entities_for(probe), self._pinned_version)
        if result.decision is CedarDecision.DENY:
            return None
        return (
            "deny-by-default self-test did not deny an unauthorized principal "
            f"(decision={result.decision.value}, reason={result.reason_code})"
        )

    def _authorize_with_entities(
        self,
        request: AuthorizationRequest,
        entities: list[CedarEntity],
        version: str | None,
    ) -> CedarResult:
        request_path: Path | None = None
        entities_path: Path | None = None
        try:
            request_path = _write_private_json(request.to_document())
            entities_path = _write_private_json([entity.to_document() for entity in entities])
            completed = self._run(
                [
                    "-f",
                    "json",
                    "authorize",
                    "--verbose",
                    "--schema",
                    str(self._schema),
                    "--policies",
                    str(self._policies),
                    "--entities",
                    str(entities_path),
                    "--request-json",
                    str(request_path),
                ],
                timeout=self._timeout,
            )
            if completed is None:
                return CedarResult.error(
                    REASON_TIMEOUT,
                    detail=f"cedar authorize exceeded {self._timeout}s",
                    engine_version=version,
                )
            return _interpret(completed, engine_version=version)
        finally:
            # Temporary request data must not survive the call, including on timeout.
            for path in (request_path, entities_path):
                if path is not None:
                    path.unlink(missing_ok=True)

    def _run(
        self, arguments: list[str], *, timeout: float
    ) -> subprocess.CompletedProcess[str] | None:
        command = [str(self._binary), *arguments]
        logger.debug("cedar invocation", extra={"cedar_arguments": arguments})
        try:
            # An argument array with shell=False: nothing in a request can become a command.
            return subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                shell=False,
            )
        except subprocess.TimeoutExpired:
            logger.warning("cedar invocation timed out", extra={"cedar_timeout_seconds": timeout})
            return None
        except OSError as exc:
            logger.warning("cedar invocation failed", extra={"cedar_error": str(exc)})
            return None


def entities_for(request: AuthorizationRequest) -> list[CedarEntity]:
    """Build the minimal entity store for one request.

    Resource ownership is an attribute of the resource so that a policy, not the caller, decides
    whether same-user scope is required.
    """
    origin_user = str(request.context.get("origin_user_id", ""))
    entities = [
        CedarEntity(
            entity_type=request.principal_type, entity_id=request.principal_id, attributes={}
        ),
        CedarEntity(
            entity_type=request.resource_type,
            entity_id=request.resource_id,
            attributes={"owner": request.resource_id},
        ),
    ]
    if origin_user:
        entities.append(CedarEntity(entity_type="User", entity_id=origin_user, attributes={}))
    return entities


def _interpret(
    completed: subprocess.CompletedProcess[str], *, engine_version: str | None
) -> CedarResult:
    """Map the CLI's exit code and output to a typed result, failing closed on anything odd."""
    stdout = completed.stdout or ""
    tokens = {line.strip() for line in stdout.splitlines()}

    if completed.returncode == 0 and ALLOW_TOKEN in tokens:
        return CedarResult(
            decision=CedarDecision.ALLOW,
            determining_policy_ids=_determining_policies(stdout),
            engine_version=engine_version,
            reason_code=REASON_ALLOW,
        )
    if completed.returncode == 2 and DENY_TOKEN in tokens:
        return CedarResult(
            decision=CedarDecision.DENY,
            determining_policy_ids=_determining_policies(stdout),
            engine_version=engine_version,
            reason_code=REASON_DENY,
        )

    messages = _json_messages(stdout, completed.stderr or "")
    reason = REASON_REQUEST_INVALID if completed.returncode == 1 else REASON_INVALID_OUTPUT
    return CedarResult(
        decision=CedarDecision.ERROR,
        errors=messages or [f"cedar exited with code {completed.returncode}"],
        engine_version=engine_version,
        reason_code=reason,
    )


def _determining_policies(stdout: str) -> list[str]:
    lines = stdout.splitlines()
    try:
        start = next(index for index, line in enumerate(lines) if _POLICY_NOTE in line)
    except StopIteration:
        return []
    policies: list[str] = []
    for line in lines[start + 1 :]:
        candidate = line.strip()
        if not candidate:
            break
        policies.append(candidate)
    return policies


def _json_messages(stdout: str, stderr: str) -> list[str]:
    """Collect Cedar's documented JSON diagnostics, redacted, without leaking policy bodies."""
    messages: list[str] = []
    for stream in (stdout, stderr):
        for line in stream.splitlines():
            candidate = line.strip()
            if not candidate.startswith("{"):
                continue
            try:
                document: Any = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if not isinstance(document, dict):
                continue
            message = str(document.get("message", "")).strip()
            causes = [str(cause) for cause in document.get("causes", []) if str(cause).strip()]
            combined = ": ".join(part for part in (message, "; ".join(causes)) if part)
            if combined:
                messages.append(redact_secrets(_strip_temp_paths(combined)))
    return messages


def _strip_temp_paths(message: str) -> str:
    """Replace temporary request file paths so diagnostics stay stable and disclose nothing."""
    return re.sub(r"\S*agtyle-cedar-\w+\.json", "<request>", message)


def _write_private_json(document: Any) -> Path:
    """Write JSON to an owner-only temporary file that the caller deletes in a finally block."""
    descriptor, name = tempfile.mkstemp(suffix=".json", prefix="agtyle-cedar-")
    path = Path(name)
    os.chmod(path, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(document, handle, ensure_ascii=False)
    return path
