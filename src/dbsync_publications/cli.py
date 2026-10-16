"""Interactive CLI: choose schema group, schema, versions and tables, then sync."""

from __future__ import annotations

import argparse
import sys
from contextlib import ExitStack

import psycopg
import questionary
from rich.console import Console
from rich.table import Table

from . import catalog
from .config import ConfigError, describe, load_settings
from .engine import SyncPlan, build_plan, execute_plan
from .models import VERSIONS_TABLE, Strategy
from .planner import split_schemas, strategy_for, table_notices

console = Console()

PUBLICATION_GROUP = "Publication schemas (*publications)"
OTHER_GROUP = "Other schemas"
ALL_TABLES = "__all_tables__"


class Aborted(Exception):
    pass


def ask(question: questionary.Question):
    """Run a prompt; Ctrl+C or an empty required answer aborts the run."""
    answer = question.ask()
    if answer is None:
        raise Aborted
    return answer


def choose_schema(source: psycopg.Connection) -> tuple[str, bool]:
    group = ask(questionary.select("What do you want to sync?", choices=[PUBLICATION_GROUP, OTHER_GROUP]))
    publication_mode = group == PUBLICATION_GROUP
    pubs, others = split_schemas(catalog.list_schemas(source))
    schemas = pubs if publication_mode else others
    if not schemas:
        console.print("[red]No schemas found in that group.[/red]")
        raise Aborted
    return ask(questionary.select("Schema:", choices=schemas)), publication_mode


def choose_versions(source: psycopg.Connection, schema: str, tables: dict) -> list[str]:
    pv = tables.get(VERSIONS_TABLE)
    if pv is None:
        console.print(
            f"[yellow]⚠ {schema} has no {VERSIONS_TABLE} table: tables without version_id will be "
            f"TRUNCATEd and fully reloaded; tables with version_id will be skipped.[/yellow]"
        )
        return []
    rows = catalog.list_versions(source, pv)
    if not rows:
        console.print(f"[yellow]⚠ {schema}.{VERSIONS_TABLE} is empty.[/yellow]")
        raise Aborted
    choices = [questionary.Choice(catalog.version_label(r), value=str(r["id"])) for r in rows]
    return ask(
        questionary.checkbox(
            "Publication versions to sync:",
            choices=choices,
            validate=lambda selected: bool(selected) or "Select at least one version",
        )
    )


def resolve_tables(selected: list[str], names: list[str]) -> list[str]:
    """Expand the "all tables" choice to every listed table."""
    if ALL_TABLES in selected:
        return list(names)
    return [name for name in selected if name != ALL_TABLES]


def choose_tables(tables: dict, publication_mode: bool, versions_available: bool) -> list[str]:
    choices = []
    for name, table in sorted(tables.items()):
        if name == VERSIONS_TABLE:
            continue
        strategy = strategy_for(table, publication_mode=publication_mode)
        notices = table_notices(table, strategy)
        title = name
        if strategy is Strategy.VERSION_REPLACE and not versions_available:
            title = f"⚠ {name}  (has version_id but no publication_versions → will be skipped)"
        elif strategy is Strategy.TRUNCATE_REPLACE:
            title = f"⚠ {name}  (no version_id → TRUNCATE + full reload)"
        elif notices:
            title = f"⚠ {name}  ({notices[0]})"
        choices.append(questionary.Choice(title, value=name))
    if not choices:
        console.print("[red]The schema has no tables to sync.[/red]")
        raise Aborted
    names = [c.value for c in choices]
    all_choice = questionary.Choice(f"★ All tables ({len(names)})", value=ALL_TABLES)
    selected = ask(
        questionary.checkbox(
            "Tables to sync:",
            choices=[all_choice, questionary.Separator(), *choices],
            validate=lambda selected: bool(selected) or "Select at least one table",
        )
    )
    return resolve_tables(selected, names)


def show_plan(plan: SyncPlan, target_labels: dict[str, str]) -> bool:
    any_ready = False
    for name, plans in plan.targets.items():
        grid = Table(title=f"{name} → {target_labels[name]}", show_lines=True)
        grid.add_column("Table")
        grid.add_column("Strategy")
        grid.add_column("Status")
        grid.add_column("Notes")
        for p in plans:
            any_ready |= p.ready
            status = "[green]ready[/green]" if p.ready else "[red]skipped[/red]"
            strategy = p.strategy.value
            if p.strategy is Strategy.TRUNCATE_REPLACE:
                strategy = f"[yellow]{strategy}[/yellow]"
            grid.add_row(p.table.name, strategy, status, "\n".join(p.messages))
        console.print(grid)
    if plan.publication_mode and plan.version_ids:
        console.print(f"Versions: {', '.join(plan.version_ids)} ({VERSIONS_TABLE} rows are upserted first)")
    return any_ready


def run(env_file: str | None) -> int:
    try:
        settings = load_settings(env_file)
    except ConfigError as exc:
        console.print(f"[red]Configuration error:[/red] {exc}")
        return 2

    target_labels = {name: describe(url) for name, url in settings.target_urls.items()}
    console.print(f"Source: [bold]{describe(settings.source_url)}[/bold]")
    for name, label in target_labels.items():
        console.print(f"Target {name}: [bold]{label}[/bold]")

    with ExitStack() as stack:
        try:
            source = stack.enter_context(psycopg.connect(settings.source_url, autocommit=True))
            targets = {
                name: stack.enter_context(psycopg.connect(url, autocommit=True))
                for name, url in settings.target_urls.items()
            }
        except psycopg.OperationalError as exc:
            console.print(f"[red]Connection error:[/red] {exc}")
            return 2

        try:
            schema, publication_mode = choose_schema(source)
            tables = catalog.load_tables(source, schema)
            version_ids = choose_versions(source, schema, tables) if publication_mode else []
            versions_available = VERSIONS_TABLE in tables and bool(version_ids)
            selected = choose_tables(tables, publication_mode, versions_available)

            with console.status("Validating schemas on every target..."):
                plan = build_plan(
                    source, targets, schema=schema, table_names=selected,
                    version_ids=version_ids, publication_mode=publication_mode,
                )
            if not show_plan(plan, target_labels):
                console.print("[red]Nothing to sync: every table was skipped.[/red]")
                return 1
            if not ask(questionary.confirm("Run the sync?", default=False)):
                raise Aborted
        except Aborted:
            console.print("Cancelled.")
            return 130

        with console.status("Syncing..."):
            results = execute_plan(source, targets, plan)

    exit_code = 0
    for result in results:
        if result.ok:
            detail = ", ".join(f"{t}: {n} rows" for t, n in result.tables.items()) or "nothing to load"
            console.print(f"[green]✔ {result.name}[/green] {detail}")
        else:
            exit_code = 1
            console.print(f"[red]✘ {result.name} rolled back:[/red] {result.error}")
    return exit_code


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="dbsync-publications",
        description="Sync selected PostgreSQL tables from a source database to up to two targets.",
    )
    parser.add_argument("--env-file", default=None, help="Path to the .env file (default: ./.env)")
    args = parser.parse_args(argv)
    sys.exit(run(args.env_file))
