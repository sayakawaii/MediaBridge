from __future__ import annotations

import re

from mediabridge.utils.acfun_html import flatten_for_acfun

#: The complete set AcFun's sanitiser was observed to keep. Anything outside it
#: is discarded server-side without an error, so the flattener must never emit
#: it. See mediabridge/utils/acfun_html.py for how this was measured.
ALLOWED_TAGS = {"p", "strong", "h1", "h2", "h3", "br", "hr", "img", "span", "div"}


def tags_in(html: str) -> set[str]:
    return {t.lower() for t in re.findall(r"<\s*/?\s*([a-zA-Z][a-zA-Z0-9]*)", html)}


def test_output_never_leaves_the_allowed_tag_set():
    messy = """
    <section class="cat cat-ai"><h2>Heading</h2>
      <details data-score="9.0"><summary><span>Item</span></summary>
        <blockquote>quoted</blockquote>
        <ul><li>one</li><li>two</li></ul>
        <table><tr><td>cell</td></tr></table>
        <em>emphasis</em> <code>code()</code>
      </details>
    </section>"""
    out = flatten_for_acfun(messy)
    assert tags_in(out) <= ALLOWED_TAGS


def test_anchors_become_visible_urls():
    # AcFun strips <a> entirely, so a link that stays an anchor is a link the
    # reader never sees -- which would silently break attribution.
    out = flatten_for_acfun('<p>See <a href="https://example.com/x">the source</a> now</p>')
    assert "<a" not in out
    assert "the source (https://example.com/x)" in out


def test_bare_url_anchor_is_not_duplicated():
    out = flatten_for_acfun('<p><a href="https://example.com/x">https://example.com/x</a></p>')
    assert out.count("https://example.com/x") == 1


def test_non_http_hrefs_are_dropped_but_text_kept():
    out = flatten_for_acfun('<p><a href="javascript:alert(1)">click</a></p>')
    assert "javascript" not in out
    assert "click" in out


def test_scripts_and_styles_lose_their_contents():
    out = flatten_for_acfun("<p>keep</p><script>var stolen = 1;</script><style>p{color:red}</style>")
    assert "stolen" not in out
    assert "color:red" not in out
    assert "keep" in out


def test_list_items_become_bulleted_paragraphs():
    out = flatten_for_acfun("<ul><li>alpha</li><li>beta</li></ul>")
    assert out == "<p>· alpha</p><p>· beta</p>"


def test_headings_collapse_to_the_two_levels_acfun_keeps():
    out = flatten_for_acfun("<h1>A</h1><h4>B</h4><h6>C</h6>")
    assert out == "<h2>A</h2><h3>B</h3><h3>C</h3>"


def test_images_keep_only_an_absolute_src():
    out = flatten_for_acfun('<p><img src="https://cdn.example.com/a.png" class="x" width="9"></p>')
    assert out == '<p><img src="https://cdn.example.com/a.png"></p>'


def test_relative_images_are_dropped_since_acfun_cannot_resolve_them():
    assert flatten_for_acfun('<p><img src="/assets/a.png"></p>') == ""


def test_images_can_be_disabled():
    assert flatten_for_acfun('<img src="https://e.com/a.png">', allow_images=False) == ""


def test_text_is_escaped():
    out = flatten_for_acfun("<p>a &lt; b &amp; c</p>")
    assert "&lt;" in out and "&amp;" in out
    assert "a < b" not in out


def test_empty_blocks_are_not_emitted():
    assert flatten_for_acfun("<p></p><div>   </div><p><span></span></p>") == ""


def test_emoji_are_removed_along_with_their_leftovers():
    # AcFun deletes emoji server-side but keeps what surrounds them, so leaving
    # "⭐️ 9.0" to the server yields " 9.0" preceded by an orphaned U+FE0F.
    out = flatten_for_acfun("<h2>🔬 科技</h2><p>⭐️ 9.0/10</p><p>🔗 来源</p>")
    assert out == "<h2>科技</h2><p>9.0/10</p><p>来源</p>"
    assert "\ufe0f" not in out


def test_a_block_of_only_emoji_disappears_entirely():
    assert flatten_for_acfun("<p>🎉🎉🎉</p>") == ""


def test_bold_survives_as_strong():
    out = flatten_for_acfun("<p><b>bold</b> and <strong>strong</strong></p>")
    assert out == "<p><strong>bold</strong> and <strong>strong</strong></p>"
