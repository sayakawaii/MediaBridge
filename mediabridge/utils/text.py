"""Text helpers for building AcFun submission fields."""

from __future__ import annotations

import logging
import re

from ..errors import ConfigError

# AcFun's own editors enforce these; exceeding them gets the request rejected
# with an unhelpful generic error, so we clamp client-side.
MAX_TITLE_LEN = 50

#: ``createDouga`` rejects a longer 简介 with ``result=109015 投稿失败：简介不能
#: 超过200个字``, and the check is server-side *after* the whole file has been
#: uploaded -- five over-long descriptions once burned fifty minutes of upload
#: in a single scheduled run. The error message also comes back empty about
#: half the time, so only the code is dependable, which is why this is clamped
#: here rather than diagnosed from the response.
MAX_VIDEO_DESC_LEN = 200

#: ``postArticle`` rejects anything longer with ``result=110014 描述信息不能超过
#: 200个汉字``. Kept separate from the video limit despite the shared value:
#: they are independent server-side checks that have already disagreed once.
MAX_ARTICLE_DESC_LEN = 200
MAX_TAG_LEN = 20
MAX_TAGS = 6

log = logging.getLogger(__name__)

_WHITESPACE = re.compile(r"[ \t\u00a0]+")
_BLANK_LINES = re.compile(r"\n{3,}")
_TAG = re.compile(r"<[^>]+>")
_BLOCK_END = re.compile(r"</(p|div|li|h[1-6]|tr|blockquote)\s*>", re.IGNORECASE)
_BR = re.compile(r"<br\s*/?>", re.IGNORECASE)


def strip_html(html: str) -> str:
    """Flatten HTML to plain text, preserving paragraph breaks."""
    text = _BR.sub("\n", html or "")
    text = _BLOCK_END.sub("\n\n", text)
    text = _TAG.sub("", text)
    for entity, char in (
        ("&nbsp;", " "),
        ("&amp;", "&"),
        ("&lt;", "<"),
        ("&gt;", ">"),
        ("&quot;", '"'),
        ("&#39;", "'"),
    ):
        text = text.replace(entity, char)
    return collapse_whitespace(text)


def collapse_whitespace(text: str) -> str:
    text = _WHITESPACE.sub(" ", text or "")
    return _BLANK_LINES.sub("\n\n", text).strip()


def truncate(text: str, limit: int, ellipsis: str = "…") -> str:
    """Trim to `limit` characters, appending an ellipsis when shortened."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - len(ellipsis))].rstrip() + ellipsis


def render_template(template: str, values: dict[str, object]) -> str:
    """Fill a ``{placeholder}`` template, leaving unknown keys untouched.

    A source that omits an optional field should not crash the run, so missing
    keys render as an empty string rather than raising KeyError.
    """

    class _Defaulting(dict):
        def __missing__(self, key: str) -> str:  # noqa: D105
            return ""

    safe = _Defaulting({k: ("" if v is None else v) for k, v in values.items()})
    try:
        return template.format_map(safe)
    except (ValueError, IndexError):
        # Template contains stray braces; publish the raw text rather than fail.
        return template


def render_within(template: str, values: dict[str, object], limit: int, flex_key: str = "description") -> str:
    """Render `template` to at most `limit` characters, shrinking `flex_key` first.

    Truncating the finished string would cut from the end, which is exactly
    where a repost's attribution lives -- the original author, the source link
    and the licence, i.e. the whole of what makes a 转载 legitimate. So the
    upstream summary gives way instead, and the credit survives.
    """
    rendered = collapse_whitespace(render_template(template, values))
    if len(rendered) <= limit:
        return rendered

    attribution = collapse_whitespace(render_template(template, {**values, flex_key: ""}))
    if len(attribution) > limit:
        raise ConfigError(
            f"desc_template renders to {len(attribution)} characters with no {{{flex_key}}} at all, "
            f"over the {limit} the platform accepts. Shrinking the summary cannot bring it under, "
            "and trimming the end would throw away the attribution.",
            hint="Shorten the fixed text in publish.desc_template.",
        )

    log.warning(
        "Description renders to %d characters, over the %d allowed; shortening the upstream "
        "summary so the %d-character attribution survives.",
        len(rendered),
        limit,
        len(attribution),
    )

    # Shrink by the measured overflow rather than a computed budget: the
    # template's own separators collapse differently once the flexible value is
    # shorter, so the only reliable length is the one we just rendered.
    flex = str(values.get(flex_key) or "")
    for _ in range(4):
        if len(rendered) <= limit:
            return rendered
        flex = truncate(flex, max(limit - (len(rendered) - len(flex)), 0))
        rendered = collapse_whitespace(render_template(template, {**values, flex_key: flex}))

    # Converging on the exact budget is not worth a fifth render; dropping the
    # summary entirely still leaves the credit, which is the part worth keeping.
    return rendered if len(rendered) <= limit else attribution


def normalise_tags(tags: list[str], limit: int = MAX_TAGS) -> list[str]:
    """Deduplicate and clean tags, dropping any AcFun would not accept.

    Over-long tags are discarded rather than truncated: clipping
    "Biological & Physical Sciences" to 20 characters yields a meaningless
    fragment, which is worse than simply not tagging it.
    """
    seen: set[str] = set()
    out: list[str] = []
    for tag in tags:
        cleaned = re.sub(r"[#\s]+", "", str(tag or ""))
        if not cleaned or len(cleaned) > MAX_TAG_LEN or cleaned.lower() in seen:
            continue
        seen.add(cleaned.lower())
        out.append(cleaned)
        if len(out) >= limit:
            break
    return out


def safe_filename(name: str, fallback: str = "media") -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name or "").strip(" .")
    return (cleaned or fallback)[:120]
