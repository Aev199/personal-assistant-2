"""Provider router: Gemini first, DeepSeek fallback for text classification."""

from __future__ import annotations

import logging
from typing import Any

from bot.adapters.deepseek_adapter import DeepSeekAdapter
from bot.adapters.gemini_adapter import GeminiAdapter


logger = logging.getLogger(__name__)


class ResilientLLMAdapter:
    """Expose one LLM interface while failing over between providers.

    Text classification prefers Gemini and falls back to DeepSeek on network,
    quota, service, circuit-breaker, malformed-response, or configuration
    failures. Audio transcription intentionally remains Gemini-only.
    """

    def __init__(self, *, gemini: GeminiAdapter, deepseek: DeepSeekAdapter) -> None:
        self.gemini = gemini
        self.deepseek = deepseek
        self.last_provider: str | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.gemini.enabled or self.deepseek.enabled)

    @property
    def circuit_open(self) -> bool:
        gemini_unavailable = (not self.gemini.enabled) or self.gemini.circuit_open
        deepseek_unavailable = (not self.deepseek.enabled) or self.deepseek.circuit_open
        return bool(gemini_unavailable and deepseek_unavailable)

    @property
    def transcription_enabled(self) -> bool:
        return bool(self.gemini.enabled and not self.gemini.circuit_open)

    async def startup(self) -> None:
        """Start both clients independently.

        One provider failing to allocate its session must never prevent the
        other provider from becoming usable. Requests still perform their own
        lazy session setup, so startup failures are logged rather than fatal.
        """
        if self.gemini.enabled:
            try:
                await self.gemini.startup()
            except Exception as exc:
                logger.warning(
                    "Gemini startup failed; DeepSeek may still serve text requests error=%s",
                    exc,
                )
        if self.deepseek.enabled:
            try:
                await self.deepseek.startup()
            except Exception as exc:
                logger.warning(
                    "DeepSeek startup failed; Gemini may still serve requests error=%s",
                    exc,
                )

    async def close(self) -> None:
        for name, provider in (("gemini", self.gemini), ("deepseek", self.deepseek)):
            try:
                await provider.close()
            except Exception as exc:
                logger.warning("LLM provider close failed provider=%s error=%s", name, exc)

    async def _call_text(self, method: str, **kwargs: Any) -> dict[str, Any]:
        primary_error: Exception | None = None

        if self.gemini.enabled and not self.gemini.circuit_open:
            try:
                result = await getattr(self.gemini, method)(**kwargs)
                self.last_provider = "gemini"
                return result
            except Exception as exc:
                primary_error = exc
                logger.warning(
                    "Gemini failed; trying DeepSeek method=%s error_type=%s error=%s",
                    method,
                    type(exc).__name__,
                    exc,
                )
        elif self.gemini.enabled:
            primary_error = RuntimeError("Gemini circuit breaker is open")
        else:
            primary_error = RuntimeError("Gemini is not configured")

        if self.deepseek.enabled and not self.deepseek.circuit_open:
            try:
                result = await getattr(self.deepseek, method)(**kwargs)
                self.last_provider = "deepseek"
                return result
            except Exception as fallback_error:
                logger.error(
                    "DeepSeek fallback failed method=%s error_type=%s error=%s",
                    method,
                    type(fallback_error).__name__,
                    fallback_error,
                )
                raise RuntimeError(
                    f"Both LLM providers failed: Gemini={primary_error}; DeepSeek={fallback_error}"
                ) from fallback_error

        if self.deepseek.enabled:
            raise RuntimeError(
                f"Gemini failed and DeepSeek circuit is open: {primary_error}"
            ) from primary_error
        raise primary_error or RuntimeError("No LLM provider is configured")

    async def classify_intake(self, *, system_prompt: str, user_prompt: str) -> dict[str, Any]:
        return await self._call_text(
            "classify_intake",
            system_prompt=system_prompt,
            user_prompt=user_prompt,
        )

    async def classify_intake_batch(self, *, system_prompt: str, user_prompt: str) -> dict[str, Any]:
        return await self._call_text(
            "classify_intake_batch",
            system_prompt=system_prompt,
            user_prompt=user_prompt,
        )

    async def transcribe_audio(
        self,
        *,
        audio_bytes: bytes,
        filename: str,
        mime_type: str | None = None,
    ) -> str:
        if not self.gemini.enabled:
            raise RuntimeError(
                "Voice transcription requires GEMINI_API_KEY; DeepSeek fallback is text-only"
            )
        result = await self.gemini.transcribe_audio(
            audio_bytes=audio_bytes,
            filename=filename,
            mime_type=mime_type,
        )
        self.last_provider = "gemini"
        return result
