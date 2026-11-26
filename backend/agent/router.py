"""The agent: works out what the latest chat message asks for and answers it in markdown.

The flow follows docs/DECISIONS.md, section 3. Open WebUI's background tasks go straight to the
fast model. Metadata questions are answered from the KB, without BigQuery. Data questions get
SQL that is written, checked and run, then shown as a table with a CSV download and, when the
user asks for one, a chart.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from datetime import datetime
from decimal import Decimal
from typing import Literal
from zoneinfo import ZoneInfo

import pandas as pd
from pydantic import BaseModel, Field

from backend.agent import prompts, replies
from backend.agent.history import clean_history, is_openwebui_task, last_sql, latest_user_text
from backend.agent.sql_writer import SqlOutcome, write_and_run
from backend.bootstrap.kb_cache import KBCache, TableDetail, TableInfo
from backend.config import AppConfig
from backend.connectors.base import BaseDBConnector
from backend.llm import LLMClient, LLMError
from backend.services.charts import ChartSpec, check_spec, render_chart
from backend.services.exports import ExportStore
from backend.services.renderer import (
    DOWNLOAD_ICON,
    render_assumptions,
    render_result,
    render_sql_block,
    render_too_large,
)

log = logging.getLogger(__name__)

# Up to this many tables, the model sees all of them and the pick step is skipped.
MAX_TABLES_WITHOUT_PICK = 15
MAX_PICKED_TABLES = 5


class Route(BaseModel):
    """What the fast model decided about the latest message."""

    intent: Literal["metadata", "data", "help"]
    wants_chart: bool = False
    uses_previous_result: bool = False


class TablePick(BaseModel):
    tables: list[str] = Field(default_factory=list)


class AnalyticsAgent:
    def __init__(
        self,
        config: AppConfig,
        connectors: dict[str, BaseDBConnector],
        kb: KBCache,
        llm: LLMClient,
        store: ExportStore,
        *,
        curation_status: Callable[[], str] | None = None,
    ):
        """``curation_status`` returns the curator's status. With an empty KB it tells
        "still reading the tables" apart from "reading them failed"."""
        self.config = config
        self.connectors = connectors
        self.kb = kb
        self.llm = llm
        self.store = store
        self.curation_status = curation_status
        # v1 answers from the first connection that connected.
        self.connector = next(iter(connectors.values()), None)

    def run(self, messages: list[dict]) -> Iterator[str]:
        """Answer the latest user message: progress lines first, then the answer.

        Never raises. A failure is logged and becomes a short message in the chat, so Open WebUI
        always gets a reply.
        """
        started = False
        try:
            for chunk in self._answer(messages):
                started = True
                yield chunk
        except LLMError:
            log.exception("the language model failed")
            yield ("\n" if started else "") + replies.LLM_FAILED
        except Exception:
            log.exception("could not answer the message")
            yield ("\n" if started else "") + replies.UNEXPECTED_ERROR

    def _answer(self, messages: list[dict]) -> Iterator[str]:
        question = latest_user_text(messages)
        if not question:
            yield self._help()
            return
        if is_openwebui_task(question):
            # Chat titles, tags and follow-up suggestions need neither the KB nor BigQuery.
            yield self.llm.complete([{"role": "user", "content": question}], tier="fast")
            return
        if self.connector is None:
            yield replies.NOT_CONNECTED
            return

        tables = self.kb.list_tables(self.connector.name)
        history = clean_history(messages)
        route = self.llm.complete_json(
            prompts.route_messages(tables, history, question), Route, tier="fast"
        )
        log.info(
            "route: %s (chart: %s, follow-up: %s)",
            route.intent,
            route.wants_chart,
            route.uses_previous_result,
        )
        if route.intent == "help":
            yield self._help()
        elif not tables:
            yield self._no_tables_reply()
        elif route.intent == "metadata":
            yield self._metadata_answer(tables, history, question)
        else:
            yield from self._data_answer(tables, route, messages, history, question)

    def _help(self) -> str:
        connection = self.connector.name if self.connector else None
        return replies.help_text(self.kb.list_tables(connection))

    def _no_tables_reply(self) -> str:
        """Why the KB has no tables yet: still being read, reading failed, or there are none."""
        status = self.curation_status() if self.curation_status else "running"
        if status == "failed":
            return replies.CATALOG_FAILED
        if status == "done":
            return replies.NO_TABLES
        return replies.SCHEMA_LOADING

    def _pick_tables(
        self, tables: list[TableInfo], history: list[dict], question: str
    ) -> list[TableDetail]:
        """All tables when there are few, otherwise the ones the fast model picks (at most 5)."""
        connection = self.connector.name
        if len(tables) <= MAX_TABLES_WITHOUT_PICK:
            return self.kb.find_tables([table.qualified_name for table in tables], connection)
        chat = prompts.pick_messages(tables, self.kb.get_business_context(), history, question)
        pick = self.llm.complete_json(chat, TablePick, tier="fast")
        picked = self.kb.find_tables(pick.tables, connection)[:MAX_PICKED_TABLES]
        log.info("picked tables: %s", ", ".join(t.qualified_name for t in picked) or "none")
        return picked

    def _metadata_answer(self, tables: list[TableInfo], history: list[dict], question: str) -> str:
        picked = self._pick_tables(tables, history, question)
        # With few tables the details already cover all of them. Otherwise the one-line catalog
        # lets the model answer "which tables ..." and still works when nothing was picked.
        all_tables = tables if len(tables) > MAX_TABLES_WITHOUT_PICK else []
        chat = prompts.metadata_messages(
            picked, all_tables, self.kb.get_business_context(), history, question
        )
        return self.llm.complete(chat, tier="fast")

    def _data_answer(
        self,
        tables: list[TableInfo],
        route: Route,
        messages: list[dict],
        history: list[dict],
        question: str,
    ) -> Iterator[str]:
        yield "> Finding the right tables…\n"
        picked = self._pick_tables(tables, history, question)
        if not picked:
            yield "\n" + replies.which_table(tables)
            return

        timezone = self.config.timezone
        chat = prompts.sql_messages(
            sql_hints=self.connector.sql_hints(),
            timezone=timezone,
            today=datetime.now(ZoneInfo(timezone)).date(),
            business_context=self.kb.get_business_context(),
            wants_chart=route.wants_chart,
            tables=picked,
            previous_sql=last_sql(messages) if route.uses_previous_result else None,
            history=history,
            question=question,
        )
        outcome = yield from write_and_run(self.llm, self.connector, chat)
        if outcome.clarifying_question:
            yield "\n" + outcome.clarifying_question
        elif outcome.result is None:
            sql_block = render_sql_block(outcome.sql) if outcome.sql else ""
            yield "\n" + _join(replies.query_failed(outcome.error), sql_block)
        else:
            yield from self._result_answer(outcome, route.wants_chart, question)

    def _result_answer(
        self, outcome: SqlOutcome, wants_chart: bool, question: str
    ) -> Iterator[str]:
        """The answer for a query that ran, laid out as in DECISIONS.md section 6."""
        result = outcome.result
        assumptions = render_assumptions(outcome.assumptions)
        sql_block = render_sql_block(outcome.sql)
        if result.too_large:
            too_large = render_too_large(result.total_rows, self.connector.config.max_rows_fetch)
            yield "\n" + _join(too_large, assumptions, sql_block)
            return
        df = result.df
        if df is None or df.empty:
            yield "\n" + _join(replies.NO_ROWS, assumptions, sql_block)
            return

        rendered = render_result(
            df, len(df), self.store, self.config.output, timezone=self.config.timezone
        )
        chart = ""
        if wants_chart:
            yield "> Drawing the chart…\n"
            chart = self._chart(df, question)
        lead = self._takeaway(question, df) if rendered.inline else replies.rows_returned(len(df))
        # The offer would only confuse after a chart was asked for, drawn or not.
        offer = replies.CHART_OFFER if not wants_chart and _worth_a_chart(df) else ""
        yield "\n" + _join(lead, rendered.markdown, chart, assumptions, sql_block, offer)

    def _takeaway(self, question: str, df: pd.DataFrame) -> str:
        """1-2 sentences on what the result shows. The row count stands in if the model fails.

        Only called for inline results (at most inline_table_max_rows), so the CSV is small.
        """
        try:
            chat = prompts.takeaway_messages(question, _raw_csv(df, self.config.timezone))
            text = self.llm.complete(chat, tier="fast").strip()
        except LLMError as exc:
            log.warning("answering without a takeaway: %s", exc)
            text = ""
        return text or replies.rows_returned(len(df))

    def _chart(self, df: pd.DataFrame, question: str) -> str:
        """The chart image and its download link, or one line saying it couldn't be drawn.

        The chart is extra: whatever goes wrong here, the rest of the answer still goes out.
        """
        try:
            spec = self._chart_spec(df, question)
            url = self.store.url_for(self.store.save_png(render_chart(df, spec)))
        except Exception:
            log.exception("could not draw a chart")
            return replies.CHART_FAILED
        title = " ".join(spec.title.replace("[", "(").replace("]", ")").split())
        return f"![{title}]({url})\n[{DOWNLOAD_ICON} Download chart]({url}?download=1)"

    def _chart_spec(self, df: pd.DataFrame, question: str) -> ChartSpec:
        """The fast model's spec. One retry with the problems when check_spec finds any."""
        chat = prompts.chart_messages(question, df)
        spec = self.llm.complete_json(chat, ChartSpec, tier="fast")
        problems = check_spec(spec, df)
        if problems:
            log.info("chart spec has problems, asking again: %s", " ".join(problems))
            chat = [
                *chat,
                {"role": "assistant", "content": spec.model_dump_json()},
                {"role": "user", "content": prompts.chart_fix_request(problems)},
            ]
            spec = self.llm.complete_json(chat, ChartSpec, tier="fast")
        return spec  # render_chart checks it again and raises if problems are left


def _join(*parts: str) -> str:
    """The non-empty parts of an answer, a blank line apart."""
    return "\n\n".join(part for part in parts if part)


def _raw_csv(df: pd.DataFrame, timezone: str) -> str:
    """The rows as CSV, unrounded. TIMESTAMP columns are moved to the timezone the chat table
    shows them in, so the takeaway and the table name the same times."""
    local = df.copy()
    for name, column in df.items():
        # A column with 9999-12-31 ("no end date") stays in UTC: east of UTC that is the
        # year 10000, which pandas can't write out.
        if (
            isinstance(column.dtype, pd.DatetimeTZDtype)
            and column.dropna().dt.year.between(2, 9998).all()
        ):
            local[name] = column.dt.tz_convert(timezone)
    return local.to_csv(index=False)


def _worth_a_chart(df: pd.DataFrame) -> bool:
    """True for two or more rows with a column of numbers to plot."""
    return len(df) >= 2 and any(_holds_numbers(column) for _, column in df.items())


def _holds_numbers(column: pd.Series) -> bool:
    if pd.api.types.is_bool_dtype(column):
        return False
    if pd.api.types.is_numeric_dtype(column):
        return True
    # BigQuery NUMERIC and BIGNUMERIC values arrive as Decimal objects in an object column.
    values = column.dropna()
    return not values.empty and all(isinstance(value, Decimal) for value in values)
