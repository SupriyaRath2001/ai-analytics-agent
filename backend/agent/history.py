"""Turn the chat history Open WebUI sends into something small and useful for the LLM.

Our earlier answers contain tables, download links, images and progress lines. To understand a
follow-up like "now split that by month", the model only needs the text and the last SQL.
Nothing is stored on our side: the history in each request is the only memory.
"""

from __future__ import annotations

import re

# Open WebUI sends its own background jobs (chat titles, tags, follow-up suggestions) to the
# selected model. Their prompts start with this marker.
OPENWEBUI_TASK_PREFIX = "### Task:"

_SQL_BLOCK = re.compile(r"```sql[ \t]*\n(.*?)```", re.DOTALL | re.IGNORECASE)
_DETAILS_BLOCK = re.compile(r"<details>.*?</details>", re.DOTALL | re.IGNORECASE)
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")


def message_text(message: dict) -> str:
    """Return the text of a chat message. Content may be a string or a list of parts."""
    content = message.get("content") or ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = [
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        ]
        return "\n".join(text for text in texts if text)
    return str(content)


def latest_user_text(messages: list[dict]) -> str:
    for message in reversed(messages):
        if message.get("role") == "user":
            return message_text(message).strip()
    return ""


def last_sql(messages: list[dict]) -> str | None:
    """The SQL of the most recent answer that ran a query, if any."""
    for message in reversed(messages):
        if message.get("role") != "assistant":
            continue
        blocks = _SQL_BLOCK.findall(message_text(message))
        if blocks:
            return blocks[-1].strip()
    return None


def clean_history(messages: list[dict], max_messages: int = 10) -> list[dict]:
    """Earlier turns only (the latest user message is handled separately), slimmed down."""
    turns = [m for m in messages if m.get("role") in ("user", "assistant")]
    if turns and turns[-1].get("role") == "user":
        turns = turns[:-1]

    cleaned = []
    for message in turns[-max_messages:]:
        text = message_text(message)
        if message["role"] == "assistant":
            text = _slim_answer(text)
        text = text.strip()
        if text:
            cleaned.append({"role": message["role"], "content": text})
    return cleaned


def is_openwebui_task(text: str) -> bool:
    return text.lstrip().startswith(OPENWEBUI_TASK_PREFIX)


def _slim_answer(text: str) -> str:
    """Drop tables, images, download links and progress lines; keep the words and the SQL."""
    sql_blocks = _SQL_BLOCK.findall(text)
    text = _DETAILS_BLOCK.sub("", text)
    text = _SQL_BLOCK.sub("", text)

    kept = []
    for line in text.splitlines():
        if _TABLE_ROW.match(line) or line.lstrip().startswith(">") or "/downloads/" in line:
            continue
        kept.append(_IMAGE.sub("", line))

    slim = re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()
    if sql_blocks:
        slim += f"\n\nPrevious SQL:\n```sql\n{sql_blocks[-1].strip()}\n```"
    return slim
