"""The message body renderer (FP T04).

A message body is plain text a stranger wrote: it is escaped first and
never trusted. What comes back is the paragraphs it was typed in, its line
breaks kept, and its ``http(s)`` URLs as links.

Pure, so it is unit-tested without a database or a request.
"""

from __future__ import annotations

import re
from typing import Final

from markupsafe import Markup, escape

#: Blank lines (any run of whitespace-only lines) separate paragraphs.
_PARAGRAPH_BREAK: Final = re.compile(r"(?:\r?\n[ \t]*){2,}")

#: A URL in already-escaped text: it stops at whitespace and at ``<`` (which
#: escaping leaves only as ``&lt;``), so no tag can be swallowed into a href.
_URL: Final = re.compile(r"https?://[^\s<]+")

#: Punctuation a sentence ends with is not part of the URL it follows.
_TRAILING: Final = ".,;:!?'\""


def linkify(escaped: str) -> str:
    """Wrap the URLs of already-escaped text in anchors."""
    return _URL.sub(_anchor, escaped)


def _anchor(match: re.Match[str]) -> str:
    url = match.group(0)
    trailing = ""
    while url and (url[-1] in _TRAILING or (url[-1] == ")" and "(" not in url)):
        trailing = url[-1] + trailing
        url = url[:-1]
    if not url:
        return trailing
    return f'<a href="{url}">{url}</a>{trailing}'


def render_body(body: str) -> Markup:
    """A plain-text message as HTML: escaped, in paragraphs, linkified.

    Blank lines start a new ``<p>``; a single newline inside a paragraph is
    a ``<br>``, because a mail body's wrapping is part of what it says.
    """
    text = (body or "").strip()
    if not text:
        return Markup("")
    parts = []
    for paragraph in _PARAGRAPH_BREAK.split(text):
        if not paragraph.strip():
            continue
        lines = [linkify(str(escape(line))) for line in paragraph.splitlines()]
        parts.append("<p>" + "<br>".join(lines) + "</p>")
    # Every line went through escape() above; only our own tags are added.
    return Markup("".join(parts))
