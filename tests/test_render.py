"""The message body renderer (FP T04), pure: no database, no request.

A body is plain text a stranger wrote. It is escaped first, then broken
into the paragraphs it was typed in, then its URLs become links.
"""

from __future__ import annotations

from markupsafe import Markup

from pyrt.tickets.render import render_body


def test_fp_t04_render_escapes_html() -> None:
    """Nothing a writer types can become a tag."""
    out = render_body('<script>alert("x")</script> & "co"')
    assert "<script>" not in out
    assert "&lt;script&gt;" in out
    assert "&amp;" in out


def test_fp_t04_render_splits_paragraphs_and_keeps_line_breaks() -> None:
    """A blank line starts a paragraph; a single newline is a break."""
    out = render_body("first line\nsecond line\n\nnext paragraph")
    assert out.count("<p>") == 2
    assert "first line<br>second line" in out
    assert "<p>next paragraph</p>" in out


def test_fp_t04_render_linkifies_http_urls() -> None:
    """http and https become anchors; other schemes are left as text."""
    out = render_body("see https://example.com/a and http://example.com/b")
    assert '<a href="https://example.com/a">https://example.com/a</a>' in out
    assert '<a href="http://example.com/b">http://example.com/b</a>' in out
    # Another scheme stays text: it is shown, never made clickable.
    assert "<a" not in render_body("javascript:alert(1)")
    assert "<a" not in render_body("ftp://example.com/x")


def test_fp_t04_render_leaves_trailing_punctuation_out_of_the_link() -> None:
    """A sentence's full stop is not part of the URL it follows."""
    out = render_body("go to https://example.com/page, then https://example.com/other.")
    assert '<a href="https://example.com/page">' in out
    assert '<a href="https://example.com/other">' in out
    assert "</a>," in out
    assert "</a>." in out


def test_fp_t04_render_keeps_a_query_string_escaped_but_whole() -> None:
    """An ampersand in a URL survives escaping and stays inside the href."""
    out = render_body("https://example.com/?a=1&b=2")
    assert '<a href="https://example.com/?a=1&amp;b=2">' in out


def test_fp_t04_render_of_nothing_is_nothing() -> None:
    """An empty or blank body renders to an empty Markup, not to "None"."""
    assert render_body("") == Markup("")
    assert render_body("   \n\n  ") == Markup("")


def test_fp_t04_render_returns_markup_so_a_template_does_not_escape_it() -> None:
    """The renderer's own tags must survive Jinja's autoescape."""
    assert isinstance(render_body("hello"), Markup)
