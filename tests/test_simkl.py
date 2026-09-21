import base64
import hashlib
import time

import httpx
import pytest

from kinopub_simkl_sync import simkl
from kinopub_simkl_sync.settings import Paths, Settings
from kinopub_simkl_sync.simkl import SimklClient, SimklError
from kinopub_simkl_sync.storage import read_json, write_json


def _settings(tmp_path):
    return Settings(simkl_client_id="cid", simkl_client_secret="csecret", paths=Paths(data_dir=tmp_path))


# -- PKCE ---------------------------------------------------------------


def test_pkce_pair_challenge_matches_verifier():
    verifier, challenge = simkl._pkce_pair()

    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert challenge == expected
    assert 43 <= len(verifier) <= 128


def test_pkce_pair_is_random_each_call():
    verifier_a, _ = simkl._pkce_pair()
    verifier_b, _ = simkl._pkce_pair()

    assert verifier_a != verifier_b


# -- authorize URL --------------------------------------------------------


def test_authorize_url_carries_pkce_and_scope(tmp_path):
    url = simkl._authorize_url(_settings(tmp_path), code_challenge="CHALLENGE", state="STATE")

    assert url.startswith("https://simkl.com/oauth2/authorize?")
    assert "client_id=cid" in url
    assert "response_type=code" in url
    assert "code_challenge=CHALLENGE" in url
    assert "code_challenge_method=S256" in url
    assert "state=STATE" in url
    assert "scope=media%3Aread+media%3Awrite" in url


# -- callback parsing -------------------------------------------------------


def test_parse_callback_query_extracts_code_and_state():
    result = simkl._parse_callback_query("GET /callback?code=ABC&state=XYZ HTTP/1.1")

    assert result == {"code": "ABC", "state": "XYZ"}


def test_parse_callback_query_extracts_error():
    result = simkl._parse_callback_query("GET /callback?error=access_denied&state=XYZ HTTP/1.1")

    assert result == {"error": "access_denied", "state": "XYZ"}


# -- access token: stored / refresh -----------------------------------------


@pytest.mark.asyncio
async def test_access_token_returns_stored_token_when_still_fresh(tmp_path):
    settings = _settings(tmp_path)
    write_json(
        settings.paths.tokens,
        {
            "simkl": {
                "access_token": "AT",
                "refresh_token": "RT",
                "expires_in": 604800,
                "obtained_at": time.time(),
            }
        },
    )

    async with SimklClient(settings) as client:
        assert await client._access_token() == "AT"


@pytest.mark.asyncio
async def test_access_token_raises_when_never_authorized(tmp_path):
    settings = _settings(tmp_path)

    async with SimklClient(settings) as client:
        with pytest.raises(SimklError, match="not authorized"):
            await client._access_token()


@pytest.mark.asyncio
async def test_access_token_refreshes_when_expired(tmp_path):
    settings = _settings(tmp_path)
    write_json(
        settings.paths.tokens,
        {
            "simkl": {
                "access_token": "OLD",
                "refresh_token": "RT",
                "expires_in": 1,
                "obtained_at": time.time() - 1000,
            }
        },
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/oauth2/token"
        body = dict(pair.split("=") for pair in request.content.decode().split("&"))
        assert body["grant_type"] == "refresh_token"
        assert body["refresh_token"] == "RT"
        assert body["client_secret"] == "csecret"
        return httpx.Response(
            200,
            json={
                "access_token": "NEW",
                "refresh_token": "RT2",
                "expires_in": 604800,
                "token_type": "Bearer",
            },
        )

    async with SimklClient(settings) as client:
        client._http = httpx.AsyncClient(base_url=simkl.SIMKL_API, transport=httpx.MockTransport(handler))
        token = await client._access_token()

    assert token == "NEW"
    stored = read_json(settings.paths.tokens)["simkl"]
    assert stored["refresh_token"] == "RT2"
    assert stored["access_token"] == "NEW"


@pytest.mark.asyncio
async def test_access_token_raises_when_refresh_rejected(tmp_path):
    settings = _settings(tmp_path)
    write_json(
        settings.paths.tokens,
        {
            "simkl": {
                "access_token": "OLD",
                "refresh_token": "RT",
                "expires_in": 1,
                "obtained_at": time.time() - 1000,
            }
        },
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant"})

    async with SimklClient(settings) as client:
        client._http = httpx.AsyncClient(base_url=simkl.SIMKL_API, transport=httpx.MockTransport(handler))
        with pytest.raises(SimklError, match="run: kts auth simkl"):
            await client._access_token()


# -- code exchange ------------------------------------------------------


@pytest.mark.asyncio
async def test_exchange_code_stores_tokens(tmp_path):
    settings = _settings(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/oauth2/token"
        body = dict(pair.split("=") for pair in request.content.decode().split("&"))
        assert body["grant_type"] == "authorization_code"
        assert body["code"] == "CODE"
        assert body["code_verifier"] == "VERIFIER"
        assert body["client_secret"] == "csecret"
        return httpx.Response(
            200,
            json={"access_token": "AT", "refresh_token": "RT", "expires_in": 604800, "token_type": "Bearer"},
        )

    async with SimklClient(settings) as client:
        client._http = httpx.AsyncClient(base_url=simkl.SIMKL_API, transport=httpx.MockTransport(handler))
        await client._exchange_code("CODE", "VERIFIER")

    stored = read_json(settings.paths.tokens)["simkl"]
    assert stored["access_token"] == "AT"
    assert stored["refresh_token"] == "RT"


@pytest.mark.asyncio
async def test_exchange_code_raises_on_failure(tmp_path):
    settings = _settings(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant"})

    async with SimklClient(settings) as client:
        client._http = httpx.AsyncClient(base_url=simkl.SIMKL_API, transport=httpx.MockTransport(handler))
        with pytest.raises(SimklError, match="token exchange failed"):
            await client._exchange_code("CODE", "VERIFIER")
