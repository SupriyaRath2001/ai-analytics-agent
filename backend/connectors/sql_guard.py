"""An offline read-only check for SQL written by the model.

This runs before anything reaches the database. It is the first of two safety
layers: the connector's dry run is the second, where the database itself reports
the statement type and the tables a query reads.
"""

from __future__ import annotations

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError, SqlglotError

# Nodes that write data, change the schema or take locks. The whole tree is
# searched, not only the top, because some dialects allow DML inside a CTE:
# WITH gone AS (DELETE FROM t RETURNING *) SELECT * FROM gone
_WRITE_OR_LOCK_NODES: dict[type[exp.Expression], str] = {
    exp.Insert: "INSERT",
    exp.Update: "UPDATE",
    exp.Delete: "DELETE",
    exp.Merge: "MERGE",
    exp.Create: "CREATE",
    exp.Drop: "DROP",
    exp.Alter: "ALTER",
    exp.TruncateTable: "TRUNCATE",
    exp.Into: "SELECT ... INTO",
    exp.Lock: "FOR UPDATE / FOR SHARE",
}

# MySQL functions that can stall the server or read files from it.
_BLOCKED_FUNCTIONS = {"sleep", "benchmark", "load_file"}

_ONLY_SELECT = "Only a single read-only SELECT query can run (WITH ... SELECT is fine)."


def check_read_only(sql: str, dialect: str) -> tuple[bool, str]:
    """(True, "") when sql is exactly one read-only SELECT/WITH query, else (False, reason)."""
    try:
        parsed = sqlglot.parse(sql, read=dialect)
    except SqlglotError as exc:
        return False, f"The SQL could not be parsed: {_parse_error_detail(exc)}. Fix the syntax."

    # Extra semicolons and comments after the last one come back as None or Semicolon.
    statements = [s for s in parsed if s is not None and not isinstance(s, exp.Semicolon)]
    if not statements:
        return False, f"No SQL statement found. {_ONLY_SELECT}"
    if len(statements) > 1:
        return False, f"Found {len(statements)} statements. {_ONLY_SELECT}"

    tree = statements[0]
    blocked = _write_or_lock(tree)
    if blocked:
        return False, f"{blocked} is not allowed. {_ONLY_SELECT}"
    if not isinstance(tree, exp.Query):
        return False, _ONLY_SELECT
    for function in tree.find_all(exp.Func):
        name = _function_name(function)
        if name in _BLOCKED_FUNCTIONS:
            return False, f"The function {name.upper()}() is not allowed. Remove it."
    return True, ""


def _write_or_lock(tree: exp.Expression) -> str:
    """The name of the first node that writes or locks, or "" when there is none."""
    for node in tree.walk():
        if isinstance(node, exp.Command):
            # sqlglot keeps statements it doesn't model (CALL, EXECUTE IMMEDIATE, ...)
            # as raw commands; the name is the leading keyword.
            return node.name.upper()
        for node_type, label in _WRITE_OR_LOCK_NODES.items():
            if isinstance(node, node_type):
                return label
    return ""


def _function_name(function: exp.Func) -> str:
    # Functions sqlglot doesn't know keep the name as written; known ones have a canonical name.
    if isinstance(function, exp.Anonymous):
        return function.name.lower()
    return function.sql_name().lower()


def _parse_error_detail(exc: SqlglotError) -> str:
    """sqlglot's first error, without the SQL excerpt and terminal colour codes it adds."""
    if isinstance(exc, ParseError) and exc.errors:
        error = exc.errors[0]
        return f"{error['description']} (line {error['line']}, column {error['col']})"
    return str(exc)
