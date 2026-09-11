"""
Phase 1 of the Postgres-pool reliability brief: prove the retry-once-on-stale
layer in database.py.

No live Postgres and no test.db - the pool, connection and cursor are all
hand-built fakes, and database.get_pg_pool is patched to return the fake pool.
Covers: _run_pooled_pg (retry once, propagate a non-stale error unchanged,
give up after a second stale failure, retry a transient getconn failure),
_is_stale_pg_connection_error (the type+message match), _getconn_pg_checked
(skip a connection psycopg2 already flagged closed), and the same behaviour
end-to-end through execute_query_pg.
"""

import unittest
from collections import deque
from unittest.mock import patch

import psycopg2

import database


# --- Fakes ----------------------------------------------------------------

class FakeCursor:
    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, query, params=None):
        self._conn.executed.append((query, params))
        err = self._conn.pop_execute_error()
        if err is not None:
            raise err

    def fetchall(self):
        return list(self._conn.rows)

    def fetchone(self):
        if self._conn.fetchone_rows is not None:
            return self._conn.fetchone_rows.pop(0) if self._conn.fetchone_rows else None
        return self._conn.rows[0] if self._conn.rows else None

    def close(self):
        return None

    @property
    def description(self):
        return self._conn.description


class FakeConn:
    def __init__(self, rows=None, execute_errors=None, closed=0, description=None, fetchone_rows=None):
        self.rows = [("ok",)] if rows is None else rows
        self.fetchone_rows = list(fetchone_rows) if fetchone_rows is not None else None
        self._execute_errors = list(execute_errors or [])
        self.closed = closed
        self.description = description
        self.executed = []
        self.commits = 0
        self.rollbacks = 0

    def pop_execute_error(self):
        return self._execute_errors.pop(0) if self._execute_errors else None

    def cursor(self, cursor_factory=None):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class FakePool:
    """Serves connections (or raises a queued exception) in order; records every
    getconn / putconn so a test can assert the retry actually happened."""

    def __init__(self, items):
        self._items = deque(items)
        self.getconn_calls = 0
        self.putconn_calls = []  # list of (conn, close)

    def getconn(self):
        self.getconn_calls += 1
        if not self._items:
            raise AssertionError("getconn called more times than the test queued")
        item = self._items.popleft()
        if isinstance(item, BaseException):
            raise item
        return item

    def putconn(self, conn, close=False):
        self.putconn_calls.append((conn, close))


def _op(conn):
    """Stand-in for execute_query_pg/execute_write_pg's inner _run(conn)."""
    with conn.cursor() as cur:
        cur.execute("SELECT 1")
        return "RESULT"


class RunPooledPgTests(unittest.TestCase):
    def test_stale_on_first_borrow_retries_once_and_succeeds(self):
        dead = FakeConn(execute_errors=[psycopg2.OperationalError(
            "server closed the connection unexpectedly")])
        good = FakeConn()
        pool = FakePool([dead, good])

        with patch.object(database, "get_pg_pool", return_value=pool):
            with patch("builtins.print") as printed:
                result = database._run_pooled_pg(_op)

        self.assertEqual(result, "RESULT")
        self.assertEqual(pool.getconn_calls, 2)
        self.assertIn((dead, True), pool.putconn_calls)   # discarded, not re-pooled
        self.assertIn((good, False), pool.putconn_calls)  # returned normally
        self.assertEqual(len(good.executed), 1)           # operation ran on the fresh conn
        # the recovery is announced in the server log (Phase 1's only visible signal)
        self.assertTrue(any("retried OK" in str(c) for c in printed.call_args_list))

    def test_non_stale_error_propagates_unchanged_without_retry(self):
        bad = FakeConn(execute_errors=[psycopg2.ProgrammingError("syntax error at or near")])
        pool = FakePool([bad])

        with patch.object(database, "get_pg_pool", return_value=pool):
            with patch("builtins.print") as printed:
                with self.assertRaises(psycopg2.ProgrammingError):
                    database._run_pooled_pg(_op)

        self.assertEqual(pool.getconn_calls, 1)                 # no retry
        self.assertEqual(pool.putconn_calls, [(bad, False)])    # rolled back and returned, not closed
        self.assertEqual(bad.rollbacks, 1)
        printed.assert_not_called()                             # nothing to announce

    def test_stale_on_both_attempts_gives_up_after_exactly_one_retry(self):
        d1 = FakeConn(execute_errors=[psycopg2.OperationalError(
            "server closed the connection unexpectedly")])
        d2 = FakeConn(execute_errors=[psycopg2.InterfaceError("connection already closed")])
        pool = FakePool([d1, d2])

        with patch.object(database, "get_pg_pool", return_value=pool):
            with patch("builtins.print") as printed:
                with self.assertRaises(psycopg2.InterfaceError):
                    database._run_pooled_pg(_op)

        self.assertEqual(pool.getconn_calls, 2)
        self.assertEqual(pool.putconn_calls, [(d1, True), (d2, True)])
        self.assertTrue(any("both attempts" in str(c) for c in printed.call_args_list))

    def test_transient_getconn_failure_is_retried(self):
        good = FakeConn()
        pool = FakePool([psycopg2.OperationalError("could not connect to server"), good])

        with patch.object(database, "get_pg_pool", return_value=pool):
            with patch("builtins.print") as printed:
                result = database._run_pooled_pg(_op)

        self.assertEqual(result, "RESULT")
        self.assertEqual(pool.getconn_calls, 2)
        self.assertEqual(pool.putconn_calls, [(good, False)])
        self.assertTrue(any("retried OK" in str(c) for c in printed.call_args_list))


class IsStalePgConnectionErrorTests(unittest.TestCase):
    def test_matches_known_stale_conditions(self):
        for exc in (
            psycopg2.OperationalError("server closed the connection unexpectedly"),
            psycopg2.InterfaceError("connection already closed"),
            psycopg2.OperationalError("could not connect to server: Connection refused"),
            psycopg2.OperationalError("terminating connection due to administrator command"),
        ):
            self.assertTrue(database._is_stale_pg_connection_error(exc), exc)

    def test_rejects_unrelated_errors(self):
        for exc in (
            psycopg2.OperationalError("deadlock detected"),
            psycopg2.OperationalError("FATAL: sorry, too many clients already"),
            psycopg2.ProgrammingError("syntax error at or near"),
            psycopg2.IntegrityError("duplicate key value violates unique constraint"),
            ValueError("not a psycopg2 error at all"),
        ):
            self.assertFalse(database._is_stale_pg_connection_error(exc), exc)


class GetconnPgCheckedTests(unittest.TestCase):
    def test_skips_a_connection_already_flagged_closed(self):
        closed_conn = FakeConn(closed=1)
        open_conn = FakeConn(closed=0)
        pool = FakePool([closed_conn, open_conn])

        with patch.object(database, "get_pg_pool", return_value=pool):
            with patch("builtins.print") as printed:
                got = database._getconn_pg_checked()

        self.assertIs(got, open_conn)
        self.assertEqual(pool.getconn_calls, 2)
        self.assertEqual(pool.putconn_calls, [(closed_conn, True)])
        self.assertTrue(any("closed pooled connection" in str(c) for c in printed.call_args_list))

    def test_returns_a_healthy_connection_untouched(self):
        open_conn = FakeConn(closed=0)
        pool = FakePool([open_conn])

        with patch.object(database, "get_pg_pool", return_value=pool):
            with patch("builtins.print") as printed:
                got = database._getconn_pg_checked()

        self.assertIs(got, open_conn)
        self.assertEqual(pool.getconn_calls, 1)
        self.assertEqual(pool.putconn_calls, [])
        printed.assert_not_called()


class ExecuteQueryPgWiringTests(unittest.TestCase):
    def test_execute_query_pg_retries_once_through_the_real_helper(self):
        dead = FakeConn(execute_errors=[psycopg2.OperationalError(
            "server closed the connection unexpectedly")])
        good = FakeConn(rows=[{"n": 1}])
        pool = FakePool([dead, good])

        with patch.object(database, "get_pg_pool", return_value=pool):
            rows = database.execute_query_pg("SELECT 1 AS n")

        self.assertEqual(rows, [{"n": 1}])
        self.assertEqual(pool.getconn_calls, 2)
        self.assertIn((dead, True), pool.putconn_calls)


class PostgresSchemaBootstrapTests(unittest.TestCase):
    def test_form_field_column_migration_adds_missing_columns(self):
        conn = FakeConn(rows=[("id",), ("document_id",), ("field_name",)])

        migrated = database._ensure_postgres_form_field_columns(conn)

        self.assertEqual(migrated, ["field_type", "field_scope", "linked_column"])
        self.assertIn("ADD COLUMN IF NOT EXISTS field_type TEXT NOT NULL DEFAULT 'text'", conn.executed[-1][0])
        self.assertIn("ADD COLUMN IF NOT EXISTS field_scope TEXT NOT NULL DEFAULT 'header'", conn.executed[-1][0])
        self.assertIn("ADD COLUMN IF NOT EXISTS linked_column TEXT", conn.executed[-1][0])
        self.assertEqual(conn.rollbacks, 0)

    def test_form_field_column_migration_is_noop_when_columns_exist(self):
        conn = FakeConn(rows=[
            ("id",), ("document_id",), ("field_name",),
            ("field_type",), ("field_scope",), ("linked_column",),
        ])

        self.assertEqual(database._ensure_postgres_form_field_columns(conn), [])
        self.assertEqual(len(conn.executed), 1)
        self.assertEqual(conn.rollbacks, 0)

    def test_form_table_migration_creates_missing_tables(self):
        conn = FakeConn(fetchone_rows=[(None, None)])

        migrated = database._ensure_postgres_form_tables(conn)

        self.assertEqual(
            migrated,
            ["voucher_line_form_entries", "voucher_line_form_values"],
        )
        self.assertIn(
            "CREATE TABLE IF NOT EXISTS voucher_line_form_entries",
            conn.executed[1][0],
        )
        self.assertIn(
            "CREATE TABLE IF NOT EXISTS voucher_line_form_values",
            conn.executed[3][0],
        )
        self.assertEqual(conn.rollbacks, 0)

    def test_form_table_migration_is_noop_when_tables_exist(self):
        conn = FakeConn(fetchone_rows=[
            ("voucher_line_form_entries", "voucher_line_form_values"),
        ])

        self.assertEqual(database._ensure_postgres_form_tables(conn), [])
        self.assertEqual(len(conn.executed), 1)
        self.assertEqual(conn.rollbacks, 0)

    def test_completed_schema_marker_skips_repeated_bootstrap(self):
        conn = FakeConn(
            rows=[("field_type",), ("field_scope",), ("linked_column",)],
            fetchone_rows=[
                ("audit_rules_config_state",),
                ("CHECK ((document_type = ANY (ARRAY['linked_template'::text, 'form'::text, 'physical_bill'::text])))",),
                ("voucher_line_form_entries", "voucher_line_form_values"),
            ],
        )

        with patch.object(database, "_POSTGRES_SCHEMA_READY", False):
            with patch("builtins.print") as printed:
                database.init_db_postgres(conn)

        self.assertEqual(conn.executed[:4], [
            ("SELECT to_regclass('public.audit_rules_config_state')", None),
            ("\n            SELECT pg_get_constraintdef(oid)\n            FROM pg_constraint\n            WHERE conrelid = 'public.audit_rule_documents'::regclass\n              AND conname = 'audit_rule_documents_document_type_check'\n        ", None),
            ("\n            SELECT column_name\n            FROM information_schema.columns\n            WHERE table_schema = 'public'\n              AND table_name = 'audit_rule_document_fields'\n        ", None),
            ("SELECT to_regclass('public.voucher_line_form_entries'), to_regclass('public.voucher_line_form_values')", None),
        ])
        self.assertEqual(len(conn.executed), 11)
        self.assertTrue(any("CREATE TABLE IF NOT EXISTS report_category_sets" in sql for sql, _ in conn.executed))
        self.assertTrue(any("CREATE TABLE IF NOT EXISTS report_categories" in sql for sql, _ in conn.executed))
        self.assertTrue(any("CREATE TABLE IF NOT EXISTS report_account_category_assignments" in sql for sql, _ in conn.executed))
        self.assertEqual(conn.commits, 1)
        printed.assert_called_once_with(
            "[postgres] existing schema verified; skipped bootstrap"
        )


if __name__ == "__main__":
    unittest.main()
