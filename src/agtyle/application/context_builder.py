"""ContextPack construction.

A ContextPack is the only context an Agent receives. It is deliberately small: the Task, a
narrow set of user preferences, an explicit authority budget, and trust labels. It never
contains credentials, the whole conversation history, or the knowledge base.
"""

from __future__ import annotations

from agtyle.domain.agents import ContextPack, ContextTask, TrustLabel
from agtyle.domain.common import JsonMapping, hash_document
from agtyle.domain.tasks import Task

#: Preference keys an Agent is allowed to see, by declared context scope.
SCOPE_PREFERENCES: dict[str, tuple[str, ...]] = {
    "timezone": ("timezone",),
    "reminder_preferences": ("reminder_lead_minutes",),
    "conversation_origin": ("channel", "conversation_id"),
}

#: Keys that must never reach an Agent even if a caller supplies them.
FORBIDDEN_PREFERENCE_KEYS: frozenset[str] = frozenset(
    {"api_key", "token", "secret", "password", "credential", "authorization"}
)


def build_context_pack(
    *,
    task: Task,
    context_scopes: list[str],
    authority_budget: list[str],
    available_preferences: JsonMapping,
    references: list[str] | None = None,
    trust_labels: dict[str, TrustLabel] | None = None,
) -> ContextPack:
    """Assemble a ContextPack containing only what the Agent's manifest entitles it to see."""
    allowed_keys: set[str] = set()
    for scope in context_scopes:
        allowed_keys.update(SCOPE_PREFERENCES.get(scope, ()))

    preferences = {
        key: value
        for key, value in available_preferences.items()
        if key in allowed_keys and key.lower() not in FORBIDDEN_PREFERENCE_KEYS
    }

    return ContextPack(
        task=ContextTask(
            id=task.id,
            task_type=task.task_type,
            objective=task.objective,
            payload=task.payload,
        ),
        references=references or [],
        user_preferences=preferences,
        authority_budget=list(authority_budget),
        trust_labels=trust_labels or {},
    )


def context_pack_hash(pack: ContextPack) -> str:
    """Hash the exact context an AgentRun saw, so the run can be explained after the fact."""
    return hash_document(pack.model_dump(mode="json"))
