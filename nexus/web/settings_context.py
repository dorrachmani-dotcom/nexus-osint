"""Context builders shared by the settings, LLM and notification routers:
the secret-field catalogue (labels/help only, never values), the settings page
context, archive settings and the email wizard context.
"""

from __future__ import annotations

import logging

from nexus import wayback
from nexus.analysis.ollama_admin import RECOMMENDED_MODELS as OLLAMA_RECOMMENDED
from nexus.db import get_connection
from nexus.mailer import (
    PROVIDERS as EMAIL_PROVIDERS,
)
from nexus.storage import (
    get_meta,
)

logger = logging.getLogger("nexus")


def _archive_settings_page_ctx(settings) -> dict:
    try:
        with get_connection() as conn:
            return _archive_settings_ctx(conn, settings)
    except Exception:
        logger.exception("Could not read archive settings")
        return _archive_settings_ctx(None, settings)


def _archive_settings_ctx(conn, settings, **extra) -> dict:
    ctx = {
        "archive_enabled": wayback.is_enabled(conn) if conn is not None else True,
        "archive_acked": wayback.opsec_acknowledged(conn) if conn is not None else False,
        "archive_keys_set": wayback.keys_configured(settings),
        "archive_saved": False,
    }
    ctx.update(extra)
    return ctx


# --- Settings / onboarding --------------------------------------------------

# Secret variables the analyst can set from the dashboard. Each maps to one
# allow-listed key in nexus/envstore.py. The actual values are written to .env
# only (never the DB) and are never rendered back — the UI shows Set / Not set.
SECRET_FIELDS: list[dict] = [
    {"key": "ANTHROPIC_API_KEY", "label": "Anthropic (Claude) API key",
     "hint": "Powers AI summaries, translation and threat scoring.",
     "url": "https://console.anthropic.com/settings/keys",
     "steps": [
         "Open console.anthropic.com and sign in (or create an account).",
         "Add a payment method under Billing — usage is pay-as-you-go.",
         "Go to Settings → API keys and click 'Create Key'.",
         "Copy the key (starts with sk-ant-) and paste it here.",
     ]},
    {"key": "GEMINI_API_KEY", "label": "Google (Gemini) API key",
     "hint": "Free alternative AI provider for analysis.",
     "url": "https://aistudio.google.com/app/apikey",
     "steps": [
         "Open aistudio.google.com and sign in with a Google account.",
         "Click 'Get API key' → 'Create API key'.",
         "Copy the generated key and paste it here.",
     ]},
    {"key": "OPENAI_API_KEY", "label": "OpenAI (ChatGPT) API key",
     "hint": "Alternative AI provider for analysis (GPT models).",
     "url": "https://platform.openai.com/api-keys",
     "steps": [
         "Open platform.openai.com and sign in (or create an account).",
         "Add a payment method under Billing — usage is pay-as-you-go.",
         "Go to API keys and click 'Create new secret key'.",
         "Copy the key (starts with sk-) and paste it here.",
     ]},
    {"key": "XAI_API_KEY", "label": "xAI (Grok) API key",
     "hint": "Alternative cloud AI provider for analysis (Grok models).",
     "url": "https://console.x.ai",
     "steps": [
         "Open console.x.ai and sign in (or create an account).",
         "Add credits or a payment method under Billing — usage is pay-as-you-go.",
         "Open 'API Keys' and click 'Create API Key'.",
         "Copy the key (starts with xai-) and paste it here.",
         "Then pick 'xAI (Grok)' (or 'Automatic') as the AI provider above.",
     ]},
    {"key": "LOCAL_LLM_API_KEY", "label": "Local AI server key (optional)",
     "hint": "Only if your local OpenAI-compatible server requires a key. LM Studio does not.",
     "url": "",
     "steps": [
         "Most local servers (LM Studio, llama.cpp, Jan) need no key — leave this empty.",
         "If you started vLLM or LocalAI with an --api-key option, paste that same value here.",
     ]},
    {"key": "SERPAPI_KEY", "label": "SERPAPI key (Google News)",
     "hint": "Lets capsule terms search Google News.",
     "url": "https://serpapi.com/manage-api-key",
     "steps": [
         "Sign up at serpapi.com (the free plan allows 100 searches/month).",
         "Open the 'Api Key' page from your dashboard.",
         "Copy your private API key and paste it here.",
         "Then add a capsule on the Topics page to give it something to search.",
     ]},
    {"key": "GOOGLE_CSE_KEY", "label": "Google Custom Search API key",
     "hint": "Searches the wider indexed web for capsule terms (free: 100/day).",
     "url": "https://developers.google.com/custom-search/v1/introduction",
     "steps": [
         "Open the link and click 'Get a Key' to create/select a Google Cloud project.",
         "Copy the generated API key and paste it here.",
         "Then set the Search Engine ID (cx) below to finish enabling it.",
     ]},
    {"key": "GOOGLE_CSE_CX", "label": "Google Custom Search engine ID (cx)",
     "hint": "The Programmable Search Engine the API key queries.",
     "url": "https://programmablesearchengine.google.com/controlpanel/all",
     "steps": [
         "Open the link and click 'Add' to create a search engine.",
         "Choose 'Search the entire web' so it isn't limited to one site.",
         "Open the engine, copy the 'Search engine ID', and paste it here.",
     ]},
    {"key": "REDDIT_CLIENT_ID", "label": "Reddit client ID",
     "hint": "First half of Reddit API credentials.",
     "url": "https://www.reddit.com/prefs/apps",
     "steps": [
         "Open reddit.com/prefs/apps while logged in.",
         "Click 'create another app...' at the bottom.",
         "Choose type 'script', set redirect URI to http://localhost:8000.",
         "The client ID is the short string just under the app name — paste it here.",
     ]},
    {"key": "REDDIT_CLIENT_SECRET", "label": "Reddit client secret",
     "hint": "Second half of Reddit API credentials.",
     "url": "https://www.reddit.com/prefs/apps",
     "steps": [
         "On the same reddit.com/prefs/apps page, open your script app.",
         "Copy the value labelled 'secret' and paste it here.",
     ]},
    {"key": "TWITTER_BEARER_TOKEN", "label": "Twitter / X bearer token",
     "hint": "Lets capsule terms search Twitter/X.",
     "url": "https://developer.twitter.com/en/portal/dashboard",
     "steps": [
         "Apply for access at developer.twitter.com and create a Project + App.",
         "Open your App → 'Keys and tokens'.",
         "Under 'Bearer Token' click Generate, then copy it.",
         "Paste the bearer token here.",
     ]},
    {"key": "TELEMETRY_API_KEY", "label": "Telegram (Telemetry) key",
     "hint": "Reads public Telegram channels and search via telemetryapp.io.",
     "url": "https://www.telemetryapp.io",
     "steps": [
         "Open Telegram and start a chat with @telemetrio_api_bot.",
         "Follow the bot's prompts to generate an access key.",
         "Copy the key and paste it here (it is sent as an 'api_key' header).",
         "Then add channels or a capsule on the Topics page.",
     ]},
    {"key": "INSTAGRAM_SESSIONID", "label": "Instagram session ID (Toutatis)",
     "hint": "Enables the Toutatis Instagram tool. Use a throwaway account.",
     "url": "https://www.instagram.com",
     "steps": [
         "Log in to Instagram in your browser (prefer a burner account).",
         "Open the browser dev tools (F12) → Application/Storage → Cookies.",
         "Find the cookie named 'sessionid' and copy its value.",
         "Paste it here. Note: it expires when you log out of that session.",
     ]},
    {"key": "ARCHIVE_ORG_ACCESS_KEY", "label": "Internet Archive access key (optional)",
     "hint": "Archiving works without it; with both keys captures use the more reliable Save Page Now 2 API.",
     "url": "https://archive.org/account/s3.php",
     "steps": [
         "Sign in at archive.org (a free account; consider a dedicated one).",
         "Open archive.org/account/s3.php to see your S3-like API keys.",
         "Copy the 'access key' and paste it here, then the secret key below.",
     ]},
    {"key": "ARCHIVE_ORG_SECRET_KEY", "label": "Internet Archive secret key (optional)",
     "hint": "The second half of the archive.org API key pair.",
     "url": "https://archive.org/account/s3.php",
     "steps": [
         "On the same archive.org/account/s3.php page, copy the 'secret key'.",
         "Paste it here. It stays in the local .env file only.",
     ]},
    # --- Email (daily brief). Rendered by the "Connect your email" wizard, not
    # the generic key list (group "email"), but kept here so set/not-set status
    # and the .env-only handling are identical to every other secret.
    {"key": "SMTP_PASSWORD", "label": "Email password / app password", "group": "email",
     "hint": "For Gmail this must be an App Password, not your normal password.",
     "url": "https://myaccount.google.com/apppasswords",
     "steps": [
         "Gmail: turn on 2-Step Verification at myaccount.google.com/security (required for app passwords).",
         "Open myaccount.google.com/apppasswords, type a name such as 'Nexus brief' and click Create.",
         "Copy the 16-character password Google shows (spaces don't matter) and paste it here.",
         "Outlook / Microsoft 365: use your account password, or an app password if your account has 2-step sign-in.",
         "Other SMTP servers: use the password your mail provider gives for SMTP sign-in.",
     ]},
    {"key": "GOOGLE_OAUTH_CLIENT_SECRET", "label": "Google OAuth client secret", "group": "email",
     "hint": "From your own Google Cloud OAuth client (type: Desktop app).",
     "url": "https://console.cloud.google.com/apis/credentials",
     "steps": []},
    {"key": "GOOGLE_OAUTH_REFRESH_TOKEN", "label": "Gmail connection", "group": "email",
     "hint": "Written by “Connect Gmail”; never typed or shown.", "steps": []},
    {"key": "RESEND_API_KEY", "label": "Resend API key", "group": "email",
     "hint": "Sends the brief through resend.com (free tier available).",
     "url": "https://resend.com/api-keys",
     "steps": [
         "Sign up at resend.com and add + verify a sending domain under 'Domains'.",
         "Open 'API Keys', click 'Create API Key' with 'Sending access'.",
         "Copy the key (starts with re_) and paste it here.",
         "Use a From address on your verified domain.",
     ]},
    {"key": "SENDGRID_API_KEY", "label": "SendGrid API key", "group": "email",
     "hint": "Sends the brief through SendGrid (Twilio).",
     "url": "https://app.sendgrid.com/settings/api_keys",
     "steps": [
         "Sign up at sendgrid.com and verify a Single Sender or a domain under 'Sender Authentication'.",
         "Open Settings → API Keys → 'Create API Key' with 'Mail Send' permission.",
         "Copy the key (starts with SG.) and paste it here.",
         "Use the verified sender as the From address.",
     ]},
]


def _secret_status(settings) -> dict[str, bool]:
    """Per-key 'is a value set?' map — booleans only, never the secret itself."""
    return {f["key"]: bool(getattr(settings, f["key"].lower(), None)) for f in SECRET_FIELDS}


def _settings_context(settings) -> dict:
    return {
        "status": settings.availability_report(),
        "claude_model": settings.claude_model,
        "gemini_model": settings.gemini_model,
        "translation_target_lang": settings.translation_target_lang,
        "rss_feeds": settings.rss_feed_list,
        "max_items": settings.analysis_max_items_per_run,
        "provider_choices": settings.PROVIDER_CHOICES,
        "chosen_provider": settings.chosen_provider(),
        "active_provider": settings.active_provider(),
        "claude_enabled": settings.claude_enabled,
        "gemini_enabled": settings.gemini_enabled,
        "openai_enabled": settings.openai_enabled,
        "openai_model": settings.openai_model,
        "ollama_enabled": settings.ollama_enabled,
        "ollama_model": settings.effective_ollama_model(),
        "ollama_base_url": settings.ollama_base_url,
        "ollama_recommended": OLLAMA_RECOMMENDED,
        "grok_enabled": settings.grok_enabled,
        "grok_model": settings.grok_model,
        "local_openai_enabled": settings.local_openai_enabled,
        "local_llm_model": settings.effective_local_llm_model(),
        "local_llm_base_url": settings.local_llm_base_url,
        "local_msg": "",
        "local_ok": True,
        "secret_fields": SECRET_FIELDS,
        "secret_status": _secret_status(settings),
        # Keyless English-translation fallback. The URL is NOT a secret, so unlike
        # the API keys it is shown back so the operator can see/edit it.
        "libretranslate_url": settings.libretranslate_url or "",
        "libretranslate_enabled": settings.libretranslate_enabled,
        "saved": False,
        "translation_saved": False,
        "translation_msg": "",
    }


# Plain (non-secret) email fields the wizard writes and shows back.
_EMAIL_PLAIN_FIELDS = (
    "SMTP_HOST", "SMTP_PORT", "SMTP_USERNAME", "SMTP_FROM", "DIGEST_TO", "GOOGLE_OAUTH_CLIENT_ID",
)


_EMAIL_SECRET_FIELDS = (
    "SMTP_PASSWORD", "RESEND_API_KEY", "SENDGRID_API_KEY", "GOOGLE_OAUTH_CLIENT_SECRET",
)


_MAX_RECIPIENTS = 10


def _email_context(settings, **extra) -> dict:
    """Everything the email wizard + digest box render. Never includes a secret."""
    from nexus.mailer import is_verified, resolve_email_config

    cfg = resolve_email_config(settings)
    with get_connection() as conn:
        verified = is_verified(conn, cfg)
        digest_enabled = get_meta(conn, "digest_enabled") == "1" and verified
        digest_time = get_meta(conn, "digest_time") or "08:00"
        scan_first = get_meta(conn, "digest_scan_first") == "1"
        last_sent = get_meta(conn, "last_digest_sent_at") or ""
        last_status = get_meta(conn, "last_digest_status") or ""
    ctx = {
        "email_providers": EMAIL_PROVIDERS,
        "email_provider": cfg.provider,
        "email_label": cfg.label,
        "email_kind": cfg.kind,
        "email_host": cfg.host,
        "email_port": cfg.port,
        "email_missing": cfg.missing,
        "email_ready": cfg.ready,
        "email_verified": verified,
        "smtp_host": settings.smtp_host or "",
        "smtp_port": settings.smtp_port or "587",
        "smtp_username": settings.smtp_username or "",
        "smtp_from": settings.smtp_from or "",
        "digest_to": settings.digest_to or "",
        "google_client_id": settings.google_oauth_client_id or "",
        "google_connected": bool(settings.google_oauth_refresh_token),
        "google_email": settings.google_oauth_email or "",
        "digest_enabled": digest_enabled,
        "digest_time": digest_time,
        "digest_scan_first": scan_first,
        "last_digest_sent_at": last_sent[:16].replace("T", " "),
        "last_digest_status": last_status,
        "email_msg": "",
        "email_ok": True,
        "digest_msg": "",
        "digest_ok": True,
    }
    ctx.update(extra)
    return ctx
