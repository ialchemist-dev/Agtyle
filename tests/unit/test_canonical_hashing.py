"""Canonical payload hashing: stable across serialization noise, sensitive to meaning."""

from __future__ import annotations

import json

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from agtyle.domain.actions import (
    compute_idempotency_key,
    compute_payload_hash,
    protected_document,
)
from agtyle.domain.common import InvalidRequestError, canonical_json, hash_document

BASE = {
    "principal_agent_id": "steward",
    "capability": "reminder.create",
    "resource_type": "ReminderCollection",
    "resource_id": "user_local",
    "schema_version": 1,
    "payload": {
        "title": "submit the report",
        "note": None,
        "scheduled_for_utc": "2026-08-10T21:00:00Z",
        "timezone": "America/Denver",
    },
}


def test_payload_hash_is_order_independent() -> None:
    reordered = {
        "payload": {
            "timezone": "America/Denver",
            "scheduled_for_utc": "2026-08-10T21:00:00Z",
            "note": None,
            "title": "submit the report",
        },
        "schema_version": 1,
        "resource_id": "user_local",
        "resource_type": "ReminderCollection",
        "capability": "reminder.create",
        "principal_agent_id": "steward",
    }
    assert compute_payload_hash(**BASE) == compute_payload_hash(**reordered)  # type: ignore[arg-type]


def test_payload_hash_ignores_insignificant_whitespace() -> None:
    spaced = json.loads(json.dumps(BASE, indent=4, separators=(" , ", " : ")))
    assert compute_payload_hash(**spaced) == compute_payload_hash(**BASE)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("principal_agent_id", "research"),
        ("capability", "reminder.update"),
        ("resource_type", "ReminderCollectionV2"),
        ("resource_id", "user_other"),
        ("schema_version", 2),
        ("payload", {**BASE["payload"], "title": "submit the reports"}),  # type: ignore[dict-item]
    ],
)
def test_payload_hash_changes_for_every_protected_field(field: str, value: object) -> None:
    changed = {**BASE, field: value}
    assert compute_payload_hash(**changed) != compute_payload_hash(**BASE)  # type: ignore[arg-type]


def test_hash_format_is_sha256_lowercase_hex() -> None:
    digest = compute_payload_hash(**BASE)  # type: ignore[arg-type]
    algorithm, _, hex_digest = digest.partition(":")
    assert algorithm == "sha256"
    assert len(hex_digest) == 64
    assert hex_digest == hex_digest.lower()


def test_protected_document_excludes_control_values() -> None:
    document = protected_document(**BASE)  # type: ignore[arg-type]
    assert set(document) == {
        "capability",
        "payload",
        "principal_agent_id",
        "resource_id",
        "resource_type",
        "schema_version",
    }


def test_idempotency_key_is_stable_for_the_same_task_and_payload() -> None:
    digest = compute_payload_hash(**BASE)  # type: ignore[arg-type]
    key = compute_idempotency_key(
        user_id="user_local",
        task_id="task_1",
        capability="reminder.create",
        payload_hash=digest,
    )
    assert key == compute_idempotency_key(
        user_id="user_local",
        task_id="task_1",
        capability="reminder.create",
        payload_hash=digest,
    )


def test_idempotency_key_changes_with_the_protected_payload() -> None:
    first = compute_idempotency_key(
        user_id="user_local",
        task_id="task_1",
        capability="reminder.create",
        payload_hash=compute_payload_hash(**BASE),  # type: ignore[arg-type]
    )
    changed = {**BASE, "payload": {**BASE["payload"], "title": "other"}}  # type: ignore[dict-item]
    second = compute_idempotency_key(
        user_id="user_local",
        task_id="task_1",
        capability="reminder.create",
        payload_hash=compute_payload_hash(**changed),  # type: ignore[arg-type]
    )
    assert first != second


def test_canonical_json_sorts_keys_and_removes_whitespace() -> None:
    assert canonical_json({"b": 1, "a": [1, {"d": 2, "c": 3}]}) == '{"a":[1,{"c":3,"d":2}],"b":1}'


def test_canonical_json_emits_non_ascii_literally() -> None:
    assert canonical_json({"t": "提交"}) == '{"t":"提交"}'


@pytest.mark.parametrize("value", [float("nan"), float("inf"), {1: "int key"}, {"s": {1, 2}}])
def test_canonical_json_rejects_values_without_a_canonical_form(value: object) -> None:
    with pytest.raises(InvalidRequestError):
        canonical_json(value)


json_documents = st.recursive(
    st.none() | st.booleans() | st.integers() | st.text(),
    lambda children: (
        st.lists(children, max_size=4)
        | st.dictionaries(st.text(min_size=1, max_size=8), children, max_size=4)
    ),
    max_leaves=12,
)


@settings(max_examples=100, deadline=None)
@given(document=json_documents)
def test_hash_survives_a_json_round_trip(document: object) -> None:
    """Serializing and re-parsing must never change the hash of a document."""
    assert hash_document(document) == hash_document(json.loads(json.dumps(document)))
