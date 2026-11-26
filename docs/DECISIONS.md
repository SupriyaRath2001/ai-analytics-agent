# v1 build plan and decisions

This file is the source of truth for v1. If code and this file disagree, fix one of them on purpose.

## 1. What v1 does

People ask questions about BigQuery data in plain English in a ChatGPT-style chat (Open WebUI).

There are two kinds of questions:

1. **Explore**: "Which tables have customer data?", "What columns are in orders and what do they mean?"
   These are answered from the KB cache, with **no BigQuery scan**.
2. **Analyse**: filters, joins across tables, group-bys and aggregates. The agent writes GoogleSQL,
   checks it, runs it on BigQuery and shows the result.

What the user gets back:

| Result | What the chat shows |
|---|---|
| 0 rows | "No rows matched", plus the assumptions and the SQL |
| 1–20 rows and ≤ 12 columns | full table in chat, a 1–2 line takeaway, a CSV download link |
| more than 20 rows, or more than 12 columns | 5-row preview, row/column count, CSV download link |
| more than `max_rows_fetch` (50,000) | nothing downloaded; the user is told the row count and how to narrow the question |
| chart needed | PNG chart in chat, plus a download link for it |

Every data answer ends with a collapsible **Show SQL** block.

## 2. Decisions

| Topic | Decision |
|---|---|
| Databases | **BigQuery only** in v1: one GCP project, all datasets (with optional include/exclude lists). The connector interface stays pluggable, so MySQL later means one new file plus one registry line. |
| Environments | One deployment = one config = one project. Dev and prod are separate deployments. |
| Cross-database joins | Not in v1. |
| Users | 50+, English. The service is a thin API; BigQuery and the LLM do the heavy work. |
| LLM | LiteLLM; the provider is chosen later. Two tiers: `fast_model` (routing, curation, chart specs, takeaways, metadata answers, Open WebUI background tasks) and `smart_model` (writing and fixing SQL). There's no model picker in the UI: Open WebUI shows one model, `analytics-agent`. `LLM_API_KEY` / `LLM_API_BASE` apply to both tiers; to mix providers, leave them empty and set each provider's own variable (`OPENAI_API_KEY`, `GEMINI_API_KEY`, `ANTHROPIC_API_KEY`), which LiteLLM reads itself. |
| Framework | Plain Python + LiteLLM + Pydantic + FastAPI. No LangChain/LangGraph. |
| Embeddings | None in v1. The fast model picks tables from a short catalog (name + one-line summary). With ≤ 15 tables, every table is passed and the pick step is skipped. |
| Prompt size | Every prompt section has a cap, so a big project can't overflow the context. The one-line catalog in the pick and metadata prompts stops at about 30,000 characters and ends with "(… N more tables not listed)". Table details show at most 150 columns per table (the curator's `MAX_PROMPT_COLUMNS`, so the same columns it described), then "… and N more columns"; a partition column past the cap is still listed, since it decides what a query costs (ingestion-time `_PARTITIONTIME` comes last). The router sees up to 150 table names, otherwise datasets with counts. Summaries are cut at 300 characters, the business context at 20,000. |
| Startup | The crawler runs in a background thread. It saves raw metadata first (seconds), then writes LLM summaries for new or changed tables. The API serves right away. If no connection's catalog can be read, the run ends as `failed`, and with an empty KB the chat says so (permissions, project name, check the logs) instead of "still reading" forever. At shutdown `curator.stop()` drops the queued tables; calls already under way finish but aren't saved. |
| Schema refresh | Only on restart. So table row counts in prompts read "about N rows (when the agent started)". |
| KB cache | DuckDB file `data/kb_cache.duckdb`, with a SHA-256 hash per table. Unchanged tables are skipped: no profiling, no LLM cost. |
| Business context | Optional `config/business_context.md`. It's loaded at startup, stored in the KB and added to SQL and metadata prompts (capped at 20,000 characters). |
| Reading metadata | BigQuery **API** (`list_datasets`, `list_tables`, `get_table`). It's free and includes nested fields, descriptions, partitioning, clustering and row counts. The original spec used INFORMATION_SCHEMA queries; the API gives the same data with zero query cost and simpler code. |
| Profiling | One query per table: `APPROX_TOP_COUNT` on top-level STRING columns. Values are kept only when a column has ≤ 15 distinct values. Profiling jobs are capped at 1 GiB billed. Tables over 1 GiB are read with `TABLESAMPLE SYSTEM (n PERCENT)`, with n the largest whole percent that stays under the cap (at least 1); views can't be sampled. Failures are logged and skipped. |
| Read-only safety | 1) SQLGlot: exactly one statement, which must be a SELECT/WITH query, with no `INFORMATION_SCHEMA` or `EXTERNAL_QUERY` (both reach past the allowed datasets). 2) BigQuery dry run: valid syntax, `statement_type == "SELECT"`, every referenced table is in the configured project and an allowed dataset, and bytes ≤ `max_bytes_billed_gb`. The same byte limit is set as `maximum_bytes_billed` on the real job. |
| Self-correction | Up to **2 retries**, each feeding the exact error back to the smart model. After that, the user gets a plain-English failure message. |
| Clarifying questions | The agent decides. It asks only when a wrong guess would mislead (unclear metric, a time range that changes the answer, a calendar choice). Otherwise it guesses and states its assumptions. |
| Row counts | `total_rows` from the finished BigQuery job, so no extra COUNT query. Rows are downloaded only when `total_rows ≤ max_rows_fetch`. |
| Charts | Only when the user asks or the question clearly needs one. Otherwise the answer ends with *"Want a chart of this? Just ask."* A ChartSpec (JSON from the fast model) feeds deterministic Matplotlib/Seaborn code. Never `exec` LLM code. Data is aggregated in SQL first. |
| Downloads | CSV (UTF-8 with BOM so Excel opens it cleanly) with the values as BigQuery sent them: unrounded numbers, UTC timestamps. Text cells that start with `=`, `+`, `-` or `@` (or a tab or carriage return) get a leading `'`, so a spreadsheet doesn't run them as formulas. Files live in `exports/` (a Docker volume) with random 32-hex names and are deleted after 24 h. Served at `/downloads/<name>`. PNGs display inline; `?download=1` forces a download. |
| Chat state | Nothing is stored server-side. Follow-ups use the chat history Open WebUI sends; the last SQL is in the previous answer's "Show SQL" block. |
| Formats | Numbers `1,234,567`; floats with 2 decimals when \|x\| ≥ 1 and 3 significant digits when \|x\| < 1 (`0.0042`); dates `YYYY-MM-DD`. The takeaway model gets the raw values (CSV) and rounds them when it writes, so it never reasons from a rounded `0`. Several currencies may exist: never convert, never add amounts across currencies. |
| Timezone | `timezone` in config (from `TZ`, default UTC). Prompts give the model today's date in that zone and tell it to use `CURRENT_DATE('<tz>')`. TIMESTAMP results (tz-aware) are shown in that zone in chat tables and in the takeaway's CSV, so the SQL writer is told not to convert them for display. The CSV download keeps UTC. |
| Hosting | One small VM with Docker Compose running the agent, Open WebUI and Caddy (HTTPS). Caddy sends `/downloads/*` to the agent and everything else to Open WebUI. Same origin, so relative links work. |
| Login | Open WebUI "Continue with Google". New users stay pending until an admin approves them (`DEFAULT_USER_ROLE=pending`). |
| Security in v1 | Minimal on purpose: the read-only checks above, an optional `AGENT_API_KEY` between Open WebUI and the agent, and the agent port is not published. No PII masking, no per-user access rules. |
| Logging | Standard Python logging to the console. No audit DB, no tracing. |
| Secrets | Only in `.env` (git-ignored). Never in code, examples or commits. |

## 3. Request flow

```
Open WebUI ──POST /v1/chat/completions──▶ AnalyticsAgent.run(messages)  (yields markdown chunks)
   1. clean the chat history, take the latest user text
   2. Open WebUI background task (a prompt that starts with "### Task:" and quotes the chat in
      <chat_history>: titles, tags, follow-ups) → answer with the fast model directly, never
      touch BigQuery. Open WebUI's RAG template also starts with "### Task:" but has no
      <chat_history>: it is a real question.
   3. route (fast model) → intent: metadata | data | help, wants_chart, uses_previous_result
      (KB still empty → a fixed reply that depends on the curator's status, see agent/router.py)
   4. metadata → KB lookup → fast model writes the answer (markdown). No BigQuery.
   5. data     → pick tables (fast) → write SQL (smart; may return a clarifying question instead)
                → check (sql_guard + dry run) → retry ≤ 2 → run → render table / CSV / chart
                → takeaway (fast) from the raw rows, for results shown in full
   6. help     → what the agent can do + 3 cheap example questions built from real table names
                (no "show sample rows": a LIMIT doesn't reduce what BigQuery scans)
```

Progress is streamed first as short blockquote lines, then the answer: `> Finding the right tables…`,
`> Writing the SQL…`, `> Checking the query…`, `> Running the query…`, and `> Drawing the chart…`
when a chart was asked for. A retry after a rejected query starts with `> Fixing the query…` in
place of `> Writing the SQL…`.

## 4. Repository layout

```
backend/
  config.py                 load + validate config.yaml (done)
  llm.py                    LiteLLM wrapper: complete(), complete_json() (done)
  main.py                   FastAPI app: lifespan, /health, /v1/models, /v1/chat/completions, /downloads
  connectors/
    base.py                 BaseDBConnector, ColumnMeta, TableMeta, QueryResult, QueryError (done)
    sql_guard.py            SQLGlot read-only check
    bigquery_connector.py   BigQueryConnector
    registry.py             type name → connector class (main.py connects them)
  bootstrap/
    kb_cache.py             DuckDB KB store
    curator.py              startup crawl, hashing, profiling, LLM summaries, business context
  agent/
    history.py              clean Open WebUI history, find the last SQL
    prompts.py              all prompt text in one place
    replies.py              the fixed replies that need no model: help, errors, short notes
    router.py               AnalyticsAgent: routing, metadata answers, rendering the answer
    sql_writer.py           the write → check → run loop of a data answer, with ≤ 2 retries
  services/
    exports.py              ExportStore: save CSV/PNG, URLs, 24 h cleanup
    renderer.py             markdown tables, previews, SQL block
    charts.py               ChartSpec + Matplotlib/Seaborn PNG rendering
config/                     config.yaml.example, business_context.md.example
tests/                      pytest, no network
docs/                       this file, deploy-vm.md, adding-a-database.md
Dockerfile, docker-compose.yml, Caddyfile, .env.example, .github/workflows/ci.yml
```

## 5. Contracts

Signatures below are binding. Everything else (private helpers, file splits) is up to the module owner.

### connectors/base.py (done)
See the file: `ColumnMeta`, `TableMeta` (with `ddl_hash()`), `QueryResult`, `QueryError`, `BaseDBConnector`
(`dialect`, `connect`, `fetch_raw_metadata`, `sample_column_values`, `dry_run_or_validate`,
`execute_query`, `sql_hints`, `close`).

### connectors/sql_guard.py
```python
def check_read_only(sql: str, dialect: str) -> tuple[bool, str]:
    """(True, "") when sql is exactly one read-only SELECT/WITH query, else (False, reason)."""
```
Reject: parse errors, zero or more than one statement, anything that isn't a query, and any node that
writes or locks (INSERT/UPDATE/DELETE/MERGE/CREATE/DROP/ALTER/commands, `SELECT … INTO`,
`FOR UPDATE`). Also reject the functions `sleep`, `benchmark` and `load_file` (useful once MySQL arrives),
and `INFORMATION_SCHEMA` and `EXTERNAL_QUERY`: catalog views can list tables outside the allowed
datasets, and federated queries run outside BigQuery's checks. Trailing semicolons and whitespace are fine.

### connectors/bigquery_connector.py
```python
class BigQueryConnector(BaseDBConnector):
    type_name = "bigquery"
    dialect -> "bigquery"
```
- `connect()`: `bigquery.Client(project=..., location=...)`, using the service-account key at
  `credentials_path` when set, otherwise Application Default Credentials.
- Allowed datasets: `include_datasets` (empty = all) minus `exclude_datasets`. `exclude_tables` holds
  `"dataset.table"` entries.
- `fetch_raw_metadata()`: one `TableMeta` per table/view, with `qualified_name = "project.dataset.table"`.
  Nested RECORD fields appear as the parent column plus `parent.child` paths. The partition column has
  `is_partition=True`; for ingestion-time partitioning, add a pseudo column `_PARTITIONTIME`.
  `clustering_order` is 1-based. A table that fails to load is logged and skipped.
- `sample_column_values(table, columns, limit=15)`: one `APPROX_TOP_COUNT` query over the top-level,
  non-repeated STRING columns in `columns`, capped at 1 GiB billed. A table over 1 GiB is read with
  `TABLESAMPLE SYSTEM (n PERCENT)` so the query fits the cap (views are read in full). Returns only the
  columns with ≤ `limit` values. Any error → log and return `{}`.
- `dry_run_or_validate(query)`: the checks in section 2. Returns `(True, "Dry run OK, will scan 1.2 GB.")`
  or `(False, "<clear reason>")`. Messages are plain enough to show the SQL writer.
- `execute_query(query, max_rows=None)`: `max_rows` defaults to `config.max_rows_fetch`. Waits up to
  `query_timeout_sec` and cancels the job on timeout. When `total_rows > max_rows`, it returns
  `QueryResult(df=None, too_large=True, ...)` without downloading. Otherwise it downloads with
  `to_dataframe(create_bqstorage_client=False)`. Google API errors become `QueryError(message)`.
- `sql_hints()`: GoogleSQL rules for the SQL writer: backticked `project.dataset.table` names, UNNEST for
  REPEATED fields, dot paths for RECORD fields, filter on the partition column when there is one,
  SAFE_DIVIDE, no `SELECT *` on wide tables, ORDER BY for top-N and time series, a LIMIT doesn't reduce
  the bytes scanned (use `TABLESAMPLE` for sample rows of big tables), and TIMESTAMPs are UTC (use
  `DATE(ts, tz)` to group by local day).

### connectors/registry.py
```python
CONNECTOR_TYPES: dict[str, type[BaseDBConnector]]          # {"bigquery": BigQueryConnector}
def create_connector(config: ConnectionConfig) -> BaseDBConnector   # ValueError for unknown types, naming the known ones
```
Connecting is main.py's job: it connects each enabled connection in config order and logs and skips one
that fails, so the API still starts.

### services/exports.py
```python
class ExportStore:
    def __init__(self, export_dir: str | Path, public_base_url: str = "", ttl_hours: int = 24): ...
    def save_csv(self, df: pd.DataFrame) -> str        # file name "<32 hex>.csv", UTF-8 with BOM, no index,
                                                       # formula-like text gets a leading '
    def save_png(self, data: bytes) -> str             # file name "<32 hex>.png"
    def url_for(self, file_name: str) -> str           # f"{public_base_url}/downloads/{file_name}"
    def path_for(self, file_name: str) -> Path | None  # None when the name is malformed, missing or expired
    def cleanup(self) -> int                           # delete expired files, return how many
```

### services/renderer.py
```python
class RenderedResult(BaseModel):
    markdown: str     # the table (or preview) plus the download-link line
    inline: bool      # True when every row is shown in the chat
    csv_file: str     # file name in the ExportStore

def format_cell(value: object, column_name: str, timezone: str = "UTC") -> str
def df_to_markdown(df: pd.DataFrame) -> str             # tz-aware timestamps in UTC
def render_result(df: pd.DataFrame, total_rows: int, store: ExportStore, output: OutputConfig,
                  timezone: str = "UTC") -> RenderedResult   # the agent passes config.timezone
def render_too_large(total_rows: int, max_rows: int) -> str
def render_sql_block(sql: str) -> str                   # <details> / <summary>Show SQL</summary> lines + ```sql block
def render_assumptions(assumptions: list[str]) -> str   # "" when empty, else "_Assumed: a; b._"
```
Number formatting: thousands separators for numbers, except integer columns whose names look like ids,
years, codes or phone and order numbers (`id`, `*_id`, `*id`, `year`, `code`, `zip`, `pin`, `phone`,
`mobile`, `number`, `no`, `num`: so `phone_number`, `order_no`, `invoice_num`). Floats get 2 decimals when
|x| ≥ 1 and 3 significant digits when |x| < 1 (`0.0042`, not `0`). Dates are ISO and timestamps
`YYYY-MM-DD HH:MM:SS`; tz-aware ones (BigQuery TIMESTAMP) are shown in `timezone`, except values within a
day of the year 1 or 9999, which can't be moved. The CSV keeps the raw values. None/NaN → empty. Escape
`|` and newlines in strings. Download line: `[⬇️ Download CSV (1,234 rows)](url)`.

### services/charts.py
```python
class ChartSpec(BaseModel):
    chart_type: Literal["bar", "line", "scatter", "hist", "box", "heatmap", "pie"]
    x_col: str
    y_col: str | None = None
    hue_col: str | None = None
    value_col: str | None = None     # heatmap cell values
    title: str
    x_label: str | None = None
    y_label: str | None = None
    horizontal: bool = False         # bar charts only
    top_n: int | None = None         # bar/pie: keep the top N; pie groups the rest as "Other"
    sort: Literal["none", "asc", "desc"] = "none"

def check_spec(spec: ChartSpec, df: pd.DataFrame) -> list[str]   # problems; empty list = fine
def render_chart(df: pd.DataFrame, spec: ChartSpec) -> bytes      # PNG bytes
```
Use the Agg backend, a seaborn whitegrid theme, 10×6 in at 150 dpi and a white background. `check_spec`
rejects a bar or line chart whose rows share an x value (or an x and `hue_col` pair): adding them up
would be wrong for averages, rates and shares, so the model must set `hue_col` or aggregate in SQL.
Bars and lines draw with `estimator="sum", errorbar=None`. Pies default to the top 8 + "Other". Bar
charts with more than 30 categories keep the top 30.

### bootstrap/kb_cache.py
```python
class ColumnInfo(BaseModel):
    name: str; data_type: str; mode: str | None; description: str; meaning: str
    sample_values: list[str]; is_partition: bool; clustering_order: int | None

class TableInfo(BaseModel):
    connection: str; qualified_name: str; schema_name: str; table_name: str; table_type: str
    description: str; summary: str; tags: list[str]; row_count: int | None; size_bytes: int | None
    column_count: int; curated: bool

class TableDetail(TableInfo):
    columns: list[ColumnInfo]

class KBCache:
    def __init__(self, path: str | Path): ...   # creates the file/tables; one connection + a threading.Lock
    def upsert_raw(self, table: TableMeta) -> bool
        # Save raw metadata. Returns True when the table is new, its ddl_hash changed or it was never
        # curated. A changed hash clears the old summary/meanings/samples. An unchanged hash only
        # refreshes row_count/size_bytes.
    def save_curation(self, connection: str, qualified_name: str, summary: str, tags: list[str],
                      column_meanings: dict[str, str], sample_values: dict[str, list[str]]) -> None
        # sets curated = True
    def remove_missing(self, connection: str, keep: set[str]) -> int
    def list_tables(self, connection: str | None = None) -> list[TableInfo]
    def get_table(self, qualified_name: str, connection: str | None = None) -> TableDetail | None
    def find_tables(self, names: list[str], connection: str | None = None) -> list[TableDetail]
        # matches qualified name, "dataset.table" or a bare table name, case-insensitive, de-duplicated
    def set_business_context(self, text: str) -> None
    def get_business_context(self) -> str
    def stats(self) -> dict[str, int]   # {"tables": n, "curated": m}
    def close(self) -> None
```

### bootstrap/curator.py
```python
class TableCuration(BaseModel):
    summary: str                      # 1–2 plain-English sentences
    tags: list[str] = []              # up to 5 lowercase domain words
    columns: dict[str, str] = {}      # column name → meaning in ≤ 15 words

class Curator:
    status: str                       # "idle" | "running" | "done" | "failed"
    def __init__(self, connectors: dict[str, BaseDBConnector], kb: KBCache, llm: LLMClient,
                 business_context_path: str | None = None, max_workers: int = 4): ...
    def run(self) -> None                         # the whole startup pass; logs; never raises
    def load_business_context(self) -> None
    def sync_raw_metadata(self) -> list[TableMeta]  # upsert all, remove dropped tables, return tables needing curation
    def curate_table(self, table: TableMeta) -> None  # profile → fast LLM → save_curation
    def stop(self) -> None                            # at shutdown: skip the queued tables
```
If the LLM fails for a table, log it and leave the table uncurated so the next start retries it. A
connection whose catalog can't be read keeps its cached tables; when no connection's catalog can be
read, `sync_raw_metadata` raises and the run ends as `failed`. After `stop()`, `curate_table` does
nothing and `_curate_all` cancels the queued tables, so shutdown waits only for the calls under way;
their results aren't saved, because the KB is being closed.

### agent/history.py
```python
def message_text(message: dict) -> str            # content may be a string or a list of parts
def latest_user_text(messages: list[dict]) -> str
def clean_history(messages: list[dict], max_messages: int = 10) -> list[dict]
    # Earlier turns only (not the latest user message), user/assistant roles. Assistant text is stripped
    # of tables, images, links and progress lines; SQL is kept as "Previous SQL:\n```sql ...```".
def last_sql(messages: list[dict]) -> str | None
def is_openwebui_task(text: str) -> bool
    # Open WebUI background prompts start with "### Task:" and quote the chat in <chat_history>. Its RAG
    # template (a question with attached files) starts with "### Task:" too, so the marker alone isn't enough.
```

### agent/router.py
```python
class AnalyticsAgent:
    def __init__(self, config: AppConfig, connectors: dict[str, BaseDBConnector], kb: KBCache,
                 llm: LLMClient, store: ExportStore, *,
                 curation_status: Callable[[], str] | None = None): ...
    def run(self, messages: list[dict]) -> Iterator[str]   # markdown chunks: progress lines, then the answer
```
v1 uses the first connection that connected. It never raises to the API: every failure becomes a friendly
message. The SQL loop is in `agent/sql_writer.py` (`write_and_run`) and the fixed replies in
`agent/replies.py`.

- `curation_status` (main.py passes `lambda: curator.status`) picks the reply when the KB has no tables:
  `idle` or `running` (or no callable) → "still reading, try again in a minute"; `failed` → the catalog
  couldn't be read (permissions, project name), check the agent logs; `done` → no tables were found
  (check the project and the dataset lists).
- Results are rendered with `render_result(..., timezone=config.timezone)`.
- The takeaway for a result shown in full (≤ 20 rows) gets the rows as CSV (`df.to_csv(index=False)`)
  with raw values and TIMESTAMP columns in the configured timezone, and is told to round sensibly.

### main.py
- `GET /health` → `{"status": "ok", "curation": curator.status, "kb": kb.stats()}`
- `GET /v1/models` → OpenAI list format with one model, id `analytics-agent`
- `POST /v1/chat/completions` → OpenAI-compatible, streaming (SSE `chat.completion.chunk`, ending with
  `data: [DONE]`) and non-streaming. Bearer auth when `agent_api_key` is set. Unknown request fields
  are ignored.
- `GET /downloads/{file_name}` → the file from ExportStore, 404 when missing or expired. CSVs download as
  attachments; PNGs display inline unless `?download=1`.
- `/health`, `/v1/models` and the API-key check are `async def`, so they never wait for a worker thread.
- Lifespan: raise AnyIO's default thread limit from 40 to 100 (a streaming answer holds a worker thread
  while it waits for the LLM or BigQuery, and downloads need threads too, so 40 slow answers would block
  every download), load `.env` with `load_dotenv()`, load config, open the KB, start the curator thread,
  clean exports hourly. `load_config()` itself reads only the environment, so a developer's `.env` never
  leaks into the tests. If connecting to BigQuery fails, keep running and explain the problem in chat (no
  crash loop). On shutdown: `curator.stop()`, then close the connectors and the KB.

## 6. Answer layout

```markdown
<1–2 sentence answer / takeaway>

| col | col |
|---|---|
| … | … |

[⬇️ Download CSV (12 rows)](/downloads/<id>.csv)

![<chart title>](/downloads/<id>.png)
[⬇️ Download chart](/downloads/<id>.png?download=1)

_Assumed: last 30 days; paid orders only._

<details>
<summary>Show SQL</summary>

```sql
SELECT …
```

</details>

_Want a chart of this? Just ask._
```

`<details>` and `<summary>` each go on their own line. Open WebUI only draws the collapsible block
then; with both on one line it shows the raw tags as text.

## 7. Code style ("humanly developed")

- Write code the way a careful person would: plain names, small functions, straightforward control flow.
- No speculative abstractions, factories, plugin frameworks or settings nobody asked for.
- Type hints on public functions. Docstrings say *why* or *what for*, not a restatement of the code.
- Comments only where the code isn't obvious. No banner comments, no emoji in code.
- Use only the dependencies in `pyproject.toml`. Ask before adding one. `sqlglot` is pinned to 30.x
  (`>=30,<31`): the read-only check matches its expression classes by name, and those can change
  between major versions.
- Raise clear exceptions. Never swallow an error silently: log it with context.
- Keep files under ~300 lines and split by responsibility.
- Tests: pytest, plain functions, fakes over heavy mocking, **no network, no real BigQuery, no real LLM**.
- `.venv/bin/ruff check .` and `.venv/bin/ruff format .` must be clean (line length 100).

## 8. Not in v1

MySQL connector, cross-database joins (DuckDB can attach both), embeddings, per-user access rules, PII
masking, audit log, schema refresh without restart, Cloud Run deployment, interactive charts, Excel export.
