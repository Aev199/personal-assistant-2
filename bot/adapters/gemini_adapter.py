"""Gemini API adapter with retry, circuit breaker, fallback, and multi-intent batch support."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
from typing import Any, Optional

import aiohttp

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Retry / backoff constants
# ---------------------------------------------------------------------------
_INITIAL_BACKOFF_SEC = 1.0
_MAX_BACKOFF_SEC = 30.0
_BACKOFF_MULTIPLIER = 2.0
_MAX_RETRIES_PER_MODEL = 3

# Circuit breaker
_CIRCUIT_BREAKER_THRESHOLD = 5       # consecutive failures
_CIRCUIT_BREAKER_COOLDOWN_SEC = 120  # disable LLM for 2 minutes

# 503 cache TTL
_503_CACHE_TTL_SEC = 120

# Retryable HTTP statuses (429 = rate limit, 5xx = server errors)
_RETRYABLE_STATUSES = {429, 500, 502, 503, 504}


class CircuitBreakerOpen(RuntimeError):
    """Raised when the circuit breaker is open (LLM temporarily disabled)."""


class GeminiAdapter:
    """Async Gemini API client with retry, circuit breaker, and multi-model fallback."""

    def __init__(
        self,
        *,
        api_key: str = "",
        base_url: str = "https://generativelanguage.googleapis.com/v1beta",
        llm_model: str = "gemini-2.5-flash",
        transcribe_model: str = "gemini-2.5-flash",
        timeout_sec: int = 45,
        fallback_models: list[str] | None = None,
    ) -> None:
        self._api_key = (api_key or "").strip()
        self._base_url = (base_url or "https://generativelanguage.googleapis.com/v1beta").rstrip("/")
        self._llm_model = (llm_model or "gemini-2.5-flash").strip()
        self._transcribe_model = (transcribe_model or "gemini-2.5-flash").strip()
        self._session: Optional[aiohttp.ClientSession] = None
        self._timeout = aiohttp.ClientTimeout(total=max(5, int(timeout_sec or 45)))

        # --- Fallback models (env-overridable) ---
        if fallback_models is None:
            raw = os.getenv("GEMINI_FALLBACK_MODELS", "").strip()
            if raw:
                fallback_models = [m.strip() for m in raw.split(",") if m.strip()]
            else:
                fallback_models = ["gemini-1.5-flash", "gemini-2.0-flash-exp"]
        self._fallback_models = fallback_models

        # --- Per-model 503 cache ---
        self._last_503_time: dict[str, float] = {}
        self._last_503_cleanup_ts: float = 0.0

        # --- Circuit breaker ---
        self._failure_count: int = 0
        self._circuit_open_until: float = 0.0

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return bool(self._api_key)

    @property
    def circuit_open(self) -> bool:
        """True when the circuit breaker has temporarily disabled LLM calls."""
        return time.time() < self._circuit_open_until

    async def startup(self) -> None:
        await self._get_session()

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self._session

    def _headers(self) -> dict[str, str]:
        return {
            "x-goog-api-key": self._api_key,
            "Content-Type": "application/json",
        }

    def _record_success(self) -> None:
        self._failure_count = 0
        # Don't reset circuit_open_until — it expires naturally

    def _record_failure(self) -> None:
        self._failure_count += 1
        if self._failure_count >= _CIRCUIT_BREAKER_THRESHOLD:
            self._circuit_open_until = time.time() + _CIRCUIT_BREAKER_COOLDOWN_SEC
            logger.warning(
                "Gemini circuit breaker OPEN (failures=%d, cooldown=%ds)",
                self._failure_count,
                _CIRCUIT_BREAKER_COOLDOWN_SEC,
            )

    def _check_circuit(self) -> None:
        if self.circuit_open:
            remaining = int(self._circuit_open_until - time.time())
            raise CircuitBreakerOpen(
                f"Gemini circuit breaker open, retry in {remaining}s"
            )

    def _cleanup_503_cache(self) -> None:
        now = time.time()
        if now - self._last_503_cleanup_ts < 60:
            return
        self._last_503_cleanup_ts = now
        stale = [k for k, ts in self._last_503_time.items() if now - ts > _503_CACHE_TTL_SEC]
        for k in stale:
            del self._last_503_time[k]

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

    # ------------------------------------------------------------------
    # Core request with retry + fallback + circuit breaker
    # ------------------------------------------------------------------

    async def _generate_content_with_fallback(
        self, *, model: str, payload: dict[str, Any], operation: str = "llm"
    ) -> dict[str, Any]:
        """Try primary model, fallback to alternatives with exponential backoff."""
        self._check_circuit()

        if not self.enabled:
            raise RuntimeError("Gemini adapter is not configured")

        models_to_try = [model] + [
            m for m in self._fallback_models if m != model
        ]
        last_error: Optional[Exception] = None

        for attempt_model in models_to_try:
            # Skip if this model had 503 recently
            cache_key = f"{operation}:{attempt_model}"
            self._cleanup_503_cache()
            if cache_key in self._last_503_time:
                if time.time() - self._last_503_time[cache_key] < 60:
                    continue

            backoff = _INITIAL_BACKOFF_SEC
            for retry in range(1 + _MAX_RETRIES_PER_MODEL):
                t0 = time.time()
                try:
                    self._check_circuit()

                    session = await self._get_session()
                    async with session.post(
                        f"{self._base_url}/models/{attempt_model}:generateContent",
                        headers=self._headers(),
                        json=payload,
                    ) as resp:
                        body = await resp.text()
                        elapsed_ms = int((time.time() - t0) * 1000)

                        if resp.status == 429:
                            logger.warning(
                                "Gemini rate-limited model=%s operation=%s retry=%d/%d latency_ms=%d",
                                attempt_model, operation, retry, _MAX_RETRIES_PER_MODEL, elapsed_ms,
                            )
                            if retry < _MAX_RETRIES_PER_MODEL:
                                await asyncio.sleep(backoff)
                                backoff = min(backoff * _BACKOFF_MULTIPLIER, _MAX_BACKOFF_SEC)
                                continue
                            raise RuntimeError(
                                f"Gemini rate-limited after {_MAX_RETRIES_PER_MODEL} retries"
                            )

                        if resp.status == 503:
                            self._last_503_time[cache_key] = time.time()
                            logger.warning(
                                "Gemini 503 model=%s operation=%s latency_ms=%d",
                                attempt_model, operation, elapsed_ms,
                            )
                            last_error = RuntimeError(f"Model {attempt_model} unavailable (503)")
                            break  # try next model

                        if resp.status in _RETRYABLE_STATUSES:
                            logger.warning(
                                "Gemini retryable status=%d model=%s operation=%s retry=%d/%d latency_ms=%d",
                                resp.status, attempt_model, operation, retry, _MAX_RETRIES_PER_MODEL, elapsed_ms,
                            )
                            if retry < _MAX_RETRIES_PER_MODEL:
                                await asyncio.sleep(backoff)
                                backoff = min(backoff * _BACKOFF_MULTIPLIER, _MAX_BACKOFF_SEC)
                                continue
                            raise RuntimeError(
                                f"Gemini request failed after retries: {resp.status} {body[:300]}"
                            )

                        if resp.status >= 400:
                            raise RuntimeError(f"Gemini request failed: {resp.status} {body[:500]}")

                        logger.debug(
                            "Gemini ok model=%s operation=%s latency_ms=%d",
                            attempt_model, operation, elapsed_ms,
                        )
                        self._record_success()
                        return json.loads(body)

                except (CircuitBreakerOpen, RuntimeError, ValueError):
                    raise
                except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
                    logger.warning(
                        "Gemini network error model=%s operation=%s retry=%d/%d err=%s",
                        attempt_model, operation, retry, _MAX_RETRIES_PER_MODEL, e,
                    )
                    if retry < _MAX_RETRIES_PER_MODEL:
                        await asyncio.sleep(backoff)
                        backoff = min(backoff * _BACKOFF_MULTIPLIER, _MAX_BACKOFF_SEC)
                        continue
                    last_error = RuntimeError(f"Gemini network error after retries: {e}")

        # All models failed
        self._record_failure()
        raise last_error or RuntimeError("All fallback models failed")

    # ------------------------------------------------------------------
    # Single-intent classification
    # ------------------------------------------------------------------

    _SINGLE_ACTION_JSON_SCHEMA = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["task", "personal_task", "reminder", "event", "idea", "reply"],
            },
            "title": {"type": "string"},
            "idea_text": {"type": "string"},
            "deadline_local": {"type": ["string", "null"]},
            "reminder_text": {"type": "string"},
            "remind_at_local": {"type": ["string", "null"]},
            "calendar_kind": {
                "type": ["string", "null"],
                "enum": ["work", "personal", None],
            },
            "start_at_local": {"type": ["string", "null"]},
            "duration_min": {"type": ["integer", "null"]},
            "project_code": {"type": ["string", "null"]},
            "project_name": {"type": ["string", "null"]},
            "assignee_name": {"type": ["string", "null"]},
            "reply": {"type": "string"},
        },
        "required": ["action", "reply"],
        "additionalProperties": False,
    }

    async def classify_intake(self, *, system_prompt: str, user_prompt: str) -> dict[str, Any]:
        """Classify a single user message into one intent."""
        payload = {
            "system_instruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
            "generationConfig": {
                "temperature": 0,
                "responseMimeType": "application/json",
                "responseJsonSchema": self._SINGLE_ACTION_JSON_SCHEMA,
            },
        }
        data = await self._generate_content_with_fallback(
            model=self._llm_model, payload=payload, operation="classify"
        )
        return self._extract_json_object(self._extract_text(data))

    # ------------------------------------------------------------------
    # Multi-intent (batch) classification
    # ------------------------------------------------------------------

    _BATCH_ACTION_JSON_SCHEMA = {
        "type": "object",
        "properties": {
            "actions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["task", "personal_task", "reminder", "event", "idea"],
                        },
                        "title": {"type": "string"},
                        "idea_text": {"type": "string"},
                        "deadline_local": {"type": ["string", "null"]},
                        "reminder_text": {"type": "string"},
                        "remind_at_local": {"type": ["string", "null"]},
                        "calendar_kind": {
                            "type": ["string", "null"],
                            "enum": ["work", "personal", None],
                        },
                        "start_at_local": {"type": ["string", "null"]},
                        "duration_min": {"type": ["integer", "null"]},
                        "project_code": {"type": ["string", "null"]},
                        "project_name": {"type": ["string", "null"]},
                        "assignee_name": {"type": ["string", "null"]},
                    },
                    "required": ["action", "title"],
                    "additionalProperties": False,
                },
                "minItems": 1,
            },
            "reply": {"type": "string"},
        },
        "required": ["actions", "reply"],
        "additionalProperties": False,
    }

    async def classify_intake_batch(
        self, *, system_prompt: str, user_prompt: str
    ) -> dict[str, Any]:
        """Classify a message into zero or more intents (batch mode)."""
        payload = {
            "system_instruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
            "generationConfig": {
                "temperature": 0,
                "responseMimeType": "application/json",
                "responseJsonSchema": self._BATCH_ACTION_JSON_SCHEMA,
            },
        }
        data = await self._generate_content_with_fallback(
            model=self._llm_model, payload=payload, operation="classify_batch"
        )
        return self._extract_json_object(self._extract_text(data))

    # ------------------------------------------------------------------
    # Audio transcription
    # ------------------------------------------------------------------

    async def transcribe_audio(
        self, *, audio_bytes: bytes, filename: str, mime_type: str | None = None
    ) -> str:
        if not audio_bytes:
            raise ValueError("audio payload is empty")
        payload = {
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {
                            "text": (
                                "Generate a verbatim transcript of the speech in this audio. "
                                "Return only the transcript text."
                            ),
                        },
                        {
                            "inline_data": {
                                "mime_type": mime_type or "audio/ogg",
                                "data": base64.b64encode(audio_bytes).decode("ascii"),
                            }
                        },
                    ],
                }
            ],
            "generationConfig": {"temperature": 0},
        }
        data = await self._generate_content_with_fallback(
            model=self._transcribe_model, payload=payload, operation="transcribe"
        )
        return self._extract_text(data).strip()
