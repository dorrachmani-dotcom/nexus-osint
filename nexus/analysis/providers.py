"""Provider-agnostic LLM backends for the intelligence core.

The analysis pipeline does not care *which* model produces an assessment — it
only needs text in, JSON-ish text out. This module hides every supported backend
behind one small interface so the rest of the system stays identical whichever
one the analyst picks — Anthropic (Claude), Google (Gemini), OpenAI (ChatGPT),
or a fully local model via Ollama:

  * `LLMProvider.complete(system, user)` -> raw model text.
  * `get_provider(settings)` -> the backend the analyst selected, or None.

Graceful degradation is preserved everywhere: a missing SDK or API key means
`get_provider` returns None and the whole analysis pass becomes a silent no-op.
Secrets are read from Settings (which loads them from .env) — never persisted.
The Ollama backend needs no key at all and keeps analysed content on this
machine (no network calls leave it).
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod

from nexus.config import Settings

logger = logging.getLogger("nexus.analysis")


class LLMProvider(ABC):
    """One text-in / text-out chat backend."""

    name: str = "base"

    def __init__(self, model: str) -> None:
        self.model = model

    @abstractmethod
    def complete(self, system_prompt: str, user_prompt: str, max_tokens: int = 1024) -> str:
        """Return the model's raw text reply (callers parse JSON out of it)."""
        raise NotImplementedError

    def chat(self, system_prompt: str, user_prompt: str, max_tokens: int = 1024) -> str:
        """Free-form prose reply for the interactive assistant.

        The analysis pipeline forces JSON output on some backends; the in-app
        assistant needs ordinary text instead. The base implementation reuses
        ``complete`` (correct for Anthropic/OpenAI, which never force JSON);
        backends that *do* force JSON override this to turn it off.
        """
        return self.complete(system_prompt, user_prompt, max_tokens)


class AnthropicProvider(LLMProvider):
    """Claude via the official Anthropic SDK, with prompt caching on the system
    block (it is stable across a batch, so the cache is reused server-side)."""

    name = "anthropic"

    def __init__(self, api_key: str, model: str) -> None:
        super().__init__(model)
        from anthropic import Anthropic  # guarded by the factory below

        self._client = Anthropic(api_key=api_key)

    def complete(self, system_prompt: str, user_prompt: str, max_tokens: int = 1024) -> str:
        message = self._client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            system=[
                {
                    "type": "text",
                    "text": system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": user_prompt}],
        )
        return "".join(
            block.text
            for block in message.content
            if getattr(block, "type", None) == "text"
        )


class GeminiProvider(LLMProvider):
    """Google Gemini via the google-genai SDK. JSON output is requested via the
    response mime type so replies map cleanly onto our schemas.

    Gemini 2.5+ "flash" models enable an internal *thinking* step that consumes
    the output-token budget BEFORE any visible text is produced — so a modest
    ``max_output_tokens`` can come back empty or truncated. Rather than disable
    thinking (which lowers answer quality), we keep a **bounded** thinking budget
    and add it **on top of** the caller's requested ``max_tokens``, so the visible
    answer always keeps its full room. Older models that don't accept the field
    just get the plain token limit.
    """

    name = "gemini"
    think_budget = 768  # bounded reasoning; added on top of the output budget.

    def __init__(self, api_key: str, model: str) -> None:
        super().__init__(model)
        from google import genai  # guarded by the factory below

        self._genai = genai
        self._client = genai.Client(api_key=api_key)

    def _supports_thinking(self) -> bool:
        """Only 2.5+/3.x models accept thinking_config; older ones 400 on it."""
        m = (self.model or "").lower()
        return ("2.5" in m) or ("gemini-3" in m) or ("-3." in m)

    def _config(self, system_prompt: str, max_tokens: int, *, json_mode: bool) -> dict:
        cfg: dict = {"system_instruction": system_prompt}
        if self._supports_thinking():
            # Keep reasoning, but reserve the requested tokens for the answer by
            # granting the thinking budget as extra headroom — never starves output.
            cfg["thinking_config"] = {"thinking_budget": self.think_budget}
            cfg["max_output_tokens"] = max_tokens + self.think_budget
        else:
            cfg["max_output_tokens"] = max_tokens
        if json_mode:
            cfg["response_mime_type"] = "application/json"
        return cfg

    def complete(self, system_prompt: str, user_prompt: str, max_tokens: int = 1024) -> str:
        response = self._client.models.generate_content(
            model=self.model,
            contents=user_prompt,
            config=self._config(system_prompt, max_tokens, json_mode=True),
        )
        return getattr(response, "text", "") or ""

    def chat(self, system_prompt: str, user_prompt: str, max_tokens: int = 1024) -> str:
        # Plain text (no JSON mime type) for the interactive assistant.
        response = self._client.models.generate_content(
            model=self.model,
            contents=user_prompt,
            config=self._config(system_prompt, max_tokens, json_mode=False),
        )
        return getattr(response, "text", "") or ""


class OpenAIProvider(LLMProvider):
    """OpenAI (ChatGPT) via the official openai SDK. JSON shape is requested in
    the prompt (which sometimes asks for an object and sometimes an array), so we
    do not force response_format — the downstream parser tolerantly extracts it,
    exactly as for the Anthropic backend."""

    name = "openai"

    def __init__(self, api_key: str, model: str) -> None:
        super().__init__(model)
        from openai import OpenAI  # guarded by the factory below

        self._client = OpenAI(api_key=api_key)

    def complete(self, system_prompt: str, user_prompt: str, max_tokens: int = 1024) -> str:
        response = self._client.chat.completions.create(
            model=self.model,
            max_tokens=max_tokens,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )
        return response.choices[0].message.content or ""


class OllamaProvider(LLMProvider):
    """A fully local model via Ollama (https://ollama.com).

    Runs on this machine, so analysed content NEVER leaves it — the privacy-first
    backend for organisations that can't send data to a cloud API. Talks to the
    local Ollama server over HTTP (httpx, already a dependency, so there is no
    extra SDK to install). ``format=json`` asks the model for JSON, matching what
    the downstream parser expects.
    """

    name = "ollama"

    def __init__(self, base_url: str, model: str) -> None:
        super().__init__(model)
        import httpx  # always installed (used by other sources)

        self._httpx = httpx
        self._base_url = base_url.rstrip("/")

    def complete(self, system_prompt: str, user_prompt: str, max_tokens: int = 1024) -> str:
        response = self._httpx.post(
            f"{self._base_url}/api/chat",
            json={
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "stream": False,
                "format": "json",
                "options": {"num_predict": max_tokens},
            },
            # Local generation can be slow on CPU-only machines; allow generous time.
            timeout=180.0,
        )
        response.raise_for_status()
        data = response.json()
        return (data.get("message") or {}).get("content", "") or ""

    def chat(self, system_prompt: str, user_prompt: str, max_tokens: int = 1024) -> str:
        # Same local call but without ``format=json`` so the reply is prose.
        response = self._httpx.post(
            f"{self._base_url}/api/chat",
            json={
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "stream": False,
                "options": {"num_predict": max_tokens},
            },
            timeout=180.0,
        )
        response.raise_for_status()
        data = response.json()
        return (data.get("message") or {}).get("content", "") or ""


def check_ollama(base_url: str, model: str) -> dict:
    """Plain-language health check for the local Ollama server.

    Returns a dict the Settings UI renders for non-technical users:
      - server_up:     could we reach the Ollama server at all?
      - model_present: is the requested model already downloaded?
      - models:         list of model names currently installed (for hints)
      - error:          short human message when something is wrong
    Never raises — a down server is the normal "not set up yet" state.
    """
    result = {
        "server_up": False,
        "model_present": False,
        "models": [],
        "error": "",
        "base_url": base_url,
        "model": model,
    }
    try:
        import httpx

        resp = httpx.get(f"{base_url.rstrip('/')}/api/tags", timeout=4.0)
        resp.raise_for_status()
        result["server_up"] = True
        tags = resp.json().get("models", []) or []
        # Ollama reports names like "llama3.1:latest"; match on the base name too.
        names = [t.get("name", "") for t in tags if t.get("name")]
        result["models"] = names
        wanted = (model or "").strip()
        result["model_present"] = any(
            n == wanted or n.split(":")[0] == wanted.split(":")[0] for n in names
        )
        if not result["model_present"]:
            result["error"] = (
                f'The server is running, but the "{wanted}" model is not '
                f"downloaded yet. Run:  ollama pull {wanted}"
            )
    except Exception:
        result["error"] = (
            "Could not reach the local Ollama server. Make sure Ollama is "
            "installed and running on this machine."
        )
    return result


def get_provider(settings: Settings) -> LLMProvider | None:
    """Construct the analyst-selected provider, or None if unavailable.

    Resolution: `settings.active_provider()` already factors in the dashboard
    override, the env default, and key presence. Here we only have to build the
    client and swallow a missing SDK (so analysis degrades to a no-op instead of
    crashing the scan).
    """
    provider = settings.active_provider()
    if provider == "off":
        return None

    try:
        if provider == "anthropic":
            return AnthropicProvider(settings.anthropic_api_key, settings.claude_model)
        if provider == "gemini":
            return GeminiProvider(settings.gemini_api_key, settings.gemini_model)
        if provider == "openai":
            return OpenAIProvider(settings.openai_api_key, settings.openai_model)
        if provider == "ollama":
            return OllamaProvider(
                settings.ollama_base_url, settings.effective_ollama_model()
            )
    except ImportError:
        logger.warning("%s SDK not installed; skipping analysis.", provider)
        return None
    except Exception:
        logger.exception("Failed to initialise %s provider; skipping analysis.", provider)
        return None
    return None
