"""Intent: the preserved record of what the user actually said."""

from __future__ import annotations

from typing import Annotated

from pydantic import Field, StringConstraints, field_validator

from agtyle.domain.common import (
    DomainModel,
    IntentId,
    UserId,
    UtcDatetime,
    hash_document,
)

MAX_INPUT_LENGTH = 20_000


class Intent(DomainModel):
    """The original interaction, preserved byte-for-byte after request decoding.

    Interpretation may be added later but never replaces ``original_input``.
    """

    id: IntentId
    user_id: UserId
    origin_channel: Annotated[str, StringConstraints(min_length=1, max_length=50)]
    origin_conversation_id: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    interaction_idempotency_key: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    request_hash: str
    original_input: Annotated[str, Field(min_length=1, max_length=MAX_INPUT_LENGTH)]
    interpreted_outcome: str | None = None
    created_at: UtcDatetime

    @field_validator("original_input")
    @classmethod
    def _reject_control_characters(cls, value: str) -> str:
        # Preserve the input verbatim; reject only NUL, which cannot round-trip through SQLite TEXT.
        if "\x00" in value:
            raise ValueError("original_input must not contain NUL characters")
        return value


def interaction_request_hash(
    *,
    user_id: str,
    conversation_id: str,
    channel: str,
    original_input: str,
    preferred_execution_mode: str,
) -> str:
    """Hash the request body so a reused idempotency key with a different body is detectable."""
    return hash_document(
        {
            "channel": channel,
            "conversation_id": conversation_id,
            "input": original_input,
            "preferred_execution_mode": preferred_execution_mode,
            "user_id": user_id,
        }
    )
