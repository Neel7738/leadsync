"""
Dynamic LLM Routing Manager with Autonomous Local Fallback.

Routes between cloud providers (NIM, OpenAI, Anthropic, Google, Groq)
and local models via Ollama. Dynamically detects available local models
and falls back through the chain automatically.

Priority chain:
  1. Preferred/configured cloud provider
  2. Alternative cloud providers (with API keys)
  3. Local Ollama models (ordered by intelligence then speed)

Features:
  - Automatic health checking of Ollama
  - Dynamic model discovery from Ollama
  - Cost tracking per request
  - Latency monitoring
  - Automatic provider demotion on repeated failures
"""

import os
import time
import logging
from typing import List, Dict, Any, Optional
from dataclasses import dataclass, field

try:
    import httpx
except ImportError:
    httpx = None  # type: ignore

from ..config import get_settings

logger = logging.getLogger("LLMManager")


@dataclass
class LLMResponse:
    """Result of an LLM completion call."""
    content: str
    provider: str
    model: str
    latency: float
    tokens_used: Optional[int] = None
    cost_usd: Optional[float] = None
    cached: bool = False


@dataclass
class ProviderHealth:
    """Track health status of a provider."""
    name: str
    consecutive_failures: int = 0
    last_success: Optional[float] = None
    last_failure: Optional[float] = None
    total_calls: int = 0
    total_failures: int = 0
    avg_latency: float = 0.0
    _latencies: List[float] = field(default_factory=list)

    def record_success(self, latency: float) -> None:
        self.consecutive_failures = 0
        self.last_success = time.time()
        self.total_calls += 1
        self._latencies.append(latency)
        if len(self._latencies) > 100:
            self._latencies = self._latencies[-100:]
        self.avg_latency = sum(self._latencies) / len(self._latencies)

    def record_failure(self) -> None:
        self.consecutive_failures += 1
        self.last_failure = time.time()
        self.total_calls += 1
        self.total_failures += 1

    @property
    def success_rate(self) -> float:
        if self.total_calls == 0:
            return 1.0
        return 1.0 - (self.total_failures / self.total_calls)

    @property
    def is_healthy(self) -> bool:
        """Provider is considered unhealthy after 3+ consecutive failures."""
        return self.consecutive_failures < 3


class LLMManager:
    """
    Handles dynamic routing and fallback for LLM calls.

    Priority: Configured Cloud Provider -> Alternative Cloud -> Local Ollama.
    Automatically detects which Ollama models are available and uses them
    in order of intelligence (larger models first).
    """

    # Ollama models ordered by intelligence (best first, fastest last)
    OLLAMA_MODEL_CASCADE = [
        "llama3.1:8b",
        "llama3.2:3b",
        "llama3.2:1b",
        "phi3:latest",
        "gemma2:2b",
        "mistral:latest",
        "qwen2.5:3b",
        "qwen2.5:1.5b",
        "tinyllama:latest",
    ]

    # Approximate cost per 1K tokens (USD) — local = 0
    COST_TABLE = {
        "gpt-4o": {"input": 0.005, "output": 0.015},
        "gpt-4o-mini": {"input": 0.00015, "output": 0.0006},
        "gpt-3.5-turbo": {"input": 0.001, "output": 0.002},
        "claude-3-opus": {"input": 0.015, "output": 0.075},
        "claude-3-sonnet": {"input": 0.003, "output": 0.015},
        "claude-3-haiku": {"input": 0.00025, "output": 0.00125},
        "meta/llama-3.1-70b-instruct": {"input": 0.00063, "output": 0.00063},
        "meta/llama-3.1-405b-instruct": {"input": 0.0027, "output": 0.0027},
        "gemini-1.5-flash": {"input": 0.000075, "output": 0.0003},
        "gemini-1.5-pro": {"input": 0.00125, "output": 0.005},
        "llama-3.1-8b-instant": {"input": 0.00005, "output": 0.00008},
    }

    def __init__(self):
        self.settings = get_settings()
        self.timeout = httpx.Timeout(60.0, connect=10.0) if httpx else None
        self._health: Dict[str, ProviderHealth] = {}
        self._available_ollama_models: Optional[List[str]] = None
        self._ollama_checked_at: float = 0

    def get_health_report(self) -> Dict[str, Any]:
        """Return health metrics for all providers."""
        report = {}
        for name, h in self._health.items():
            report[name] = {
                "consecutive_failures": h.consecutive_failures,
                "total_calls": h.total_calls,
                "success_rate": round(h.success_rate, 4),
                "avg_latency": round(h.avg_latency, 3),
                "is_healthy": h.is_healthy,
            }
        return report

    def generate(
        self,
        prompt: str,
        system_message: str = "You are a professional sales intelligence agent.",
        temperature: float = 0.7,
        max_tokens: int = 1000,
        preferred_provider: Optional[str] = None,
    ) -> LLMResponse:
        """
        Route completion request through the provider hierarchy.
        Tries each provider in order; falls back to local Ollama.
        Raises RuntimeError only if ALL providers fail.
        """
        providers = self._get_provider_hierarchy(preferred_provider)

        last_error = None
        for provider_cfg in providers:
            name = provider_cfg["name"]
            if name not in self._health:
                self._health[name] = ProviderHealth(name=name)

            health = self._health[name]
            if not health.is_healthy:
                logger.debug(f"Skipping unhealthy provider: {name} ({health.consecutive_failures} failures)")
                continue

            try:
                logger.info(f"Trying LLM: {name} ({provider_cfg.get('model', 'local')})")
                result = self._call_provider(provider_cfg, prompt, system_message, temperature, max_tokens)
                if result:
                    health.record_success(result.latency)
                    return result
            except Exception as e:
                health.record_failure()
                last_error = e
                logger.warning(f"Provider {name} failed: {e}")
                continue

        # Last resort: try unhealthy providers too (things may have recovered)
        for provider_cfg in providers:
            name = provider_cfg["name"]
            try:
                logger.info(f"Retry (unhealthy): {name}")
                result = self._call_provider(provider_cfg, prompt, system_message, temperature, max_tokens)
                if result:
                    self._health[name].record_success(result.latency)
                    return result
            except Exception as e:
                self._health[name].record_failure()
                last_error = e
                continue

        raise RuntimeError(
            f"All LLM providers failed. Last error: {last_error}"
        )

    def is_local_available(self) -> bool:
        """Check if Ollama is running with at least one model."""
        try:
            models = self._get_ollama_models()
            return len(models) > 0
        except Exception:
            return False

    def _get_provider_hierarchy(self, preferred: Optional[str]) -> List[Dict[str, Any]]:
        """Build the provider routing order."""
        primary = preferred or self.settings.llm_provider
        hierarchy: List[Dict[str, Any]] = []

        # Cloud layer
        if primary == "nim":
            hierarchy.append(self._make_nim_cfg())
        elif primary == "openai":
            hierarchy.append(self._make_openai_cfg())
        elif primary == "anthropic":
            hierarchy.append(self._make_anthropic_cfg())
        elif primary == "google":
            hierarchy.append(self._make_google_cfg())
        elif primary == "groq":
            hierarchy.append(self._make_groq_cfg())

        # Add other cloud providers as fallbacks
        seen = {h["name"] for h in hierarchy}
        for cfg in [
            self._make_openai_cfg(),
            self._make_anthropic_cfg(),
            self._make_nim_cfg(),
            self._make_google_cfg(),
            self._make_groq_cfg(),
        ]:
            if cfg["name"] not in seen:
                hierarchy.append(cfg)
                seen.add(cfg["name"])

        # Local Ollama as final fallback
        hierarchy.append(self._make_ollama_cfg())

        return hierarchy

    def _make_openai_cfg(self) -> Dict[str, Any]:
        return {
            "name": "openai",
            "type": "openai_compat",
            "url": "https://api.openai.com/v1",
            "key": self.settings.openai_api_key or os.environ.get("OPENAI_API_KEY"),
            "model": self.settings.llm_model_generation,
        }

    def _make_anthropic_cfg(self) -> Dict[str, Any]:
        return {
            "name": "anthropic",
            "type": "anthropic",
            "url": "https://api.anthropic.com/v1",
            "key": self.settings.anthropic_api_key or os.environ.get("ANTHROPIC_API_KEY"),
            "model": "claude-3-haiku-20240307",
        }

    def _make_nim_cfg(self) -> Dict[str, Any]:
        return {
            "name": "nvidia-nim",
            "type": "openai_compat",
            "url": "https://integrate.api.nvidia.com/v1",
            "key": self.settings.nvidia_api_key or os.environ.get("NVIDIA_API_KEY"),
            "model": "meta/llama-3.1-70b-instruct",
        }

    def _make_google_cfg(self) -> Dict[str, Any]:
        return {
            "name": "google",
            "type": "google",
            "url": "https://generativelanguage.googleapis.com/v1beta",
            "key": self.settings.google_api_key or os.environ.get("GOOGLE_API_KEY"),
            "model": "gemini-1.5-flash",
        }

    def _make_groq_cfg(self) -> Dict[str, Any]:
        return {
            "name": "groq",
            "type": "openai_compat",
            "url": "https://api.groq.com/openai/v1",
            "key": self.settings.groq_api_key or os.environ.get("GROQ_API_KEY"),
            "model": "llama-3.1-8b-instant",
        }

    def _make_ollama_cfg(self) -> Dict[str, Any]:
        return {
            "name": "ollama",
            "type": "local",
            "url": self.settings.ollama_host,
            "models": self._get_ollama_models(),
        }

    def _get_ollama_models(self) -> List[str]:
        """Query Ollama for available models, ordered by intelligence."""
        now = time.time()
        # Cache for 60 seconds
        if self._available_ollama_models is not None and (now - self._ollama_checked_at) < 60:
            return self._available_ollama_models

        try:
            if not httpx:
                self._available_ollama_models = []
                return []
            resp = httpx.get(
                f"{self.settings.ollama_host}/api/tags",
                timeout=5.0,
            )
            if resp.status_code == 200:
                data = resp.json()
                available = [m["name"] for m in data.get("models", [])]
                # Sort: prefer our cascade order, unknown models go last
                cascade = self.OLLAMA_MODEL_CASCADE
                ordered = []
                for m in cascade:
                    # Match by prefix (e.g. "llama3.1:8b" matches "llama3.1:8b-instruct-q4_0")
                    if any(avail.startswith(m.split(":")[0]) and (m.split(":")[1] in avail if ":" in m else True) for avail in available):
                        ordered.append(m)
                for av in available:
                    if av not in ordered:
                        ordered.append(av)
                self._available_ollama_models = ordered
                self._ollama_checked_at = now
                logger.info(f"Ollama models available: {ordered}")
                return ordered
        except Exception as e:
            logger.debug(f"Ollama not reachable: {e}")

        self._available_ollama_models = []
        self._ollama_checked_at = now
        return []

    def _call_provider(
        self,
        cfg: Dict[str, Any],
        prompt: str,
        system: str,
        temp: float,
        max_t: int,
    ) -> Optional[LLMResponse]:
        """Call a single provider. Returns None or raises."""
        if not httpx:
            raise RuntimeError("httpx not installed")

        start = time.time()
        ptype = cfg["type"]

        if ptype == "openai_compat":
            key = cfg.get("key")
            if not key:
                return None  # Skip — no key configured
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.post(
                    f"{cfg['url']}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": cfg["model"],
                        "messages": [
                            {"role": "system", "content": system},
                            {"role": "user", "content": prompt},
                        ],
                        "temperature": temp,
                        "max_tokens": max_t,
                    },
                )
                if resp.status_code == 200:
                    data = resp.json()
                    latency = time.time() - start
                    content = data["choices"][0]["message"]["content"].strip()
                    tokens = data.get("usage", {}).get("total_tokens")
                    cost = self._estimate_cost(cfg["model"], tokens)
                    return LLMResponse(
                        content=content,
                        provider=cfg["name"],
                        model=cfg["model"],
                        latency=latency,
                        tokens_used=tokens,
                        cost_usd=cost,
                    )
                else:
                    raise RuntimeError(f"{cfg['name']} returned {resp.status_code}: {resp.text[:200]}")

        elif ptype == "anthropic":
            key = cfg.get("key")
            if not key:
                return None
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.post(
                    f"{cfg['url']}/messages",
                    headers={
                        "x-api-key": key,
                        "Content-Type": "application/json",
                        "anthropic-version": "2023-06-01",
                    },
                    json={
                        "model": cfg["model"],
                        "max_tokens": max_t,
                        "system": system,
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": temp,
                    },
                )
                if resp.status_code == 200:
                    data = resp.json()
                    latency = time.time() - start
                    content = data["content"][0]["text"].strip()
                    tokens = data.get("usage", {})
                    total_tokens = (tokens.get("input_tokens", 0) or 0) + (tokens.get("output_tokens", 0) or 0)
                    cost = self._estimate_cost(cfg["model"], total_tokens)
                    return LLMResponse(
                        content=content,
                        provider="anthropic",
                        model=cfg["model"],
                        latency=latency,
                        tokens_used=total_tokens or None,
                        cost_usd=cost,
                    )
                else:
                    raise RuntimeError(f"anthropic returned {resp.status_code}: {resp.text[:200]}")

        elif ptype == "google":
            key = cfg.get("key")
            if not key:
                return None
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.post(
                    f"{cfg['url']}/models/{cfg['model']}:generateContent?key={key}",
                    headers={"Content-Type": "application/json"},
                    json={
                        "contents": [{"parts": [{"text": prompt}]}],
                        "systemInstruction": {"parts": [{"text": system}]},
                        "generationConfig": {"temperature": temp, "maxOutputTokens": max_t},
                    },
                )
                if resp.status_code == 200:
                    data = resp.json()
                    latency = time.time() - start
                    candidates = data.get("candidates", [])
                    if candidates:
                        content = candidates[0]["content"]["parts"][0]["text"].strip()
                        return LLMResponse(
                            content=content,
                            provider="google",
                            model=cfg["model"],
                            latency=latency,
                        )
                    return None
                else:
                    raise RuntimeError(f"google returned {resp.status_code}: {resp.text[:200]}")

        elif ptype == "local":
            return self._call_ollama(cfg, prompt, system, temp, max_t)

        return None

    def _call_ollama(
        self,
        cfg: Dict[str, Any],
        prompt: str,
        system: str,
        temp: float,
        max_t: int,
    ) -> Optional[LLMResponse]:
        """Call Ollama with model cascade."""
        models = cfg.get("models", [])
        if not models:
            return None

        url = cfg["url"]
        start = time.time()

        for model in models:
            try:
                with httpx.Client(timeout=httpx.Timeout(30.0, connect=5.0)) as client:
                    resp = client.post(
                        f"{url}/api/chat",
                        json={
                            "model": model,
                            "messages": [
                                {"role": "system", "content": system},
                                {"role": "user", "content": prompt},
                            ],
                            "stream": False,
                            "options": {
                                "temperature": temp,
                                "num_predict": max_t,
                            },
                        },
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        content = data.get("message", {}).get("content", "").strip()
                        if content:
                            latency = time.time() - start
                            return LLMResponse(
                                content=content,
                                provider="ollama",
                                model=model,
                                latency=latency,
                                tokens_used=data.get("eval_count"),
                                cost_usd=0.0,
                            )
            except Exception as e:
                logger.debug(f"Ollama model {model} failed: {e}")
                continue

        return None

    def _estimate_cost(self, model: str, total_tokens: Optional[int]) -> Optional[float]:
        """Estimate cost in USD based on model and token count."""
        if total_tokens is None:
            return None
        costs = self.COST_TABLE.get(model)
        if not costs:
            return None
        # Assume 50/50 input/output split for estimation
        input_cost = (total_tokens / 2) * costs["input"] / 1000
        output_cost = (total_tokens / 2) * costs["output"] / 1000
        return round(input_cost + output_cost, 6)


# Singleton
llm_manager = LLMManager()
