import struct
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal
from functools import partial

import pandas as pd
import pytest
from db_dtypes import DateDtype

from backend.services.charts import ChartSpec, _bar_order, _pie_slices, check_spec, render_chart

# seaborn 0.13 passes vert= to Axes.bxp, which matplotlib 3.11 deprecates. Not ours to fix.
pytestmark = pytest.mark.filterwarnings("ignore:vert. bool was deprecated:DeprecationWarning")

# Shaped like a BigQuery result: DATE arrives as the "dbdate" dtype, NUMERIC as Decimal objects.
SALES = pd.DataFrame(
    {
        "city": ["Pune", "Delhi", "Mumbai", "Pune", "Delhi", "Mumbai"],
        "month": pd.Series([date(2024, 1, 1)] * 3 + [date(2024, 2, 1)] * 3, dtype=DateDtype()),
        "orders": [10, 20, 30, 15, 25, 35],
        "revenue": [Decimal(n) for n in ["1200.50", "2400", "3600", "1800", "3000", "4200"]],
    }
)

SPECS = {
    "bar": ChartSpec(chart_type="bar", x_col="city", y_col="orders", title="Orders by city"),
    "horizontal bar": ChartSpec(
        chart_type="bar",
        x_col="city",
        y_col="revenue",
        title="Revenue by city",
        horizontal=True,
        sort="desc",
    ),
    "line": ChartSpec(
        chart_type="line", x_col="month", y_col="orders", hue_col="city", title="Orders by month"
    ),
    "scatter": ChartSpec(chart_type="scatter", x_col="orders", y_col="revenue", title="Revenue"),
    "hist": ChartSpec(chart_type="hist", x_col="orders", title="Orders per row"),
    "box": ChartSpec(chart_type="box", x_col="city", y_col="orders", title="Orders by city"),
    "heatmap": ChartSpec(
        chart_type="heatmap", x_col="month", y_col="city", value_col="orders", title="Orders"
    ),
    "pie": ChartSpec(chart_type="pie", x_col="city", y_col="orders", title="Share of orders"),
}

# Visits per traffic source, in no particular order. 780 in total.
VISITS = {
    "Print": 20,
    "Google": 120,
    "Email": 100,
    "Radio": 10,
    "Direct": 110,
    "Ads": 90,
    "Social": 80,
    "Podcast": 30,
    "Bing": 70,
    "Referral": 60,
    "Partner": 50,
    "Yahoo": 40,
}
SOURCES = pd.DataFrame({"source": list(VISITS), "visits": list(VISITS.values())})
PIE = ChartSpec(chart_type="pie", x_col="source", y_col="visits", title="Visits by source")


def png_size(png):
    """(width, height) from the PNG header."""
    return struct.unpack(">II", png[16:24])


@pytest.mark.parametrize("spec", SPECS.values(), ids=SPECS.keys())
def test_every_chart_type_renders_a_png(spec):
    png = render_chart(SALES, spec)

    assert png.startswith(b"\x89PNG")
    assert png_size(png) == (1500, 900)  # 10 x 6 inches at 150 dpi


def test_valid_spec_has_no_problems():
    assert check_spec(SPECS["horizontal bar"], SALES) == []  # Decimal values count as numbers


def test_missing_column_is_named_with_the_available_ones():
    spec = ChartSpec(chart_type="bar", x_col="country", y_col="orders", title="Orders")

    [problem] = check_spec(spec, SALES)

    assert "'country' is not in the result" in problem
    assert "(columns: city, month, orders, revenue)" in problem


@pytest.mark.parametrize("chart_type", ["bar", "line", "scatter", "box", "pie"])
def test_charts_of_numbers_need_y_col(chart_type):
    spec = ChartSpec(chart_type=chart_type, x_col="city", title="Orders")

    [problem] = check_spec(spec, SALES)

    assert "needs y_col" in problem


@pytest.mark.parametrize("left_out", ["y_col", "value_col"])
def test_heatmap_needs_y_col_and_value_col(left_out):
    columns = {"y_col": "city", "value_col": "orders"}
    del columns[left_out]
    spec = ChartSpec(chart_type="heatmap", x_col="month", title="Orders", **columns)

    assert check_spec(spec, SALES) == [
        "A heatmap needs y_col (the rows) and value_col (the number in each cell)."
    ]


@pytest.mark.parametrize(
    "spec",
    [
        ChartSpec(chart_type="bar", x_col="month", y_col="city", title="Orders"),
        ChartSpec(chart_type="heatmap", x_col="month", y_col="orders", value_col="city", title="x"),
    ],
    ids=["bar y_col", "heatmap value_col"],
)
def test_plotted_values_must_be_numbers(spec):
    [problem] = check_spec(spec, SALES)

    assert problem.startswith("Column 'city' must hold numbers")


def test_render_refuses_a_spec_with_problems():
    spec = ChartSpec(chart_type="pie", x_col="city", title="Orders")

    with pytest.raises(ValueError, match="needs y_col"):
        render_chart(SALES, spec)


def test_pie_shows_the_top_8_and_groups_the_rest_as_other():
    slices = _pie_slices(SOURCES, PIE)

    top_8 = ["Google", "Direct", "Email", "Ads", "Social", "Bing", "Referral", "Partner"]
    assert list(slices.index) == [*top_8, "Other"]
    assert slices["Other"] == 40 + 30 + 20 + 10
    assert slices.sum() == 780
    assert render_chart(SOURCES, PIE).startswith(b"\x89PNG")


def test_pie_top_n_changes_how_many_slices_are_named():
    slices = _pie_slices(SOURCES, PIE.model_copy(update={"top_n": 3}))

    assert list(slices.index) == ["Google", "Direct", "Email", "Other"]
    assert slices["Other"] == 780 - 330


def test_pie_has_one_other_slice_when_the_data_has_its_own():
    data = SOURCES.replace({"source": {"Direct": "Other"}})  # now the second-largest source

    slices = _pie_slices(data, PIE)

    top_8 = ["Google", "Email", "Ads", "Social", "Bing", "Referral", "Partner", "Yahoo"]
    assert list(slices.index) == [*top_8, "Other"]
    assert slices["Other"] == 110 + 30 + 20 + 10


def test_bars_keep_the_30_largest_categories():
    data = pd.DataFrame({"product": [f"product {i:02d}" for i in range(40)], "units": range(40)})
    spec = ChartSpec(chart_type="bar", x_col="product", y_col="units", title="Units sold")

    order = _bar_order(data, spec)

    assert order == [f"product {i:02d}" for i in range(10, 40)]  # in the query's order
    assert _bar_order(data, spec.model_copy(update={"top_n": 50})) == order
    assert render_chart(data, spec).startswith(b"\x89PNG")


def test_bar_top_n_and_sort():
    data = pd.DataFrame({"product": [f"product {i:02d}" for i in range(40)], "units": range(40)})
    spec = ChartSpec(
        chart_type="bar", x_col="product", y_col="units", title="Units", top_n=3, sort="desc"
    )

    assert _bar_order(data, spec) == ["product 39", "product 38", "product 37"]


def test_bars_add_up_repeated_categories_instead_of_averaging():
    repeated = pd.DataFrame({"city": ["Pune", "Pune", "Delhi"], "orders": [10, 20, 5]})
    totals = pd.DataFrame({"city": ["Pune", "Delhi"], "orders": [30, 5]})
    spec = ChartSpec(chart_type="bar", x_col="city", y_col="orders", title="Orders")

    assert render_chart(repeated, spec) == render_chart(totals, spec)


DAYS = [date(2024, 1, day) for day in (1, 2, 3, 4)]
DATE_COLUMNS = {
    "DATE": pd.Series(DAYS, dtype=DateDtype()),
    "date objects": pd.Series(DAYS, dtype=object),
    "TIMESTAMP": pd.Series(pd.to_datetime(DAYS)).dt.tz_localize("UTC"),
}


@pytest.mark.parametrize("days", DATE_COLUMNS.values(), ids=DATE_COLUMNS.keys())
def test_line_chart_with_dates_on_the_x_axis(days):
    data = pd.DataFrame({"day": days, "orders": [5, 7, 6, 9]})
    spec = ChartSpec(chart_type="line", x_col="day", y_col="orders", title="Orders per day")

    png = render_chart(data, spec)

    assert png.startswith(b"\x89PNG")
    # The dates go on a time axis, in date order, so shuffled rows draw the same chart.
    assert render_chart(data.iloc[[2, 0, 3, 1]], spec) == png


def test_dollar_signs_in_titles_and_labels_do_not_break_the_chart():
    # Text between two "$" signs used to be read as math; the first deal made rendering fail.
    deals = ["Buy 1 get 1 at $5 #promo $", "Plan B ($19.99/mo)"]
    data = pd.DataFrame({"deal": deals, "orders": [5, 3]})

    for chart_type in ("bar", "pie"):
        spec = ChartSpec(
            chart_type=chart_type,
            x_col="deal",
            y_col="orders",
            title="Revenue ($) vs cost ($)",
            y_label="Orders ($5 deals and $10 deals)",
        )
        assert render_chart(data, spec).startswith(b"\x89PNG")


def test_charts_can_be_drawn_from_several_threads_at_once():
    expected = [render_chart(SALES, spec) for spec in SPECS.values()]

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(partial(render_chart, SALES), SPECS.values()))

    # Through pyplot's shared "current figure", threads would draw on each other's charts.
    differ = [name for name, a, b in zip(SPECS, results, expected, strict=True) if a != b]
    assert differ == []
