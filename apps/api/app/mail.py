"""Outgoing email: verification links, password resets, and workspace invites.

SMTP when SMTP_HOST is set (smtplib in a thread, so the event loop keeps going). Without it,
dev and test print the message to stderr (links included: you click them from the terminal);
staging and production refuse to start while email sign-up is on
(Settings.production_problems). Tests swap in MemoryMailer through `get_mailer`.

Messages are plain text: no tracking, no remote images. A failed send is logged and raised as
MailError; callers decide whether the request fails.
"""

import asyncio
import logging
import smtplib
import ssl
import sys
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import make_msgid
from typing import Annotated, Protocol
from urllib.parse import quote, urlencode

from fastapi import Depends

from app.config import Settings, get_settings

log = logging.getLogger(__name__)

SMTP_TIMEOUT_SECONDS = 15.0


class MailError(Exception):
    pass


@dataclass(frozen=True)
class Mail:
    to: str
    subject: str
    text: str


class Mailer(Protocol):
    async def send(self, mail: Mail) -> None: ...


class SmtpMailer:
    def __init__(self, settings: Settings) -> None:
        self._s = settings

    def _send_sync(self, mail: Mail) -> None:
        s = self._s
        message = EmailMessage()
        message["From"] = s.email_from
        message["To"] = mail.to
        message["Subject"] = mail.subject
        message["Message-ID"] = make_msgid(domain=s.email_from.rpartition("@")[2].strip("> "))
        message.set_content(mail.text)
        host, port = s.smtp_host or "", s.smtp_port
        context = ssl.create_default_context()
        smtp: smtplib.SMTP
        if s.smtp_security == "ssl":
            smtp = smtplib.SMTP_SSL(host, port, timeout=SMTP_TIMEOUT_SECONDS, context=context)
        else:
            smtp = smtplib.SMTP(host, port, timeout=SMTP_TIMEOUT_SECONDS)
        with smtp:
            if s.smtp_security == "starttls":
                smtp.starttls(context=context)
            if s.smtp_username and s.smtp_password:
                smtp.login(s.smtp_username, s.smtp_password.get_secret_value())
            smtp.send_message(message)

    async def send(self, mail: Mail) -> None:
        try:
            await asyncio.to_thread(self._send_sync, mail)
        except (OSError, smtplib.SMTPException) as exc:
            log.warning("email to %s failed: %s", _mask(mail.to), type(exc).__name__)
            raise MailError(f"Could not send email: {type(exc).__name__}") from None


class LogMailer:
    """No SMTP: dev and test print the whole message to stderr (the log would redact the link's
    token, and the link is what you need there); anywhere else only who and what is logged."""

    def __init__(self, settings: Settings) -> None:
        self._dev = settings.app_env in ("dev", "test")

    async def send(self, mail: Mail) -> None:
        if self._dev:
            banner = f"----- email (not sent: no SMTP_HOST) to {mail.to}: {mail.subject} -----"
            print(f"{banner}\n{mail.text}{'-' * len(banner)}", file=sys.stderr, flush=True)
        else:
            log.warning("email to %s not sent (no SMTP_HOST): %s", _mask(mail.to), mail.subject)


@dataclass
class MemoryMailer:
    """Tests: keeps what was sent."""

    outbox: list[Mail] = field(default_factory=list)

    async def send(self, mail: Mail) -> None:
        self.outbox.append(mail)


def _mask(address: str) -> str:
    local, _, domain = address.partition("@")
    return f"{local[:1]}***@{domain}"


def mailer_for(settings: Settings) -> Mailer:
    if settings.smtp_host:
        return SmtpMailer(settings)
    return LogMailer(settings)


def get_mailer(settings: Annotated[Settings, Depends(get_settings)]) -> Mailer:
    return mailer_for(settings)


# --- messages --------------------------------------------------------------------------------


def _link(settings: Settings, path: str, **params: str) -> str:
    query = urlencode(params, quote_via=quote)
    return f"{settings.web_origin.rstrip('/')}{path}?{query}"


def verification_mail(settings: Settings, to: str, token: str) -> Mail:
    link = _link(settings, "/verify-email", token=token)
    hours = settings.email_verification_hours
    return Mail(
        to=to,
        subject="Confirm your email for Groundcrew",
        text=(
            "Open this link to confirm your email address and finish creating your Groundcrew "
            f"account:\n\n{link}\n\nThe link works once and expires in {hours} hours. If you did "
            "not sign up, ignore this email; no account is created without it.\n"
        ),
    )


def reset_mail(settings: Settings, to: str, token: str) -> Mail:
    link = _link(settings, "/reset-password", token=token)
    minutes = settings.password_reset_minutes
    return Mail(
        to=to,
        subject="Reset your Groundcrew password",
        text=(
            f"Open this link to choose a new password:\n\n{link}\n\nThe link works once and "
            f"expires in {minutes} minutes. Choosing a new password signs you out everywhere. "
            "If you did not ask for this, ignore this email; your password stays the same.\n"
        ),
    )


def account_exists_mail(settings: Settings, to: str) -> Mail:
    signin = _link(settings, "/signin", email=to)
    forgot = _link(settings, "/forgot-password", email=to)
    return Mail(
        to=to,
        subject="You already have a Groundcrew account",
        text=(
            "Someone (probably you) tried to sign up for Groundcrew with this address, but it "
            f"already has an account.\n\nSign in: {signin}\nForgot your password? {forgot}\n\n"
            "If this was not you, ignore this email.\n"
        ),
    )


def invite_mail(settings: Settings, to: str, workspace: str, inviter: str) -> Mail:
    signup = _link(settings, "/signup", email=to)
    return Mail(
        to=to,
        subject=f"{inviter} invited you to {workspace} on Groundcrew",
        text=(
            f'{inviter} added you to the workspace "{workspace}" on Groundcrew.\n\n'
            f"Create your account with this address (or sign in if you have one):\n{signup}\n\n"
            "You join the workspace as soon as this address is confirmed.\n"
        ),
    )
