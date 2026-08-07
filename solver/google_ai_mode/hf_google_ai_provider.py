"""Cliente privado do Google Modo IA hospedado em Hugging Face Spaces.

Somente a imagem efemera do desafio e o prompt visual sao enviados. O token
do Hugging Face vem do ambiente do processo e nunca aparece em logs/health.
"""

from __future__ import annotations

import re
import hashlib
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class HuggingFaceProviderError(RuntimeError):
    """Falha segura e classificavel da rota Hugging Face."""


@dataclass(slots=True)
class HuggingFaceGoogleAIResult:
    answer: str
    http_requests: int = 0
    ai_queries: int = 1
    sources: tuple[Any, ...] = ()
    route: str = "huggingface"


def _safe_error(exc: Exception, token: str) -> str:
    detail = str(exc or type(exc).__name__)
    if token:
        detail = detail.replace(token, "[REDACTED]")
    detail = re.sub(r"https?://[^\s]+", "[remote-url]", detail)
    return f"{type(exc).__name__}: {detail}"[:500]


class HuggingFaceGoogleAIProvider:
    def __init__(
        self,
        *,
        space_id: str,
        token: str,
        timeout_seconds: float = 75.0,
        cooldown_seconds: float = 180.0,
        api_name: str = "/test_google_ai",
    ) -> None:
        self.space_id = str(space_id or "").strip()
        self.token = str(token or "").strip()
        self.timeout_seconds = max(15.0, float(timeout_seconds))
        self.cooldown_seconds = max(15.0, float(cooldown_seconds))
        self.api_name = str(api_name or "/test_google_ai").strip()
        self._lock = threading.Lock()
        self._client: Any = None
        self._cooldown_until = 0.0
        self._last_error: str | None = None
        self._successes = 0
        self._failures = 0

    @property
    def configured(self) -> bool:
        return bool(self.space_id and self.token)

    def _get_client(self) -> Any:
        if self._client is None:
            from gradio_client import Client

            self._client = Client(
                self.space_id,
                token=self.token,
                verbose=False,
                download_files=False,
                analytics_enabled=False,
                httpx_kwargs={"timeout": min(self.timeout_seconds, 45.0)},
            )
        return self._client

    def query(self, image_path: str | Path, prompt: str) -> HuggingFaceGoogleAIResult:
        if not self.configured:
            raise HuggingFaceProviderError("huggingface_not_configured")
        image = Path(image_path)
        if not image.is_file() or image.stat().st_size <= 0:
            raise HuggingFaceProviderError("huggingface_image_missing")

        with self._lock:
            remaining = self._cooldown_until - time.monotonic()
            if remaining > 0:
                raise HuggingFaceProviderError(
                    f"huggingface_circuit_open:{int(remaining) + 1}s"
                )
            job = None
            try:
                from gradio_client import handle_file

                client = self._get_client()
                job = client.submit(
                    handle_file(str(image)),
                    str(prompt),
                    api_name=self.api_name,
                )
                payload = job.result(timeout=self.timeout_seconds)
                if not isinstance(payload, dict):
                    raise HuggingFaceProviderError("huggingface_invalid_response")
                if not payload.get("ok") or not str(payload.get("answer") or "").strip():
                    reason = str(payload.get("error_type") or payload.get("error") or "provider_failed")
                    unusual = bool(payload.get("unusual_traffic"))
                    raise HuggingFaceProviderError(
                        f"huggingface_{'unusual_traffic' if unusual else reason}"[:300]
                    )
                self._successes += 1
                self._last_error = None
                self._cooldown_until = 0.0
                source_count = max(0, int(payload.get("source_count") or 0))
                return HuggingFaceGoogleAIResult(
                    answer=str(payload["answer"]),
                    http_requests=max(0, int(payload.get("http_requests") or 0)),
                    ai_queries=max(1, int(payload.get("ai_queries") or 1)),
                    sources=tuple(None for _ in range(source_count)),
                )
            except Exception as exc:
                if job is not None:
                    try:
                        job.cancel()
                    except Exception:
                        pass
                self._client = None
                self._failures += 1
                self._last_error = _safe_error(exc, self.token)
                self._cooldown_until = time.monotonic() + self.cooldown_seconds
                raise HuggingFaceProviderError(self._last_error) from exc

    def health(self) -> dict[str, Any]:
        remaining = max(0.0, self._cooldown_until - time.monotonic())
        return {
            "configured": self.configured,
            "space": self.space_id if self.configured else None,
            "api_name": self.api_name,
            "serialized": True,
            "cooldown_remaining_seconds": round(remaining, 1),
            "successes": self._successes,
            "failures": self._failures,
            "last_error": self._last_error,
            "token_exposed": False,
        }


class HuggingFaceGoogleAIPool:
    """Distribui imagens entre Spaces e mantém circuito separado por Space."""

    def __init__(
        self,
        *,
        space_ids: list[str],
        token: str,
        timeout_seconds: float = 60.0,
        cooldown_seconds: float = 180.0,
        api_name: str = "/test_google_ai",
    ) -> None:
        unique = list(dict.fromkeys(str(item or "").strip() for item in space_ids if str(item or "").strip()))
        self.providers = [
            HuggingFaceGoogleAIProvider(
                space_id=space_id,
                token=token,
                timeout_seconds=timeout_seconds,
                cooldown_seconds=cooldown_seconds,
                api_name=api_name,
            )
            for space_id in unique
        ]

    @property
    def configured(self) -> bool:
        return any(provider.configured for provider in self.providers)

    def query(self, image_path: str | Path, prompt: str) -> HuggingFaceGoogleAIResult:
        configured = [provider for provider in self.providers if provider.configured]
        if not configured:
            raise HuggingFaceProviderError("huggingface_pool_not_configured")
        image = Path(image_path)
        digest = hashlib.sha256(image.read_bytes()).digest()
        offset = int.from_bytes(digest[:4], "big") % len(configured)
        ordered = configured[offset:] + configured[:offset]
        errors: list[str] = []
        for provider in ordered:
            try:
                result = provider.query(image, prompt)
                result.route = f"huggingface:{provider.space_id}"
                return result
            except HuggingFaceProviderError as exc:
                errors.append(f"{provider.space_id}:{_safe_error(exc, provider.token)}")
        raise HuggingFaceProviderError("huggingface_pool_failed:" + " | ".join(errors))

    def health(self) -> dict[str, Any]:
        return {
            "configured": self.configured,
            "spaces": [provider.health() for provider in self.providers],
            "count": len(self.providers),
            "token_exposed": False,
        }
