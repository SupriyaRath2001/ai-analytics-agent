"""The SQL loop of a data answer: write the query, check it, run it, and fix it when rejected.

The smart model writes the SQL. When the dry run or BigQuery rejects it, the exact error goes
back to the model so it can correct the query, at most MAX_RETRIES times for both kinds of error
together.
"""

from __future__ import annotations

import logging
from collections.abc import Generator
from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from backend.agent import prompts
from backend.connectors.base import BaseDBConnector, QueryError, QueryResult
from backend.llm import LLMClient

log = logging.getLogger(__name__)

MAX_RETRIES = 2


class SqlDraft(BaseModel):
    """What the smart model returns: a query, or a question for the user instead."""

    sql: str | None = None  # null when the model asks a question
    assumptions: list[str] = Field(default_factory=list)
    clarifying_question: str | None = None


@dataclass
class SqlOutcome:
    """How the loop ended: with a result, with a question for the user, or with the last error."""

    sql: str = ""  # the last query written
    assumptions: list[str] = field(default_factory=list)
    result: QueryResult | None = None
    clarifying_question: str = ""
    error: str = ""


def write_and_run(
    llm: LLMClient, connector: BaseDBConnector, chat: list[dict]
) -> Generator[str, None, SqlOutcome]:
    """Yield progress lines while the SQL is written, checked and run, then return the outcome.

    Use it as ``outcome = yield from write_and_run(...)``, so the progress lines reach the chat
    while the work goes on. ``chat`` is the SQL writer prompt from ``prompts.sql_messages``.
    """
    attempts = 1 + MAX_RETRIES
    sql = error = ""
    for attempt in range(1, attempts + 1):
        yield "> Writing the SQL…\n" if attempt == 1 else "> Fixing the query…\n"
        draft = llm.complete_json(chat, SqlDraft, tier="smart")
        question = (draft.clarifying_question or "").strip()
        if question:
            return SqlOutcome(clarifying_question=question)

        sql = (draft.sql or "").strip()
        yield "> Checking the query…\n"
        ok, message = connector.dry_run_or_validate(sql)
        if ok:
            log.info("query check passed: %s", message)
            yield "> Running the query…\n"
            try:
                result = connector.execute_query(sql)
            except QueryError as exc:
                message = str(exc)
            else:
                return SqlOutcome(sql=sql, assumptions=draft.assumptions, result=result)

        error = message
        log.info("query attempt %d of %d was rejected: %s", attempt, attempts, error)
        chat = [
            *chat,
            {"role": "assistant", "content": draft.model_dump_json()},
            {"role": "user", "content": prompts.sql_fix_request(error)},
        ]
    return SqlOutcome(sql=sql, error=error)
