"""Turn a kino.pub dump into a sync plan.

Matching starts from the IMDb id, which kino.pub stores for nearly every item
as a bare integer. Items without one land in `unmatched` for manual review:
nothing downstream can match them reliably, and guessing would fabricate
history.
"""

from __future__ import annotations

from datetime import UTC, datetime

from .models import (
    WATCHED,
    Dump,
    EpisodeWatch,
    KinopubItem,
    KinopubVideo,
    KinopubWatching,
    MovieWatch,
    Plan,
    Progress,
    Unmatched,
    WatchlistShow,
)

MOVIE_TYPES = frozenset({"movie", "documovie", "3d", "concert"})

# Below 1% is noise (opened and immediately closed); at or above 80% kino.pub
# itself would already mark the item watched, so an unfinished item has to
# stay strictly inside that band to be recorded as in-progress.
MIN_PERCENT = 1.0
MAX_PERCENT = 79.9

WATCHED_AT_UNKNOWN = "unknown"


def imdb_id(item: KinopubItem) -> str | None:
    """kino.pub stores the imdb id as an integer, without the tt prefix."""
    if not item.imdb:
        return None
    digits = str(item.imdb).removeprefix("tt")
    if not digits.isdigit() or int(digits) == 0:
        return None
    return f"tt{int(digits):07d}"


def watched_at(timestamp: int | None) -> str:
    """ISO 8601 UTC datetime for Simkl, or the literal "unknown" when kino.pub
    has no timestamp — Simkl then marks the item watched without a date."""
    if not timestamp:
        return WATCHED_AT_UNKNOWN
    return datetime.fromtimestamp(timestamp, tz=UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def playback_percent(position: int, duration: int) -> float | None:
    """Playback percentage, or None if out of the in-progress band."""
    if not duration or not position:
        return None
    percent = round(min(position / duration * 100, MAX_PERCENT), 1)
    return percent if percent >= MIN_PERCENT else None


def _movie_entries(
    item: KinopubItem, imdb: str, watching: KinopubWatching | None
) -> tuple[MovieWatch | None, Progress | None]:
    videos: list[KinopubVideo] = watching.videos if watching else []

    if videos and all(video.status == WATCHED for video in videos):
        movie = MovieWatch(
            kinopub_id=item.id,
            title=item.title,
            year=item.year,
            imdb=imdb,
            watched_at=watched_at(max(video.updated or 0 for video in videos)),
        )
        return movie, None

    percent = playback_percent(
        sum(video.time or 0 for video in videos),
        sum(video.duration or 0 for video in videos),
    )
    if percent is None:
        return None, None
    progress = Progress(kinopub_id=item.id, title=item.title, year=item.year, imdb=imdb, percent=percent)
    return None, progress


def _show_entries(
    item: KinopubItem, imdb: str, watching: KinopubWatching | None
) -> tuple[list[EpisodeWatch], list[Progress]]:
    watched: list[EpisodeWatch] = []
    progress: list[Progress] = []

    for season in watching.seasons if watching else []:
        for episode in season.episodes:
            if episode.status == WATCHED:
                watched.append(
                    EpisodeWatch(
                        kinopub_id=item.id,
                        title=item.title,
                        year=item.year,
                        imdb=imdb,
                        season=season.number,
                        episode=episode.number,
                        watched_at=watched_at(episode.updated),
                    )
                )
                continue
            percent = playback_percent(episode.time or 0, episode.duration or 0)
            if percent is not None:
                progress.append(
                    Progress(
                        kinopub_id=item.id,
                        title=item.title,
                        year=item.year,
                        imdb=imdb,
                        season=season.number,
                        episode=episode.number,
                        percent=percent,
                    )
                )
    return watched, progress


def build_plan(dump: Dump) -> Plan:
    plan = Plan()

    for item_id, item in dump.items.items():
        imdb = imdb_id(item)
        if imdb is None:
            plan.unmatched.append(
                Unmatched(title=item.title, reason="no imdb id", kinopub_id=item.id, year=item.year)
            )
            continue
        watching = dump.watching.get(item_id)
        if item.type in MOVIE_TYPES:
            movie, movie_progress = _movie_entries(item, imdb, watching)
            if movie:
                plan.movies.append(movie)
            if movie_progress:
                plan.progress.append(movie_progress)
        else:
            episodes, episode_progress = _show_entries(item, imdb, watching)
            plan.episodes.extend(episodes)
            plan.progress.extend(episode_progress)

    for entry in dump.watchlist:
        item = dump.items.get(str(entry.id)) or KinopubItem(id=entry.id, title=entry.title, year=entry.year)
        imdb = imdb_id(item)
        if imdb is None:
            plan.unmatched.append(
                Unmatched(
                    title=item.title,
                    reason="watchlist: no imdb id",
                    kinopub_id=entry.id,
                    year=item.year,
                )
            )
            continue
        plan.watchlist.append(WatchlistShow(kinopub_id=entry.id, title=item.title, year=item.year, imdb=imdb))

    return plan


def format_summary(plan: Plan, unmatched_preview: int = 15) -> str:
    stats = plan.stats
    lines = [
        f"movies watched:   {stats.movies}",
        f"episodes watched: {stats.episodes} across {stats.shows} shows",
        f"in progress:      {stats.progress}",
        f"watchlist:        {stats.watchlist}",
        f"unmatched:        {stats.unmatched}",
    ]
    lines += [
        f"  - {entry.title} ({entry.year}): {entry.reason}" for entry in plan.unmatched[:unmatched_preview]
    ]
    if stats.unmatched > unmatched_preview:
        lines.append(f"  ... and {stats.unmatched - unmatched_preview} more (see sync_plan.json)")
    return "\n".join(lines)
