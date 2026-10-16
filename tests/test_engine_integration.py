"""End-to-end sync against real PostgreSQL databases.

Opt-in: set DBSYNC_TEST_ADMIN_URL to a role that can CREATE/DROP DATABASE, e.g.
postgresql://postgres:secret@localhost:5432/postgres. The test creates and
drops throwaway databases named dbsync_it_*.
"""

from __future__ import annotations

import os

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from dbsync_publications.engine import build_plan, execute_plan

ADMIN_URL = os.environ.get("DBSYNC_TEST_ADMIN_URL")
pytestmark = pytest.mark.skipif(not ADMIN_URL, reason="DBSYNC_TEST_ADMIN_URL not set")

V1 = "11111111-1111-1111-1111-111111111111"
V2 = "22222222-2222-2222-2222-222222222222"

DDL = """
CREATE SCHEMA locations;
CREATE TABLE locations.countries (id int PRIMARY KEY, code text NOT NULL);
CREATE TABLE locations.regions (
    id int PRIMARY KEY, name text NOT NULL, country_id int REFERENCES locations.countries(id));
CREATE SCHEMA demo_publications;
CREATE TABLE demo_publications.publication_versions (
    id uuid PRIMARY KEY, publication_year int NOT NULL, publication_quarter int NOT NULL,
    version int NOT NULL, latest boolean NOT NULL DEFAULT false);
CREATE TABLE demo_publications.prices (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    version_id uuid NOT NULL REFERENCES demo_publications.publication_versions(id),
    value {value_type});
CREATE TABLE demo_publications.system_pricing (
    id int PRIMARY KEY, region_id int REFERENCES locations.regions(id), value numeric);
"""

SOURCE_DATA = f"""
INSERT INTO locations.countries VALUES (1, 'AR'), (2, 'CL');
INSERT INTO locations.regions VALUES (1, 'North', 1), (2, 'South', 1), (3, 'East', 2);
INSERT INTO demo_publications.publication_versions VALUES
    ('{V1}', 2026, 2, 1, false), ('{V2}', 2026, 3, 1, true);
INSERT INTO demo_publications.prices (version_id, value) VALUES
    ('{V1}', 10), ('{V1}', 11), ('{V2}', 20), ('{V2}', 21), ('{V2}', 22);
INSERT INTO demo_publications.system_pricing VALUES (1, 1, 5.5), (2, 2, 6.5);
"""


def _url(dbname: str) -> str:
    return make_conninfo(ADMIN_URL, dbname=dbname)


@pytest.fixture
def databases():
    names = ["dbsync_it_src", "dbsync_it_t1", "dbsync_it_t2"]
    with psycopg.connect(ADMIN_URL, autocommit=True) as admin:
        for name in names:
            admin.execute(f"DROP DATABASE IF EXISTS {name}")
            admin.execute(f"CREATE DATABASE {name}")
    conns = {name: psycopg.connect(_url(name), autocommit=True) for name in names}
    conns["dbsync_it_src"].execute(DDL.format(value_type="numeric") + SOURCE_DATA)
    conns["dbsync_it_t1"].execute(DDL.format(value_type="numeric"))
    # Target 2 drifts: prices.value is integer instead of numeric.
    conns["dbsync_it_t2"].execute(DDL.format(value_type="integer"))
    yield conns["dbsync_it_src"], {"target_1": conns["dbsync_it_t1"], "target_2": conns["dbsync_it_t2"]}
    for conn in conns.values():
        conn.close()
    with psycopg.connect(ADMIN_URL, autocommit=True) as admin:
        for name in names:
            admin.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")


def _rows(conn, query):
    return conn.execute(query).fetchall()


def test_publication_sync_replaces_selected_versions_and_skips_drifted_table(databases):
    source, targets = databases
    t1, t2 = targets["target_1"], targets["target_2"]
    for target in (t1, t2):
        target.execute(f"""
            INSERT INTO locations.regions VALUES (1, 'North'), (2, 'South');
            INSERT INTO demo_publications.publication_versions VALUES ('{V1}', 2026, 2, 1, true);
            INSERT INTO demo_publications.prices (version_id, value) VALUES ('{V1}', 99);
            INSERT INTO demo_publications.system_pricing VALUES (9, 1, 1.0);
        """)

    plan = build_plan(
        source, targets, schema="demo_publications",
        table_names=["prices", "system_pricing"], version_ids=[V1], publication_mode=True,
    )
    t2_plans = {p.table.name: p for p in plan.targets["target_2"]}
    assert not t2_plans["prices"].ready
    assert "column value: type differs (source numeric, target integer)" in t2_plans["prices"].messages

    results = {r.name: r for r in execute_plan(source, targets, plan)}
    assert results["target_1"].ok and results["target_2"].ok, results

    # Target 1: stale v1 row replaced by source v1 rows; v2 never downloaded.
    assert _rows(t1, "SELECT version_id::text, value FROM demo_publications.prices ORDER BY value") == [
        (V1, 10), (V1, 11)
    ]
    # publication_versions row upserted from source (latest flag updated).
    assert _rows(t1, "SELECT id::text, latest FROM demo_publications.publication_versions") == [(V1, False)]
    # Table without version_id is truncated and fully reloaded.
    assert _rows(t1, "SELECT id, value FROM demo_publications.system_pricing ORDER BY id") == [(1, 5.5), (2, 6.5)]
    # Regions already exist; the country they reference in source is added (insert only).
    assert results["target_1"].tables == {
        "locations.countries (parent rows)": 1, "locations.regions (parent rows)": 0,
        "prices": 2, "system_pricing": 2,
    }

    # Target 2: drifted prices untouched, system_pricing still synced.
    assert _rows(t2, "SELECT value FROM demo_publications.prices") == [(99,)]
    assert _rows(t2, "SELECT id FROM demo_publications.system_pricing ORDER BY id") == [(1,), (2,)]
    assert results["target_2"].tables == {
        "locations.countries (parent rows)": 1, "locations.regions (parent rows)": 0, "system_pricing": 2,
    }


def test_other_schema_inserts_only_new_rows(databases):
    source, targets = databases
    t1 = targets["target_1"]
    t1.execute("INSERT INTO locations.regions VALUES (1, 'Norte')")

    plan = build_plan(
        source, {"target_1": t1}, schema="locations",
        table_names=["regions"], version_ids=[], publication_mode=False,
    )
    results = execute_plan(source, {"target_1": t1}, plan)

    assert results[0].ok
    assert results[0].tables == {"locations.countries (parent rows)": 2, "regions": 2}
    # Existing row is kept as-is; only the new ones are added, after their countries.
    assert _rows(t1, "SELECT id, name FROM locations.regions ORDER BY id") == [
        (1, "Norte"), (2, "South"), (3, "East")
    ]
    assert _rows(t1, "SELECT id FROM locations.countries ORDER BY id") == [(1,), (2,)]


def test_version_table_is_never_truncated_without_versions(databases):
    source, targets = databases
    t1 = targets["target_1"]
    t1.execute(f"""
        INSERT INTO locations.regions VALUES (1, 'North'), (2, 'South');
        INSERT INTO demo_publications.publication_versions VALUES ('{V1}', 2026, 2, 1, true);
        INSERT INTO demo_publications.prices (version_id, value) VALUES ('{V1}', 99);
    """)

    plan = build_plan(
        source, {"target_1": t1}, schema="demo_publications",
        table_names=["prices", "system_pricing"], version_ids=[], publication_mode=True,
    )
    results = execute_plan(source, {"target_1": t1}, plan)

    assert results[0].ok
    assert results[0].tables == {
        "locations.countries (parent rows)": 1, "locations.regions (parent rows)": 0, "system_pricing": 2,
    }
    assert _rows(t1, "SELECT value FROM demo_publications.prices") == [(99,)]


def test_missing_parent_rows_are_copied_and_existing_ones_kept(databases):
    source, targets = databases
    t1 = targets["target_1"]
    # Region 1 exists with another name; region 2 and both countries are missing.
    t1.execute("INSERT INTO locations.regions VALUES (1, 'Norte')")

    plan = build_plan(
        source, {"target_1": t1}, schema="demo_publications",
        table_names=["system_pricing"], version_ids=[], publication_mode=True,
    )
    notes = plan.targets["target_1"][0].messages
    assert "missing parent rows in locations.regions are copied from source (ON CONFLICT DO NOTHING)" in notes
    results = execute_plan(source, {"target_1": t1}, plan)

    assert results[0].ok, results
    # Only referenced parents: region 3 (East) and country 2 (CL) are never copied.
    assert _rows(t1, "SELECT id, name, country_id FROM locations.regions ORDER BY id") == [
        (1, "Norte", None), (2, "South", 1)
    ]
    assert _rows(t1, "SELECT id, code FROM locations.countries") == [(1, "AR")]
    assert results[0].tables == {
        "locations.countries (parent rows)": 1, "locations.regions (parent rows)": 1, "system_pricing": 2,
    }


def test_parent_table_differing_in_target_skips_child(databases):
    source, targets = databases
    t1 = targets["target_1"]
    t1.execute("ALTER TABLE locations.countries ADD COLUMN extra text")

    plan = build_plan(
        source, {"target_1": t1}, schema="demo_publications",
        table_names=["system_pricing"], version_ids=[], publication_mode=True,
    )
    pricing = plan.targets["target_1"][0]
    assert not pricing.ready
    assert "parent table locations.countries differs in target (column extra: exists only in target)" in (
        pricing.messages
    )
    execute_plan(source, {"target_1": t1}, plan)
    assert _rows(t1, "SELECT count(*) FROM locations.regions") == [(0,)]


def test_failure_rolls_back_whole_target(databases):
    source, targets = databases
    t1 = targets["target_1"]
    # system_pricing COPY violates a CHECK that only exists in the target.
    t1.execute(f"""
        ALTER TABLE demo_publications.system_pricing ADD CONSTRAINT small_value CHECK (value < 6);
        INSERT INTO demo_publications.publication_versions VALUES ('{V1}', 2026, 2, 1, true);
        INSERT INTO demo_publications.prices (version_id, value) VALUES ('{V1}', 99);
    """)

    plan = build_plan(
        source, {"target_1": t1}, schema="demo_publications",
        table_names=["prices", "system_pricing"], version_ids=[V1], publication_mode=True,
    )
    results = execute_plan(source, {"target_1": t1}, plan)

    assert not results[0].ok
    assert "small_value" in results[0].error
    assert _rows(t1, "SELECT value FROM demo_publications.prices") == [(99,)]
    assert _rows(t1, "SELECT latest FROM demo_publications.publication_versions") == [(True,)]
    # Parent rows inserted earlier in the same transaction are rolled back too.
    assert _rows(t1, "SELECT count(*) FROM locations.regions") == [(0,)]
