from datetime import datetime

from dbsync_publications.migrations import (
    HistoryRow,
    is_local,
    load_repo,
    migration_status,
    needs_no_transaction,
    split_statements,
    version_key,
)


def row(rank, version, script=None, success=True, type_="SQL", checksum=1):
    return HistoryRow(
        installed_rank=rank,
        version=version,
        description=f"desc {version}",
        type=type_,
        script=script or f"V{version}__x.sql",
        checksum=checksum,
        installed_by="migrator",
        installed_on=datetime(2026, 10, 1),
        execution_time=5,
        success=success,
    )


def write_repo(tmp_path, *names):
    for name in names:
        (tmp_path / name).write_text("SELECT 1;")
    return load_repo(tmp_path)


def test_load_repo_parses_versioned_files_only(tmp_path):
    repo = write_repo(
        tmp_path, "V1__baseline_2026_09_25.sql", "V202610161619__add_index.sql", "README.md", "R__view.sql"
    )
    assert sorted(repo) == ["1", "202610161619"]
    assert repo["202610161619"].description == "add index"
    assert repo["202610161619"].script == "V202610161619__add_index.sql"


def test_version_key_orders_numerically():
    assert sorted(["10", "9", "1.2", "1.10"], key=version_key) == ["1.2", "1.10", "9", "10"]


def test_split_statements_respects_quotes_comments_and_dollar_bodies():
    sql = """-- comment; with semicolon
CREATE INDEX CONCURRENTLY a ON t (x);
INSERT INTO t VALUES ('a;b');
CREATE FUNCTION f() RETURNS int AS $body$ BEGIN RETURN 1; END $body$ LANGUAGE plpgsql;
/* block; comment */ SELECT 2
"""
    statements = split_statements(sql)
    assert len(statements) == 4
    assert statements[0].endswith("CREATE INDEX CONCURRENTLY a ON t (x)")
    assert statements[1] == "INSERT INTO t VALUES ('a;b')"
    assert "RETURN 1; END" in statements[2]
    assert statements[3].endswith("SELECT 2")


def test_needs_no_transaction_detects_concurrently_outside_comments():
    assert needs_no_transaction("CREATE INDEX CONCURRENTLY IF NOT EXISTS i ON t (x);")
    assert needs_no_transaction("drop index concurrently i;")
    assert not needs_no_transaction("-- CREATE INDEX CONCURRENTLY later\nCREATE INDEX i ON t (x);")
    assert not needs_no_transaction("CREATE INDEX i ON t (x);")


def test_is_local_accepts_loopback_and_sockets_only():
    assert is_local("postgresql://u:p@localhost:5432/db")
    assert is_local("postgresql://u:p@127.0.0.1/db")
    assert is_local("postgresql:///db")
    assert not is_local("postgresql://u:p@db.example.rds.amazonaws.com/db")


def test_runnable_are_applied_in_source_and_missing_on_target(tmp_path):
    repo = write_repo(tmp_path, "V1__base.sql", "V2__a.sql", "V3__b.sql", "V4__c.sql", "V5__new.sql")
    source = [row(1, "1", type_="BASELINE"), row(2, "2"), row(3, "3"), row(4, "4")]
    target = [row(1, "1", type_="BASELINE"), row(2, "2"), row(3, "3", success=False)]

    status = migration_status(repo, source, target)

    assert [r.version for r in status.runnable] == ["3", "4"]
    assert [r.version for r in status.failed] == ["3"]
    assert status.missing_files == []
    # V5 exists only in the repository: not applied in source yet, so never runnable.
    assert status.not_in_source == ["5"]


def test_source_rows_without_repo_file_are_reported(tmp_path):
    repo = write_repo(tmp_path, "V1__base.sql")
    status = migration_status(repo, [row(1, "1", type_="BASELINE"), row(2, "2")], [row(1, "1", type_="BASELINE")])
    assert status.runnable == []
    assert status.missing_files == ["2"]


def test_without_source_history_pending_is_informative_only(tmp_path):
    repo = write_repo(tmp_path, "V1__base.sql", "V2__a.sql", "V3__b.sql")
    status = migration_status(repo, None, [row(1, "1", type_="BASELINE"), row(2, "2")])
    assert status.runnable == []
    assert status.repo_pending == ["3"]


def test_target_without_history_has_nothing_runnable(tmp_path):
    repo = write_repo(tmp_path, "V1__base.sql", "V2__a.sql")
    status = migration_status(repo, [row(1, "1", type_="BASELINE"), row(2, "2")], None)
    assert not status.target_has_history
    assert status.runnable == []
