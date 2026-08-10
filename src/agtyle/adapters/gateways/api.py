"""FastAPI Interaction Gateway.

Route handlers translate between HTTP and application services. They never touch SQLAlchemy
models, never call a Capability Adapter, and never invent a status code that the application
layer did not justify. Errors are RFC 9457-style problem details carrying only the stable code,
a safe title, a safe detail and the Task id when one exists.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, Header, Path, Query, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from agtyle.application.interaction_service import (
    HandleInteraction,
    InteractionOutcome,
    InteractionResult,
)
from agtyle.bootstrap import Container, build_container
from agtyle.config import Settings
from agtyle.domain.common import (
    AgtyleError,
    ErrorCode,
    InteractionIdempotencyConflictError,
)
from agtyle.domain.tasks import ExecutionMode
from agtyle.observability.logging import get_logger

logger = get_logger(__name__)

PROBLEM_CONTENT_TYPE = "application/problem+json"

STATUS_BY_CODE: dict[ErrorCode, int] = {
    ErrorCode.INVALID_REQUEST: status.HTTP_400_BAD_REQUEST,
    ErrorCode.CLARIFICATION_REQUIRED: status.HTTP_400_BAD_REQUEST,
    ErrorCode.INTERACTION_IDEMPOTENCY_CONFLICT: status.HTTP_409_CONFLICT,
    ErrorCode.ACTION_IDEMPOTENCY_CONFLICT: status.HTTP_409_CONFLICT,
    ErrorCode.ILLEGAL_TRANSITION: status.HTTP_409_CONFLICT,
    ErrorCode.POLICY_DENIED: status.HTTP_403_FORBIDDEN,
    ErrorCode.APPROVAL_REQUIRED: status.HTTP_403_FORBIDDEN,
    ErrorCode.UNSUPPORTED_ASSIGNMENT: status.HTTP_422_UNPROCESSABLE_CONTENT,
    ErrorCode.INVALID_AGENT_OUTPUT: status.HTTP_422_UNPROCESSABLE_CONTENT,
    ErrorCode.ACTION_SCHEMA_INVALID: status.HTTP_422_UNPROCESSABLE_CONTENT,
    ErrorCode.POLICY_ENGINE_ERROR: status.HTTP_503_SERVICE_UNAVAILABLE,
    ErrorCode.CONFIGURATION_INVALID: status.HTTP_503_SERVICE_UNAVAILABLE,
    ErrorCode.CAPABILITY_TRANSIENT_FAILURE: status.HTTP_503_SERVICE_UNAVAILABLE,
}


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class InteractionRequest(StrictModel):
    user_id: Annotated[str, Field(min_length=1, max_length=200)]
    conversation_id: Annotated[str, Field(min_length=1, max_length=200)]
    channel: Annotated[str, Field(min_length=1, max_length=50)] = "api"
    input: Annotated[str, Field(min_length=1, max_length=20_000)]
    preferred_execution_mode: ExecutionMode = ExecutionMode.DELEGATED


class TaskReceiptBody(StrictModel):
    task_id: str
    status: str
    assigned_agent_id: str
    execution_mode: str
    accepted_at: str


class InteractionResponse(StrictModel):
    intent_id: str
    outcome: str
    message: str
    task_receipt: TaskReceiptBody | None = None
    missing: list[str] = Field(default_factory=list)
    result: dict[str, Any] | None = None


class HealthResponse(StrictModel):
    status: Literal["ok", "degraded"]
    checks: dict[str, Any] = Field(default_factory=dict)


def problem(error: AgtyleError, *, status_code: int | None = None) -> JSONResponse:
    """RFC 9457-style problem details containing nothing internal."""
    public = error.to_public_dict()
    body = {
        "type": f"https://agtyle.local/problems/{public['code'].lower()}",
        "title": public["title"],
        "status": status_code or STATUS_BY_CODE.get(error.code, status.HTTP_400_BAD_REQUEST),
        "detail": public["detail"],
        "code": public["code"],
    }
    if "task_id" in public:
        body["task_id"] = public["task_id"]
    return JSONResponse(
        status_code=int(body["status"]), content=body, media_type=PROBLEM_CONTENT_TYPE
    )


def get_container(request: Request) -> Container:
    container: Container = request.app.state.container
    return container


ContainerDep = Annotated[Container, Depends(get_container)]


def create_app(settings: Settings | None = None, *, container: Container | None = None) -> FastAPI:
    """Build the ASGI app. A prebuilt container lets tests share one database and clock."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        owned = container is None
        app.state.container = container or build_container(settings)
        try:
            yield
        finally:
            if owned:
                app.state.container.dispose()

    app = FastAPI(
        title="Agtyle",
        version="1.0.0",
        summary="An auditable execution spine for delegated agent work.",
        lifespan=lifespan,
    )

    @app.exception_handler(AgtyleError)
    async def _agtyle_error_handler(_: Request, error: AgtyleError) -> Response:
        return problem(error)

    # ---------------------------------------------------------------------------------
    # Health
    # ---------------------------------------------------------------------------------

    @app.get("/health/live", response_model=HealthResponse)
    async def live() -> HealthResponse:
        return HealthResponse(status="ok", checks={"process": "running"})

    @app.get("/health/ready")
    async def ready(container: ContainerDep, response: Response) -> HealthResponse:
        """Ready means the system could execute an Action right now, not that it is busy.

        A Worker that is idle, or not running at all, does not make the system unready.
        """
        checks: dict[str, Any] = {}
        from agtyle.adapters.persistence import migrator

        try:
            checks["database_migrated"] = migrator.is_up_to_date(
                container.settings, container.engine
            )
        except Exception as exc:
            checks["database_migrated"] = False
            checks["database_error"] = type(exc).__name__

        checks["registry_agents"] = [agent.id for agent in container.registry.agents]
        checks["capabilities"] = [item.name for item in container.registry.capabilities]

        consistency = await container.registry_sync.check()
        checks["registry_snapshot_synced"] = consistency.synced
        checks["registry_snapshot_consistent"] = consistency.consistent
        if not consistency.consistent:
            checks["registry_disagreements"] = [
                item.model_dump(mode="json") for item in consistency.disagreements
            ]

        report = await container.authorization.validate_policy_set()
        checks["cedar_binary_present"] = container.authorization.configuration_problem() is None
        checks["cedar_version"] = report.engine_version
        checks["cedar_policies_valid"] = report.valid
        if not report.valid:
            checks["cedar_errors"] = report.errors

        healthy = bool(
            checks["database_migrated"]
            and checks["cedar_binary_present"]
            and checks["cedar_policies_valid"]
            and checks["registry_agents"]
            and checks["registry_snapshot_consistent"]
        )
        response.status_code = (
            status.HTTP_200_OK if healthy else status.HTTP_503_SERVICE_UNAVAILABLE
        )
        return HealthResponse(status="ok" if healthy else "degraded", checks=checks)

    # ---------------------------------------------------------------------------------
    # Interactions
    # ---------------------------------------------------------------------------------

    @app.post("/v1/interactions", status_code=status.HTTP_202_ACCEPTED)
    async def create_interaction(
        body: InteractionRequest,
        container: ContainerDep,
        response: Response,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1)],
    ) -> Any:
        try:
            result = await container.interaction_service.handle(
                HandleInteraction(
                    user_id=body.user_id,
                    conversation_id=body.conversation_id,
                    channel=body.channel,
                    input=body.input,
                    idempotency_key=idempotency_key,
                    preferred_execution_mode=body.preferred_execution_mode,
                )
            )
        except InteractionIdempotencyConflictError as conflict:
            return problem(conflict)
        except AgtyleError as error:
            return problem(error)

        response.status_code = _status_for(result)
        return _interaction_body(result)

    # ---------------------------------------------------------------------------------
    # Inspection
    # ---------------------------------------------------------------------------------

    @app.get("/v1/tasks/{task_id}")
    async def get_task(container: ContainerDep, task_id: Annotated[str, Path(min_length=1)]) -> Any:
        async with container.uow_factory() as uow:
            task = await uow.tasks.get(task_id)
            runs = await uow.agent_runs.list_for_task(task_id) if task else []
        if task is None:
            return _not_found("task", task_id)
        return {
            "task_id": task.id,
            "intent_id": task.intent_id,
            "task_type": task.task_type,
            "status": task.status.value,
            "execution_mode": task.execution_mode.value,
            "assigned_agent_id": task.assigned_agent_id,
            "objective": task.objective,
            "attempt_count": task.attempt_count,
            "max_attempts": task.max_attempts,
            "last_error_code": task.last_error_code.value if task.last_error_code else None,
            "created_at": task.created_at.isoformat(),
            "updated_at": task.updated_at.isoformat(),
            "agent_runs": [
                {"id": run.id, "attempt": run.attempt, "status": run.status.value} for run in runs
            ],
        }

    @app.get("/v1/tasks/{task_id}/timeline")
    async def get_timeline(
        container: ContainerDep, task_id: Annotated[str, Path(min_length=1)]
    ) -> Any:
        timeline = await container.timeline_service.for_task(task_id)
        if timeline is None:
            return _not_found("task", task_id)
        return timeline.model_dump(mode="json")

    @app.get("/v1/reminders/{reminder_id}")
    async def get_reminder(
        container: ContainerDep, reminder_id: Annotated[str, Path(min_length=1)]
    ) -> Any:
        async with container.uow_factory() as uow:
            reminder = await uow.reminders.get(reminder_id)
            notifications = (
                await uow.notifications.list_for_reminder(reminder_id) if reminder else []
            )
        if reminder is None:
            return _not_found("reminder", reminder_id)
        return {
            "reminder_id": reminder.id,
            "user_id": reminder.user_id,
            "source_task_id": reminder.source_task_id,
            "title": reminder.title,
            "note": reminder.note,
            "scheduled_for_utc": reminder.scheduled_for_utc.isoformat(),
            "local_time": reminder.local_time().isoformat(),
            "timezone": reminder.timezone,
            "status": reminder.status.value,
            "delivered_at": reminder.delivered_at.isoformat() if reminder.delivered_at else None,
            "notifications": [
                {
                    "id": item.id,
                    "delivery_key": item.delivery_key,
                    "delivery_status": item.delivery_status.value,
                }
                for item in notifications
            ],
        }

    @app.get("/v1/notifications")
    async def list_notifications(
        container: ContainerDep,
        task_id: Annotated[str | None, Query()] = None,
        reminder_id: Annotated[str | None, Query()] = None,
        delivery_status: Annotated[str | None, Query(alias="status")] = None,
    ) -> Any:
        async with container.uow_factory() as uow:
            if task_id:
                found = await uow.notifications.list_for_task(task_id)
            elif reminder_id:
                found = await uow.notifications.list_for_reminder(reminder_id)
            else:
                return problem(
                    AgtyleError("provide task_id or reminder_id", code=ErrorCode.INVALID_REQUEST)
                )
        items = [
            {
                "id": item.id,
                "kind": item.kind.value,
                "delivery_key": item.delivery_key,
                "delivery_status": item.delivery_status.value,
                "attempt_count": item.attempt_count,
                "delivered_at": item.delivered_at.isoformat() if item.delivered_at else None,
                "task_id": item.task_id,
                "reminder_id": item.reminder_id,
            }
            for item in found
            if delivery_status is None or item.delivery_status.value == delivery_status
        ]
        return {"notifications": items, "count": len(items)}

    return app


def _status_for(result: InteractionResult) -> int:
    match result.outcome:
        case InteractionOutcome.TASK_ASSIGNED:
            return status.HTTP_202_ACCEPTED
        case InteractionOutcome.COMPLETED:
            return status.HTTP_200_OK
        case InteractionOutcome.DIRECT_RESPONSE | InteractionOutcome.CLARIFICATION_REQUIRED:
            return status.HTTP_200_OK
    raise AssertionError(f"unhandled outcome: {result.outcome}")  # pragma: no cover


def _interaction_body(result: InteractionResult) -> dict[str, Any]:
    body: dict[str, Any] = {
        "intent_id": result.intent_id,
        "outcome": result.outcome.value,
        "message": result.message,
    }
    if result.task_receipt is not None:
        body["task_receipt"] = {
            "task_id": result.task_receipt.task_id,
            "status": result.task_receipt.status.value,
            "assigned_agent_id": result.task_receipt.assigned_agent_id,
            "execution_mode": result.task_receipt.execution_mode.value,
            "accepted_at": result.task_receipt.accepted_at.isoformat(),
        }
    if result.missing:
        body["missing"] = result.missing
    if result.result is not None:
        body["result"] = result.result
    return body


def _not_found(kind: str, identifier: str) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_404_NOT_FOUND,
        media_type=PROBLEM_CONTENT_TYPE,
        content={
            "type": "https://agtyle.local/problems/not-found",
            "title": f"{kind.capitalize()} not found",
            "status": 404,
            "detail": f"no {kind} with id {identifier}",
            "code": "AGT-INPUT-001",
        },
    )
