"""The dedup ledger.

Persisted as JSON and committed back to the repository by the workflow.
`actions/cache` would be the obvious alternative but it is evicted after seven
days without a hit, which silently turns into republishing everything.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import MediaItem, PublishResult

log = logging.getLogger(__name__)

STATE_VERSION = 1

#: Per-entry key holding the outcome of the platform's own post-submission
#: processing. Absent means there was never anything to wait for -- which is
#: what every entry written before deferred verification existed looks like, so
#: an old ledger loads unchanged and is never re-checked.
VERIFICATION = "verification"

#: Submitted, outcome not yet known. The only value a later run acts on.
PENDING = "pending"

VERIFIED_AT = "verified_at"


class State:
    """Tracks which items have already been published."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.published: dict[str, dict[str, Any]] = {}
        self._dirty = False

    def load(self) -> State:
        if not self.path.is_file():
            log.info("No state file at %s; starting with an empty ledger.", self.path)
            return self

        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            # Treating a corrupt ledger as empty would republish everything, so
            # refuse to run instead.
            raise RuntimeError(
                f"State file {self.path} is unreadable ({exc}). Fix or delete it before running."
            ) from exc

        self.published = data.get("published") or {}
        log.info("Loaded %d previously published item(s) from %s", len(self.published), self.path)
        return self

    def is_published(self, key: str) -> bool:
        return key in self.published

    def remote_id(self, key: str) -> str:
        return str((self.published.get(key) or {}).get("remote_id") or "")

    def mark_refreshed(self, key: str) -> None:
        """Note a re-submission without disturbing the original publish date."""
        entry = self.published.get(key)
        if entry is None:
            return
        entry["refreshed_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self._dirty = True

    def record(self, item: MediaItem, result: PublishResult) -> None:
        entry: dict[str, Any] = {
            "title": item.title,
            "source_url": item.webpage_url,
            "remote_id": result.remote_id,
            "remote_url": result.url,
            "published_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        if result.pending_verification:
            entry[VERIFICATION] = PENDING
        self.published[item.dedup_key] = entry
        self._dirty = True

    def pending_verification(self) -> list[tuple[str, dict[str, Any]]]:
        """Entries whose platform-side outcome an earlier run could not know."""
        return [(key, entry) for key, entry in self.published.items() if entry.get(VERIFICATION) == PENDING]

    def resolve_verification(self, key: str, outcome: str) -> None:
        """Record what a later run learned, so the entry is not asked about again."""
        entry = self.published.get(key)
        if entry is None:
            return
        entry[VERIFICATION] = outcome
        entry[VERIFIED_AT] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self._dirty = True

    def release(self, key: str) -> dict[str, Any] | None:
        """Forget an item, freeing its dedup key for a later run to try again."""
        entry = self.published.pop(key, None)
        if entry is not None:
            self._dirty = True
        return entry

    def save(self) -> bool:
        """Write the ledger back, returning True when anything changed."""
        if not self._dirty:
            return False

        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": STATE_VERSION,
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "published": self.published,
        }
        # Write via a temp file so an interrupted run cannot truncate the ledger.
        temp = self.path.with_suffix(self.path.suffix + ".tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        temp.replace(self.path)
        log.info("Saved state: %d published item(s)", len(self.published))
        self._dirty = False
        return True
