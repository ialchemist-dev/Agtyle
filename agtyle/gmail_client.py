from __future__ import annotations

import base64
import html
import re
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
]


class GmailClient:
    def __init__(self, credentials_path: Path, token_path: Path, max_results: int = 8) -> None:
        self.credentials_path = credentials_path
        self.token_path = token_path
        self.max_results = max_results
        self._service = None

    def _credentials(self) -> Credentials:
        creds: Credentials | None = None
        if self.token_path.exists():
            creds = Credentials.from_authorized_user_file(str(self.token_path), SCOPES)
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                if not self.credentials_path.exists():
                    raise RuntimeError(
                        f"Missing {self.credentials_path}. Create a Google OAuth Desktop client, "
                        "download it as credentials.json, then retry."
                    )
                flow = InstalledAppFlow.from_client_secrets_file(str(self.credentials_path), SCOPES)
                creds = flow.run_local_server(port=0)
            self.token_path.write_text(creds.to_json())
        return creds

    def service(self):
        if self._service is None:
            self._service = build("gmail", "v1", credentials=self._credentials(), cache_discovery=False)
        return self._service

    def search(self, query: str, max_results: int | None = None) -> dict[str, Any]:
        limit = min(max_results or self.max_results, 20)
        response = (
            self.service().users().messages().list(userId="me", q=query, maxResults=limit).execute()
        )
        messages = []
        for item in response.get("messages", []):
            msg = (
                self.service()
                .users()
                .messages()
                .get(
                    userId="me",
                    id=item["id"],
                    format="metadata",
                    metadataHeaders=["From", "To", "Subject", "Date", "Message-ID"],
                )
                .execute()
            )
            headers = self._headers(msg)
            messages.append(
                {
                    "message_id": msg["id"],
                    "thread_id": msg.get("threadId"),
                    "from": headers.get("from", ""),
                    "to": headers.get("to", ""),
                    "subject": headers.get("subject", "(no subject)"),
                    "date": headers.get("date", ""),
                    "snippet": msg.get("snippet", ""),
                }
            )
        return {"query": query, "count": len(messages), "messages": messages}

    def read(self, message_id: str) -> dict[str, Any]:
        msg = (
            self.service().users().messages().get(userId="me", id=message_id, format="full").execute()
        )
        headers = self._headers(msg)
        return {
            "message_id": msg["id"],
            "thread_id": msg.get("threadId"),
            "from": headers.get("from", ""),
            "to": headers.get("to", ""),
            "subject": headers.get("subject", "(no subject)"),
            "date": headers.get("date", ""),
            "internet_message_id": headers.get("message-id", ""),
            "body": self._extract_body(msg.get("payload", {})),
        }

    def create_reply_draft(self, message_id: str, body: str) -> dict[str, Any]:
        original = self.read(message_id)
        profile = self.service().users().getProfile(userId="me").execute()
        sender = profile.get("emailAddress", "me")
        recipient = original["from"]
        subject = original["subject"]
        if not subject.lower().startswith("re:"):
            subject = f"Re: {subject}"

        message = EmailMessage()
        message["To"] = recipient
        message["From"] = sender
        message["Subject"] = subject
        internet_id = original.get("internet_message_id")
        if internet_id:
            message["In-Reply-To"] = internet_id
            message["References"] = internet_id
        message.set_content(body)

        raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
        draft_body = {
            "message": {
                "raw": raw,
                "threadId": original.get("thread_id"),
            }
        }
        draft = self.service().users().drafts().create(userId="me", body=draft_body).execute()
        return {
            "draft_id": draft["id"],
            "message_id": message_id,
            "to": recipient,
            "subject": subject,
            "body": body,
        }

    def send_draft(self, draft_id: str) -> dict[str, Any]:
        sent = self.service().users().drafts().send(userId="me", body={"id": draft_id}).execute()
        return {"sent": True, "message_id": sent.get("id"), "thread_id": sent.get("threadId")}

    @staticmethod
    def _headers(msg: dict[str, Any]) -> dict[str, str]:
        return {
            h.get("name", "").lower(): h.get("value", "")
            for h in msg.get("payload", {}).get("headers", [])
        }

    def _extract_body(self, payload: dict[str, Any]) -> str:
        plain = self._find_part(payload, "text/plain")
        if plain:
            return self._decode_body(plain)
        html_part = self._find_part(payload, "text/html")
        if html_part:
            raw_html = self._decode_body(html_part)
            text = re.sub(r"<\s*br\s*/?>", "\n", raw_html, flags=re.I)
            text = re.sub(r"</p\s*>", "\n\n", text, flags=re.I)
            text = re.sub(r"<[^>]+>", "", text)
            return html.unescape(text).strip()
        body = payload.get("body", {}).get("data")
        return self._decode_data(body) if body else ""

    def _find_part(self, payload: dict[str, Any], mime_type: str) -> str | None:
        if payload.get("mimeType") == mime_type:
            return payload.get("body", {}).get("data")
        for part in payload.get("parts", []) or []:
            found = self._find_part(part, mime_type)
            if found:
                return found
        return None

    @staticmethod
    def _decode_body(data: str) -> str:
        return GmailClient._decode_data(data)

    @staticmethod
    def _decode_data(data: str) -> str:
        padding = "=" * (-len(data) % 4)
        return base64.urlsafe_b64decode(data + padding).decode("utf-8", errors="replace").strip()
