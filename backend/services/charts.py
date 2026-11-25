"""Charts from a ChartSpec: the fast model says what to draw, fixed code here draws it.

Model output is never executed. ``check_spec`` compares a spec with the data and explains any
problem in plain words, so the model can fix its spec; ``render_chart`` draws the PNG.

Charts use matplotlib's object API (Figure + FigureCanvasAgg) and never pyplot: the API draws
charts for several requests at once, and pyplot's global state isn't thread-safe.
"""

from __future__ import annotations

import io
from collections.abc import Callable
from datetime import date
from typing import Literal

import numpy as np
import pandas as pd
import seaborn as sns
from matplotlib.axes import Axes
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure
from matplotlib.ticker import FuncFormatter, MaxNLocator
from pydantic import BaseModel

FIGURE_SIZE = (10, 6)  # inches: 1500 x 900 pixels at 150 dpi
DPI = 150
MAX_BARS = 30
PIE_SLICES = 8
MAX_ANNOTATED_CELLS = 100  # bigger heatmaps get no numbers written in their cells

CHART_NAMES = {
    "bar": "bar chart",
    "line": "line chart",
    "scatter": "scatter plot",
    "hist": "histogram",
    "box": "box plot",
    "heatmap": "heatmap",
    "pie": "pie chart",
}

# The theme changes matplotlib's global settings, so it is set once here, never per chart.
# parse_math is off because matplotlib reads text between two "$" signs as math: a title like
# "Revenue ($) vs cost ($)" came out garbled, and a label like "$5 #1 deal $" didn't draw at all.
sns.set_theme(style="whitegrid", rc={"text.parse_math": False})


class ChartSpec(BaseModel):
    """What to draw, written by the fast model. Column names refer to the query result."""

    chart_type: Literal["bar", "line", "scatter", "hist", "box", "heatmap", "pie"]
    x_col: str
    y_col: str | None = None
    hue_col: str | None = None
    value_col: str | None = None  # heatmap cell values
    title: str
    x_label: str | None = None
    y_label: str | None = None
    horizontal: bool = False  # bar charts only
    top_n: int | None = None  # bar/pie: keep the top N; pie groups the rest as "Other"
    sort: Literal["none", "asc", "desc"] = "none"


def check_spec(spec: ChartSpec, df: pd.DataFrame) -> list[str]:
    """Problems that would stop the chart from drawing; an empty list means it's fine."""
    if df.empty:
        return ["The result has no rows to chart."]

    problems = []
    for column in (spec.x_col, spec.y_col, spec.hue_col, spec.value_col):
        if column is not None and column not in df.columns:
            available = ", ".join(str(name) for name in df.columns)
            problems.append(f"Column {column!r} is not in the result (columns: {available}).")

    name = CHART_NAMES[spec.chart_type]
    if spec.chart_type in ("bar", "line", "scatter", "box", "pie") and not spec.y_col:
        problems.append(f"A {name} needs y_col, the column with the numbers.")
    if spec.chart_type == "heatmap" and not (spec.y_col and spec.value_col):
        problems.append("A heatmap needs y_col (the rows) and value_col (the number in each cell).")

    number_col = _number_column(spec)
    if number_col is not None and number_col in df.columns:
        values = _as_numbers(df[number_col])
        if values is None:
            problems.append(f"Column {number_col!r} must hold numbers for a {name}.")
        elif values.isna().all():
            problems.append(f"Column {number_col!r} has no values to plot.")
        elif spec.chart_type == "pie" and (values < 0).any():
            problems.append("A pie chart can't show negative values; use a bar chart instead.")
        elif spec.chart_type == "pie" and values.sum() == 0:
            problems.append(f"The values in {number_col!r} add up to zero, so there's no pie.")

    if spec.top_n is not None and spec.top_n < 1:
        problems.append("top_n must be 1 or more.")
    return problems


def render_chart(df: pd.DataFrame, spec: ChartSpec) -> bytes:
    """Draw the chart as PNG bytes. Raises ValueError when check_spec finds problems."""
    problems = check_spec(spec, df)
    if problems:
        raise ValueError("Can't draw this chart: " + " ".join(problems))

    figure = Figure(figsize=FIGURE_SIZE, dpi=DPI, facecolor="white", layout="constrained")
    FigureCanvasAgg(figure)  # attaches itself to the figure
    ax = figure.add_subplot()
    _DRAWERS[spec.chart_type](ax, _prepare(df, spec), spec)
    if spec.chart_type != "pie":
        _finish_axes(ax, spec)
    ax.set_title(spec.title)

    buffer = io.BytesIO()
    figure.savefig(buffer, format="png", dpi=DPI, facecolor="white")
    return buffer.getvalue()


def _draw_bar(ax: Axes, data: pd.DataFrame, spec: ChartSpec) -> None:
    order = _bar_order(data, spec)
    data = data[data[spec.x_col].isin(order)]
    # Horizontal bars list the categories down the side, so the columns swap axes.
    x, y = (spec.y_col, spec.x_col) if spec.horizontal else (spec.x_col, spec.y_col)
    sns.barplot(
        data=data,
        x=x,
        y=y,
        hue=spec.hue_col,
        order=order,
        orient="y" if spec.horizontal else "x",
        estimator="sum",  # rows with the same category add up instead of being averaged
        errorbar=None,
        ax=ax,
    )
    if not spec.horizontal:
        _tilt_crowded_labels(ax)


def _bar_order(data: pd.DataFrame, spec: ChartSpec) -> list:
    """The categories to draw, in order: the top N by total (never more than 30)."""
    totals = data.groupby(spec.x_col, sort=False)[spec.y_col].sum()
    limit = min(spec.top_n or MAX_BARS, MAX_BARS)
    if len(totals) > limit:
        totals = totals[totals.index.isin(totals.nlargest(limit).index)]
    if spec.sort != "none":
        totals = totals.sort_values(ascending=spec.sort == "asc", kind="stable")
    return list(totals.index)


def _draw_line(ax: Axes, data: pd.DataFrame, spec: ChartSpec) -> None:
    x = spec.x_col
    if _is_ordered(data[x]):  # text labels keep the order the query returned
        data = data.sort_values(x, kind="stable")
    # Dots keep short series visible; a single point would otherwise draw nothing at all.
    marker = "o" if data[x].nunique() <= 50 else None
    sns.lineplot(
        data=data,
        x=x,
        y=spec.y_col,
        hue=spec.hue_col,
        estimator="sum",  # like bars: rows with the same x add up
        errorbar=None,
        marker=marker,
        ax=ax,
    )
    if pd.api.types.is_integer_dtype(data[x]):
        ax.xaxis.set_major_locator(MaxNLocator(integer=True))  # years and hours, never 2023.5


def _draw_scatter(ax: Axes, data: pd.DataFrame, spec: ChartSpec) -> None:
    sns.scatterplot(data=data, x=spec.x_col, y=spec.y_col, hue=spec.hue_col, ax=ax)


def _draw_hist(ax: Axes, data: pd.DataFrame, spec: ChartSpec) -> None:
    if spec.y_col:
        # The counts were worked out in SQL, so weight each x by them. seaborn can't choose
        # bins by itself with weights: use one bin per distinct value, up to 50.
        bins = max(1, min(data[spec.x_col].nunique(), 50))
        sns.histplot(
            data=data, x=spec.x_col, weights=spec.y_col, hue=spec.hue_col, bins=bins, ax=ax
        )
        ax.set_ylabel(spec.y_col)
    else:
        sns.histplot(data=data, x=spec.x_col, hue=spec.hue_col, ax=ax)


def _draw_box(ax: Axes, data: pd.DataFrame, spec: ChartSpec) -> None:
    sns.boxplot(data=data, x=spec.x_col, y=spec.y_col, hue=spec.hue_col, ax=ax)
    _tilt_crowded_labels(ax)


def _draw_heatmap(ax: Axes, data: pd.DataFrame, spec: ChartSpec) -> None:
    table = _heatmap_table(data, spec)
    cells = table.to_numpy(dtype=float)
    whole_numbers = np.array_equal(cells, np.round(cells), equal_nan=True)
    sns.heatmap(
        table,
        cmap="Blues",
        annot=table.size <= MAX_ANNOTATED_CELLS,
        fmt=",.0f" if whole_numbers else ",.2f",
        cbar_kws={"format": FuncFormatter(_tick_label)},
        ax=ax,
    )
    ax.grid(False)


def _heatmap_table(data: pd.DataFrame, spec: ChartSpec) -> pd.DataFrame:
    """value_col summed for each (y, x) pair: y values down the side, x values along the bottom."""
    table = data.pivot_table(
        index=spec.y_col, columns=spec.x_col, values=spec.value_col, aggfunc="sum", sort=False
    )
    # Numbers and dates read best sorted; text labels keep the order the query returned.
    if _is_ordered(table.index):
        table = table.sort_index()
    if _is_ordered(table.columns):
        table = table.sort_index(axis=1)
    return table


def _draw_pie(ax: Axes, data: pd.DataFrame, spec: ChartSpec) -> None:
    slices = _pie_slices(data, spec)
    ax.pie(
        slices.to_numpy(),
        labels=[str(label) for label in slices.index],
        autopct=lambda percent: f"{percent:.0f}%" if percent >= 3 else "",  # none on slivers
        startangle=90,
        counterclock=False,
        colors=sns.color_palette(n_colors=len(slices)),
    )


def _pie_slices(data: pd.DataFrame, spec: ChartSpec) -> pd.Series:
    """Totals per category, largest first. Past the top N (8 by default) the rest is "Other"."""
    totals = data.groupby(spec.x_col)[spec.y_col].sum().sort_values(ascending=False, kind="stable")
    keep = spec.top_n or PIE_SLICES
    if len(totals) <= keep:
        return totals
    # A category the data already calls "Other" joins the grouped slice, so only one has that name.
    is_other = np.array([str(label) == "Other" for label in totals.index], dtype=bool)
    top = totals[~is_other].iloc[:keep]
    other = pd.Series([totals.drop(top.index).sum()], index=["Other"])
    return pd.concat([top, other])


_DRAWERS: dict[str, Callable[[Axes, pd.DataFrame, ChartSpec], None]] = {
    "bar": _draw_bar,
    "line": _draw_line,
    "scatter": _draw_scatter,
    "hist": _draw_hist,
    "box": _draw_box,
    "heatmap": _draw_heatmap,
    "pie": _draw_pie,
}


def _number_column(spec: ChartSpec) -> str | None:
    """The column that must hold numbers: the cell values for heatmaps, y_col otherwise."""
    return spec.value_col if spec.chart_type == "heatmap" else spec.y_col


def _prepare(df: pd.DataFrame, spec: ChartSpec) -> pd.DataFrame:
    """A copy of the columns the chart uses, converted to types seaborn handles well."""
    used = [spec.x_col, spec.y_col, spec.hue_col, spec.value_col]
    data = df[list(dict.fromkeys(column for column in used if column))].copy()
    number_col = _number_column(spec)
    if number_col:
        data[number_col] = _as_numbers(data[number_col])
    if spec.chart_type in ("line", "scatter", "hist"):
        data[spec.x_col] = _continuous(data[spec.x_col])
    return data


def _as_numbers(column: pd.Series) -> pd.Series | None:
    """The column as floats, or None if it doesn't hold numbers.

    BigQuery NUMERIC values arrive as Decimal objects and nullable integers carry pd.NA;
    plain floats avoid surprises in seaborn and matplotlib.
    """
    if pd.api.types.is_numeric_dtype(column):
        return column.astype(float)
    if column.dtype == object:
        try:
            return pd.to_numeric(column).astype(float)
        except (TypeError, ValueError):
            return None
    return None


def _continuous(column: pd.Series) -> pd.Series:
    """Make dates and Decimals plot on a continuous axis instead of as text labels.

    BigQuery DATE values arrive as datetime.date objects, which seaborn treats as text.
    """
    if not pd.api.types.is_datetime64_any_dtype(column):
        values = column.dropna()
        if not values.empty and all(isinstance(value, date) for value in values):
            return pd.to_datetime(column)
    if column.dtype == object:
        numbers = _as_numbers(column)
        if numbers is not None:
            return numbers
    return column


def _is_ordered(values: pd.Series | pd.Index) -> bool:
    return pd.api.types.is_numeric_dtype(values) or pd.api.types.is_datetime64_any_dtype(values)


def _finish_axes(ax: Axes, spec: ChartSpec) -> None:
    """The spec's axis labels (seaborn's column names stay otherwise) and readable numbers."""
    x_axis, y_axis = ax.xaxis, ax.yaxis
    if spec.chart_type == "bar" and spec.horizontal:
        x_axis, y_axis = y_axis, x_axis  # the x_col categories run down the side
    if spec.chart_type != "heatmap":  # a heatmap's numbers are on its colour bar
        # 1,500,000 instead of matplotlib's 1.5 with a "1e6" note
        y_axis.set_major_formatter(FuncFormatter(_tick_label))
    if spec.x_label:
        x_axis.set_label_text(spec.x_label)
    if spec.y_label:
        y_axis.set_label_text(spec.y_label)


def _tilt_crowded_labels(ax: Axes) -> None:
    """Slant category labels that would overlap if written side by side."""
    labels = ax.get_xticklabels()
    if sum(len(label.get_text()) for label in labels) > 60:
        for label in labels:
            label.set_rotation(30)
            label.set_horizontalalignment("right")


def _tick_label(value: float, _position: int | None = None) -> str:
    value += 0.0  # turns -0.0 into 0.0
    if abs(value) >= 1000:
        return f"{value:,.0f}"
    return f"{value:g}"
