"""All prompt text: the system prompts, and the functions that build each chat for the LLM.

Each ``*_messages`` function returns a chat for LLMClient: a system prompt with the rules, and one
user message with the context (tables, conversation, question) in labelled sections. The fixed
answers that need no model are in replies.py.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import date

import pandas as pd

from backend.bootstrap.kb_cache import ColumnInfo, TableDetail, TableInfo
from backend.services.renderer import df_to_markdown

# The router sees every table name up to this many; with more, the datasets and their counts.
MAX_ROUTER_TABLE_NAMES = 150
MAX_SUMMARY_CHARS = 300
CHART_SAMPLE_ROWS = 5

ROUTE_SYSTEM = """\
You sort the messages sent to a chat assistant that answers questions about a company's
BigQuery data.

Decide:
- intent:
  - "metadata": a question about the data's structure that the table catalog answers without
    running a query: which tables exist, what a table or column holds or means, column types,
    typical values, table sizes.
  - "data": the answer needs numbers or rows from the data itself: counts, totals, averages,
    trends, top lists, comparisons, filtered lists, or a chart of these. A follow-up that
    changes or charts an earlier data answer is "data" too.
  - "help": greetings, thanks, questions about what the assistant can do or how to use it, and
    anything that isn't about the data.
- wants_chart: true when the user asks for a chart, graph, plot or another visual, or says
  something like "chart that".
- uses_previous_result: true when the message builds on the previous answer, for example
  "now by month", "only Delhi", "same for last year" or "chart that"."""

PICK_SYSTEM = """\
You pick the tables an SQL writer needs to answer a question about a company's BigQuery data.

Reply with at most 5 table names from the catalog, written exactly as in the catalog, the most
useful first.
- Include every table that a join needs.
- For a follow-up question, keep the tables of the previous SQL unless the question asks for
  other data.
- Return an empty list when no table fits."""

METADATA_SYSTEM = """\
You answer questions about a company's BigQuery tables for business users. Use only the
catalog information you are given: table summaries and descriptions, row counts, and columns
with their types, descriptions, meanings and sample values.
- When the catalog doesn't hold the answer, say so plainly, for example "The catalog doesn't
  say what this column means." Never invent tables, columns or values.
- You can't see the data itself. When someone wants numbers or rows from the data, suggest
  they ask for them, for example "Ask me: how many orders were there per month?"
- Sample values are listed only for columns with a few distinct values.
- Reply in markdown: the answer first, then a short list or a small table when it helps.
  Write table names as `dataset.table` and column names in backticks."""

TAKEAWAY_SYSTEM = """\
You write the takeaway shown above a query result in a chat: 1-2 plain sentences that answer
the question with the key numbers from the result, written as they appear there.
- Don't list every row and don't guess at causes.
- Never add up or compare amounts in different currencies as if they were one.
- Plain text only: no headings, lists or tables."""

CHART_SYSTEM = """\
You choose how to chart a query result. Fixed code draws the chart from your reply.
- Use column names exactly as listed.
- chart_type: "line" for values over time (x_col is the date or time column), "bar" to compare
  categories, "pie" only for shares of a whole with a few categories, "scatter" for two
  numeric columns, "hist" for the distribution of one numeric column, "box" for the spread of
  a numeric column per category, "heatmap" for a number across two categories (x_col and y_col
  are the categories, value_col is the number).
- y_col is the numeric column for bar, line, scatter, box and pie charts.
- hue_col is optional: a category column that splits the data into coloured series.
- With many categories, set top_n to keep the biggest. Use horizontal bars for long names.
- title: short and specific. x_label and y_label: plain words, or null."""


def route_messages(tables: list[TableInfo], history: list[dict], question: str) -> list[dict]:
    return _chat(
        ROUTE_SYSTEM,
        ("Tables", table_names(tables)),
        ("Conversation so far", transcript(history)),
        ("Latest message", question),
    )


def pick_messages(
    tables: list[TableInfo], business_context: str, history: list[dict], question: str
) -> list[dict]:
    return _chat(
        PICK_SYSTEM,
        ("Business context", business_context),
        ("Catalog", catalog(tables)),
        ("Conversation so far", transcript(history)),
        ("Question", question),
    )


def metadata_messages(
    tables: list[TableDetail],
    all_tables: list[TableInfo],
    business_context: str,
    history: list[dict],
    question: str,
) -> list[dict]:
    """``all_tables`` adds the one-line catalog, for questions about tables not shown in full."""
    return _chat(
        METADATA_SYSTEM,
        ("Business context", business_context),
        ("All tables", catalog(all_tables)),
        ("Table details", table_details(tables)),
        ("Conversation so far", transcript(history)),
        ("Question", question),
    )


def sql_messages(
    *,
    sql_hints: str,
    timezone: str,
    today: date,
    business_context: str,
    wants_chart: bool,
    tables: list[TableDetail],
    previous_sql: str | None,
    history: list[dict],
    question: str,
) -> list[dict]:
    chart_rule = ""
    if wants_chart:
        chart_rule = (
            "\n- The user wants a chart: aggregate in SQL so the result is small and ready to\n"
            "  plot, for example one row per month or per category."
        )
    system = f"""\
You are a careful analytics engineer. You write one GoogleSQL query for BigQuery that answers
the user's question.

GoogleSQL rules:
{sql_hints}

Dates: today is {today:%A}, {today.isoformat()}, in the {timezone} timezone. For "today", "this
month", "last 7 days" and the like, use CURRENT_DATE('{timezone}'), never a fixed date. To
group or filter a TIMESTAMP by local day, use DATE(ts, '{timezone}').

More rules:
- Use only the tables and columns you are given. Never guess other names.
- Follow the definitions and rules in the business context, when there is one.
- Several currencies may exist. Never convert amounts between currencies and never add up
  amounts in different currencies: keep them apart, for example by grouping by the currency.
- Don't add a LIMIT unless the question asks for a top N or a sample: the user can download
  every row of the result.{chart_rule}

Ask or guess: ask a clarifying question only when a wrong guess would mislead: an unclear
metric definition, a time range that changes the answer, or a calendar choice (calendar or
financial year, the first day of the week). Otherwise make a sensible guess and list it in
assumptions.

Reply fields:
- sql: one SELECT (or WITH ... SELECT) statement, or null when you ask a question.
- assumptions: each guess you made, as a short phrase such as "last 30 days" or "paid orders
  only". An empty list when you made none.
- clarifying_question: one short question for the user when you must ask first, else null."""
    previous = f"```sql\n{previous_sql}\n```" if previous_sql else ""
    return _chat(
        system,
        ("Business context", business_context),
        ("Tables", table_details(tables)),
        ("Previous SQL, which the question builds on", previous),
        ("Conversation so far", transcript(history)),
        ("Question", question),
    )


def sql_fix_request(error: str) -> str:
    """The reply to a rejected query. The error goes back as it is, so the model can fix it."""
    return f"The query was rejected: {error.rstrip('. ')}. Return the corrected JSON."


def takeaway_messages(question: str, result_table: str) -> list[dict]:
    return _chat(TAKEAWAY_SYSTEM, ("Question", question), ("Result", result_table))


def chart_messages(question: str, df: pd.DataFrame) -> list[dict]:
    columns = "\n".join(f"- {name} ({dtype})" for name, dtype in df.dtypes.items())
    return _chat(
        CHART_SYSTEM,
        ("Question", question),
        (f"Result columns ({len(df):,} rows)", columns),
        ("First rows", df_to_markdown(df.head(CHART_SAMPLE_ROWS))),
    )


def chart_fix_request(problems: list[str]) -> str:
    listed = "\n".join(f"- {problem}" for problem in problems)
    return f"That chart spec has problems:\n{listed}\nReturn the corrected JSON."


def table_names(tables: list[TableInfo]) -> str:
    """Each table as dataset.table or, when there are very many, each dataset with a count."""
    if not tables:
        return "(none yet)"
    if len(tables) <= MAX_ROUTER_TABLE_NAMES:
        return "\n".join(f"- {short_name(table)}" for table in tables)
    counts = Counter(table.schema_name for table in tables)
    return "\n".join(f"- {name} ({count:,} tables)" for name, count in sorted(counts.items()))


def catalog(tables: list[TableInfo]) -> str:
    """One "qualified_name — summary" line per table."""
    return "\n".join(
        f"- {table.qualified_name} — {summary_line(table) or '(no summary yet)'}"
        for table in tables
    )


def table_details(tables: list[TableDetail]) -> str:
    """Everything the KB knows about each table, for SQL writing and metadata answers."""
    return "\n\n".join(_table_detail(table) for table in tables)


def transcript(history: list[dict]) -> str:
    """The earlier turns from clean_history as "User: ..." and "Assistant: ..." paragraphs."""
    return "\n\n".join(f"{m['role'].capitalize()}: {m['content']}" for m in history)


def short_name(table: TableInfo) -> str:
    return f"{table.schema_name}.{table.table_name}"


def summary_line(table: TableInfo) -> str:
    """The summary, or the description until the table is curated, as one short line."""
    text = _one_line(table.summary or table.description)
    if len(text) > MAX_SUMMARY_CHARS:
        text = text[: MAX_SUMMARY_CHARS - 1].rstrip() + "…"
    return text


def _chat(system: str, *sections: tuple[str, str]) -> list[dict]:
    """The system prompt, then one user message of "Title:" sections. Empty ones are left out."""
    user = "\n\n".join(f"{title}:\n{text}" for title, text in sections if text)
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _table_detail(table: TableDetail) -> str:
    facts = table.table_type.lower()
    if table.row_count is not None:
        facts += f", {table.row_count:,} rows"
    lines = [f"`{table.qualified_name}` ({facts})"]
    if table.summary:
        lines.append(f"Summary: {table.summary}")
    if table.description:
        lines.append(f"Description: {_one_line(table.description)}")
    lines.append("Columns:")
    lines.extend(_column_line(column) for column in table.columns)
    return "\n".join(lines)


def _column_line(column: ColumnInfo) -> str:
    kind = [column.data_type]
    if column.mode == "REPEATED":
        kind.append("REPEATED")
    if column.is_partition:
        kind.append("partition column")
    if column.clustering_order:
        kind.append(f"clustering #{column.clustering_order}")
    notes = [_one_line(text) for text in (column.description, column.meaning) if text]
    if column.sample_values:
        notes.append("values: " + json.dumps(column.sample_values, ensure_ascii=False))
    line = f"- {column.name} ({', '.join(kind)})"
    return f"{line}: {'; '.join(notes)}" if notes else line


def _one_line(text: str) -> str:
    return " ".join(text.split())
