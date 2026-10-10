"""Local model management: Ollama check/pull and the generic local-LLM server."""

from __future__ import annotations

import re

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from nexus.config import get_settings
from nexus.db import get_connection
from nexus.envstore import (
    reload_settings,
    update_env,
)
from nexus.storage import (
    set_meta,
)
from nexus.web.common import TEMPLATES
from nexus.web.settings_context import _settings_context

router = APIRouter()


@router.post("/settings/ollama/check", response_class=HTMLResponse)
def settings_check_ollama(request: Request) -> HTMLResponse:
    """Ping the local Ollama server and report status in plain language.

    Lets a non-technical user confirm their local model is set up correctly
    without touching a terminal. Talks only to localhost; never leaks anything.
    """
    from nexus.analysis.providers import check_ollama

    settings = get_settings()
    result = check_ollama(settings.ollama_base_url, settings.effective_ollama_model())
    return TEMPLATES.TemplateResponse(
        request, "_ollama_check.html", {"check": result}
    )


_OLLAMA_MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,79}(:[A-Za-z0-9._-]{1,40})?")


@router.post("/settings/ollama/pull", response_class=HTMLResponse)
def settings_ollama_pull(request: Request, model: str = Form("")) -> HTMLResponse:
    """Download a local model from inside the app — no terminal needed.

    Persists the picked model as the active local model (DB meta override, so it
    takes effect with no restart), then kicks off the download on the local
    Ollama server and returns a self-polling progress partial. Talks only to
    localhost; nothing leaves the machine.
    """
    from nexus.analysis.ollama_admin import start_pull

    settings = get_settings()
    model = (model or "").strip() or settings.effective_ollama_model()
    # Free-text names are allowed (any Ollama model), but only in Ollama's tag
    # shape, and never a ":cloud" variant: those run remotely, not on this machine.
    if not _OLLAMA_MODEL_RE.fullmatch(model) or "cloud" in model.lower():
        return TEMPLATES.TemplateResponse(request, "_ollama_pull.html", {"pull": {
            "status": "error", "model": model,
            "error": "That doesn't look like a local Ollama model name "
                     "(for example gemma4:e4b or qwen3.8:27b). Cloud variants are not allowed.",
        }})
    # Remember the choice so analysis uses it immediately (preference, not secret).
    with get_connection() as conn:
        set_meta(conn, "ollama_model", model)
    state = start_pull(settings.ollama_base_url, model)
    return TEMPLATES.TemplateResponse(request, "_ollama_pull.html", {"pull": state})


@router.get("/settings/ollama/pull/status", response_class=HTMLResponse)
def settings_ollama_pull_status(request: Request) -> HTMLResponse:
    """Current download progress — the progress partial polls this while running."""
    from nexus.analysis.ollama_admin import pull_state

    return TEMPLATES.TemplateResponse(
        request, "_ollama_pull.html", {"pull": pull_state()}
    )


# --- Local OpenAI-compatible server (LM Studio, llama.cpp, vLLM, Jan, ...) ---

# Model ids on local servers look like "qwen2.5-7b-instruct",
# "lmstudio-community/Meta-Llama-3.1-8B-Instruct-GGUF" or "model@q4_k_m".
_LOCAL_MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/:@+-]{0,199}")


def _valid_local_base_url(url: str) -> bool:
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and bool(parts.hostname) and not parts.username


@router.post("/settings/local-llm", response_class=HTMLResponse)
def settings_local_llm(
    request: Request, base_url: str = Form(""), model: str = Form("")
) -> HTMLResponse:
    """Save the local server address (.env, not a secret) and model (DB meta).

    The address is the operator's own machine/LAN server, so the public-URL SSRF
    guard used for LibreTranslate deliberately does not apply; it only has to
    be a well-formed http(s) URL.
    """
    settings = get_settings()
    base_url = (base_url or "").strip()
    model = (model or "").strip()
    msg, ok = "", True
    if base_url and not _valid_local_base_url(base_url):
        msg, ok = "Not saved — the server address must look like http://localhost:1234/v1.", False
    elif model and not _LOCAL_MODEL_RE.fullmatch(model):
        msg, ok = "Not saved — that model name contains characters a server would not use.", False
    else:
        if base_url and base_url != settings.local_llm_base_url:
            update_env({"LOCAL_LLM_BASE_URL": base_url})
            reload_settings()
        with get_connection() as conn:
            set_meta(conn, "local_llm_model", model or None)
        msg = "Saved." if model else "Saved. Pick a model so the local server can be used."
    settings = get_settings()
    return TEMPLATES.TemplateResponse(
        request, "_provider_status.html",
        {**_settings_context(settings), "local_msg": msg, "local_ok": ok},
    )


@router.post("/settings/local-llm/check", response_class=HTMLResponse)
def settings_local_llm_check(request: Request) -> HTMLResponse:
    """Ping the local OpenAI-compatible server and list its models. Never raises."""
    from nexus.analysis.providers import check_openai_compatible

    settings = get_settings()
    result = check_openai_compatible(
        settings.local_llm_base_url, settings.effective_local_llm_model(),
        settings.local_llm_api_key,
    )
    return TEMPLATES.TemplateResponse(request, "_local_llm_check.html", {"check": result})
