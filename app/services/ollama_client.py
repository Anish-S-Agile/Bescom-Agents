"""
Minimal async client for a local Ollama server (mistral), used by the OrchestratorAgent for routing,
field extraction and email drafting.

Talks to Ollama's REST API directly with httpx instead of langchain-ollama,
because the pinned `ollama==0.1.7` package is incompatible with the installed
langchain-ollama.

Environment:
    ORCHESTRATOR_LLM_PROVIDER   "ollama" (default) or "remote" to disable Ollama routing
    ORCHESTRATOR_OLLAMA_MODEL   model name, default "mistral"
    OLLAMA_BASE_URL             default http://localhost:11434
    ORCHESTRATOR_LLM_TIMEOUT    seconds per call, default 30
    ORCHESTRATOR_LLM_COOLDOWN   seconds to skip Ollama after a failure, default 60
"""
import json
import logging
import os
import threading
import time
from typing import Any, Dict, Optional

import httpx

logger = logging.getLogger(__name__)


def _resolve_base_url(url: str) -> str:
    """OLLAMA_BASE_URL may point at localhost (laptop) or a VM; in a container localhost means the Docker host."""
    from urllib.parse import urlsplit

    from app.services.runtime_env import container_host
    url = url.strip().rstrip("/")
    parts = urlsplit(url)
    host = container_host(parts.hostname)
    if host and host != parts.hostname:
        netloc = host + (f":{parts.port}" if parts.port else "")
        return parts._replace(netloc=netloc).geturl()
    return url


class OllamaUnavailable(Exception):
    """Ollama is disabled, cooling down after a failure, or returned unusable output."""


class OllamaClient:
    def __init__(self) -> None:
        self.enabled = os.getenv("ORCHESTRATOR_LLM_PROVIDER", "ollama").lower() == "ollama"
        self.model = os.getenv("ORCHESTRATOR_OLLAMA_MODEL", "mistral")
        self.base_url = _resolve_base_url(os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"))
        self.timeout = float(os.getenv("ORCHESTRATOR_LLM_TIMEOUT", "30"))
        self.cooldown = float(os.getenv("ORCHESTRATOR_LLM_COOLDOWN", "60"))
        self._down_until = 0.0

    @property
    def available(self) -> bool:
        return self.enabled and time.monotonic() >= self._down_until

    def _trip(self) -> None:
        self._down_until = time.monotonic() + self.cooldown

    async def chat_json(self, system: str, user: str, max_tokens: int = 120, timeout: Optional[float] = None,
                        operation: str = "orchestrator_route") -> Dict[str, Any]:
        """One non-streaming chat call that must return a JSON object (Ollama `format: json`)."""
        from app.services.metrics_recorder import metrics

        if not self.available:
            raise OllamaUnavailable("ollama disabled or cooling down")

        started = time.time()
        payload = {
            "model": self.model,
            "stream": False,
            "format": "json",
            "keep_alive": "30m",  # avoid the multi-second cold load on every request
            "options": {"temperature": 0, "num_predict": max_tokens},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        try:
            async with httpx.AsyncClient(timeout=timeout or self.timeout) as client:
                resp = await client.post(f"{self.base_url}/api/chat", json=payload)
                resp.raise_for_status()
                body = resp.json()
            parsed = json.loads(body["message"]["content"])
            if not isinstance(parsed, dict):
                raise ValueError("model did not return a JSON object")
        except Exception as e:  # network, HTTP, timeout, or malformed JSON
            self._trip()
            metrics.track_llm_call(
                model=f"ollama/{self.model}", operation=operation,
                duration=time.time() - started, success=False, error_type=type(e).__name__)
            raise OllamaUnavailable(f"{type(e).__name__}: {e}") from e

        metrics.track_llm_call(
            model=f"ollama/{self.model}", operation=operation,
            input_tokens=int(body.get("prompt_eval_count") or 0),
            output_tokens=int(body.get("eval_count") or 0),
            duration=time.time() - started, success=True)
        return parsed

    async def status(self, max_age: float = 15.0) -> Dict[str, Any]:
        """Is Ollama reachable and is the model pulled? Cached briefly so health checks stay cheap."""
        cached = getattr(self, "_status_cache", None)
        if cached and time.monotonic() - cached[0] < max_age:
            return {**cached[1], "cooldown_seconds": self._cooldown_left()}
        info: Dict[str, Any] = {"provider": "ollama", "base_url": self.base_url, "model": self.model,
                                "enabled": self.enabled, "reachable": False, "model_pulled": False,
                                "installed_models": []}
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                resp = await client.get(f"{self.base_url}/api/tags")
                resp.raise_for_status()
            names = [m.get("name", "") for m in resp.json().get("models", [])]
            info.update(reachable=True, installed_models=names,
                        # "mistral" matches "mistral:latest"; "mistral:7b" must match exactly
                        model_pulled=any(n == self.model or (":" not in self.model and n.split(":")[0] == self.model)
                                         for n in names))
            if not info["model_pulled"]:
                info["hint"] = f"run `ollama pull {self.model}`"
        except Exception as e:
            info["error"] = f"{type(e).__name__}: {e}"[:160]
        self._status_cache = (time.monotonic(), info)
        return {**info, "cooldown_seconds": self._cooldown_left()}

    def _cooldown_left(self) -> int:
        return max(int(self._down_until - time.monotonic()), 0)

    def reset_cooldown(self) -> None:
        self._down_until = 0.0

    def warm_up(self) -> None:
        """Fire-and-forget model load so the first real request isn't a cold start."""
        if not self.enabled:
            return

        def _load() -> None:
            try:
                httpx.post(
                    f"{self.base_url}/api/generate",
                    json={"model": self.model, "prompt": "", "keep_alive": "30m"},
                    timeout=180,
                )
                logger.info(f"🔥 Ollama model '{self.model}' warmed up")
            except Exception as e:
                logger.warning(f"⚠️ Ollama warm-up failed (orchestrator will fall back): {e}")

        threading.Thread(target=_load, name="ollama-warmup", daemon=True).start()


ollama_client = OllamaClient()
