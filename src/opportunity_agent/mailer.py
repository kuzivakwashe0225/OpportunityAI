"""Sending mail - currently the one thing this system says to a new user.

Configured entirely from the environment (SMTP_HOST, SMTP_PORT, SMTP_USER,
SMTP_PASSWORD, SMTP_FROM, SMTP_STARTTLS). Two decisions worth stating:

**Unconfigured means unavailable, not silent.** `send()` raises rather than
pretending to deliver. Registration depends on that: an account whose password
was never delivered is an account nobody can log into, and creating one while
reporting success is worse than refusing.

**Nothing here is logged.** The temporary password passes through this module,
so no exception message, no debug print, and no `repr` of the message body
goes anywhere. The one thing worth logging - that a send happened, to which
address - is left to the caller.
"""

from __future__ import annotations

import os
import smtplib
import ssl
from email.message import EmailMessage


class MailError(Exception):
    """Delivery failed, or mail was never configured. Never contains the body."""


def is_configured() -> bool:
    return bool(os.getenv("SMTP_HOST") and os.getenv("SMTP_FROM"))


def send(*, to: str, subject: str, body: str) -> None:
    host = os.getenv("SMTP_HOST")
    sender = os.getenv("SMTP_FROM")
    if not host or not sender:
        raise MailError(
            "email is not configured (SMTP_HOST and SMTP_FROM). New accounts "
            "are not created until their password can actually be delivered."
        )

    message = EmailMessage()
    message["From"] = sender
    message["To"] = to
    message["Subject"] = subject
    message.set_content(body)

    port = int(os.getenv("SMTP_PORT", "587"))
    username = os.getenv("SMTP_USER")
    password = os.getenv("SMTP_PASSWORD")
    use_starttls = os.getenv("SMTP_STARTTLS", "true").lower() not in ("0", "false", "no")

    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=20, context=ssl.create_default_context()) as smtp:
                if username:
                    smtp.login(username, password or "")
                smtp.send_message(message)
        else:
            with smtplib.SMTP(host, port, timeout=20) as smtp:
                if use_starttls:
                    smtp.starttls(context=ssl.create_default_context())
                if username:
                    smtp.login(username, password or "")
                smtp.send_message(message)
    except (smtplib.SMTPException, OSError) as error:
        # Deliberately reports the error type and not the exception's own text
        # in full - SMTP servers have been known to echo the submitted message
        # back in an error, and this one contains a password.
        raise MailError(f"could not send mail: {type(error).__name__}") from error
