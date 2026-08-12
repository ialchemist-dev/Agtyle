from __future__ import annotations

import os
import sys
from pathlib import Path

import sounddevice as sd

from .config import Settings


def main() -> int:
    failures = 0
    print("Agtyle preflight\n")

    try:
        settings = Settings.load()
        print("✓ OPENAI_API_KEY is set")
        print(f"✓ Realtime model: {settings.realtime_model}")
        print(f"✓ Hotkey: {settings.hotkey}")
    except Exception as exc:  # noqa: BLE001
        print(f"✗ configuration: {exc}")
        return 1

    if settings.gmail_credentials.exists():
        print(f"✓ Gmail OAuth credentials: {settings.gmail_credentials}")
    else:
        print(f"✗ Gmail OAuth credentials missing: {settings.gmail_credentials}")
        failures += 1

    if settings.gmail_token.exists():
        print(f"✓ Gmail token exists: {settings.gmail_token}")
    else:
        print("• Gmail token not created yet; first Gmail request will open browser OAuth")

    try:
        default_input, default_output = sd.default.device
        devices = sd.query_devices()
        input_name = devices[default_input]["name"] if default_input is not None and default_input >= 0 else "none"
        output_name = devices[default_output]["name"] if default_output is not None and default_output >= 0 else "none"
        print(f"✓ Default microphone: {input_name}")
        print(f"✓ Default speaker: {output_name}")
    except Exception as exc:  # noqa: BLE001
        print(f"✗ audio device check failed: {exc}")
        failures += 1

    db_parent = settings.db_path.parent if settings.db_path.parent != Path("") else Path(".")
    if os.access(db_parent, os.W_OK):
        print(f"✓ Runtime state writable: {settings.db_path}")
    else:
        print(f"✗ Runtime state path not writable: {settings.db_path}")
        failures += 1

    if failures:
        print(f"\nPreflight found {failures} blocking issue(s).")
        return 1
    print("\nPreflight passed. Run: agtyle")
    return 0


if __name__ == "__main__":
    sys.exit(main())
