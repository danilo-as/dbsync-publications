"""Sync engine: plan per target, export to temporary CSV, load with COPY."""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import psycopg
from psycopg import sql

from . import catalog
from .models import VERSION_COLUMN, VERSIONS_TABLE, ParentFill, Strategy, TableInfo, TablePlan
from .planner import parent_closure, plan_target, strategy_for, topo_order

CHUNK_SIZE = 1024 * 1024


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
) -> list[TargetResult]:
    """Export every ready table once, then load each target in its own transaction."""
    ready_tables = {p.table.name: p.table for plans in plan.targets.values() for p in plans if p.ready}
    needs_versions = any(
        p.ready and p.strategy is Strategy.VERSION_REPLACE for plans in plan.targets.values() for p in plans
    )

    with tempfile.TemporaryDirectory(prefix="dbsync-") as tmp:
        tmp_dir = Path(tmp)
        files: dict[str, Path] = {}
        exported: dict[str, int] = {}
        filters: dict[str, sql.Composed | None] = {}
        for name, table in ready_tables.items():
            strategy = next(p.strategy for plans in plan.targets.values() for p in plans if p.table.name == name)
            where = _version_filter(table, plan.version_ids) if strategy is Strategy.VERSION_REPLACE else None
            filters[table.qualified] = where
            files[name], exported[name] = _export(source, table, where, tmp_dir)
        needed = {parent for plans in plan.targets.values() for p in plans if p.ready for parent in p.parents}
        parent_filter = _ParentFilter(plan.parents, filters, needed)
        for parent in sorted(needed):
            files[parent], _ = _export(source, plan.parents[parent].table, parent_filter.of(parent), tmp_dir)
        if needs_versions and plan.versions_table is not None:
            pv = plan.versions_table
            files[VERSIONS_TABLE], _ = _export(source, pv, _id_filter(pv, plan.version_ids), tmp_dir)

        results = []
        for name, conn in targets.items():
            ready = [p for p in plan.targets[name] if p.ready]
            if not ready:
                results.append(TargetResult(name, ok=True))
                continue
            try:
                counts = _load_target(conn, plan, ready, files, exported)
                results.append(TargetResult(name, ok=True, tables=counts))
            except psycopg.Error as exc:
                results.append(TargetResult(name, ok=False, error=str(exc).strip()))
        return results


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


class _ParentFilter:
    """Build the WHERE clause that selects only the parent rows the synced rows reference.

    A parent row is needed when an exported child row (a synced table, or another
    needed parent) points at it. Self-referencing parents are copied whole, because
    their own ancestors would otherwise be missing.
    """

    def __init__(
        self,
        parents: dict[str, ParentFill],
        child_filters: dict[str, sql.Composed | None],
        needed: set[str],
    ):
        self._parents = parents
        self._child_filters = child_filters
        self._needed = needed
        self._memo: dict[str, sql.Composable | None] = {}

    def of(self, name: str, path: frozenset[str] = frozenset()) -> sql.Composable | None:
        if name in self._memo:
            return self._memo[name]
        fill = self._parents[name]
        if fill.self_referencing:
            return None
        parts: list[sql.Composable] = []
        for edge in fill.edges:
            child = edge.child
            if child in path:  # cycle between parent tables
                continue
            if child in self._child_filters:
                where = self._child_filters[child]
            elif child in self._needed:
                where = self.of(child, path | {name})
            else:
                continue
            subquery = sql.SQL("SELECT {} FROM {}").format(
                sql.SQL(", ").join(sql.Identifier(c) for c in edge.fk.columns), _qualified_name(child)
            )
            if where is not None:
                subquery += sql.SQL(" WHERE ") + where
            parts.append(
                sql.SQL("({}) IN ({})").format(
                    sql.SQL(", ").join(sql.Identifier(c) for c in edge.fk.referenced_columns), subquery
                )
            )
        result = sql.SQL(" OR ").join(parts) if parts else sql.SQL("FALSE")
        self._memo[name] = result
        return result


def _qualified_name(qualified: str) -> sql.Composed:
    schema, _, name = qualified.partition(".")
    return sql.SQL("{}.{}").format(sql.Identifier(schema), sql.Identifier(name))


def _export(source: psycopg.Connection, table: TableInfo, where: sql.Composed | None, tmp_dir: Path) -> tuple[Path, int]:
    path = tmp_dir / f"{table.schema}.{table.name}.csv"
    select = sql.SQL("SELECT {} FROM {}").format(_column_list(table), _qualified(table))
    if where is not None:
        select += sql.SQL(" WHERE ") + where
    query = sql.SQL("COPY ({}) TO STDOUT WITH (FORMAT csv, HEADER)").format(select)
    with source.cursor() as cur, path.open("wb") as fh:
        with cur.copy(query) as copy:
            for chunk in copy:
                fh.write(chunk)
        rows = cur.rowcount
    return path, rows


def _copy_in(cur: psycopg.Cursor, target: sql.Composable, columns: sql.Composable, path: Path) -> None:
    query = sql.SQL("COPY {} ({}) FROM STDIN WITH (FORMAT csv, HEADER)").format(target, columns)
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
) -> dict[str, int]:
    strategies = {p.table.name: p.strategy for p in ready}
    ordered = topo_order([p.table for p in ready])
    counts: dict[str, int] = {}

    with conn.transaction(), conn.cursor() as cur:
        if Strategy.VERSION_REPLACE in strategies.values() and plan.versions_table is not None:
            _upsert_versions(cur, plan.versions_table, files[VERSIONS_TABLE])

        # Missing parent rows of unselected tables: insert only, never update or delete.
        needed = {parent for p in ready for parent in p.parents}
        parent_tables = topo_order([plan.parents[name].table for name in needed])
        for index, parent in enumerate(parent_tables):
            staging = _copy_via_staging(cur, parent, files[parent.qualified], f"p{index}")
            cur.execute(_insert_from(parent, staging) + sql.SQL(" ON CONFLICT DO NOTHING"))
            counts[f"{parent.qualified} (parent rows)"] = cur.rowcount

        # Children first so deletes/truncates never trip a FK between selected tables.
        for table in reversed(ordered):
            strategy = strategies[table.name]
            if strategy is Strategy.VERSION_REPLACE:
                cur.execute(
                    sql.SQL("DELETE FROM {} WHERE ").format(_qualified(table))
                    + _version_filter(table, plan.version_ids)
                )
            elif strategy is Strategy.TRUNCATE_REPLACE:
                cur.execute(sql.SQL("TRUNCATE {}").format(_qualified(table)))

        # Parents first so inserted children always find their referenced rows.
        for index, table in enumerate(ordered, start=1):
            if strategies[table.name] is Strategy.INSERT_NEW:
                staging = _copy_via_staging(cur, table, files[table.name], index)
                cur.execute(_insert_from(table, staging) + sql.SQL(" ON CONFLICT DO NOTHING"))
                counts[table.name] = cur.rowcount
            else:
                _copy_in(cur, _qualified(table), _column_list(table), files[table.name])
                counts[table.name] = exported[table.name]
    return counts
