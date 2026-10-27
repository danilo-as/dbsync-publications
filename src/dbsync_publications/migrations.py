"""Flyway history check against the bmi-data-warehouse-sql migrations directory.

The source history is the reference: a migration can run on a local target when the
source applied it successfully and the target has not. After its SQL runs, the source
history row is copied, so version, description, type, script and checksum stay
identical to what Flyway recorded.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field, fields
from datetime import datetime
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

HISTORY_SCHEMA = "flyway"
HISTORY_TABLE = "flyway_schema_history"
BASELINE_TYPE = "BASELINE"
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}

_FILE_PATTERN = re.compile(r"^V(?P<version>\d+(?:[._]\d+)*)__(?P<description>.+)\.sql$")
_DOLLAR_TAG = re.compile(r"\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$")
_NON_TRANSACTIONAL = re.compile(r"\bconcurrently\b", re.IGNORECASE)

HISTORY_DDL = """
CREATE SCHEMA IF NOT EXISTS flyway;
CREATE TABLE IF NOT EXISTS flyway.flyway_schema_history (
    installed_rank integer NOT NULL,
    version varchar(50),
    description varchar(200) NOT NULL,
    type varchar(20) NOT NULL,
    script varchar(1000) NOT NULL,
    checksum integer,
    installed_by varchar(100) NOT NULL,
    installed_on timestamp NOT NULL DEFAULT now(),
    execution_time integer NOT NULL,
    success boolean NOT NULL,
    CONSTRAINT flyway_schema_history_pk PRIMARY KEY (installed_rank)
);
CREATE INDEX IF NOT EXISTS flyway_schema_history_s_idx ON flyway.flyway_schema_history (success);
"""


@dataclass(frozen=True)
class MigrationFile:
    version: str
    description: str
    script: str
    path: Path


@dataclass(frozen=True)
class HistoryRow:
    installed_rank: int
    version: str | None
    description: str
    type: str
    script: str
    checksum: int | None
    installed_by: str
    installed_on: datetime
    execution_time: int
    success: bool


HISTORY_COLUMNS = [f.name for f in fields(HistoryRow)]


@dataclass
class MigrationStatus:
    target_has_history: bool
    runnable: list[HistoryRow] = field(default_factory=list)  # source rows to run and copy, in version order
    failed: list[HistoryRow] = field(default_factory=list)  # target rows with success = false
    missing_files: list[str] = field(default_factory=list)  # applied in source, file not in repository
    not_in_source: list[str] = field(default_factory=list)  # repository versions the source has not applied
    repo_pending: list[str] = field(default_factory=list)  # repository versions the target has not applied


def version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.split(r"[._]", version))


def load_repo(directory: Path) -> dict[str, MigrationFile]:
    """Versioned migrations (``V<version>__<description>.sql``) keyed by version."""
    repo: dict[str, MigrationFile] = {}
    for path in sorted(directory.glob("V*.sql")):
        match = _FILE_PATTERN.match(path.name)
        if match:
            version = match["version"].replace("_", ".")
            repo[version] = MigrationFile(version, match["description"].replace("_", " "), path.name, path)
    return repo


def is_local(url: str) -> bool:
    """True for loopback hosts and Unix sockets: the only targets migrations may run on."""
    host = conninfo_to_dict(url).get("host") or ""
    return not host or host.startswith("/") or host in LOCAL_HOSTS


def _scan(text: str) -> tuple[list[str], str]:
    """Split SQL on top-level semicolons; also return the code with comments and literals removed."""
    statements: list[str] = []
    code: list[str] = []
    start = i = 0
    while i < len(text):
        ch = text[i]
        if text.startswith("--", i):
            end = text.find("\n", i)
            i = len(text) if end == -1 else end
        elif text.startswith("/*", i):
            depth, i = 1, i + 2
            while i < len(text) and depth:
                if text.startswith("/*", i):
                    depth, i = depth + 1, i + 2
                elif text.startswith("*/", i):
                    depth, i = depth - 1, i + 2
                else:
                    i += 1
        elif ch in "'\"":
            escapes = ch == "'" and i > 0 and text[i - 1] in "eE"
            i += 1
            while i < len(text):
                if escapes and text[i] == "\\":
                    i += 2
                elif text[i] == ch:
                    if text.startswith(ch * 2, i):
                        i += 2
                    else:
                        break
                else:
                    i += 1
            i += 1
            code.append(" ")
        elif ch == "$" and (match := _DOLLAR_TAG.match(text, i)) and not _ends_identifier(text, i):
            end = text.find(match.group(), match.end())
            i = len(text) if end == -1 else end + len(match.group())
            code.append(" ")
        elif ch == ";":
            statements.append(text[start:i])
            code.append(";")
            i += 1
            start = i
        else:
            code.append(ch)
            i += 1
    statements.append(text[start:])
    return [s.strip() for s in statements if _has_code(s)], "".join(code)


def _ends_identifier(text: str, i: int) -> bool:
    """A ``$`` right after an identifier character is part of the identifier, not a dollar quote."""
    return i > 0 and (text[i - 1].isalnum() or text[i - 1] == "_")


def _has_code(statement: str) -> bool:
    return bool(_scan_code(statement).strip())


def _scan_code(statement: str) -> str:
    stripped = re.sub(r"--[^\n]*", "", statement)
    return re.sub(r"/\*.*?\*/", "", stripped, flags=re.DOTALL)


def split_statements(text: str) -> list[str]:
    return _scan(text)[0]


def needs_no_transaction(text: str) -> bool:
    """Statements such as CREATE INDEX CONCURRENTLY cannot run inside a transaction."""
    return bool(_NON_TRANSACTIONAL.search(_scan(text)[1]))


def _applied(rows: list[HistoryRow]) -> tuple[set[str], tuple[int, ...] | None]:
    applied = {r.version for r in rows if r.success and r.version}
    baselines = [version_key(r.version) for r in rows if r.type == BASELINE_TYPE and r.success and r.version]
    return applied, max(baselines, default=None)


def _pending(repo: dict[str, MigrationFile], rows: list[HistoryRow]) -> list[str]:
    applied, baseline = _applied(rows)
    return sorted(
        (v for v in repo if v not in applied and (baseline is None or version_key(v) > baseline)),
        key=version_key,
    )


def migration_status(
    repo: dict[str, MigrationFile],
    source: list[HistoryRow] | None,
    target: list[HistoryRow] | None,
) -> MigrationStatus:
    """Compare one target with the source history and the repository.

    ``None`` means the history could not be read (missing table or no permission).
    """
    status = MigrationStatus(target_has_history=target is not None)
    if source is not None:
        status.not_in_source = _pending(repo, source)
    if target is None:
        return status

    status.failed = [r for r in target if not r.success]
    status.repo_pending = _pending(repo, target)
    if source is None:
        return status

    applied, baseline = _applied(target)
    candidates = sorted(
        (r for r in source if r.success and r.version and r.type != BASELINE_TYPE),
        key=lambda r: version_key(r.version or ""),
    )
    for candidate in candidates:
        version = candidate.version or ""
        if version in applied or (baseline is not None and version_key(version) <= baseline):
            continue
        if version in repo:
            status.runnable.append(candidate)
        else:
            status.missing_files.append(version)
    return status


def _history() -> sql.Composed:
    return sql.SQL("{}.{}").format(sql.Identifier(HISTORY_SCHEMA), sql.Identifier(HISTORY_TABLE))


def read_history(conn: psycopg.Connection) -> list[HistoryRow] | None:
    """Rows of flyway_schema_history, or None when the table does not exist.

    Permission errors propagate so the caller can report them.
    """
    exists = conn.execute("SELECT to_regclass(%s)", (f"{HISTORY_SCHEMA}.{HISTORY_TABLE}",)).fetchone()
    if not exists or exists[0] is None:
        return None
    query = sql.SQL("SELECT {} FROM {} ORDER BY installed_rank").format(
        sql.SQL(", ").join(sql.Identifier(c) for c in HISTORY_COLUMNS), _history()
    )
    return [HistoryRow(*values) for values in conn.execute(query).fetchall()]


def _insert_row(conn: psycopg.Connection, row: HistoryRow) -> None:
    conn.execute(
        sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
            _history(),
            sql.SQL(", ").join(sql.Identifier(c) for c in HISTORY_COLUMNS),
            sql.SQL(", ").join(sql.Placeholder() * len(HISTORY_COLUMNS)),
        ),
        [getattr(row, c) for c in HISTORY_COLUMNS],
    )


def bootstrap_history(conn: psycopg.Connection, rows: list[HistoryRow]) -> None:
    """Create the history table on a target that has none and copy the source rows as-is."""
    with conn.transaction():
        conn.execute(HISTORY_DDL)
        for row in rows:
            _insert_row(conn, row)


def _record(conn: psycopg.Connection, source_row: HistoryRow, elapsed: int) -> None:
    """Copy the source history row; a failed row of the same version is replaced (like ``flyway repair``)."""
    conn.execute(sql.SQL("DELETE FROM {} WHERE version = %s AND NOT success").format(_history()), (source_row.version,))
    next_rank = conn.execute(sql.SQL("SELECT coalesce(max(installed_rank), 0) + 1 FROM {}").format(_history()))
    user = conn.execute("SELECT current_user").fetchone()
    _insert_row(
        conn,
        HistoryRow(
            installed_rank=next_rank.fetchone()[0],  # type: ignore[index]
            version=source_row.version,
            description=source_row.description,
            type=source_row.type,
            script=source_row.script,
            checksum=source_row.checksum,
            installed_by=user[0],  # type: ignore[index]
            installed_on=datetime.now(),
            execution_time=elapsed,
            success=True,
        ),
    )


def apply_migration(conn: psycopg.Connection, migration: MigrationFile, source_row: HistoryRow) -> int:
    """Run one migration on an autocommit connection and record the copied history row.

    Transactional migrations run in one transaction together with their history row.
    Migrations with CONCURRENTLY run statement by statement outside a transaction, as
    Flyway does; their row is recorded only after every statement succeeded. Returns
    the execution time in milliseconds.
    """
    text = migration.path.read_text(encoding="utf-8-sig")
    started = time.monotonic()
    if needs_no_transaction(text):
        for statement in split_statements(text):
            conn.execute(statement)  # type: ignore[arg-type]  # trusted repository SQL
        elapsed = int((time.monotonic() - started) * 1000)
        with conn.transaction():
            _record(conn, source_row, elapsed)
        return elapsed

    with conn.transaction():
        conn.execute(text)  # type: ignore[arg-type]  # trusted repository SQL, may hold several statements
        elapsed = int((time.monotonic() - started) * 1000)
        _record(conn, source_row, elapsed)
    return elapsed
