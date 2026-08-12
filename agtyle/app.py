from __future__ import annotations

import asyncio
import logging
import signal

from pynput import keyboard

from .config import Settings
from .gmail_client import GmailClient
from .realtime import RealtimeExecutive
from .runtime import ApprovalGuard, ExecutiveRuntime, TaskStore


def build_app(settings: Settings) -> tuple[RealtimeExecutive, keyboard.GlobalHotKeys]:
    events: asyncio.Queue[dict] = asyncio.Queue()
    gmail = GmailClient(
        credentials_path=settings.gmail_credentials,
        token_path=settings.gmail_token,
        max_results=settings.max_email_results,
    )
    guard = ApprovalGuard()
    runtime = ExecutiveRuntime(
        gmail=gmail,
        store=TaskStore(settings.db_path),
        event_queue=events,
        approval_guard=guard,
    )
    executive = RealtimeExecutive(
        api_key=settings.openai_api_key,
        model=settings.realtime_model,
        voice=settings.realtime_voice,
        runtime=runtime,
        approval_guard=guard,
    )
    hotkeys = keyboard.GlobalHotKeys({settings.hotkey: executive.toggle_mic_threadsafe})
    return executive, hotkeys


async def async_main() -> None:
    settings = Settings.load()
    executive, hotkeys = build_app(settings)
    hotkeys.start()

    print("Agtyle Voice Email MVP")
    print(f"Hotkey: {settings.hotkey}")
    print("Press the hotkey to toggle listening. Press Ctrl-C to quit.")
    print("The first Gmail operation will open Google OAuth in your browser if token.json does not exist.")

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def request_stop() -> None:
        stop_event.set()
        executive._stopped.set()  # app owns the executive lifecycle

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, request_stop)
        except NotImplementedError:
            pass

    run_task = asyncio.create_task(executive.run())
    try:
        await asyncio.wait(
            {run_task, asyncio.create_task(stop_event.wait())},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if run_task.done():
            exc = run_task.exception()
            if exc:
                raise exc
    finally:
        executive._stopped.set()
        if not run_task.done():
            await run_task
        hotkeys.stop()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
