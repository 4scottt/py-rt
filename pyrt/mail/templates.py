"""The plain-text bodies of the three notifications (FP M07).

Written fresh: Request Tracker's own templates are GPL and are not read or
copied. Every body ends with the ticket's link, built from ``BASE_URL``
(FP O05), and every message's subject carries the tag (FP M08), so a reply
threads back onto the ticket.
"""

from __future__ import annotations


def _with_link(lines: list[str], link: str, text: str = "") -> str:
    """Join a body: the lines, the link on its own line, then any message."""
    parts = ["\n".join(lines), f"    {link}"]
    if text.strip():
        parts.append(text.strip())
    return "\n\n".join(parts) + "\n"


def created_body(ticket_id: int, subject: str, link: str, text: str = "") -> str:
    """The autoreply: the ticket is open, and where to read it."""
    return _with_link(
        [
            "This is an automatic reply; there is no need to answer it.",
            "",
            f"Your request has been opened as ticket #{ticket_id}.",
            f"Subject: {subject}",
        ],
        link,
        text,
    )


def correspond_body(ticket_id: int, subject: str, link: str, text: str = "") -> str:
    """A reply was added to the ticket."""
    return _with_link(
        [
            f"There is a reply on ticket #{ticket_id}.",
            f"Subject: {subject}",
        ],
        link,
        text,
    )


def resolved_body(ticket_id: int, subject: str, link: str) -> str:
    """The ticket was resolved."""
    return _with_link(
        [
            f"Ticket #{ticket_id} has been resolved.",
            f"Subject: {subject}",
        ],
        link,
    )
