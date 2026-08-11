"""Committed JSON Schemas and the Pydantic models must accept and reject the same documents."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from agtyle.adapters.validation.json_schema import JsonSchemaRegistry
from agtyle.domain.common import AgtyleError
from agtyle.domain.reminders import ReminderCreatePayload

from .conftest import fixture_paths, schema_name

PYDANTIC_MODELS = {"reminder-create": ReminderCreatePayload}


def _load(path: Path) -> dict[str, object]:
    document: dict[str, object] = json.loads(path.read_text(encoding="utf-8"))
    return document


def _pydantic_accepts(path: Path) -> bool:
    model = PYDANTIC_MODELS[schema_name(path)]
    try:
        model(**_load(path))
    except (ValidationError, AgtyleError, TypeError):
        return False
    return True


def _schema_accepts(registry: JsonSchemaRegistry, path: Path) -> bool:
    return registry.validate_payload(
        capability="reminder.create", schema_version=1, payload=_load(path)
    ).valid


def test_fixture_directories_are_not_empty() -> None:
    assert fixture_paths("valid"), "no valid fixtures committed"
    assert fixture_paths("invalid"), "no invalid fixtures committed"


@pytest.mark.parametrize("path", fixture_paths("valid"), ids=lambda p: p.stem)
def test_every_valid_fixture_passes_json_schema(registry: JsonSchemaRegistry, path: Path) -> None:
    result = registry.validate_payload(
        capability="reminder.create", schema_version=1, payload=_load(path)
    )
    assert result.valid, result.errors


@pytest.mark.parametrize("path", fixture_paths("valid"), ids=lambda p: p.stem)
def test_every_valid_fixture_passes_pydantic(path: Path) -> None:
    assert _pydantic_accepts(path)


@pytest.mark.parametrize("path", fixture_paths("invalid"), ids=lambda p: p.stem)
def test_every_invalid_fixture_fails_json_schema(registry: JsonSchemaRegistry, path: Path) -> None:
    assert not _schema_accepts(registry, path)


@pytest.mark.parametrize("path", fixture_paths("invalid"), ids=lambda p: p.stem)
def test_every_invalid_fixture_fails_pydantic(path: Path) -> None:
    assert not _pydantic_accepts(path)


@pytest.mark.parametrize(
    "path", fixture_paths("valid") + fixture_paths("invalid"), ids=lambda p: p.stem
)
def test_json_schema_and_pydantic_agree(registry: JsonSchemaRegistry, path: Path) -> None:
    assert _schema_accepts(registry, path) is _pydantic_accepts(path)


def test_additional_properties_are_rejected(registry: JsonSchemaRegistry) -> None:
    payload = {
        "title": "submit the report",
        "scheduled_for_utc": "2026-08-10T21:00:00Z",
        "timezone": "America/Denver",
        "surprise": True,
    }
    assert not registry.validate_payload(
        capability="reminder.create", schema_version=1, payload=payload
    ).valid


def test_unregistered_capability_version_fails_closed(registry: JsonSchemaRegistry) -> None:
    result = registry.validate_payload(capability="reminder.create", schema_version=99, payload={})
    assert not result.valid
    assert "no committed schema" in result.errors[0]


def test_reminder_schema_declares_its_identity_and_version(registry: JsonSchemaRegistry) -> None:
    schema_id = registry.schema_id_for("reminder.create", 1)
    assert schema_id is not None
    document = registry.document(schema_id)
    assert document["$id"] == schema_id
    assert document["x-agtyle-schema-version"] == 1
    assert document["additionalProperties"] is False
    assert set(document["required"]) == {"title", "scheduled_for_utc", "timezone"}
