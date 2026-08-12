from __future__ import annotations

import asyncio
import base64
import json
import logging
from typing import Any

import sounddevice as sd
import websockets

from .runtime import ApprovalGuard, ExecutiveRuntime

log = logging.getLogger(__name__)

SAMPLE_RATE = 24_000
CHANNELS = 1
DTYPE = "int16"

EXECUTIVE_INSTRUCTIONS = """
You are Agtyle, a persistent voice-first executive secretary. Be concise, natural, and responsive.
Use the user's language. The user may keep talking while background work runs.

CRITICAL RUNTIME RULES:
- You are the only user-facing agent.
- Gmail work is delegated through dispatch_gmail_task. The tool returns a task receipt quickly; the real work continues asynchronously.
- After a task receipt, acknowledge briefly and continue listening. Never pretend the result is already available.
- Background task completion/failure events arrive later as SYSTEM messages beginning with [AGTYLE TASK EVENT]. Treat those events as authoritative task state.
- When a task completes, summarize the useful result naturally. Do not read opaque IDs unless needed for a later tool call.
- Never claim an email was sent until a send_draft task has completed successfully.
- Do not poll when a background task is running unless the user explicitly asks for status.

EMAIL FLOW:
- To inspect mail, use search_email. Gmail search syntax is accepted. For "today" or "recent" you may use newer_than:1d unless the user specifies otherwise.
- To inspect a selected message fully, use read_email with its message_id.
- Draft the reply conversationally with the user. When the wording is ready, use create_reply_draft with the source message_id and exact body. This creates a real Gmail draft but does not send it.
- After draft creation completes, read or summarize the exact draft and ask for explicit approval.
- Only call send_draft after the user has explicitly said to send (for example "send it", "可以发送", "发出去"). The local runtime independently enforces this rule.
- If send is rejected for missing approval, ask for explicit approval; do not work around the guard.

CONVERSATION:
- Do not interrupt the user. Background results are surfaced by the runtime only at a safe conversational gap.
- Keep acknowledgements short: e.g. "Got it, I'm checking." Then remain available for more instructions.
- If a task fails, explain the useful error in one or two sentences and suggest the next concrete action.
""".strip()

TOOL = {
    "type": "function",
    "name": "dispatch_gmail_task",
    "description": "Dispatch a Gmail operation asynchronously. Returns immediately with a task receipt; completion arrives later as a task event.",
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["search_email", "read_email", "create_reply_draft", "send_draft", "task_status"],
            },
            "query": {"type": "string", "description": "Gmail search query for search_email."},
            "max_results": {"type": "integer", "minimum": 1, "maximum": 20},
            "message_id": {"type": "string", "description": "Gmail message ID for read_email/create_reply_draft."},
            "body": {"type": "string", "description": "Exact plain-text reply body for create_reply_draft."},
            "draft_id": {"type": "string", "description": "Gmail draft ID for send_draft."},
            "task_id": {"type": "string", "description": "Task ID for task_status."},
        },
        "required": ["action"],
        "additionalProperties": False,
    },
}


class RealtimeExecutive:
    def __init__(
        self,
        api_key: str,
        model: str,
        voice: str,
        runtime: ExecutiveRuntime,
        approval_guard: ApprovalGuard,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.voice = voice
        self.runtime = runtime
        self.approval_guard = approval_guard
        self.ws = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.mic_enabled = False
        self.user_speaking = False
        self.assistant_speaking = False
        self._audio_in: asyncio.Queue[bytes] = asyncio.Queue(maxsize=100)
        self._pending_events: asyncio.Queue[dict[str, Any]] = runtime.event_queue
        self._input_stream = None
        self._output_stream = None
        self._stopped = asyncio.Event()

    def set_mic_enabled(self, enabled: bool) -> None:
        self.mic_enabled = enabled
        print("\n🎙️  Agtyle listening" if enabled else "\n⏸️  Agtyle muted")

    def toggle_mic_threadsafe(self) -> None:
        if self.loop:
            self.loop.call_soon_threadsafe(self.set_mic_enabled, not self.mic_enabled)

    async def run(self) -> None:
        self.loop = asyncio.get_running_loop()
        url = f"wss://api.openai.com/v1/realtime?model={self.model}"
        headers = {"Authorization": f"Bearer {self.api_key}"}
        async with websockets.connect(url, additional_headers=headers, max_size=None) as ws:
            self.ws = ws
            await self._configure_session()
            self._start_audio()
            tasks = [
                asyncio.create_task(self._send_audio_loop()),
                asyncio.create_task(self._receive_loop()),
                asyncio.create_task(self._notification_loop()),
            ]
            try:
                await self._stopped.wait()
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                self._stop_audio()

    async def _configure_session(self) -> None:
        await self._send(
            {
                "type": "session.update",
                "session": {
                    "type": "realtime",
                    "instructions": EXECUTIVE_INSTRUCTIONS,
                    "output_modalities": ["audio"],
                    "audio": {
                        "input": {
                            "format": {"type": "audio/pcm", "rate": SAMPLE_RATE},
                            "noise_reduction": {"type": "far_field"},
                            "transcription": {"model": "gpt-4o-mini-transcribe"},
                            "turn_detection": {
                                "type": "semantic_vad",
                                "eagerness": "medium",
                                "create_response": True,
                                "interrupt_response": True,
                            },
                        },
                        "output": {
                            "format": {"type": "audio/pcm", "rate": SAMPLE_RATE},
                            "voice": self.voice,
                        },
                    },
                    "tools": [TOOL],
                    "tool_choice": "auto",
                },
            }
        )

    def _start_audio(self) -> None:
        def on_input(indata, frames, time_info, status):  # noqa: ANN001
            if status:
                log.debug("input audio status: %s", status)
            if not self.mic_enabled or not self.loop:
                return
            chunk = bytes(indata)
            self.loop.call_soon_threadsafe(self._queue_audio, chunk)

        self._input_stream = sd.RawInputStream(
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype=DTYPE,
            blocksize=480,
            callback=on_input,
        )
        self._output_stream = sd.RawOutputStream(
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype=DTYPE,
            blocksize=0,
        )
        self._input_stream.start()
        self._output_stream.start()

    def _stop_audio(self) -> None:
        for stream in (self._input_stream, self._output_stream):
            if stream:
                try:
                    stream.stop()
                    stream.close()
                except Exception:  # noqa: BLE001
                    pass

    def _queue_audio(self, chunk: bytes) -> None:
        if self._audio_in.full():
            try:
                self._audio_in.get_nowait()
            except asyncio.QueueEmpty:
                pass
        self._audio_in.put_nowait(chunk)

    async def _send_audio_loop(self) -> None:
        while True:
            chunk = await self._audio_in.get()
            if not self.mic_enabled:
                continue
            await self._send(
                {
                    "type": "input_audio_buffer.append",
                    "audio": base64.b64encode(chunk).decode("ascii"),
                }
            )

    async def _receive_loop(self) -> None:
        assert self.ws is not None
        async for raw in self.ws:
            event = json.loads(raw)
            kind = event.get("type", "")

            if kind == "input_audio_buffer.speech_started":
                self.user_speaking = True
            elif kind == "input_audio_buffer.speech_stopped":
                self.user_speaking = False
            elif kind == "conversation.item.input_audio_transcription.completed":
                transcript = str(event.get("transcript", "")).strip()
                if transcript:
                    self.approval_guard.observe_user_transcript(transcript)
                    log.info("user: %s", transcript)
            elif kind == "response.output_audio.delta":
                self.assistant_speaking = True
                if self._output_stream:
                    audio = base64.b64decode(event.get("delta", ""))
                    await asyncio.to_thread(self._output_stream.write, audio)
            elif kind == "response.output_audio.done":
                self.assistant_speaking = False
            elif kind == "response.output_audio_transcript.done":
                transcript = str(event.get("transcript", "")).strip()
                if transcript:
                    log.info("assistant: %s", transcript)
            elif kind == "response.function_call_arguments.done":
                asyncio.create_task(self._handle_tool_call(event))
            elif kind == "error":
                log.error("Realtime error: %s", event.get("error"))

    async def _handle_tool_call(self, event: dict[str, Any]) -> None:
        call_id = event.get("call_id")
        try:
            args = json.loads(event.get("arguments") or "{}")
            action = str(args.pop("action"))
            receipt = await self.runtime.dispatch(action, args)
        except Exception as exc:  # noqa: BLE001
            receipt = {"accepted": False, "error": f"{type(exc).__name__}: {exc}"}
        await self._send(
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": json.dumps(receipt, ensure_ascii=False),
                },
            }
        )
        await self._send({"type": "response.create"})

    async def _notification_loop(self) -> None:
        while True:
            event = await self._pending_events.get()
            if event.get("type") == "task.started":
                continue
            while self.user_speaking or self.assistant_speaking or not self.mic_enabled:
                await asyncio.sleep(0.15)
            await asyncio.sleep(0.55)
            if self.user_speaking or self.assistant_speaking or not self.mic_enabled:
                await self._pending_events.put(event)
                await asyncio.sleep(0.2)
                continue
            await self._inject_task_event(event)

    async def _inject_task_event(self, event: dict[str, Any]) -> None:
        text = "[AGTYLE TASK EVENT]\n" + json.dumps(event, ensure_ascii=False)
        await self._send(
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "system",
                    "content": [{"type": "input_text", "text": text}],
                },
            }
        )
        await self._send(
            {
                "type": "response.create",
                "response": {
                    "instructions": "A background task event just arrived. Briefly report the useful result now. Do not mention internal event JSON or opaque IDs unless needed.",
                },
            }
        )

    async def _send(self, event: dict[str, Any]) -> None:
        if not self.ws:
            raise RuntimeError("Realtime websocket is not connected")
        await self.ws.send(json.dumps(event, ensure_ascii=False))
