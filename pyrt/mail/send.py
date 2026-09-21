"""Outgoing mail: the ``Sender`` interface and its three implementations.

FP M06 and the user's rule of plan §6: **no real mail leaves the container
by default**. ``MAIL_MODE`` unset means ``log`` (one JSON line per message,
nothing leaves the process); ``file`` appends to ``MAIL_FILE``; ``smtp`` is
the only mode that opens a socket, and it is chosen by ``MAIL_MODE=smtp``
alone -- ``SMTP_HOST`` set under any other mode sends nothing.

Plan §6 names no ``From`` address, so one is derived here: the local part
``py-rt`` at ``BASE_URL``'s host, falling back to a slug of ``SITE_NAME``
when there is no host to read (see :func:`from_address`).
"""

from __future__ import annotations

import logging
import re
import smtplib
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path
from typing import Final, Protocol
from urllib.parse import urlsplit

from pyrt.config import Settings, load_settings

log = logging.getLogger(__name__)

#: The header every message carries, so a log line, a file or a relay can be
#: tied back to the ticket without parsing the subject.
TICKET_HEADER: Final = "X-PyRT-Ticket"

#: How much of the body the log sender records (the line stays a line).
LOG_BODY_CHARS: Final = 200

#: The local part of the derived ``From`` address, and its last-resort domain.
FROM_LOCAL_PART: Final = "py-rt"
FROM_FALLBACK_DOMAIN: Final = "py-rt.invalid"

#: How long the SMTP sender waits on the relay.
SMTP_TIMEOUT: Final = 30

_NOT_DOMAIN = re.compile(r"[^a-z0-9.-]+")


def from_address(settings: Settings) -> str:
    """The ``From`` of every message this side sends.

    No ``From`` setting exists in plan §6's environment, so the address is
    derived: ``py-rt@<BASE_URL's host>`` when ``BASE_URL`` is set (the one
    name the deployment already agrees on), else ``py-rt@<SITE_NAME slug>``,
    else ``py-rt@py-rt.invalid``.
    """
    host = urlsplit(settings.base_url).hostname or ""
    domain = _NOT_DOMAIN.sub("-", host.lower()).strip("-.")
    if not domain:
        domain = _NOT_DOMAIN.sub("-", settings.site_name.lower()).strip("-.")
    return f"{FROM_LOCAL_PART}@{domain or FROM_FALLBACK_DOMAIN}"


@dataclass(frozen=True, slots=True)
class Mail:
    """One outgoing message, sender-agnostic.

    ``headers`` carries at least ``X-PyRT-Ticket``; :meth:`for_ticket` is the
    constructor the notifications use.
    """

    to: list[str]
    subject: str
    body: str
    from_address: str
    headers: dict[str, str] = field(default_factory=dict)

    @classmethod
    def for_ticket(
        cls,
        settings: Settings,
        *,
        ticket_id: int,
        to: list[str],
        subject: str,
        body: str,
        headers: dict[str, str] | None = None,
    ) -> Mail:
        """A message about one ticket, its ``X-PyRT-Ticket`` header set."""
        return cls(
            to=list(to),
            subject=subject,
            body=body,
            from_address=from_address(settings),
            headers={TICKET_HEADER: str(ticket_id), **(headers or {})},
        )

    @property
    def ticket_id(self) -> int | None:
        """The ticket this message is about, read back from its header."""
        raw = self.headers.get(TICKET_HEADER, "")
        return int(raw) if raw.isdigit() else None

    def as_message(self) -> EmailMessage:
        """The RFC 5322 form the file and SMTP senders write."""
        message = EmailMessage()
        message["From"] = self.from_address
        message["To"] = ", ".join(self.to)
        message["Subject"] = self.subject
        message["Date"] = formatdate(localtime=False)
        message["Message-ID"] = make_msgid(domain=self.from_address.rpartition("@")[2] or None)
        for name, value in self.headers.items():
            message[name] = value
        message.set_content(self.body)
        return message


class Sender(Protocol):
    """What a mail back end owes the notifications."""

    def send(self, mail: Mail) -> None:
        """Deliver ``mail``, however this back end delivers."""


@dataclass(frozen=True, slots=True)
class LogSender:
    """One JSON log line per message; nothing leaves the process (M06)."""

    def send(self, mail: Mail) -> None:
        log.info(
            "mail",
            extra={
                "to": ", ".join(mail.to),
                "subject": mail.subject,
                "ticket": mail.ticket_id,
                "body": mail.body[:LOG_BODY_CHARS],
            },
        )


@dataclass(frozen=True, slots=True)
class FileSender:
    """Append the message to ``MAIL_FILE``, blank line between messages.

    The demo's ``mail.log``, the same artefact the RT sides leave behind.
    """

    path: Path

    def send(self, mail: Mail) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        text = mail.as_message().as_string().rstrip("\n")
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(f"{text}\n\n")


@dataclass(frozen=True, slots=True)
class SmtpSender:
    """The only sender that opens a socket, and only under ``MAIL_MODE=smtp``.

    STARTTLS when the relay offers it, ``SMTP_USER``/``SMTP_PASSWORD`` when a
    user is set; no authentication and no TLS are required of a local relay.
    """

    host: str
    port: int = 25
    user: str = ""
    password: str = ""
    timeout: int = SMTP_TIMEOUT

    def send(self, mail: Mail) -> None:
        with smtplib.SMTP(self.host, self.port, timeout=self.timeout) as smtp:
            smtp.ehlo()
            if smtp.has_extn("starttls"):
                smtp.starttls()
                smtp.ehlo()
            if self.user:
                smtp.login(self.user, self.password)
            smtp.send_message(mail.as_message())


@dataclass(slots=True)
class RecordingSender:
    """Keeps what it was given; the tests' sender, never wired in the app."""

    sent: list[Mail] = field(default_factory=list)

    def send(self, mail: Mail) -> None:
        self.sent.append(mail)

    @property
    def recipients(self) -> list[str]:
        """Every address every recorded message went to, in order."""
        return [address for mail in self.sent for address in mail.to]


def sender_from_settings(settings: Settings) -> Sender:
    """The sender ``MAIL_MODE`` asks for (M06).

    ``smtp`` without an ``SMTP_HOST`` is a misconfiguration, not a reason to
    guess at localhost: it logs a warning and falls back to the log sender.
    """
    mode = (settings.mail_mode or "log").lower()
    if mode == "file":
        return FileSender(Path(settings.mail_file))
    if mode == "smtp":
        if not settings.smtp_host:
            log.warning("MAIL_MODE=smtp without SMTP_HOST: logging mail instead")
            return LogSender()
        return SmtpSender(
            host=settings.smtp_host,
            port=settings.smtp_port,
            user=settings.smtp_user,
            password=settings.smtp_password,
        )
    return LogSender()


_sender: Sender | None = None


def configure(sender: Sender) -> None:
    """Set the process-wide sender (the app does this once at start)."""
    global _sender
    _sender = sender


def reset() -> None:
    """Forget the configured sender; the next :func:`current` derives one."""
    global _sender
    _sender = None


def current(settings: Settings | None = None) -> Sender:
    """The configured sender, deriving one from settings on first use."""
    global _sender
    if _sender is None:
        _sender = sender_from_settings(settings or load_settings())
    return _sender
