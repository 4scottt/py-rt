"""Reading one incoming message (FP M05).

The gateway is handed a whole RFC 5322 message as bytes and needs five
things out of it: who wrote it, what it is about, what it says, the
``Message-ID`` that ties a reply to a mail client's thread, and the
headers to keep beside the body. Nothing else: this side files tickets,
it is not a mail store, so attachments, parts it cannot read and every
header it does not name are left where they are (the raw headers are kept
as a string, so nothing is lost that a person could not read back).

The rules, in the order the plan (§10) and the matrix (M05) set them:

* the body is the **first ``text/plain`` part**, the message walked depth
  first, with the part's own charset honoured and undecodable bytes
  replaced rather than refused;
* a message with no plain part at all falls back to the first
  ``text/html`` one, its tags stripped to text;
* a message with neither has an empty body, which is a ticket with no
  content, not a refusal;
* a missing or unreadable ``From`` **is** a refusal (:class:`ParseError`):
  every ticket this gateway files has a requestor, and the address is the
  only thing that names one.

Pure: no session, no settings, no logging. The gateway turns a
:class:`ParseError` into its ``not ok: <reason>`` line.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from email import message_from_bytes, policy
from email.message import EmailMessage, Message
from email.utils import parseaddr
from html.parser import HTMLParser
from typing import Final

#: The two refusals this module raises, verbatim as the ``not ok`` reason.
NO_FROM: Final = "the message has no From address"
BAD_FROM: Final = "the From header is not an address"

#: What the columns hold: ``messages.headers`` is a ``TEXT`` and
#: ``messages.message_id`` a ``VARCHAR(160)``, so a message with a header
#: block or an id longer than that is stored short rather than refused.
HEADERS_LIMIT: Final = 60_000
MESSAGE_ID_LIMIT: Final = 160

#: Tags whose text is never part of the message, and tags that end a line.
DROPPED_TAGS: Final[frozenset[str]] = frozenset({"script", "style", "head", "title"})
BLOCK_TAGS: Final[frozenset[str]] = frozenset(
    {
        "br",
        "p",
        "div",
        "tr",
        "li",
        "ul",
        "ol",
        "table",
        "blockquote",
        "pre",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
    }
)

#: The header/body boundary of a raw message, either line ending.
_HEADER_END: Final = re.compile(rb"\r?\n\r?\n")
_SPACES: Final = re.compile(r"[ \t\r\f\v]+")
_BLANK_LINES: Final = re.compile(r"\n{3,}")


class ParseError(Exception):
    """A message the gateway cannot read. Its text is the refusal's reason."""


@dataclass(frozen=True, slots=True)
class Parsed:
    """What the gateway uses of one message.

    ``from_address`` is an addr-spec, lower-cased (addresses are matched
    without case); ``from_name`` is the display name and is often empty;
    ``headers`` is the raw top-level header block, the body left out.
    """

    from_address: str
    from_name: str
    subject: str
    message_id: str | None
    text: str
    headers: str


def parse_message(raw: bytes) -> Parsed:
    """Read one message, or raise :class:`ParseError` when it has no sender."""
    message: EmailMessage = message_from_bytes(raw, policy=policy.default)
    address, display_name = _sender_of(message)
    return Parsed(
        from_address=address,
        from_name=display_name,
        subject=_header(message, "Subject"),
        message_id=_message_id(message),
        text=body_text(message),
        headers=raw_headers(raw),
    )


# --- the pieces ------------------------------------------------------------


def _sender_of(message: Message[str, str]) -> tuple[str, str]:
    """The ``From`` address and display name, or :class:`ParseError`.

    The parsed header is asked first (it has decoded any RFC 2047 word for
    us); a header too broken to carry addresses falls back to
    :func:`email.utils.parseaddr` over its text, and something that is
    still not an address is the refusal.
    """
    header = message.get("From")
    if header is None:
        raise ParseError(NO_FROM)
    addresses = getattr(header, "addresses", ())
    if addresses:
        display_name = _clean(str(addresses[0].display_name or ""))
        address = _clean(str(addresses[0].addr_spec or ""))
    else:
        display_name, address = (_clean(part) for part in parseaddr(str(header)))
    address = address.strip().lower()
    if not _is_address(address):
        raise ParseError(BAD_FROM)
    return address, display_name.strip()


def _is_address(address: str) -> bool:
    """One ``@`` with something either side of it, and no whitespace."""
    local, separator, domain = address.partition("@")
    return bool(separator and local and domain) and not any(c.isspace() for c in address)


def _header(message: Message[str, str], name: str) -> str:
    """One header decoded to text, ``""`` when the message has none."""
    value = message.get(name)
    return "" if value is None else _clean(str(value)).strip()


def _message_id(message: Message[str, str]) -> str | None:
    """The ``Message-ID``, or None when there is none to keep."""
    value = _header(message, "Message-ID")
    return value[:MESSAGE_ID_LIMIT] if value else None


def raw_headers(raw: bytes) -> str:
    """The top-level header block as it arrived, the body left out."""
    end = _HEADER_END.search(raw)
    head = raw[: end.start()] if end else raw
    return _clean(head.decode("utf-8", errors="replace"))[:HEADERS_LIMIT]


def body_text(message: Message[str, str]) -> str:
    """The message's text: the first plain part, else the HTML one stripped."""
    plain = _first_part(message, "text/plain")
    if plain is not None:
        return _decoded(plain)
    html = _first_part(message, "text/html")
    if html is not None:
        return html_to_text(_decoded(html))
    return ""


def _first_part(message: Message[str, str], content_type: str) -> Message[str, str] | None:
    """The first part of that type, depth first, attachments passed over.

    ``walk`` is the depth-first order the standard library already gives,
    so a ``multipart/alternative`` whose HTML comes first still yields the
    plain part the plan asks for: it is the *type* that decides, not the
    order the sender wrote the parts in.
    """
    for part in message.walk():
        if part.get_content_maintype() == "multipart":
            continue
        if (part.get_content_disposition() or "") == "attachment":
            continue
        if part.get_content_type() == content_type:
            return part
    return None


def _decoded(part: Message[str, str]) -> str:
    """One part's payload as text, its own charset honoured.

    Undecodable bytes are replaced, and a charset Python does not know
    falls back to UTF-8: a message that says it is ``x-broken`` still
    becomes a ticket.
    """
    payload = part.get_payload(decode=True)
    if not isinstance(payload, bytes):  # pragma: no cover - a defective part
        return _clean(str(part.get_payload()))
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


# --- HTML to text ----------------------------------------------------------


class _TextExtractor(HTMLParser):
    """The little stripper of M05: tags out, line breaks where they belong.

    ``convert_charrefs`` (on by default) unescapes the entities as it goes,
    so ``&amp;`` arrives as ``&`` and nothing is unescaped twice.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._dropping = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in DROPPED_TAGS:
            self._dropping += 1
        elif tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in DROPPED_TAGS:
            self._dropping = max(0, self._dropping - 1)
        elif tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._dropping:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    """An HTML body as the plain text a ticket's history shows.

    Not a renderer: the tags go, the block ones leave a line break behind,
    runs of spaces collapse and three blank lines become one. A person
    reading the ticket sees what the sender wrote, in the order they wrote
    it, and nothing of the markup.
    """
    extractor = _TextExtractor()
    extractor.feed(html)
    extractor.close()
    lines = [_SPACES.sub(" ", line).strip() for line in "".join(extractor.parts).split("\n")]
    return _BLANK_LINES.sub("\n\n", "\n".join(lines)).strip()


def _clean(value: str) -> str:
    """Text the database can store: surrogates from raw bytes replaced.

    The bytes parser keeps an undecodable header byte as a surrogate, and a
    surrogate cannot be written to a utf8mb4 column. One round trip through
    UTF-8 turns each into ``?``.
    """
    return value.encode("utf-8", errors="replace").decode("utf-8", errors="replace")
