# Low-Level Design (LLD)

This is the inside of the `agent` container: every module in `backend/`, how a chat message
becomes an answer, what runs at start-up and what the KB cache stores. For the system around it
(Caddy, Open WebUI, Google Cloud, DeepSeek), see [HLD.md](HLD.md).

One picture with every function would be too small to read, so the design has five views:

1. [Classes: the request path](#1-classes-the-request-path): from the API to the answer.
2. [Classes: data access, KB cache and settings](#2-classes-data-access-kb-cache-and-settings):
   BigQuery, the start-up curation and the config.
3. [One chat request, step by step](#3-one-chat-request-step-by-step): the main flow.
4. [Start-up and background jobs](#4-start-up-and-background-jobs): what runs when the container starts.
5. [KB cache tables](#5-kb-cache-tables): the DuckDB file the agent keeps.

The [limits and constants](#limits-and-constants) table at the end lists every number in the code.

How to read the class diagrams: boxes marked `module` are Python files with functions only. A
solid arrow means "calls or owns", a dotted arrow "creates or returns", a hollow triangle
"inherits from" and a filled diamond "is made of". A box that says "details in diagram 1 / 2" is
shown in full in the other class diagram.

## 1. Classes: the request path

```mermaid
classDiagram
  direction LR

  class FastAPIApp {
    <<module main.py>>
    MODEL_ID: analytics-agent
    WORKER_THREADS: 100
    lifespan(app)
    require_api_key(request)
    health() GET /health
    list_models() GET /v1/models
    chat_completions(request) POST /v1/chat/completions
    get_download(file_name) GET /downloads/file
    _read_chat_request(request) messages and stream
    _sse_events(pieces) SSE chunks
    _connect(config) connectors by name
    _clean_exports(store, stop)
  }
  class AnalyticsAgent {
    <<class router.py>>
    MAX_TABLES_WITHOUT_PICK: 15
    MAX_PICKED_TABLES: 5
    connector: the first connected connector
    run(messages) Iterator~str~
    _answer(messages) Iterator~str~
    _help() str
    _no_tables_reply() str
    _pick_tables(tables, history, question) list~TableDetail~
    _metadata_answer(tables, history, question) str
    _data_answer(tables, route, messages, history, question) Iterator~str~
    _result_answer(outcome, wants_chart, question) Iterator~str~
    _takeaway(question, df) str
    _chart(df, question) str
    _chart_spec(df, question) ChartSpec
  }
  class Route {
    <<pydantic>>
    intent: metadata or data or help
    wants_chart: bool
    uses_previous_result: bool
  }
  class TablePick {
    <<pydantic>>
    tables: list~str~
  }
  class SqlWriter {
    <<module sql_writer.py>>
    MAX_RETRIES: 2
    write_and_run(llm, connector, chat) SqlOutcome
  }
  class SqlDraft {
    <<pydantic>>
    sql: str or null
    assumptions: list~str~
    clarifying_question: str or null
  }
  class SqlOutcome {
    <<dataclass>>
    sql: str
    assumptions: list~str~
    result: QueryResult or null
    clarifying_question: str
    error: str
  }
  class Prompts {
    <<module prompts.py>>
    ROUTE_SYSTEM, PICK_SYSTEM, METADATA_SYSTEM
    TAKEAWAY_SYSTEM, CHART_SYSTEM
    route_messages(tables, history, question)
    pick_messages(tables, business_context, history, question)
    metadata_messages(tables, all_tables, business_context, history, question)
    sql_messages(sql_hints, timezone, today, business_context, wants_chart, tables, previous_sql, history, question)
    sql_fix_request(error) str
    takeaway_messages(question, result_csv)
    chart_messages(question, df)
    chart_fix_request(problems) str
    table_names(tables) str
    catalog(tables) str
    table_details(tables) str
  }
  class History {
    <<module history.py>>
    latest_user_text(messages) str
    is_openwebui_task(text) bool
    clean_history(messages, max_messages=10) list~dict~
    last_sql(messages) str or null
  }
  class Replies {
    <<module replies.py>>
    NOT_CONNECTED, SCHEMA_LOADING, CATALOG_FAILED, NO_TABLES
    LLM_FAILED, UNEXPECTED_ERROR, NO_ROWS
    CHART_FAILED, CHART_OFFER
    help_text(tables) str
    which_table(tables) str
    query_failed(error) str
    rows_returned(count) str
  }
  class LLMClient {
    <<class llm.py>>
    litellm.drop_params: True
    model_for(tier) model name
    complete(messages, tier) str
    complete_json(messages, schema, tier) pydantic model
  }
  class LLMError {
    <<exception>>
  }
  class Renderer {
    <<module renderer.py>>
    render_result(df, total_rows, store, output, timezone) RenderedResult
    render_sql_block(sql) str
    render_assumptions(assumptions) str
    render_too_large(total_rows, max_rows) str
    format_cell(value, column_name, timezone) str
    df_to_markdown(df) str
  }
  class RenderedResult {
    <<pydantic>>
    markdown: str
    inline: bool
    csv_file: str
  }
  class Charts {
    <<module charts.py>>
    chart types: bar line scatter hist box heatmap pie
    check_spec(spec, df) list~str~
    render_chart(df, spec) PNG bytes
  }
  class ChartSpec {
    <<pydantic>>
    chart_type, x_col, y_col, hue_col, value_col
    title, x_label, y_label
    horizontal, top_n, sort
  }
  class ExportStore {
    <<class exports.py>>
    export_dir: exports
    ttl_hours: 24
    save_csv(df) file name
    save_png(data) file name
    url_for(file_name) str
    path_for(file_name) path or null
    cleanup() int
  }
  class KBCache {
    <<class kb_cache.py>>
    details in diagram 2
  }
  class BaseDBConnector {
    <<abstract base.py>>
    details in diagram 2
  }
  class QueryResult {
    <<pydantic>>
    details in diagram 2
  }

  FastAPIApp --> AnalyticsAgent : run(messages)
  FastAPIApp --> ExportStore : path_for, for downloads
  AnalyticsAgent --> History
  AnalyticsAgent --> Prompts
  AnalyticsAgent --> Replies
  AnalyticsAgent --> KBCache : list_tables, find_tables
  AnalyticsAgent --> LLMClient : fast tier
  AnalyticsAgent --> SqlWriter : write_and_run
  AnalyticsAgent --> Renderer
  AnalyticsAgent --> Charts
  AnalyticsAgent --> ExportStore : save_png
  AnalyticsAgent ..> Route
  AnalyticsAgent ..> TablePick
  SqlWriter --> LLMClient : smart tier
  SqlWriter --> BaseDBConnector : dry run, execute
  SqlWriter ..> SqlDraft
  SqlWriter ..> SqlOutcome
  SqlOutcome --> QueryResult
  LLMClient ..> LLMError
  Renderer --> ExportStore : save_csv
  Renderer ..> RenderedResult
  Charts ..> ChartSpec
  Prompts --> Renderer : df_to_markdown
  Replies --> Prompts : short_name, summary_line
```

## 2. Classes: data access, KB cache and settings

```mermaid
classDiagram
  direction LR

  class FastAPIApp {
    <<module main.py>>
    details in diagram 1
  }
  class Config {
    <<module config.py>>
    load_config(path) AppConfig
    _expand(text) fills in values from the environment
  }
  class AppConfig {
    <<pydantic>>
    connections: list~ConnectionConfig~
    llm: LLMConfig
    output: OutputConfig
    timezone, kb_path
    business_context_path, agent_api_key
  }
  class ConnectionConfig {
    <<pydantic>>
    name, type, enabled, project, location
    credentials_path, include_datasets
    exclude_datasets, exclude_tables
    max_bytes_billed_gb, query_timeout_sec, max_rows_fetch
  }
  class LLMConfig {
    <<pydantic>>
    fast_model, smart_model, api_key, api_base
    temperature, timeout_sec, num_retries
  }
  class OutputConfig {
    <<pydantic>>
    inline_table_max_rows, inline_table_max_columns
    preview_rows, export_dir, export_ttl_hours
    public_base_url
  }
  class Registry {
    <<module registry.py>>
    CONNECTOR_TYPES: bigquery to BigQueryConnector
    create_connector(config) BaseDBConnector
  }
  class BaseDBConnector {
    <<abstract base.py>>
    type_name: str
    config: ConnectionConfig
    name: str
    dialect() str
    connect()
    fetch_raw_metadata() list~TableMeta~
    sample_column_values(table, columns, limit=15) dict
    dry_run_or_validate(query) ok and message
    execute_query(query, max_rows) QueryResult
    sql_hints() str
    close()
  }
  class BigQueryConnector {
    <<class bigquery_connector.py>>
    type_name: bigquery
    client: bigquery.Client
    connect() key file or VM credentials
    fetch_raw_metadata() list~TableMeta~
    sample_column_values(table, columns, limit) dict
    dry_run_or_validate(query) ok and message
    execute_query(query, max_rows) QueryResult
    sql_hints() GoogleSQL rules
    _table_access_problem(tables) str
    _dataset_allowed(dataset_id) bool
    _table_excluded(dataset_id, table_id) bool
    _max_bytes_billed() int or null
    _wait_for(job) rows
  }
  class SqlGuard {
    <<module sql_guard.py>>
    check_read_only(sql, dialect) ok and reason
    one statement, and it must be a query
    blocks INSERT UPDATE DELETE MERGE CREATE
    blocks DROP ALTER TRUNCATE INTO LOCK
    blocks EXTERNAL_QUERY SLEEP BENCHMARK LOAD_FILE
    blocks INFORMATION_SCHEMA
  }
  class TableMeta {
    <<pydantic>>
    connection, schema_name, table_name
    qualified_name, table_type, description
    columns: list~ColumnMeta~
    row_count, size_bytes
    ddl_hash() SHA-256
  }
  class ColumnMeta {
    <<pydantic>>
    name, data_type, mode, description
    is_partition, clustering_order
  }
  class QueryResult {
    <<pydantic>>
    sql: str
    df: DataFrame or null
    total_rows: int
    too_large: bool
    bytes_processed, duration_sec
  }
  class QueryError {
    <<exception>>
  }
  class Curator {
    <<class curator.py>>
    status: idle, running, done or failed
    max_workers: 4
    run()
    stop()
    load_business_context()
    sync_raw_metadata() list~TableMeta~
    curate_table(table)
    _curate_all(tables)
  }
  class TableCuration {
    <<pydantic>>
    summary: str
    tags: list~str~
    columns: column name to meaning
  }
  class LLMClient {
    <<class llm.py>>
    details in diagram 1
  }
  class KBCache {
    <<class kb_cache.py>>
    file: data/kb_cache.duckdb
    lock: one lock around one connection
    upsert_raw(table) needs curating
    save_curation(connection, name, summary, tags, meanings, samples)
    remove_missing(connection, keep) int
    list_tables(connection) list~TableInfo~
    get_table(name, connection) TableDetail or null
    find_tables(names, connection) list~TableDetail~
    set_business_context(text)
    get_business_context() str
    stats() tables and curated
    close()
  }
  class TableInfo {
    <<pydantic>>
    connection, qualified_name
    schema_name, table_name, table_type
    description, summary, tags
    row_count, size_bytes, column_count, curated
  }
  class TableDetail {
    <<pydantic>>
    columns: list~ColumnInfo~
  }
  class ColumnInfo {
    <<pydantic>>
    name, data_type, mode, description
    meaning, sample_values
    is_partition, clustering_order
  }

  FastAPIApp --> Config : load_config
  FastAPIApp --> Registry : create_connector
  FastAPIApp --> KBCache : creates
  FastAPIApp --> Curator : starts a thread
  Config ..> AppConfig
  AppConfig *-- ConnectionConfig
  AppConfig *-- LLMConfig
  AppConfig *-- OutputConfig
  Registry ..> BigQueryConnector : builds
  BaseDBConnector <|-- BigQueryConnector
  BaseDBConnector --> ConnectionConfig
  BigQueryConnector --> SqlGuard : check_read_only
  BigQueryConnector ..> TableMeta
  BigQueryConnector ..> QueryResult
  BigQueryConnector ..> QueryError
  TableMeta *-- ColumnMeta
  Curator --> BaseDBConnector : fetch_raw_metadata, sample_column_values
  Curator --> LLMClient : fast tier
  Curator --> KBCache : upsert_raw, save_curation
  Curator ..> TableCuration
  KBCache ..> TableMeta : stores
  KBCache ..> TableInfo
  TableInfo <|-- TableDetail
  TableDetail *-- ColumnInfo
```

## 3. One chat request, step by step

A data question with every branch. The `> …` lines are progress lines the user sees while the
agent works.

```mermaid
sequenceDiagram
  autonumber
  actor U as User
  participant W as Open WebUI
  participant API as main.py (FastAPI)
  participant AG as AnalyticsAgent
  participant KB as KBCache (DuckDB)
  participant LLM as LLMClient (DeepSeek)
  participant SW as write_and_run
  participant BQC as BigQueryConnector
  participant BQ as BigQuery
  participant OUT as renderer, charts, ExportStore

  U->>W: Ask a question in the chat
  W->>API: POST /v1/chat/completions<br/>Bearer AGENT_API_KEY, stream true
  API->>API: require_api_key() and _read_chat_request()
  API->>AG: run(messages)
  Note over AG: run() never raises.<br/>LLMError becomes LLM_FAILED,<br/>any other error UNEXPECTED_ERROR
  AG->>AG: latest_user_text() and is_openwebui_task()
  alt Open WebUI background task (chat title, tags)
    AG->>LLM: complete(question, fast)
    LLM-->>AG: text
  else no database connected
    AG-->>API: NOT_CONNECTED
  else a real question
    AG->>KB: list_tables(connection)
    AG->>AG: clean_history(): last 10 messages,<br/>tables and links removed
    AG->>LLM: complete_json(route prompt, Route, fast)
    LLM-->>AG: intent, wants_chart, uses_previous_result
    alt intent is help
      AG-->>API: help_text() with example questions
    else the KB has no tables yet
      AG-->>API: SCHEMA_LOADING, CATALOG_FAILED or NO_TABLES
    else intent is metadata
      AG->>KB: find_tables(): all tables when 15 or fewer,<br/>else up to 5 picked by the fast model
      AG->>LLM: complete(metadata prompt, fast)
      LLM-->>AG: answer from the catalog
      AG-->>API: the answer, with no BigQuery scan
    else intent is data
      AG-->>API: "> Finding the right tables…"
      AG->>KB: find_tables()<br/>(the fast model picks when there are more than 15)
      AG->>SW: write_and_run(llm, connector, SQL prompt)
      loop up to 3 attempts (1 + MAX_RETRIES)
        SW-->>API: "> Writing the SQL…" or "> Fixing the query…"
        SW->>LLM: complete_json(SqlDraft, smart)
        LLM-->>SW: sql, assumptions, clarifying_question
        break the model asks a clarifying question
          SW-->>AG: SqlOutcome with the question
        end
        SW-->>API: "> Checking the query…"
        SW->>BQC: dry_run_or_validate(sql)
        BQC->>BQC: sql_guard: one SELECT, no writes,<br/>no INFORMATION_SCHEMA or EXTERNAL_QUERY
        BQC->>BQ: dry run job
        BQ-->>BQC: statement type, tables read, bytes to scan
        BQC->>BQC: SELECT only, allowed datasets only,<br/>bytes within max_bytes_billed (50 GB)
        alt the check passed
          SW-->>API: "> Running the query…"
          SW->>BQC: execute_query(sql)
          BQC->>BQ: query job (maximum_bytes_billed, 120 s timeout)
          BQ-->>BQC: rows
          BQC-->>SW: QueryResult: a DataFrame,<br/>or too_large above 50,000 rows
        else rejected or failed
          SW->>SW: add sql_fix_request(error)<br/>to the chat and try again
        end
      end
      SW-->>AG: SqlOutcome
      alt a clarifying question
        AG-->>API: the question
      else no working query after 3 attempts
        AG-->>API: query_failed(error) and the Show SQL block
      else too many rows, or no rows
        AG-->>API: render_too_large() or NO_ROWS, and the Show SQL block
      else rows came back
        AG->>OUT: render_result(): save the CSV, then the table<br/>(up to 20 rows and 12 columns) or a 5-row preview
        opt the user asked for a chart
          AG-->>API: "> Drawing the chart…"
          AG->>LLM: complete_json(ChartSpec, fast),<br/>check_spec(), at most one retry
          AG->>OUT: render_chart() with matplotlib<br/>and seaborn, then save_png()
        end
        AG->>LLM: takeaway (fast, only when the whole table is shown)
        AG-->>API: takeaway, table, CSV link, chart,<br/>assumptions, Show SQL, chart offer
      end
    end
  end
  API-->>W: SSE chat.completion.chunk events, then [DONE]
  W-->>U: the rendered answer
  U->>API: GET /downloads/FILE_ID.csv or .png<br/>(through Caddy)
  API->>OUT: path_for(): 32 hex characters, file not expired
  OUT-->>U: the file
```

## 4. Start-up and background jobs

```mermaid
flowchart TB
  start(["The container starts:<br/>uvicorn backend.main:app, 1 worker"])

  subgraph LIFE["lifespan() in main.py"]
    direction TB
    l0["Runs once, before the first request"]
    l1["Thread limit set to 100, so slow answers don't block downloads"]
    l2["load_dotenv(): read .env"]
    l3["load_config(): read config.yaml, fill in values from .env, validate with pydantic"]
    l4["KBCache(data/kb_cache.duckdb): create the tables if they are missing"]
    l5["LLMClient(fast model, smart model)"]
    l6["ExportStore(exports/, 24 h): delete expired files"]
    l7["_connect(): create_connector() and connect() for each enabled connection.<br/>A connection that fails is logged and skipped."]
    l8["Start a thread: Curator.run()"]
    l9["Start a thread: _clean_exports()"]
    l10["app.state.agent = AnalyticsAgent(...)"]
    l0 ~~~ l1
    l1 --> l2 --> l3 --> l4 --> l5 --> l6 --> l7 --> l8 --> l9 --> l10
  end

  start --> l1
  l10 --> ready(["The API answers requests right away"])

  subgraph CUR["Curator.run()"]
    direction TB
    cs["Background thread.<br/>status: idle, running, then done or failed"]
    c1["load_business_context(): business_context.md, at most 20,000 characters, saved in kb_kv"]
    c2["sync_raw_metadata(): fetch_raw_metadata() for each connection<br/>list_datasets, include and exclude lists, list_tables, get_table"]
    c3{"For each table, kb.upsert_raw():<br/>new, changed ddl_hash, or not curated yet?"}
    c4["Queue the table for curation"]
    c5["Keep its curation, refresh only the row count and size"]
    c6["kb.remove_missing(): forget tables that were dropped"]
    c7["_curate_all(): ThreadPoolExecutor with 4 workers"]
    c8["curate_table(): sample_column_values()<br/>APPROX_TOP_COUNT on STRING columns, keeps columns with 15 values or fewer,<br/>scans at most 1 GiB (TABLESAMPLE on big tables)"]
    c9["Fast model: complete_json(TableCuration)<br/>summary, up to 5 tags, a meaning for each column"]
    c10["kb.save_curation(): curated = TRUE"]
    c11["Log: curated n/total tables"]
    c12["The table stays uncurated and is tried again on the next start"]
    cs ~~~ c1
    c1 --> c2 --> c3
    c3 -->|"yes"| c4
    c3 -->|"no"| c5
    c4 --> c6
    c5 --> c6
    c6 --> c7 --> c8 --> c9 --> c10 --> c11
    c9 -.->|"LLM or BigQuery error"| c12
  end

  subgraph CLEAN["_clean_exports()"]
    e1["Background thread. Every 3,600 seconds: ExportStore.cleanup()<br/>deletes CSV and PNG files older than 24 h"]
  end

  l8 -.-> c1
  l9 -.-> e1
```

Because `upsert_raw` compares a hash of each table's structure, a restart re-curates only new or
changed tables. An unchanged project costs no profiling and no LLM calls.

## 5. KB cache tables

The KB cache is one DuckDB file on the `agent-data` volume. It is only a cache: deleting it costs
one full curation pass on the next start.

```mermaid
erDiagram
  kb_tables ||--o{ kb_columns : "has columns"
  kb_tables {
    varchar connection PK "connection name from config.yaml"
    varchar qualified_name PK "project.dataset.table"
    varchar schema_name "the BigQuery dataset"
    varchar table_name
    varchar table_type "TABLE or VIEW"
    varchar description "from BigQuery"
    varchar summary "from the LLM, empty until curated"
    varchar_list tags "up to 5 lowercase words"
    bigint row_count
    bigint size_bytes
    integer column_count
    boolean curated
    varchar ddl_hash "SHA-256 of name, type, description and columns"
  }
  kb_columns {
    varchar connection FK
    varchar qualified_name FK
    integer position "column order"
    varchar name "dot path for nested fields"
    varchar data_type
    varchar mode "NULLABLE, REQUIRED or REPEATED"
    varchar description "from BigQuery"
    varchar meaning "from the LLM"
    varchar_list sample_values "only STRING columns with 15 values or fewer"
    boolean is_partition
    integer clustering_order
  }
  kb_kv {
    varchar key PK "business_context"
    varchar value "the business rules text"
  }
```

## Limits and constants

| Where | Name | Value | What it does |
|---|---|---|---|
| `main.py` | `WORKER_THREADS` | 100 | Worker threads, so slow answers don't block downloads |
| `main.py` | `EXPORT_CLEANUP_INTERVAL_SEC` | 3,600 | How often expired downloads are deleted |
| `router.py` | `MAX_TABLES_WITHOUT_PICK` | 15 | Up to this many tables, the model sees all of them |
| `router.py` | `MAX_PICKED_TABLES` | 5 | Most tables picked for one question |
| `sql_writer.py` | `MAX_RETRIES` | 2 | Extra tries after a rejected query (3 attempts in all) |
| `history.py` | `max_messages` | 10 | Earlier chat messages sent to the models |
| `prompts.py` | `MAX_ROUTER_TABLE_NAMES` | 150 | More tables than this: the router sees dataset counts instead |
| `prompts.py` | `MAX_CATALOG_CHARS` | 30,000 | Size limit of the one-line catalog |
| `prompts.py` | `MAX_SUMMARY_CHARS` | 300 | Longest table summary line |
| `prompts.py` | `CHART_SAMPLE_ROWS` | 5 | Rows the chart model sees |
| `curator.py` | `max_workers` | 4 | Tables curated at the same time |
| `curator.py` | `MAX_PROMPT_COLUMNS` | 150 | Columns per table sent to the model |
| `curator.py` | `MAX_BUSINESS_CONTEXT_CHARS` | 20,000 | Longest business context used |
| `curator.py` | `MAX_TAGS` | 5 | Tags kept per table |
| `bigquery_connector.py` | `_PROFILE_MAX_BYTES_BILLED` | 1 GiB | Scan cap for profiling one table |
| `base.py` | `sample_column_values(limit)` | 15 | Most distinct values kept as samples |
| `replies.py` | `MAX_LISTED_TABLES` | 25 | Tables listed when the agent asks which one to use |
| `charts.py` | `MAX_BARS`, `PIE_SLICES` | 30, 8 | Most bars, and pie slices before "Other" |
| `config.yaml` | `max_bytes_billed_gb` | 50 | Scan cap per query (dry run and job) |
| `config.yaml` | `query_timeout_sec` | 120 | A query running longer is cancelled |
| `config.yaml` | `max_rows_fetch` | 50,000 | More rows: counted, not downloaded |
| `config.yaml` | `inline_table_max_rows`, `inline_table_max_columns` | 20, 12 | Bigger results show a preview instead |
| `config.yaml` | `preview_rows` | 5 | Rows in a preview |
| `config.yaml` | `export_ttl_hours` | 24 | How long CSV and PNG files are kept |
| `config.py` | `LLMConfig` | temperature 0, timeout 120 s, 2 retries | Settings for every model call |
