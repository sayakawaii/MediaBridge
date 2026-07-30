"""Reduce arbitrary HTML to the subset AcFun's article storage actually keeps.

This is measured rather than guessed. Across 26 published articles (~120 kB of
stored body HTML, including one 68 kB piece with 1034 paragraphs and 680
images) AcFun's server-side sanitiser only ever preserved::

    p  strong  h1  h2  h3  br  hr  img  span  div

and ``src`` was the *only* attribute that survived on any of them.

The consequence that shapes this module: **no ``<a>`` element survives.** Not
one appeared in the entire sample. Anything richer that we send is dropped
silently, with no error from ``postArticle`` -- so a feed whose value lies in
its citations would arrive stripped of every link, turning a properly credited
repost into an unattributed copy. Links are therefore rendered as visible text
before submission, which is ugly but is the only form that reaches the reader.
"""

from __future__ import annotations

import re
from html import escape
from html.parser import HTMLParser
from urllib.parse import urlparse

#: Tags whose *contents* are dropped wholesale.
DROP_SUBTREE = frozenset(
    {
        "script",
        "style",
        "svg",
        "iframe",
        "noscript",
        "template",
        "head",
        "math",
        "object",
        "embed",
        "video",
        "audio",
        "canvas",
        "form",
        "button",
        "select",
        "textarea",
    }
)

#: Tags that end the current paragraph and start a new one.
BLOCK_TAGS = frozenset(
    {
        "p",
        "div",
        "section",
        "article",
        "header",
        "footer",
        "aside",
        "main",
        "nav",
        "blockquote",
        "pre",
        "figure",
        "figcaption",
        "details",
        "summary",
        "ul",
        "ol",
        "li",
        "dl",
        "dt",
        "dd",
        "table",
        "thead",
        "tbody",
        "tr",
        "td",
        "th",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "hr",
    }
)

_HEADING_TAGS = {"h1": "h2", "h2": "h2", "h3": "h3", "h4": "h3", "h5": "h3", "h6": "h3", "summary": "h3"}

#: `summary` is the clickable label of a <details>; AcFun has no collapsible
#: element, so it becomes an ordinary sub-heading and the body is inlined.
LIST_BULLET = "· "

_WS = re.compile(r"[ \t\r\n\u00a0]+")

#: AcFun also strips emoji from article bodies (only its own 站内表情 are
#: allowed, hence the `supportZtEmot` flag). Removing them here rather than
#: letting the server do it avoids the debris it leaves behind: a heading that
#: begins with an orphaned space, or a bare U+FE0F variation selector left
#: standing where "⭐️" used to be.
_EMOJI = re.compile(
    "["
    "\U0001f000-\U0001faff"  # pictographs, flags, symbols
    "\U00002600-\U000027bf"  # misc symbols and dingbats
    "\U00002b00-\U00002bff"  # arrows and stars, incl. U+2B50 ⭐
    "\U0000fe0e-\U0000fe0f"  # variation selectors
    "\U0000200d"  # zero-width joiner
    "]"
)


def _is_http_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


class _Block:
    __slots__ = ("tag", "parts", "prefix")

    def __init__(self, tag: str, prefix: str = "") -> None:
        self.tag = tag
        self.parts: list[str] = []
        self.prefix = prefix

    def render(self) -> str:
        inner = "".join(self.parts)
        # Collapse runs of whitespace that survive tag removal.
        inner = _WS.sub(" ", inner).strip()
        inner = re.sub(r"(?:<br/>\s*)+$", "", inner).strip()
        if not inner:
            return ""
        if self.prefix:
            inner = escape(self.prefix) + inner
        return f"<{self.tag}>{inner}</{self.tag}>"


class _Flattener(HTMLParser):
    def __init__(self, *, allow_images: bool = True) -> None:
        super().__init__(convert_charrefs=True)
        self._out: list[str] = []
        self._block: _Block | None = None
        self._drop_depth = 0
        self._bold_depth = 0
        self._href_stack: list[tuple[str, int]] = []
        self._allow_images = allow_images

    # -- block plumbing ---------------------------------------------------

    def _flush(self) -> None:
        if self._block is not None:
            rendered = self._block.render()
            if rendered:
                self._out.append(rendered)
            self._block = None

    def _open(self, tag: str, prefix: str = "") -> None:
        self._flush()
        self._block = _Block(tag, prefix)

    def _emit_inline(self, html_fragment: str) -> None:
        if self._block is None:
            self._block = _Block("p")
        self._block.parts.append(html_fragment)

    # -- HTMLParser hooks -------------------------------------------------

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._drop_depth:
            if tag in DROP_SUBTREE:
                self._drop_depth += 1
            return
        if tag in DROP_SUBTREE:
            self._drop_depth = 1
            return

        attributes = {k.lower(): (v or "") for k, v in attrs}

        if tag == "br":
            self._emit_inline("<br/>")
            return
        if tag == "hr":
            self._flush()
            self._out.append("<hr/>")
            return
        if tag == "img":
            self._image(attributes)
            return
        if tag in ("strong", "b"):
            self._bold_depth += 1
            self._emit_inline("<strong>")
            return
        if tag == "a":
            href = attributes.get("href", "").strip()
            self._href_stack.append((href, self._current_len()))
            return
        if tag in _HEADING_TAGS:
            self._open(_HEADING_TAGS[tag])
            return
        if tag == "li":
            self._open("p", LIST_BULLET)
            return
        if tag in BLOCK_TAGS:
            self._open("p")
            return
        # Everything else (span, em, i, code, u, small, ...) contributes text only.

    def handle_endtag(self, tag: str) -> None:
        if self._drop_depth:
            if tag in DROP_SUBTREE:
                self._drop_depth -= 1
            return

        if tag in ("strong", "b"):
            if self._bold_depth:
                self._bold_depth -= 1
                self._emit_inline("</strong>")
            return
        if tag == "a":
            self._close_anchor()
            return
        if tag in BLOCK_TAGS or tag in _HEADING_TAGS:
            self._flush()

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in ("br", "hr", "img"):
            self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        if self._drop_depth or not data:
            return
        text = _WS.sub(" ", _EMOJI.sub("", data))
        if not text.strip() and self._block is None:
            return
        self._emit_inline(escape(text))

    # -- inline helpers ---------------------------------------------------

    def _current_len(self) -> int:
        return len(self._block.parts) if self._block else 0

    def _image(self, attributes: dict[str, str]) -> None:
        src = (attributes.get("src") or attributes.get("data-src") or "").strip()
        if not self._allow_images or not _is_http_url(src):
            return
        self._flush()
        self._out.append(f'<p><img src="{escape(src, quote=True)}"></p>')

    def _close_anchor(self) -> None:
        """Render ``<a href=X>text</a>`` as text that survives sanitisation."""
        if not self._href_stack:
            return
        href, start = self._href_stack.pop()
        if not _is_http_url(href) or self._block is None:
            return

        label_html = "".join(self._block.parts[start:])
        label = _WS.sub(" ", re.sub(r"<[^>]+>", "", label_html)).strip()

        # A bare-URL anchor (`<a href=X>X</a>`) must not become "X (X)".
        if label.rstrip("/") == href.rstrip("/"):
            return
        self._block.parts.append(f" ({escape(href)})" if label else escape(href))

    def close(self) -> None:  # noqa: D102
        super().close()
        self._flush()

    def result(self) -> str:
        return "".join(self._out)


def flatten_for_acfun(html_text: str, *, allow_images: bool = True) -> str:
    """Rewrite `html_text` into the tag subset AcFun keeps.

    Links become ``label (https://example.com)`` because anchors are stripped
    server-side; see the module docstring.
    """
    parser = _Flattener(allow_images=allow_images)
    parser.feed(html_text or "")
    parser.close()
    return parser.result()
