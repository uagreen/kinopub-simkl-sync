"""Dump everything watch-related from kino.pub into data/kinopub_dump.json.

Metadata (including the imdb id) comes from the item embedded in each history
record, so watched titles need no extra /v1/items call and survive removal from
the catalog. /v1/watching supplies per-episode status, position and timestamps
and answers even for deleted items. /v1/items is needed only for ids that never
appear in history — started-but-unwatched movies and the watchlist — and is
404-tolerant.

When a previous dump exists, the pull is incremental: history's `last_seen` is
a per-item high-water mark, so an item that hasn't resurfaced at the top of
history since the last pull has not changed, and its cached metadata/watch
state is reused instead of re-fetched.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from typing import Any

from rich.progress import Progress

from .kinopub import KinopubClient
from .models import Dump
from .settings import Settings
from .storage import read_model, write_model

log = logging.getLogger(__name__)


async def pull(settings: Settings) -> Dump:
    previous = read_model(settings.paths.dump, Dump)
    watermark = max((record.get("last_seen") or 0 for record in previous.history), default=0) if previous else None

    async with KinopubClient(settings) as client:
        new_history = await client.history(stop_after=watermark)
        print(f"history: {len(new_history)} {'new/updated ' if previous else ''}records")

        changed_ids = {
            record["item"]["id"]
            for record in new_history
            if isinstance(record.get("item"), dict) and record["item"].get("id")
        }

        if previous:
            history = new_history + [
                record for record in previous.history if (record.get("item") or {}).get("id") not in changed_ids
            ]
            items: dict[str, Any] = {key: value.model_dump() for key, value in previous.items.items()}
            watching: dict[str, Any] = {key: value.model_dump() for key, value in previous.watching.items()}
        else:
            history = new_history
            items = {}
            watching = {}

        # New/updated history records carry fresh item metadata.
        items.update(
            {
                str(record["item"]["id"]): record["item"]
                for record in new_history
                if isinstance(record.get("item"), dict) and record["item"].get("id")
            }
        )

        # Ids that may be absent from history yet still carry watch state.
        unwatched, watchlist = await asyncio.gather(client.unwatched_movies(), client.watchlist())
        extra_ids = {record["id"] for record in (*unwatched, *watchlist) if record.get("id")}

        # Unchanged items keep their cached watch state; only what actually
        # moved (or was never tied to a history record) needs a fresh check.
        refresh_ids = (
            sorted(changed_ids | extra_ids) if previous else sorted({int(key) for key in items} | extra_ids)
        )

        async def fetch(item_id: int) -> tuple[int, dict[str, Any] | None, dict[str, Any] | None]:
            state = await client.watching(item_id)
            metadata = None if str(item_id) in items else await client.item(item_id)
            return item_id, state, metadata

        missing: list[int] = []
        # Sequential, not asyncio.gather'd: kino.pub rate-limits by request
        # rate, not just concurrency, and a swarm of tasks refills a freed
        # semaphore slot the instant one backs off from a 429, so the whole
        # batch never actually slows down.
        with Progress(transient=True, disable=not sys.stdout.isatty()) as progress:
            bar = progress.add_task("watch states", total=len(refresh_ids))
            for item_id in refresh_ids:
                _, state, metadata = await fetch(item_id)
                if state is not None:
                    watching[str(item_id)] = state
                elif str(item_id) in watching:
                    del watching[str(item_id)]
                if metadata is not None:
                    items[str(item_id)] = metadata
                elif str(item_id) not in items:
                    missing.append(item_id)
                progress.advance(bar)

    dump = Dump.model_validate(
        {
            "pulled_at": int(time.time()),
            "history": history,
            "items": items,
            "watching": watching,
            "watchlist": watchlist,
        }
    )
    write_model(settings.paths.dump, dump)
    if missing:
        print(f"skipped {len(missing)} ids with no metadata (deleted, absent from history)")
    print(
        f"dump saved: {settings.paths.dump} "
        f"({len(dump.items)} items, {len(dump.watching)} watch states, {len(history)} history records)"
    )
    return dump
