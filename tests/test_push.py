import pytest

from kinopub_simkl_sync.floppy import FloppyNotFoundError
from kinopub_simkl_sync.models import EpisodeWatch, MovieWatch, Plan
from kinopub_simkl_sync.push import (
    PushState,
    group_episodes_by_imdb,
    push_floppy,
    push_myshows,
    watched_on_simkl,
)

WATCHED_MOVIES = [
    {"ids": {"simkl": 1, "imdb": "tt5442430"}, "title": "Life", "status": "completed"},
    {"ids": {"simkl": 2, "imdb": "tt0000002"}, "title": "Plan to watch", "status": "plantowatch"},
]

WATCHED_SHOWS = [
    {
        "ids": {"simkl": 180770, "imdb": "tt14688458"},
        "title": "Silo",
        "status": "watching",
        "seasons": [{"number": 1, "episodes": [{"number": 1}, {"number": 2}]}],
    }
]


class FakeSimkl:
    async def all_items(self, media_type):
        return WATCHED_MOVIES if media_type == "movies" else WATCHED_SHOWS


@pytest.mark.asyncio
async def test_watched_index_only_counts_completed_movies():
    movies, episodes = await watched_on_simkl(FakeSimkl())

    assert movies == {"tt5442430"}
    assert episodes == {("tt14688458", 1, 1), ("tt14688458", 1, 2)}


# -- MyShows -----------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    async def instant(*_args, **_kwargs):
        return None

    monkeypatch.setattr("kinopub_simkl_sync.push.asyncio.sleep", instant)


def _episode(imdb: str, season: int, episode: int, title: str = "Show") -> EpisodeWatch:
    return EpisodeWatch(
        kinopub_id=1, title=title, year=2020, imdb=imdb, season=season, episode=episode, watched_at="unknown"
    )


class FakeMyShows:
    def __init__(self, shows: dict[str, dict | None]):
        self.shows = shows
        self.synced: list[tuple[int, list[int]]] = []

    async def show_by_imdb(self, imdb: str) -> dict | None:
        return self.shows.get(imdb)

    async def sync_episodes(self, show_id: int, episode_ids: list[int]) -> bool:
        self.synced.append((show_id, episode_ids))
        return True


def test_group_episodes_by_imdb():
    episodes = [_episode("tt1", 1, 1), _episode("tt1", 1, 2), _episode("tt2", 1, 1)]

    groups = group_episodes_by_imdb(episodes)

    assert set(groups) == {"tt1", "tt2"}
    assert len(groups["tt1"]) == 2
    assert len(groups["tt2"]) == 1


@pytest.mark.asyncio
async def test_push_myshows_matches_and_records_new_episodes(tmp_path):
    episodes = [_episode("tt1", 1, 1, "Show A"), _episode("tt1", 1, 2, "Show A")]
    show = {
        "id": 100,
        "episodes": [
            {"seasonNumber": 1, "episodeNumber": 1, "id": 900},
            {"seasonNumber": 1, "episodeNumber": 2, "id": 901},
        ],
    }
    client = FakeMyShows({"tt1": show})
    state = PushState()
    state_path = tmp_path / "push_state_myshows.json"

    await push_myshows(Plan(episodes=episodes), client, state, state_path, dry_run=False)

    assert client.synced == [(100, [900, 901])]
    assert {e.state_key for e in episodes} <= set(state.pushed)
    assert state_path.exists()


@pytest.mark.asyncio
async def test_push_myshows_skips_shows_already_fully_synced(tmp_path):
    episode = _episode("tt1", 1, 1, "Show A")
    show = {"id": 100, "episodes": [{"seasonNumber": 1, "episodeNumber": 1, "id": 900}]}
    client = FakeMyShows({"tt1": show})
    state = PushState()
    state.record([episode])
    state_path = tmp_path / "push_state_myshows.json"

    await push_myshows(Plan(episodes=[episode]), client, state, state_path, dry_run=False)

    assert client.synced == []


@pytest.mark.asyncio
async def test_push_myshows_records_unmatched_show_and_episode(tmp_path):
    matched = _episode("tt1", 1, 1, "Show A")
    unmatched_episode = _episode("tt1", 1, 99, "Show A")  # no such episode on myshows
    unmatched_show = _episode("tt2", 1, 1, "Show B")  # show not found on myshows
    show = {"id": 100, "episodes": [{"seasonNumber": 1, "episodeNumber": 1, "id": 900}]}
    client = FakeMyShows({"tt1": show, "tt2": None})
    state = PushState()
    state_path = tmp_path / "push_state_myshows.json"

    await push_myshows(
        Plan(episodes=[matched, unmatched_episode, unmatched_show]), client, state, state_path, dry_run=False
    )

    assert client.synced == [(100, [900])]
    assert matched.state_key in state.pushed
    assert unmatched_episode.state_key not in state.pushed
    reasons = {(entry.get("imdb"), entry.get("reason")) for entry in state.not_found}
    assert ("tt1", "episode not found on myshows") in reasons
    assert ("tt2", "show not found on myshows") in reasons


@pytest.mark.asyncio
async def test_push_myshows_dry_run_makes_no_calls(tmp_path):
    episode = _episode("tt1", 1, 1, "Show A")
    client = FakeMyShows({"tt1": {"id": 100, "episodes": []}})
    state = PushState()
    state_path = tmp_path / "push_state_myshows.json"

    await push_myshows(Plan(episodes=[episode]), client, state, state_path, dry_run=True)

    assert client.synced == []
    assert not state_path.exists()


# -- Floppy -------------------------------------------------------------


def _movie(imdb: str, kinopub_id: int, watched_at: str = "2024-06-01T20:00:00.000Z") -> MovieWatch:
    return MovieWatch(kinopub_id=kinopub_id, title="Movie", year=2020, imdb=imdb, watched_at=watched_at)


def _fp_episode(
    imdb: str, kinopub_id: int, season: int, episode: int, watched_at: str = "2024-06-01T20:00:00.000Z"
) -> EpisodeWatch:
    return EpisodeWatch(
        kinopub_id=kinopub_id,
        title="Show",
        year=2020,
        imdb=imdb,
        season=season,
        episode=episode,
        watched_at=watched_at,
    )


class FakeFloppy:
    def __init__(
        self,
        tmdb_ids: dict[tuple[str, str], int | None],
        *,
        missing_movies: frozenset[int] = frozenset(),
        missing_episodes: frozenset[tuple[int, int, int]] = frozenset(),
    ):
        self.tmdb_ids = tmdb_ids
        self.missing_movies = missing_movies
        self.missing_episodes = missing_episodes
        self.movie_watches: list[tuple[int, str | None, str]] = []
        self.episode_watches: list[tuple[int, int, int, str | None, str]] = []

    async def resolve_tmdb_id(self, kind: str, imdb_id: str) -> int | None:
        return self.tmdb_ids.get((kind, imdb_id))

    async def watch_movie(self, tmdb_id: int, *, end_date: str | None, external_id: str) -> None:
        if tmdb_id in self.missing_movies:
            raise FloppyNotFoundError("Could not resolve movie.")
        self.movie_watches.append((tmdb_id, end_date, external_id))

    async def watch_episode(
        self, tmdb_id: int, season: int, episode: int, *, end_date: str | None, external_id: str
    ) -> None:
        if (tmdb_id, season, episode) in self.missing_episodes:
            raise FloppyNotFoundError("Episode not found.")
        self.episode_watches.append((tmdb_id, season, episode, end_date, external_id))


@pytest.mark.asyncio
async def test_push_floppy_pushes_movie_with_resolved_tmdb_id(tmp_path):
    movie = _movie("tt5442430", 42)
    client = FakeFloppy({("movie", "tt5442430"): 641})
    state = PushState()
    state_path = tmp_path / "push_state_floppy.json"

    await push_floppy(Plan(movies=[movie]), client, state, state_path, dry_run=False)

    assert client.movie_watches == [(641, "2024-06-01T20:00:00.000Z", "kinopub:42")]
    assert movie.state_key in state.pushed


@pytest.mark.asyncio
async def test_push_floppy_omits_end_date_for_unknown_watched_at(tmp_path):
    movie = _movie("tt5442430", 42, watched_at="unknown")
    client = FakeFloppy({("movie", "tt5442430"): 641})
    state = PushState()
    state_path = tmp_path / "push_state_floppy.json"

    await push_floppy(Plan(movies=[movie]), client, state, state_path, dry_run=False)

    assert client.movie_watches == [(641, None, "kinopub:42")]


@pytest.mark.asyncio
async def test_push_floppy_pushes_episodes_grouped_by_show(tmp_path):
    episodes = [_fp_episode("tt1", 1, 1, 1), _fp_episode("tt1", 1, 1, 2)]
    client = FakeFloppy({("tv", "tt1"): 1400})
    state = PushState()
    state_path = tmp_path / "push_state_floppy.json"

    await push_floppy(Plan(episodes=episodes), client, state, state_path, dry_run=False)

    assert client.episode_watches == [
        (1400, 1, 1, "2024-06-01T20:00:00.000Z", "kinopub:1:1:1"),
        (1400, 1, 2, "2024-06-01T20:00:00.000Z", "kinopub:1:1:2"),
    ]
    assert {e.state_key for e in episodes} <= set(state.pushed)


@pytest.mark.asyncio
async def test_push_floppy_records_unmatched_movie_without_tmdb_id(tmp_path):
    movie = _movie("tt9999999", 1)
    client = FakeFloppy({("movie", "tt9999999"): None})
    state = PushState()
    state_path = tmp_path / "push_state_floppy.json"

    await push_floppy(Plan(movies=[movie]), client, state, state_path, dry_run=False)

    assert client.movie_watches == []
    assert movie.state_key not in state.pushed
    assert state.not_found == [
        {"imdb": "tt9999999", "title": "Movie", "reason": "no tmdb match for this imdb id"}
    ]


@pytest.mark.asyncio
async def test_push_floppy_records_unmatched_show_for_all_its_episodes(tmp_path):
    episodes = [_fp_episode("tt2", 1, 1, 1), _fp_episode("tt2", 1, 1, 2)]
    client = FakeFloppy({("tv", "tt2"): None})
    state = PushState()
    state_path = tmp_path / "push_state_floppy.json"

    await push_floppy(Plan(episodes=episodes), client, state, state_path, dry_run=False)

    assert client.episode_watches == []
    assert len(state.not_found) == 2
    assert all(entry["reason"] == "no tmdb match for this imdb id" for entry in state.not_found)


@pytest.mark.asyncio
async def test_push_floppy_records_floppy_404_episode_and_continues(tmp_path):
    episodes = [_fp_episode("tt1", 1, 0, 1), _fp_episode("tt1", 1, 1, 1), _fp_episode("tt3", 3, 1, 1)]
    client = FakeFloppy(
        {("tv", "tt1"): 1400, ("tv", "tt3"): 1500}, missing_episodes=frozenset({(1400, 0, 1)})
    )
    state = PushState()
    state_path = tmp_path / "push_state_floppy.json"

    await push_floppy(Plan(episodes=episodes), client, state, state_path, dry_run=False)

    assert [(w[0], w[1], w[2]) for w in client.episode_watches] == [(1400, 1, 1), (1500, 1, 1)]
    assert episodes[0].state_key not in state.pushed
    assert {episodes[1].state_key, episodes[2].state_key} <= set(state.pushed)
    assert state.not_found == [
        {
            "imdb": "tt1",
            "tmdb": 1400,
            "title": "Show",
            "season": 0,
            "episode": 1,
            "reason": "floppy 404: Episode not found.",
        }
    ]
    assert PushState.load(state_path).not_found == state.not_found


@pytest.mark.asyncio
async def test_push_floppy_records_floppy_404_movie_and_continues(tmp_path):
    missing, ok = _movie("tt1", 1), _movie("tt2", 2)
    client = FakeFloppy({("movie", "tt1"): 10, ("movie", "tt2"): 20}, missing_movies=frozenset({10}))
    state = PushState()
    state_path = tmp_path / "push_state_floppy.json"

    await push_floppy(Plan(movies=[missing, ok]), client, state, state_path, dry_run=False)

    assert [w[0] for w in client.movie_watches] == [20]
    assert missing.state_key not in state.pushed
    assert state.not_found == [
        {"imdb": "tt1", "tmdb": 10, "title": "Movie", "reason": "floppy 404: Could not resolve movie."}
    ]


@pytest.mark.asyncio
async def test_push_floppy_does_not_duplicate_not_found_on_rerun(tmp_path):
    episodes = [_fp_episode("tt1", 1, 0, 1)]
    client = FakeFloppy({("tv", "tt1"): 1400}, missing_episodes=frozenset({(1400, 0, 1)}))
    state = PushState()
    state_path = tmp_path / "push_state_floppy.json"

    await push_floppy(Plan(episodes=episodes), client, state, state_path, dry_run=False)
    await push_floppy(Plan(episodes=episodes), client, state, state_path, dry_run=False)

    assert len(state.not_found) == 1


@pytest.mark.asyncio
async def test_push_floppy_skips_already_pushed_entries(tmp_path):
    movie = _movie("tt5442430", 42)
    client = FakeFloppy({("movie", "tt5442430"): 641})
    state = PushState()
    state.record([movie])
    state_path = tmp_path / "push_state_floppy.json"

    await push_floppy(Plan(movies=[movie]), client, state, state_path, dry_run=False)

    assert client.movie_watches == []


@pytest.mark.asyncio
async def test_push_floppy_dry_run_makes_no_calls(tmp_path):
    movie = _movie("tt5442430", 42)
    client = FakeFloppy({("movie", "tt5442430"): 641})
    state = PushState()
    state_path = tmp_path / "push_state_floppy.json"

    await push_floppy(Plan(movies=[movie]), client, state, state_path, dry_run=True)

    assert client.movie_watches == []
    assert not state_path.exists()
