# kinopub-simkl-sync

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.13+](https://img.shields.io/badge/python-3.13%2B-blue.svg)](https://www.python.org/downloads/)

Sync watch history from [kino.pub](https://kino.pub) to [Simkl](https://simkl.com):
every fully watched movie and episode, with original watch timestamps. Safe to
run once for a one-time migration or on a recurring schedule (cron) to keep
Simkl current — `kts pull` after the first run only fetches what changed.

Simkl accepts IMDb ids directly — the same identity kino.pub stores for nearly
every item — so there is no episode-identity reconciliation step: a kino.pub
season maps onto a Simkl season one to one. Items without an IMDb id land in
`unmatched` for manual review rather than being guessed.

Optionally, watched episodes can also be pushed to
[MyShows](https://myshows.me) with `--myshows` (see
[Pushing to MyShows](#pushing-to-myshows-optional) below) — it is off by
default because MyShows can't preserve the original watch dates the way Simkl
does. Movies and episodes can also be pushed to a self-hosted
[Floppy](https://github.com/dannyvfilms/Floppy) instance with `--floppy` (see
[Pushing to Floppy](#pushing-to-floppy-optional)) — off by default since it
needs its own app registration and a TMDB API key.

## Setup

```bash
uv sync
```

Python 3.13+, `uv` for everything. The pipeline is async end to end; API
payloads are validated into pydantic models at the boundary, so anything
downstream of a client is typed.

```bash
uv run pytest && uv run ruff check src tests && uv run basedpyright
```

kino.pub needs no registration: its device-code flow authorizes on your own
sign-in, so it ships with the public client credentials used by the `xbmc`
open-source client. Simkl has no such public client — register an app at
<https://simkl.com/settings/developer> with the standard OAuth2 flow enabled
and a redirect URI of `http://localhost:8000/callback` (or whatever you set
`SIMKL_REDIRECT_URI` to below — it must match byte-for-byte), then put its
client id/secret into `.env`:

```
SIMKL_CLIENT_ID=...
SIMKL_CLIENT_SECRET=...
# only needed if you registered the app with a non-default redirect URI
SIMKL_REDIRECT_URI=http://localhost:8000/callback
```

If `api.service-kp.com` is blocked on your network, point kino.pub at a
mirror instead (path layout is the same under `/oauth2/*` and `/v1/*`):

```
KINOPUB_API_URL=https://cdn-service.online/api
```

MyShows needs no app registration either — its OAuth password grant only
needs your own account's login/password, sent alongside a public "playground"
client id/secret MyShows itself embeds in its API doc page (see
[Pushing to MyShows](#pushing-to-myshows-optional)):

```
MYSHOWS_USERNAME=...
MYSHOWS_PASSWORD=...
```

Floppy is self-hosted, so there is no default host: get an API token from
your instance's Settings -> Integrations, and a free TMDB API key from
<https://www.themoviedb.org/settings/api> (Floppy's watch endpoints only
match movies/shows by TMDB id, not IMDb — see
[Pushing to Floppy](#pushing-to-floppy-optional) for why):

```
FLOPPY_URL=https://floppy.example.com
FLOPPY_API_TOKEN=...
TMDB_API_KEY=...
```

## Usage

Authorize once, then sync:

```bash
uv run kts auth kinopub   # prints a code to enter at kino.pub/device
uv run kts auth simkl     # opens simkl.com in your browser to authorize
uv run kts sync --dry-run # pull, plan, and report what would be pushed
uv run kts sync           # the whole pipeline
uv run kts status         # local state: dump age, pending pushes, authorization
```

The individual stages exist for when one needs inspecting on its own:

```bash
uv run kts pull                  # dump history + per-item progress -> data/kinopub_dump.json
uv run kts plan                  # build sync plan + summary -> data/sync_plan.json
uv run kts plan --export out.json  # also write the plan to an explicit path
uv run kts push --all            # or --movies / --shows
uv run kts push --all --dry-run  # report what would be pushed, write nothing
uv run kts push --myshows        # additionally/separately: watched episodes -> MyShows
uv run kts push --floppy         # additionally/separately: movies + episodes -> Floppy
```

**Re-authorization deadline:** kino.pub invalidates a refresh token after 30
days of disuse, after which `kts auth kinopub` is required again. Simkl's
OAuth2 v2 access token lasts 7 days and refreshes automatically; its refresh
token lasts 180 days and resets on each use, so only 180 days of the sync
never running requires `kts auth simkl` again. `kts status` reports both.

**Running `kts auth simkl` on a headless/remote server:** it starts a
loopback listener on `SIMKL_REDIRECT_URI` (`localhost:8000` by default) on
the machine that runs the command, then prints a URL to open in a browser.
If you open that URL on your *local* machine instead, Simkl's redirect back
to `localhost:8000` resolves on your local machine too — where nothing is
listening — so the callback never reaches the server and the command times
out after 5 minutes (`timed out waiting for browser authorization`). Forward
the port over SSH first, then run the command inside that same SSH session
and open the printed URL locally as usual:

```bash
ssh -L 8000:localhost:8000 user@your-server
# inside that session:
uv run kts auth simkl
```

## Behavior notes

- **Idempotent.** Simkl does not dedupe history plays, so every pushed entry is
  recorded in `data/push_state.json` and skipped on re-runs. Items already
  watched on the Simkl account (via `/sync/all-items`) are skipped too.
- **Incremental pull.** `kts pull` always fetches every history page on the
  first run. On later runs, kino.pub's `/v1/history` is sorted newest-first by
  `last_seen`, so pagination stops as soon as it reaches a record already seen
  in the previous dump; per-item watch state is only re-fetched for items that
  actually moved (plus the watchlist and in-progress-but-historyless movies,
  which are always cheap to re-check). A re-run with nothing new takes seconds
  instead of the tens of minutes a full pull takes on a large account.
- **Timestamps.** `watched_at` comes from the kino.pub per-episode `updated`
  field (unix time of the last status change). When kino.pub has no timestamp,
  the entry is sent with `watched_at: "unknown"` — Simkl marks it watched
  without a date.
- **Reads are sequential, one request at a time.** kino.pub rate-limits by
  request rate, not just concurrency — firing requests in parallel (even a
  handful) reliably trips a 429 that a swarm of in-flight requests can't back
  off from cleanly. History pages and per-item watch-state lookups are
  fetched one at a time, with retry-with-backoff on transient errors and 429s.
  This makes a full pull slow (large accounts can take 15+ minutes) but
  reliable; the incremental pull above is what makes repeat runs fast.
- History is pushed to Simkl in batches of 500 entries, with a short pause
  between requests.
- **`push_state.json` key formats are a compatibility surface** — they are
  what makes a re-run idempotent on an already synced account. `models.py`
  owns them and a test locks them.
- Tokens live in `data/tokens.json` (chmod 600); the whole `data/` dir is gitignored.
- **MyShows keeps its own state file**, `data/push_state_myshows.json` —
  separate from Simkl's, since it is a different service with different
  idempotency rules (see [Pushing to MyShows](#pushing-to-myshows-optional)).
- **Floppy keeps its own state file too**, `data/push_state_floppy.json`, plus
  `data/tmdb_cache.json` for resolved imdb -> tmdb ids (see
  [Pushing to Floppy](#pushing-to-floppy-optional)).

## Pushing to MyShows (optional)

`--myshows` pushes watched *episodes* to [MyShows](https://myshows.me),
matched by IMDb id the same way as Simkl. It is opt-in — pass it to `push` or
`sync` explicitly — because of two hard limits in MyShows' own API, not this
tool:

- **No movies.** MyShows is a TV-show tracker; its API has no movie endpoints
  at all. `--myshows` only ever touches `plan.episodes`.
- **No historical watch dates.** Every MyShows write endpoint
  (`manage.CheckEpisode`, `manage.SyncEpisodes`) dates the episode "now",
  server-side — there is no `watched_at`-equivalent field anywhere in the
  API. Episodes pushed to MyShows will show today's date, not when you
  actually watched them on kino.pub.

Authorization is a plain OAuth2 password grant (`MYSHOWS_USERNAME`/
`MYSHOWS_PASSWORD` from `.env`, see Setup) — no device flow, no persisted
token; the client logs in once per run and holds the token in memory only.

Because `manage.SyncEpisodes`'s replace-vs-add semantics for a show's watched
episodes aren't documented, every push resends a show's *complete* known-watched
episode list from the plan rather than just what changed since last time —
correct regardless of which way that method actually behaves. `push_state_myshows.json`
only decides which shows are worth resolving and re-sending on a given run
(skip a show once every episode currently in the plan is already recorded),
not what gets sent once a show is due for a write.

## Pushing to Floppy (optional)

`--floppy` pushes watched *movies and episodes* to a self-hosted
[Floppy](https://github.com/dannyvfilms/Floppy) instance, with original watch
dates preserved — same as Simkl, unlike MyShows.

Floppy's own watch endpoints only match a movie or show by a `tmdb` (or
`manual`) source id — there is no `imdb` source for movies/shows in its API.
Since kino.pub only ever gives us an IMDb id, every push resolves it to a TMDB
id first, via TMDB's public "find by external id" endpoint
(`GET /3/find/{imdb_id}?external_source=imdb_id`, needs `TMDB_API_KEY`, see
Setup). Resolved ids (and confirmed non-matches, so an untracked title isn't
re-queried every run) are cached in `data/tmdb_cache.json`. A title with no
TMDB listing lands in `push_state_floppy.json`'s `not_found` list rather than
being skipped silently.

Each watch call also carries a stable `external_id` derived from the kino.pub
item id (`kinopub:{id}` for a movie, `kinopub:{id}:{season}:{episode}` for an
episode) — Floppy uses it server-side to make a replayed call idempotent
instead of appending a duplicate play, on top of the local
`push_state_floppy.json` skip that avoids the request entirely on a re-run.

Unlike Simkl and MyShows, there is no per-request pause between writes:
Floppy is an instance you control, not a shared service with a remote rate
limit to be gentle with.

## Running on a schedule (cron)

`kts sync` (pull + plan + push) is safe to run unattended once both services
are authorized — the incremental pull and the idempotent push mean a
no-op run costs a handful of requests and does not double-push anything.

```cron
# /etc/cron.d/kinopub-simkl-sync, or `crontab -e` for your user
# Every 6 hours. Adjust the path and schedule to taste.
0 */6 * * * cd /opt/kinopub-simkl-sync && /path/to/uv run kts sync >> data/sync.log 2>&1
```

Notes:

- Authorize both services interactively **before** adding the cron job
  (`kts auth kinopub`, `kts auth simkl`) — cron cannot complete a device-code
  flow or a browser-based OAuth2 redirect. `kts status` confirms both tokens
  are valid first. (MyShows and Floppy need no such step —
  `MYSHOWS_USERNAME`/`MYSHOWS_PASSWORD` and `FLOPPY_URL`/`FLOPPY_API_TOKEN`/
  `TMDB_API_KEY` in `.env` are enough for `--myshows`/`--floppy` to work
  unattended too.)
- `kts auth kinopub`/`kts auth simkl` must be re-run manually if the account
  goes 30 (kino.pub) or 180 (Simkl) days without a successful token refresh
  (i.e. the cron job itself stops running for that long) — see the
  re-authorization note above. There is no unattended recovery from an
  expired refresh token by design: kino.pub has no client
  secret worth protecting server-side, but re-issuing a device code still
  requires visiting kino.pub once.
- Use an absolute path to `uv` in the crontab line (`which uv`) — cron does
  not source your shell profile, so `PATH` is minimal.
- `cd` into the project directory first (as above), or pass `uv run --project
  /opt/kinopub-simkl-sync kts sync` — `uv run` needs to find `pyproject.toml`.
  `.env` and `data/` are resolved against the installed package's own
  location regardless of the working directory, so this is only about `uv`
  finding the project, not about config/data discovery.
- Redirect output to a log file (`>> data/sync.log 2>&1` above); cron mails
  stdout/stderr to the crontab owner by default, which gets noisy fast.

## What is intentionally not transferred

- **Playback progress and the kino.pub watchlist** — the plan (`kts plan`)
  still records them for informational summary/status purposes, but nothing
  pushes them to Simkl.
- **Ratings/votes** — the kino.pub API does not expose the user's own votes at
  all: voting is write-only (`/v1/items/vote?id=&like=`, binary like/dislike),
  item payloads carry only community stats and no `my vote` field. There is
  nothing to read.
- **`counter` from history** — it is a technical player-access counter, not a
  view count: one 90-minute episode shows counter=339 within a single day,
  ordinary single-sitting movies show 1–2. kino.pub has no rewatch data at
  all (history keeps one record per episode/video; re-watching just bumps
  last_seen/counter), so mapping counter to Simkl rewatches would fabricate
  history.
- **`first_seen`** — one watch event per item is recorded; the completion
  moment (`updated`) is what "watched at" means.

## kino.pub API surface (probed, not just documented)

The API was probed beyond its docs (~130 candidate paths). **Mutations in this
API are performed via GET** (`/v1/items/vote`, `/v1/watching/toggle`,
`/v1/watching/marktime`, `/v1/history/clear-*`), so any probing must exclude
write-verb paths — a blind sweep corrupts the account.

Result: no undocumented personal-data endpoint exists. Everything carrying a
user signal is already consumed by `pull`:

| Endpoint | Personal signal |
|---|---|
| `/v1/history` | per item: `time`, `first_seen`, `last_seen`, `counter` |
| `/v1/watching?id=` | per episode: `status`, `time`, `updated` |
| `/v1/watching/movies`, `/v1/watching/serials` | unfinished items, watchlist |
| `/v1/bookmarks`, item field `bookmarks` | user-created folders |
| `/v1/user` | username, reg date, subscription, `show_erotic`/`show_uncertain` |
| `/v1/device` | device list with `last_seen` — session metadata, not watch data |

Absent everywhere: the user's own votes (item payloads expose only community
`rating`/`rating_percentage`/`rating_votes`; `views` is global), any per-user
play count, and any rewatch log. `/v1/history` ignores `type`/`media` filters
and caps `perpage` at 50. No `/v2`, no stats, notifications, or
recommendations endpoints. `/v1/items/{id}` additionally serves `similar`,
`comments`, and `trailer` — all catalog data.

Field semantics worth knowing when reading the code: `/v1/watching?id=` reports
per-episode `status` as `-1` unwatched, `0` in progress, `1` watched, alongside
`time` (position in seconds) and `updated` (unix time of the last status
change). Authorization is an OAuth2 device-code flow against `/oauth2/device`.
The official documentation lives at <https://kinoapi.com>.

## Acknowledgments

Started as a fork of [kinopub-trakt-sync](https://github.com/asbabiy/kinopub-trakt-sync)
by [Alexander Babiy](https://github.com/asbabiy) — thank you for the original
kino.pub client and project structure this builds on. This fork moved the
sync target from Trakt to Simkl, made the kino.pub pull incremental, and
added optional MyShows and Floppy push targets.

## License

MIT — see [LICENSE](LICENSE). Not affiliated with kino.pub, Simkl, MyShows, or Floppy.
