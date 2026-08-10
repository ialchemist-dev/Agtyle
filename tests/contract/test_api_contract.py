"""Public API contract: shapes, status codes, problem details and OpenAPI agreement."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agtyle.adapters.gateways.api import PROBLEM_CONTENT_TYPE, create_app
from agtyle.adapters.notifications.recording import RecordingNotificationAdapter
from agtyle.bootstrap import Container, build_container, migrate
from agtyle.config import Settings
from agtyle.ports.clock import FrozenClock
from agtyle.ports.id_generator import DeterministicIdGenerator

BODY: dict[str, Any] = {
    "user_id": "user_local",
    "conversation_id": "conv_demo",
    "channel": "api",
    "input": "Remind me to submit the report at 2026-08-10T21:00:00Z",
    "preferred_execution_mode": "delegated",
}


@pytest.fixture
def container(settings: Settings, clock: FrozenClock) -> Iterator[Container]:
    migrate(settings)
    built = build_container(
        settings,
        clock=clock,
        ids=DeterministicIdGenerator(),
        notification_adapters={"recording": RecordingNotificationAdapter()},
        configure_logs=False,
    )
    yield built
    built.dispose()


@pytest.fixture
def client(container: Container) -> Iterator[TestClient]:
    with TestClient(create_app(container=container)) as test_client:
        yield test_client


def test_liveness_does_not_depend_on_anything(client: TestClient) -> None:
    response = client.get("/health/live")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_readiness_reports_database_registry_and_cedar(client: TestClient) -> None:
    response = client.get("/health/ready")
    assert response.status_code == 200
    checks = response.json()["checks"]
    assert checks["database_migrated"] is True
    assert checks["cedar_binary_present"] is True
    assert checks["cedar_policies_valid"] is True
    assert checks["cedar_version"] == "4.12.0"
    assert set(checks["registry_agents"]) == {"executive", "steward"}
    # Readiness must not require a Worker to be polling.
    assert "worker" not in str(checks).lower()


def test_delegated_interaction_returns_202_with_a_receipt(client: TestClient) -> None:
    response = client.post("/v1/interactions", json=BODY, headers={"Idempotency-Key": "k1"})
    assert response.status_code == 202
    document = response.json()
    assert document["intent_id"].startswith("int_")
    receipt = document["task_receipt"]
    assert receipt["status"] == "assigned"
    assert receipt["assigned_agent_id"] == "steward"
    assert receipt["execution_mode"] == "delegated"
    assert receipt["task_id"].startswith("task_")

    # The receipt must describe committed state, readable through a separate request.
    stored = client.get(f"/v1/tasks/{receipt['task_id']}")
    assert stored.status_code == 200
    assert stored.json()["status"] == "assigned"


def test_replaying_the_same_key_and_body_returns_the_same_receipt(client: TestClient) -> None:
    first = client.post("/v1/interactions", json=BODY, headers={"Idempotency-Key": "k1"})
    second = client.post("/v1/interactions", json=BODY, headers={"Idempotency-Key": "k1"})
    assert second.status_code == 202
    assert second.json()["task_receipt"]["task_id"] == first.json()["task_receipt"]["task_id"]


def test_reusing_a_key_with_a_different_body_is_409(client: TestClient) -> None:
    client.post("/v1/interactions", json=BODY, headers={"Idempotency-Key": "k1"})
    response = client.post(
        "/v1/interactions",
        json={**BODY, "input": "Remind me to do something else at 2026-08-10T21:00:00Z"},
        headers={"Idempotency-Key": "k1"},
    )
    assert response.status_code == 409
    assert response.headers["content-type"].startswith(PROBLEM_CONTENT_TYPE)
    problem = response.json()
    assert problem["code"] == "AGT-INPUT-003"
    assert set(problem) >= {"type", "title", "status", "detail", "code"}


def test_a_missing_idempotency_key_is_rejected(client: TestClient) -> None:
    assert client.post("/v1/interactions", json=BODY).status_code == 422


def test_an_unknown_field_is_rejected(client: TestClient) -> None:
    response = client.post(
        "/v1/interactions",
        json={**BODY, "principal_agent_id": "steward"},
        headers={"Idempotency-Key": "k1"},
    )
    assert response.status_code == 422


def test_a_request_needing_clarification_returns_200_without_a_task(client: TestClient) -> None:
    response = client.post(
        "/v1/interactions",
        json={**BODY, "input": "Remind me to call mum sometime"},
        headers={"Idempotency-Key": "k2"},
    )
    assert response.status_code == 200
    document = response.json()
    assert document["outcome"] == "clarification_required"
    assert "task_receipt" not in document
    assert document["missing"]


def test_unknown_task_returns_a_problem_document(client: TestClient) -> None:
    response = client.get("/v1/tasks/task_00000000-0000-7000-8000-00000000dead")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith(PROBLEM_CONTENT_TYPE)


def test_problem_documents_contain_no_traceback_or_policy_text(client: TestClient) -> None:
    client.post("/v1/interactions", json=BODY, headers={"Idempotency-Key": "k1"})
    response = client.post(
        "/v1/interactions",
        json={**BODY, "input": "Remind me to do something else at 2026-08-10T21:00:00Z"},
        headers={"Idempotency-Key": "k1"},
    )
    body = response.text.lower()
    for forbidden in ("traceback", "permit (", "forbid (", ".cedar", "sqlalchemy", 'file "'):
        assert forbidden not in body, f"response leaked {forbidden!r}"


def test_timeline_separates_current_state_from_history(client: TestClient) -> None:
    created = client.post("/v1/interactions", json=BODY, headers={"Idempotency-Key": "k1"})
    task_id = created.json()["task_receipt"]["task_id"]
    response = client.get(f"/v1/tasks/{task_id}/timeline")
    assert response.status_code == 200
    document = response.json()
    assert set(document) == {"task_id", "current_state", "history", "record_counts"}
    assert document["current_state"]["status"] == "assigned"


def test_notifications_endpoint_requires_a_subject(client: TestClient) -> None:
    assert client.get("/v1/notifications").status_code == 400


def test_openapi_documents_every_public_route(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()
    paths = set(schema["paths"])
    assert paths >= {
        "/health/live",
        "/health/ready",
        "/v1/interactions",
        "/v1/tasks/{task_id}",
        "/v1/tasks/{task_id}/timeline",
        "/v1/reminders/{reminder_id}",
        "/v1/notifications",
    }


def test_the_specification_example_body_validates_against_openapi(client: TestClient) -> None:
    """The example in §13.2 of the specification must be accepted verbatim."""
    schema = client.get("/openapi.json").json()
    request_schema = schema["components"]["schemas"]["InteractionRequest"]
    assert set(request_schema["required"]) == {"user_id", "conversation_id", "input"}
    assert request_schema["additionalProperties"] is False

    response = client.post(
        "/v1/interactions",
        json={
            "user_id": "user_local",
            "conversation_id": "conv_demo",
            "channel": "api",
            "input": "Remind me to submit the report at 2026-08-10T21:00:00Z",
            "preferred_execution_mode": "delegated",
        },
        headers={"Idempotency-Key": "demo-interaction-001"},
    )
    assert response.status_code == 202
