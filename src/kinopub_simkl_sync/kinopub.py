"""kino.pub API client: device-code auth and concurrent read-only extraction.

API reference: https://kinoapi.com/ (base https://api.service-kp.com/v1).

Careful: this API performs mutations over GET (/v1/items/vote,
/v1/watching/toggle, /v1/watching/marktime, /v1/history/clear-*). Nothing here
touches those paths — the client is read-only by construction.
"""

from __future__ import annotations

import asyncio
import logging
import time
from http import HTTPStatus
from types import TracebackType
from typing import Any, Self

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from .settings import Settings
from .storage import read_json, write_secret_json

log = logging.getLogger(__name__)

# kino.pub is a small service and throttles readily: this is the level that
# empirically completes a full pull without tripping 429s in bursts.
CONCURRENCY = 3
HISTORY_PAGE_SIZE = 50  # API hard cap
POLL_TIMEOUT_SECONDS = 300
MAX_ATTEMPTS = 5

# kino.pub sits behind Cloudflare, which is more likely to flag httpx's default
# UA as a bot; the official XBMC client sends a real browser UA on every request.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36"
)


class KinopubError(RuntimeError):
    pass


def _is_retryable(exc: BaseException) -> bool:
    """Network hiccups and throttling both deserve a retry; 404 and friends do not."""
    if isinstance(exc, httpx.TransportError):
        return True
    return isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == HTTPStatus.TOO_MANY_REQUESTS


class KinopubClient:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._http = httpx.AsyncClient(timeout=30, headers={"User-Agent": USER_AGENT})
        self._semaphore = asyncio.Semaphore(CONCURRENCY)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self._http.aclose()

    # -- auth ------------------------------------------------------------

    async def device_auth(self) -> None:
        credentials = {
            "client_id": self._settings.kinopub_client_id,
            "client_secret": self._settings.kinopub_client_secret,
        }
        response = await self._http.post(
            self._settings.kinopub_device_url, data={"grant_type": "device_code", **credentials}
        )
        response.raise_for_status()
        device = response.json()
        print(f"Open {device['verification_uri']} and enter code: {device['user_code']}")

        deadline = time.time() + device.get("expires_in", POLL_TIMEOUT_SECONDS)
        while time.time() < deadline:
            await asyncio.sleep(device.get("interval", 5))
            response = await self._http.post(
                self._settings.kinopub_device_url,
                data={"grant_type": "device_token", "code": device["code"], **credentials},
            )
            if response.status_code == HTTPStatus.OK:
                self._store_tokens(response.json())
                print("kino.pub: authorized")
                return
            if response.json().get("error") != "authorization_pending":
                raise KinopubError(response.text)
        raise KinopubError("device code expired, run auth again")

    def _store_tokens(self, payload: dict[str, Any]) -> None:
        tokens = read_json(self._settings.paths.tokens, default={})
        tokens["kinopub"] = {**payload, "obtained_at": int(time.time())}
        write_secret_json(self._settings.paths.tokens, tokens)

    async def _access_token(self) -> str:
        tokens = read_json(self._settings.paths.tokens, default={}).get("kinopub")
        if not tokens:
            raise KinopubError("not authorized, run: kts auth kinopub")
        if time.time() <= tokens["obtained_at"] + tokens.get("expires_in", 3600) - 60:
            return tokens["access_token"]

        # The refresh token expires after 30 days of disuse; past that only a
        # fresh device authorization helps.
        response = await self._http.post(
            self._settings.kinopub_token_url,
            data={
                "grant_type": "refresh_token",
                "refresh_token": tokens["refresh_token"],
                "client_id": self._settings.kinopub_client_id,
                "client_secret": self._settings.kinopub_client_secret,
            },
        )
        if response.status_code != HTTPStatus.OK:
            raise KinopubError(f"token refresh failed ({response.status_code}), run: kts auth kinopub")
        self._store_tokens(response.json())
        return read_json(self._settings.paths.tokens)["kinopub"]["access_token"]

    # -- transport -------------------------------------------------------

    @retry(
        retry=retry_if_exception(_is_retryable),
        stop=stop_after_attempt(MAX_ATTEMPTS),
        wait=wait_exponential(min=1, max=20),
        reraise=True,
    )
    async def get(self, path: str, **params: Any) -> dict[str, Any]:
        params["access_token"] = await self._access_token()
        async with self._semaphore:
            response = await self._http.get(f"{self._settings.kinopub_api_url}{path}", params=params)
        response.raise_for_status()
        return response.json()

    async def get_optional(self, path: str, **params: Any) -> dict[str, Any] | None:
        """Like get(), but None when the item was deleted from the catalog."""
        try:
            return await self.get(path, **params)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == HTTPStatus.NOT_FOUND:
                log.debug("kino.pub 404 for %s", path)
                return None
            raise

    # -- data ------------------------------------------------------------

    async def history(self, stop_after: int | None = None) -> list[dict[str, Any]]:
        """The view log, newest first, down to `stop_after`. Records embed their
        item metadata (including the imdb id), so watched titles need no
        /v1/items call and survive removal from the catalog.

        Pages are sorted by descending `last_seen`, so once a page's records
        drop to `last_seen <= stop_after` the rest of history is already known
        from a previous pull and fetching stops there. `stop_after=None` (no
        previous pull) always walks every page."""
        records: list[dict[str, Any]] = []
        page = 1
        page_count = 1
        while page <= page_count:
            response = await self.get("/v1/history", page=page, perpage=HISTORY_PAGE_SIZE)
            page_records = list(response.get("history") or [])
            page_count = int((response.get("pagination") or {}).get("total") or 1)
            if stop_after is None:
                records.extend(page_records)
            else:
                new_records = [r for r in page_records if (r.get("last_seen") or 0) > stop_after]
                records.extend(new_records)
                if len(new_records) < len(page_records):
                    break
            page += 1
        return records

    async def item(self, item_id: int) -> dict[str, Any] | None:
        payload = await self.get_optional(f"/v1/items/{item_id}")
        return payload["item"] if payload else None

    async def watching(self, item_id: int) -> dict[str, Any] | None:
        """Per-episode watch state: status (-1/0/1), position and timestamp.
        Answers even for items already removed from the catalog."""
        payload = await self.get_optional("/v1/watching", id=item_id)
        return payload["item"] if payload else None

    async def unwatched_movies(self) -> list[dict[str, Any]]:
        return list((await self.get("/v1/watching/movies")).get("items") or [])

    async def watchlist(self) -> list[dict[str, Any]]:
        return list((await self.get("/v1/watching/serials", subscribed=1)).get("items") or [])
