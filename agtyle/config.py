from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    openai_api_key: str
    realtime_model: str
    realtime_voice: str
    hotkey: str
    gmail_credentials: Path
    gmail_token: Path
    db_path: Path
    max_email_results: int

    @classmethod
    def load(cls) -> "Settings":
        load_dotenv()
        key = os.getenv("OPENAI_API_KEY", "").strip()
        if not key:
            raise RuntimeError("OPENAI_API_KEY is missing. Copy .env.example to .env and set it.")
        return cls(
            openai_api_key=key,
            realtime_model=os.getenv("OPENAI_REALTIME_MODEL", "gpt-realtime"),
            realtime_voice=os.getenv("OPENAI_REALTIME_VOICE", "marin"),
            hotkey=os.getenv("AGTYLE_HOTKEY", "<cmd>+<shift>+space"),
            gmail_credentials=Path(os.getenv("AGTYLE_GMAIL_CREDENTIALS", "credentials.json")),
            gmail_token=Path(os.getenv("AGTYLE_GMAIL_TOKEN", "token.json")),
            db_path=Path(os.getenv("AGTYLE_DB", "agtyle.db")),
            max_email_results=max(1, min(20, int(os.getenv("AGTYLE_MAX_EMAIL_RESULTS", "8")))),
        )
