"""Apply a sync plan to Simkl, and separately to MyShows.

Idempotency has two layers for Simkl, because it does not deduplicate history
plays: locally, every pushed entry is recorded in data/push_state.json and
skipped on re-runs; remotely, items already watched on the account are
skipped too, so a first run against a non-empty account does not double
existing plays.

MyShows gets its own push_state_myshows.json (a different service, a
different key namespace — not to be mixed with Simkl's) and a different
idempotency shape: see push_myshows for why it always resends a show's
complete watched-episode list rather than just what's new.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from .floppy import FloppyClient, FloppyNotFoundError
from .models import EpisodeWatch, MovieWatch, Plan
from .myshows import MyShowsClient
from .simkl import SimklClient
from .storage import read_model, write_model
from .transform import WATCHED_AT_UNKNOWN

log = logging.getLogger(__name__)

HISTORY_CHUNK = 500
WRITE_INTERVAL_SECONDS = 1  # be gentle with Simkl's write endpoint


class PushState(BaseModel):
    """Which plan entries already reached Simkl.

    Sole owner of the key format: everything else asks an entry for its
    `state_key`, so push and status can never drift apart.
    """

    pushed: list[str] = []
    not_found: list[dict[str, Any]] = []

    @classmethod
    def load(cls, path: Path) -> PushState:
        return read_model(path, cls) or cls()

    def save(self, path: Path) -> None:
        write_model(path, self)

    def __contains__(self, entry: MovieWatch | EpisodeWatch) -> bool:
        return entry.state_key in set(self.pushed)

    def record(self, entries: Iterable[MovieWatch | EpisodeWatch]) -> None:
        self.pushed.extend(entry.state_key for entry in entries)


def movies_payload(movies: list[MovieWatch]) -> dict[str, Any]:
    return {"movies": [{"ids": {"imdb": movie.imdb}, "watched_at": movie.watched_at} for movie in movies]}


def episodes_payload(episodes: list[EpisodeWatch]) -> dict[str, Any]:
    shows: dict[str, dict[int, list[dict[str, Any]]]] = {}
    for entry in episodes:
        seasons = shows.setdefault(entry.imdb, {})
        seasons.setdefault(entry.season, []).append({"number": entry.episode, "watched_at": entry.watched_at})
    return {
        "shows": [
            {
                "ids": {"imdb": imdb},
                "seasons": [{"number": number, "episodes": eps} for number, eps in sorted(seasons.items())],
            }
            for imdb, seasons in shows.items()
        ]
    }


async def watched_on_simkl(client: SimklClient) -> tuple[set[str], set[tuple[str, int, int]]]:
    """Everything the account already counts as watched, indexed by imdb id."""
    movies: set[str] = set()
    for row in await client.all_items("movies"):
        if row.get("status") != "completed":
            continue
        imdb = (row.get("ids") or {}).get("imdb")
        if imdb:
            movies.add(imdb)

    episodes: set[tuple[str, int, int]] = set()
    for row in await client.all_items("shows"):
        imdb = (row.get("ids") or {}).get("imdb")
        if not imdb:
            continue
        for season in row.get("seasons") or []:
            for episode in season.get("episodes") or []:
                episodes.add((imdb, season["number"], episode["number"]))
    return movies, episodes


def _chunks[T](items: list[T], size: int) -> Iterator[list[T]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


async def push_movies(
    plan: Plan, client: SimklClient, state: PushState, state_path: Path, *, dry_run: bool
) -> None:
    watched_movies, _ = await watched_on_simkl(client)
    movies = [movie for movie in plan.movies if movie not in state and movie.imdb not in watched_movies]
    skipped = len(plan.movies) - len(movies)
    print(f"movies: {len(movies)} to push, {skipped} already synced")
    if dry_run:
        return

    for chunk in _chunks(movies, HISTORY_CHUNK):
        result = await client.add_to_history(movies_payload(chunk))
        state.record(chunk)
        state.not_found.extend(result.get("not_found", {}).get("movies", []))
        state.save(state_path)
        print(f"  chunk: added {result.get('added', {}).get('movies', len(chunk))}")
        await asyncio.sleep(WRITE_INTERVAL_SECONDS)

    if state.not_found:
        print(f"not found on Simkl: {len(state.not_found)} (see push_state.json)")


async def push_floppy(
    plan: Plan, client: FloppyClient, state: PushState, state_path: Path, *, dry_run: bool
) -> None:
    """Floppy has no bulk history endpoint that preserves per-item dates, so
    this writes one watch call per movie and per episode — each carrying a
    stable `external_id`, which makes a replayed call idempotent server-side
    on top of the local push_state skip. No write_interval pause between
    calls: unlike Simkl/MyShows this is a self-hosted instance the user
    controls, not a shared service with a remote rate limit to respect.

    A 404 from a watch call (Floppy could not resolve that title/episode) is
    recorded in not_found and the run carries on; the entry is not marked
    pushed, so it is retried next run in case TMDB catches up."""
    movies = [movie for movie in plan.movies if movie not in state]
    episodes_by_show = group_episodes_by_imdb([e for e in plan.episodes if e not in state])
    print(f"movies: {len(movies)} to push, {len(plan.movies) - len(movies)} already synced")
    total_episodes = sum(len(entries) for entries in episodes_by_show.values())
    skipped_episodes = len(plan.episodes) - total_episodes
    print(f"episodes: {total_episodes} to push, {skipped_episodes} already synced")
    if dry_run:
        return

    for movie in movies:
        tmdb_id = await client.resolve_tmdb_id("movie", movie.imdb)
        if tmdb_id is None:
            state.not_found.append(
                {"imdb": movie.imdb, "title": movie.title, "reason": "no tmdb match for this imdb id"}
            )
            state.save(state_path)
            continue
        end_date = None if movie.watched_at == WATCHED_AT_UNKNOWN else movie.watched_at
        try:
            await client.watch_movie(tmdb_id, end_date=end_date, external_id=f"kinopub:{movie.kinopub_id}")
        except FloppyNotFoundError as error:
            _note_not_found(
                state,
                {
                    "imdb": movie.imdb,
                    "tmdb": tmdb_id,
                    "title": movie.title,
                    "reason": f"floppy 404: {error.detail}",
                },
            )
            state.save(state_path)
            continue
        state.record([movie])
        state.save(state_path)

    for imdb, entries in episodes_by_show.items():
        tmdb_id = await client.resolve_tmdb_id("tv", imdb)
        if tmdb_id is None:
            for entry in entries:
                state.not_found.append(
                    {
                        "imdb": imdb,
                        "title": entry.title,
                        "season": entry.season,
                        "episode": entry.episode,
                        "reason": "no tmdb match for this imdb id",
                    }
                )
            state.save(state_path)
            continue
        for entry in entries:
            end_date = None if entry.watched_at == WATCHED_AT_UNKNOWN else entry.watched_at
            try:
                await client.watch_episode(
                    tmdb_id,
                    entry.season,
                    entry.episode,
                    end_date=end_date,
                    external_id=f"kinopub:{entry.kinopub_id}:{entry.season}:{entry.episode}",
                )
            except FloppyNotFoundError as error:
                _note_not_found(
                    state,
                    {
                        "imdb": imdb,
                        "tmdb": tmdb_id,
                        "title": entry.title,
                        "season": entry.season,
                        "episode": entry.episode,
                        "reason": f"floppy 404: {error.detail}",
                    },
                )
                continue
            state.record([entry])
        state.save(state_path)

    if state.not_found:
        print(f"not found on Floppy: {len(state.not_found)} (see push_state_floppy.json)")


def _note_not_found(state: PushState, entry: dict[str, Any]) -> None:
    """Retried 404s would otherwise append the same entry on every run."""
    if entry not in state.not_found:
        state.not_found.append(entry)


def group_episodes_by_imdb(episodes: list[EpisodeWatch]) -> dict[str, list[EpisodeWatch]]:
    groups: dict[str, list[EpisodeWatch]] = {}
    for entry in episodes:
        groups.setdefault(entry.imdb, []).append(entry)
    return groups


async def push_myshows(
    plan: Plan, client: MyShowsClient, state: PushState, state_path: Path, *, dry_run: bool
) -> None:
    """MyShows has no movies endpoint, so this only ever touches plan.episodes.

    Per show, it always sends the *complete* watched-episode list from the
    plan — not just what push_state doesn't have yet. manage.SyncEpisodes's
    replace-vs-add semantics aren't documented, and resending the full known
    state is correct under either reading; the state_key skip below only
    decides which shows are worth a resolve+write round trip this run.
    """
    groups = group_episodes_by_imdb(plan.episodes)
    shows_with_news = {
        imdb: entries for imdb, entries in groups.items() if any(e not in state for e in entries)
    }
    print(
        f"shows: {len(shows_with_news)} with new episodes, {len(groups) - len(shows_with_news)} already synced"
    )
    if dry_run:
        return

    for imdb, entries in shows_with_news.items():
        show = await client.show_by_imdb(imdb)
        if show is None:
            state.not_found.append(
                {"imdb": imdb, "title": entries[0].title, "reason": "show not found on myshows"}
            )
            state.save(state_path)
            continue

        by_se = {(e["seasonNumber"], e["episodeNumber"]): e["id"] for e in show.get("episodes") or []}
        episode_ids: list[int] = []
        matched: list[EpisodeWatch] = []
        for entry in entries:
            episode_id = by_se.get((entry.season, entry.episode))
            if episode_id is None:
                state.not_found.append(
                    {
                        "imdb": imdb,
                        "title": entry.title,
                        "season": entry.season,
                        "episode": entry.episode,
                        "reason": "episode not found on myshows",
                    }
                )
                continue
            episode_ids.append(episode_id)
            matched.append(entry)

        if episode_ids:
            await client.sync_episodes(show["id"], episode_ids)
            state.record(entry for entry in matched if entry not in state)
            print(f"  {entries[0].title}: synced {len(episode_ids)} episodes")
        state.save(state_path)
        await asyncio.sleep(WRITE_INTERVAL_SECONDS)

    if state.not_found:
        print(f"not found on MyShows: {len(state.not_found)} (see push_state_myshows.json)")


async def push_episodes(
    plan: Plan, client: SimklClient, state: PushState, state_path: Path, *, dry_run: bool
) -> None:
    _, watched_episodes = await watched_on_simkl(client)
    episodes = [
        entry
        for entry in plan.episodes
        if entry not in state and (entry.imdb, entry.season, entry.episode) not in watched_episodes
    ]
    skipped = len(plan.episodes) - len(episodes)
    print(f"episodes: {len(episodes)} to push, {skipped} already synced")
    if dry_run:
        return

    for chunk in _chunks(episodes, HISTORY_CHUNK):
        result = await client.add_to_history(episodes_payload(chunk))
        state.record(chunk)
        state.not_found.extend(result.get("not_found", {}).get("shows", []))
        state.save(state_path)
        print(f"  chunk: added {result.get('added', {}).get('episodes', len(chunk))}")
        await asyncio.sleep(WRITE_INTERVAL_SECONDS)

    if state.not_found:
        print(f"not found on Simkl: {len(state.not_found)} (see push_state.json)")
