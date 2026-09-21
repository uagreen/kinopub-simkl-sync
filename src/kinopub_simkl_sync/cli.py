"""Command line interface.

One-off use is `kts sync`; the individual steps (pull, plan, push) exist for
when a stage needs inspecting on its own.
"""

from __future__ import annotations

import asyncio
import logging
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer

from . import status as status_module
from . import transform
from .floppy import FloppyClient, FloppyError
from .kinopub import KinopubClient, KinopubError
from .models import Dump, Plan
from .myshows import MyShowsClient, MyShowsError
from .pull import pull
from .push import PushState, push_episodes, push_floppy, push_movies, push_myshows
from .settings import Settings
from .simkl import SimklClient, SimklError
from .storage import read_model, write_model

app = typer.Typer(
    help="One-way sync of kino.pub watch history to Simkl.",
    no_args_is_help=True,
    add_completion=False,
)


class Service(StrEnum):
    KINOPUB = "kinopub"
    SIMKL = "simkl"


# -- steps, shared by the individual commands and by `sync` -----------------


async def _plan_step(settings: Settings, dump: Dump, *, export: Path | None = None) -> Plan:
    plan = transform.build_plan(dump)
    write_model(settings.paths.plan, plan)
    if export is not None:
        write_model(export, plan)
    print(transform.format_summary(plan))
    return plan


async def _push_step(
    settings: Settings, plan: Plan, *, movies: bool, shows: bool, myshows: bool, floppy: bool, dry_run: bool
) -> None:
    if movies or shows:
        state = PushState.load(settings.paths.push_state)
        async with SimklClient(settings) as client:
            if movies:
                await push_movies(plan, client, state, settings.paths.push_state, dry_run=dry_run)
            if shows:
                await push_episodes(plan, client, state, settings.paths.push_state, dry_run=dry_run)
    if myshows:
        myshows_state = PushState.load(settings.paths.push_state_myshows)
        async with MyShowsClient(settings) as client:
            await push_myshows(
                plan, client, myshows_state, settings.paths.push_state_myshows, dry_run=dry_run
            )
    if floppy:
        floppy_state = PushState.load(settings.paths.push_state_floppy)
        async with FloppyClient(settings) as client:
            await push_floppy(plan, client, floppy_state, settings.paths.push_state_floppy, dry_run=dry_run)


def _load_dump(settings: Settings) -> Dump:
    dump = read_model(settings.paths.dump, Dump)
    if dump is None:
        raise typer.BadParameter("no dump found, run: kts pull")
    return dump


def _load_plan(settings: Settings) -> Plan:
    plan = read_model(settings.paths.plan, Plan)
    if plan is None:
        raise typer.BadParameter("no plan found, run: kts plan")
    return plan


# -- commands ---------------------------------------------------------------


@app.callback()
def main_options(
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Log API-level detail.")] = False,
) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )


@app.command()
def auth(service: Annotated[Service, typer.Argument(help="Which service to authorize.")]) -> None:
    """Authorize a service: kino.pub via device-code, Simkl via OAuth2 (browser)."""

    async def run() -> None:
        settings = Settings()
        if service is Service.KINOPUB:
            async with KinopubClient(settings) as client:
                await client.device_auth()
        else:
            async with SimklClient(settings) as client:
                await client.authorize()

    asyncio.run(run())


@app.command(name="pull")
def pull_command() -> None:
    """Dump watch history and progress from kino.pub into data/."""
    asyncio.run(pull(Settings()))


@app.command(name="plan")
def plan_command(
    export: Annotated[Path | None, typer.Option("--export", help="Also write the plan to this path.")] = None,
) -> None:
    """Build a sync plan from the dump."""

    async def run() -> None:
        settings = Settings()
        await _plan_step(settings, _load_dump(settings), export=export)
        print(f"plan saved: {settings.paths.plan}")

    asyncio.run(run())


@app.command(name="push")
def push_command(
    *,
    movies: Annotated[bool, typer.Option("--movies", help="Watched movies, to Simkl.")] = False,
    shows: Annotated[bool, typer.Option("--shows", help="Watched shows and episodes, to Simkl.")] = False,
    all_sections: Annotated[bool, typer.Option("--all", help="Movies and shows, to Simkl.")] = False,
    myshows: Annotated[
        bool,
        typer.Option(
            "--myshows",
            help="Watched episodes, to MyShows (no movies — MyShows has no movie tracking; "
            "watched_at is not preserved — MyShows dates every write as 'now').",
        ),
    ] = False,
    floppy: Annotated[
        bool,
        typer.Option("--floppy", help="Watched movies and episodes, to a self-hosted Floppy instance."),
    ] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Only report what would be pushed.")] = False,
) -> None:
    """Apply the plan to Simkl, MyShows, and/or Floppy."""
    if not (movies or shows or all_sections or myshows or floppy):
        raise typer.BadParameter("select --movies / --shows / --all / --myshows / --floppy")

    async def run() -> None:
        settings = Settings()
        await _push_step(
            settings,
            _load_plan(settings),
            movies=movies or all_sections,
            shows=shows or all_sections,
            myshows=myshows,
            floppy=floppy,
            dry_run=dry_run,
        )

    asyncio.run(run())


@app.command(name="sync")
def sync_command(
    myshows: Annotated[
        bool, typer.Option("--myshows", help="Also push watched episodes to MyShows.")
    ] = False,
    floppy: Annotated[
        bool, typer.Option("--floppy", help="Also push watched movies and episodes to Floppy.")
    ] = False,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Pull and plan, but write nothing to Simkl/MyShows/Floppy.")
    ] = False,
) -> None:
    """Run the whole pipeline: pull, plan, push to Simkl (and, with --myshows/--floppy, those too)."""

    async def run() -> None:
        settings = Settings()
        dump = await pull(settings)
        plan = await _plan_step(settings, dump)
        await _push_step(
            settings, plan, movies=True, shows=True, myshows=myshows, floppy=floppy, dry_run=dry_run
        )
        if dry_run:
            print("dry run: nothing was written to Simkl/MyShows/Floppy")

    asyncio.run(run())


@app.command(name="status")
def status_command() -> None:
    """Report local sync state: dump age, pending pushes, authorization."""
    print(status_module.format_status(status_module.collect(Settings())))


def main() -> None:
    try:
        app()
    except (KinopubError, SimklError, MyShowsError, FloppyError) as exc:
        # Outside click's runtime, so exit directly: typer.Exit would surface
        # as an uncaught exception instead of an exit code.
        typer.secho(f"error: {exc}", fg=typer.colors.RED, err=True)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
