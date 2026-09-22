"""Simkl API client: OAuth2 v2 (Authorization Code + PKCE) auth and watch-history sync.

API reference: https://api.simkl.org/ (auth host https://simkl.com, API host
https://api.simkl.com). Every request carries `simkl-api-key: {client_id}`;
write endpoints also need `Authorization: Bearer {access_token}`. Auth v2 is
standards-compliant OAuth 2.0 with mandatory PKCE (S256): access tokens last
7 days, refresh tokens last 180 days and reset on each use, so a token close
to expiry is refreshed transparently and only a refresh-token that has itself
gone stale needs an interactive `kts auth simkl` again.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import secrets
import time
import webbrowser
from http import HTTPStatus
from types import TracebackType
from typing import Any, Self
from urllib.parse import parse_qsl, urlencode, urlsplit

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from .settings import SIMKL_API, Settings
from .storage import read_json, write_secret_json

log = logging.getLogger(__name__)

MAX_RATE_LIMIT_RETRIES = 5
DEFAULT_RETRY_AFTER = 2
REFRESH_SKEW_SECONDS = 60
AUTHORIZE_URL = "https://simkl.com/oauth2/authorize"
SCOPE = "media:read media:write"


class SimklError(RuntimeError):
    pass


def _pkce_pair() -> tuple[str, str]:
    """A PKCE (RFC 7636) verifier/challenge pair; S256 is the only method v2 accepts."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def _authorize_url(settings: Settings, *, code_challenge: str, state: str) -> str:
    params = {
        "response_type": "code",
        "client_id": settings.simkl_client_id,
        "redirect_uri": settings.simkl_redirect_uri,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "scope": SCOPE,
        "state": state,
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


def _parse_pasted_callback(pasted: str) -> dict[str, str]:
    """What the user pastes back after authorizing: either the full redirect URL
    (the browser can't load `http://localhost:.../callback`, but its address bar
    still carries the query string) or, failing that, just the bare code."""
    pasted = pasted.strip()
    query = urlsplit(pasted).query
    if query:
        return dict(parse_qsl(query))
    return {"code": pasted}


class SimklClient:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._http = httpx.AsyncClient(base_url=SIMKL_API, timeout=30)

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

    async def authorize(self) -> None:
        """Standard OAuth2 Authorization Code flow with PKCE: opens the browser at
        simkl.com and asks the user to paste back the resulting redirect URL. No
        local listener, so this needs no open or forwarded port — the redirect
        target (`SIMKL_REDIRECT_URI`, a loopback address by default) only has to
        match what's registered with the app; nothing has to actually be
        listening there. The browser will fail to load it, but its address bar
        still carries the `code`/`state` query string, which is all we need."""
        verifier, challenge = _pkce_pair()
        state = secrets.token_urlsafe(16)
        url = _authorize_url(self._settings, code_challenge=challenge, state=state)
        print(f"Open {url}")
        webbrowser.open(url)
        pasted = await asyncio.to_thread(
            input, "After authorizing, paste the redirect URL (or just the code) here: "
        )
        callback = _parse_pasted_callback(pasted)

        if "error" in callback:
            raise SimklError(f"authorization denied: {callback['error']}")
        if "state" in callback and callback["state"] != state:
            raise SimklError("authorization failed: state mismatch")
        code = callback.get("code")
        if not code:
            raise SimklError("authorization failed: no code returned")

        await self._exchange_code(code, verifier)
        print("simkl: authorized")

    async def _exchange_code(self, code: str, verifier: str) -> None:
        payload = {
            "grant_type": "authorization_code",
            "client_id": self._settings.simkl_client_id,
            "code": code,
            "redirect_uri": self._settings.simkl_redirect_uri,
            "code_verifier": verifier,
        }
        if self._settings.simkl_client_secret:
            payload["client_secret"] = self._settings.simkl_client_secret
        response = await self._http.post("/oauth2/token", data=payload)
        if response.status_code != HTTPStatus.OK:
            raise SimklError(f"token exchange failed: {response.status_code} {response.text}")
        self._store_tokens(response.json())

    def _store_tokens(self, payload: dict[str, Any]) -> None:
        tokens = read_json(self._settings.paths.tokens, default={})
        tokens["simkl"] = {**payload, "obtained_at": int(time.time())}
        write_secret_json(self._settings.paths.tokens, tokens)

    async def _access_token(self) -> str:
        tokens = read_json(self._settings.paths.tokens, default={}).get("simkl")
        if not tokens:
            raise SimklError("not authorized, run: kts auth simkl")
        if time.time() <= tokens["obtained_at"] + tokens.get("expires_in", 0) - REFRESH_SKEW_SECONDS:
            return tokens["access_token"]

        payload = {
            "grant_type": "refresh_token",
            "client_id": self._settings.simkl_client_id,
            "refresh_token": tokens["refresh_token"],
        }
        if self._settings.simkl_client_secret:
            payload["client_secret"] = self._settings.simkl_client_secret
        response = await self._http.post("/oauth2/token", data=payload)
        if response.status_code != HTTPStatus.OK:
            raise SimklError(f"token refresh failed ({response.status_code}), run: kts auth simkl")
        payload = response.json()
        self._store_tokens(payload)
        return payload["access_token"]

    # -- transport ---------------------------------------------------------

    @retry(
        retry=retry_if_exception_type(httpx.TransportError),
        stop=stop_after_attempt(3),
        wait=wait_exponential(min=1, max=10),
        reraise=True,
    )
    async def request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> httpx.Response:
        headers = {
            "simkl-api-key": self._settings.simkl_client_id,
            "Authorization": f"Bearer {await self._access_token()}",
        }
        for _ in range(MAX_RATE_LIMIT_RETRIES):
            response = await self._http.request(method, path, json=json_body, params=params, headers=headers)
            if response.status_code != HTTPStatus.TOO_MANY_REQUESTS:
                return response
            delay = int(response.headers.get("Retry-After", DEFAULT_RETRY_AFTER))
            log.debug("simkl throttled %s, waiting %ss", path, delay)
            await asyncio.sleep(delay)
        raise SimklError(f"rate limited repeatedly on {path}")

    async def get_json(self, path: str, **params: Any) -> Any:
        response = await self.request("GET", path, params=params or None)
        response.raise_for_status()
        return response.json()

    # -- sync ------------------------------------------------------------

    async def all_items(self, media_type: str) -> list[dict[str, Any]]:
        """Everything the account has under `media_type` ("movies" or "shows"),
        with per-episode watched state nested under each show."""
        payload = await self.get_json(f"/sync/all-items/{media_type}")
        return list(payload.get(media_type) or [])

    async def add_to_history(self, body: dict[str, Any]) -> dict[str, Any]:
        response = await self.request("POST", "/sync/history", json_body=body)
        response.raise_for_status()
        return response.json()
