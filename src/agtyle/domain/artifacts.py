"""Artifact metadata. Storage adapters are out of the reminder baseline."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated

from pydantic import Field, StringConstraints

from agtyle.domain.common import ArtifactId, DomainModel, JsonMapping, TaskId, UtcDatetime


class ArtifactKind(StrEnum):
    DRAFT = "draft"
    REPORT = "report"
    SUMMARY = "summary"
    RAW_SOURCE = "raw_source"


class Artifact(DomainModel):
    """A durable work product produced by a Task."""

    id: ArtifactId
    task_id: TaskId
    kind: ArtifactKind
    media_type: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    storage_ref: Annotated[str, StringConstraints(min_length=1, max_length=500)]
    content_hash: str
    metadata: JsonMapping = Field(default_factory=dict)
    created_at: UtcDatetime
