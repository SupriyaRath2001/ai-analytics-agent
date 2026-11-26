import copy
import logging
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from backend.agent.router import AnalyticsAgent, Route, TablePick
from backend.agent.sql_writer import SqlDraft
from backend.bootstrap.kb_cache import KBCache
from backend.config import AppConfig, ConnectionConfig, LLMConfig
from backend.connectors.base import BaseDBConnector, ColumnMeta, QueryError, QueryResult, TableMeta
from backend.llm import LLMError
from backend.services.charts import ChartSpec
from backend.services.exports import ExportStore
from backend.services.renderer import DOWNLOAD_ICON

ORDERS = "my-project.sales.orders"
SQL = "SELECT status, COUNT(*) AS orders FROM `my-project.sales.orders` GROUP BY status"
STATUS_COUNTS = pd.DataFrame({"status": ["paid", "refunded", "pending"], "orders": [120, 8, 15]})
PROGRESS = [
    "> Finding the right tables…\n",
    "> Writing the SQL…\n",
    "> Checking the query…\n",
    "> Running the query…\n",
]
LLM_FAILED = "The language model didn't respond. Please try again in a moment."


class FakeLLM:
    """Stands in for LLMClient with canned replies.

    ``replies`` maps a schema (Route, TablePick, SqlDraft, ChartSpec) to its replies, used in
    order; the last one repeats. ``complete`` answers with ``text``. A reply that is an exception
    is raised instead.
    """

    def __init__(self, replies=None, text="Most orders are paid."):
        self.replies = {schema.__name__: list(items) for schema, items in (replies or {}).items()}
        self.texts = [text]
        self.calls = []  # (schema name or "text", messages, tier)

    def complete(self, messages, tier="fast"):
        self.calls.append(("text", copy.deepcopy(messages), tier))
        return next_reply(self.texts)

    def complete_json(self, messages, schema, tier="fast"):
        self.calls.append((schema.__name__, copy.deepcopy(messages), tier))
        return next_reply(self.replies[schema.__name__])

    def chats(self, kind):
        """The messages sent in each call of one kind: "Route", "SqlDraft", "text", ..."""
        return [messages for name, messages, _ in self.calls if name == kind]


class FakeConnector(BaseDBConnector):
    """Scripted dry runs (the last one repeats) and query results (used in order)."""

    type_name = "fake"

    def __init__(self, dry_runs=None, results=()):
        super().__init__(ConnectionConfig(name="warehouse", type="fake", max_rows_fetch=1000))
        self.dry_runs = list(dry_runs or [(True, "Dry run OK, will scan 1.0 KB.")])
        self.results = list(results)
        self.checked = []
        self.executed = []

    @property
    def dialect(self):
        return "bigquery"

    def connect(self):
        pass

    def fetch_raw_metadata(self):
        return []

    def sample_column_values(self, table, columns, limit=15):
        return {}

    def dry_run_or_validate(self, query):
        self.checked.append(query)
        return next_reply(self.dry_runs)

    def execute_query(self, query, max_rows=None):
        self.executed.append(query)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def sql_hints(self):
        return "- Name every table in full, in backticks."


def next_reply(queue):
    reply = queue.pop(0) if len(queue) > 1 else queue[0]
    if isinstance(reply, Exception):
        raise reply
    return reply


def make_table(name, columns, dataset="sales"):
    return TableMeta(
        connection="warehouse",
        schema_name=dataset,
        table_name=name,
        qualified_name=f"my-project.{dataset}.{name}",
        columns=columns,
        row_count=1200,
    )


@pytest.fixture
def kb(tmp_path):
    cache = KBCache(tmp_path / "kb.duckdb")
    order_columns = [
        ColumnMeta(name="order_id", data_type="STRING"),
        ColumnMeta(name="status", data_type="STRING"),
        ColumnMeta(name="amount", data_type="NUMERIC", description="Order value"),
        ColumnMeta(name="created_at", data_type="TIMESTAMP", is_partition=True),
    ]
    customer_columns = [
        ColumnMeta(name="customer_id", data_type="STRING"),
        ColumnMeta(name="city", data_type="STRING"),
    ]
    cache.upsert_raw(make_table("orders", order_columns))
    cache.upsert_raw(make_table("customers", customer_columns))
    cache.save_curation(
        "warehouse",
        ORDERS,
        summary="One row per customer order.",
        tags=["sales"],
        column_meanings={"status": "Payment state of the order"},
        sample_values={"status": ["paid", "refunded", "pending"]},
    )
    cache.set_business_context("Revenue means paid orders only.")
    yield cache
    cache.close()


@pytest.fixture
def many_tables_kb(tmp_path):
    cache = KBCache(tmp_path / "many.duckdb")
    for i in range(20):
        columns = [ColumnMeta(name="value", data_type="INT64")]
        cache.upsert_raw(make_table(f"t{i:02}", columns, dataset="web"))
    yield cache
    cache.close()


@pytest.fixture
def store(tmp_path):
    return ExportStore(tmp_path / "exports")


def make_agent(kb, store, llm, connector, curation_status=None):
    config = AppConfig(
        connections=[ConnectionConfig(name="warehouse", type="fake")],
        llm=LLMConfig(fast_model="fake/fast", smart_model="fake/smart"),
        timezone="Asia/Kolkata",
    )
    connectors = {connector.name: connector} if connector else {}
    return AnalyticsAgent(config, connectors, kb, llm, store, curation_status=curation_status)


def user(text):
    return {"role": "user", "content": text}


def ask(agent, question, history=()):
    """The agent's chunks joined, as the chat shows them."""
    return "".join(agent.run([*history, user(question)]))


def data_llm(*drafts, route=None, charts=None, text="Most orders are paid."):
    replies = {Route: [route or Route(intent="data")], SqlDraft: list(drafts)}
    if charts:
        replies[ChartSpec] = charts
    return FakeLLM(replies, text=text)


def result(df):
    return QueryResult(sql=SQL, df=df, total_rows=len(df))


def assert_in_order(text, *parts):
    """Each part is in the text, after the part before it."""
    position = 0
    for part in parts:
        found = text.find(part, position)
        assert found != -1, f"{part!r} is missing or out of order"
        position = found + len(part)


def test_openwebui_task_goes_straight_to_the_fast_model(kb, store):
    llm = FakeLLM(text='{"title": "Orders by status"}')
    connector = FakeConnector()
    task = (
        "### Task:\nGenerate a concise title for this chat.\n"
        "<chat_history>\nUSER: Orders by status?\n</chat_history>"
    )

    chunks = list(make_agent(kb, store, llm, connector).run([user(task)]))

    assert chunks == ['{"title": "Orders by status"}']
    assert llm.calls == [("text", [user(task)], "fast")]
    assert connector.checked == connector.executed == []


def test_metadata_answer_comes_from_the_kb_without_bigquery(kb, store):
    llm = FakeLLM({Route: [Route(intent="metadata")]}, text="`sales.orders` has one row per order.")
    connector = FakeConnector()

    text = ask(make_agent(kb, store, llm, connector), "What columns are in orders?")

    assert text == "`sales.orders` has one row per order."
    assert connector.checked == connector.executed == []
    [(system, prompt)] = llm.chats("text")
    assert "say so plainly" in system["content"]
    assert_in_order(
        prompt["content"],
        "Business context:\nRevenue means paid orders only.",
        "`my-project.sales.orders` (table), about 1,200 rows (when the agent started)\n"
        "Summary: One row per customer order.",
        '- status (STRING): Payment state of the order; values: ["paid", "refunded", "pending"]',
        "- amount (NUMERIC): Order value",
        "- created_at (TIMESTAMP, partition column)",
        "Question:\nWhat columns are in orders?",
    )
    assert "All tables:" not in prompt["content"]  # two tables: the details cover them all


def test_inline_result_has_takeaway_table_csv_link_and_sql(kb, store):
    llm = data_llm(SqlDraft(sql=SQL, assumptions=["all time"]))
    connector = FakeConnector(results=[result(STATUS_COUNTS)])

    chunks = list(make_agent(kb, store, llm, connector).run([user("Orders by status?")]))

    assert chunks[:4] == PROGRESS
    answer = "".join(chunks[4:])
    assert answer.startswith("\nMost orders are paid.\n\n| status | orders |\n|---|---|\n")
    csv_file = re.search(r"/downloads/([0-9a-f]{32}\.csv)", answer).group(1)
    assert store.path_for(csv_file) is not None
    assert_in_order(
        answer,
        "| paid | 120 |",
        "| pending | 15 |",
        f"[{DOWNLOAD_ICON} Download CSV (3 rows)](/downloads/{csv_file})",
        "_Assumed: all time._",
        f"<details><summary>Show SQL</summary>\n\n```sql\n{SQL}\n```\n\n</details>",
    )
    assert answer.endswith("</details>\n\n_Want a chart of this? Just ask._")
    assert connector.executed == [SQL]
    assert {kind: tier for kind, _, tier in llm.calls} == {
        "Route": "fast",
        "SqlDraft": "smart",
        "text": "fast",
    }
    [takeaway] = llm.chats("text")
    raw_rows = "Result (CSV):\nstatus,orders\npaid,120\nrefunded,8\npending,15"
    assert raw_rows in takeaway[-1]["content"]


def test_takeaway_sees_raw_values_and_the_times_the_table_shows(kb, store):
    df = pd.DataFrame(
        {
            "signed_up": pd.to_datetime(["2026-03-01 04:30:00", "2026-03-02 18:45:00"]),
            "conversion": [0.004123, 0.0049],
            "revenue": [1234.5678, 98765.4321],
        }
    )
    df["signed_up"] = df["signed_up"].dt.tz_localize("UTC")  # BigQuery TIMESTAMPs are UTC
    llm = data_llm(SqlDraft(sql=SQL))
    connector = FakeConnector(results=[result(df)])

    text = ask(make_agent(kb, store, llm, connector), "Conversion by signup time?")

    [(system, prompt)] = llm.chats("text")
    assert "raw, unrounded values. Round numbers sensibly" in system["content"]
    assert_in_order(
        prompt["content"],
        "Result (CSV):\nsigned_up,conversion,revenue",
        "2026-03-01 10:00:00+05:30,0.004123,1234.5678",
        "2026-03-03 00:15:00+05:30,0.0049,98765.4321",
    )
    # The chat table shows the same local times, in the configured timezone.
    assert_in_order(text, "| 2026-03-01 10:00:00 |", "| 2026-03-03 00:15:00 |")


def test_takeaway_keeps_end_of_time_timestamps_in_utc(kb, store):
    # 9999-12-31 means "no end date". East of UTC it would be the year 10000.
    valid_to = pd.Series([pd.Timestamp("9999-12-31 23:59:59", tz="UTC").as_unit("us")])
    df = pd.DataFrame({"plan": ["pro"], "valid_to": valid_to})
    llm = data_llm(SqlDraft(sql=SQL))
    connector = FakeConnector(results=[result(df)])

    ask(make_agent(kb, store, llm, connector), "Until when is the pro plan valid?")

    [takeaway] = llm.chats("text")
    assert "pro,9999-12-31 23:59:59+00:00" in takeaway[-1]["content"]


def test_large_result_shows_a_preview_and_the_row_count(kb, store):
    df = pd.DataFrame({"order_id": [f"o-{i}" for i in range(50)], "amount": range(50)})
    llm = data_llm(SqlDraft(sql=SQL))
    connector = FakeConnector(results=[result(df)])

    text = ask(make_agent(kb, store, llm, connector), "List all orders")

    assert_in_order(
        text,
        "\n\nYour query returned 50 rows.",
        "Showing the first 5 of 50 rows (2 columns).",
        "| o-4 | 4 |",
        f"[{DOWNLOAD_ICON} Download CSV (50 rows)]",
        "Show SQL",
    )
    assert "| o-5 |" not in text
    assert llm.chats("text") == []  # no takeaway for a preview


def test_too_large_result_asks_to_narrow_the_question(kb, store):
    llm = data_llm(SqlDraft(sql=SQL))
    huge = QueryResult(sql=SQL, total_rows=2_000_000, too_large=True)
    connector = FakeConnector(results=[huge])

    text = ask(make_agent(kb, store, llm, connector), "Every order ever")

    assert_in_order(
        text,
        "This result has 2,000,000 rows, which is more than the 1,000 I can return.",
        f"```sql\n{SQL}\n```",
    )
    assert list(store.export_dir.iterdir()) == []


def test_zero_rows_says_so_with_the_assumptions_and_sql(kb, store):
    llm = data_llm(SqlDraft(sql=SQL, assumptions=["city is Delhi"]))
    connector = FakeConnector(results=[result(STATUS_COUNTS.iloc[0:0])])

    text = ask(make_agent(kb, store, llm, connector), "Orders in Delhi by status")

    assert_in_order(
        text,
        "No rows matched your question. Check the filter values",
        "_Assumed: city is Delhi._",
        f"```sql\n{SQL}\n```",
    )
    assert "Download CSV" not in text and "Want a chart" not in text


def test_rejected_query_is_sent_back_and_the_fix_runs(kb, store):
    bad = "SELECT status, COUNT(*) FROM `my-project.sales.order` GROUP BY status"
    error = "BigQuery rejected the query: Not found: Table my-project:sales.order"
    llm = data_llm(SqlDraft(sql=bad), SqlDraft(sql=SQL))
    connector = FakeConnector(
        dry_runs=[(False, error), (True, "Dry run OK, will scan 1.0 KB.")],
        results=[result(STATUS_COUNTS)],
    )

    chunks = list(make_agent(kb, store, llm, connector).run([user("Orders by status?")]))

    assert chunks[:6] == [
        "> Finding the right tables…\n",
        "> Writing the SQL…\n",
        "> Checking the query…\n",
        "> Fixing the query…\n",
        "> Checking the query…\n",
        "> Running the query…\n",
    ]
    assert connector.checked == [bad, SQL]
    assert connector.executed == [SQL]
    first, second = llm.chats("SqlDraft")
    assert second == [
        *first,
        {"role": "assistant", "content": SqlDraft(sql=bad).model_dump_json()},
        {"role": "user", "content": f"The query was rejected: {error}. Return the corrected JSON."},
    ]
    assert "| paid | 120 |" in "".join(chunks)


def test_three_rejections_end_in_a_plain_failure_with_the_sql(kb, store):
    llm = data_llm(*(SqlDraft(sql=f"SELECT {n} FROM broken") for n in (1, 2, 3)))
    connector = FakeConnector(dry_runs=[(False, "Syntax error: Unexpected keyword FROM at [1:10]")])

    text = ask(make_agent(kb, store, llm, connector), "Orders by status?")

    assert len(llm.chats("SqlDraft")) == 3
    assert connector.executed == []
    assert_in_order(
        text,
        "\n\nI couldn't build a working query for this question.",
        "Last error: Syntax error: Unexpected keyword FROM at [1:10]",
        "Try rephrasing the question, or name the table to use.",
        "<details><summary>Show SQL</summary>\n\n```sql\nSELECT 3 FROM broken\n```",
    )


def test_run_errors_share_the_same_two_retries(kb, store):
    llm = data_llm(SqlDraft(sql="SELECT 1"), SqlDraft(sql="SELECT 2"), SqlDraft(sql="SELECT 3"))
    too_big = "The query would scan 80.0 GB, more than the 50 GB limit."
    connector = FakeConnector(
        dry_runs=[(False, too_big), (True, "Dry run OK, will scan 1.0 GB.")],
        results=[QueryError("Division by zero: 1 / 0"), QueryError("Resources exceeded")],
    )

    text = ask(make_agent(kb, store, llm, connector), "Orders by status?")

    assert connector.checked == ["SELECT 1", "SELECT 2", "SELECT 3"]
    assert connector.executed == ["SELECT 2", "SELECT 3"]
    assert [chat[-1]["content"] for chat in llm.chats("SqlDraft")[1:]] == [
        f"The query was rejected: {too_big} Return the corrected JSON.",
        "The query was rejected: Division by zero: 1 / 0. Return the corrected JSON.",
    ]
    assert "Last error: Resources exceeded" in text


def test_clarifying_question_is_the_whole_answer_and_nothing_runs(kb, store):
    question = "Do you mean the calendar year or the financial year (April to March)?"
    llm = data_llm(SqlDraft(clarifying_question=question))
    connector = FakeConnector()

    chunks = list(make_agent(kb, store, llm, connector).run([user("Revenue last year?")]))

    assert chunks == ["> Finding the right tables…\n", "> Writing the SQL…\n", f"\n{question}"]
    assert connector.checked == connector.executed == []


def test_chart_is_drawn_saved_and_shown_with_a_download_link(kb, store):
    spec = ChartSpec(chart_type="bar", x_col="status", y_col="orders", title="Orders by status")
    llm = data_llm(SqlDraft(sql=SQL), route=Route(intent="data", wants_chart=True), charts=[spec])
    connector = FakeConnector(results=[result(STATUS_COUNTS)])

    chunks = list(make_agent(kb, store, llm, connector).run([user("Chart orders by status")]))

    assert chunks[:5] == [*PROGRESS, "> Drawing the chart…\n"]
    text = "".join(chunks)
    png = re.search(r"!\[Orders by status\]\(/downloads/([0-9a-f]{32}\.png)\)", text).group(1)
    assert store.path_for(png).read_bytes().startswith(b"\x89PNG")
    assert_in_order(
        text,
        "Most orders are paid.",
        "Download CSV (3 rows)",
        f"![Orders by status](/downloads/{png})\n"
        f"[{DOWNLOAD_ICON} Download chart](/downloads/{png}?download=1)",
        "Show SQL",
    )
    assert "Want a chart" not in text
    [chart_chat] = llm.chats("ChartSpec")
    assert "- orders (int64)" in chart_chat[-1]["content"]
    assert "aggregate in SQL" in llm.chats("SqlDraft")[0][0]["content"]


def test_chart_problems_get_one_retry_then_the_answer_goes_without_a_chart(kb, store):
    wrong = ChartSpec(chart_type="bar", x_col="state", y_col="orders", title="Orders")
    llm = data_llm(SqlDraft(sql=SQL), route=Route(intent="data", wants_chart=True), charts=[wrong])
    connector = FakeConnector(results=[result(STATUS_COUNTS)])

    text = ask(make_agent(kb, store, llm, connector), "Chart orders by state")

    first, retry = llm.chats("ChartSpec")
    assert retry[:-2] == first
    assert retry[-1]["content"].startswith(
        "That chart spec has problems:\n- Column 'state' is not in the result"
    )
    assert_in_order(text, "| paid | 120 |", "I couldn't draw a chart for this result.", "Show SQL")
    assert list(store.export_dir.glob("*.png")) == []
    assert "Want a chart" not in text


def test_failed_takeaway_falls_back_to_the_row_count(kb, store):
    llm = data_llm(SqlDraft(sql=SQL), text=LLMError("the model timed out"))
    connector = FakeConnector(results=[result(STATUS_COUNTS)])

    text = ask(make_agent(kb, store, llm, connector), "Orders by status?")

    assert_in_order(text, "\n\nYour query returned 3 rows.\n\n| status | orders |", "Show SQL")


def test_llm_error_becomes_a_friendly_message_after_the_progress_lines(kb, store, caplog):
    llm = data_llm(LLMError("LLM call to fake/smart failed: timeout"))
    agent = make_agent(kb, store, llm, FakeConnector())

    with caplog.at_level(logging.ERROR, logger="backend.agent.router"):
        chunks = list(agent.run([user("Orders by status?")]))

    assert chunks == ["> Finding the right tables…\n", "> Writing the SQL…\n", f"\n{LLM_FAILED}"]
    assert "fake/smart failed: timeout" in caplog.text


def test_llm_error_before_any_progress_is_just_the_message(kb, store):
    llm = FakeLLM({Route: [LLMError("rate limited")]})

    assert ask(make_agent(kb, store, llm, FakeConnector()), "Orders by status?") == LLM_FAILED


def test_unexpected_error_is_logged_and_answered_politely(kb, store, caplog):
    class BrokenConnector(FakeConnector):
        def dry_run_or_validate(self, query):
            raise RuntimeError("credentials expired")

    agent = make_agent(kb, store, data_llm(SqlDraft(sql=SQL)), BrokenConnector())

    with caplog.at_level(logging.ERROR, logger="backend.agent.router"):
        text = ask(agent, "Orders by status?")

    assert "> Checking the query…\n\nSorry, something went wrong while answering." in text
    assert text.endswith("check the agent logs.")
    assert "RuntimeError: credentials expired" in caplog.text  # with the traceback


def test_help_answer_uses_real_table_names_without_a_model(kb, store):
    llm = FakeLLM({Route: [Route(intent="help")]})

    text = ask(make_agent(kb, store, llm, FakeConnector()), "Hi! What can you do?")

    assert_in_order(
        text,
        "I answer questions about your BigQuery data in plain English.",
        "- What columns are in `sales.customers` and what do they mean?",
        "- How many rows does `sales.orders` have?",
        "- Count the rows in `sales.customers` per month and show it as a chart.",
    )
    assert "sample rows" not in text  # SELECT * ... LIMIT 10 still scans the whole table
    assert llm.chats("text") == []


def test_empty_message_gets_the_help_answer(kb, store):
    llm = FakeLLM()

    text = ask(make_agent(kb, store, llm, FakeConnector()), "   ")

    assert text.startswith("I answer questions about your BigQuery data")
    assert llm.calls == []


def test_without_a_connection_the_agent_says_bigquery_is_not_connected(kb, store):
    llm = FakeLLM()

    text = ask(make_agent(kb, store, llm, None), "Orders by status?")

    assert text.startswith("BigQuery isn't connected")
    assert "agent logs" in text
    assert llm.calls == []


@pytest.mark.parametrize("intent", ["metadata", "data"])
def test_empty_kb_says_the_schema_is_still_being_read(tmp_path, store, intent):
    empty = KBCache(tmp_path / "empty.duckdb")
    llm = FakeLLM({Route: [Route(intent=intent)]})

    text = ask(make_agent(empty, store, llm, FakeConnector()), "What tables are there?")

    empty.close()
    assert text.startswith("I'm still reading the schema")
    assert "try again in a minute" in text


@pytest.mark.parametrize(
    ("status", "start", "also"),
    [
        ("idle", "I'm still reading the schema", "try again in a minute"),
        ("running", "I'm still reading the schema", "try again in a minute"),
        ("failed", "I couldn't read the list of tables from BigQuery", "check the agent logs"),
        ("done", "I didn't find any tables in BigQuery", "include and exclude lists"),
    ],
)
def test_empty_kb_reply_follows_the_curation_status(tmp_path, store, status, start, also):
    empty = KBCache(tmp_path / "empty.duckdb")
    llm = FakeLLM({Route: [Route(intent="data")]})
    agent = make_agent(empty, store, llm, FakeConnector(), curation_status=lambda: status)

    text = ask(agent, "Orders by status?")

    empty.close()
    assert text.startswith(start)
    assert also in text
    if status == "failed":
        assert "permissions problem or a wrong project name" in text
        assert "try again in a minute" not in text  # waiting won't help


def test_with_many_tables_the_fast_model_picks_at_most_five(many_tables_kb, store):
    names = ["web.t03", "T07", "no_such_table", "t01", "t02", "t04", "t05"]
    llm = FakeLLM(
        {
            Route: [Route(intent="data")],
            TablePick: [TablePick(tables=names)],
            SqlDraft: [SqlDraft(clarifying_question="Which value do you mean?")],
        }
    )

    ask(make_agent(many_tables_kb, store, llm, FakeConnector()), "Total value per table")

    [pick] = llm.chats("TablePick")
    assert "- my-project.web.t00 — (no summary yet)" in pick[-1]["content"]
    [sql_chat] = llm.chats("SqlDraft")
    shown = re.findall(r"^`my-project\.web\.(t\d\d)`", sql_chat[-1]["content"], re.MULTILINE)
    assert shown == ["t03", "t07", "t01", "t02", "t04"]


def test_when_no_table_fits_a_data_question_the_agent_asks_which_one(many_tables_kb, store):
    llm = FakeLLM({Route: [Route(intent="data")], TablePick: [TablePick(tables=[])]})

    chunks = list(make_agent(many_tables_kb, store, llm, FakeConnector()).run([user("Revenue?")]))

    assert chunks[0] == "> Finding the right tables…\n"
    assert_in_order("".join(chunks), "Which one should I use?", "- `web.t00`", "- `web.t19`")
    assert llm.chats("SqlDraft") == []


def test_metadata_with_many_tables_also_gets_the_catalog(many_tables_kb, store):
    llm = FakeLLM(
        {Route: [Route(intent="metadata")], TablePick: [TablePick(tables=[])]},
        text="There are 20 tables in `web`.",
    )

    text = ask(make_agent(many_tables_kb, store, llm, FakeConnector()), "What tables exist?")

    assert text == "There are 20 tables in `web`."
    [chat] = llm.chats("text")
    assert "All tables:\n- my-project.web.t00 — (no summary yet)" in chat[-1]["content"]
    assert "Table details:" not in chat[-1]["content"]


def test_sql_prompt_has_the_rules_the_context_and_the_previous_sql(kb, store):
    previous = "SELECT status, COUNT(*) AS orders FROM `my-project.sales.orders` GROUP BY status"
    earlier_answer = (
        "Most orders are paid.\n\n<details><summary>Show SQL</summary>\n\n"
        f"```sql\n{previous}\n```\n\n</details>"
    )
    history = [user("Orders by status?"), {"role": "assistant", "content": earlier_answer}]
    route = Route(intent="data", uses_previous_result=True)
    llm = data_llm(SqlDraft(clarifying_question="Which city?"), route=route)

    ask(make_agent(kb, store, llm, FakeConnector()), "Now only Delhi", history)

    [route_chat] = llm.chats("Route")
    assert_in_order(
        route_chat[-1]["content"], "- sales.customers\n- sales.orders", "User: Orders by status?"
    )
    [(system, prompt)] = llm.chats("SqlDraft")
    today = datetime.now(ZoneInfo("Asia/Kolkata")).date().isoformat()
    for rule in [
        "GoogleSQL",
        "- Name every table in full, in backticks.",
        f"{today}, in the Asia/Kolkata timezone",
        "CURRENT_DATE('Asia/Kolkata')",
        "result are shown to the user in the Asia/Kolkata timezone automatically",
        "never add up\n  amounts in different currencies",
        "ask a clarifying question only when a wrong guess would mislead",
    ]:
        assert rule in system["content"]
    assert "aggregate in SQL" not in system["content"]  # no chart was asked for
    assert_in_order(
        prompt["content"],
        "Business context:\nRevenue means paid orders only.",
        "Tables:\n`my-project.sales.customers`",
        f"Previous SQL, which the question builds on:\n```sql\n{previous}\n```",
        "Conversation so far:\nUser: Orders by status?",
        "Question:\nNow only Delhi",
    )
