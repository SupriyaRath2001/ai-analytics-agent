from backend.agent import prompts
from backend.bootstrap.curator import MAX_PROMPT_COLUMNS
from backend.bootstrap.kb_cache import ColumnInfo, TableDetail, TableInfo


def table_info(name, summary="", row_count=1200):
    return TableInfo(
        connection="warehouse",
        qualified_name=f"my-project.web.{name}",
        schema_name="web",
        table_name=name,
        table_type="TABLE",
        description="",
        summary=summary,
        tags=[],
        row_count=row_count,
        size_bytes=None,
        column_count=0,
        curated=bool(summary),
    )


def column(name, data_type="STRING", mode=None, description="", is_partition=False):
    return ColumnInfo(
        name=name,
        data_type=data_type,
        mode=mode,
        description=description,
        meaning="",
        sample_values=[],
        is_partition=is_partition,
        clustering_order=None,
    )


def table_detail(columns, row_count=1200):
    info = table_info("events", row_count=row_count)
    return TableDetail(**info.model_dump(), columns=columns)


def test_a_small_catalog_lists_every_table():
    tables = [table_info("events", "One row per page view."), table_info("users")]

    assert prompts.catalog(tables) == (
        "- my-project.web.events — One row per page view.\n"
        "- my-project.web.users — (no summary yet)"
    )


def test_a_huge_catalog_stops_at_the_character_budget():
    summary = "One row per event, with the page, the device and the campaign. " * 2
    tables = [table_info(f"t{i:04}", summary) for i in range(2000)]

    lines = prompts.catalog(tables).splitlines()

    listed, note = lines[:-1], lines[-1]
    assert len("\n".join(listed)) <= prompts.MAX_CATALOG_CHARS
    assert len(listed) > 100
    assert all(line.endswith("campaign.") for line in listed)  # no line is cut halfway
    assert note == f"(… {2000 - len(listed):,} more tables not listed)"


def test_the_pick_and_metadata_prompts_use_the_capped_catalog():
    tables = [table_info(f"t{i:04}", "x" * 200) for i in range(1000)]

    pick = prompts.pick_messages(tables, "", [], "Revenue?")[-1]["content"]
    metadata = prompts.metadata_messages([], tables, "", [], "Which tables?")[-1]["content"]

    for prompt in (pick, metadata):
        assert "more tables not listed)" in prompt
        assert len(prompt) < prompts.MAX_CATALOG_CHARS + 1000


def test_table_details_show_the_columns_the_curator_described():
    columns = [column(f"col_{i}", "INT64") for i in range(MAX_PROMPT_COLUMNS + 10)]
    # Ingestion-time partitioning: the connector adds this pseudo column last.
    columns.append(column("_PARTITIONTIME", "TIMESTAMP", is_partition=True))

    text = prompts.table_details([table_detail(columns)])

    assert f"- col_{MAX_PROMPT_COLUMNS - 1} (INT64)" in text
    assert f"- col_{MAX_PROMPT_COLUMNS} (INT64)" not in text
    # It decides what a query costs, so it stays past the cap.
    assert "- _PARTITIONTIME (TIMESTAMP, partition column)" in text
    assert text.endswith("- … and 10 more columns")


def test_fields_inside_repeated_fields_say_to_unnest():
    columns = [
        column("items", "RECORD", mode="REPEATED"),
        column("items.sku", "STRING"),
        column("items.price", "NUMERIC", description="Unit price"),
        column("items.options", "RECORD", mode="REPEATED"),
        column("items.options.name", "STRING"),
        column("device", "RECORD"),
        column("device.category", "STRING"),
        column("tags", "STRING", mode="REPEATED"),
    ]

    lines = prompts.table_details([table_detail(columns)]).splitlines()

    assert lines[1] == "Columns:"
    assert lines[2:] == [
        "- items (RECORD, REPEATED)",
        "- items.sku (STRING) — inside repeated items, use UNNEST(items)",
        "- items.price (NUMERIC): Unit price — inside repeated items, use UNNEST(items)",
        "- items.options (RECORD, REPEATED) — inside repeated items, use UNNEST(items)",
        "- items.options.name (STRING) — inside repeated items and items.options, "
        "UNNEST each of them, outermost first",
        "- device (RECORD)",
        "- device.category (STRING)",
        "- tags (STRING, REPEATED)",
    ]


def test_row_counts_are_marked_as_read_at_startup():
    with_count = prompts.table_details([table_detail([column("id")])])
    without_count = prompts.table_details([table_detail([column("id")], row_count=None)])

    assert with_count.startswith(
        "`my-project.web.events` (table), about 1,200 rows (when the agent started)\n"
    )
    assert without_count.startswith("`my-project.web.events` (table)\n")
