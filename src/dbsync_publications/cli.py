"""Interactive CLI: choose schema group, schema, versions and tables, then sync."""

from __future__ import annotations

import argparse
import sys
from contextlib import ExitStack

import psycopg
import questionary
from rich.console import Console
from rich.table import Table

from . import catalog, migrations
from .config import ConfigError, Settings, describe, load_settings
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


def _versions(values: list[str]) -> str:
    return ", ".join(f"V{v}" for v in values)


def _read_history(conn: psycopg.Connection, label: str) -> tuple[bool, list | None]:
    """Return (readable, rows); rows is None when the history table does not exist."""
    try:
        return True, migrations.read_history(conn)
    except psycopg.Error as exc:
        console.print(f"[yellow]⚠ {label}: cannot read flyway history ({str(exc).strip().splitlines()[0]})[/yellow]")
        return False, None


def check_migrations(settings: Settings, source: psycopg.Connection, targets: dict[str, psycopg.Connection]) -> None:
    """Report Flyway migrations missing on each target; offer to run them on local targets only."""
    directory = settings.migrations_dir
    if directory is None:
        console.print(f"[dim]{migrations.HISTORY_TABLE} check skipped: MIGRATIONS_DIR is not set.[/dim]")
        return
    if not directory.is_dir():
        console.print(f"[yellow]⚠ MIGRATIONS_DIR {directory} is not a directory: migration check skipped.[/yellow]")
        return
    console.rule("Flyway migrations")
    repo = migrations.load_repo(directory)

    source_ok, source_rows = _read_history(source, "Source")
    if source_ok and source_rows is None:
        console.print("[yellow]⚠ Source has no flyway history table.[/yellow]")
    elif source_rows is not None:
        status = migrations.migration_status(repo, source_rows, None)
        applied = [r.version for r in source_rows if r.success and r.version]
        latest = f"latest V{max(applied, key=migrations.version_key)}" if applied else "no successful versions"
        console.print(f"Source: {len(source_rows)} history rows, {latest}.")
        if status.not_in_source:
            console.print(f"[dim]Not applied in source yet (never run here): {_versions(status.not_in_source)}[/dim]")
    if source_rows is None:
        console.print("[yellow]Without the source history no migration can be run, only reported.[/yellow]")

    for name, conn in targets.items():
        local = migrations.is_local(settings.target_urls[name])
        readable, rows = _read_history(conn, name)
        if not readable:
            continue
        if rows is None:
            console.print(f"[yellow]⚠ {name}: no flyway history table.[/yellow]")
            if not (local and source_rows):
                continue
            if not ask(
                questionary.confirm(
                    f"Create the history on {name} by copying the {len(source_rows)} source rows as-is? "
                    "Only correct if this database already matches the source.",
                    default=False,
                )
            ):
                continue
            try:
                migrations.bootstrap_history(conn, source_rows)
                rows = migrations.read_history(conn)
            except psycopg.Error as exc:
                console.print(f"[red]✘ {name}: history copy failed and was rolled back:[/red] {str(exc).strip()}")
                continue
            console.print(f"[green]✔ {name}: history copied from source.[/green]")

        status = migrations.migration_status(repo, source_rows, rows)
        if status.failed:
            failed = _versions([r.version or "" for r in status.failed])
            console.print(f"[red]✘ {name}: failed migrations in history: {failed}[/red]")
        if status.missing_files:
            console.print(
                f"[yellow]⚠ {name}: applied in source but missing in {directory} "
                f"(update the repository): {_versions(status.missing_files)}[/yellow]"
            )
        if source_rows is None:
            if status.repo_pending:
                pending = _versions(status.repo_pending)
                console.print(f"[yellow]⚠ {name}: not applied (per repository): {pending}[/yellow]")
            continue
        if not status.runnable:
            console.print(f"[green]✔ {name}: up to date with the source.[/green]")
            continue
        console.print(f"[yellow]⚠ {name}: {len(status.runnable)} migrations applied in source are missing:[/yellow]")
        for row in status.runnable:
            console.print(f"   {row.script}")
        if not local:
            console.print(f"   [dim]{name} is not local: migrations are only run on localhost targets.[/dim]")
            continue
        if not ask(questionary.confirm(f"Run them on {name}?", default=False)):
            continue
        for row in status.runnable:
            migration = repo[row.version]
            try:
                elapsed = migrations.apply_migration(conn, migration, row)
            except psycopg.Error as exc:
                console.print(f"[red]✘ {migration.script} failed, later migrations not run:[/red] {str(exc).strip()}")
                break
            console.print(f"[green]✔ {migration.script}[/green] ({elapsed} ms)")


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
            check_migrations(settings, source, targets)
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
