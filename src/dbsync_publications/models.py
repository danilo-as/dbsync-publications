"""Domain models shared by the catalog, planner and sync engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

VERSION_COLUMN = "version_id"
VERSIONS_TABLE = "publication_versions"
PUBLICATION_SUFFIX = "publications"


@dataclass(frozen=True)
class Column:
    name: str
    data_type: str
    not_null: bool
    generated: bool = False
    identity_always: bool = False


@dataclass(frozen=True)
class ForeignKey:
    name: str
    table: str  # qualified referencing table, e.g. "schema.child"
    referenced: str  # qualified referenced table, e.g. "schema.parent"


@dataclass(frozen=True)
class TableInfo:
    schema: str
    name: str
    columns: tuple[Column, ...]
    primary_key: tuple[str, ...] = ()
    has_unique_key: bool = False
    incoming_fks: tuple[ForeignKey, ...] = ()
    outgoing_fks: tuple[ForeignKey, ...] = ()

    @property
    def qualified(self) -> str:
        return f"{self.schema}.{self.name}"

    @property
    def has_version_id(self) -> bool:
        return any(c.name == VERSION_COLUMN for c in self.columns)

    @property
    def version_id_type(self) -> str | None:
        return next((c.data_type for c in self.columns if c.name == VERSION_COLUMN), None)

    @property
    def copy_columns(self) -> list[str]:
        """Columns that can be written explicitly (generated columns excluded)."""
        return [c.name for c in self.columns if not c.generated]

    @property
    def has_identity_always(self) -> bool:
        return any(c.identity_always for c in self.columns)


class Strategy(Enum):
    VERSION_REPLACE = "DELETE by version_id + COPY"
    TRUNCATE_REPLACE = "TRUNCATE + COPY"
    INSERT_NEW = "COPY to staging + INSERT ON CONFLICT DO NOTHING"


@dataclass
class TablePlan:
    table: TableInfo
    strategy: Strategy
    ready: bool = True
    messages: list[str] = field(default_factory=list)
