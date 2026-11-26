import re
from datetime import UTC, date, datetime
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest
from db_dtypes import DateDtype

from backend.agent.history import last_sql
from backend.config import OutputConfig
from backend.services.exports import ExportStore
from backend.services.renderer import (
    df_to_markdown,
    format_cell,
    render_assumptions,
    render_result,
    render_sql_block,
    render_too_large,
)

BASE_URL = "https://agent.example.com"


@pytest.fixture
def store(tmp_path):
    return ExportStore(tmp_path / "exports", public_base_url=BASE_URL)


def numbers_frame(rows, columns=2):
    """rows x columns of small integers, in columns named col_0, col_1, ..."""
    return pd.DataFrame({f"col_{i}": range(rows) for i in range(columns)})


def table_rows(markdown):
    """The data rows of the markdown table, without the header and separator lines."""
    return [line for line in markdown.splitlines() if line.startswith("|")][2:]


def saved_rows(store, file_name):
    return len(pd.read_csv(store.path_for(file_name), encoding="utf-8-sig"))


@pytest.mark.parametrize(
    ("value", "column", "expected"),
    [
        (1234567, "revenue", "1,234,567"),
        (np.int64(1234567), "orders", "1,234,567"),
        (-1234, "change", "-1,234"),
        (999, "orders", "999"),
    ],
)
def test_numbers_get_thousands_separators(value, column, expected):
    assert format_cell(value, column) == expected


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("id", 12345),
        ("customer_id", 12345),
        ("userId", 12345),
        ("CustomerID", 12345),
        ("userid", 12345),
        ("year", 2024),
        ("order_year", np.int64(2024)),
        ("code", 4401),
        ("country_code", 4401),
        ("zip", 110001),
        ("zipcode", 110001),
        ("pin", 560001),
        ("pincode", 560001),
        ("phone", 9876543210),
    ],
)
def test_ids_years_and_codes_are_left_alone(column, value):
    assert format_cell(value, column) == str(value)


@pytest.mark.parametrize("column", ["amount_paid", "totalPaid", "max_bid", "rows_valid"])
def test_words_that_merely_end_in_id_are_still_amounts(column):
    assert format_cell(1234567, column) == "1,234,567"


@pytest.mark.parametrize(
    "column",
    ["phone_number", "mobile", "mobile_no", "order_no", "OrderNo", "invoice_num", "phoneNumber"],
)
def test_phone_and_order_numbers_get_no_separators(column):
    assert format_cell(9876543210, column) == "9876543210"


def test_number_words_count_only_as_the_last_word():
    assert format_cell(1234567, "number_of_orders") == "1,234,567"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (1234.567, "1,234.57"),
        (-1234.567, "-1,234.57"),
        (12.0, "12"),
        (1.004, "1"),
        (np.float32(1.25), "1.25"),
        (Decimal("1234567.891"), "1,234,567.89"),  # BigQuery NUMERIC
    ],
)
def test_numbers_from_one_up_get_at_most_two_decimals(value, expected):
    assert format_cell(value, "amount") == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0.0042, "0.0042"),
        (0.01374, "0.0137"),
        (0.5, "0.5"),
        (-0.001, "-0.001"),
        (0.0000001234, "0.000000123"),  # written out, not as 1.23e-07
        (0.9996, "1"),
        (np.float32(0.0042), "0.0042"),
        (Decimal("0.000123456"), "0.000123"),
    ],
)
def test_numbers_below_one_keep_three_significant_digits(value, expected):
    assert format_cell(value, "conversion_rate") == expected


@pytest.mark.parametrize("value", [0, 0.0, -0.0, Decimal("0.000"), Decimal("-0")])
def test_zero_is_0(value):
    assert format_cell(value, "amount") == "0"


def test_dates_are_iso():
    assert format_cell(date(2024, 1, 31), "order_date") == "2024-01-31"


@pytest.mark.parametrize(
    "value",
    [
        datetime(2024, 1, 31, 9, 30, 15, 123456),
        pd.Timestamp("2024-01-31 09:30:15.123456789", tz="UTC"),  # BigQuery TIMESTAMP
        np.datetime64("2024-01-31T09:30:15"),
    ],
)
def test_timestamps_are_shown_to_the_second(value):
    assert format_cell(value, "created_at") == "2024-01-31 09:30:15"


@pytest.mark.parametrize(
    ("value", "timezone", "expected"),
    [
        (pd.Timestamp("2024-01-31 09:30:15", tz="UTC"), "Asia/Kolkata", "2024-01-31 15:00:15"),
        (datetime(2024, 1, 31, 20, 0, tzinfo=UTC), "Asia/Kolkata", "2024-02-01 01:30:00"),
        (pd.Timestamp("2024-01-15 12:00", tz="UTC"), "America/New_York", "2024-01-15 07:00:00"),
        (pd.Timestamp("2024-07-15 12:00", tz="UTC"), "America/New_York", "2024-07-15 08:00:00"),
        (pd.Timestamp("2024-01-31 15:00:15", tz="Asia/Kolkata"), "UTC", "2024-01-31 09:30:15"),
    ],
)
def test_timestamps_with_a_zone_are_shown_in_the_configured_timezone(value, timezone, expected):
    assert format_cell(value, "created_at", timezone=timezone) == expected


@pytest.mark.parametrize(
    "value", [datetime(2024, 1, 31, 9, 30, 15), np.datetime64("2024-01-31T09:30:15")]
)
def test_timestamps_without_a_zone_are_not_converted(value):  # BigQuery DATETIME
    assert format_cell(value, "created_at", timezone="Asia/Kolkata") == "2024-01-31 09:30:15"


@pytest.mark.parametrize(
    "value",
    [
        datetime(9999, 12, 31, 23, 59, 59, 999999, tzinfo=UTC),
        pd.Timestamp(datetime(9999, 12, 31, 23, 59, 59, 999999, tzinfo=UTC)),
    ],
)
def test_the_end_of_time_is_not_pushed_past_year_9999(value):
    # History tables use this for "no end date"; in Kolkata it would be in the year 10000.
    assert format_cell(value, "valid_to", timezone="Asia/Kolkata") == "9999-12-31 23:59:59"


@pytest.mark.parametrize("value", [None, float("nan"), pd.NA, pd.NaT, Decimal("NaN")])
def test_missing_values_are_empty(value):
    assert format_cell(value, "revenue") == ""


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (r"Pune | West", r"Pune \| West"),
        (r"a\|b", r"a\\\|b"),  # a backslash in the data can't cancel the pipe's escape
        ("New\nDelhi", "New Delhi"),
        ("two\r\n\r\nlines", "two lines"),
    ],
)
def test_pipes_and_line_breaks_are_escaped(text, expected):
    assert format_cell(text, "city") == expected


def test_markdown_table_has_a_header_a_separator_and_one_row_per_record():
    df = pd.DataFrame({"city": ["Pune", "Delhi"], "orders": [1500, 20], "revenue": [1234.5, None]})

    assert df_to_markdown(df).splitlines() == [
        "| city | orders | revenue |",
        "|---|---|---|",
        "| Pune | 1,500 | 1,234.5 |",
        "| Delhi | 20 |  |",
    ]


def test_bigquery_column_types_are_formatted():
    df = pd.DataFrame(
        {
            "customer_id": pd.array([101, None], dtype="Int64"),
            "orders": pd.array([1500, None], dtype="Int64"),
            "revenue": [Decimal("1234567.891"), None],
            "day": pd.Series([date(2024, 1, 31), None], dtype=DateDtype()),
            "paid_at": pd.Series([pd.Timestamp("2024-01-31 09:30:15.5", tz="UTC"), pd.NaT]),
        }
    )

    assert table_rows(df_to_markdown(df)) == [
        "| 101 | 1,500 | 1,234,567.89 | 2024-01-31 | 2024-01-31 09:30:15 |",
        "|  |  |  |  |  |",
    ]


def test_small_result_is_shown_in_full(store):
    df = pd.DataFrame({"city": ["Pune", "Delhi"], "orders": [1500, 20]})

    result = render_result(df, total_rows=2, store=store, output=OutputConfig())

    assert result.inline is True
    assert result.markdown == (
        "| city | orders |\n"
        "|---|---|\n"
        "| Pune | 1,500 |\n"
        "| Delhi | 20 |\n"
        "\n"
        f"[⬇️ Download CSV (2 rows)]({BASE_URL}/downloads/{result.csv_file})"
    )
    assert saved_rows(store, result.csv_file) == 2


def test_chat_shows_local_time_but_the_csv_keeps_the_raw_value(store):
    df = pd.DataFrame({"paid_at": [pd.Timestamp("2024-01-31 09:30:15", tz="UTC")]})

    result = render_result(df, 1, store, OutputConfig(), timezone="Asia/Kolkata")

    assert table_rows(result.markdown) == ["| 2024-01-31 15:00:15 |"]
    csv_text = store.path_for(result.csv_file).read_text(encoding="utf-8-sig")
    assert csv_text.splitlines() == ["paid_at", "2024-01-31 09:30:15+00:00"]


def test_preview_also_shows_local_time(store):
    paid_at = pd.Series(pd.date_range("2024-01-31 09:30", periods=30, freq="h", tz="UTC"))

    result = render_result(
        pd.DataFrame({"paid_at": paid_at}), 30, store, OutputConfig(), "Asia/Kolkata"
    )

    assert result.inline is False
    assert table_rows(result.markdown)[0] == "| 2024-01-31 15:00:00 |"


def test_20_rows_and_12_columns_still_fit_inline(store):
    result = render_result(numbers_frame(20, 12), 20, store, OutputConfig())

    assert result.inline is True
    assert len(table_rows(result.markdown)) == 20


@pytest.mark.parametrize(
    ("rows", "columns", "note"),
    [
        (1234, 2, "Showing the first 5 of 1,234 rows (2 columns)."),
        (21, 12, "Showing the first 5 of 21 rows (12 columns)."),
        (8, 13, "Showing the first 5 of 8 rows (13 columns)."),
        (3, 13, "The result has 3 rows and 13 columns."),
    ],
)
def test_more_than_20_rows_or_12_columns_shows_a_preview(store, rows, columns, note):
    result = render_result(numbers_frame(rows, columns), rows, store, OutputConfig())

    assert result.inline is False
    assert result.markdown.startswith(f"{note}\n\n| col_0 |")
    assert len(table_rows(result.markdown)) == min(rows, 5)
    assert saved_rows(store, result.csv_file) == rows  # the CSV always has every row


@pytest.mark.parametrize("base_url", [BASE_URL, ""])
@pytest.mark.parametrize(("rows", "label"), [(1234, "1,234 rows"), (1, "1 row")])
def test_download_line_links_to_the_saved_csv(tmp_path, base_url, rows, label):
    store = ExportStore(tmp_path, public_base_url=base_url)

    result = render_result(numbers_frame(rows), rows, store, OutputConfig(public_base_url=base_url))

    assert re.fullmatch(r"[0-9a-f]{32}\.csv", result.csv_file)
    assert store.path_for(result.csv_file) is not None
    assert result.markdown.endswith(
        f"\n\n[⬇️ Download CSV ({label})]({base_url}/downloads/{result.csv_file})"
    )


def test_too_large_message_gives_both_counts():
    message = render_too_large(1_234_567, 50_000)

    assert "1,234,567 rows" in message
    assert "50,000" in message


def test_sql_block_layout():
    block = render_sql_block("\n  SELECT city\nFROM `p.sales.orders`  \n")

    assert block == (
        "<details><summary>Show SQL</summary>\n"
        "\n"
        "```sql\n"
        "SELECT city\n"
        "FROM `p.sales.orders`\n"
        "```\n"
        "\n"
        "</details>"
    )


def test_follow_up_questions_find_the_sql_in_the_block():
    sql = "SELECT city, COUNT(*) AS orders\nFROM `p.sales.orders`\nGROUP BY city"
    answer = f"Pune has the most orders.\n\n{render_sql_block(sql)}"

    assert last_sql([{"role": "assistant", "content": answer}]) == sql


def test_no_assumptions_means_no_line():
    assert render_assumptions([]) == ""
    assert render_assumptions(["", "  "]) == ""


def test_assumptions_are_one_italic_line():
    line = render_assumptions(["last 30 days", "paid orders only."])

    assert line == "_Assumed: last 30 days; paid orders only._"
    assert render_assumptions(["prices\nin INR"]) == "_Assumed: prices in INR._"
