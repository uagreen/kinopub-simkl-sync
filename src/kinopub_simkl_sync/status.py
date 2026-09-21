"""Local health check: what the files on disk say, without touching any API.

Answers the three questions a scheduled sync raises between runs: how fresh is
the dump, what is still waiting to be pushed, and will authorization survive
until the next run.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

from pydantic import BaseModel

from .models import Plan, PlanStats
from .push import PushState
from .settings import Settings
from .storage import read_json, read_model

# kino.pub invalidates a refresh token after 30 days of disuse; Simkl's OAuth2
# v2 refresh token lasts 180 days and resets on each use. Past either deadline
# only a fresh interactive authorization helps.
KINOPUB_REFRESH_DAYS = 30
SIMKL_REFRESH_DAYS = 180
REFRESH_DAYS = {"kinopub": KINOPUB_REFRESH_DAYS, "simkl": SIMKL_REFRESH_DAYS}
DAY_SECONDS = 86400


class TokenStatus(BaseModel):
    service: str
    authorized_at: str
    access_valid: bool
    refresh_deadline: str | None = None
    refresh_days_left: int | None = None


class Status(BaseModel):
    pulled_at: str | None = None
    dump_age_days: int | None = None
    plan: PlanStats | None = None
    pending: dict[str, int] = {}
    tokens: list[TokenStatus] = []

    @property
    def pending_total(self) -> int:
        return sum(self.pending.values())


def _moment(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=UTC).strftime("%Y-%m-%d %H:%M UTC")


class StoredToken(BaseModel):
    """The fields of data/tokens.json that say anything about validity."""

    obtained_at: float = 0
    expires_in: float = 0


def _token_status(service: str, raw: object) -> TokenStatus | None:
    if not isinstance(raw, dict):
        return None
    token = StoredToken.model_validate(raw)
    obtained_at = token.obtained_at
    expires_in = token.expires_in
    status = TokenStatus(
        service=service,
        authorized_at=_moment(obtained_at),
        access_valid=time.time() < obtained_at + expires_in,
    )
    if (refresh_days := REFRESH_DAYS.get(service)) is not None:
        deadline = obtained_at + refresh_days * DAY_SECONDS
        status.refresh_deadline = _moment(deadline)
        status.refresh_days_left = int((deadline - time.time()) // DAY_SECONDS)
    return status


def collect(settings: Settings) -> Status:
    status = Status()

    dump = read_json(settings.paths.dump)
    if isinstance(dump, dict) and dump.get("pulled_at"):
        status.pulled_at = _moment(dump["pulled_at"])
        status.dump_age_days = int((time.time() - dump["pulled_at"]) // DAY_SECONDS)

    plan = read_model(settings.paths.plan, Plan)
    if plan is not None:
        status.plan = plan.stats
        pushed = set(PushState.load(settings.paths.push_state).pushed)
        status.pending = {
            "movies": sum(1 for entry in plan.movies if entry.state_key not in pushed),
            "episodes": sum(1 for entry in plan.episodes if entry.state_key not in pushed),
        }

    tokens = read_json(settings.paths.tokens, default={})
    for service in ("kinopub", "simkl"):
        if (token := _token_status(service, tokens.get(service))) is not None:
            status.tokens.append(token)
    return status


def _token_detail(token: TokenStatus) -> str:
    if token.refresh_days_left is not None:
        # An expired access token still auto-refreshes, up to the refresh-token
        # deadline (both kino.pub and Simkl work this way now).
        if token.refresh_days_left < 0:
            return f"re-authorization needed — run: kts auth {token.service}"
        detail = "access token valid" if token.access_valid else "access token expired (auto-refreshes)"
        return f"{detail}, re-auth needed after {token.refresh_deadline} ({token.refresh_days_left}d left)"

    if token.access_valid:
        return "access token valid"
    return f"access token expired — run: kts auth {token.service}"


def format_status(status: Status) -> str:
    lines: list[str] = []

    if status.pulled_at is None:
        lines.append("dump:    none yet — run: kts pull")
    else:
        lines.append(f"dump:    {status.pulled_at} ({status.dump_age_days}d ago)")

    if status.plan is None:
        lines.append("plan:    none yet — run: kts plan")
    else:
        plan = status.plan
        lines.append(
            f"plan:    {plan.movies} movies, {plan.episodes} episodes across {plan.shows} shows, "
            f"{plan.progress} in progress, {plan.unmatched} unmatched"
        )
        if status.pending_total:
            pending = ", ".join(f"{count} {name}" for name, count in status.pending.items() if count)
            lines.append(f"pending: {pending}")
        else:
            lines.append("pending: nothing — everything in the plan reached Simkl")

    if not status.tokens:
        lines.append("auth:    none — run: kts auth kinopub && kts auth simkl")
    for token in status.tokens:
        lines.append(f"auth:    {token.service}: {_token_detail(token)}")

    return "\n".join(lines)
