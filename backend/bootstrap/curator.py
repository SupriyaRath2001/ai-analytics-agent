"""The startup pass that fills the KB: raw metadata first, then LLM summaries.

Saving the raw metadata takes seconds, so the agent can answer right away. Then each new or
changed table is profiled and described by the fast model. Tables whose structure hasn't
changed since the last start are skipped, so a restart costs no profiling and no LLM calls.
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from pydantic import BaseModel, Field

from backend.bootstrap.kb_cache import KBCache
from backend.connectors.base import BaseDBConnector, ColumnMeta, TableMeta
from backend.llm import LLMClient, LLMError

log = logging.getLogger(__name__)

MAX_BUSINESS_CONTEXT_CHARS = 20_000
MAX_TAGS = 5
# On very wide tables the prompt and the reply would get too long for a fast model.
# The first columns are plenty for a summary.
MAX_PROMPT_COLUMNS = 150

SYSTEM_PROMPT = (
    "You describe database tables for a data catalog. Business users read your text to find "
    "the right table, and an SQL writer reads it to understand the columns. Write short, plain, "
    "factual English. Don't guess beyond what the names, descriptions and values show."
)


class TableCuration(BaseModel):
    """What the fast model returns for one table."""

    summary: str  # 1-2 plain-English sentences
    tags: list[str] = Field(default_factory=list)  # up to 5 lowercase domain words
    columns: dict[str, str] = Field(default_factory=dict)  # column name -> meaning, <= 15 words


class Curator:
    """Keeps the KB in step with the databases. ``run`` is meant for a background thread."""

    def __init__(
        self,
        connectors: dict[str, BaseDBConnector],
        kb: KBCache,
        llm: LLMClient,
        business_context_path: str | None = None,
        max_workers: int = 4,
    ):
        self.connectors = connectors
        self.kb = kb
        self.llm = llm
        self.business_context_path = business_context_path
        self.max_workers = max_workers
        self.status = "idle"  # shown by /health: idle -> running -> done, or failed
        self._stopping = threading.Event()

    def stop(self) -> None:
        """Called at shutdown, so the app doesn't wait for every queued table to be curated.

        Calls already under way finish, but their results aren't saved. The tables not
        curated yet are picked up on the next start.
        """
        self._stopping.set()

    def run(self) -> None:
        """The whole startup pass. Problems are logged, never raised, so the API keeps serving."""
        self.status = "running"
        try:
            self.load_business_context()
            self._curate_all(self.sync_raw_metadata())
        except Exception:
            log.exception("the KB startup pass failed")
            self.status = "failed"
        else:
            self.status = "done"

    def load_business_context(self) -> None:
        """Store the optional business rules file in the KB, or "" when there is none."""
        self.kb.set_business_context(self._read_business_context())

    def sync_raw_metadata(self) -> list[TableMeta]:
        """Save every table's catalog metadata and forget tables that were dropped.

        Returns the tables that need curating. A connection whose catalog can't be read is
        skipped and its cached tables are kept, so one bad start doesn't empty the KB. When
        no catalog at all could be read, it raises, so the run ends as "failed" and the chat
        can say so instead of "still loading" forever.
        """
        pending: list[TableMeta] = []
        unreadable = 0
        for name, connector in self.connectors.items():
            try:
                tables = connector.fetch_raw_metadata()
            except Exception:
                log.exception("could not read the tables of %r; its cached tables are kept", name)
                unreadable += 1
                continue
            to_curate = []
            for table in tables:
                if self.kb.upsert_raw(table):
                    to_curate.append(table)
            removed = self.kb.remove_missing(name, {table.qualified_name for table in tables})
            log.info(
                "connection %r: %d tables, %d to curate, %d dropped",
                name,
                len(tables),
                len(to_curate),
                removed,
            )
            pending.extend(to_curate)
        if self.connectors and unreadable == len(self.connectors):
            raise RuntimeError("could not read the tables of any connection, see the errors above")
        return pending

    def curate_table(self, table: TableMeta) -> None:
        """Profile one table, have the fast model describe it and save the result.

        Raises when the connector, the model or the KB fails; the table then stays
        uncurated and the next start tries again. After stop() it does nothing, so shutdown
        doesn't wait for the queue or write to a KB that is being closed.
        """
        if self._stopping.is_set():
            return
        connector = self.connectors[table.connection]
        samples = connector.sample_column_values(table, [column.name for column in table.columns])
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": _table_prompt(table, samples)},
        ]
        curation = self.llm.complete_json(messages, TableCuration, tier="fast")
        if self._stopping.is_set():
            return
        self.kb.save_curation(
            table.connection,
            table.qualified_name,
            summary=curation.summary.strip(),
            tags=_clean_tags(curation.tags),
            column_meanings=curation.columns,
            sample_values=samples,
        )

    def _curate_all(self, tables: list[TableMeta]) -> None:
        """Curate tables in parallel. One table failing doesn't stop the others."""
        if not tables:
            log.info("every table is already curated")
            return
        total = len(tables)
        curated = failed = 0
        with ThreadPoolExecutor(max_workers=self.max_workers, thread_name_prefix="curator") as pool:
            futures = {pool.submit(self.curate_table, table): table for table in tables}
            for future in as_completed(futures):
                if self._stopping.is_set():
                    pool.shutdown(cancel_futures=True)  # drop the queued tables
                    log.info(
                        "curation stopped at %d/%d tables, the rest wait for the next start",
                        curated,
                        total,
                    )
                    return
                name = futures[future].qualified_name
                try:
                    future.result()
                except LLMError as exc:
                    failed += 1
                    log.warning("could not curate %s, will retry on next start: %s", name, exc)
                except Exception:
                    failed += 1
                    log.exception("could not curate %s, will retry on next start", name)
                else:
                    curated += 1
                    log.info("curated %d/%d tables", curated, total)
        if failed:
            log.warning("%d of %d tables were not curated, see the errors above", failed, total)

    def _read_business_context(self) -> str:
        if not self.business_context_path:
            return ""
        path = Path(self.business_context_path)
        if not path.exists():
            log.info("no business context file at %s", path)
            return ""
        try:
            text = path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError) as exc:
            log.error("could not read business context %s, going on without it: %s", path, exc)
            return ""
        if len(text) > MAX_BUSINESS_CONTEXT_CHARS:
            log.warning(
                "business context %s has %d characters; only the first %d are used",
                path,
                len(text),
                MAX_BUSINESS_CONTEXT_CHARS,
            )
            text = text[:MAX_BUSINESS_CONTEXT_CHARS]
        log.info("loaded business context from %s (%d characters)", path, len(text))
        return text


def _table_prompt(table: TableMeta, samples: dict[str, list[str]]) -> str:
    columns = table.columns[:MAX_PROMPT_COLUMNS]
    lines = [
        f"Table: {table.qualified_name} ({table.table_type})",
        f"Description: {table.description or '(none)'}",
        "Columns:",
        *(_column_line(column, samples.get(column.name, [])) for column in columns),
    ]
    if len(table.columns) > len(columns):
        lines.append(f"- ... and {len(table.columns) - len(columns)} more columns")
    lines += [
        "",
        "Reply with:",
        "- summary: 1-2 sentences on what the table holds and what it is useful for.",
        "- tags: up to 5 lowercase domain words, such as sales, customers or marketing.",
        "- columns: each column name listed above, written exactly as above, mapped to its "
        "meaning in at most 15 words.",
    ]
    return "\n".join(lines)


def _column_line(column: ColumnMeta, values: list[str]) -> str:
    line = f"- {column.name} ({column.data_type})"
    if column.description:
        line += f": {column.description}"
    if values:
        line += f" [values: {', '.join(values)}]"
    return line


def _clean_tags(tags: list[str]) -> list[str]:
    """Lowercase, no blanks or repeats, at most five: models don't always follow the rules."""
    cleaned: list[str] = []
    for tag in tags:
        word = tag.strip().lower()
        if word and word not in cleaned:
            cleaned.append(word)
    return cleaned[:MAX_TAGS]
