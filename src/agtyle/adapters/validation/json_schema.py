"""JSON Schema adapter over the committed contracts in ``contracts/schemas``.

The adapter loads every schema once at startup so that a missing or malformed contract is a
configuration failure rather than a runtime surprise on the Action path.
"""

from __future__ import annotations

import json
import zoneinfo
from pathlib import Path
from typing import Any, Final

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError

from agtyle.domain.common import ConfigurationInvalidError, JsonMapping
from agtyle.ports.schema_registry import SchemaValidationResult

CAPABILITY_KEY: Final = "x-agtyle-capability"
VERSION_KEY: Final = "x-agtyle-schema-version"

agtyle_format_checker = FormatChecker()


@agtyle_format_checker.checks("iana-time-zone", raises=())
def _is_iana_time_zone(value: object) -> bool:
    """Custom format so the committed schema rejects zones the runtime cannot resolve."""
    if not isinstance(value, str):
        return True
    try:
        zoneinfo.ZoneInfo(value)
    except Exception:
        return False
    return True


class JsonSchemaRegistry:
    """Loads ``contracts/schemas/v<n>`` and validates Action payloads against it."""

    def __init__(self, contracts_dir: Path) -> None:
        self._contracts_dir = contracts_dir
        self._by_capability: dict[tuple[str, int], str] = {}
        self._validators: dict[str, Draft202012Validator] = {}
        self._documents: dict[str, JsonMapping] = {}
        self._load()

    def _load(self) -> None:
        schema_root = self._contracts_dir / "schemas"
        if not schema_root.is_dir():
            raise ConfigurationInvalidError(f"missing contracts directory: {schema_root}")
        for path in sorted(schema_root.rglob("*.schema.json")):
            document = self._read(path)
            schema_id = str(document.get("$id") or path.name)
            Draft202012Validator.check_schema(document)
            self._documents[schema_id] = document
            self._validators[schema_id] = Draft202012Validator(
                document, format_checker=agtyle_format_checker
            )
            capability = document.get(CAPABILITY_KEY)
            version = document.get(VERSION_KEY)
            if isinstance(capability, str) and isinstance(version, int):
                key = (capability, version)
                if key in self._by_capability:
                    raise ConfigurationInvalidError(
                        f"duplicate schema for capability {capability}@{version}"
                    )
                self._by_capability[key] = schema_id
        if not self._validators:
            raise ConfigurationInvalidError(f"no schemas found under {schema_root}")

    @staticmethod
    def _read(path: Path) -> JsonMapping:
        try:
            document: Any = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ConfigurationInvalidError(f"invalid JSON in {path.name}: {exc.msg}") from exc
        if not isinstance(document, dict):
            raise ConfigurationInvalidError(f"schema {path.name} must be a JSON object")
        return document

    @property
    def schema_ids(self) -> list[str]:
        return sorted(self._validators)

    def document(self, schema_id: str) -> JsonMapping:
        try:
            return self._documents[schema_id]
        except KeyError as exc:
            raise ConfigurationInvalidError(f"unknown schema id: {schema_id}") from exc

    def schema_id_for(self, capability: str, schema_version: int) -> str | None:
        return self._by_capability.get((capability, schema_version))

    def validate_document(self, schema_id: str, document: JsonMapping) -> SchemaValidationResult:
        validator = self._validators.get(schema_id)
        if validator is None:
            return SchemaValidationResult(
                valid=False, schema_id=schema_id, errors=[f"unknown schema id: {schema_id}"]
            )
        errors = sorted(validator.iter_errors(document), key=_error_sort_key)
        return SchemaValidationResult(
            valid=not errors,
            schema_id=schema_id,
            errors=[_describe(error) for error in errors],
        )

    def validate_payload(
        self, *, capability: str, schema_version: int, payload: JsonMapping
    ) -> SchemaValidationResult:
        schema_id = self.schema_id_for(capability, schema_version)
        if schema_id is None:
            # Fail closed: an unregistered capability version has no contract to satisfy.
            return SchemaValidationResult(
                valid=False,
                schema_id=f"{capability}@{schema_version}",
                errors=[f"no committed schema for capability {capability}@{schema_version}"],
            )
        return self.validate_document(schema_id, payload)


def _error_sort_key(error: ValidationError) -> tuple[str, str]:
    return ("/".join(str(part) for part in error.absolute_path), error.message)


def _describe(error: ValidationError) -> str:
    location = "/".join(str(part) for part in error.absolute_path) or "<root>"
    return f"{location}: {error.message}"
