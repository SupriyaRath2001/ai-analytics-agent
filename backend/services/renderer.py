"""Markdown for data answers: result tables, previews, the download line and the SQL block.

The layout and the number and date formats follow docs/DECISIONS.md, sections 5 and 6.
"""

from __future__ import annotations

import numbers
import re
from datetime import date, datetime
from decimal import Decimal

import numpy as np
import pandas as pd
from pydantic import BaseModel

from backend.config import OutputConfig
from backend.services.exports import ExportStore

DOWNLOAD_ICON = "⬇️"  # the down-arrow emoji at the start of download links

# Integer columns whose names end in these words are labels, not amounts: 2024, not 2,024.
# Names ending in "id" or "code" (customer_id, userId, zipcode, pincode) count too.
_LABEL_WORDS = {"year", "zip", "pin", "phone"}


class RenderedResult(BaseModel):
    markdown: str  # the table (or preview) plus the download-link line
    inline: bool  # True when every row is shown in the chat
    csv_file: str  # file name in the ExportStore


def format_cell(value: object, column_name: str) -> str:
    """One table cell as text: 1,234,567 / 1,234.57 / 2024-01-31 / 2024-01-31 09:30:00."""
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
        return value.replace(tzinfo=None).isoformat(sep=" ", timespec="seconds")
    if isinstance(value, date):
        return value.isoformat()
    return _escape(str(value))


def df_to_markdown(df: pd.DataFrame) -> str:
    """The whole DataFrame as a markdown table; callers pass only the rows they want shown."""
    columns = [str(column) for column in df.columns]
    lines = [
        "| " + " | ".join(_escape(column) for column in columns) + " |",
        "|" + "---|" * len(columns),
    ]
    for row in df.itertuples(index=False, name=None):
        cells = [format_cell(value, column) for value, column in zip(row, columns, strict=True)]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def render_result(
    df: pd.DataFrame, total_rows: int, store: ExportStore, output: OutputConfig
) -> RenderedResult:
    """Save the rows as CSV, then show them all, or a short preview when the result is big."""
    csv_file = store.save_csv(df)
    url = store.url_for(csv_file)
    download = f"[{DOWNLOAD_ICON} Download CSV ({_count(total_rows, 'row')})]({url})"
    column_count = len(df.columns)
    if (
        total_rows <= output.inline_table_max_rows
        and column_count <= output.inline_table_max_columns
    ):
        markdown = f"{df_to_markdown(df)}\n\n{download}"
        return RenderedResult(markdown=markdown, inline=True, csv_file=csv_file)

    preview = df.head(output.preview_rows)
    columns = _count(column_count, "column")
    if len(preview) < total_rows:
        note = f"Showing the first {len(preview):,} of {_count(total_rows, 'row')} ({columns})."
    else:  # every row fits in the preview, the result is just too wide to count as inline
        note = f"The result has {_count(total_rows, 'row')} and {columns}."
    markdown = f"{note}\n\n{df_to_markdown(preview)}\n\n{download}"
    return RenderedResult(markdown=markdown, inline=False, csv_file=csv_file)


def render_too_large(total_rows: int, max_rows: int) -> str:
    return (
        f"This result has {total_rows:,} rows, which is more than the {max_rows:,} I can return. "
        "Try adding a date range or a filter, or ask for a summary (totals by month, top 10, …)."
    )


def render_sql_block(sql: str) -> str:
    """A collapsed "Show SQL" section. Follow-up questions find the previous SQL in it."""
    return f"<details><summary>Show SQL</summary>\n\n```sql\n{sql.strip()}\n```\n\n</details>"


def render_assumptions(assumptions: list[str]) -> str:
    """One italic line, e.g. "_Assumed: last 30 days; paid orders only._", or "" for none."""
    # Joined into one sentence, so line breaks and each item's own final period are dropped.
    items = [" ".join(item.split()).rstrip(". ") for item in assumptions]
    items = [item for item in items if item]
    if not items:
        return ""
    return f"_Assumed: {'; '.join(items)}._"


def _is_missing(value: object) -> bool:
    """None, NaN, NaT and pd.NA. Lists and arrays (REPEATED columns) are never missing."""
    return pd.api.types.is_scalar(value) and bool(pd.isna(value))


def _looks_like_label(column_name: str) -> bool:
    # Words from snake_case and camelCase names: "CustomerID" -> ["Customer", "ID"].
    words = re.findall(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+", column_name)
    if not words:
        return False
    last = words[-1].lower()
    return last.endswith(("id", "code")) or last in _LABEL_WORDS


def _format_fraction(value: numbers.Real | Decimal) -> str:
    """Thousands separators and at most 2 decimals: 1,234.57, 0.5, 12."""
    text = f"{value:,.2f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text == "-0" else text  # -0.001 rounds to "-0.00"


def _escape(text: str) -> str:
    """Keep text inside its table cell: escape backslashes and pipes, flatten line breaks."""
    # Backslashes first, so a "\|" already in the data can't end the cell.
    text = text.replace("\\", "\\\\").replace("|", "\\|")
    return re.sub(r"[\r\n]+", " ", text)


def _count(number: int, noun: str) -> str:
    """'1 row', '1,234 rows'."""
    return f"{number:,} {noun}" if number == 1 else f"{number:,} {noun}s"
