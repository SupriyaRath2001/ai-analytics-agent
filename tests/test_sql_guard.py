import pytest

from backend.connectors.sql_guard import check_read_only

READ_ONLY = {
    "plain select": "SELECT 1",
    "trailing semicolons and whitespace": "  SELECT 1 ;  ;\n",
    "comment after the semicolon": "SELECT 1;\n-- assumes paid orders only",
    "with": "WITH paid AS (SELECT * FROM `p.sales.orders` WHERE paid) SELECT COUNT(*) FROM paid",
    "union": "SELECT 1 UNION ALL SELECT 2",
    "parenthesised": "(SELECT 1)",
    "unnest": "SELECT o.id, item.sku FROM `p.sales.orders` AS o, UNNEST(o.items) AS item",
    "time travel is not a lock": (
        "SELECT * FROM `p.sales.orders` "
        "FOR SYSTEM_TIME AS OF TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 1 HOUR)"
    ),
    "keywords inside names": "SELECT insert_date, `update`, sleep_minutes FROM t",
    "write hidden in a comment": "SELECT 1 -- ; DROP TABLE t",
}


@pytest.mark.parametrize("sql", READ_ONLY.values(), ids=READ_ONLY.keys())
def test_read_only_queries_pass(sql):
    assert check_read_only(sql, "bigquery") == (True, "")


REJECTED = {
    "insert": ("INSERT INTO t (a) VALUES (1)", "bigquery", "INSERT is not allowed"),
    "update": ("UPDATE t SET a = 1 WHERE true", "bigquery", "UPDATE is not allowed"),
    "delete": ("DELETE FROM t WHERE true", "bigquery", "DELETE is not allowed"),
    "merge": (
        "MERGE t USING s ON t.id = s.id WHEN MATCHED THEN DELETE",
        "bigquery",
        "MERGE is not allowed",
    ),
    "create": ("CREATE TABLE t AS SELECT 1 AS a", "bigquery", "CREATE is not allowed"),
    "drop": ("DROP TABLE t", "bigquery", "DROP is not allowed"),
    "alter": ("ALTER TABLE t ADD COLUMN b INT64", "bigquery", "ALTER is not allowed"),
    "truncate": ("TRUNCATE TABLE t", "bigquery", "TRUNCATE is not allowed"),
    "call": ("CALL `p.d.proc`()", "bigquery", "CALL is not allowed"),
    "execute immediate": ("EXECUTE IMMEDIATE 'DROP TABLE t'", "bigquery", "EXECUTE is not allowed"),
    "dml inside a cte": (
        "WITH gone AS (DELETE FROM t RETURNING *) SELECT * FROM gone",
        "postgres",
        "DELETE is not allowed",
    ),
    "select into": ("SELECT a INTO @v FROM t", "mysql", "SELECT ... INTO is not allowed"),
    "for update": ("SELECT * FROM t FOR UPDATE", "mysql", "FOR UPDATE"),
    "lock in a subquery": (
        "SELECT a FROM t WHERE a IN (SELECT b FROM u LOCK IN SHARE MODE)",
        "mysql",
        "FOR UPDATE / FOR SHARE is not allowed",
    ),
    "sleep": ("SELECT SLEEP(5)", "mysql", "SLEEP() is not allowed"),
    "benchmark": ("SELECT BENCHMARK(1000000, MD5('a'))", "mysql", "BENCHMARK() is not allowed"),
    "load_file": ("SELECT LOAD_FILE('/etc/passwd')", "mysql", "LOAD_FILE() is not allowed"),
    "not a query": ("SHOW TABLES", "mysql", "Only a single read-only SELECT"),
    "export": (
        "EXPORT DATA OPTIONS(uri='gs://b/*.csv', format='CSV') AS SELECT 1",
        "bigquery",
        "Only a single read-only SELECT",
    ),
    "two statements": ("SELECT 1; SELECT 2", "bigquery", "Found 2 statements"),
    "select then drop": ("SELECT 1; DROP TABLE t", "bigquery", "Found 2 statements"),
    "empty": ("", "bigquery", "No SQL statement"),
    "only semicolons and comments": (" ; -- nothing here", "bigquery", "No SQL statement"),
    "syntax error": ("SELEC 1", "bigquery", "could not be parsed"),
    "unterminated string": ("SELECT 'abc", "bigquery", "could not be parsed"),
}


@pytest.mark.parametrize(("sql", "dialect", "reason"), REJECTED.values(), ids=REJECTED.keys())
def test_unsafe_or_invalid_sql_is_rejected(sql, dialect, reason):
    ok, message = check_read_only(sql, dialect)

    assert not ok
    assert reason in message


def test_parse_errors_are_readable():
    ok, message = check_read_only("SELECT (1", "bigquery")

    assert not ok
    assert message == "The SQL could not be parsed: Expecting ) (line 1, column 9). Fix the syntax."
