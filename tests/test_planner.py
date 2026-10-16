from dbsync_publications.models import Column, ForeignKey, Strategy, TableInfo
from dbsync_publications.planner import (
    plan_target,
    split_schemas,
    strategy_for,
    topo_order,
)
from dbsync_publications.validation import compare_tables

SCHEMA = "cobalt_forecast_publications"


def col(name, data_type="text", not_null=False):
    return Column(name=name, data_type=data_type, not_null=not_null)


def table(name, columns, schema=SCHEMA, pk=("id",), unique=True, incoming=(), outgoing=()):
    return TableInfo(
        schema=schema,
        name=name,
        columns=tuple(columns),
        primary_key=tuple(pk),
        has_unique_key=unique,
        incoming_fks=tuple(incoming),
        outgoing_fks=tuple(outgoing),
    )


def fk(name, table, referenced):
    return ForeignKey(name=name, table=table, referenced=referenced)


PV = table("publication_versions", [col("id", "uuid", True), col("publication_year", "integer", True)])
VERSIONED = table("prices", [col("id", "uuid", True), col("version_id", "uuid", True), col("value", "numeric")])
NO_VERSION = table("system_pricing", [col("id", "uuid", True), col("value", "numeric")])


def test_split_schemas_uses_publications_suffix():
    pubs, others = split_schemas(["locations", "cobalt_forecast_publications", "public", "ev_forecast_publications"])
    assert pubs == ["cobalt_forecast_publications", "ev_forecast_publications"]
    assert others == ["locations", "public"]


def test_strategy_selection():
    assert strategy_for(VERSIONED, publication_mode=True) is Strategy.VERSION_REPLACE
    assert strategy_for(NO_VERSION, publication_mode=True) is Strategy.TRUNCATE_REPLACE
    assert strategy_for(VERSIONED, publication_mode=False) is Strategy.INSERT_NEW


def test_version_table_without_versions_is_skipped_never_truncated():
    # Schema without publication_versions (or no versions selected): source_pv is None.
    plans = plan_target(
        tables=[VERSIONED, NO_VERSION],
        strategies={"prices": Strategy.VERSION_REPLACE, "system_pricing": Strategy.TRUNCATE_REPLACE},
        target_catalog={"prices": VERSIONED, "system_pricing": NO_VERSION},
        source_pv=None,
    )
    by_name = {p.table.name: p for p in plans}
    assert not by_name["prices"].ready
    assert by_name["prices"].messages == [
        "has version_id but no publication versions to filter by: skipped (never truncated)"
    ]
    assert by_name["system_pricing"].ready


def test_compare_tables_reports_type_nullability_and_missing_columns():
    target = table(
        "prices",
        [col("id", "uuid", True), col("version_id", "uuid", False), col("value", "integer"), col("extra")],
    )
    diffs = compare_tables(VERSIONED, target)
    assert "column version_id: nullability differs (source NOT NULL, target NULL)" in diffs
    assert "column value: type differs (source numeric, target integer)" in diffs
    assert "column extra: exists only in target" in diffs
    assert compare_tables(VERSIONED, VERSIONED) == []
    assert compare_tables(VERSIONED, None) == ["table does not exist in target"]


def test_compare_ignores_column_order():
    reordered = table("prices", list(reversed(VERSIONED.columns)))
    assert compare_tables(VERSIONED, reordered) == []


def test_mismatching_table_is_skipped_and_others_continue():
    bad_target = table("prices", [col("id", "uuid", True), col("version_id", "uuid", True)])
    plans = plan_target(
        tables=[VERSIONED, NO_VERSION],
        strategies={"prices": Strategy.VERSION_REPLACE, "system_pricing": Strategy.TRUNCATE_REPLACE},
        target_catalog={"prices": bad_target, "system_pricing": NO_VERSION, "publication_versions": PV},
        source_pv=PV,
    )
    by_name = {p.table.name: p for p in plans}
    assert not by_name["prices"].ready
    assert "column value: exists only in source" in by_name["prices"].messages
    assert by_name["system_pricing"].ready


def test_truncate_table_has_notice_and_incoming_fk_blocks_it():
    referenced = table(
        "system_pricing",
        NO_VERSION.columns,
        incoming=[fk("fk_child", f"{SCHEMA}.child", f"{SCHEMA}.system_pricing")],
    )
    ok = plan_target([NO_VERSION], {"system_pricing": Strategy.TRUNCATE_REPLACE}, {"system_pricing": NO_VERSION}, None)
    assert ok[0].ready
    assert any("has no version_id" in m and "TRUNCATE" in m for m in ok[0].messages)

    blocked = plan_target([NO_VERSION], {"system_pricing": Strategy.TRUNCATE_REPLACE}, {"system_pricing": referenced}, None)
    assert not blocked[0].ready
    assert any("fk_child" in m and "CASCADE" in m for m in blocked[0].messages)


def test_outgoing_fk_warns_about_parent_table():
    child = table(
        "system_pricing",
        NO_VERSION.columns,
        outgoing=[fk("fk_region", f"{SCHEMA}.system_pricing", "locations.regions")],
    )
    plans = plan_target([child], {"system_pricing": Strategy.TRUNCATE_REPLACE}, {"system_pricing": child}, None)
    assert plans[0].ready
    assert any("locations.regions" in m for m in plans[0].messages)


def test_publication_versions_mismatch_skips_version_tables_only():
    bad_pv = table("publication_versions", [col("id", "uuid", True)])
    plans = plan_target(
        tables=[VERSIONED, NO_VERSION],
        strategies={"prices": Strategy.VERSION_REPLACE, "system_pricing": Strategy.TRUNCATE_REPLACE},
        target_catalog={"prices": VERSIONED, "system_pricing": NO_VERSION, "publication_versions": bad_pv},
        source_pv=PV,
    )
    by_name = {p.table.name: p for p in plans}
    assert not by_name["prices"].ready
    assert any("publication_versions" in m for m in by_name["prices"].messages)
    assert by_name["system_pricing"].ready


def test_insert_new_without_unique_key_warns():
    plain = table("events", [col("value")], schema="public", pk=(), unique=False)
    plans = plan_target([plain], {"events": Strategy.INSERT_NEW}, {"events": plain}, None)
    assert plans[0].ready
    assert any("no PK/unique" in m for m in plans[0].messages)


def test_topo_order_puts_parents_first():
    parent = table("parent", [col("id")])
    child = table("child", [col("id")], outgoing=[fk("fk_p", f"{SCHEMA}.child", f"{SCHEMA}.parent")])
    selfref = table("tree", [col("id")], outgoing=[fk("fk_t", f"{SCHEMA}.tree", f"{SCHEMA}.tree")])
    names = [t.name for t in topo_order([child, selfref, parent])]
    assert names.index("parent") < names.index("child")
    assert "tree" in names
