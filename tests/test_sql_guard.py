import tomllib
from pathlib import Path

import pytest
import sqlglot

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
    "sample rows": "SELECT * FROM `p.sales.orders` TABLESAMPLE SYSTEM (1 PERCENT) LIMIT 10",
    "information_schema only in text and names": (
        "SELECT 'INFORMATION_SCHEMA.TABLES' AS note, information_schema "
        "FROM `p.sales.information_schema_backup`"
    ),
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


INFORMATION_SCHEMA = {
    "region, backticked": "SELECT * FROM `region-us`.INFORMATION_SCHEMA.TABLES",
    "region, plain": "SELECT * FROM region-us.INFORMATION_SCHEMA.TABLES",
    "whole path in backticks": "SELECT * FROM `my-project.region-us.INFORMATION_SCHEMA.JOBS`",
    "dataset, plain": "SELECT * FROM hr.INFORMATION_SCHEMA.COLUMNS",
    "every part backticked": "SELECT * FROM `my-project`.`hr`.`INFORMATION_SCHEMA`.`TABLES`",
    "lower case": "SELECT table_name FROM hr.information_schema.tables",
    "mixed case": "SELECT * FROM `my-project.hr.Information_Schema.Columns`",
    "no qualifier": "SELECT * FROM INFORMATION_SCHEMA.SCHEMATA",
    "in a join": (
        "SELECT o.id FROM `p.sales.orders` AS o JOIN hr.INFORMATION_SCHEMA.TABLES AS t ON TRUE"
    ),
    "in a cte": "WITH t AS (SELECT * FROM `p`.hr.INFORMATION_SCHEMA.TABLES) SELECT * FROM t",
    "in a subquery": (
        "SELECT * FROM `p.sales.orders` WHERE status IN "
        "(SELECT table_name FROM `region-eu.INFORMATION_SCHEMA.TABLES`)"
    ),
}


@pytest.mark.parametrize("sql", INFORMATION_SCHEMA.values(), ids=INFORMATION_SCHEMA.keys())
def test_information_schema_is_rejected(sql):
    # A dry run doesn't list these views among the tables read, so the dataset allowlist
    # would never see them.
    ok, message = check_read_only(sql, "bigquery")

    assert not ok
    assert "metadata questions are answered from the catalog" in message


EXTERNAL_QUERIES = {
    "plain": "SELECT * FROM EXTERNAL_QUERY('my-project.us.crm', 'SELECT * FROM customers')",
    "lower case": 'SELECT * FROM external_query("my-project.us.crm", "SELECT 1")',
    "backticked": "SELECT * FROM `EXTERNAL_QUERY`('crm', 'SELECT 1')",
    "in a subquery": (
        "SELECT id FROM `p.sales.orders` WHERE id IN "
        "(SELECT id FROM EXTERNAL_QUERY('crm', 'SELECT id FROM vip'))"
    ),
}


@pytest.mark.parametrize("sql", EXTERNAL_QUERIES.values(), ids=EXTERNAL_QUERIES.keys())
def test_external_query_is_rejected(sql):
    ok, message = check_read_only(sql, "bigquery")

    assert not ok
    assert "federated queries aren't allowed" in message


def test_sqlglot_is_pinned_to_the_major_version_the_guard_is_tested_on():
    # The guard matches sqlglot's expression classes (exp.Insert, exp.Command, ...), which can
    # change between major versions.
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    dependencies = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["dependencies"]

    assert "sqlglot>=30,<31" in dependencies
    assert sqlglot.__version__.split(".")[0] == "30"


def test_parse_errors_are_readable():
    ok, message = check_read_only("SELECT (1", "bigquery")

    assert not ok
    assert message == "The SQL could not be parsed: Expecting ) (line 1, column 9). Fix the syntax."
