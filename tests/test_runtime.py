from __future__ import annotations

import asyncio
from pathlib import Path

from agtyle.runtime import ApprovalGuard, ExecutiveRuntime, TaskStore


class FakeGmail:
    max_results = 8

    def search(self, query, max_results):
        return {"query": query, "count": 1, "messages": [{"message_id": "m1", "subject": "Hello"}]}

    def read(self, message_id):
        return {"message_id": message_id, "body": "body"}

    def create_reply_draft(self, message_id, body):
        return {"draft_id": "d1", "message_id": message_id, "body": body}

    def send_draft(self, draft_id):
        return {"sent": True, "message_id": "sent1", "draft_id": draft_id}


def test_approval_guard_accepts_explicit_english_and_chinese():
    guard = ApprovalGuard(max_age_seconds=60)
    guard.observe_user_transcript("Looks good, send it")
    assert guard.has_recent_explicit_approval()
    guard.observe_user_transcript("可以发送")
    assert guard.has_recent_explicit_approval()


def test_approval_guard_rejects_non_approval():
    guard = ApprovalGuard(max_age_seconds=60)
    guard.observe_user_transcript("Please draft a reply")
    assert not guard.has_recent_explicit_approval()


def test_send_is_rejected_without_explicit_approval(tmp_path: Path):
    async def scenario():
        guard = ApprovalGuard(max_age_seconds=60)
        runtime = ExecutiveRuntime(FakeGmail(), TaskStore(tmp_path / "db.sqlite"), asyncio.Queue(), guard)
        receipt = await runtime.dispatch("send_draft", {"draft_id": "d1"})
        assert receipt["accepted"] is False

    asyncio.run(scenario())


def test_background_search_completes_and_emits_event(tmp_path: Path):
    async def scenario():
        guard = ApprovalGuard(max_age_seconds=60)
        events = asyncio.Queue()
        store = TaskStore(tmp_path / "db.sqlite")
        runtime = ExecutiveRuntime(FakeGmail(), store, events, guard)
        receipt = await runtime.dispatch("search_email", {"query": "newer_than:1d"})
        assert receipt["accepted"] is True
        started = await asyncio.wait_for(events.get(), timeout=1)
        completed = await asyncio.wait_for(events.get(), timeout=1)
        assert started["type"] == "task.started"
        assert completed["type"] == "task.completed"
        task = store.get(receipt["task_id"])
        assert task is not None
        assert task.status == "completed"
        assert task.result["count"] == 1

    asyncio.run(scenario())
