"""The KB cache: what the agent knows about each table, kept in one DuckDB file.

It holds the raw catalog metadata, the LLM summaries, column meanings, sample values and
the business context. Explore questions are answered from here without a BigQuery scan,
and a restart only re-curates tables whose structure changed. It is only a cache:
deleting the file costs one full curation pass on the next start.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import duckdb
from pydantic import BaseModel

from backend.connectors.base import TableMeta

BUSINESS_CONTEXT_KEY = "business_context"

# kb_kv is a small key/value table; today it only holds the business context.
SCHEMA = """
CREATE TABLE IF NOT EXISTS kb_tables (
    connection     VARCHAR NOT NULL,
    qualified_name VARCHAR NOT NULL,
    schema_name    VARCHAR NOT NULL,
    table_name     VARCHAR NOT NULL,
    table_type     VARCHAR NOT NULL,
    description    VARCHAR NOT NULL,
    summary        VARCHAR NOT NULL,
    tags           VARCHAR[] NOT NULL,
    row_count      BIGINT,
    size_bytes     BIGINT,
    column_count   INTEGER NOT NULL,
    curated        BOOLEAN NOT NULL,
    ddl_hash       VARCHAR NOT NULL,
    PRIMARY KEY (connection, qualified_name)
);

CREATE TABLE IF NOT EXISTS kb_columns (
    connection       VARCHAR NOT NULL,
    qualified_name   VARCHAR NOT NULL,
    position         INTEGER NOT NULL,
    name             VARCHAR NOT NULL,
    data_type        VARCHAR NOT NULL,
    mode             VARCHAR,
    description      VARCHAR NOT NULL,
    meaning          VARCHAR NOT NULL,
    sample_values    VARCHAR[] NOT NULL,
    is_partition     BOOLEAN NOT NULL,
    clustering_order INTEGER
);

CREATE TABLE IF NOT EXISTS kb_kv (
    key   VARCHAR PRIMARY KEY,
    value VARCHAR NOT NULL
);
"""


class ColumnInfo(BaseModel):
    """A column, with what curation added to the catalog metadata."""

    name: str
    data_type: str
    mode: str | None
    description: str
    meaning: str  # from the LLM; "" until the table is curated
    sample_values: list[str]  # distinct values, only for low-cardinality STRING columns
    is_partition: bool
    clustering_order: int | None


class TableInfo(BaseModel):
    """One catalog entry: enough to pick tables without loading their columns."""

    connection: str
    qualified_name: str
    schema_name: str
    table_name: str
    table_type: str
    description: str
    summary: str  # from the LLM; "" until curated
    tags: list[str]
    row_count: int | None
    size_bytes: int | None
    column_count: int
    curated: bool


class TableDetail(TableInfo):
    """A table with its columns, for metadata answers and SQL prompts."""

    columns: list[ColumnInfo]


# The SELECT lists follow the models, so each row maps straight onto a model.
TABLE_FIELDS = ", ".join(TableInfo.model_fields)
COLUMN_FIELDS = ", ".join(ColumnInfo.model_fields)


class KBCache:
    """Thread-safe access to the KB file.

    The curator thread writes while API request threads read, so every public method
    takes the same lock around the single connection. Private helpers expect the caller
    to hold it.
    """

    def __init__(self, path: str | Path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = duckdb.connect(str(path))
        self._lock = threading.Lock()
        self._db.execute(SCHEMA)

    def upsert_raw(self, table: TableMeta) -> bool:
        """Save one table's catalog metadata. Returns True when the table needs curating.

        That is when it's new, never curated, or its structure changed. A changed table
        starts over: its old summary, meanings and sample values may no longer fit. For an
        unchanged table only the row count and size are refreshed.
        """
        ddl_hash = table.ddl_hash()
        with self._transaction():
            row = self._db.execute(
                "SELECT ddl_hash, curated FROM kb_tables "
                "WHERE connection = ? AND qualified_name = ?",
                [table.connection, table.qualified_name],
            ).fetchone()
            if row is not None:
                old_hash, curated = row
                if old_hash == ddl_hash:
                    self._db.execute(
                        "UPDATE kb_tables SET row_count = ?, size_bytes = ? "
                        "WHERE connection = ? AND qualified_name = ?",
                        [table.row_count, table.size_bytes, table.connection, table.qualified_name],
                    )
                    return not curated
                self._delete(table.connection, table.qualified_name)
            self._insert(table, ddl_hash)
        return True

    def save_curation(
        self,
        connection: str,
        qualified_name: str,
        summary: str,
        tags: list[str],
        column_meanings: dict[str, str],
        sample_values: dict[str, list[str]],
    ) -> None:
        """Store the LLM summary and the profiled values, and mark the table curated.

        Columns left out of the dicts get no meaning and no values; unknown names are ignored.
        """
        key = [connection, qualified_name]
        with self._transaction():
            (updated,) = self._db.execute(
                "UPDATE kb_tables SET summary = ?, tags = ?, curated = TRUE "
                "WHERE connection = ? AND qualified_name = ?",
                [summary, tags, *key],
            ).fetchone()
            if not updated:
                raise KeyError(f"table {qualified_name} ({connection}) is not in the KB")
            rows = self._db.execute(
                "SELECT name FROM kb_columns WHERE connection = ? AND qualified_name = ?", key
            ).fetchall()
            if not rows:
                return
            params = []
            for (name,) in rows:
                params += [name, column_meanings.get(name, ""), sample_values.get(name, [])]
            values = _value_rows("(?, ?, ?)", len(rows))
            self._db.execute(
                "UPDATE kb_columns SET meaning = new.meaning, sample_values = new.sample_values "
                f"FROM (VALUES {values}) AS new (name, meaning, sample_values) "
                "WHERE connection = ? AND qualified_name = ? AND kb_columns.name = new.name",
                [*params, *key],
            )

    def remove_missing(self, connection: str, keep: set[str]) -> int:
        """Delete the tables of ``connection`` whose qualified name isn't in ``keep``.

        Used after a crawl to forget dropped tables. Returns how many were deleted.
        """
        with self._transaction():
            rows = self._db.execute(
                "SELECT qualified_name FROM kb_tables WHERE connection = ?", [connection]
            ).fetchall()
            dropped = [name for (name,) in rows if name not in keep]
            for name in dropped:
                self._delete(connection, name)
        return len(dropped)

    def list_tables(self, connection: str | None = None) -> list[TableInfo]:
        with self._lock:
            return self._select_tables(connection)

    def get_table(self, qualified_name: str, connection: str | None = None) -> TableDetail | None:
        """The table with exactly this qualified name, or None."""
        with self._lock:
            tables = self._select_tables(connection, qualified_name)
            return self._with_columns(tables[0]) if tables else None

    def find_tables(self, names: list[str], connection: str | None = None) -> list[TableDetail]:
        """Look up tables by the names a person or a model wrote, in the order given.

        A name may be "project.dataset.table", "dataset.table" or a bare table name, in any
        case and with or without backticks. A bare name matches the table in every dataset
        that has one. Unknown names are skipped and each table is returned once.
        """
        with self._lock:
            candidates = [(table, _names_for(table)) for table in self._select_tables(connection)]
            found: list[TableInfo] = []
            for name in names:
                wanted = name.replace("`", "").strip().lower()
                for table, table_names in candidates:
                    if wanted in table_names and table not in found:
                        found.append(table)
            return [self._with_columns(table) for table in found]

    def set_business_context(self, text: str) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO kb_kv (key, value) VALUES (?, ?)",
                [BUSINESS_CONTEXT_KEY, text],
            )

    def get_business_context(self) -> str:
        """The stored business context, or "" when there is none."""
        with self._lock:
            row = self._db.execute(
                "SELECT value FROM kb_kv WHERE key = ?", [BUSINESS_CONTEXT_KEY]
            ).fetchone()
        return row[0] if row else ""

    def stats(self) -> dict[str, int]:
        """Table counts for the health check: {"tables": n, "curated": m}."""
        with self._lock:
            tables, curated = self._db.execute(
                "SELECT count(*), count(*) FILTER (WHERE curated) FROM kb_tables"
            ).fetchone()
        return {"tables": tables, "curated": curated}

    def close(self) -> None:
        with self._lock:
            self._db.close()

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        """Hold the lock and run the block as one transaction, so a failure changes nothing."""
        with self._lock:
            self._db.begin()
            try:
                yield
            except BaseException:
                self._db.rollback()
                raise
            self._db.commit()

    def _insert(self, table: TableMeta, ddl_hash: str) -> None:
        """Add a table and its columns, with no curation yet."""
        self._db.execute(
            "INSERT INTO kb_tables (connection, qualified_name, schema_name, table_name, "
            "table_type, description, summary, tags, row_count, size_bytes, column_count, "
            "curated, ddl_hash) VALUES (?, ?, ?, ?, ?, ?, '', [], ?, ?, ?, FALSE, ?)",
            [
                table.connection,
                table.qualified_name,
                table.schema_name,
                table.table_name,
                table.table_type,
                table.description,
                table.row_count,
                table.size_bytes,
                len(table.columns),
                ddl_hash,
            ],
        )
        if not table.columns:
            return
        params = []
        for position, column in enumerate(table.columns):
            params += [
                table.connection,
                table.qualified_name,
                position,
                column.name,
                column.data_type,
                column.mode,
                column.description,
                column.is_partition,
                column.clustering_order,
            ]
        values = _value_rows("(?, ?, ?, ?, ?, ?, ?, '', [], ?, ?)", len(table.columns))
        self._db.execute(
            "INSERT INTO kb_columns (connection, qualified_name, position, name, data_type, mode, "
            f"description, meaning, sample_values, is_partition, clustering_order) VALUES {values}",
            params,
        )

    def _delete(self, connection: str, qualified_name: str) -> None:
        key = [connection, qualified_name]
        self._db.execute("DELETE FROM kb_columns WHERE connection = ? AND qualified_name = ?", key)
        self._db.execute("DELETE FROM kb_tables WHERE connection = ? AND qualified_name = ?", key)

    def _select_tables(
        self, connection: str | None, qualified_name: str | None = None
    ) -> list[TableInfo]:
        """Tables matching the filters that are set, sorted by connection and name."""
        sql = f"SELECT {TABLE_FIELDS} FROM kb_tables WHERE TRUE"
        params: list[str] = []
        if connection is not None:
            sql += " AND connection = ?"
            params.append(connection)
        if qualified_name is not None:
            sql += " AND qualified_name = ?"
            params.append(qualified_name)
        rows = self._fetch(f"{sql} ORDER BY connection, qualified_name", params)
        return [TableInfo(**row) for row in rows]

    def _with_columns(self, table: TableInfo) -> TableDetail:
        rows = self._fetch(
            f"SELECT {COLUMN_FIELDS} FROM kb_columns "
            "WHERE connection = ? AND qualified_name = ? ORDER BY position",
            [table.connection, table.qualified_name],
        )
        return TableDetail(**table.model_dump(), columns=[ColumnInfo(**row) for row in rows])

    def _fetch(self, sql: str, params: list[Any]) -> list[dict[str, Any]]:
        result = self._db.execute(sql, params)
        names = [column[0] for column in result.description]
        return [dict(zip(names, row, strict=True)) for row in result.fetchall()]


def _value_rows(row: str, count: int) -> str:
    """``count`` copies of a VALUES row, for writing all of a table's columns in one statement.

    DuckDB runs each executemany row as its own statement, which made the first sync of a
    big project take a minute instead of seconds.
    """
    return ", ".join([row] * count)


def _names_for(table: TableInfo) -> set[str]:
    """The lowercase names a table can be asked for by."""
    return {
        table.qualified_name.lower(),
        f"{table.schema_name}.{table.table_name}".lower(),
        table.table_name.lower(),
    }
