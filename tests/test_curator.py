import logging
import re

import pytest

from backend.bootstrap.curator import (
    MAX_BUSINESS_CONTEXT_CHARS,
    MAX_PROMPT_COLUMNS,
    Curator,
    TableCuration,
)
from backend.bootstrap.kb_cache import KBCache
from backend.config import ConnectionConfig
from backend.connectors.base import BaseDBConnector, ColumnMeta, TableMeta
from backend.llm import LLMError

ORDERS = "my-project.sales.orders"
CUSTOMERS = "my-project.sales.customers"


def make_table(name, dataset="sales", connection="warehouse", columns=None):
    if columns is None:
        columns = [
            ColumnMeta(name="order_id", data_type="STRING", description="Order id"),
            ColumnMeta(name="status", data_type="STRING"),
            ColumnMeta(name="created_at", data_type="TIMESTAMP", is_partition=True),
        ]
    return TableMeta(
        connection=connection,
        schema_name=dataset,
        table_name=name,
        qualified_name=f"my-project.{dataset}.{name}",
        description=f"Customer {name}.",
        columns=columns,
    )


class FakeConnector(BaseDBConnector):
    """Serves a fixed table list. Tests change ``tables`` between starts."""

    type_name = "fake"

    def __init__(self, tables, samples=None, name="warehouse"):
        super().__init__(ConnectionConfig(name=name, type="fake"))
        self.tables = tables
        self.samples = samples or {}
        self.sampled = []  # (qualified_name, columns) per profiling call
        self.broken = False

    @property
    def dialect(self):
        return "bigquery"

    def connect(self):
        pass

    def fetch_raw_metadata(self):
        if self.broken:
            raise RuntimeError("403 Access Denied")
        return list(self.tables)

    def sample_column_values(self, table, columns, limit=15):
        self.sampled.append((table.qualified_name, columns))
        return self.samples.get(table.qualified_name, {})

    def dry_run_or_validate(self, query):
        raise NotImplementedError

    def execute_query(self, query, max_rows=None):
        raise NotImplementedError


class FakeLLM:
    """Stands in for LLMClient. Answers from the table name in the prompt, or fails for some."""

    def __init__(self, fail_for=()):
        self.fail_for = set(fail_for)
        self.calls = []

    def complete_json(self, messages, schema, tier="fast"):
        table = re.search(r"^Table: (\S+)", messages[-1]["content"], re.MULTILINE).group(1)
        self.calls.append({"table": table, "messages": messages, "schema": schema, "tier": tier})
        if table in self.fail_for:
            raise LLMError("the model timed out")
        return schema(
            summary=f" One row per record in {table}. ",
            tags=["Sales", " orders ", "sales", ""],
            columns={"status": "Payment state of the order"},
        )

    @property
    def asked(self):
        return sorted(call["table"] for call in self.calls)


class ShutdownLLM(FakeLLM):
    """The app shuts down while the model describes the first table."""

    curator = None

    def complete_json(self, messages, schema, tier="fast"):
        self.curator.stop()
        return super().complete_json(messages, schema, tier)


@pytest.fixture
def kb(tmp_path):
    cache = KBCache(tmp_path / "kb.duckdb")
    yield cache
    cache.close()


@pytest.fixture
def connector():
    return FakeConnector(
        [make_table("orders"), make_table("customers")],
        samples={ORDERS: {"status": ["paid", "refunded"]}},
    )


def start(kb, connector, llm=None, **options):
    """One app start: a new Curator over the same KB file."""
    llm = llm or FakeLLM()
    curator = Curator({connector.name: connector}, kb, llm, **options)
    curator.run()
    return curator, llm


def test_first_start_curates_every_table(kb, connector, caplog):
    llm = FakeLLM()
    curator = Curator({"warehouse": connector}, kb, llm)
    assert curator.status == "idle"

    with caplog.at_level(logging.INFO, logger="backend.bootstrap.curator"):
        curator.run()

    assert curator.status == "done"
    assert llm.asked == [CUSTOMERS, ORDERS]
    assert all(call["schema"] is TableCuration and call["tier"] == "fast" for call in llm.calls)
    assert (ORDERS, ["order_id", "status", "created_at"]) in connector.sampled
    orders = kb.get_table(ORDERS)
    assert orders.curated is True
    assert orders.summary == f"One row per record in {ORDERS}."
    assert orders.tags == ["sales", "orders"]
    status = orders.columns[1]
    assert status.meaning == "Payment state of the order"
    assert status.sample_values == ["paid", "refunded"]
    assert kb.stats() == {"tables": 2, "curated": 2}
    assert "curated 2/2 tables" in caplog.text


def test_prompt_shows_the_table_and_its_columns(kb, connector):
    _, llm = start(kb, connector)

    call = next(call for call in llm.calls if call["table"] == ORDERS)
    system, user = call["messages"]
    assert system["role"] == "system"
    prompt = user["content"]
    assert f"Table: {ORDERS} (TABLE)" in prompt
    assert "Description: Customer orders." in prompt
    assert "- order_id (STRING): Order id" in prompt
    assert "- status (STRING) [values: paid, refunded]" in prompt
    assert "- created_at (TIMESTAMP)" in prompt


def test_very_wide_tables_list_only_the_first_columns(kb):
    count = MAX_PROMPT_COLUMNS + 10
    columns = [ColumnMeta(name=f"col_{i}", data_type="INT64") for i in range(count)]
    connector = FakeConnector([make_table("wide", columns=columns)])

    _, llm = start(kb, connector)

    prompt = llm.calls[0]["messages"][-1]["content"]
    assert f"- col_{MAX_PROMPT_COLUMNS - 1} (INT64)" in prompt
    assert f"- col_{MAX_PROMPT_COLUMNS} (INT64)" not in prompt
    assert "and 10 more columns" in prompt


def test_unchanged_tables_are_skipped_on_the_next_start(kb, connector):
    start(kb, connector)
    connector.sampled.clear()

    curator, llm = start(kb, connector)

    assert curator.status == "done"
    assert llm.calls == []
    assert connector.sampled == []  # no profiling queries either
    assert kb.stats() == {"tables": 2, "curated": 2}


def test_a_changed_table_is_curated_again(kb, connector):
    start(kb, connector)
    columns = [*make_table("orders").columns, ColumnMeta(name="amount", data_type="NUMERIC")]
    connector.tables = [make_table("orders", columns=columns), make_table("customers")]

    _, llm = start(kb, connector)

    assert llm.asked == [ORDERS]
    orders = kb.get_table(ORDERS)
    assert orders.curated is True
    assert [c.name for c in orders.columns] == ["order_id", "status", "created_at", "amount"]


def test_a_dropped_table_is_removed(kb, connector):
    start(kb, connector)
    connector.tables = [make_table("orders")]

    curator, llm = start(kb, connector)

    assert curator.status == "done"
    assert kb.get_table(CUSTOMERS) is None
    assert llm.calls == []
    assert kb.stats() == {"tables": 1, "curated": 1}


def test_llm_failure_leaves_the_table_for_the_next_start(kb, connector, caplog):
    with caplog.at_level(logging.WARNING, logger="backend.bootstrap.curator"):
        curator, _ = start(kb, connector, FakeLLM(fail_for={ORDERS}))

    assert curator.status == "done"
    assert kb.get_table(ORDERS).curated is False
    assert kb.get_table(CUSTOMERS).curated is True
    assert f"could not curate {ORDERS}" in caplog.text

    _, llm = start(kb, connector)

    assert llm.asked == [ORDERS]
    assert kb.get_table(ORDERS).curated is True


def test_a_connection_that_cannot_be_read_keeps_its_cached_tables(kb, connector):
    other = FakeConnector([make_table("events", dataset="web", connection="other")], name="other")
    connectors = {"warehouse": connector, "other": other}
    Curator(connectors, kb, FakeLLM()).run()
    connector.broken = True
    other.tables = []  # the healthy connection really lost its table

    curator = Curator(connectors, kb, FakeLLM())
    curator.run()

    assert curator.status == "done"
    assert [t.qualified_name for t in kb.list_tables("warehouse")] == [CUSTOMERS, ORDERS]
    assert kb.list_tables("other") == []


def test_the_run_fails_when_no_catalog_can_be_read(kb, connector, caplog):
    connector.broken = True

    with caplog.at_level(logging.ERROR, logger="backend.bootstrap.curator"):
        curator, llm = start(kb, connector)

    # "failed", not "done": the chat tells the user to check the logs instead of waiting.
    assert curator.status == "failed"
    assert kb.stats() == {"tables": 0, "curated": 0}
    assert llm.calls == []
    assert "403 Access Denied" in caplog.text
    assert "could not read the tables of any connection" in caplog.text


def test_stop_drops_the_queue_and_saves_nothing_more(kb, caplog):
    connector = FakeConnector([make_table(f"t{i}") for i in range(6)])
    llm = ShutdownLLM()
    curator = Curator({"warehouse": connector}, kb, llm, max_workers=1)
    llm.curator = curator

    with caplog.at_level(logging.INFO, logger="backend.bootstrap.curator"):
        curator.run()

    assert len(llm.calls) == 1  # the call under way finished, the queued tables were skipped
    assert len(connector.sampled) == 1
    assert kb.stats() == {"tables": 6, "curated": 0}  # its result came after stop()
    assert "curation stopped at 0/6 tables" in caplog.text

    _, llm = start(kb, connector)

    assert len(llm.calls) == 6  # the next start curates them all


def test_business_context_is_loaded(kb, connector, tmp_path):
    path = tmp_path / "business_context.md"
    path.write_text("## Metrics\nRevenue = paid orders.\n", encoding="utf-8")

    start(kb, connector, business_context_path=str(path))

    assert kb.get_business_context() == "## Metrics\nRevenue = paid orders."


def test_long_business_context_is_cut_with_a_warning(kb, tmp_path, caplog):
    path = tmp_path / "business_context.md"
    path.write_text("x" * (MAX_BUSINESS_CONTEXT_CHARS + 500), encoding="utf-8")
    curator = Curator({}, kb, FakeLLM(), business_context_path=str(path))

    with caplog.at_level(logging.WARNING, logger="backend.bootstrap.curator"):
        curator.load_business_context()

    assert kb.get_business_context() == "x" * MAX_BUSINESS_CONTEXT_CHARS
    assert f"only the first {MAX_BUSINESS_CONTEXT_CHARS} are used" in caplog.text


@pytest.mark.parametrize("file_name", [None, "missing.md"])
def test_no_business_context_file_stores_empty_text(kb, tmp_path, file_name):
    kb.set_business_context("rules from an earlier start")
    path = str(tmp_path / file_name) if file_name else None

    Curator({}, kb, FakeLLM(), business_context_path=path).load_business_context()

    assert kb.get_business_context() == ""


def test_unexpected_error_marks_the_run_failed_without_raising(kb, connector, monkeypatch):
    def broken_upsert(table):
        raise RuntimeError("disk full")

    monkeypatch.setattr(kb, "upsert_raw", broken_upsert)
    curator = Curator({"warehouse": connector}, kb, FakeLLM())

    curator.run()

    assert curator.status == "failed"
