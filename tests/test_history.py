from backend.agent.history import (
    clean_history,
    is_openwebui_task,
    last_sql,
    latest_user_text,
    message_text,
)

PREVIOUS_ANSWER = """> Finding the right tables…
> Running the query…

Paid orders grew 12% in March.

| month | orders |
|---|---|
| 2026-02 | 1,200 |
| 2026-03 | 1,344 |

[⬇️ Download CSV (2 rows)](/downloads/0123456789abcdef0123456789abcdef.csv)

![Orders by month](/downloads/fedcba9876543210fedcba9876543210.png)

_Assumed: paid orders only._

<details>
<summary>Show SQL</summary>

```sql
SELECT month, COUNT(*) AS orders FROM `p.sales.orders` GROUP BY month
```

</details>
"""

CHAT = [
    {"role": "system", "content": "You are helpful."},
    {"role": "user", "content": "Orders per month?"},
    {"role": "assistant", "content": PREVIOUS_ANSWER},
    {"role": "user", "content": "Now only Delhi"},
]


def test_message_text_reads_strings_and_parts():
    assert message_text({"content": "hi"}) == "hi"
    parts = [
        {"type": "text", "text": "show"},
        {"type": "image_url"},
        {"type": "text", "text": "it"},
    ]
    assert message_text({"content": parts}) == "show\nit"
    assert message_text({"content": None}) == ""


def test_latest_user_text():
    assert latest_user_text(CHAT) == "Now only Delhi"
    assert latest_user_text([]) == ""


def test_last_sql_comes_from_the_show_sql_block():
    assert last_sql(CHAT) == "SELECT month, COUNT(*) AS orders FROM `p.sales.orders` GROUP BY month"
    assert last_sql([{"role": "user", "content": "hello"}]) is None


def test_answers_with_the_old_one_line_block_still_work():
    # Answers from before the layout fix have <details><summary> on one line.
    old_answer = PREVIOUS_ANSWER.replace("<details>\n<summary>", "<details><summary>")
    assert "<details><summary>Show SQL</summary>" in old_answer
    chat = [{"role": "assistant", "content": old_answer}]

    assert last_sql(chat) == "SELECT month, COUNT(*) AS orders FROM `p.sales.orders` GROUP BY month"
    assert "<details>" not in clean_history(chat)[0]["content"]


def test_clean_history_keeps_words_and_sql_only():
    history = clean_history(CHAT)

    assert [m["role"] for m in history] == ["user", "assistant"]  # no system, no latest question
    answer = history[1]["content"]
    assert "Paid orders grew 12% in March." in answer
    assert "_Assumed: paid orders only._" in answer
    assert "Previous SQL:\n```sql\nSELECT month" in answer
    for noise in ["| month |", "/downloads/", "Finding the right tables", "<details>"]:
        assert noise not in answer


def test_clean_history_keeps_only_recent_turns():
    chat = [{"role": "user", "content": f"q{i}"} for i in range(30)]
    assert len(clean_history(chat, max_messages=4)) == 4


TITLE_TASK = """### Task:
Generate a concise, 3-5 word title with an emoji summarizing the chat history.
### Chat History:
<chat_history>
USER: Orders per month?
</chat_history>"""

# Open WebUI's RAG template, sent when the user attaches a file: a real question.
RAG_PROMPT = """### Task:
Respond to the user query using the provided context.

<context>
<source id="1">targets.pdf: March target is 1,300 orders.</source>
</context>

<user_query>
Did we hit the March target?
</user_query>"""


def test_openwebui_task_prompts_are_detected():
    assert is_openwebui_task(TITLE_TASK)
    assert is_openwebui_task("\n  " + TITLE_TASK)


def test_questions_are_not_openwebui_tasks():
    assert not is_openwebui_task("What is the revenue by month?")
    assert not is_openwebui_task(RAG_PROMPT)
    # The tag alone isn't enough: the prompt must start with the task marker.
    assert not is_openwebui_task("Why does <chat_history> show up in my export?")
