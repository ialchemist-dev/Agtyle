# Agtyle Voice Email MVP

A minimal Mac-first implementation of the voice-first Executive described in `VOICE_EMAIL_MVP.md`.

The prototype is intentionally narrow: press a global hotkey, talk to one persistent realtime Executive, ask it to inspect Gmail, discuss a reply, create a real Gmail draft, hear the result, and explicitly approve before sending. Gmail work runs asynchronously so the voice conversation remains available while external work is in progress.

## What is implemented

- Global macOS hotkey (`⌘⇧Space` by default) toggles microphone streaming.
- OpenAI Realtime speech-to-speech session using 24 kHz PCM audio.
- Semantic VAD and interruption handling.
- Persistent user-facing Executive Agent.
- Realtime function calling into a local asynchronous task runtime.
- SQLite task state.
- Event-driven task completion; no polling required.
- Completion results wait for a conversational gap before being spoken.
- Gmail desktop OAuth.
- Gmail search, message read, reply draft creation, and draft send.
- Reply threading headers and Gmail `threadId` support.
- Local explicit-approval guard before `send_draft` is accepted.
- Basic tests for async task completion and approval enforcement.
- Local preflight command for configuration and audio devices.

## Runtime shape

```text
Mac hotkey + microphone
        │
        ▼
OpenAI Realtime session  <──────────────┐
        │                               │
        │ function call                 │ task completion event
        ▼                               │
ExecutiveRuntime ──> async Task ──> Gmail API
        │
        └── SQLite task state
```

The important detail is that a Realtime tool call only dispatches work and immediately returns a task receipt. It does **not** wait for Gmail. When Gmail finishes, the task runtime emits an event. The Realtime layer waits until the user and assistant are both quiet, injects that event into the live conversation, and asks the Executive to report it.

## Requirements

- macOS
- Python 3.11+
- microphone and speaker
- an OpenAI API key with Realtime API access
- a Google Cloud project with Gmail API enabled
- a Gmail account you can authorize for the desktop app

## 1. Get the branch

```bash
git clone https://github.com/ialchemist-dev/Agtyle.git
cd Agtyle
git switch agent/voice-email-mvp-design
```

This branch is intentionally the clean MVP line rather than the existing `main` implementation.

## 2. Create a Python environment

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -e ".[dev]"
```

If `sounddevice` cannot find an audio backend on your machine, install PortAudio and retry:

```bash
brew install portaudio
pip install -e ".[dev]"
```

## 3. Configure OpenAI

```bash
cp .env.example .env
```

Edit `.env` and set:

```text
OPENAI_API_KEY=sk-...
```

Defaults:

```text
OPENAI_REALTIME_MODEL=gpt-realtime
OPENAI_REALTIME_VOICE=marin
AGTYLE_HOTKEY=<cmd>+<shift>+space
```

The API key stays in `.env`, which is gitignored.

## 4. Configure Gmail OAuth

In Google Cloud:

1. Create or select a project.
2. Enable the **Gmail API**.
3. Configure the OAuth consent screen.
4. Create an OAuth client with application type **Desktop app**.
5. Download the client JSON.
6. Save it in this repository as `credentials.json`.

For an External OAuth app that is still in testing, add your Gmail account as a test user.

The MVP requests these scopes:

```text
https://www.googleapis.com/auth/gmail.readonly
https://www.googleapis.com/auth/gmail.compose
```

The first Gmail operation opens the browser for Google OAuth. After authorization, refresh credentials are stored locally in `token.json`. Both `credentials.json` and `token.json` are gitignored.

This first OAuth bootstrap is the one intentional screen-based setup step in the MVP. Normal email use after that is voice-first.

## 5. Run preflight

```bash
python -m agtyle.preflight
```

Expected output includes:

```text
✓ OPENAI_API_KEY is set
✓ Gmail OAuth credentials: credentials.json
✓ Default microphone: ...
✓ Default speaker: ...
Preflight passed. Run: agtyle
```

A missing `token.json` is not an error; it will be created by the first Gmail OAuth flow.

## 6. Run tests

```bash
pytest
```

The tests do not call OpenAI or Gmail. They validate the local task/event path and the send approval guard.

## 7. Start Agtyle

```bash
agtyle
```

On first launch, macOS may ask for microphone and/or Accessibility/Input Monitoring permission. Grant those permissions to the terminal application you are using (Terminal, iTerm, etc.) if the global hotkey or microphone is blocked.

The terminal will show:

```text
Agtyle Voice Email MVP
Hotkey: <cmd>+<shift>+space
Press the hotkey to toggle listening. Press Ctrl-C to quit.
```

Press `⌘⇧Space`. You should see:

```text
🎙️  Agtyle listening
```

Press it again to mute:

```text
⏸️  Agtyle muted
```

## First test script

After enabling listening, try this sequence naturally rather than reading it mechanically:

```text
You: Check my email from the last day and tell me what needs my attention.

Agtyle: Got it, I'm checking.

# While Gmail is still working, deliberately issue another conversational instruction.
You: And keep it concise when you report back.

# When the background task completes and you are quiet, Agtyle reports the result.
You: Read the email from Alice.

You: I want to reply that Tuesday works, and ask whether 2 PM is okay.

Agtyle: <proposes / creates the reply draft and reads it back>

You: Send it.

Agtyle: <dispatches send, then only confirms after Gmail reports success>
```

The key MVP acceptance test is not just whether Gmail works. It is whether you can continue talking while Gmail work is running and whether the completed task returns into the live conversation without asking the Executive to poll.

## Safety behavior

### Sending requires explicit spoken approval

`send_draft` has two gates:

1. The Executive prompt says it must not send until the user explicitly approves.
2. The local runtime independently checks the most recent user audio transcript for an explicit approval phrase within a short time window.

Examples accepted by the current MVP include:

```text
send it
send that
go ahead
looks good, send it
可以发送
发出去
确认发送
就这样发
```

A request such as “draft a reply” does not authorize sending.

This is intentionally conservative. The guard is an MVP safety boundary, not a complete production authorization system.

### Task state is explicit

The language model's conversation memory is not the operational source of truth. Tasks are persisted in `agtyle.db` with states such as `queued`, `running`, `completed`, and `failed`.

## Files

```text
VOICE_EMAIL_MVP.md        Design document
README.md                 Setup and test instructions
pyproject.toml            Python package and dependencies
.env.example              Runtime configuration template
agtyle/config.py          Environment configuration
agtyle/gmail_client.py    Gmail OAuth/search/read/draft/send adapter
agtyle/runtime.py         Task store, async execution, approval guard
agtyle/realtime.py        Realtime websocket, audio, tool calls, event injection
agtyle/app.py             macOS global-hotkey process
agtyle/preflight.py       Local environment/audio check
tests/test_runtime.py     Core task and approval tests
```

## Current MVP limitations

- The process must remain running in a terminal; it is not yet packaged as a `.app` or LaunchAgent.
- The hotkey toggles microphone streaming but keeps the Realtime connection alive during the test session.
- Gmail OAuth bootstrap requires a browser once.
- Replies are plain text; attachments and rich formatting are not implemented.
- Search/read/draft/send are the only Gmail operations.
- There is one Executive and one Gmail capability rather than a general multi-agent registry.
- Task notification policy is intentionally simple: wait for no detected user speech, no assistant output, and a short extra quiet gap.
- The local transcript approval guard uses phrase matching; production authorization should use a stronger explicit approval state machine.
- There is no mobile app, wake word, dedicated hardware, calendar, GitHub, or general workflow engine in this branch.

## Troubleshooting

### Hotkey does nothing

Give the terminal app Accessibility/Input Monitoring permission in macOS System Settings, then restart the terminal process.

You can also change the binding in `.env`, for example:

```text
AGTYLE_HOTKEY=<ctrl>+<alt>+space
```

### No microphone audio

Check:

```bash
python -m agtyle.preflight
```

Then verify macOS microphone permission for the terminal app.

### Realtime connection fails

Confirm `OPENAI_API_KEY` is valid and that the account/project has Realtime API access. The terminal logs server-side Realtime errors.

### Gmail says `credentials.json` is missing

Download a Google OAuth **Desktop app** client JSON and save it at the repository root as `credentials.json`, or change `AGTYLE_GMAIL_CREDENTIALS` in `.env`.

### Gmail OAuth scope changed

Delete `token.json` and authorize again. Google tokens are scoped at authorization time.

### Gmail reply appears outside the original thread

The adapter supplies Gmail `threadId`, matching subject, `In-Reply-To`, and `References`. If the original message lacks a usable Internet `Message-ID`, Gmail may still make its own threading decision.

## Definition of done for this MVP

The prototype passes its core test when all of the following are true on the Mac:

1. A global hotkey starts voice input without opening Gmail.
2. Speech feels realtime enough for normal conversation.
3. A Gmail request is acknowledged immediately and dispatched asynchronously.
4. The Executive can continue receiving speech while Gmail runs.
5. A completed Gmail task automatically returns into the live conversation at a quiet gap.
6. The user can select/read an email by voice.
7. The user can describe a reply by voice and create a real Gmail draft.
8. The draft is presented for review.
9. Sending is rejected without explicit approval.
10. After explicit approval, the Gmail draft sends and only then is success announced.

That is the entire product boundary for this branch.
