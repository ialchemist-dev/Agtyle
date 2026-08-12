# Agtyle Voice-First Email MVP

**Status:** MVP design draft  
**Date:** 2026-08-12  
**Scope:** MacBook-first, voice-only interaction, Gmail as the first external capability  
**Primary hypothesis:** A user should be able to complete an email workflow without opening Gmail or touching a traditional application UI.

---

## 1. Purpose

This MVP exists to validate one narrow but foundational idea for Agtyle:

> The user should interact with a persistent Executive Agent rather than directly operating software interfaces.

For the first implementation, the Executive Agent acts as a voice-first secretary. The user summons it with a global hotkey, speaks naturally, and can ask it to inspect Gmail, summarize messages, draft replies, read drafts back, and send only after explicit approval.

The objective is not to build the full Agtyle system. The objective is to prove that the core interaction model feels natural enough to replace direct interaction with Gmail for a small but complete workflow.

The MVP should optimize for:

- low conversational latency;
- continuous availability while background work is running;
- asynchronous task execution;
- event-driven completion reporting;
- explicit human approval before consequential actions;
- minimal UI;
- clean boundaries that can later support additional agents and tools.

---

## 2. Target User Experience

A representative interaction should look like this:

1. The user presses a global hotkey on the MacBook.
2. Agtyle immediately opens a realtime voice session.
3. The user says: “Check my email and tell me what needs my attention.”
4. The Executive Agent immediately acknowledges the request and dispatches a Gmail task without blocking the conversation.
5. The Executive Agent remains available for additional speech while the Gmail task runs.
6. The Gmail worker completes and emits a structured completion event.
7. The Executive Agent receives the event while still maintaining the live conversation.
8. It waits for an appropriate conversational gap rather than interrupting the user mid-sentence.
9. It says that the email check is complete and summarizes the relevant messages.
10. The user asks to hear one message in more detail.
11. The user describes how they want to respond.
12. The system drafts a reply and reads it back.
13. The user edits it by voice if necessary.
14. The user explicitly approves sending.
15. Only then does the system send the email through Gmail.
16. The Executive Agent confirms successful delivery.

At no point should the user need to open Gmail.

---

## 3. Core Architectural Principle

The central design decision is to separate the realtime conversational loop from task execution.

The Executive Agent must remain conversationally responsive even while external work is still in progress.

This means the system is not modeled as:

```text
user request -> tool call -> wait -> result -> model response
```

Instead, it is modeled as two concurrent flows:

```text
Realtime conversation loop
    user speech <-> Executive Agent

Background task loop
    Executive Agent -> task dispatch -> worker/tool -> completion event -> Executive Agent
```

The conversation loop must never be held hostage by a slow API call.

---

## 4. MVP Components

### 4.1 Mac Voice Client

A small native or lightweight Mac application is responsible for the local interaction surface.

Responsibilities:

- register a global hotkey;
- start or foreground the voice session;
- capture microphone audio;
- stream audio to the realtime voice model;
- play streamed audio responses;
- show only minimal status when needed;
- maintain a persistent websocket or equivalent realtime connection during the active session.

The client should not contain domain logic for Gmail.

For the first version, a simple status indicator is enough:

- idle;
- listening;
- speaking;
- background task running;
- approval required;
- error.

The MVP is intentionally voice-first, not UI-first.

### 4.2 Realtime Voice Layer

Use OpenAI’s Realtime API as the low-latency speech interface.

Its role is to provide:

- streaming speech input;
- low-latency speech output;
- interruption handling;
- conversational turn-taking;
- function/tool calling into the Executive runtime.

The realtime model is the user-facing conversational surface. It should not directly perform long-running work synchronously.

Its most important behavior is fast acknowledgement and continued presence.

Example:

```text
User: Check my email.
Executive: Sure. I’m checking it now.
```

The acknowledgement should happen immediately after task acceptance, not after Gmail has completed.

### 4.3 Executive Agent

The Executive Agent is the persistent secretary and the only agent the user directly talks to.

Responsibilities:

- interpret user intent;
- answer ordinary conversational questions when no external task is required;
- create and dispatch background tasks;
- track active task state;
- continue talking while tasks run;
- receive task events;
- decide when to surface completed work;
- manage conversational context;
- request approval for consequential actions;
- translate structured worker output into natural spoken summaries.

The Executive should not be treated as a blocking request handler. It is a long-lived event consumer.

Conceptually, it owns an event loop that receives multiple event types:

```text
UserSpeechEvent
TaskStartedEvent
TaskProgressEvent
TaskCompletedEvent
TaskFailedEvent
ApprovalRequestedEvent
ApprovalGrantedEvent
ApprovalRejectedEvent
```

The Executive may receive user speech and task completion events in either order.

### 4.4 Task Manager

The Task Manager provides durable task state and asynchronous execution semantics.

A minimal task record should contain:

```text
task_id
conversation_id
task_type
status
created_at
started_at
completed_at
input
result
error
requires_approval
approval_state
```

Initial task states:

```text
queued
running
completed
failed
waiting_for_approval
cancelled
```

For the first local MVP, this can be implemented with a lightweight persistent store such as SQLite.

The important property is not sophisticated scheduling. The important property is that the Executive can dispatch a task and immediately return to the conversation.

### 4.5 Event Bus

Background work must report back through events rather than requiring the user or Executive Agent to poll.

For the first version, this does not require Kafka, NATS, or another large infrastructure dependency.

A simple in-process async queue is sufficient if all components live in one process. If workers live in separate processes, a local Redis instance or another lightweight message channel is acceptable.

The interface should nevertheless be designed as an event bus so that the implementation can evolve later.

Example completion event:

```json
{
  "type": "task.completed",
  "task_id": "task_123",
  "source": "gmail",
  "result": {
    "important_messages": 3,
    "summary": "..."
  }
}
```

The Executive subscribes to these events continuously.

### 4.6 Gmail Capability

For the MVP, Gmail is the only external capability.

Use the official Gmail API with OAuth.

Required operations:

- search/list messages;
- fetch message metadata;
- fetch full message content;
- inspect conversation threads;
- create drafts;
- send approved replies.

The Gmail integration should expose structured operations rather than leaking Gmail-specific API details into the Executive Agent.

Example capability interface:

```text
search_messages(query)
get_message(message_id)
get_thread(thread_id)
create_reply_draft(thread_id, body)
send_reply(draft_id)
```

The interface may later be implemented through a direct API adapter, MCP server, or a specialist agent without changing the Executive’s mental model.

---

## 5. Non-Blocking Task Dispatch

This is the most important runtime behavior in the MVP.

When the user requests work, the Executive should:

1. understand the request;
2. create a task record;
3. dispatch the task asynchronously;
4. receive a real task receipt;
5. acknowledge the task to the user;
6. return immediately to listening.

Pseudo-flow:

```python
async def handle_user_request(request):
    task = await task_manager.create(request)
    await task_runner.dispatch(task)

    speak("Got it. I'm checking now.")

    # Do not await task completion here.
    return_to_conversation()
```

The worker executes independently:

```python
async def gmail_worker(task):
    result = await gmail.execute(task.input)
    await event_bus.publish(TaskCompleted(task.id, result))
```

The Executive receives the completion through its event loop:

```python
async for event in event_bus:
    executive_state.apply(event)
    notification_policy.consider(event)
```

This separation preserves natural conversational latency.

---

## 6. Completion Notification Policy

Task completion should not automatically interrupt the user.

Default policy for the MVP:

> Background results become eligible to speak immediately after completion, but are spoken only when the user is not actively speaking and the current conversational turn has reached a natural pause.

This requires a small notification scheduler owned by the Executive.

Possible states:

```text
pending_result
safe_to_interrupt
speaking_result
suppressed
```

The scheduler should prioritize:

1. urgent failures or required approvals;
2. directly requested task results;
3. ordinary completion updates;
4. low-priority informational updates.

For the initial Gmail MVP, a simple rule is enough:

- never interrupt detected user speech;
- after the user finishes a turn, surface the oldest relevant completed task;
- if the conversation has moved to a clearly unrelated topic, briefly announce completion and ask whether the user wants the result now.

---

## 7. Conversation State and Task State

The Executive must maintain both conversational context and explicit task state.

These are different things.

Conversational context answers questions such as:

- What were we talking about?
- What did the user mean by “that email”?
- What tone does the user want?

Task state answers questions such as:

- Is the Gmail search still running?
- Did it fail?
- What result did it return?
- Is a draft waiting for approval?
- Was the reply already sent?

A language-model context window should not be the source of truth for operational state.

The Executive should be able to answer:

```text
User: What happened with that email task?
```

without re-running Gmail if the task already completed.

---

## 8. Human Approval Boundary

Reading data and taking external action are not equivalent.

The MVP should distinguish between read-only and consequential operations.

Read-only operations may execute immediately:

- searching email;
- reading messages;
- summarizing messages;
- generating a proposed reply.

Consequential operations require explicit approval:

- sending an email;
- deleting or archiving mail if added later;
- modifying labels if considered consequential;
- any future action that affects an external system in a meaningful way.

For sending mail, the flow should be:

```text
Draft -> read back to user -> optional edits -> explicit approval -> send
```

The system must not infer approval merely because the user asked for a draft.

Examples of valid approval:

```text
Send it.
Yes, send that.
Looks good, go ahead.
```

The send operation should be idempotency-protected so a repeated event or reconnection cannot accidentally send twice.

---

## 9. Proposed Runtime Shape

A minimal implementation can run entirely on the user’s MacBook.

```text
+-----------------------------------------------------------+
|                     Mac Voice Client                      |
|  Global Hotkey | Mic | Speaker | Minimal Status          |
+-----------------------------+-----------------------------+
                              |
                              v
+-----------------------------------------------------------+
|                   Realtime Voice Session                  |
|            Streaming audio + tool/function calls          |
+-----------------------------+-----------------------------+
                              |
                              v
+-----------------------------------------------------------+
|                     Executive Runtime                     |
| Intent | Conversation State | Task Table | Notification   |
+-----------+-----------------------+-----------------------+
            |                       ^
            | dispatch              | events
            v                       |
+----------------------+    +-------------------------------+
|     Task Runner      |--->|          Event Bus            |
+----------+-----------+    +-------------------------------+
           |
           v
+-----------------------------------------------------------+
|                    Gmail Capability                       |
| Search | Read | Thread | Draft | Send                     |
+-----------------------------------------------------------+
```

For the first implementation, all boxes except OpenAI and Gmail can live inside one local application process.

That keeps the MVP simple while preserving the architectural boundaries.

---

## 10. Suggested Technical Stack

This is intentionally lightweight and replaceable.

### Mac client

Reasonable options:

- Swift/SwiftUI for a native global-hotkey application;
- Electron/Tauri if iteration speed matters more than native integration;
- a small Python prototype if the first goal is simply proving the interaction loop.

### Voice

- OpenAI Realtime API;
- persistent WebRTC or websocket session depending on the chosen client architecture.

### Executive runtime

- Python or TypeScript;
- asyncio / async event loop;
- explicit task state rather than relying purely on model memory.

### Persistence

- SQLite for task state, approvals, and execution history.

### Gmail

- Google OAuth 2.0;
- Gmail REST API.

### Internal messaging

Start with:

- in-process async queues.

Only add Redis, NATS, or another broker if process separation becomes useful.

---

## 11. MVP Scope

### In scope

- global hotkey on Mac;
- realtime voice conversation;
- persistent Executive Agent during the active session;
- asynchronous task dispatch;
- task table;
- event-driven completion callbacks;
- non-interrupting completion notifications;
- Gmail OAuth;
- search/read/summarize email;
- draft replies;
- read drafts aloud;
- voice editing of drafts;
- explicit approval before sending;
- send confirmation.

### Explicitly out of scope

- mobile client;
- dedicated hardware;
- wake-word detection;
- always-on microphone;
- multiple specialist agents;
- GitHub integration;
- calendar integration;
- general-purpose workflow engine;
- distributed deployment;
- autonomous email sending;
- long-term personal memory beyond what is needed for the email session;
- elaborate UI.

These are deliberately postponed so the first prototype tests the interaction model rather than the breadth of the platform.

---

## 12. Key Design Invariants

The MVP should preserve the following invariants from day one.

### 12.1 The Executive is always the user-facing agent

Workers and capability adapters never speak directly to the user.

### 12.2 Long-running work never blocks the realtime conversation

The Executive dispatches and returns to listening.

### 12.3 Background work reports through events

The Executive should not need to poll each worker to discover whether work completed.

### 12.4 Operational state is explicit

Task status lives in a task store, not only inside the model context.

### 12.5 Completion does not imply interruption

Results are surfaced according to conversational timing policy.

### 12.6 Consequential actions require approval

Drafting is not sending.

### 12.7 External capabilities are replaceable adapters

The Executive should not care whether Gmail is reached through a direct API, MCP, or a future specialist agent.

---

## 13. First End-to-End Acceptance Test

The first milestone is successful only if the following scenario works without opening Gmail:

```text
1. User activates Agtyle with a Mac hotkey.
2. User asks: “Do I have any important email today?”
3. Executive acknowledges immediately.
4. Gmail search runs asynchronously.
5. User can continue speaking while Gmail work is running.
6. Gmail completion event reaches the Executive.
7. Executive waits until the user is not speaking.
8. Executive summarizes relevant messages aloud.
9. User chooses one message.
10. Executive reads enough context for a reply decision.
11. User dictates the intended response.
12. Executive creates a draft.
13. Executive reads the draft aloud.
14. User approves it explicitly.
15. Gmail sends exactly once.
16. Executive confirms the send result.
```

If this interaction feels natural, the project has validated the most important Agtyle hypothesis.

---

## 14. What This MVP Is Really Testing

The technical integrations are not the main uncertainty. Realtime speech and Gmail APIs already exist.

The real experiment is whether the following interaction architecture is compelling:

> One persistent secretary remains present, while external work happens asynchronously behind it.

If that works, Gmail is only the first capability.

The same runtime can later dispatch GitHub, calendar, research, file-management, coding, and other specialist tasks without changing the user’s interaction model.

That is the architectural seed of Agtyle.
