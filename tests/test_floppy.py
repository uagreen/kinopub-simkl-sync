import json

import httpx
import pytest

from kinopub_simkl_sync.floppy import FloppyClient, FloppyError, FloppyNotFoundError
from kinopub_simkl_sync.settings import Paths, Settings
from kinopub_simkl_sync.storage import read_json, write_json


def _settings(tmp_path, **overrides):
    defaults = {
        "floppy_url": "https://floppy.example.com",
        "floppy_api_token": "ftoken",
        "tmdb_api_key": "tkey",
    }
    return Settings(paths=Paths(data_dir=tmp_path), **{**defaults, **overrides})


def _client(settings, *, floppy_handler=None, tmdb_handler=None) -> FloppyClient:
    client = FloppyClient(settings)
    if floppy_handler is not None:
        client._http = httpx.AsyncClient(
            base_url=settings.floppy_url,
            headers={"X-API-Key": settings.floppy_api_token},
            transport=httpx.MockTransport(floppy_handler),
        )
    if tmdb_handler is not None:
        client._tmdb = httpx.AsyncClient(
            base_url="https://api.themoviedb.org/3", transport=httpx.MockTransport(tmdb_handler)
        )
    return client


# -- config validation --------------------------------------------------


def test_missing_floppy_url_raises(tmp_path):
    with pytest.raises(FloppyError, match="FLOPPY_URL"):
        FloppyClient(_settings(tmp_path, floppy_url=""))


def test_missing_floppy_api_token_raises(tmp_path):
    with pytest.raises(FloppyError, match="FLOPPY_API_TOKEN"):
        FloppyClient(_settings(tmp_path, floppy_api_token=""))


def test_missing_tmdb_api_key_raises(tmp_path):
    with pytest.raises(FloppyError, match="TMDB_API_KEY"):
        FloppyClient(_settings(tmp_path, tmdb_api_key=""))


# -- resolve_tmdb_id ------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_tmdb_id_finds_and_caches_movie(tmp_path):
    settings = _settings(tmp_path)
    calls = []

    def tmdb_handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.url.path == "/3/find/tt5442430"
        assert request.url.params["external_source"] == "imdb_id"
        assert request.url.params["api_key"] == "tkey"
        return httpx.Response(200, json={"movie_results": [{"id": 641}], "tv_results": []})

    async with _client(settings, tmdb_handler=tmdb_handler) as client:
        tmdb_id = await client.resolve_tmdb_id("movie", "tt5442430")
        assert tmdb_id == 641
        # second call for the same id is served from cache, no extra request
        again = await client.resolve_tmdb_id("movie", "tt5442430")
        assert again == 641

    assert len(calls) == 1
    assert read_json(settings.paths.tmdb_cache)["movie:tt5442430"] == 641


@pytest.mark.asyncio
async def test_resolve_tmdb_id_caches_no_match(tmp_path):
    settings = _settings(tmp_path)

    def tmdb_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"movie_results": [], "tv_results": []})

    async with _client(settings, tmdb_handler=tmdb_handler) as client:
        tmdb_id = await client.resolve_tmdb_id("tv", "tt0000002")

    assert tmdb_id is None
    assert read_json(settings.paths.tmdb_cache)["tv:tt0000002"] is None


@pytest.mark.asyncio
async def test_resolve_tmdb_id_reads_existing_cache_without_network_call(tmp_path):
    settings = _settings(tmp_path)
    write_json(settings.paths.tmdb_cache, {"movie:tt5442430": 641})

    def tmdb_handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("should not hit the network for a cached id")

    async with _client(settings, tmdb_handler=tmdb_handler) as client:
        assert await client.resolve_tmdb_id("movie", "tt5442430") == 641


# -- watch_movie / watch_episode -------------------------------------------


@pytest.mark.asyncio
async def test_watch_movie_posts_end_date_and_external_id(tmp_path):
    settings = _settings(tmp_path)

    def floppy_handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/v1/media/movie/tmdb/641/watch/"
        assert request.headers["X-API-Key"] == "ftoken"
        body = json.loads(request.content)
        assert body == {"external_id": "kinopub:1", "end_date": "2024-06-01T20:00:00.000Z"}
        return httpx.Response(201, json={})

    async with _client(settings, floppy_handler=floppy_handler) as client:
        await client.watch_movie(641, end_date="2024-06-01T20:00:00.000Z", external_id="kinopub:1")


@pytest.mark.asyncio
async def test_watch_movie_omits_end_date_when_none(tmp_path):
    settings = _settings(tmp_path)

    def floppy_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body == {"external_id": "kinopub:1"}
        return httpx.Response(200, json={})

    async with _client(settings, floppy_handler=floppy_handler) as client:
        await client.watch_movie(641, end_date=None, external_id="kinopub:1")


@pytest.mark.asyncio
async def test_watch_movie_raises_on_error_status(tmp_path):
    settings = _settings(tmp_path)

    def floppy_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "not found"})

    async with _client(settings, floppy_handler=floppy_handler) as client:
        with pytest.raises(FloppyError, match="404"):
            await client.watch_movie(641, end_date=None, external_id="kinopub:1")


@pytest.mark.asyncio
async def test_watch_episode_raises_not_found_with_detail_on_404(tmp_path):
    settings = _settings(tmp_path)

    def floppy_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "Episode not found."})

    async with _client(settings, floppy_handler=floppy_handler) as client:
        with pytest.raises(FloppyNotFoundError) as excinfo:
            await client.watch_episode(1400, 0, 1, end_date=None, external_id="kinopub:9:0:1")

    assert excinfo.value.detail == "Episode not found."


@pytest.mark.asyncio
async def test_watch_movie_error_other_than_404_is_not_not_found(tmp_path):
    settings = _settings(tmp_path)

    def floppy_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    async with _client(settings, floppy_handler=floppy_handler) as client:
        with pytest.raises(FloppyError, match="500") as excinfo:
            await client.watch_movie(641, end_date=None, external_id="kinopub:1")

    assert not isinstance(excinfo.value, FloppyNotFoundError)


@pytest.mark.asyncio
async def test_watch_episode_posts_expected_path(tmp_path):
    settings = _settings(tmp_path)

    def floppy_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/media/tv/tmdb/1400/2/episodes/5/watch/"
        return httpx.Response(201, json={})

    async with _client(settings, floppy_handler=floppy_handler) as client:
        await client.watch_episode(1400, 2, 5, end_date=None, external_id="kinopub:9:2:5")
