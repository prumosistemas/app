"""Resolvedor visual Qwen via Hugging Face Inference Providers.

O cliente usa os tokens HF ja injetados no processo e devolve o mesmo contrato
minimo consumido pelo resolvedor visual. Segredos nunca entram em logs ou health.
"""

from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests


class HuggingFaceQwenError(RuntimeError):
    """Falha segura e classificavel da inferencia Qwen."""


@dataclass(slots=True)
class HuggingFaceQwenResult:
    answer: str
    http_requests: int = 1
    ai_queries: int = 1
    sources: tuple[Any, ...] = ()
    route: str = "huggingface_qwen"


def temporal_target_prompt(question: str) -> str:
    """Prompt curto para mapas temporais, reduzindo tokens e latencia."""
    return f"""
Analyze only the attached image. The original challenge asks: "{question}".
The image is a temporal occupancy overlay of one scene. Static targets remain
sharp. Cyan/turquoise marks show where the moving object visited or stayed;
strong cyan means repeated occupancy. For "never", choose the only target with
no cyan directly on its body, petals, or center. Cyan in the background between
targets does not count.

Return only one valid JSON object, without Markdown:
{{"label":"short target name","box":{{"x1":0,"y1":0,"x2":0,"y2":0}},"x":0,"y":0,"confidence":0.0}}
All coordinates must be integers normalized from 0 to 1000 over the full image.
The point x,y must be safely inside the selected target. Do not list rejected
targets and do not add any other keys.
""".strip()


def _safe_error(exc: Exception, token: str = "") -> str:
    detail = str(exc or type(exc).__name__)
    if token:
        detail = detail.replace(token, "[REDACTED]")
    detail = re.sub(r"https?://[^\s]+", "[remote-url]", detail)
    return f"{type(exc).__name__}: {detail}"[:400]


def _json_candidates(text: str):
    stripped = str(text or "").strip().lstrip("\ufeff")
    if stripped:
        yield stripped
    for match in re.finditer(r"```(?:json)?\s*(.*?)```", stripped, flags=re.I | re.S):
        yield match.group(1).strip()
    start, end = stripped.find("{"), stripped.rfind("}")
    if start >= 0 and end > start:
        yield stripped[start : end + 1]


def _parse_target(text: str) -> dict[str, Any]:
    parsed: dict[str, Any] | None = None
    for candidate in _json_candidates(text):
        variants = [candidate]
        # Alguns providers devolvem uma chave isolada sem aspas, por exemplo
        # {"x": 400, y: 300}. A reparacao e deliberadamente estreita.
        variants.append(
            re.sub(r'([,{]\s*)([A-Za-z_][A-Za-z0-9_]*)\s*:', r'\1"\2":', candidate)
        )
        for variant in variants:
            try:
                value = json.loads(variant)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                parsed = value
                break
        if parsed is not None:
            break
    if parsed is None:
        raise HuggingFaceQwenError("qwen_invalid_json")

    x, y = parsed.get("x"), parsed.get("y")
    if isinstance(x, (list, tuple)) and len(x) >= 2 and y is None:
        x, y = x[0], x[1]
    try:
        x, y = float(x), float(y)
    except (TypeError, ValueError) as exc:
        raise HuggingFaceQwenError("qwen_missing_coordinates") from exc
    if not (0 <= x <= 1000 and 0 <= y <= 1000):
        raise HuggingFaceQwenError("qwen_coordinates_out_of_range")

    box = parsed.get("box") or parsed.get("caixa") or {}
    if isinstance(box, (list, tuple)) and len(box) >= 4:
        box = {"x1": box[0], "y1": box[1], "x2": box[2], "y2": box[3]}
    if isinstance(box, dict) and isinstance(box.get("x1"), (list, tuple)):
        packed = box["x1"]
        if len(packed) >= 4:
            box = {"x1": packed[0], "y1": packed[1], "x2": packed[2], "y2": packed[3]}
    try:
        x1, y1, x2, y2 = (float(box[key]) for key in ("x1", "y1", "x2", "y2"))
        if not (0 <= x1 < x2 <= 1000 and 0 <= y1 < y2 <= 1000):
            raise ValueError
    except (KeyError, TypeError, ValueError):
        # O clique ainda e relocado no frame vivo; uma caixa conservadora ao
        # redor do ponto permite usar respostas compactas sem inventar escala.
        x1, y1 = max(0.0, x - 55.0), max(0.0, y - 55.0)
        x2, y2 = min(1000.0, x + 55.0), min(1000.0, y + 55.0)

    try:
        confidence = min(1.0, max(0.0, float(parsed.get("confidence", 0.0))))
    except (TypeError, ValueError):
        confidence = 0.0
    if confidence < 0.72:
        raise HuggingFaceQwenError("qwen_low_confidence")
    label = str(parsed.get("label") or parsed.get("target") or "alvo temporal").strip()[:80]
    return {
        "acao": "clicar_ponto",
        "descricao_geral": "Alvo temporal localizado pelo Qwen3-VL.",
        "objetos": {
            "objeto_1": {
                "nome": label,
                "caixa": {"x1": x1, "y1": y1, "x2": x2, "y2": y2},
                "corresponde_pergunta": True,
                "confianca": confidence,
                "motivo": "alvo indicado na evidencia temporal",
            }
        },
        "escolha": {
            "objeto": "objeto_1",
            "x": x,
            "y": y,
            "descricao_do_alvo": label,
            "argumento": "alvo temporal indicado pelo modelo",
            "confianca": confidence,
        },
        "observacoes": "resposta compacta normalizada pelo adaptador Qwen",
    }


class HuggingFaceQwenProvider:
    def __init__(
        self,
        *,
        label: str,
        token: str,
        model: str = "Qwen/Qwen3-VL-235B-A22B-Instruct",
        timeout_seconds: float = 35.0,
        cooldown_seconds: float = 300.0,
    ) -> None:
        self.label = str(label or "hf").strip()[:40]
        self.token = str(token or "").strip()
        self.model = str(model or "").strip()
        self.timeout_seconds = max(10.0, float(timeout_seconds))
        self.cooldown_seconds = max(30.0, float(cooldown_seconds))
        self._lock = threading.Lock()
        self._cooldown_until = 0.0
        self._last_error: str | None = None
        self._successes = 0
        self._failures = 0
        self._prompt_tokens = 0
        self._completion_tokens = 0

    @property
    def configured(self) -> bool:
        return bool(self.token and self.model)

    def query(self, image_path: str | Path, prompt: str) -> HuggingFaceQwenResult:
        image = Path(image_path)
        if not self.configured:
            raise HuggingFaceQwenError("qwen_not_configured")
        if not image.is_file() or image.stat().st_size <= 0:
            raise HuggingFaceQwenError("qwen_image_missing")
        if not self._lock.acquire(blocking=False):
            raise HuggingFaceQwenError("qwen_busy")
        try:
            remaining = self._cooldown_until - time.monotonic()
            if remaining > 0:
                raise HuggingFaceQwenError(f"qwen_circuit_open:{int(remaining) + 1}s")
            mime = mimetypes.guess_type(str(image))[0] or "image/jpeg"
            encoded = base64.b64encode(image.read_bytes()).decode("ascii")
            payload = {
                "model": self.model,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:{mime};base64,{encoded}"},
                            },
                            {"type": "text", "text": str(prompt)},
                        ],
                    }
                ],
                "temperature": 0,
                "max_tokens": 220,
            }
            response = requests.post(
                "https://router.huggingface.co/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=self.timeout_seconds,
            )
            if response.status_code != 200:
                if response.status_code in {402, 403}:
                    self._cooldown_until = time.monotonic() + 3600.0
                raise HuggingFaceQwenError(f"qwen_http_{response.status_code}")
            body = response.json()
            raw = str(body["choices"][0]["message"]["content"])
            normalized = _parse_target(raw)
            usage = body.get("usage") if isinstance(body, dict) else {}
            self._prompt_tokens += max(0, int((usage or {}).get("prompt_tokens") or 0))
            self._completion_tokens += max(0, int((usage or {}).get("completion_tokens") or 0))
            self._successes += 1
            self._last_error = None
            self._cooldown_until = 0.0
            return HuggingFaceQwenResult(
                answer=json.dumps(normalized, ensure_ascii=False, separators=(",", ":")),
                route=f"huggingface_qwen:{self.label}",
            )
        except HuggingFaceQwenError as exc:
            self._failures += 1
            self._last_error = _safe_error(exc, self.token)
            if self._cooldown_until <= time.monotonic():
                self._cooldown_until = time.monotonic() + self.cooldown_seconds
            raise
        except Exception as exc:
            self._failures += 1
            self._last_error = _safe_error(exc, self.token)
            self._cooldown_until = time.monotonic() + self.cooldown_seconds
            raise HuggingFaceQwenError(self._last_error) from exc
        finally:
            self._lock.release()

    def health(self) -> dict[str, Any]:
        # Estimativa superior usa o maior preco observado entre os providers
        # habilitados para o 235B: US$0,30/M input e US$1,50/M output.
        estimated = self._prompt_tokens * 0.30 / 1_000_000 + self._completion_tokens * 1.50 / 1_000_000
        return {
            "configured": self.configured,
            "label": self.label,
            "model": self.model if self.configured else None,
            "successes": self._successes,
            "failures": self._failures,
            "cooldown_remaining_seconds": round(max(0.0, self._cooldown_until - time.monotonic()), 1),
            "prompt_tokens": self._prompt_tokens,
            "completion_tokens": self._completion_tokens,
            "estimated_cost_upper_usd": round(estimated, 6),
            "last_error": self._last_error,
            "token_exposed": False,
        }


class HuggingFaceQwenPool:
    def __init__(self, *, tokens: list[tuple[str, str]], **provider_options: Any) -> None:
        self.providers = [
            HuggingFaceQwenProvider(label=label, token=token, **provider_options)
            for label, token in tokens
            if str(token or "").strip()
        ]

    @property
    def configured(self) -> bool:
        return bool(self.providers)

    def query(self, image_path: str | Path, prompt: str) -> HuggingFaceQwenResult:
        if not self.providers:
            raise HuggingFaceQwenError("qwen_pool_not_configured")
        digest = hashlib.sha256(Path(image_path).read_bytes()).digest()
        offset = int.from_bytes(digest[:4], "big") % len(self.providers)
        errors = []
        for provider in self.providers[offset:] + self.providers[:offset]:
            try:
                return provider.query(image_path, prompt)
            except HuggingFaceQwenError as exc:
                errors.append(f"{provider.label}:{_safe_error(exc, provider.token)}")
        raise HuggingFaceQwenError("qwen_pool_failed:" + " | ".join(errors))

    def health(self) -> dict[str, Any]:
        return {
            "configured": self.configured,
            "count": len(self.providers),
            "providers": [provider.health() for provider in self.providers],
            "token_exposed": False,
        }
