"""Pure planning rules: which strategy each table uses and whether it can run."""

from __future__ import annotations

from collections.abc import Callable, Iterable

from .models import PUBLICATION_SUFFIX, VERSIONS_TABLE, ParentEdge, ParentFill, Strategy, TableInfo, TablePlan
from .validation import compare_tables


def split_schemas(schemas: Iterable[str]) -> tuple[list[str], list[str]]:
    """Split schema names into (publication schemas, other schemas)."""
    pubs, others = [], []
    for name in sorted(schemas):
        (pubs if name.endswith(PUBLICATION_SUFFIX) else others).append(name)
    return pubs, others


NO_VERSIONS_MESSAGE = "has version_id but no publication versions to filter by: skipped (never truncated)"


def strategy_for(table: TableInfo, *, publication_mode: bool) -> Strategy:
    if not publication_mode:
        return Strategy.INSERT_NEW
    if table.has_version_id:
        return Strategy.VERSION_REPLACE
    return Strategy.TRUNCATE_REPLACE


def table_notices(table: TableInfo, strategy: Strategy) -> list[str]:
    """Warnings that depend only on the source table and its strategy."""
    notices: list[str] = []
    if strategy is Strategy.TRUNCATE_REPLACE:
        notices.append("has no version_id: it will be TRUNCATEd and fully reloaded")
    if strategy is Strategy.INSERT_NEW and not table.has_unique_key:
        notices.append("no PK/unique constraint: ON CONFLICT cannot detect duplicates, every row will be inserted")
    return notices


def parent_closure(
    tables: list[TableInfo],
    lookup: Callable[[str], TableInfo | None],
    exclude: Iterable[str] = (),
) -> tuple[dict[str, ParentFill], dict[str, tuple[str, ...]], dict[str, str]]:
    """Find the unselected tables whose referenced rows must exist before loading ``tables``.

    Walks outgoing foreign keys recursively (parents of parents). Selected tables,
    self references and ``exclude`` are not followed. ``lookup`` resolves a qualified
    name to the source table. Returns the parent fills, the parents each selected
    table depends on (parents first) and, per selected table, a parent missing in
    the source.
    """
    selected = {t.qualified for t in tables}
    skip = selected | set(exclude)
    fills: dict[str, ParentFill] = {}
    deps: dict[str, tuple[str, ...]] = {}
    missing: dict[str, str] = {}

    def edges_of(child: TableInfo) -> list[ParentEdge]:
        return [
            ParentEdge(child.qualified, fk)
            for fk in child.outgoing_fks
            if fk.referenced not in skip and fk.referenced != child.qualified
        ]

    for table in tables:
        reached: dict[str, TableInfo] = {}
        stack = [table]
        while stack:
            child = stack.pop()
            for edge in edges_of(child):
                name = edge.fk.referenced
                parent = fills[name].table if name in fills else lookup(name)
                if parent is None:
                    missing.setdefault(table.name, name)
                    continue
                fill = fills.setdefault(name, ParentFill(parent))
                if edge not in fill.edges:
                    fill.edges.append(edge)
                if name not in reached:
                    reached[name] = parent
                    stack.append(parent)
        deps[table.name] = tuple(t.qualified for t in topo_order(list(reached.values())))
    return fills, deps, missing


def _parent_messages(
    table: TableInfo,
    parents: dict[str, ParentFill],
    deps: tuple[str, ...],
    target_parents: dict[str, TableInfo | None],
) -> tuple[list[str], list[str]]:
    """Return (blocking problems, notices) for the parent tables of one selected table."""
    problems: list[str] = []
    notices: list[str] = []
    for name in deps:
        fill = parents[name]
        diffs = compare_tables(fill.table, target_parents.get(name))
        if diffs:
            problems.append(f"parent table {name} differs in target ({'; '.join(diffs)})")
        elif fill.self_referencing:
            notices.append(f"parent {name} references itself: all its rows are copied (ON CONFLICT DO NOTHING)")
        else:
            notices.append(f"missing parent rows in {name} are copied from source (ON CONFLICT DO NOTHING)")
    return problems, notices


def plan_target(
    tables: list[TableInfo],
    strategies: dict[str, Strategy],
    target_catalog: dict[str, TableInfo],
    source_pv: TableInfo | None,
    parents: dict[str, ParentFill] | None = None,
    parent_deps: dict[str, tuple[str, ...]] | None = None,
    target_parents: dict[str, TableInfo | None] | None = None,
    missing_parents: dict[str, str] | None = None,
) -> list[TablePlan]:
    """Decide, for one target, which selected tables are ready to sync.

    ``source_pv`` is None when there are no publication versions to filter by
    (schema without publication_versions or no versions selected); version
    tables are then skipped instead of truncated. ``parents``/``parent_deps``
    come from :func:`parent_closure`; ``target_parents`` holds the target
    version of every parent table (None when it does not exist).
    """
    parents = parents or {}
    parent_deps = parent_deps or {}
    target_parents = target_parents or {}
    missing_parents = missing_parents or {}
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

        if strategy is Strategy.VERSION_REPLACE and source_pv is None:
            plan.ready = False
            plan.messages.append(NO_VERSIONS_MESSAGE)
        elif diffs:
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

        if plan.ready and table.name in missing_parents:
            plan.ready = False
            plan.messages.append(f"parent table {missing_parents[table.name]} does not exist in source")
        if plan.ready:
            deps = parent_deps.get(table.name, ())
            problems, parent_notices = _parent_messages(table, parents, deps, target_parents)
            if problems:
                plan.ready = False
                plan.messages.extend(problems)
            else:
                plan.parents = deps
                plan.messages.extend(table_notices(table, strategy) + parent_notices)
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
