"""Knowledge port seam. No production adapter is part of the reminder baseline."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from agtyle.domain.agents import TrustLabel
from agtyle.domain.common import DomainModel


class KnowledgeQuery(DomainModel):
    text: str
    limit: int = 5


class KnowledgeFragment(DomainModel):
    reference: str
    content: str
    trust_label: TrustLabel = TrustLabel.UNTRUSTED_EXTERNAL


@runtime_checkable
class KnowledgePort(Protocol):
    async def retrieve(self, query: KnowledgeQuery) -> list[KnowledgeFragment]:
        """Return supporting fragments. Retrieved content is data, never instruction."""
        ...
