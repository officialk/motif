"""Outgoing email (invites and password resets) over SMTP.

Configured from MOTIF_SMTP_* (see app/config.py). Sending never raises: callers get
False and show the link to the admin instead. Logs carry no passwords or links.
"""

from __future__ import annotations

import html
import logging
import smtplib
import ssl
import threading
from email.message import EmailMessage

from app import config

log = logging.getLogger(__name__)

TIMEOUT = 15.0  # seconds for the whole send, connect included


def configured() -> bool:
    return bool(config.SMTP_HOST and (config.SMTP_FROM or config.SMTP_USER))


def _deliver(msg: EmailMessage) -> None:
    context = ssl.create_default_context()
    if config.SMTP_TLS == "ssl":
        smtp: smtplib.SMTP = smtplib.SMTP_SSL(config.SMTP_HOST, config.SMTP_PORT, timeout=TIMEOUT, context=context)
    else:
        smtp = smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=TIMEOUT)
    with smtp:
        if config.SMTP_TLS == "starttls":
            smtp.starttls(context=context)
        if config.SMTP_USER:
            smtp.login(config.SMTP_USER, config.SMTP_PASSWORD or "")
        smtp.send_message(msg)


def send(to: str, subject: str, text: str, html_body: str) -> bool:
    """Send one message in a worker thread; False on any failure or after TIMEOUT."""
    if not configured():
        return False
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = config.SMTP_FROM or config.SMTP_USER
    msg["To"] = to
    msg.set_content(text)
    msg.add_alternative(html_body, subtype="html")
    result: dict = {}

    def run() -> None:
        try:
            _deliver(msg)
            result["ok"] = True
        except Exception as e:  # noqa: BLE001 - any SMTP/socket error means "not sent"
            result["error"] = type(e).__name__

    worker = threading.Thread(target=run, name="mailer", daemon=True)
    worker.start()
    worker.join(TIMEOUT + 5)
    if result.get("ok"):
        return True
    log.warning("could not send %r via %s:%s (%s)", subject, config.SMTP_HOST, config.SMTP_PORT,
                result.get("error", "timed out"))
    return False


def _page(heading: str, lead: str, link: str, button: str, note: str) -> str:
    h, l, n = html.escape(heading), html.escape(lead), html.escape(note)
    url = html.escape(link, quote=True)
    return (f'<div style="font-family:sans-serif;max-width:480px;margin:auto;color:#222">'
            f"<h2>{h}</h2><p>{l}</p>"
            f'<p><a href="{url}" style="display:inline-block;padding:10px 20px;background:#1db954;color:#000;'
            f'border-radius:20px;text-decoration:none;font-weight:bold">{html.escape(button)}</a></p>'
            f'<p style="font-size:12px;color:#666">{n}<br>{url}</p></div>')


def send_invite(to: str, link: str) -> bool:
    text = ("You've been invited to Motif, a generative radio.\n\n"
            f"Set your password to join:\n{link}\n\nThe link works once and expires in 48 hours.\n")
    return send(to, "You're invited to Motif", text,
                _page("You've been invited to Motif", "Set your password to start listening.", link,
                      "Set password", "The link works once and expires in 48 hours."))


def send_reset(to: str, link: str) -> bool:
    text = (f"A password reset was requested for your Motif account.\n\nSet a new password:\n{link}\n\n"
            "The link works once and expires in 1 hour. Ignore this email if you didn't expect it.\n")
    return send(to, "Reset your Motif password", text,
                _page("Reset your password", "Choose a new password for your Motif account.", link,
                      "Reset password", "The link works once and expires in 1 hour."))
