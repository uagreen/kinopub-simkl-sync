"""Floppy API client: self-hosted media tracker, watch writes via TMDB ids.

API reference: <your instance>/api/docs/ (REST, `/api/v1`, auth via
`X-API-Key`). Floppy's movie/tv watch endpoints only accept `tmdb` or
`manual` as a source (see VALID_SOURCES in its api/helpers.py) — there is no
`imdb` source for movies/shows, unlike Simkl and MyShows. kino.pub only ever
gives us imdb ids, so every push first resolves imdb -> tmdb through TMDB's
public "find by external id" endpoint, with results cached to
data/tmdb_cache.json (a `None` entry means "confirmed no match", not "not
looked up yet", so a title with no TMDB listing does not get re-queried
every run).

Each watch call carries a stable `external_id` (built from the kino.pub item
id), which Floppy uses server-side to make a replayed call idempotent
instead of appending a duplicate play — see MediaMovieWatchView /
MediaEpisodeWatchView in its api/fork_views_tracking.py.
"""

from __future__ import annotations

from http import HTTPStatus
from types import TracebackType
from typing import Any, Self

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from .settings import Settings
from .storage import read_json, write_json

TMDB_API = "https://api.themoviedb.org/3"


class FloppyError(RuntimeError):
    pass


class FloppyNotFoundError(FloppyError):
    """A watch call Floppy answered with 404: it could not resolve the title
    or episode (typically kino.pub numbering a special/season that TMDB lacks).
    Per-entry, not fatal — push_floppy records it and moves on."""

    def __init__(self, detail: str) -> None:
        super().__init__(f"HTTP 404: {detail}")
        self.detail = detail


class FloppyClient:
    def __init__(self, settings: Settings) -> None:
        if not settings.floppy_url:
            raise FloppyError("FLOPPY_URL not set in .env")
        if not settings.floppy_api_token:
            raise FloppyError("FLOPPY_API_TOKEN not set in .env")
        if not settings.tmdb_api_key:
            raise FloppyError("TMDB_API_KEY not set in .env")

        self._settings = settings
        self._http = httpx.AsyncClient(
            base_url=settings.floppy_url, timeout=30, headers={"X-API-Key": settings.floppy_api_token}
        )
        self._tmdb = httpx.AsyncClient(base_url=TMDB_API, timeout=30)
        self._tmdb_cache: dict[str, int | None] = read_json(settings.paths.tmdb_cache, default={})

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self._http.aclose()
        await self._tmdb.aclose()

    # -- id resolution ---------------------------------------------------

    async def resolve_tmdb_id(self, kind: str, imdb_id: str) -> int | None:
        """kind is "movie" or "tv". Cached on data/tmdb_cache.json across runs."""
        key = f"{kind}:{imdb_id}"
        if key in self._tmdb_cache:
            return self._tmdb_cache[key]

        response = await self._tmdb_request(f"/find/{imdb_id}", params={"external_source": "imdb_id"})
        response.raise_for_status()
        results = response.json().get(f"{kind}_results") or []
        tmdb_id = results[0]["id"] if results else None

        self._tmdb_cache[key] = tmdb_id
        write_json(self._settings.paths.tmdb_cache, self._tmdb_cache)
        return tmdb_id

    @retry(
        retry=retry_if_exception_type(httpx.TransportError),
        stop=stop_after_attempt(3),
        wait=wait_exponential(min=1, max=10),
        reraise=True,
    )
    async def _tmdb_request(self, path: str, *, params: dict[str, Any]) -> httpx.Response:
        return await self._tmdb.get(path, params={**params, "api_key": self._settings.tmdb_api_key})

    # -- watch writes ------------------------------------------------------

    async def watch_movie(self, tmdb_id: int, *, end_date: str | None, external_id: str) -> None:
        await self._watch(
            f"/api/v1/media/movie/tmdb/{tmdb_id}/watch/", end_date=end_date, external_id=external_id
        )

    async def watch_episode(
        self, tmdb_id: int, season: int, episode: int, *, end_date: str | None, external_id: str
    ) -> None:
        await self._watch(
            f"/api/v1/media/tv/tmdb/{tmdb_id}/{season}/episodes/{episode}/watch/",
            end_date=end_date,
            external_id=external_id,
        )

    @retry(
        retry=retry_if_exception_type(httpx.TransportError),
        stop=stop_after_attempt(3),
        wait=wait_exponential(min=1, max=10),
        reraise=True,
    )
    async def _watch(self, path: str, *, end_date: str | None, external_id: str) -> None:
        body: dict[str, Any] = {"external_id": external_id}
        if end_date is not None:
            body["end_date"] = end_date
        response = await self._http.post(path, json=body)
        if response.status_code == HTTPStatus.NOT_FOUND:
            raise FloppyNotFoundError(_detail(response))
        if response.status_code not in (HTTPStatus.OK, HTTPStatus.CREATED):
            raise FloppyError(f"{path} failed: HTTP {response.status_code}: {response.text}")


def _detail(response: httpx.Response) -> str:
    try:
        detail = response.json().get("detail")
    except ValueError:
        detail = None
    return str(detail) if detail else response.text
