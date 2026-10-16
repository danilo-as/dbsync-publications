"""PostgreSQL catalog introspection."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from .models import VERSIONS_TABLE, Column, ForeignKey, TableInfo

SYSTEM_SCHEMA_FILTER = """
    nspname NOT IN ('information_schema', 'pg_catalog', 'pg_toast')
    AND nspname NOT LIKE 'pg_temp_%%' AND nspname NOT LIKE 'pg_toast_temp_%%'
"""

# Ordinary and partitioned tables only: views and partitions are never synced directly.
TABLE_KINDS = "c.relkind IN ('r', 'p') AND NOT c.relispartition"


def list_schemas(conn: psycopg.Connection) -> list[str]:
    rows = conn.execute(f"SELECT nspname FROM pg_namespace WHERE {SYSTEM_SCHEMA_FILTER} ORDER BY 1").fetchall()
    return [r[0] for r in rows]


def load_tables(conn: psycopg.Connection, schema: str) -> dict[str, TableInfo]:
    """Return every base table of a schema keyed by table name."""
    columns: dict[str, list[Column]] = defaultdict(list)
    for relname, attname, data_type, not_null, generated, identity_always in conn.execute(
        f"""
        SELECT c.relname, a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull,
               a.attgenerated <> '', a.attidentity = 'a'
        FROM pg_attribute a
        JOIN pg_class c ON c.oid = a.attrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = %s AND {TABLE_KINDS} AND a.attnum > 0 AND NOT a.attisdropped
        ORDER BY c.relname, a.attnum
        """,
        (schema,),
    ):
        columns[relname].append(Column(attname, data_type, not_null, generated, identity_always))

    primary_keys: dict[str, tuple[str, ...]] = {}
    unique_tables: set[str] = set()
    for relname, is_primary, key_columns in conn.execute(
        """
        SELECT c.relname, i.indisprimary,
               array(SELECT a.attname FROM unnest(i.indkey) WITH ORDINALITY k(attnum, ord)
                     JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.attnum
                     ORDER BY k.ord)
        FROM pg_index i
        JOIN pg_class c ON c.oid = i.indrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = %s AND i.indisunique
        """,
        (schema,),
    ):
        unique_tables.add(relname)
        if is_primary:
            primary_keys[relname] = tuple(key_columns)

    incoming: dict[str, list[ForeignKey]] = defaultdict(list)
    outgoing: dict[str, list[ForeignKey]] = defaultdict(list)
    for conname, child_schema, child, parent_schema, parent, child_cols, parent_cols in conn.execute(
        """
        SELECT con.conname, cn.nspname, cc.relname, pn.nspname, pc.relname,
               array(SELECT a.attname FROM unnest(con.conkey) WITH ORDINALITY k(attnum, ord)
                     JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = k.attnum
                     ORDER BY k.ord),
               array(SELECT a.attname FROM unnest(con.confkey) WITH ORDINALITY k(attnum, ord)
                     JOIN pg_attribute a ON a.attrelid = con.confrelid AND a.attnum = k.attnum
                     ORDER BY k.ord)
        FROM pg_constraint con
        JOIN pg_class cc ON cc.oid = con.conrelid
        JOIN pg_namespace cn ON cn.oid = cc.relnamespace
        JOIN pg_class pc ON pc.oid = con.confrelid
        JOIN pg_namespace pn ON pn.oid = pc.relnamespace
        WHERE con.contype = 'f' AND (cn.nspname = %s OR pn.nspname = %s)
        """,
        (schema, schema),
    ):
        fk = ForeignKey(
            conname, f"{child_schema}.{child}", f"{parent_schema}.{parent}", tuple(child_cols), tuple(parent_cols)
        )
        if child_schema == schema:
            outgoing[child].append(fk)
        if parent_schema == schema:
            incoming[parent].append(fk)

    return {
        name: TableInfo(
            schema=schema,
            name=name,
            columns=tuple(cols),
            primary_key=primary_keys.get(name, ()),
            has_unique_key=name in unique_tables,
            incoming_fks=tuple(sorted(incoming[name], key=lambda f: f.name)),
            outgoing_fks=tuple(sorted(outgoing[name], key=lambda f: f.name)),
        )
        for name, cols in columns.items()
    }


def list_versions(conn: psycopg.Connection, versions_table: TableInfo) -> list[dict[str, Any]]:
    """Rows of publication_versions, newest first.

    Optional columns (publication_month, latest/latest_version) vary per schema,
    so the ORDER BY only uses columns that exist.
    """
    available = {c.name for c in versions_table.columns}
    order = [
        sql.SQL("{} DESC NULLS LAST").format(sql.Identifier(name))
        for name in ("publication_year", "publication_quarter", "publication_month", "version", "creation_date")
        if name in available
    ]
    query = sql.SQL("SELECT * FROM {}.{}").format(
        sql.Identifier(versions_table.schema), sql.Identifier(VERSIONS_TABLE)
    )
    if order:
        query += sql.SQL(" ORDER BY ") + sql.SQL(", ").join(order)
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(query).fetchall()


def version_label(row: dict[str, Any]) -> str:
    parts = []
    if row.get("publication_year") is not None:
        parts.append(str(row["publication_year"]))
    if row.get("publication_quarter") is not None:
        parts.append(f"Q{row['publication_quarter']}")
    if row.get("publication_month") is not None:
        parts.append(f"M{row['publication_month']}")
    if row.get("version") is not None:
        parts.append(f"v{row['version']}")
    if row.get("latest") or row.get("latest_version"):
        parts.append("(latest)")
    if row.get("creation_date") is not None:
        parts.append(f"- created {row['creation_date']:%Y-%m-%d}")
    parts.append(f"[{row['id']}]")
    return " ".join(parts)
