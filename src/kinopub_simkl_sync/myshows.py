"""MyShows API client: password-grant auth and episode-check writes.

API reference: https://api.myshows.me/shared/doc/ (JSON-RPC 2.0 over
https://api.myshows.me/v2/rpc/, OAuth2 for auth). MyShows tracks TV shows
only — the API has no movie endpoints at all, so nothing here handles movies.

MyShows has no field for a historical watched_at anywhere in the write API
(CheckEpisode/SyncEpisodes/SyncEpisodesDelta all date the write server-side,
as "now") — this client makes no attempt to preserve kino.pub's watch dates.

`manage.SyncEpisodes`'s exact replace-vs-add semantics aren't documented (its
sibling `SyncEpisodesDelta` splits checked/unchecked explicitly, which hints
SyncEpisodes might replace a show's whole watched state). push.py sidesteps
the question by always sending the complete known-watched episode list for a
show, never just the new ones — correct either way.
"""

from __future__ import annotations

import logging
from http import HTTPStatus
from types import TracebackType
from typing import Any, Self

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from .settings import Settings

log = logging.getLogger(__name__)

AUTH_URL = "https://myshows.me/oauth/token"
RPC_URL = "https://api.myshows.me/v2/rpc/"


class MyShowsError(RuntimeError):
    pass


class MyShowsClient:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._http = httpx.AsyncClient(timeout=30)
        self._request_id = 0
        self._logged_in = False

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self._http.aclose()

    # -- auth --------------------------------------------------------------

    async def _login(self) -> None:
        if self._logged_in:
            return
        if not self._settings.myshows_username or not self._settings.myshows_password:
            raise MyShowsError("MYSHOWS_USERNAME/MYSHOWS_PASSWORD not set in .env")
        response = await self._http.post(
            AUTH_URL,
            data={
                "grant_type": "password",
                "client_id": self._settings.myshows_client_id,
                "client_secret": self._settings.myshows_client_secret,
                "username": self._settings.myshows_username,
                "password": self._settings.myshows_password,
            },
        )
        if response.status_code != HTTPStatus.OK:
            raise MyShowsError(f"login failed: HTTP {response.status_code}: {response.text}")
        self._http.headers["Authorization"] = f"Bearer {response.json()['access_token']}"
        self._logged_in = True

    # -- transport -----------------------------------------------------------

    @retry(
        retry=retry_if_exception_type(httpx.TransportError),
        stop=stop_after_attempt(3),
        wait=wait_exponential(min=1, max=10),
        reraise=True,
    )
    async def rpc(self, method: str, params: dict[str, Any]) -> Any:
        await self._login()
        self._request_id += 1
        response = await self._http.post(
            RPC_URL,
            json={"jsonrpc": "2.0", "method": method, "params": params, "id": self._request_id},
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("error") is not None:
            raise MyShowsError(f"{method}: {payload['error']}")
        return payload["result"]

    # -- show/episode resolution ---------------------------------------------

    async def show_by_imdb(self, imdb: str) -> dict[str, Any] | None:
        """Resolve a show by imdb id (kino.pub's "ttNNNNNNN" form), episodes
        included. None when MyShows has no matching show.

        GetByExternalId's own `episodes` field comes back empty in practice
        despite the documented schema — GetById(withEpisodes=true) is what
        actually populates it, so both calls are needed.
        """
        digits = imdb.removeprefix("tt").lstrip("0") or "0"
        try:
            summary = await self.rpc("shows.GetByExternalId", {"id": int(digits), "source": "imdb"})
        except MyShowsError:
            return None
        return await self.rpc("shows.GetById", {"showId": summary["id"], "withEpisodes": True})

    async def sync_episodes(self, show_id: int, episode_ids: list[int]) -> bool:
        return bool(await self.rpc("manage.SyncEpisodes", {"showId": show_id, "episodeIds": episode_ids}))
