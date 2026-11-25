from concurrent.futures import ThreadPoolExecutor

import pytest

from backend.bootstrap.kb_cache import KBCache
from backend.connectors.base import ColumnMeta, TableMeta

ORDERS = "my-project.sales.orders"


def make_table(name="orders", dataset="sales", connection="warehouse", columns=None, **extra):
    if columns is None:
        columns = [
            ColumnMeta(
                name="order_id", data_type="STRING", mode="REQUIRED", description="Order id"
            ),
            ColumnMeta(name="status", data_type="STRING", clustering_order=1),
            ColumnMeta(name="created_at", data_type="TIMESTAMP", is_partition=True),
        ]
    return TableMeta(
        connection=connection,
        schema_name=dataset,
        table_name=name,
        qualified_name=f"my-project.{dataset}.{name}",
        columns=columns,
        **extra,
    )


def curate_orders(kb):
    kb.save_curation(
        "warehouse",
        ORDERS,
        summary="One row per customer order.",
        tags=["sales", "orders"],
        column_meanings={"status": "Payment state of the order", "not_a_column": "ignored"},
        sample_values={"status": ["paid", "refunded"]},
    )


@pytest.fixture
def kb(tmp_path):
    cache = KBCache(tmp_path / "kb.duckdb")
    yield cache
    cache.close()


def test_new_table_is_saved_and_needs_curation(kb):
    assert kb.upsert_raw(make_table(row_count=10, size_bytes=2048)) is True

    [table] = kb.list_tables()
    assert table.qualified_name == ORDERS
    assert (table.schema_name, table.table_name, table.table_type) == ("sales", "orders", "TABLE")
    assert (table.row_count, table.size_bytes, table.column_count) == (10, 2048, 3)
    assert table.curated is False
    assert table.summary == ""
    assert kb.stats() == {"tables": 1, "curated": 0}


def test_curation_is_saved_per_column(kb):
    kb.upsert_raw(make_table())

    curate_orders(kb)

    table = kb.get_table(ORDERS)
    assert table.curated is True
    assert table.summary == "One row per customer order."
    assert table.tags == ["sales", "orders"]
    assert [c.name for c in table.columns] == ["order_id", "status", "created_at"]
    order_id, status, created_at = table.columns
    assert (order_id.mode, order_id.description) == ("REQUIRED", "Order id")
    assert (order_id.meaning, order_id.sample_values) == ("", [])
    assert status.meaning == "Payment state of the order"
    assert status.sample_values == ["paid", "refunded"]
    assert status.clustering_order == 1
    assert created_at.is_partition is True
    assert kb.stats() == {"tables": 1, "curated": 1}


def test_unchanged_table_keeps_its_curation_and_gets_new_counts(kb):
    kb.upsert_raw(make_table(row_count=10, size_bytes=100))
    curate_orders(kb)

    assert kb.upsert_raw(make_table(row_count=25, size_bytes=400)) is False

    table = kb.get_table(ORDERS)
    assert (table.row_count, table.size_bytes) == (25, 400)
    assert table.curated is True
    assert table.summary == "One row per customer order."
    assert table.columns[1].sample_values == ["paid", "refunded"]


def test_uncurated_table_still_needs_curation(kb):
    kb.upsert_raw(make_table())

    assert kb.upsert_raw(make_table()) is True


def test_changed_table_drops_the_old_curation(kb):
    kb.upsert_raw(make_table())
    curate_orders(kb)
    columns = [*make_table().columns, ColumnMeta(name="amount", data_type="NUMERIC")]

    assert kb.upsert_raw(make_table(columns=columns)) is True

    table = kb.get_table(ORDERS)
    assert table.curated is False
    assert (table.summary, table.tags) == ("", [])
    assert [c.name for c in table.columns] == ["order_id", "status", "created_at", "amount"]
    assert all(c.meaning == "" and c.sample_values == [] for c in table.columns)
    assert table.column_count == 4


def test_remove_missing_only_touches_the_given_connection(kb):
    kb.upsert_raw(make_table("orders"))
    kb.upsert_raw(make_table("refunds"))
    kb.upsert_raw(make_table("refunds", connection="other"))

    assert kb.remove_missing("warehouse", keep={ORDERS}) == 1

    assert [(t.connection, t.table_name) for t in kb.list_tables()] == [
        ("other", "refunds"),
        ("warehouse", "orders"),
    ]
    # Its columns went too: adding the table back doesn't double them.
    kb.upsert_raw(make_table("refunds"))
    assert len(kb.get_table("my-project.sales.refunds", connection="warehouse").columns) == 3


def test_find_tables_accepts_the_usual_ways_of_naming_a_table(kb):
    kb.upsert_raw(make_table("orders"))
    kb.upsert_raw(make_table("customers"))
    kb.upsert_raw(make_table("orders", dataset="staging"))

    found = kb.find_tables(
        ["Customers", "SALES.ORDERS", ORDERS, "`my-project.sales.customers`", "nope"]
    )

    assert [t.qualified_name for t in found] == ["my-project.sales.customers", ORDERS]
    assert [c.name for c in found[1].columns] == ["order_id", "status", "created_at"]


def test_a_bare_table_name_matches_every_dataset(kb):
    kb.upsert_raw(make_table("orders"))
    kb.upsert_raw(make_table("orders", dataset="staging"))

    found = kb.find_tables(["orders", "staging.orders"])

    assert [t.qualified_name for t in found] == [ORDERS, "my-project.staging.orders"]


def test_connection_narrows_the_lookups(kb):
    kb.upsert_raw(make_table())
    kb.upsert_raw(make_table(connection="other"))

    assert [t.connection for t in kb.list_tables("other")] == ["other"]
    assert kb.get_table(ORDERS, connection="other").connection == "other"
    assert [t.connection for t in kb.find_tables(["orders"], connection="warehouse")] == [
        "warehouse"
    ]
    assert kb.get_table(ORDERS, connection="missing") is None
    assert kb.get_table("my-project.sales.unknown") is None


def test_table_without_columns(kb):
    assert kb.upsert_raw(make_table("empty_view", columns=[], table_type="VIEW")) is True

    kb.save_curation("warehouse", "my-project.sales.empty_view", "A view.", [], {}, {})

    table = kb.get_table("my-project.sales.empty_view")
    assert (table.columns, table.column_count, table.curated) == ([], 0, True)


def test_save_curation_for_an_unknown_table_raises(kb):
    with pytest.raises(KeyError, match="not in the KB"):
        kb.save_curation("warehouse", ORDERS, "A summary.", [], {}, {})


def test_business_context_is_replaced(kb):
    assert kb.get_business_context() == ""

    kb.set_business_context("Revenue = paid orders.")
    kb.set_business_context("Revenue = paid orders minus refunds.")

    assert kb.get_business_context() == "Revenue = paid orders minus refunds."


def test_everything_survives_a_restart(tmp_path):
    path = tmp_path / "data" / "nested" / "kb.duckdb"  # the folders don't exist yet
    kb = KBCache(path)
    kb.upsert_raw(make_table())
    curate_orders(kb)
    kb.set_business_context("Weeks start on Monday.")
    kb.close()

    kb = KBCache(path)
    try:
        assert kb.upsert_raw(make_table()) is False
        assert kb.get_table(ORDERS).columns[1].sample_values == ["paid", "refunded"]
        assert kb.get_business_context() == "Weeks start on Monday."
    finally:
        kb.close()


def test_threads_can_share_one_cache(kb):
    tables = [make_table(f"table_{i}") for i in range(30)]

    def curate(table):
        kb.upsert_raw(table)
        kb.save_curation(table.connection, table.qualified_name, "A table.", [], {}, {})
        kb.find_tables([table.table_name])
        return len(kb.list_tables())

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(curate, tables))

    assert kb.stats() == {"tables": 30, "curated": 30}
