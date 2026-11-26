"""The fixed answers the agent gives without asking a model: help, errors and short notes.

Long lines are kept whole on purpose: the chat may show a line break wherever the text has one.
"""

from __future__ import annotations

from backend.agent.prompts import short_name, summary_line
from backend.bootstrap.kb_cache import TableInfo

# When the agent asks which table to use, it lists at most this many.
MAX_LISTED_TABLES = 25

NOT_CONNECTED = (
    "BigQuery isn't connected, so I can't look at the data right now. Please check the agent "
    "logs and the connection settings in `config/config.yaml`."
)
SCHEMA_LOADING = (
    "I'm still reading the schema of your BigQuery tables. Please try again in a minute. "
    "If this keeps happening, check the agent logs."
)
CATALOG_FAILED = (
    "I couldn't read the list of tables from BigQuery, so I can't answer questions about the "
    "data yet. This is usually a permissions problem or a wrong project name. Please check the "
    "agent logs."
)
NO_TABLES = (
    "I didn't find any tables in BigQuery. Check the project name and the dataset include and "
    "exclude lists in `config/config.yaml`, and that the service account can see the datasets."
)
LLM_FAILED = "The language model didn't respond. Please try again in a moment."
UNEXPECTED_ERROR = (
    "Sorry, something went wrong while answering. Please try again, and if it keeps "
    "happening, check the agent logs."
)
NO_ROWS = (
    "No rows matched your question. Check the filter values in the SQL below: the data may "
    "spell a name, status or code differently."
)
CHART_FAILED = "I couldn't draw a chart for this result."
CHART_OFFER = "_Want a chart of this? Just ask._"


def rows_returned(count: int) -> str:
    return f"Your query returned {count:,} {'row' if count == 1 else 'rows'}."


def query_failed(error: str) -> str:
    return (
        "I couldn't build a working query for this question.\n\n"
        f"Last error: {' '.join(error.split())}\n\n"
        "Try rephrasing the question, or name the table to use."
    )


def which_table(tables: list[TableInfo]) -> str:
    """Asks the user to choose, when no table seemed to fit a data question."""
    lines = []
    for table in tables[:MAX_LISTED_TABLES]:
        about = summary_line(table)
        lines.append(f"- `{short_name(table)}`" + (f": {about}" if about else ""))
    if len(tables) > MAX_LISTED_TABLES:
        lines.append(f"- …and {len(tables) - MAX_LISTED_TABLES:,} more")
    return (
        "I couldn't tell which table to use for this. Which one should I use?\n\n"
        + "\n".join(lines)
        + "\n\nYou can also name the table in your question."
    )


def help_text(tables: list[TableInfo]) -> str:
    """What the agent can do, with example questions about the first tables in the KB."""
    names = [f"`{short_name(table)}`" for table in tables[:3]]
    if names:
        first, second, third = (names * 3)[:3]
        # Cheap questions only: "show 10 sample rows" scans the whole table in BigQuery,
        # because a LIMIT doesn't reduce the bytes read.
        examples = [
            f"What columns are in {first} and what do they mean?",
            f"How many rows does {second} have?",
            f"Count the rows in {third} per month and show it as a chart.",
        ]
    else:
        examples = [
            "Which tables have customer data?",
            "What columns are in the orders table and what do they mean?",
            "Show total sales per month this year as a chart.",
        ]
    lines = [
        "I answer questions about your BigQuery data in plain English.",
        "",
        "- **Explore**: ask which tables exist, what a table holds or what its columns mean. "
        "I answer from the table catalog, without running a query.",
        "- **Analyse**: ask for counts, totals, trends, top lists or comparisons. I write the "
        "SQL, check it, run it on BigQuery and show the result, a CSV download and the SQL.",
        '- **Charts**: ask for a chart, or say "chart that" after an answer.',
        '- **Follow-ups**: "now by month", "only Delhi" or "same for last year" build on the '
        "previous answer.",
        "",
        "Try asking:",
        *(f"- {example}" for example in examples),
    ]
    return "\n".join(lines)
