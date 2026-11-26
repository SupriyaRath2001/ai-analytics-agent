import logging
from types import SimpleNamespace

import pandas as pd
import pytest
import sqlglot
from google.api_core.exceptions import BadRequest, Forbidden
from google.cloud import bigquery
from google.cloud.bigquery.dataset import DatasetListItem
from google.cloud.bigquery.table import TableListItem
from sqlglot import exp

from backend.config import ConnectionConfig
from backend.connectors import bigquery_connector
from backend.connectors.base import ColumnMeta, QueryError, TableMeta
from backend.connectors.bigquery_connector import BigQueryConnector, _human_bytes
from backend.connectors.sql_guard import check_read_only

PROJECT = "my-project"
GIB = 1024**3


class FakeRows:
    """Stands in for RowIterator: iterable, with total_rows and to_dataframe()."""

    def __init__(self, rows, total_rows=None):
        self.rows = list(rows)
        self.total_rows = len(self.rows) if total_rows is None else total_rows
        self.downloaded = False

    def __iter__(self):
        return iter(self.rows)

    def to_dataframe(self, create_bqstorage_client=True):
        assert create_bqstorage_client is False  # the Storage API needs extra permissions
        self.downloaded = True
        return pd.DataFrame(self.rows)


class FakeJob:
    def __init__(self, rows=(), total_rows=None, error=None, bytes_processed=2048):
        self.job_id = "job-1"
        self.rows = FakeRows(rows, total_rows)
        self.error = error
        self.total_bytes_processed = bytes_processed
        self.timeout = None
        self.cancelled = False

    def result(self, timeout=None):
        self.timeout = timeout
        if self.error is not None:
            raise self.error
        return self.rows

    def cancel(self):
        self.cancelled = True
        return True


class FakeClient:
    """The parts of bigquery.Client the connector uses. It records every query."""

    def __init__(self, jobs=(), tables=(), broken=()):
        self.project = PROJECT
        self.jobs = list(jobs)  # handed out by query() in order; exceptions are raised
        self.tables = {_name(resource): resource for resource in tables}
        self.broken = set(broken)  # "dataset.table" names whose get_table() fails
        self.queries = []
        self.loaded = []

    def query(self, sql, job_config=None):
        self.queries.append((sql, job_config))
        job = self.jobs.pop(0)
        if isinstance(job, Exception):
            raise job
        return job

    def list_datasets(self, project=None):
        assert project == PROJECT
        dataset_ids = sorted({name.split(".")[0] for name in self.tables})
        return [
            DatasetListItem({"datasetReference": {"projectId": PROJECT, "datasetId": dataset_id}})
            for dataset_id in dataset_ids
        ]

    def list_tables(self, dataset):
        return [
            TableListItem(resource)
            for name, resource in self.tables.items()
            if name.split(".")[0] == dataset.dataset_id
        ]

    def get_table(self, ref):
        name = f"{ref.dataset_id}.{ref.table_id}"
        self.loaded.append(name)
        if name in self.broken:
            raise Forbidden(f"Access Denied: Table {PROJECT}:{name}")
        return bigquery.Table.from_api_repr(self.tables[name])


def _name(resource):
    ref = resource["tableReference"]
    return f"{ref['datasetId']}.{ref['tableId']}"


def table_resource(name, fields, table_type="TABLE", **extra):
    """A table as the API returns it from tables.get."""
    dataset_id, table_id = name.split(".")
    return {
        "tableReference": {"projectId": PROJECT, "datasetId": dataset_id, "tableId": table_id},
        "type": table_type,
        "schema": {"fields": fields},
        **extra,
    }


def dry_run_job(statement_type="SELECT", tables=(), bytes_processed=0):
    """A real QueryJob, built from what the API returns for a dry run."""
    keys = ["projectId", "datasetId", "tableId"]
    referenced = [dict(zip(keys, name.split("."), strict=True)) for name in tables]
    resource = {
        "jobReference": {"projectId": PROJECT, "jobId": "dry-run"},
        "configuration": {"query": {"query": "SELECT 1"}, "dryRun": True},
        "statistics": {
            "query": {
                "statementType": statement_type,
                "totalBytesProcessed": str(bytes_processed),
                "referencedTables": referenced,
            }
        },
    }
    return bigquery.QueryJob.from_api_repr(resource, SimpleNamespace(project=PROJECT))


def make_connector(client, **settings):
    config = ConnectionConfig(name="warehouse", type="bigquery", project=PROJECT, **settings)
    return BigQueryConnector(config, client=client)


ORDERS = table_resource(
    "sales.orders",
    [
        {"name": "order_id", "type": "INTEGER", "mode": "REQUIRED", "description": "Order number"},
        {"name": "created_at", "type": "TIMESTAMP"},
        {"name": "country", "type": "STRING"},
        {"name": "status", "type": "STRING"},
        {
            "name": "device",
            "type": "RECORD",
            "fields": [
                {"name": "category", "type": "STRING"},
                {
                    "name": "browser",
                    "type": "RECORD",
                    "fields": [{"name": "name", "type": "STRING"}],
                },
            ],
        },
        {
            "name": "items",
            "type": "RECORD",
            "mode": "REPEATED",
            "fields": [{"name": "sku", "type": "STRING"}, {"name": "qty", "type": "INTEGER"}],
        },
    ],
    description="One row per order",
    numRows="1200",
    numBytes="34567",
    timePartitioning={"type": "DAY", "field": "created_at"},
    clustering={"fields": ["country", "status"]},
)


def test_metadata_flattens_records_and_marks_partition_and_clustering():
    [table] = make_connector(FakeClient(tables=[ORDERS])).fetch_raw_metadata()

    assert table.qualified_name == "my-project.sales.orders"
    assert (table.connection, table.schema_name, table.table_name) == (
        "warehouse",
        "sales",
        "orders",
    )
    assert (table.table_type, table.description) == ("TABLE", "One row per order")
    assert (table.row_count, table.size_bytes) == (1200, 34567)
    columns = {column.name: column for column in table.columns}
    assert list(columns) == [
        "order_id",
        "created_at",
        "country",
        "status",
        "device",
        "device.category",
        "device.browser",
        "device.browser.name",
        "items",
        "items.sku",
        "items.qty",
    ]
    assert columns["order_id"] == ColumnMeta(
        name="order_id", data_type="INTEGER", mode="REQUIRED", description="Order number"
    )
    assert (columns["items"].data_type, columns["items"].mode) == ("RECORD", "REPEATED")
    assert [name for name, column in columns.items() if column.is_partition] == ["created_at"]
    clustering = {name: c.clustering_order for name, c in columns.items() if c.clustering_order}
    assert clustering == {"country": 1, "status": 2}


def test_ingestion_time_partitioning_adds_a_partitiontime_column():
    events = table_resource(
        "analytics.events", [{"name": "name", "type": "STRING"}], timePartitioning={"type": "DAY"}
    )

    [table] = make_connector(FakeClient(tables=[events])).fetch_raw_metadata()

    assert [(c.name, c.data_type, c.is_partition) for c in table.columns] == [
        ("name", "STRING", False),
        ("_PARTITIONTIME", "TIMESTAMP", True),
    ]


def test_range_partition_column_is_marked():
    customers = table_resource(
        "sales.customers",
        [{"name": "customer_id", "type": "INTEGER"}, {"name": "name", "type": "STRING"}],
        rangePartitioning={
            "field": "customer_id",
            "range": {"start": "0", "end": "1000", "interval": "10"},
        },
    )

    [table] = make_connector(FakeClient(tables=[customers])).fetch_raw_metadata()

    assert [c.name for c in table.columns if c.is_partition] == ["customer_id"]


def test_dataset_and_table_filters_are_applied():
    one_column = [{"name": "id", "type": "INTEGER"}]
    client = FakeClient(
        tables=[
            ORDERS,
            table_resource("sales.tmp_orders", one_column),
            table_resource("sales.daily_revenue", one_column, table_type="VIEW"),
            table_resource("marketing.campaigns", one_column),
            table_resource("staging.raw_orders", one_column),
        ]
    )
    connector = make_connector(
        client,
        include_datasets=["sales", "marketing"],
        exclude_datasets=["marketing"],
        exclude_tables=["sales.tmp_orders"],
    )

    tables = connector.fetch_raw_metadata()

    assert sorted(t.qualified_name for t in tables) == [
        "my-project.sales.daily_revenue",
        "my-project.sales.orders",
    ]
    assert "sales.tmp_orders" not in client.loaded  # excluded tables are never loaded
    view = next(t for t in tables if t.table_name == "daily_revenue")
    assert (view.table_type, view.row_count) == ("VIEW", None)


def test_table_that_fails_to_load_is_logged_and_skipped(caplog):
    secret = table_resource("sales.secret", [{"name": "id", "type": "INTEGER"}])
    client = FakeClient(tables=[ORDERS, secret], broken={"sales.secret"})

    with caplog.at_level(logging.WARNING):
        tables = make_connector(client).fetch_raw_metadata()

    assert [t.table_name for t in tables] == ["orders"]
    assert "sales.secret" in caplog.text


ORDERS_META = TableMeta(
    connection="warehouse",
    schema_name="sales",
    table_name="orders",
    qualified_name="my-project.sales.orders",
    columns=[
        ColumnMeta(name="order_id", data_type="INTEGER"),
        ColumnMeta(name="status", data_type="STRING", mode="NULLABLE"),
        ColumnMeta(name="country", data_type="STRING", mode="REQUIRED"),
        ColumnMeta(name="coupon", data_type="STRING", mode="NULLABLE"),
        ColumnMeta(name="email", data_type="STRING", mode="NULLABLE"),
        ColumnMeta(name="tags", data_type="STRING", mode="REPEATED"),
        ColumnMeta(name="device", data_type="RECORD", mode="NULLABLE"),
        ColumnMeta(name="device.category", data_type="STRING", mode="NULLABLE"),
    ],
)


def test_profiling_runs_one_capped_query_over_top_level_string_columns():
    client = FakeClient(jobs=[FakeJob(rows=[{"c0": [], "c1": []}])])
    asked_for = ["order_id", "status", "country", "tags", "device", "device.category"]

    make_connector(client).sample_column_values(ORDERS_META, asked_for, limit=15)

    [(sql, job_config)] = client.queries
    assert sql == (
        "SELECT APPROX_TOP_COUNT(`status`, 16) AS c0, APPROX_TOP_COUNT(`country`, 16) AS c1 "
        "FROM `my-project.sales.orders`"
    )
    assert job_config.maximum_bytes_billed == GIB


@pytest.mark.parametrize(
    ("size_bytes", "percent"),
    [
        (GIB + 1, 99),
        (2 * GIB, 50),
        (3 * GIB, 33),
        (150 * GIB, 1),  # 0.67 % would fit; 1 is the smallest sample
    ],
)
def test_tables_over_1_gib_are_sampled_to_fit_the_cap(size_bytes, percent):
    table = ORDERS_META.model_copy(update={"size_bytes": size_bytes})
    client = FakeClient(jobs=[FakeJob(rows=[{"c0": []}])])

    make_connector(client).sample_column_values(table, ["status"])

    [(sql, job_config)] = client.queries
    assert sql == (
        "SELECT APPROX_TOP_COUNT(`status`, 16) AS c0 "
        f"FROM `my-project.sales.orders` TABLESAMPLE SYSTEM ({percent} PERCENT)"
    )
    assert job_config.maximum_bytes_billed == GIB
    # Valid GoogleSQL: the sample belongs to the table it follows, and nothing is lost on a
    # round trip through the parser.
    tree = sqlglot.parse_one(sql, read="bigquery")
    [sampled] = [table for table in tree.find_all(exp.Table) if table.args.get("sample")]
    assert sampled.name == "orders"
    assert tree.sql("bigquery") == sql
    assert check_read_only(sql, "bigquery") == (True, "")


@pytest.mark.parametrize(
    "update",
    [
        {"size_bytes": GIB},
        {"size_bytes": None},
        {"size_bytes": 5 * GIB, "table_type": "VIEW"},
    ],
    ids=["exactly 1 GiB", "size unknown", "view"],
)
def test_small_tables_and_views_are_profiled_in_full(update):
    client = FakeClient(jobs=[FakeJob(rows=[{"c0": []}])])

    make_connector(client).sample_column_values(ORDERS_META.model_copy(update=update), ["status"])

    [(sql, _)] = client.queries
    assert "TABLESAMPLE" not in sql


def test_profiling_keeps_only_columns_with_few_values():
    row = {
        "c0": [
            {"value": "paid", "count": 90},
            {"value": None, "count": 5},
            {"value": "refunded", "count": 3},
        ],
        "c1": [{"value": f"country {i}", "count": 1} for i in range(16)],  # too many values
        "c2": None,  # what APPROX_TOP_COUNT returns for an empty table
    }
    client = FakeClient(jobs=[FakeJob(rows=[row])])

    samples = make_connector(client).sample_column_values(
        ORDERS_META, ["status", "country", "coupon"]
    )

    assert samples == {"status": ["paid", "refunded"]}


def test_profiling_errors_are_logged_and_return_nothing(caplog):
    message = "Query exceeded limit for bytes billed: 1073741824. 2147483648 or higher required."
    error = BadRequest(message, errors=[{"message": message, "reason": "bytesBilledLimitExceeded"}])
    client = FakeClient(jobs=[FakeJob(error=error)])

    with caplog.at_level(logging.WARNING):
        samples = make_connector(client).sample_column_values(ORDERS_META, ["status"])

    assert samples == {}
    assert "bytes billed" in caplog.text


def test_profiling_skips_the_query_when_no_column_qualifies():
    client = FakeClient()

    samples = make_connector(client).sample_column_values(ORDERS_META, ["order_id", "tags"])

    assert samples == {}
    assert client.queries == []


def test_dry_run_reports_the_scan_size():
    job = dry_run_job(tables=["my-project.sales.orders"], bytes_processed=1288490189)
    client = FakeClient(jobs=[job])

    result = make_connector(client).dry_run_or_validate(
        "SELECT status FROM `my-project.sales.orders`"
    )

    assert result == (True, "Dry run OK, will scan 1.2 GB.")
    [(_, job_config)] = client.queries
    assert (job_config.dry_run, job_config.use_query_cache) == (True, False)


def test_dry_run_rejects_writes_without_calling_bigquery():
    client = FakeClient()

    ok, message = make_connector(client).dry_run_or_validate("DROP TABLE `my-project.sales.orders`")

    assert not ok
    assert "DROP is not allowed" in message
    assert client.queries == []


def test_dry_run_returns_only_the_bigquery_error_text():
    error = BadRequest(
        "POST https://bigquery.googleapis.com/bigquery/v2/projects/my-project/jobs: "
        "Unrecognized name: amout at [1:8]",
        errors=[{"message": "Unrecognized name: amout at [1:8]", "reason": "invalidQuery"}],
    )
    connector = make_connector(FakeClient(jobs=[error]))

    result = connector.dry_run_or_validate("SELECT amout FROM `my-project.sales.orders`")

    assert result == (False, "BigQuery rejected the query: Unrecognized name: amout at [1:8]")


def test_dry_run_requires_a_select_statement():
    client = FakeClient(jobs=[dry_run_job(statement_type="SCRIPT")])

    ok, message = make_connector(client).dry_run_or_validate("SELECT 1")

    assert not ok
    assert "SCRIPT" in message


@pytest.mark.parametrize(
    ("table", "reason"),
    [
        ("bigquery-public-data.samples.natality", "outside the project my-project"),
        ("my-project.staging.raw_orders", "dataset staging is not available"),
        ("my-project.hr.salaries", "dataset hr is not available"),
        ("my-project.sales.tmp_orders", "`my-project.sales.tmp_orders` is excluded"),
    ],
)
def test_dry_run_rejects_tables_outside_the_allowed_set(table, reason):
    client = FakeClient(jobs=[dry_run_job(tables=["my-project.sales.orders", table])])
    connector = make_connector(
        client,
        include_datasets=["sales", "hr"],
        exclude_datasets=["hr"],
        exclude_tables=["sales.tmp_orders"],
    )

    ok, message = connector.dry_run_or_validate("SELECT 1")

    assert not ok
    assert reason in message
    assert "Use only" in message or "Query only" in message  # tells the SQL writer what to do


def test_dry_run_enforces_the_byte_limit():
    client = FakeClient(jobs=[dry_run_job(bytes_processed=3 * GIB)])

    ok, message = make_connector(client, max_bytes_billed_gb=2).dry_run_or_validate("SELECT 1")

    assert not ok
    assert "3.0 GB, more than the 2 GB limit" in message


def test_execute_downloads_rows_and_records_stats():
    job = FakeJob(rows=[{"status": "paid", "orders": 90}], bytes_processed=4096)
    client = FakeClient(jobs=[job])
    connector = make_connector(client, max_bytes_billed_gb=50, query_timeout_sec=30)

    result = connector.execute_query("SELECT status, COUNT(*) AS orders FROM t GROUP BY status")

    assert result.df.to_dict("records") == [{"status": "paid", "orders": 90}]
    assert (result.total_rows, result.too_large, result.bytes_processed) == (1, False, 4096)
    assert result.duration_sec >= 0
    [(_, job_config)] = client.queries
    assert job_config.maximum_bytes_billed == 50 * GIB
    assert job.timeout == 30


def test_execute_without_a_byte_limit_leaves_the_job_uncapped():
    client = FakeClient(jobs=[FakeJob()])

    make_connector(client).execute_query("SELECT 1")

    [(_, job_config)] = client.queries
    assert job_config.maximum_bytes_billed is None


def test_too_many_rows_are_counted_but_not_downloaded():
    job = FakeJob(rows=[{"id": 1}], total_rows=50_001)

    result = make_connector(FakeClient(jobs=[job])).execute_query("SELECT id FROM t")

    assert (result.too_large, result.total_rows, result.df) == (True, 50_001, None)
    assert not job.rows.downloaded


def test_max_rows_argument_overrides_the_config():
    job = FakeJob(rows=[{"id": 1}, {"id": 2}, {"id": 3}])

    result = make_connector(FakeClient(jobs=[job])).execute_query("SELECT id FROM t", max_rows=2)

    assert result.too_large


def test_timeout_cancels_the_job():
    job = FakeJob(error=TimeoutError())
    connector = make_connector(FakeClient(jobs=[job]), query_timeout_sec=5)

    with pytest.raises(QueryError, match="longer than 5 seconds"):
        connector.execute_query("SELECT 1")
    assert job.cancelled


def test_api_errors_become_query_errors_with_the_api_message():
    error = BadRequest(
        "Division by zero: 1 / 0\n\nLocation: US\nJob ID: job-1\n",
        errors=[
            {"reason": "invalidQuery", "location": "query", "message": "Division by zero: 1 / 0"}
        ],
    )
    connector = make_connector(FakeClient(jobs=[FakeJob(error=error)]))

    with pytest.raises(QueryError) as caught:
        connector.execute_query("SELECT 1 / 0")

    assert str(caught.value) == "Division by zero: 1 / 0"


class RecordingClient:
    """Replaces bigquery.Client so connect() can be tested without Google."""

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.project = kwargs["project"] or "project-from-credentials"


def test_connect_uses_the_key_file_when_set(monkeypatch):
    monkeypatch.setattr(bigquery_connector.bigquery, "Client", RecordingClient)
    monkeypatch.setattr(
        bigquery_connector.service_account.Credentials,
        "from_service_account_file",
        lambda path: f"credentials from {path}",
    )
    config = ConnectionConfig(
        name="warehouse",
        type="bigquery",
        project=PROJECT,
        location="EU",
        credentials_path="/app/secrets/sa.json",
    )
    connector = BigQueryConnector(config)

    connector.connect()

    assert connector.client.kwargs == {
        "project": PROJECT,
        "credentials": "credentials from /app/secrets/sa.json",
        "location": "EU",
    }


def test_connect_falls_back_to_application_default_credentials(monkeypatch):
    monkeypatch.setattr(bigquery_connector.bigquery, "Client", RecordingClient)
    connector = BigQueryConnector(ConnectionConfig(name="warehouse", type="bigquery"))

    connector.connect()

    assert connector.client.kwargs == {"project": None, "credentials": None, "location": None}
    assert connector.project == "project-from-credentials"


def test_sql_hints_cover_the_googlesql_rules():
    hints = make_connector(FakeClient()).sql_hints()

    for rule in ["`project.dataset.table`", "UNNEST", "dot paths", "partition column"]:
        assert rule in hints
    for rule in ["SAFE_DIVIDE", "SELECT *", "ORDER BY", "UTC", "DATE(ts, "]:
        assert rule in hints


def test_sql_hints_point_to_the_prompts_timezone_and_explain_sampling():
    hints = " ".join(make_connector(FakeClient()).sql_hints().split())

    assert "<timezone>" not in hints  # a placeholder the model might copy as it is
    assert "use DATE(ts, tz) with the timezone given in the prompt" in hints
    assert "LIMIT doesn't reduce the bytes BigQuery scans" in hints
    assert "TABLESAMPLE SYSTEM (1 PERCENT) right after the table name and keep the LIMIT" in hints


@pytest.mark.parametrize(
    ("num_bytes", "text"),
    [
        (0, "0 bytes"),
        (1023, "1023 bytes"),
        (1536, "1.5 KB"),
        (1288490189, "1.2 GB"),
        (5 * 1024**4, "5.0 TB"),
    ],
)
def test_byte_counts_are_human_readable(num_bytes, text):
    assert _human_bytes(num_bytes) == text
