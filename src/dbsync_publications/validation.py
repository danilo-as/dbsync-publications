"""Structural comparison between a source table and its target counterpart."""

from __future__ import annotations

from .models import TableInfo


def _nullability(not_null: bool) -> str:
    return "NOT NULL" if not_null else "NULL"


def compare_tables(source: TableInfo, target: TableInfo | None) -> list[str]:
    """Return human-readable differences; an empty list means identical.

    Columns are compared by name, data type and nullability. Column order is
    ignored because every COPY uses an explicit column list.
    """
    if target is None:
        return ["table does not exist in target"]

    diffs: list[str] = []
    target_columns = {c.name: c for c in target.columns}
    source_names = {c.name for c in source.columns}

    for column in source.columns:
        other = target_columns.get(column.name)
        if other is None:
            diffs.append(f"column {column.name}: exists only in source")
            continue
        if column.data_type != other.data_type:
            diffs.append(
                f"column {column.name}: type differs (source {column.data_type}, target {other.data_type})"
            )
        if column.not_null != other.not_null:
            diffs.append(
                f"column {column.name}: nullability differs "
                f"(source {_nullability(column.not_null)}, target {_nullability(other.not_null)})"
            )

    for column in target.columns:
        if column.name not in source_names:
            diffs.append(f"column {column.name}: exists only in target")

    return diffs
