"""Pure planning rules: which strategy each table uses and whether it can run."""

from __future__ import annotations

from collections.abc import Iterable

from .models import PUBLICATION_SUFFIX, VERSIONS_TABLE, Strategy, TableInfo, TablePlan
from .validation import compare_tables


def split_schemas(schemas: Iterable[str]) -> tuple[list[str], list[str]]:
    """Split schema names into (publication schemas, other schemas)."""
    pubs, others = [], []
    for name in sorted(schemas):
        (pubs if name.endswith(PUBLICATION_SUFFIX) else others).append(name)
    return pubs, others


def strategy_for(table: TableInfo, *, publication_mode: bool, versions_available: bool) -> Strategy:
    if not publication_mode:
        return Strategy.INSERT_NEW
    if table.has_version_id and versions_available:
        return Strategy.VERSION_REPLACE
    return Strategy.TRUNCATE_REPLACE


def table_notices(table: TableInfo, strategy: Strategy) -> list[str]:
    """Warnings that depend only on the source table and its strategy."""
    notices: list[str] = []
    if strategy is Strategy.TRUNCATE_REPLACE:
        notices.append("has no version_id: it will be TRUNCATEd and fully reloaded")
    if strategy is Strategy.INSERT_NEW and not table.has_unique_key:
        notices.append("no PK/unique constraint: ON CONFLICT cannot detect duplicates, every row will be inserted")
    for fk in table.outgoing_fks:
        if fk.referenced != table.qualified:
            notices.append(f"FK {fk.name} requires parent rows in {fk.referenced} on the target")
    return notices


def plan_target(
    tables: list[TableInfo],
    strategies: dict[str, Strategy],
    target_catalog: dict[str, TableInfo],
    source_pv: TableInfo | None,
) -> list[TablePlan]:
    """Decide, for one target, which selected tables are ready to sync."""
    pv_problem: str | None = None
    if source_pv is not None and any(s is Strategy.VERSION_REPLACE for s in strategies.values()):
        pv_diffs = compare_tables(source_pv, target_catalog.get(VERSIONS_TABLE))
        if pv_diffs:
            pv_problem = f"{VERSIONS_TABLE} differs in target ({'; '.join(pv_diffs)})"

    plans: list[TablePlan] = []
    for table in tables:
        strategy = strategies[table.name]
        target = target_catalog.get(table.name)
        plan = TablePlan(table=table, strategy=strategy)
        diffs = compare_tables(table, target)

        if diffs:
            plan.ready = False
            plan.messages.extend(diffs)
        elif strategy is Strategy.VERSION_REPLACE and pv_problem:
            plan.ready = False
            plan.messages.append(pv_problem)
        elif strategy is Strategy.TRUNCATE_REPLACE:
            blockers = [fk for fk in target.incoming_fks if fk.table != target.qualified]
            if blockers:
                plan.ready = False
                plan.messages.extend(
                    f"referenced by FK {fk.name} from {fk.table}: TRUNCATE without CASCADE is not possible"
                    for fk in blockers
                )

        if plan.ready:
            plan.messages.extend(table_notices(table, strategy))
        plans.append(plan)
    return plans


def topo_order(tables: list[TableInfo]) -> list[TableInfo]:
    """Order tables so referenced (parent) tables come before their children.

    Self references are ignored; cycles fall back to name order.
    """
    by_name = {t.qualified: t for t in tables}
    pending = dict(sorted(by_name.items()))
    ordered: list[TableInfo] = []
    while pending:
        ready = [
            name
            for name, t in pending.items()
            if not any(fk.referenced in pending and fk.referenced != name for fk in t.outgoing_fks)
        ]
        if not ready:
            ready = [next(iter(pending))]
        for name in ready:
            ordered.append(pending.pop(name))
    return ordered
