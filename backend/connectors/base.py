"""The contract every database connector follows.

The agent, the KB cache and the API only talk to this interface. Adding a new
database (MySQL, Postgres, ...) means writing one connector class and adding it
to ``backend/connectors/registry.py``. Nothing else changes.
"""

from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, ClassVar

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from backend.config import ConnectionConfig


class QueryError(Exception):
    """A query failed in the database. The message is safe to show the SQL writer for a retry."""


class ColumnMeta(BaseModel):
    """One column. Nested fields use dot paths, e.g. ``device.category``."""

    name: str
    data_type: str
    mode: str | None = None  # NULLABLE / REQUIRED / REPEATED in BigQuery
    description: str = ""
    is_partition: bool = False
    clustering_order: int | None = None


class TableMeta(BaseModel):
    """One table or view, as read from the database catalog."""

    connection: str
    schema_name: str  # dataset in BigQuery, database in MySQL
    table_name: str
    qualified_name: str  # the name to use in SQL, e.g. "my-project.sales.orders"
    table_type: str = "TABLE"
    description: str = ""
    columns: list[ColumnMeta] = Field(default_factory=list)
    row_count: int | None = None
    size_bytes: int | None = None

    def ddl_hash(self) -> str:
        """SHA-256 of everything that changes what the table means.

        Row counts and sizes are left out on purpose: they change all the time
        without the table needing a new summary.
        """
        payload = {
            "qualified_name": self.qualified_name,
            "table_type": self.table_type,
            "description": self.description,
            "columns": [column.model_dump() for column in self.columns],
        }
        raw = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class QueryResult(BaseModel):
    """The outcome of ``execute_query``.

    ``df`` is None when ``too_large`` is True: the rows were counted but not
    downloaded, so the user can be asked to narrow the question.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    sql: str
    df: pd.DataFrame | None = None
    total_rows: int = 0
    too_large: bool = False
    bytes_processed: int | None = None
    duration_sec: float | None = None


class BaseDBConnector(ABC):
    """Base class for all connectors. Subclasses set ``type_name``."""

    type_name: ClassVar[str]

    def __init__(self, config: ConnectionConfig):
        self.config = config
        self.name = config.name

    @property
    @abstractmethod
    def dialect(self) -> str:
        """SQLGlot dialect name, e.g. "bigquery" or "mysql"."""

    @abstractmethod
    def connect(self) -> None:
        """Create the client or connection pool. Must be read-only."""

    @abstractmethod
    def fetch_raw_metadata(self) -> list[TableMeta]:
        """Read every allowed table and its columns from the database catalog."""

    @abstractmethod
    def sample_column_values(
        self, table: TableMeta, columns: list[str], limit: int = 15
    ) -> dict[str, list[str]]:
        """Return the distinct values of the low-cardinality columns among ``columns``.

        Columns with more than ``limit`` distinct values are left out, so the
        result only holds enum-like columns (status, country, plan, ...).
        """

    @abstractmethod
    def dry_run_or_validate(self, query: str) -> tuple[bool, str]:
        """Check a query without running it. Returns (ok, message)."""

    @abstractmethod
    def execute_query(self, query: str, max_rows: int | None = None) -> QueryResult:
        """Run a validated, read-only query. Raises QueryError when the database rejects it."""

    def sql_hints(self) -> str:
        """Dialect-specific rules for the SQL-writing prompt."""
        return ""

    def close(self) -> None:  # noqa: B027 - optional hook; most connectors need nothing here
        """Release resources. Optional."""
