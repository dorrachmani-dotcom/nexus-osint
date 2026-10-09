"""Outbound email for the daily brief — SMTP or an email-API service.

This is the ONLY place the app sends email, and it only ever runs when the
operator has explicitly connected an account in Settings. Supported:

  * ``gmail``    — smtp.gmail.com:587 (STARTTLS) with a Google App Password.
  * ``outlook``  — smtp.office365.com:587 (STARTTLS) for Outlook / Microsoft 365.
  * ``smtp``     — any SMTP server: SSL on port 465, STARTTLS on 587 / 25.
  * ``resend``   — https://api.resend.com/emails with a Bearer API key.
  * ``sendgrid`` — https://api.sendgrid.com/v3/mail/send with a Bearer API key.

There are deliberately no OAuth flows (they would need a client secret, which
cannot live in a public repo); app passwords and API keys only.

Rules kept here:
  * Secrets (SMTP password / API key) come from Settings, i.e. ``.env`` only.
  * The password, the API key and the message body are never logged.
  * ``send_email`` never raises: every failure becomes a short plain-English
    message for the UI, so a mail problem can never break a request or a scan.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import re
import smtplib
import socket
import ssl
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parseaddr

logger = logging.getLogger("nexus.mailer")

SMTP_TIMEOUT = 20.0
API_TIMEOUT = 20.0
# Attachments above this are linked instead of attached (most providers cap
# messages around 10-25 MB, and a daily brief should stay light).
MAX_ATTACHMENT_BYTES = 8 * 1024 * 1024

PROVIDERS: dict[str, dict] = {
    "gmail": {"label": "Gmail", "kind": "smtp", "host": "smtp.gmail.com", "port": 587},
    "outlook": {
        "label": "Outlook / Microsoft 365", "kind": "smtp",
        "host": "smtp.office365.com", "port": 587,
    },
    "resend": {"label": "Resend", "kind": "api", "api_url": "https://api.resend.com/emails"},
    "sendgrid": {
        "label": "SendGrid", "kind": "api",
        "api_url": "https://api.sendgrid.com/v3/mail/send",
    },
    "smtp": {"label": "Custom SMTP server", "kind": "smtp"},
}

# .env keys that make up the email connection. Saving any of them from the UI
# clears the "verified" flag so the digest needs a fresh successful test.
EMAIL_ENV_KEYS: frozenset[str] = frozenset(
    {
        "SMTP_HOST", "SMTP_PORT", "SMTP_USERNAME", "SMTP_PASSWORD", "SMTP_FROM",
        "DIGEST_TO", "RESEND_API_KEY", "SENDGRID_API_KEY",
    }
)

_ADDR_RE = re.compile(r"^[^@\s<>,;\"']+@[^@\s<>,;\"']+\.[^@\s<>,;\"']+$")


def valid_address(value: str) -> bool:
    """Loose sanity check for one plain email address (no display name)."""
    return bool(_ADDR_RE.match((value or "").strip()))


def _clean_header(value: str) -> str:
    """Strip CR/LF so a value can never inject extra mail headers."""
    return re.sub(r"[\r\n]+", " ", value or "").strip()


@dataclass
class EmailConfig:
    """The resolved, ready-to-use email connection (secrets included in memory
    only — never rendered, logged or stored outside .env)."""

    provider: str = ""
    label: str = ""
    kind: str = ""  # "smtp" | "api" | ""
    host: str = ""
    port: int = 587
    username: str = ""
    password: str = field(default="", repr=False)
    api_key: str = field(default="", repr=False)
    api_url: str = ""
    sender: str = ""
    recipients: list[str] = field(default_factory=list)

    @property
    def missing(self) -> list[str]:
        """Human labels of the settings still needed for this provider."""
        if not self.provider:
            return ["an email provider"]
        need: list[str] = []
        if self.kind == "smtp":
            if not self.host:
                need.append("SMTP server (SMTP_HOST)")
            if self.provider in ("gmail", "outlook"):
                if not self.username:
                    need.append("your email address (SMTP_USERNAME)")
                if not self.password:
                    need.append(
                        "app password (SMTP_PASSWORD)" if self.provider == "gmail"
                        else "password (SMTP_PASSWORD)"
                    )
            elif self.username and not self.password:
                need.append("SMTP password (SMTP_PASSWORD)")
        else:
            if not self.api_key:
                need.append(f"{self.label} API key")
        if not valid_address(self.sender):
            need.append("a valid From address (SMTP_FROM)")
        if not self.recipients:
            need.append("at least one recipient (DIGEST_TO)")
        return need

    @property
    def ready(self) -> bool:
        return not self.missing

    def fingerprint(self) -> str:
        """Identifies the current connection WITHOUT containing any secret.

        Stored as the "verified" marker after a successful test. Only whether a
        password/key is present goes in, never its value or a hash of it.
        """
        parts = [
            self.provider, self.host, str(self.port), self.username.lower(),
            self.sender.lower(), ",".join(r.lower() for r in self.recipients),
            "pw" if self.password else "", "key" if self.api_key else "",
        ]
        return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:32]


def _infer_provider(settings) -> str:
    if (getattr(settings, "smtp_host", "") or "").strip():
        host = settings.smtp_host.strip().lower()
        if host == "smtp.gmail.com":
            return "gmail"
        if host in ("smtp.office365.com", "smtp-mail.outlook.com"):
            return "outlook"
        return "smtp"
    if getattr(settings, "resend_api_key", None):
        return "resend"
    if getattr(settings, "sendgrid_api_key", None):
        return "sendgrid"
    return ""


def chosen_email_provider(settings) -> str:
    """Dashboard pick (DB meta) > EMAIL_PROVIDER in .env > inferred from keys."""
    from nexus.storage import get_meta_value

    for raw in (get_meta_value("email_provider", None), getattr(settings, "email_provider", "")):
        value = (raw or "").strip().lower()
        if value in PROVIDERS:
            return value
    return _infer_provider(settings)


def resolve_email_config(settings) -> EmailConfig:
    """Build the EmailConfig from Settings + the dashboard provider pick. Never raises."""
    try:
        provider = chosen_email_provider(settings)
    except Exception:
        provider = ""
    if provider not in PROVIDERS:
        return EmailConfig()
    spec = PROVIDERS[provider]
    username = (getattr(settings, "smtp_username", "") or "").strip()
    password = getattr(settings, "smtp_password", None) or ""
    if provider == "gmail":
        # Google shows app passwords as "abcd efgh ijkl mnop"; spaces are cosmetic.
        password = password.replace(" ", "")
    sender = (getattr(settings, "smtp_from", "") or "").strip()
    sender = parseaddr(sender)[1] if sender else ""
    if not sender and spec["kind"] == "smtp":
        sender = username
    recipients = [
        r.strip() for r in (getattr(settings, "digest_to", "") or "").split(",")
        if valid_address(r.strip())
    ]
    cfg = EmailConfig(
        provider=provider, label=spec["label"], kind=spec["kind"],
        username=username, sender=sender, recipients=recipients,
    )
    if spec["kind"] == "smtp":
        if provider == "smtp":
            cfg.host = (getattr(settings, "smtp_host", "") or "").strip()
            try:
                cfg.port = int(str(getattr(settings, "smtp_port", "") or "587").strip())
            except (TypeError, ValueError):
                cfg.port = 587
            if not 0 < cfg.port < 65536:
                cfg.port = 587
        else:
            cfg.host = spec["host"]
            cfg.port = spec["port"]
        cfg.password = password
    else:
        cfg.api_url = spec["api_url"]
        key_attr = "resend_api_key" if provider == "resend" else "sendgrid_api_key"
        cfg.api_key = (getattr(settings, key_attr, None) or "").strip()
    return cfg


# --------------------------------------------------------------------- sending


@dataclass
class SendResult:
    ok: bool
    message: str


Attachment = tuple[str, bytes, str]  # (filename, content, mime type)


def _build_message(
    cfg: EmailConfig, subject: str, text: str, html: str | None,
    attachments: list[Attachment],
) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = _clean_header(subject)
    msg["From"] = _clean_header(cfg.sender)
    msg["To"] = ", ".join(_clean_header(r) for r in cfg.recipients)
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=(cfg.sender.split("@")[-1] or "localhost"))
    msg.set_content(text or "")
    if html:
        msg.add_alternative(html, subtype="html")
    for filename, content, mime in attachments:
        maintype, _, subtype = (mime or "application/octet-stream").partition("/")
        msg.add_attachment(
            content, maintype=maintype, subtype=subtype or "octet-stream",
            filename=_clean_header(filename),
        )
    return msg


def _send_smtp(cfg: EmailConfig, msg: EmailMessage) -> SendResult:
    where = f"{cfg.host}:{cfg.port}"
    context = ssl.create_default_context()
    try:
        if cfg.port == 465:
            server = smtplib.SMTP_SSL(cfg.host, cfg.port, timeout=SMTP_TIMEOUT, context=context)
        else:
            server = smtplib.SMTP(cfg.host, cfg.port, timeout=SMTP_TIMEOUT)
        try:
            server.ehlo()
            if cfg.port != 465:
                if server.has_extn("starttls"):
                    server.starttls(context=context)
                    server.ehlo()
                elif cfg.port == 587 or cfg.username:
                    # Never send a password over an unencrypted connection.
                    return SendResult(False, (
                        f"The mail server at {where} does not offer an encrypted "
                        "connection (STARTTLS), so nothing was sent. Use port 465 "
                        "(SSL) or 587 (STARTTLS)."
                    ))
            if cfg.username and cfg.password:
                server.login(cfg.username, cfg.password)
            server.send_message(msg)
        finally:
            try:
                server.quit()
            except Exception:
                pass
    except smtplib.SMTPAuthenticationError:
        hint = (
            " For Gmail you must use an App Password (not your normal password); "
            "see the steps in Settings."
            if cfg.provider == "gmail" else
            " Check the username and password. Some Microsoft 365 organisations "
            "turn off SMTP sign-in; if so, use an email API service instead."
            if cfg.provider == "outlook" else ""
        )
        return SendResult(False, "The mail server rejected the username or password." + hint)
    except smtplib.SMTPRecipientsRefused:
        return SendResult(False, "The mail server refused the recipient address(es). Check DIGEST_TO.")
    except smtplib.SMTPSenderRefused:
        return SendResult(False, (
            "The mail server refused the From address. It usually has to be the "
            "same account you sign in with."
        ))
    except (socket.timeout, TimeoutError):
        return SendResult(False, f"Timed out connecting to the mail server at {where}.")
    except ssl.SSLError:
        return SendResult(False, (
            f"Could not set up a secure connection with {where}. Check the port: "
            "465 uses SSL, 587 uses STARTTLS."
        ))
    except (smtplib.SMTPException, OSError) as exc:
        # Exception class only — SMTP replies can echo parts of the message.
        return SendResult(False, (
            f"Could not send through the mail server at {where} "
            f"({type(exc).__name__}). Check the server address and port."
        ))
    except Exception as exc:  # pragma: no cover - defensive
        return SendResult(False, f"Unexpected email error ({type(exc).__name__}).")
    return SendResult(True, f"Sent via {cfg.label} to {len(cfg.recipients)} recipient(s).")


def _api_error_reason(resp, secret: str) -> str:
    """Short, secret-free reason text from an email-API error response."""
    reason = ""
    try:
        data = resp.json()
        if isinstance(data, dict):
            reason = str(data.get("message") or "")
            errors = data.get("errors")
            if not reason and isinstance(errors, list) and errors:
                first = errors[0]
                reason = str(first.get("message") if isinstance(first, dict) else first)
    except Exception:
        reason = ""
    if secret:
        reason = reason.replace(secret, "[key]")
    return _clean_header(reason)[:200]


def _send_api(
    cfg: EmailConfig, subject: str, text: str, html: str | None,
    attachments: list[Attachment],
) -> SendResult:
    import httpx

    subject = _clean_header(subject)
    encoded = [
        (name, base64.b64encode(content).decode("ascii"), mime)
        for name, content, mime in attachments
    ]
    if cfg.provider == "resend":
        payload: dict = {
            "from": cfg.sender, "to": cfg.recipients, "subject": subject, "text": text or "",
        }
        if html:
            payload["html"] = html
        if encoded:
            payload["attachments"] = [{"filename": n, "content": c} for n, c, _m in encoded]
    else:  # sendgrid
        content = [{"type": "text/plain", "value": text or " "}]
        if html:
            content.append({"type": "text/html", "value": html})
        payload = {
            "personalizations": [{"to": [{"email": r} for r in cfg.recipients]}],
            "from": {"email": cfg.sender},
            "subject": subject,
            "content": content,
        }
        if encoded:
            payload["attachments"] = [
                {"content": c, "filename": n, "type": m, "disposition": "attachment"}
                for n, c, m in encoded
            ]
    try:
        resp = httpx.post(
            cfg.api_url, json=payload, timeout=API_TIMEOUT,
            headers={"Authorization": f"Bearer {cfg.api_key}"},
        )
    except Exception as exc:
        return SendResult(False, (
            f"Could not reach {cfg.label} ({type(exc).__name__}). Check your internet connection."
        ))
    if resp.status_code in (401, 403):
        return SendResult(False, (
            f"{cfg.label} rejected the API key (HTTP {resp.status_code}). Check the key "
            "and that it is allowed to send email."
        ))
    if resp.status_code >= 400:
        reason = _api_error_reason(resp, cfg.api_key)
        tail = f": {reason}" if reason else ""
        hint = (
            " The From address must be on a domain you verified with the service."
            if resp.status_code in (400, 403, 422) else ""
        )
        return SendResult(False, f"{cfg.label} refused the message (HTTP {resp.status_code}){tail}.{hint}")
    return SendResult(True, f"Sent via {cfg.label} to {len(cfg.recipients)} recipient(s).")


def send_email(
    settings_or_cfg,
    subject: str,
    text: str,
    html: str | None = None,
    attachments: list[Attachment] | None = None,
) -> SendResult:
    """Send one multipart (plain text + HTML) message. Never raises.

    Accepts either Settings or a resolved EmailConfig. Oversized attachments
    are dropped (the caller links them instead); the result message says so.
    """
    try:
        cfg = (
            settings_or_cfg if isinstance(settings_or_cfg, EmailConfig)
            else resolve_email_config(settings_or_cfg)
        )
        if not cfg.ready:
            return SendResult(False, "Email is not set up yet — missing: " + ", ".join(cfg.missing) + ".")
        kept: list[Attachment] = []
        total = 0
        dropped = 0
        for att in attachments or []:
            size = len(att[1])
            if total + size > MAX_ATTACHMENT_BYTES:
                dropped += 1
                continue
            kept.append(att)
            total += size
        if cfg.kind == "smtp":
            result = _send_smtp(cfg, _build_message(cfg, subject, text, html, kept))
        else:
            result = _send_api(cfg, subject, text, html, kept)
        if result.ok:
            logger.info("Email sent via %s to %d recipient(s).", cfg.label, len(cfg.recipients))
            if dropped:
                result.message += f" {dropped} attachment(s) were too large and were left out."
        else:
            logger.warning("Email via %s not sent: %s", cfg.label, result.message)
        return result
    except Exception as exc:  # pragma: no cover - last-resort guard
        logger.warning("Email send failed unexpectedly (%s).", type(exc).__name__)
        return SendResult(False, f"Unexpected email error ({type(exc).__name__}).")


# ------------------------------------------------------------ verified marker


def is_verified(conn, cfg: EmailConfig) -> bool:
    """True if the CURRENT connection passed a test send (non-secret DB meta)."""
    from nexus.storage import get_meta

    return bool(cfg.ready) and get_meta(conn, "email_verified") == cfg.fingerprint()


def mark_verified(conn, cfg: EmailConfig) -> None:
    from nexus.storage import set_meta

    set_meta(conn, "email_verified", cfg.fingerprint())


def clear_verified(conn) -> None:
    """Forget the verified state and switch the digest off until re-tested."""
    from nexus.storage import set_meta

    set_meta(conn, "email_verified", None)
    set_meta(conn, "digest_enabled", "0")
