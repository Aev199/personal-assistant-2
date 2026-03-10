"""Minimal Gemini API adapter for free-form intake and voice transcription."""

from __future__ import annotations

import base64
import json
from typing import Any, Optional

import aiohttp


class GeminiAdapter:
    def __init__(
        self,
        *,
        api_key: str = "",
        base_url: str = "https://generativelanguage.googleapis.com/v1beta",
        llm_model: str = "gemini-2.5-flash",
        transcribe_model: str = "gemini-2.5-flash",
        timeout_sec: int = 45,
    ) -> None:
        self._api_key = (api_key or "").strip()
        self._base_url = (base_url or "https://generativelanguage.googleapis.com/v1beta").rstrip("/")
        self._llm_model = (llm_model or "gemini-2.5-flash").strip()
        self._transcribe_model = (transcribe_model or "gemini-2.5-flash").strip()
        self._session: Optional[aiohttp.ClientSession] = None
        self._timeout = aiohttp.ClientTimeout(total=max(5, int(timeout_sec or 45)))

    @property
    def enabled(self) -> bool:
        return bool(self._api_key)

    async def startup(self) -> None:
        await self._get_session()

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self._session

    def _headers(self) -> dict[str, str]:
        return {
            "x-goog-api-key": self._api_key,
            "Content-Type": "application/json",
        }

    @staticmethod
    def _extract_json_object(raw: str) -> dict[str, Any]:
        text = str(raw or "").strip()
        if not text:
            raise ValueError("empty Gemini response")
        if text.startswith("```"):
            text = text.strip("`")
            if text.lower().startswith("json"):
                text = text[4:].strip()
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end >= start:
            text = text[start : end + 1]
        payload = json.loads(text)
        if not isinstance(payload, dict):
            raise ValueError("Gemini response is not a JSON object")
        return payload

    @staticmethod
    def _extract_text(data: dict[str, Any]) -> str:
        candidates = data.get("candidates") or []
        if not candidates:
            feedback = data.get("promptFeedback") or {}
            block_reason = feedback.get("blockReason")
            if block_reason:
                raise RuntimeError(f"Gemini returned no candidates: {block_reason}")
            raise RuntimeError("Gemini returned no candidates")
        content = (candidates[0] or {}).get("content") or {}
        parts = content.get("parts") or []
        texts: list[str] = []
        for part in parts:
            if not isinstance(part, dict):
                continue
            txt = part.get("text")
            if txt:
                texts.append(str(txt))
        out = "\n".join(texts).strip()
        if not out:
            raise RuntimeError("Gemini returned empty text")
        return out

    async def _generate_content(self, *, model: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.enabled:
            raise RuntimeError("Gemini adapter is not configured")
        session = await self._get_session()
        async with session.post(
            f"{self._base_url}/models/{model}:generateContent",
            headers=self._headers(),
            json=payload,
        ) as resp:
            body = await resp.text()
            if resp.status >= 400:
                raise RuntimeError(f"Gemini request failed: {resp.status} {body[:500]}")
            return json.loads(body)

    async def classify_intake(self, *, system_prompt: str, user_prompt: str) -> dict[str, Any]:
        payload = {
            "system_instruction": {
                "parts": [{"text": system_prompt}],
            },
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": user_prompt}],
                }
            ],
            "generationConfig": {
                "temperature": 0,
                "responseMimeType": "application/json",
                "responseJsonSchema": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["task", "reminder", "nav", "reply"],
                        },
                        "title": {"type": "string"},
                        "deadline_local": {"type": ["string", "null"]},
                        "reminder_text": {"type": "string"},
                        "remind_at_local": {"type": ["string", "null"]},
                        "project_code": {"type": ["string", "null"]},
                        "screen": {
                            "type": ["string", "null"],
                            "enum": [
                                "home",
                                "projects",
                                "today",
                                "overdue",
                                "all_tasks",
                                "work",
                                "inbox",
                                "help",
                                "add",
                                "team",
                                "stats",
                                None,
                            ],
                        },
                        "reply": {"type": "string"},
                    },
                    "required": ["action", "reply"],
                    "additionalProperties": True,
                },
            },
        }
        data = await self._generate_content(model=self._llm_model, payload=payload)
        return self._extract_json_object(self._extract_text(data))

    async def transcribe_audio(self, *, audio_bytes: bytes, filename: str, mime_type: str | None = None) -> str:
        if not audio_bytes:
            raise ValueError("audio payload is empty")
        payload = {
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {"text": "Generate a verbatim transcript of the speech in this audio. Return only the transcript text."},
                        {
                            "inline_data": {
                                "mime_type": mime_type or "audio/ogg",
                                "data": base64.b64encode(audio_bytes).decode("ascii"),
                            }
                        },
                    ],
                }
            ],
            "generationConfig": {
                "temperature": 0,
            },
        }
        data = await self._generate_content(model=self._transcribe_model, payload=payload)
        return self._extract_text(data).strip()
