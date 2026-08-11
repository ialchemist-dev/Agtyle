"""Versioned Action schema validation.

Schema validation is a distinct fail-closed gate that runs before authorization: an Action
whose payload does not match its committed contract never reaches Cedar or an adapter.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import Field

from agtyle.domain.common import DomainModel, JsonMapping


class SchemaValidationResult(DomainModel):
    valid: bool
    schema_id: str
    errors: list[str] = Field(default_factory=list)

    def raise_for_status(self) -> None:
        from agtyle.domain.common import ActionSchemaInvalidError

        if not self.valid:
            raise ActionSchemaInvalidError("; ".join(self.errors) or "payload failed validation")


@runtime_checkable
class ActionSchemaPort(Protocol):
    def schema_id_for(self, capability: str, schema_version: int) -> str | None:
        """Return the committed schema identifier for a capability version, if registered."""
        ...

    def validate_payload(
        self, *, capability: str, schema_version: int, payload: JsonMapping
    ) -> SchemaValidationResult:
        """Validate a payload against its versioned schema, failing closed when unknown."""
        ...
