"""Runtime configuration and on-disk locations."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]

SIMKL_API = "https://api.simkl.com"


class Paths(BaseModel):
    data_dir: Path = PROJECT_ROOT / "data"

    @property
    def tokens(self) -> Path:
        return self.data_dir / "tokens.json"

    @property
    def dump(self) -> Path:
        return self.data_dir / "kinopub_dump.json"

    @property
    def plan(self) -> Path:
        return self.data_dir / "sync_plan.json"

    @property
    def push_state(self) -> Path:
        return self.data_dir / "push_state.json"

    @property
    def push_state_myshows(self) -> Path:
        return self.data_dir / "push_state_myshows.json"

    @property
    def push_state_floppy(self) -> Path:
        return self.data_dir / "push_state_floppy.json"

    @property
    def tmdb_cache(self) -> Path:
        return self.data_dir / "tmdb_cache.json"


class Settings(BaseSettings):
    """Environment-driven configuration; `.env` seeds anything unset.

    kino.pub needs no registration: its device-code flow authenticates on the
    user's own authorization, so reusing the public client id of an existing
    open-source client (xbmc) sidesteps app creation entirely. Simkl has no
    such public client — an app must be registered at
    simkl.com/settings/developer, and its client id is required on every
    request via the `simkl-api-key` header.
    """

    model_config = SettingsConfigDict(env_file=PROJECT_ROOT / ".env", extra="ignore")

    kinopub_client_id: str = "xbmc"
    kinopub_client_secret: str = "cgg3gtifu46urtfp2zp1nqtba0k2ezxh"
    # Some networks block api.service-kp.com outright; override with a mirror
    # (e.g. https://cdn-service.online/api, path layout differs slightly but
    # /oauth2/* and /v1/* both resolve under it) via KINOPUB_API_URL in .env.
    kinopub_api_url: str = "https://api.service-kp.com"

    simkl_client_id: str = ""
    simkl_client_secret: str = ""
    # Must match the redirect URI registered for the app at
    # simkl.com/settings/developer byte-for-byte. `kts auth simkl` briefly
    # listens on this loopback address to catch the OAuth2 redirect.
    simkl_redirect_uri: str = "http://localhost:8000/callback"

    # MyShows has no device-code flow — password grant only. "apidoc"/"apidoc"
    # is the public playground client MyShows itself embeds in its Swagger doc
    # page (api.myshows.me/shared/doc/); third-party clients reuse it the same
    # way kino.pub's "xbmc" client id gets reused.
    myshows_client_id: str = "apidoc"
    myshows_client_secret: str = "apidoc"
    myshows_username: str = ""
    myshows_password: str = ""

    # Floppy is self-hosted, so there is no default host — the base URL of
    # your own instance and an API token from Settings -> Integrations.
    floppy_url: str = ""
    floppy_api_token: str = ""
    # Floppy's watch endpoints only accept tmdb or manual as a source; kino.pub
    # only has imdb ids, so a free TMDB API key resolves imdb -> tmdb before
    # every push (see floppy.py). Get one at themoviedb.org/settings/api.
    tmdb_api_key: str = ""

    paths: Paths = Field(default_factory=Paths)

    @property
    def kinopub_device_url(self) -> str:
        return f"{self.kinopub_api_url}/oauth2/device"

    @property
    def kinopub_token_url(self) -> str:
        return f"{self.kinopub_api_url}/oauth2/token"
