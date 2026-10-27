"""Flyway history check and local migration runs against real PostgreSQL databases.

Opt-in like test_engine_integration: set DBSYNC_TEST_ADMIN_URL to a role that can
CREATE/DROP DATABASE and roles.
"""

from __future__ import annotations

import os

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from dbsync_publications import migrations

ADMIN_URL = os.environ.get("DBSYNC_TEST_ADMIN_URL")
pytestmark = pytest.mark.skipif(not ADMIN_URL, reason="DBSYNC_TEST_ADMIN_URL not set")

SOURCE_HISTORY = """
INSERT INTO flyway.flyway_schema_history VALUES
    (1, '1', 'existing production schema', 'BASELINE', 'existing production schema', NULL, 'migrator', now(), 0, true),
    (2, '2', 'create items', 'SQL', 'V2__create_items.sql', 1217294286, 'migrator', now(), 12, true),
    (3, '3', 'index items', 'SQL', 'V3__index_items.sql', -250399985, 'migrator', now(), 7, true),
    (4, '4', 'broken', 'SQL', 'V4__broken.sql', 42, 'migrator', now(), 3, true);
"""

FILES = {
    "V1__baseline.sql": "CREATE TABLE never_run (id int);",
    "V2__create_items.sql": "CREATE SCHEMA demo;\nCREATE TABLE demo.items (id int PRIMARY KEY, name text);\n"
    "INSERT INTO demo.items VALUES (1, 'a;b');\n",
    "V3__index_items.sql": "-- concurrent index\nCREATE INDEX CONCURRENTLY IF NOT EXISTS items_name_idx ON demo.items (name);\n",
    "V4__broken.sql": "CREATE TABLE demo.half (id int);\nSELECT * FROM missing_table;\n",
}


def _url(dbname: str) -> str:
    return make_conninfo(ADMIN_URL, dbname=dbname)


@pytest.fixture
def dbs(tmp_path):
    names = ["dbsync_mig_src", "dbsync_mig_tgt"]
    with psycopg.connect(ADMIN_URL, autocommit=True) as admin:
        for name in names:
            admin.execute(f"DROP DATABASE IF EXISTS {name}")
            admin.execute(f"CREATE DATABASE {name}")
    source = psycopg.connect(_url(names[0]), autocommit=True)
    target = psycopg.connect(_url(names[1]), autocommit=True)
    source.execute(migrations.HISTORY_DDL + SOURCE_HISTORY)
    for name, text in FILES.items():
        (tmp_path / name).write_text(text)
    yield source, target, migrations.load_repo(tmp_path)
    source.close()
    target.close()
    with psycopg.connect(ADMIN_URL, autocommit=True) as admin:
        for name in names:
            admin.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")


def _history(conn):
    return conn.execute(
        "SELECT version, description, type, script, checksum, success FROM flyway.flyway_schema_history "
        "ORDER BY installed_rank"
    ).fetchall()


def test_runs_missing_migrations_and_copies_identical_history_rows(dbs):
    source, target, repo = dbs
    target.execute(
        migrations.HISTORY_DDL
        + """INSERT INTO flyway.flyway_schema_history VALUES
        (1, '1', 'existing production schema', 'BASELINE', 'existing production schema', NULL, 'x', now(), 0, true),
        (2, '2', 'create items', 'SQL', 'V2__create_items.sql', 1217294286, 'x', now(), 1, false);"""
    )
    status = migrations.migration_status(repo, migrations.read_history(source), migrations.read_history(target))
    assert [r.version for r in status.runnable] == ["2", "3", "4"]

    for row in status.runnable[:2]:
        migrations.apply_migration(target, repo[row.version], row)

    assert target.execute("SELECT id, name FROM demo.items").fetchall() == [(1, "a;b")]
    assert target.execute("SELECT to_regclass('demo.items_name_idx') IS NOT NULL").fetchone() == (True,)
    assert target.execute("SELECT to_regclass('public.never_run')").fetchone() == (None,)
    # The failed V2 row is replaced; copied rows match the source exactly.
    assert _history(target) == [
        ("1", "existing production schema", "BASELINE", "existing production schema", None, True),
        ("2", "create items", "SQL", "V2__create_items.sql", 1217294286, True),
        ("3", "index items", "SQL", "V3__index_items.sql", -250399985, True),
    ]
    installed_by = target.execute("SELECT DISTINCT installed_by FROM flyway.flyway_schema_history WHERE installed_rank > 1")
    assert installed_by.fetchall() == [(target.execute("SELECT current_user").fetchone()[0],)]

    # A failing transactional migration leaves no partial object and no history row.
    with pytest.raises(psycopg.errors.UndefinedTable):
        migrations.apply_migration(target, repo["4"], status.runnable[2])
    assert target.execute("SELECT to_regclass('demo.half')").fetchone() == (None,)
    assert [v for v, *_ in _history(target)] == ["1", "2", "3"]


def test_bootstrap_copies_source_history_as_is(dbs):
    source, target, repo = dbs
    assert migrations.read_history(target) is None

    source_rows = migrations.read_history(source)
    migrations.bootstrap_history(target, source_rows)

    assert migrations.read_history(target) == source_rows
    assert migrations.migration_status(repo, source_rows, migrations.read_history(target)).runnable == []


def test_history_without_read_permission_raises(dbs):
    source, _, _ = dbs
    with psycopg.connect(ADMIN_URL, autocommit=True) as admin:
        admin.execute("DROP ROLE IF EXISTS dbsync_no_flyway")
        admin.execute("CREATE ROLE dbsync_no_flyway LOGIN PASSWORD 'x'")
    try:
        url = make_conninfo(_url("dbsync_mig_src"), user="dbsync_no_flyway", password="x")
        with psycopg.connect(url, autocommit=True) as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
            migrations.read_history(conn)
    finally:
        with psycopg.connect(ADMIN_URL, autocommit=True) as admin:
            admin.execute("DROP ROLE IF EXISTS dbsync_no_flyway")


def test_cli_check_continues_with_empty_source_history(dbs, monkeypatch, tmp_path):
    from dbsync_publications import cli
    from dbsync_publications.config import Settings

    source, target, _ = dbs
    source.execute("DELETE FROM flyway.flyway_schema_history")
    monkeypatch.setattr(cli, "ask", lambda question: True)
    settings = Settings(_url("dbsync_mig_src"), {"TARGET_1": _url("dbsync_mig_tgt")}, tmp_path)

    cli.check_migrations(settings, source, {"TARGET_1": target})

    assert migrations.read_history(target) is None  # nothing to copy, nothing run
