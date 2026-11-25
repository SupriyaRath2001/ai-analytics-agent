"""BigQuery: catalog metadata from the API (free), and checked, capped query jobs."""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator, Sequence

from google.api_core.exceptions import GoogleAPIError
from google.cloud import bigquery
from google.oauth2 import service_account

from backend.config import ConnectionConfig
from backend.connectors.base import (
    BaseDBConnector,
    ColumnMeta,
    QueryError,
    QueryResult,
    TableMeta,
)
from backend.connectors.sql_guard import check_read_only

log = logging.getLogger(__name__)

_GIB = 1024**3
_PROFILE_MAX_BYTES_BILLED = _GIB

_SQL_HINTS = """\
- Write GoogleSQL. Name every table in full, in backticks: `project.dataset.table`.
- REPEATED fields are arrays: read them with UNNEST, e.g. `FROM t, UNNEST(t.items) AS item`.
- RECORD fields are structs: use dot paths, e.g. `device.category`.
- When a table has a partition column, filter on it to scan less data.
- Divide with SAFE_DIVIDE(a, b), so a zero divisor gives NULL instead of an error.
- Don't use SELECT * on wide tables: select only the columns you need.
- Add ORDER BY for top-N questions and time series.
- TIMESTAMP values are in UTC. To group or filter by local day, use DATE(ts, '<timezone>')."""


class BigQueryConnector(BaseDBConnector):
    """One GCP project, optionally narrowed to some datasets and tables."""

    type_name = "bigquery"

    def __init__(self, config: ConnectionConfig, client: bigquery.Client | None = None):
        super().__init__(config)
        # Created by connect(). Tests pass a fake client here instead.
        self.client = client

    @property
    def dialect(self) -> str:
        return "bigquery"

    @property
    def project(self) -> str:
        """The configured project, or the one the client found in the credentials."""
        return self.client.project

    def connect(self) -> None:
        # Without a key file the client uses Application Default Credentials,
        # e.g. the service account of the VM it runs on.
        credentials = None
        if self.config.credentials_path:
            credentials = service_account.Credentials.from_service_account_file(
                self.config.credentials_path
            )
        self.client = bigquery.Client(
            project=self.config.project,
            credentials=credentials,
            location=self.config.location,
        )
        log.info("connected to BigQuery project %s", self.client.project)

    def close(self) -> None:
        if self.client is not None:
            self.client.close()

    def fetch_raw_metadata(self) -> list[TableMeta]:
        tables = []
        for dataset in self.client.list_datasets(project=self.project):
            if not self._dataset_allowed(dataset.dataset_id):
                continue
            for item in self.client.list_tables(dataset.reference):
                if self._table_excluded(item.dataset_id, item.table_id):
                    continue
                try:
                    table = self.client.get_table(item.reference)
                except GoogleAPIError as exc:
                    log.warning(
                        "skipping table %s.%s: %s",
                        item.dataset_id,
                        item.table_id,
                        _api_message(exc),
                    )
                    continue
                tables.append(self._table_meta(table))
        log.info("read metadata for %d tables in %s", len(tables), self.project)
        return tables

    def sample_column_values(
        self, table: TableMeta, columns: list[str], limit: int = 15
    ) -> dict[str, list[str]]:
        wanted = set(columns)
        names = [
            column.name
            for column in table.columns
            if column.name in wanted
            and column.data_type == "STRING"
            and column.mode != "REPEATED"
            and "." not in column.name  # nested fields are left out
        ]
        if not names:
            return {}
        # Asking for limit + 1 values shows whether a column has more than `limit`.
        top_counts = ", ".join(
            f"APPROX_TOP_COUNT(`{name}`, {limit + 1}) AS c{i}" for i, name in enumerate(names)
        )
        sql = f"SELECT {top_counts} FROM `{table.qualified_name}`"
        job_config = bigquery.QueryJobConfig(maximum_bytes_billed=_PROFILE_MAX_BYTES_BILLED)
        try:
            job = self.client.query(sql, job_config=job_config)
            row = next(iter(job.result(timeout=self.config.query_timeout_sec)))
            return _low_cardinality_values(row, names, limit)
        except Exception as exc:  # sample values are optional; the table works without them
            log.warning("could not profile %s: %s", table.qualified_name, _api_message(exc))
            return {}

    def dry_run_or_validate(self, query: str) -> tuple[bool, str]:
        ok, reason = check_read_only(query, self.dialect)
        if not ok:
            return False, reason

        job_config = bigquery.QueryJobConfig(dry_run=True, use_query_cache=False)
        try:
            job = self.client.query(query, job_config=job_config)
        except GoogleAPIError as exc:
            return False, f"BigQuery rejected the query: {_api_message(exc)}"

        if job.statement_type != "SELECT":
            return False, (
                f"BigQuery reads this as a {job.statement_type} statement, but only SELECT "
                "queries can run. Rewrite it as a single SELECT."
            )
        problem = self._table_access_problem(job.referenced_tables)
        if problem:
            return False, problem

        scanned = job.total_bytes_processed or 0
        limit = self._max_bytes_billed()
        if limit is not None and scanned > limit:
            return False, (
                f"The query would scan {_human_bytes(scanned)}, more than the "
                f"{self.config.max_bytes_billed_gb:g} GB limit. Filter on the partition "
                "column, narrow the date range or select fewer columns."
            )
        return True, f"Dry run OK, will scan {_human_bytes(scanned)}."

    def execute_query(self, query: str, max_rows: int | None = None) -> QueryResult:
        if max_rows is None:
            max_rows = self.config.max_rows_fetch
        job_config = bigquery.QueryJobConfig()
        limit = self._max_bytes_billed()
        if limit is not None:
            job_config.maximum_bytes_billed = limit

        started = time.monotonic()
        try:
            job = self.client.query(query, job_config=job_config)
        except GoogleAPIError as exc:
            raise QueryError(_api_message(exc)) from exc
        rows = self._wait_for(job)
        total_rows = rows.total_rows or 0
        log.info(
            "query job %s finished: %d rows, %s processed",
            job.job_id,
            total_rows,
            _human_bytes(job.total_bytes_processed or 0),
        )

        if total_rows > max_rows:
            # Counted, not downloaded: the user is asked to narrow the question instead.
            return QueryResult(
                sql=query,
                total_rows=total_rows,
                too_large=True,
                bytes_processed=job.total_bytes_processed,
                duration_sec=_seconds_since(started),
            )
        try:
            df = rows.to_dataframe(create_bqstorage_client=False)
        except GoogleAPIError as exc:
            raise QueryError(_api_message(exc)) from exc
        return QueryResult(
            sql=query,
            df=df,
            total_rows=total_rows,
            bytes_processed=job.total_bytes_processed,
            duration_sec=_seconds_since(started),
        )

    def sql_hints(self) -> str:
        return _SQL_HINTS

    def _wait_for(self, job: bigquery.QueryJob) -> bigquery.table.RowIterator:
        timeout = self.config.query_timeout_sec
        try:
            return job.result(timeout=timeout)
        except TimeoutError as exc:
            _cancel(job)
            raise QueryError(
                f"The query ran longer than {timeout} seconds and was cancelled. Make it "
                "cheaper: filter on the partition column, narrow the date range or aggregate more."
            ) from exc
        except GoogleAPIError as exc:
            raise QueryError(_api_message(exc)) from exc

    def _table_meta(self, table: bigquery.Table) -> TableMeta:
        return TableMeta(
            connection=self.name,
            schema_name=table.dataset_id,
            table_name=table.table_id,
            qualified_name=f"{table.project}.{table.dataset_id}.{table.table_id}",
            table_type=table.table_type or "TABLE",
            description=table.description or "",
            columns=_columns(table),
            row_count=table.num_rows,
            size_bytes=table.num_bytes,
        )

    def _table_access_problem(self, tables: list[bigquery.TableReference]) -> str:
        """Why the query may not read one of these tables, or "" when it may read them all."""
        for ref in tables:
            name = f"{ref.project}.{ref.dataset_id}.{ref.table_id}"
            if ref.project != self.project:
                return (
                    f"`{name}` is outside the project {self.project}. "
                    f"Query only tables in `{self.project}`."
                )
            if not self._dataset_allowed(ref.dataset_id):
                return (
                    f"The dataset {ref.dataset_id} is not available. "
                    "Use only the tables you were given."
                )
            if self._table_excluded(ref.dataset_id, ref.table_id):
                return f"The table `{name}` is excluded. Use only the tables you were given."
        return ""

    def _dataset_allowed(self, dataset_id: str) -> bool:
        include = self.config.include_datasets
        if include and dataset_id not in include:
            return False
        return dataset_id not in self.config.exclude_datasets

    def _table_excluded(self, dataset_id: str, table_id: str) -> bool:
        return f"{dataset_id}.{table_id}" in self.config.exclude_tables

    def _max_bytes_billed(self) -> int | None:
        if self.config.max_bytes_billed_gb is None:
            return None
        return int(self.config.max_bytes_billed_gb * _GIB)


def _human_bytes(num_bytes: int) -> str:
    """1288490189 -> "1.2 GB". Units are 1024-based, like the BigQuery console."""
    if num_bytes < 1024:
        return f"{num_bytes} bytes"
    units = ["KB", "MB", "GB", "TB", "PB"]
    size = num_bytes / 1024
    index = 0
    while size >= 1024 and index < len(units) - 1:
        size /= 1024
        index += 1
    return f"{size:.1f} {units[index]}"


def _columns(table: bigquery.Table) -> list[ColumnMeta]:
    """Flatten the schema: a RECORD is listed itself, then each child as ``parent.child``."""
    partition_field = None
    if table.time_partitioning is not None:
        partition_field = table.time_partitioning.field  # None for ingestion-time partitioning
    elif table.range_partitioning is not None:
        partition_field = table.range_partitioning.field
    clustering = table.clustering_fields or []

    columns = []
    for path, field in _walk_schema(table.schema):
        columns.append(
            ColumnMeta(
                name=path,
                data_type=field.field_type,
                mode=field.mode,
                description=field.description or "",
                is_partition=path == partition_field,
                clustering_order=clustering.index(path) + 1 if path in clustering else None,
            )
        )
    if table.time_partitioning is not None and partition_field is None:
        columns.append(
            ColumnMeta(
                name="_PARTITIONTIME",
                data_type="TIMESTAMP",
                mode="NULLABLE",
                description="Pseudo column: ingestion time, truncated to the partition. "
                "Filter on it to scan less data.",
                is_partition=True,
            )
        )
    return columns


def _walk_schema(
    fields: Sequence[bigquery.SchemaField], prefix: str = ""
) -> Iterator[tuple[str, bigquery.SchemaField]]:
    for field in fields:
        path = f"{prefix}{field.name}"
        yield path, field
        yield from _walk_schema(field.fields, prefix=f"{path}.")


def _low_cardinality_values(
    row: bigquery.Row, names: list[str], limit: int
) -> dict[str, list[str]]:
    """Read one APPROX_TOP_COUNT row. A column is kept when it shows at most ``limit`` values.

    NULL counts towards the limit (it is one of the values returned) but is left
    out of the samples.
    """
    samples = {}
    for i, name in enumerate(names):
        top = row[f"c{i}"] or []  # NULL when the table is empty
        if len(top) > limit:
            continue
        values = [str(item["value"]) for item in top if item["value"] is not None]
        if values:
            samples[name] = values
    return samples


def _api_message(exc: Exception) -> str:
    """The API's own error text, without the HTTP method, URL and job ID around it."""
    errors = getattr(exc, "errors", None)
    if errors and isinstance(errors[0], dict) and errors[0].get("message"):
        return errors[0]["message"]
    return getattr(exc, "message", None) or str(exc)


def _cancel(job: bigquery.QueryJob) -> None:
    try:
        job.cancel()
    except GoogleAPIError as exc:
        log.warning("could not cancel BigQuery job %s: %s", job.job_id, _api_message(exc))


def _seconds_since(started: float) -> float:
    return round(time.monotonic() - started, 2)
