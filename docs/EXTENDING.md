# Writing a source

A source discovers candidate items and normalises them into `MediaItem`. It never downloads media, never publishes, and never learns which platform will consume its output — that decoupling is what lets you add a source without touching anything else.

Built-in sources are registered through the very same entry-point group that third-party packages use, so a plugin you write is not a second-class citizen.

## The interface

```python
from mediabridge.models import FETCH_DIRECT, MediaItem
from mediabridge.sources.base import Source, SourceOptions


class MyOptions(SourceOptions):
    feed_url: str
    limit: int = 5


class MySource(Source):
    type_name = "mysource"
    options_model = MyOptions
    description = "One line shown by `mediabridge sources`."

    options: MyOptions

    def discover(self) -> list[MediaItem]:
        payload = self.get_json(self.options.feed_url)
        return [
            self.make_item(
                id=str(entry["id"]),           # must be stable across runs
                title=entry["title"],
                webpage_url=entry["link"],
                description=entry.get("summary", ""),
                author=entry.get("author", ""),
                license="CC BY 4.0",
                license_url="https://creativecommons.org/licenses/by/4.0/",
                duration=entry.get("seconds"),
                thumbnail_url=entry.get("image", ""),
                filesize_approx=entry.get("bytes"),
                download_url=entry["media"],
                fetch_strategy=FETCH_DIRECT,
            )
            for entry in payload["items"][: self.options.limit]
        ]
```

Options are validated by pydantic, so a typo in `config.yaml` produces a precise error before anything runs, and `get_json` gives you retries and consistent error wrapping for free.

## Rules that matter

**`id` must be stable.** It is half of the dedup key (`{source_name}:{id}`). If it changes between runs, the item is republished. Use the upstream's own identifier, never an array index or a timestamp.

**Filter licences yourself.** The orchestrator does not second-guess a source. Use `mediabridge.filters.licenses`:

```python
from mediabridge.filters import licenses

allowed = licenses.parse_allowlist(self.options.license_allow)
if not licenses.is_allowed(name, url, allowed):
    continue
```

`identify()` returns an empty string for anything it does not recognise, and `is_allowed()` treats that as "not permitted". Keep it that way — an unrecognised licence is not a licence.

**Report a size when you can.** `filesize_approx` is what protects the runner's disk before a download starts. If the upstream offers several renditions, consult `self.max_bytes` (injected from the global limits) and pick one that fits, as `archive_org` does.

## Choosing a fetch strategy

| Strategy | Use when | Requires |
| --- | --- | --- |
| `FETCH_YTDLP` | The site is a video platform yt-dlp understands | `download_url` may be a yt-dlp pseudo-URL, e.g. `peertube:host:uuid`; falls back to `webpage_url` |
| `FETCH_DIRECT` | You already know the exact media file URL | `download_url` |
| `FETCH_NONE` | Article-style items carrying their payload inline | `body_html` |

Non-MP4 containers are transcoded automatically, and a cover frame is extracted if the source supplies no thumbnail. You do not need to handle either.

## Writing an article source

An article source sets `fetch_strategy=FETCH_NONE`, fills `body_html`, and is paired with `publish.target: acfun_article` (which needs `realm_id` as well as `channel_id`; run `mediabridge channels --articles`). Nothing is downloaded except an optional cover from `thumbnail_url`.

Pass your HTML through the flattener before assigning it:

```python
from mediabridge.utils.acfun_html import flatten_for_acfun

body_html = flatten_for_acfun(raw_html)
```

This is not cosmetic. AcFun sanitises article bodies server-side and **silently discards** everything outside a narrow subset — `postArticle` still returns success, so the loss is invisible until you look at the published page. Measured across 38 published articles (~170 kB of stored body HTML), the survivors were:

| Kept | Discarded |
| --- | --- |
| `p` `strong` `h1` `h2` `h3` `br` `hr` `img` `span` `div`, and `src` on `img` | every other tag, **every `<a>`**, every other attribute, and all emoji |

No anchor appeared anywhere in the sample, so a link left as `<a href>` reaches the reader as bare text with the URL gone. `flatten_for_acfun` therefore rewrites links as `label (https://example.com)`, converts list items to bulleted paragraphs, inlines `<details>`, drops relative image sources that AcFun could not resolve anyway, and removes emoji — the server deletes those but keeps the space around them, so `⭐️ 9.0/10` would otherwise arrive as ` 9.0/10` behind an orphaned U+FE0F.

Article submissions differ from video in two ways the publisher handles for you. A cover is optional. And attribution is appended to the body: `postArticle` has no `originalLinkUrl` field but places no limit on `detail`, so the publisher adds a 转载信息 block naming the author, the source link, the licence and the licence terms to whatever `body_html` you produce. Your source does not need to write one, and should not — a second block would only duplicate it.

The 200-character description limit applies to articles and video alike (`result=110014` and `result=109015`), and both go through `render_within`, which shrinks the upstream summary instead of trimming the credit off the end. A `desc_template` whose fixed text alone exceeds the limit raises `ConfigError` rather than producing an uncredited repost.

`mediabridge/sources/horizon.py` is a complete worked example.

## Registering it

In your package's `pyproject.toml`:

```toml
[project.entry-points."mediabridge.sources"]
mysource = "my_package.source:MySource"
```

Then:

```bash
pip install -e .
mediabridge sources          # your source appears in the list
```

Use it by `type` like any built-in:

```yaml
sources:
  - name: my-feed
    type: mysource
    options:
      feed_url: https://example.org/feed.json
    publish:
      channel_id: 190
      creation_type: 1
```

An entry point shadows a built-in of the same name, so you can override a bundled source without forking.

## Testing without the network

Stub `get_json` and assert on the `MediaItem`s. Every built-in source is tested this way in `tests/test_sources.py`, because no CI job should depend on a third party's uptime — and because several sources are simply unreachable from some networks.

```python
def test_discovers_items():
    class Stubbed(MySource):
        def get_json(self, url, params=None, timeout=30):
            return {"items": [{"id": "1", "title": "T", "link": "u", "media": "m.mp4"}]}

    items = Stubbed("my-feed", {"feed_url": "https://example.org/feed.json"}).discover()
    assert items[0].dedup_key == "my-feed:1"
```

# Writing a publisher

Same pattern in the other direction: implement `publish`, register under `mediabridge.publishers`, and select it with `publish.target` in the config.

```python
from mediabridge.models import FetchedMedia, PublishResult
from mediabridge.publishers.base import Publisher


class MyPublisher(Publisher):
    type_name = "myplatform"

    def validate_config(self, publish_config) -> None:
        """Fail fast on bad targeting, before anything is downloaded."""

    def publish(self, fetched: FetchedMedia, publish_config) -> PublishResult:
        if self.ctx.dry_run:
            return PublishResult(ok=True, dry_run=True)
        ...
        return PublishResult(ok=True, remote_id="123", url="https://...")
```

Honour `self.ctx.dry_run`, and raise `SkipItem` rather than an error for anything that is a deliberate non-publish.
