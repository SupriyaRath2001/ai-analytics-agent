# Adding a database

v1 ships with BigQuery only. The agent, the KB cache and the API only talk to the
`BaseDBConnector` interface in `backend/connectors/base.py`, so a new database needs:

1. one connector class,
2. one line in `backend/connectors/registry.py`,
3. a config example,
4. tests with a fake client.

The steps below use MySQL as the example.

## 1. Write the connector

Create `backend/connectors/mysql_connector.py` with a subclass of `BaseDBConnector`. Set:

- `type_name`: the `type:` value used in `config/config.yaml` (`"mysql"`).
- `dialect`: the SQLGlot dialect name (`"mysql"`). The read-only SQL check parses queries with
  it.

Then implement these methods:

| Method | What it must do |
|---|---|
| `connect()` | Create the client or connection. Use a database user that can only read. |
| `fetch_raw_metadata()` | Return one `TableMeta` per allowed table or view, with its columns as `ColumnMeta`. Log and skip a table that fails to load. |
| `sample_column_values(table, columns, limit=15)` | Return the values of text columns that have at most `limit` distinct values (status, country, plan, ...). On any error, log it and return `{}`. |
| `dry_run_or_validate(query)` | Check a query without running it. Return `(True, "<short note>")` or `(False, "<clear reason>")`. The reason is shown to the SQL writer so it can fix the query. |
| `execute_query(query, max_rows=None)` | Run the query. `max_rows` defaults to `config.max_rows_fetch`. With more rows than that, return `QueryResult(df=None, too_large=True, ...)` without downloading them. Turn database errors into `QueryError(message)`. |
| `sql_hints()` | Optional. Dialect rules for the SQL-writing prompt. |
| `close()` | Optional. Release the connection. |

Settings come from the connection's block in `config/config.yaml`. Keys that `ConnectionConfig`
doesn't define (like `host`) are kept, so you can read them as `self.config.host`.

A skeleton to start from:

```python
"""MySQL connector."""

from __future__ import annotations

import logging

from backend.connectors.base import BaseDBConnector, QueryResult, TableMeta

log = logging.getLogger(__name__)


class MySQLConnector(BaseDBConnector):
    type_name = "mysql"

    @property
    def dialect(self) -> str:
        return "mysql"

    def connect(self) -> None:
        # Open a connection as a read-only user, e.g. with PyMySQL:
        #   self.connection = pymysql.connect(host=self.config.host, port=self.config.port,
        #       user=self.config.user, password=self.config.password,
        #       database=self.config.database)
        raise NotImplementedError

    def fetch_raw_metadata(self) -> list[TableMeta]:
        # Read information_schema.TABLES and information_schema.COLUMNS for the database.
        # One TableMeta per table or view:
        #   schema_name = database, table_name = table, qualified_name = "database.table",
        #   description = TABLE_COMMENT, row_count = TABLE_ROWS (an estimate in InnoDB),
        #   size_bytes = DATA_LENGTH, columns = one ColumnMeta per column (COLUMN_COMMENT as
        #   the description).
        raise NotImplementedError

    def sample_column_values(
        self, table: TableMeta, columns: list[str], limit: int = 15
    ) -> dict[str, list[str]]:
        # For each text column: SELECT col FROM table GROUP BY col LIMIT limit + 1.
        # Keep only the columns that returned at most `limit` values.
        # On any error: log it and return {}.
        raise NotImplementedError

    def dry_run_or_validate(self, query: str) -> tuple[bool, str]:
        # MySQL has no dry run. EXPLAIN <query> checks the syntax and table access without
        # reading data. Also check that every table is in the configured database.
        raise NotImplementedError

    def execute_query(self, query: str, max_rows: int | None = None) -> QueryResult:
        # max_rows defaults to self.config.max_rows_fetch.
        # 1. Count the rows: SELECT COUNT(*) FROM (<query>) AS t
        # 2. More than max_rows: return QueryResult(sql=query, total_rows=count, too_large=True)
        # 3. Otherwise fetch the rows into a DataFrame and return it with total_rows=count.
        # Limit the run time with MAX_EXECUTION_TIME, based on self.config.query_timeout_sec.
        # Raise QueryError(str(exc)) when the driver raises.
        raise NotImplementedError

    def sql_hints(self) -> str:
        return (
            "- Quote names with backticks: `database`.`table`.\n"
            "- Use LIMIT for top-N questions and ORDER BY for time series.\n"
            "- Use NULLIF(x, 0) when dividing, to avoid division by zero.\n"
        )

    def close(self) -> None:
        # Close self.connection if connect() opened one.
        ...
```

Add the driver (for example `pymysql`) to `dependencies` in `pyproject.toml`. New dependencies
need the maintainers' OK, so mention it in your pull request.

## 2. Register the class

In `backend/connectors/registry.py`, import the class and add it to `CONNECTOR_TYPES`:

```python
from backend.connectors.mysql_connector import MySQLConnector

CONNECTOR_TYPES: dict[str, type[BaseDBConnector]] = {
    "bigquery": BigQueryConnector,
    "mysql": MySQLConnector,
}
```

## 3. Add a config example

Add a commented-out block to `config/config.yaml.example` so people can copy it:

```yaml
connections:
  - name: shop
    type: mysql
    host: ${MYSQL_HOST}
    port: 3306
    user: ${MYSQL_USER}
    password: ${MYSQL_PASSWORD}
    database: shop
    query_timeout_sec: 60
    max_rows_fetch: 50000
```

Add the new variables, empty, to `.env.example`. Passwords and keys only ever go in `.env`.

In v1 the agent answers from one connection only: the first one in `connections` that connected
at startup (cross-database questions are not in v1). A connection that fails to connect is
logged and skipped, so the agent then quietly uses the next one. To try your connector, list it
first and check the startup log, or set `enabled: false` on the BigQuery connection.

The prompts in `backend/agent/prompts.py` were written for BigQuery (GoogleSQL,
`CURRENT_DATE('<tz>')`). Put your dialect's rules in `sql_hints()` and check the prompts for
BigQuery-only wording.

## 4. Write tests with a fake client

Tests must not use the network or a real database. Skip `connect()` and give the connector a fake
connection that returns canned rows and records the SQL it receives:

```python
from backend.config import ConnectionConfig
from backend.connectors.mysql_connector import MySQLConnector
from backend.connectors.registry import CONNECTOR_TYPES


class FakeCursor:
    """Acts like a DB-API cursor: returns canned rows and records every query."""

    def __init__(self, rows):
        self.rows = rows
        self.queries = []

    def execute(self, sql, params=None):
        self.queries.append(sql)

    def fetchall(self):
        return self.rows

    def __enter__(self):  # supports "with connection.cursor() as cursor:"
        return self

    def __exit__(self, *exc_info):
        return None


class FakeConnection:
    def __init__(self, rows):
        self.fake_cursor = FakeCursor(rows)

    def cursor(self):
        return self.fake_cursor


def make_connector(rows):
    connector = MySQLConnector(ConnectionConfig(name="shop", type="mysql", database="shop"))
    connector.connection = FakeConnection(rows)  # instead of connect(): no real database
    return connector


def test_mysql_is_registered():
    assert CONNECTOR_TYPES["mysql"] is MySQLConnector


def test_large_result_is_counted_but_not_downloaded():
    connector = make_connector(rows=[(60_000,)])  # the answer to the COUNT(*) query

    result = connector.execute_query("SELECT * FROM orders", max_rows=50_000)

    assert result.too_large
    assert result.df is None
    assert result.total_rows == 60_000
```

Before you open a pull request, check that `ruff check .`, `ruff format --check .` and `pytest -q`
all pass.
