"""DeepSeek API adapter used as a low-cost text-classification fallback.

The adapter intentionally implements the same small public surface consumed by
``freeform_intake`` as :class:`GeminiAdapter`: ``classify_intake`` and
``classify_intake_batch``.  Audio transcription remains Gemini-only because the
DeepSeek Chat Completions API accepts text messages, not Telegram audio blobs.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any, Optional

import aiohttp


logger = logging.getLogger(__name__)

_RETRYABLE_STATUSES = {429, 500, 502, 503, 504}
_INITIAL_BACKOFF_SEC = 1.0
_MAX_BACKOFF_SEC = 12.0
_MAX_RETRIES = 2
_CIRCUIT_BREAKER_THRESHOLD = 4
_CIRCUIT_BREAKER_COOLDOWN_SEC = 120


class DeepSeekCircuitBreakerOpen(RuntimeError):
    """Raised while repeated DeepSeek failures keep its circuit open."""


class DeepSeekAdapter:
    """Minimal async DeepSeek Chat Completions client with JSON output."""

    def __init__(
        self,
        *,
        api_key: str = "",
        base_url: str = "https://api.deepseek.com",
        model: str = "deepseek-v4-flash",
        timeout_sec: int = 45,
        user_id: str = "personal-assistant",
    ) -> None:
        self._api_key = str(api_key or "").strip()
        self._base_url = str(base_url or "https://api.deepseek.com").rstrip("/")
        self._model = str(model or "deepseek-v4-flash").strip()
        self._timeout = aiohttp.ClientTimeout(total=max(5, int(timeout_sec or 45)))
        self._session: Optional[aiohttp.ClientSession] = None
        cleaned_user_id = re.sub(r"[^a-zA-Z0-9_-]+", "-", str(user_id or "personal-assistant"))
        self._user_id = cleaned_user_id.strip("-")[:512] or "personal-assistant"
        self._failure_count = 0
        self._circuit_open_until = 0.0

    @property
    def enabled(self) -> bool:
        return bool(self._api_key)

    @property
    def circuit_open(self) -> bool:
        return time.time() < self._circuit_open_until

    async def startup(self) -> None:
        if self.enabled:
            await self._get_session()

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self._session

    def _check_circuit(self) -> None:
        if self.circuit_open:
            remaining = max(1, int(self._circuit_open_until - time.time()))
            raise DeepSeekCircuitBreakerOpen(f"DeepSeek circuit open, retry in {remaining}s")

    def _record_success(self) -> None:
        self._failure_count = 0

    def _record_failure(self) -> None:
        self._failure_count += 1
        if self._failure_count >= _CIRCUIT_BREAKER_THRESHOLD:
            self._circuit_open_until = time.time() + _CIRCUIT_BREAKER_COOLDOWN_SEC
            logger.warning(
                "DeepSeek circuit breaker OPEN failures=%d cooldown=%ds",
                self._failure_count,
                _CIRCUIT_BREAKER_COOLDOWN_SEC,
            )

    @staticmethod
    def _extract_json_object(raw: str) -> dict[str, Any]:
        text = str(raw or "").strip()
        if not text:
            raise ValueError("empty DeepSeek response")
        if text.startswith("```"):
            text = text.strip("`").strip()
            if text.lower().startswith("json"):
                text = text[4:].strip()
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end >= start:
            text = text[start : end + 1]
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("DeepSeek response is not a JSON object")
        return data

    @staticmethod
    def _extract_text(data: dict[str, Any]) -> str:
        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError("DeepSeek returned no choices")
        message = (choices[0] or {}).get("message") or {}
        content = str(message.get("content") or "").strip()
        if not content:
            raise RuntimeError("DeepSeek returned empty content")
        return content

    async def _chat_json(self, *, system_prompt: str, user_prompt: str, operation: str) -> dict[str, Any]:
        if not self.enabled:
            raise RuntimeError("DeepSeek adapter is not configured")
        self._check_circuit()

        payload = {
            "model": self._model,
            "messages": [
                {
                    "role": "system",
                    "content": system_prompt
                    + "\nReturn exactly one valid JSON object. Do not use markdown or commentary.",
                },
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0,
            "stream": False,
            "response_format": {"type": "json_object"},
            "thinking": {"type": "disabled"},
            "max_tokens": 4096,
            "user_id": self._user_id,
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        backoff = _INITIAL_BACKOFF_SEC
        last_error: Exception | None = None
        for retry in range(_MAX_RETRIES + 1):
            started = time.time()
            try:
                self._check_circuit()
                session = await self._get_session()
                async with session.post(
                    f"{self._base_url}/chat/completions",
                    headers=headers,
                    json=payload,
                ) as response:
                    body = await response.text()
                    latency_ms = int((time.time() - started) * 1000)
                    if response.status in _RETRYABLE_STATUSES:
                        last_error = RuntimeError(
                            f"DeepSeek retryable failure: {response.status} {body[:300]}"
                        )
                        logger.warning(
                            "DeepSeek retryable status=%d operation=%s retry=%d/%d latency_ms=%d",
                            response.status,
                            operation,
                            retry,
                            _MAX_RETRIES,
                            latency_ms,
                        )
                        if retry < _MAX_RETRIES:
                            await asyncio.sleep(backoff)
                            backoff = min(backoff * 2.0, _MAX_BACKOFF_SEC)
                            continue
                        break
                    if response.status >= 400:
                        raise RuntimeError(
                            f"DeepSeek request failed: {response.status} {body[:500]}"
                        )
                    data = json.loads(body)
                    result = self._extract_json_object(self._extract_text(data))
                    self._record_success()
                    logger.info(
                        "DeepSeek fallback succeeded operation=%s model=%s latency_ms=%d",
                        operation,
                        self._model,
                        latency_ms,
                    )
                    return result
            except (DeepSeekCircuitBreakerOpen, ValueError, json.JSONDecodeError) as exc:
                last_error = exc
                break
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
                last_error = RuntimeError(f"DeepSeek network error: {exc}")
                logger.warning(
                    "DeepSeek network error operation=%s retry=%d/%d error=%s",
                    operation,
                    retry,
                    _MAX_RETRIES,
                    exc,
                )
                if retry < _MAX_RETRIES:
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2.0, _MAX_BACKOFF_SEC)
                    continue
                break
            except RuntimeError as exc:
                last_error = exc
                break

        self._record_failure()
        raise last_error or RuntimeError("DeepSeek request failed")

    async def classify_intake(self, *, system_prompt: str, user_prompt: str) -> dict[str, Any]:
        return await self._chat_json(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            operation="classify",
        )

    async def classify_intake_batch(self, *, system_prompt: str, user_prompt: str) -> dict[str, Any]:
        return await self._chat_json(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            operation="classify_batch",
        )

    async def transcribe_audio(self, **_: Any) -> str:
        raise RuntimeError("DeepSeek text API does not support audio transcription")
