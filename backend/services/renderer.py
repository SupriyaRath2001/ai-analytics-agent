"""Markdown for data answers: result tables, previews, the download line and the SQL block.

The layout and the number and date formats follow docs/DECISIONS.md, sections 5 and 6.
"""

from __future__ import annotations

import numbers
import re
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from pydantic import BaseModel

from backend.config import OutputConfig
from backend.services.exports import ExportStore

DOWNLOAD_ICON = "⬇️"  # the down-arrow emoji at the start of download links

# Integer columns whose names end in these words are labels, not amounts: 2024, not 2,024, and
# phone or order numbers (phone_number, mobile, order_no, invoice_num) without separators.
# Names ending in "id" or "code" (customer_id, userId, zipcode, pincode) count too.
_LABEL_WORDS = {"year", "zip", "pin", "phone", "mobile", "number", "no", "num"}
# Ordinary words that end in "id" but name amounts and counts: amount_paid, max_bid, rows_valid.
_NOT_ID_WORDS = set("paid unpaid prepaid repaid bid aid valid invalid void android".split())

# Local times only exist from year 1 to 9999. The 9999-12-31 23:59:59 UTC that history tables use
# for "no end date" would land in the year 10000 east of UTC, so values this close to either end
# are shown without converting them.
_EARLIEST = datetime.min.replace(tzinfo=UTC) + timedelta(days=1)
_LATEST = datetime.max.replace(tzinfo=UTC) - timedelta(days=1)


class RenderedResult(BaseModel):
    markdown: str  # the table (or preview) plus the download-link line
    inline: bool  # True when every row is shown in the chat
    csv_file: str  # file name in the ExportStore


def format_cell(value: object, column_name: str, timezone: str = "UTC") -> str:
    """One table cell as text: 1,234,567 / 1,234.57 / 0.0042 / 2024-01-31 / 2024-01-31 09:30:00.

    Timestamps that carry a zone (BigQuery TIMESTAMP) are shown in ``timezone``; naive ones
    (DATETIME) as they are.
    """
    if _is_missing(value):
        return ""
    if isinstance(value, (bool, np.bool_)):
        return str(bool(value))
    if isinstance(value, numbers.Integral):
        return str(int(value)) if _looks_like_label(column_name) else f"{int(value):,}"
    if isinstance(value, (numbers.Real, Decimal)):  # Decimal: BigQuery NUMERIC / BIGNUMERIC
        return _format_fraction(value)
    if isinstance(value, np.datetime64):
        value = pd.Timestamp(value)
    if isinstance(value, datetime):  # also pd.Timestamp; checked before date, its parent class
        return _format_datetime(value, timezone)
    if isinstance(value, date):
        return value.isoformat()
    return _escape(str(value))


def df_to_markdown(df: pd.DataFrame) -> str:
    """The whole DataFrame as a markdown table; callers pass only the rows they want shown.

    Timestamps that carry a zone are shown in UTC.
    """
    return _markdown_table(df, "UTC")


def render_result(
    df: pd.DataFrame,
    total_rows: int,
    store: ExportStore,
    output: OutputConfig,
    timezone: str = "UTC",
) -> RenderedResult:
    """Save the rows as CSV, then show them all, or a short preview when the result is big.

    The chat shows timestamps in ``timezone``; the CSV keeps the values as BigQuery sent them.
    """
    csv_file = store.save_csv(df)
    url = store.url_for(csv_file)
    download = f"[{DOWNLOAD_ICON} Download CSV ({_count(total_rows, 'row')})]({url})"
    column_count = len(df.columns)
    if (
        total_rows <= output.inline_table_max_rows
        and column_count <= output.inline_table_max_columns
    ):
        markdown = f"{_markdown_table(df, timezone)}\n\n{download}"
        return RenderedResult(markdown=markdown, inline=True, csv_file=csv_file)

    preview = df.head(output.preview_rows)
    columns = _count(column_count, "column")
    if len(preview) < total_rows:
        note = f"Showing the first {len(preview):,} of {_count(total_rows, 'row')} ({columns})."
    else:  # every row fits in the preview, the result is just too wide to count as inline
        note = f"The result has {_count(total_rows, 'row')} and {columns}."
    markdown = f"{note}\n\n{_markdown_table(preview, timezone)}\n\n{download}"
    return RenderedResult(markdown=markdown, inline=False, csv_file=csv_file)


def render_too_large(total_rows: int, max_rows: int) -> str:
    return (
        f"This result has {total_rows:,} rows, which is more than the {max_rows:,} I can return. "
        "Try adding a date range or a filter, or ask for a summary (totals by month, top 10, …)."
    )


def render_sql_block(sql: str) -> str:
    """A collapsed "Show SQL" section. Follow-up questions find the previous SQL in it.

    ``<details>`` and ``<summary>`` each end their own line: Open WebUI only turns them into a
    collapsible block then. On one line, the chat shows the raw tags as text.
    """
    return f"<details>\n<summary>Show SQL</summary>\n\n```sql\n{sql.strip()}\n```\n\n</details>"


def render_assumptions(assumptions: list[str]) -> str:
    """One italic line, e.g. "_Assumed: last 30 days; paid orders only._", or "" for none."""
    # Joined into one sentence, so line breaks and each item's own final period are dropped.
    items = [" ".join(item.split()).rstrip(". ") for item in assumptions]
    items = [item for item in items if item]
    if not items:
        return ""
    return f"_Assumed: {'; '.join(items)}._"


def _markdown_table(df: pd.DataFrame, timezone: str) -> str:
    columns = [str(column) for column in df.columns]
    lines = [
        "| " + " | ".join(_escape(column) for column in columns) + " |",
        "|" + "---|" * len(columns),
    ]
    for row in df.itertuples(index=False, name=None):
        cells = [
            format_cell(value, column, timezone) for value, column in zip(row, columns, strict=True)
        ]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def _is_missing(value: object) -> bool:
    """None, NaN, NaT and pd.NA. Lists and arrays (REPEATED columns) are never missing."""
    return pd.api.types.is_scalar(value) and bool(pd.isna(value))


def _looks_like_label(column_name: str) -> bool:
    # Words from snake_case and camelCase names: "CustomerID" -> ["Customer", "ID"].
    words = re.findall(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+", column_name)
    if not words:
        return False
    last = words[-1].lower()
    if last in _NOT_ID_WORDS:
        return False
    return last.endswith(("id", "code")) or last in _LABEL_WORDS


def _format_fraction(value: numbers.Real | Decimal) -> str:
    """1,234.57 and 12 (at most 2 decimals); below 1, three significant digits: 0.0042, 0.0137."""
    if value == 0:
        return "0"  # also -0.0
    if abs(value) >= 1:
        text = f"{value:,.2f}"
    else:
        # Two decimals would turn 0.0042 into 0. Round in scientific notation, then write the
        # number out in full: Python's own short form for 0.000042 is 4.2e-05.
        text = format(Decimal(f"{value:.2e}"), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _format_datetime(value: datetime, timezone: str) -> str:
    """2024-01-31 09:30:15. A value with a zone is converted to ``timezone`` first."""
    if value.utcoffset() is not None and _EARLIEST <= value <= _LATEST:
        value = value.astimezone(ZoneInfo(timezone))
    return value.replace(tzinfo=None).isoformat(sep=" ", timespec="seconds")


def _escape(text: str) -> str:
    """Keep text inside its table cell: escape backslashes and pipes, flatten line breaks."""
    # Backslashes first, so a "\|" already in the data can't end the cell.
    text = text.replace("\\", "\\\\").replace("|", "\\|")
    return re.sub(r"[\r\n]+", " ", text)


def _count(number: int, noun: str) -> str:
    """'1 row', '1,234 rows'."""
    return f"{number:,} {noun}" if number == 1 else f"{number:,} {noun}s"
