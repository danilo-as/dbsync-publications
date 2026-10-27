"""Sync engine: plan per target, export to temporary CSV, load with COPY."""

from __future__ import annotations

import csv
import tempfile
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import psycopg
from psycopg import sql

from . import catalog
from .models import VERSION_COLUMN, VERSIONS_TABLE, ParentEdge, ParentFill, Strategy, TableInfo, TablePlan
from .planner import parent_closure, plan_target, strategy_for, topo_order

CHUNK_SIZE = 1024 * 1024
# NULL as \N (never quoted) so the CSV keeps NULL apart from the empty string ("").
CSV_NULL = "\\N"
CSV_OPTIONS = sql.SQL("FORMAT csv, HEADER, NULL {}").format(sql.Literal(CSV_NULL))


@dataclass
class SyncPlan:
    schema: str
    publication_mode: bool
    version_ids: list[str]
    source_tables: dict[str, TableInfo]
    versions_table: TableInfo | None
    targets: dict[str, list[TablePlan]]
    parents: dict[str, ParentFill] = field(default_factory=dict)


@dataclass
class TargetResult:
    name: str
    ok: bool
    error: str | None = None
    tables: dict[str, int] = field(default_factory=dict)


class Progress:
    """Receives the start and end of every sync step. The default reports nothing."""

    def start(self, label: str) -> None:
        pass

    def done(self, label: str, detail: str) -> None:
        pass


@dataclass
class _StepRows:
    count: int | None = None


@contextmanager
def _step(progress: Progress, label: str) -> Iterator[_StepRows]:
    progress.start(label)
    began = time.monotonic()
    rows = _StepRows()
    yield rows
    elapsed = f"{time.monotonic() - began:.1f}s"
    progress.done(label, elapsed if rows.count is None else f"{rows.count} rows, {elapsed}")


class _CatalogCache:
    """Resolve qualified table names, loading each schema's catalog once."""

    def __init__(self, conn: psycopg.Connection, preloaded: dict[str, dict[str, TableInfo]] | None = None):
        self._conn = conn
        self._schemas = dict(preloaded or {})

    def schema(self, name: str) -> dict[str, TableInfo]:
        if name not in self._schemas:
            self._schemas[name] = catalog.load_tables(self._conn, name)
        return self._schemas[name]

    def table(self, qualified: str) -> TableInfo | None:
        schema, _, name = qualified.partition(".")
        return self.schema(schema).get(name)


def build_plan(
    source: psycopg.Connection,
    targets: dict[str, psycopg.Connection],
    *,
    schema: str,
    table_names: list[str],
    version_ids: list[str],
    publication_mode: bool,
) -> SyncPlan:
    source_catalog = catalog.load_tables(source, schema)
    # Without selected versions there is nothing to filter by: version tables get skipped.
    versions_table = source_catalog.get(VERSIONS_TABLE) if publication_mode and version_ids else None

    tables = [source_catalog[name] for name in table_names if name != VERSIONS_TABLE]
    strategies = {t.name: strategy_for(t, publication_mode=publication_mode) for t in tables}
    # Selected publication_versions rows are upserted separately, so they are never parent fills.
    upserted = [versions_table.qualified] if versions_table is not None else []
    source_cache = _CatalogCache(source, {schema: source_catalog})
    parents, deps, missing = parent_closure(tables, source_cache.table, upserted)

    plans = {}
    for name, conn in targets.items():
        target_cache = _CatalogCache(conn)
        plans[name] = plan_target(
            tables,
            strategies,
            target_cache.schema(schema),
            versions_table,
            parents=parents,
            parent_deps=deps,
            target_parents={parent: target_cache.table(parent) for parent in parents},
            missing_parents=missing,
        )
    return SyncPlan(schema, publication_mode, list(version_ids), source_catalog, versions_table, plans, parents)


def execute_plan(
    source: psycopg.Connection,
    targets: dict[str, psycopg.Connection],
    plan: SyncPlan,
    progress: Progress | None = None,
) -> list[TargetResult]:
    """Export every ready table once, then load each target in its own transaction."""
    progress = progress or Progress()
    ready_tables = {p.table.name: p.table for plans in plan.targets.values() for p in plans if p.ready}
    needs_versions = any(
        p.ready and p.strategy is Strategy.VERSION_REPLACE for plans in plan.targets.values() for p in plans
    )

    with tempfile.TemporaryDirectory(prefix="dbsync-") as tmp:
        tmp_dir = Path(tmp)
        files: dict[str, Path] = {}
        exported: dict[str, int] = {}
        for name, table in ready_tables.items():
            strategy = next(p.strategy for plans in plan.targets.values() for p in plans if p.table.name == name)
            where = _version_filter(table, plan.version_ids) if strategy is Strategy.VERSION_REPLACE else None
            with _step(progress, f"Export {table.qualified}") as rows:
                files[name], exported[name] = _export(source, table, where, tmp_dir)
                rows.count = exported[name]
        needed = {parent for plans in plan.targets.values() for p in plans if p.ready for parent in p.parents}
        exported_files = {ready_tables[name].qualified: path for name, path in files.items()}
        parent_keys = _ParentKeys(plan.parents, needed, exported_files)
        # Children before parents, so every parent's keys come from files already exported.
        for parent in reversed(topo_order([plan.parents[name].table for name in needed])):
            with _step(progress, f"Export parent rows of {parent.qualified}") as rows:
                where = parent_keys.filter(parent.qualified)
                files[parent.qualified], rows.count = _export(source, parent, where, tmp_dir)
                exported_files[parent.qualified] = files[parent.qualified]
        if needs_versions and plan.versions_table is not None:
            pv = plan.versions_table
            with _step(progress, f"Export {pv.qualified}") as rows:
                files[VERSIONS_TABLE], rows.count = _export(source, pv, _id_filter(pv, plan.version_ids), tmp_dir)

        results = []
        for name, conn in targets.items():
            ready = [p for p in plan.targets[name] if p.ready]
            if not ready:
                results.append(TargetResult(name, ok=True))
                continue
            try:
                counts = _load_target(conn, plan, ready, files, exported, _TargetProgress(progress, name))
                results.append(TargetResult(name, ok=True, tables=counts))
            except psycopg.Error as exc:
                results.append(TargetResult(name, ok=False, error=str(exc).strip()))
        return results


class _TargetProgress(Progress):
    """Prefix every step label with the target name."""

    def __init__(self, inner: Progress, target: str):
        self._inner = inner
        self._target = target

    def start(self, label: str) -> None:
        self._inner.start(f"{self._target}: {label}")

    def done(self, label: str, detail: str) -> None:
        self._inner.done(f"{self._target}: {label}", detail)


def _qualified(table: TableInfo) -> sql.Composed:
    return sql.SQL("{}.{}").format(sql.Identifier(table.schema), sql.Identifier(table.name))


def _column_list(table: TableInfo) -> sql.Composed:
    return sql.SQL(", ").join(sql.Identifier(c) for c in table.copy_columns)


def _array_of(values: list[str], data_type: str) -> sql.Composed:
    return sql.SQL("ARRAY[{}]::{}[]").format(
        sql.SQL(", ").join(sql.Literal(str(v)) for v in values), sql.SQL(data_type)
    )


def _version_filter(table: TableInfo, version_ids: list[str]) -> sql.Composed:
    return sql.SQL("{} = ANY({})").format(
        sql.Identifier(VERSION_COLUMN), _array_of(version_ids, table.version_id_type or "uuid")
    )


def _id_filter(pv: TableInfo, version_ids: list[str]) -> sql.Composed:
    id_type = next(c.data_type for c in pv.columns if c.name == "id")
    return sql.SQL("{} = ANY({})").format(sql.Identifier("id"), _array_of(version_ids, id_type))


def referenced_keys(
    path: Path, column_groups: Iterable[tuple[str, ...]]
) -> dict[tuple[str, ...], set[tuple[str, ...]]]:
    """Read the distinct non-NULL values of each column group from a CSV written by ``_export``.

    NULL is written unquoted as ``CSV_NULL``. A text value equal to it is written quoted, but the
    csv module drops quotes, so such a key reads as NULL: its parent row is not copied and the
    load then fails on the foreign key instead of silently skipping data.
    """
    groups = list(dict.fromkeys(column_groups))
    keys: dict[tuple[str, ...], set[tuple[str, ...]]] = {group: set() for group in groups}
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.reader(fh)
        header = next(reader)
        positions = {group: [header.index(c) for c in group] for group in groups}
        for row in reader:
            for group, indexes in positions.items():
                key = tuple(row[i] for i in indexes)
                if CSV_NULL not in key:
                    keys[group].add(key)
    return keys


class _ParentKeys:
    """Build the WHERE clause that selects only the parent rows the exported rows reference.

    The referenced keys are read from the CSV files already exported (synced tables and
    needed parents), so the source never re-scans a large child table per foreign key.
    Each child file is read once, for every foreign key it holds toward a needed parent.
    Self-referencing parents are copied whole, because their own ancestors would otherwise
    be missing.
    """

    def __init__(self, parents: dict[str, ParentFill], needed: set[str], files: dict[str, Path]):
        self._parents = parents
        self._needed = needed
        self._files = files  # qualified table -> exported CSV, filled as parents get exported
        self._keys: dict[tuple[str, tuple[str, ...]], set[tuple[str, ...]]] = {}

    def filter(self, name: str) -> sql.Composable | None:
        fill = self._parents[name]
        if fill.self_referencing:
            return None
        by_columns: dict[tuple[str, ...], set[tuple[str, ...]]] = {}
        for edge in fill.edges:
            # Skips unselected children and, in a cycle between parents, a child not exported yet.
            if edge.child not in self._files:
                continue
            by_columns.setdefault(edge.fk.referenced_columns, set()).update(self._keys_of(edge))
        types = {c.name: c.data_type for c in fill.table.columns}
        parts = [_key_filter(columns, keys, types) for columns, keys in by_columns.items() if keys]
        return sql.SQL(" OR ").join(parts) if parts else sql.SQL("FALSE")

    def _keys_of(self, edge: ParentEdge) -> set[tuple[str, ...]]:
        if (edge.child, edge.fk.columns) not in self._keys:
            groups = [
                e.fk.columns for parent in self._needed for e in self._parents[parent].edges if e.child == edge.child
            ]
            for columns, keys in referenced_keys(self._files[edge.child], groups).items():
                self._keys[(edge.child, columns)] = keys
        return self._keys[(edge.child, edge.fk.columns)]


def _key_filter(columns: tuple[str, ...], keys: set[tuple[str, ...]], types: dict[str, str]) -> sql.Composed:
    ordered = sorted(keys)
    arrays = [_array_of([key[i] for key in ordered], types[c]) for i, c in enumerate(columns)]
    if len(columns) == 1:
        return sql.SQL("{} = ANY({})").format(sql.Identifier(columns[0]), arrays[0])
    return sql.SQL("({}) IN (SELECT * FROM unnest({}))").format(
        sql.SQL(", ").join(sql.Identifier(c) for c in columns), sql.SQL(", ").join(arrays)
    )


def _qualified_name(qualified: str) -> sql.Composed:
    schema, _, name = qualified.partition(".")
    return sql.SQL("{}.{}").format(sql.Identifier(schema), sql.Identifier(name))


def _export(source: psycopg.Connection, table: TableInfo, where: sql.Composed | None, tmp_dir: Path) -> tuple[Path, int]:
    path = tmp_dir / f"{table.schema}.{table.name}.csv"
    select = sql.SQL("SELECT {} FROM {}").format(_column_list(table), _qualified(table))
    if where is not None:
        select += sql.SQL(" WHERE ") + where
    query = sql.SQL("COPY ({}) TO STDOUT WITH ({})").format(select, CSV_OPTIONS)
    with source.cursor() as cur, path.open("wb") as fh:
        with cur.copy(query) as copy:
            for chunk in copy:
                fh.write(chunk)
        rows = cur.rowcount
    return path, rows


def _copy_in(cur: psycopg.Cursor, target: sql.Composable, columns: sql.Composable, path: Path) -> None:
    query = sql.SQL("COPY {} ({}) FROM STDIN WITH ({})").format(target, columns, CSV_OPTIONS)
    with cur.copy(query) as copy, path.open("rb") as fh:
        while chunk := fh.read(CHUNK_SIZE):
            copy.write(chunk)


def _copy_via_staging(cur: psycopg.Cursor, table: TableInfo, path: Path, index: int | str) -> sql.Identifier:
    staging = sql.Identifier(f"_dbsync_stg_{index}")
    cur.execute(
        sql.SQL("CREATE TEMP TABLE {} (LIKE {}) ON COMMIT DROP").format(staging, _qualified(table))
    )
    _copy_in(cur, staging, _column_list(table), path)
    return staging


def _insert_from(table: TableInfo, staging: sql.Identifier) -> sql.Composed:
    overriding = sql.SQL(" OVERRIDING SYSTEM VALUE") if table.has_identity_always else sql.SQL("")
    return sql.SQL("INSERT INTO {} ({}){} SELECT {} FROM {}").format(
        _qualified(table), _column_list(table), overriding, _column_list(table), staging
    )


def _upsert_versions(cur: psycopg.Cursor, pv: TableInfo, path: Path) -> None:
    staging = _copy_via_staging(cur, pv, path, 0)
    key = pv.primary_key or ("id",)
    updates = [c for c in pv.copy_columns if c not in key]
    conflict = sql.SQL(" ON CONFLICT ({}) ").format(sql.SQL(", ").join(sql.Identifier(k) for k in key))
    if updates:
        conflict += sql.SQL("DO UPDATE SET ") + sql.SQL(", ").join(
            sql.SQL("{0} = EXCLUDED.{0}").format(sql.Identifier(c)) for c in updates
        )
    else:
        conflict += sql.SQL("DO NOTHING")
    cur.execute(_insert_from(pv, staging) + conflict)


def _load_target(
    conn: psycopg.Connection,
    plan: SyncPlan,
    ready: list[TablePlan],
    files: dict[str, Path],
    exported: dict[str, int],
    progress: Progress,
) -> dict[str, int]:
    strategies = {p.table.name: p.strategy for p in ready}
    ordered = topo_order([p.table for p in ready])
    counts: dict[str, int] = {}

    with conn.transaction(), conn.cursor() as cur:
        if Strategy.VERSION_REPLACE in strategies.values() and plan.versions_table is not None:
            with _step(progress, f"Upsert {plan.versions_table.qualified}"):
                _upsert_versions(cur, plan.versions_table, files[VERSIONS_TABLE])

        # Missing parent rows of unselected tables: insert only, never update or delete.
        needed = {parent for p in ready for parent in p.parents}
        parent_tables = topo_order([plan.parents[name].table for name in needed])
        for index, parent in enumerate(parent_tables):
            with _step(progress, f"Insert missing parent rows into {parent.qualified}") as rows:
                staging = _copy_via_staging(cur, parent, files[parent.qualified], f"p{index}")
                cur.execute(_insert_from(parent, staging) + sql.SQL(" ON CONFLICT DO NOTHING"))
                counts[f"{parent.qualified} (parent rows)"] = rows.count = cur.rowcount

        # Children first so deletes/truncates never trip a FK between selected tables.
        for table in reversed(ordered):
            strategy = strategies[table.name]
            if strategy is Strategy.VERSION_REPLACE:
                with _step(progress, f"Delete selected versions from {table.qualified}") as rows:
                    cur.execute(
                        sql.SQL("DELETE FROM {} WHERE ").format(_qualified(table))
                        + _version_filter(table, plan.version_ids)
                    )
                    rows.count = cur.rowcount
            elif strategy is Strategy.TRUNCATE_REPLACE:
                with _step(progress, f"Truncate {table.qualified}"):
                    cur.execute(sql.SQL("TRUNCATE {}").format(_qualified(table)))

        # Parents first so inserted children always find their referenced rows.
        for index, table in enumerate(ordered, start=1):
            with _step(progress, f"Load {table.qualified}") as rows:
                if strategies[table.name] is Strategy.INSERT_NEW:
                    staging = _copy_via_staging(cur, table, files[table.name], index)
                    cur.execute(_insert_from(table, staging) + sql.SQL(" ON CONFLICT DO NOTHING"))
                    counts[table.name] = cur.rowcount
                else:
                    _copy_in(cur, _qualified(table), _column_list(table), files[table.name])
                    counts[table.name] = exported[table.name]
                rows.count = counts[table.name]
    return counts
