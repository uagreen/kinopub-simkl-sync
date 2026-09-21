import time

from kinopub_simkl_sync import status as status_module
from kinopub_simkl_sync.models import EpisodeWatch, MovieWatch, Plan
from kinopub_simkl_sync.push import PushState
from kinopub_simkl_sync.settings import Paths, Settings
from kinopub_simkl_sync.storage import write_json, write_model

DAY = 86400


def _settings(tmp_path):
    return Settings(paths=Paths(data_dir=tmp_path))


def _plan():
    return Plan(
        movies=[MovieWatch(kinopub_id=1, title="m", imdb="tt1", watched_at="unknown")],
        episodes=[
            EpisodeWatch(kinopub_id=2, title="s", imdb="tt2", season=1, episode=1, watched_at="unknown"),
            EpisodeWatch(kinopub_id=2, title="s", imdb="tt2", season=1, episode=2, watched_at="unknown"),
        ],
    )


def test_status_on_an_empty_directory(tmp_path):
    report = status_module.collect(_settings(tmp_path))
    assert report.pulled_at is None and report.plan is None and report.tokens == []

    text = status_module.format_status(report)
    assert "run: kts pull" in text
    assert "run: kts auth kinopub" in text


def test_pending_counts_only_unpushed_entries(tmp_path):
    settings = _settings(tmp_path)
    plan = _plan()
    write_model(settings.paths.plan, plan)

    state = PushState()
    state.record([plan.movies[0], plan.episodes[0]])
    state.save(settings.paths.push_state)

    report = status_module.collect(settings)
    assert report.pending == {"movies": 0, "episodes": 1}
    assert report.pending_total == 1
    assert "1 episodes" in status_module.format_status(report)


def test_everything_pushed_reads_as_clean(tmp_path):
    settings = _settings(tmp_path)
    plan = _plan()
    write_model(settings.paths.plan, plan)
    state = PushState()
    state.record([*plan.movies, *plan.episodes])
    state.save(settings.paths.push_state)

    text = status_module.format_status(status_module.collect(settings))
    assert "pending: nothing" in text


def test_kinopub_refresh_deadline_is_reported(tmp_path):
    settings = _settings(tmp_path)
    now = time.time()
    write_json(
        settings.paths.tokens,
        {
            "kinopub": {"obtained_at": now - 25 * DAY, "expires_in": 3600},
            "simkl": {"obtained_at": now, "expires_in": 604800},
        },
    )

    report = status_module.collect(settings)
    kinopub = next(token for token in report.tokens if token.service == "kinopub")
    simkl = next(token for token in report.tokens if token.service == "simkl")

    assert kinopub.refresh_days_left == 4  # 30-day window, 25 days used
    assert kinopub.access_valid is False  # one-hour access token, long expired
    assert simkl.access_valid is True
    assert simkl.refresh_days_left == 179  # OAuth2 v2 refresh token: fresh 180-day window


def test_simkl_refresh_deadline_is_reported(tmp_path):
    settings = _settings(tmp_path)
    write_json(
        settings.paths.tokens,
        {"simkl": {"obtained_at": time.time() - 170 * DAY, "expires_in": 604800}},
    )

    report = status_module.collect(settings)
    simkl = next(token for token in report.tokens if token.service == "simkl")

    assert simkl.refresh_days_left == 9  # 180-day window, 170 days used
    assert simkl.access_valid is False  # 7-day access token, long expired


def test_expired_simkl_refresh_asks_for_reauthorization(tmp_path):
    settings = _settings(tmp_path)
    write_json(
        settings.paths.tokens,
        {"simkl": {"obtained_at": time.time() - 200 * DAY, "expires_in": 604800}},
    )

    text = status_module.format_status(status_module.collect(settings))
    assert "run: kts auth simkl" in text


def test_expired_kinopub_refresh_asks_for_reauthorization(tmp_path):
    settings = _settings(tmp_path)
    write_json(
        settings.paths.tokens,
        {"kinopub": {"obtained_at": time.time() - 40 * DAY, "expires_in": 3600}},
    )

    text = status_module.format_status(status_module.collect(settings))
    assert "run: kts auth kinopub" in text


def test_dump_age(tmp_path):
    settings = _settings(tmp_path)
    write_json(settings.paths.dump, {"pulled_at": int(time.time() - 3 * DAY)})

    report = status_module.collect(settings)
    assert report.dump_age_days == 3
    assert "3d ago" in status_module.format_status(report)
