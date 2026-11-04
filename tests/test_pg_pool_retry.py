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
from contextlib import ExitStack
from unittest.mock import patch

import psycopg2

import database


# --- Fakes ----------------------------------------------------------------

class FakeCursor:
    def __init__(self, conn):
        self._conn = conn
        self._position = 0
        self._returns_rows = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, query, params=None):
        self._conn.executed.append((query, params))
        sql = (query.decode() if isinstance(query, bytes) else query).strip().upper()
        self._returns_rows = sql.startswith(('SELECT', 'WITH', 'SHOW', 'EXPLAIN')) or ' RETURNING ' in sql
        err = self._conn.pop_execute_error()
        if err is not None:
            raise err

    def fetchall(self):
        if self.description is None:
            raise psycopg2.ProgrammingError('no results to fetch')
        return list(self._conn.rows)

    def fetchmany(self, size):
        self._conn.fetch_sizes.append(size)
        error = self._conn.fetch_errors.pop(0) if self._conn.fetch_errors else None
        if error is not None:
            raise error
        rows = self._conn.rows[self._position:self._position + size]
        self._position += len(rows)
        return rows

    def fetchone(self):
        if self._conn.fetchone_rows is not None:
            return self._conn.fetchone_rows.pop(0) if self._conn.fetchone_rows else None
        return self._conn.rows[0] if self._conn.rows else None

    def close(self):
        return None

    @property
    def description(self):
        return (self._conn.description or [('result',)]) if self._returns_rows else None


class FakeConn:
    def __init__(self, rows=None, execute_errors=None, closed=0, description=None, fetchone_rows=None,
                 fetch_errors=None, commit_error=None):
        self.rows = [("ok",)] if rows is None else rows
        self.fetchone_rows = list(fetchone_rows) if fetchone_rows is not None else None
        self._execute_errors = list(execute_errors or [])
        self.closed = closed
        self.description = description
        self.executed = []
        self.commits = 0
        self.rollbacks = 0
        self.fetch_errors = list(fetch_errors or [])
        self.fetch_sizes = []
        self.cursor_names = []
        self.commit_error = commit_error

    def pop_execute_error(self):
        return self._execute_errors.pop(0) if self._execute_errors else None

    def cursor(self, cursor_factory=None, name=None):
        self.cursor_names.append(name)
        return FakeCursor(self)

    def commit(self):
        self.commits += 1
        if self.commit_error:
            raise self.commit_error

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
    def test_upload_lifecycle_locks_do_not_fetch_results_or_release_the_outer_transaction(self):
        from services import upload_lifecycle
        conn = FakeConn(rows=[{'status':'archiving'}])
        pool = FakePool([conn])
        prepared = {'operation_id':'test-operation', 'ids':[], 'lifecycle':{'scope':'test'}}
        with patch.object(database, 'get_pg_pool', return_value=pool), \
                patch.object(upload_lifecycle, 'check_scope') as check:
            with database.pg_transaction():
                upload_lifecycle.guard_lifecycle(prepared, database.execute_query_pg)
                self.assertEqual((conn.commits, conn.rollbacks), (0, 0))
                self.assertEqual(pool.putconn_calls, [])
            check.assert_called_once_with(prepared['lifecycle'], database.execute_query_pg)
        self.assertEqual([sql for sql, _ in conn.executed[:2]], [
            'LOCK TABLE client_data IN SHARE ROW EXCLUSIVE MODE',
            'LOCK TABLE data_audit_log IN SHARE ROW EXCLUSIVE MODE'])
        self.assertEqual((conn.commits, conn.rollbacks), (1, 0))

    def test_lock_failure_still_propagates_and_rolls_back_without_retry(self):
        error = psycopg2.OperationalError('canceling statement due to lock timeout')
        conn = FakeConn(execute_errors=[error])
        pool = FakePool([conn])
        with patch.object(database, 'get_pg_pool', return_value=pool):
            with self.assertRaises(psycopg2.OperationalError) as caught:
                with database.pg_transaction():
                    database.execute_query_pg('LOCK TABLE client_data IN SHARE ROW EXCLUSIVE MODE')
        self.assertIs(caught.exception, error)
        self.assertEqual((conn.commits, conn.rollbacks, pool.getconn_calls), (0, 1, 1))

    def test_empty_select_has_a_result_set_and_still_fetches(self):
        conn = FakeConn(rows=[])
        with patch.object(database, 'get_pg_pool', return_value=FakePool([conn])):
            self.assertEqual(database.execute_query_pg('SELECT id FROM example WHERE FALSE'), [])

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


class PagedReadSnapshotTests(unittest.TestCase):
    def test_actual_comparison_keeps_repeated_and_zero_lines_across_pages(self):
        from routers import import_export as uploads

        columns = [{"id": 1, "column_name": "Voucher", "data_type": "Text"},
                   {"id": 2, "column_name": "Amount", "data_type": "Numeric"}]
        incoming = [{"Voucher": "V1", "Amount": 100}, {"Voucher": "V1", "Amount": 100},
                    {"Voucher": "V2", "Amount": 0}]
        conn = FakeConn(rows=[{"id": n, "row_data": row, "source_identity": f"source-{n}",
                               "version": 4, "validation_status": "Verified"}
                              for n, row in enumerate(incoming, 10)])
        with patch.object(database, "get_pg_pool", return_value=FakePool([conn])), \
                patch.object(uploads, "UPLOAD_COMPARISON_PAGE_SIZE", 2):
            result = uploads.compare_transactions(5, 1, columns, incoming,
                                                   {"Voucher": "Voucher", "Amount": "Amount"}, {})
        self.assertEqual(sorted(result["unchanged"]), [10, 11, 12])
        self.assertEqual(result["total_existing_before_import"], 3)
        self.assertEqual(result["new"] + result["changed"] + result["missing"], [])
        self.assertEqual(conn.fetch_sizes, [2, 2, 2])
        self.assertEqual(conn.executed[1][1], (5, 1))

    def test_reads_every_page_in_one_read_only_snapshot(self):
        conn = FakeConn(rows=[{"id": n} for n in range(5)])
        pool = FakePool([conn])
        with patch.object(database, "get_pg_pool", return_value=pool):
            rows = database.execute_query_pg("SELECT id FROM example ORDER BY id", page_size=2)
        self.assertEqual(rows, [{"id": n} for n in range(5)])
        self.assertEqual(conn.fetch_sizes, [2, 2, 2, 2])
        self.assertEqual(conn.cursor_names, [None, "paged_read"])
        self.assertEqual(conn.executed[0][0], "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        self.assertEqual((conn.commits, conn.rollbacks), (0, 1))
        self.assertEqual(pool.putconn_calls, [(conn, False)])

    def test_later_page_disconnect_restarts_without_reusing_partial_rows(self):
        dead = FakeConn(rows=[{"id": 999}, {"id": 998}], fetch_errors=[
            None, psycopg2.OperationalError("SSL error: unexpected eof while reading"),
        ])
        good = FakeConn(rows=[{"id": n} for n in range(5)])
        pool = FakePool([dead, good])
        with patch.object(database, "get_pg_pool", return_value=pool):
            rows = database.execute_query_pg("SELECT id FROM example ORDER BY id", page_size=2)
        self.assertEqual(rows, [{"id": n} for n in range(5)])
        self.assertEqual(pool.getconn_calls, 2)
        self.assertEqual(pool.putconn_calls, [(dead, True), (good, False)])
        self.assertEqual(good.executed[0][0], dead.executed[0][0])
        self.assertEqual((dead.commits, good.commits), (0, 0))

    def test_second_disconnect_fails_without_returning_partial_data(self):
        conns = [FakeConn(fetch_errors=[psycopg2.OperationalError(
            "SSL error: unexpected eof while reading")]) for _ in range(2)]
        pool = FakePool(conns)
        with patch.object(database, "get_pg_pool", return_value=pool):
            with self.assertRaises(psycopg2.OperationalError):
                database.execute_query_pg("SELECT id FROM example", page_size=2)
        self.assertEqual(pool.getconn_calls, 2)
        self.assertEqual(pool.putconn_calls, [(conn, True) for conn in conns])

    def test_non_connection_error_is_not_retried(self):
        conn = FakeConn(fetch_errors=[psycopg2.OperationalError("canceling statement due to statement timeout")])
        pool = FakePool([conn])
        with patch.object(database, "get_pg_pool", return_value=pool):
            with self.assertRaises(psycopg2.OperationalError):
                database.execute_query_pg("SELECT id FROM example", page_size=2)
        self.assertEqual(pool.getconn_calls, 1)
        self.assertEqual(conn.rollbacks, 1)

    def test_empty_and_exact_page_boundary_results(self):
        for count in (0, 2, 4):
            with self.subTest(count=count):
                conn = FakeConn(rows=[{"id": n} for n in range(count)])
                with patch.object(database, "get_pg_pool", return_value=FakePool([conn])):
                    result = database.execute_query_pg("SELECT id FROM example", page_size=2)
                self.assertEqual(len(result), count)
                self.assertEqual(len(conn.fetch_sizes), count // 2 + 1)

    def test_paged_read_rejects_an_active_write_transaction(self):
        token = database._pg_transaction.set(FakeConn())
        try:
            with patch.object(database, "get_pg_pool") as pool:
                with self.assertRaises(RuntimeError):
                    database.execute_query_pg("SELECT 1", page_size=2)
                pool.assert_not_called()
        finally:
            database._pg_transaction.reset(token)

    def test_invalid_page_sizes_do_not_borrow_a_connection(self):
        for size in (0, -1, True, 1.5, "2000", 10001):
            with self.subTest(size=size), patch.object(database, "get_pg_pool") as pool:
                with self.assertRaises(ValueError):
                    database.execute_query_pg("SELECT 1", page_size=size)
                pool.assert_not_called()

    def test_new_eof_retry_is_not_enabled_for_writes(self):
        conn = FakeConn(execute_errors=[psycopg2.OperationalError(
            "SSL error: unexpected eof while reading")])
        pool = FakePool([conn])
        with patch.object(database, "get_pg_pool", return_value=pool):
            with self.assertRaises(psycopg2.OperationalError):
                database.execute_write_pg("INSERT INTO example VALUES (1)")
        self.assertEqual(pool.getconn_calls, 1)
        self.assertEqual(conn.commits, 0)


class AtomicImportTransactionTests(unittest.TestCase):
    def test_failure_after_an_earlier_batch_rolls_back_without_any_commit(self):
        for helper in (database.execute_batch_pg, database.execute_batch_insert_with_ids_pg):
            with self.subTest(helper=helper.__name__):
                conn = FakeConn()
                pool = FakePool([conn])
                target = "execute_batch" if helper is database.execute_batch_pg else "execute_values"
                first_result = None if target == "execute_batch" else [(1,), (2,)]
                with patch.object(database, "get_pg_pool", return_value=pool), patch(
                    "database.psycopg2.extras." + target,
                    side_effect=[first_result, psycopg2.OperationalError("SSL error: unexpected eof while reading")],
                ) as batch:
                    with self.assertRaises(psycopg2.OperationalError):
                        with database.pg_transaction() as active:
                            helper(active, "INSERT", [(1,), (2,), (3,)], chunk_size=2)
                self.assertEqual(batch.call_count, 2)
                self.assertEqual((conn.commits, conn.rollbacks), (0, 1))
                self.assertEqual(pool.getconn_calls, 1)
                self.assertIsNone(database._pg_transaction.get())

    def test_lost_commit_acknowledgement_never_replays_the_import(self):
        conn = FakeConn(commit_error=psycopg2.OperationalError(
            "SSL error: unexpected eof while reading"))
        pool = FakePool([conn])
        with patch.object(database, "get_pg_pool", return_value=pool):
            with self.assertRaises(psycopg2.OperationalError):
                with database.pg_transaction():
                    database.execute_write_pg("INSERT INTO example VALUES (1)")
        self.assertEqual(len(conn.executed), 1)
        self.assertEqual((conn.commits, conn.rollbacks), (1, 1))
        self.assertEqual(pool.getconn_calls, 1)


class ErrorTraceEvidenceTests(unittest.TestCase):
    def test_long_trace_keeps_root_function_and_exception_within_existing_limit(self):
        trace = 'Outer middleware frame\n' * 400 + 'database.execute_query_pg -> cur.fetchall()\nProgrammingError: no results to fetch'
        with patch.object(database, 'execute_write', return_value=864) as write:
            result = database.log_error(error_message='no results to fetch', stack_trace=trace)
        self.assertEqual(result, 864)
        stored = write.call_args.args[1][13]
        self.assertEqual(len(stored), 5000)
        self.assertTrue(stored.endswith('ProgrammingError: no results to fetch'))
        self.assertIn('database.execute_query_pg', stored)

    def test_short_and_missing_traces_remain_unchanged(self):
        for trace in (None, 'Short traceback\nValueError: source'):
            with self.subTest(trace=trace), patch.object(database, 'execute_write') as write:
                database.log_error(error_message='source', stack_trace=trace)
                self.assertEqual(write.call_args.args[1][13], trace)


class PostgresSchemaBootstrapTests(unittest.TestCase):
    def test_saved_mapping_comparison_preference_migration_is_idempotent_sql(self):
        conn = FakeConn()

        database._ensure_postgres_saved_column_mappings(conn)

        self.assertEqual(conn.executed, [(
            "ALTER TABLE saved_column_mappings ADD COLUMN IF NOT EXISTS compare_for_changes BOOLEAN",
            None,
        )])
        self.assertEqual(conn.rollbacks, 0)

    def test_form_field_column_migration_adds_missing_columns(self):
        conn = FakeConn(rows=[("id",), ("document_id",), ("field_name",)])

        migrated = database._ensure_postgres_form_field_columns(conn)

        self.assertEqual(migrated, ["field_type", "field_scope", "linked_column", "arithmetic_operands"])
        self.assertIn("ADD COLUMN IF NOT EXISTS field_type TEXT NOT NULL DEFAULT 'text'", conn.executed[-1][0])
        self.assertIn("ADD COLUMN IF NOT EXISTS field_scope TEXT NOT NULL DEFAULT 'header'", conn.executed[-1][0])
        self.assertIn("ADD COLUMN IF NOT EXISTS linked_column TEXT", conn.executed[-1][0])
        self.assertIn("ADD COLUMN IF NOT EXISTS arithmetic_operands TEXT", conn.executed[-1][0])
        self.assertEqual(conn.rollbacks, 0)

    def test_form_field_column_migration_is_noop_when_columns_exist(self):
        conn = FakeConn(rows=[
            ("id",), ("document_id",), ("field_name",),
            ("field_type",), ("field_scope",), ("linked_column",), ("arithmetic_operands",),
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
            next(sql for sql, _ in conn.executed if "CREATE TABLE IF NOT EXISTS voucher_line_form_values" in sql),
        )
        self.assertTrue(any("ADD COLUMN IF NOT EXISTS bill_group_index" in sql for sql, _ in conn.executed))
        self.assertTrue(any("idx_voucher_line_form_entries_group" in sql for sql, _ in conn.executed))
        self.assertEqual(conn.rollbacks, 0)

    def test_form_table_migration_rechecks_columns_and_indexes_when_tables_exist(self):
        conn = FakeConn(fetchone_rows=[
            ("voucher_line_form_entries", "voucher_line_form_values"),
        ])

        self.assertEqual(database._ensure_postgres_form_tables(conn), [])
        # Existing tables still need idempotent column/index checks after upgrades.
        statements = [sql for sql, _ in conn.executed]
        self.assertEqual(sum("CREATE TABLE IF NOT EXISTS" in sql for sql in statements), 2)
        self.assertTrue(any("ADD COLUMN IF NOT EXISTS bill_group_index" in sql for sql in statements))
        self.assertTrue(any("idx_voucher_line_form_entries_group" in sql for sql in statements))
        self.assertFalse(any("DROP " in sql or "INSERT " in sql for sql in statements))
        self.assertEqual(conn.rollbacks, 0)

    def test_completed_schema_marker_skips_repeated_bootstrap(self):
        conn = FakeConn(
            rows=[("field_type",), ("field_scope",), ("linked_column",), ("arithmetic_operands",)],
            fetchone_rows=[
                ("audit_rules_config_state",),
                ("CHECK ((document_type = ANY (ARRAY['linked_template'::text, 'form'::text, 'physical_bill'::text])))",),
                ("voucher_line_form_entries", "voucher_line_form_values"),
            ],
        )

        # Keep the original Form/report/account migrations real; stub unrelated
        # additive migrations and assert their fast-path wiring separately below.
        # A FIFO fake need not impersonate every newer database catalog.
        unrelated_migrations = (
            "_ensure_postgres_storage_cleanup_runs",
            "_ensure_postgres_upload_validation_plans",
            "_ensure_postgres_upload_summary_columns",
            "_ensure_postgres_client_data_history",
            "_ensure_postgres_saved_column_mappings",
            "_ensure_postgres_cash_bank_transaction_classifications",
            "_ensure_postgres_cashflow_voucher_occurrences",
            "_ensure_postgres_cashflow_prepared_tables",
            "_ensure_postgres_cashflow_report_source_policy",
            "_ensure_postgres_trial_balance_column_mappings",
            "_ensure_postgres_audit_rule_assertions",
            "_ensure_postgres_category_compiled_rulebooks",
            "_ensure_postgres_category_document_assertions",
            "_ensure_postgres_category_cross_check_assertions",
            "_ensure_postgres_voucher_check_assertions",
            "_ensure_postgres_applied_ruleset_hash",
            "_ensure_postgres_local_vendor_discovery_indexes",
            "_ensure_postgres_client_data_canonical_columns",
        )
        with ExitStack() as stack:
            stack.enter_context(patch.object(database, "_POSTGRES_SCHEMA_READY", False))
            migrations = [stack.enter_context(patch.object(database, name, return_value=[]))
                          for name in unrelated_migrations]
            seed = stack.enter_context(patch.object(database, "_seed_postgres_data"))
            printed = stack.enter_context(patch("builtins.print"))
            database.init_db_postgres(conn)
            self.assertTrue(database._POSTGRES_SCHEMA_READY)
            seed.assert_not_called()
            for migration in migrations:
                migration.assert_called_once_with(conn)

        self.assertEqual(conn.executed[:4], [
            ("SELECT to_regclass('public.audit_rules_config_state')", None),
            ("\n            SELECT pg_get_constraintdef(oid)\n            FROM pg_constraint\n            WHERE conrelid = 'public.audit_rule_documents'::regclass\n              AND conname = 'audit_rule_documents_document_type_check'\n        ", None),
            ("\n            SELECT column_name\n            FROM information_schema.columns\n            WHERE table_schema = 'public'\n              AND table_name = 'audit_rule_document_fields'\n        ", None),
            ("SELECT to_regclass('public.voucher_line_form_entries'), to_regclass('public.voucher_line_form_values')", None),
        ])
        self.assertFalse(any("CREATE TABLE IF NOT EXISTS clients" in sql for sql, _ in conn.executed))
        self.assertTrue(any("CREATE TABLE IF NOT EXISTS report_category_sets" in sql for sql, _ in conn.executed))
        self.assertTrue(any("CREATE TABLE IF NOT EXISTS report_categories" in sql for sql, _ in conn.executed))
        self.assertTrue(any("CREATE TABLE IF NOT EXISTS report_account_category_assignments" in sql for sql, _ in conn.executed))
        self.assertTrue(any("CREATE TABLE IF NOT EXISTS account_groups" in sql for sql, _ in conn.executed))
        self.assertTrue(any("CREATE INDEX IF NOT EXISTS idx_account_groups_client" in sql for sql, _ in conn.executed))
        self.assertTrue(any("CREATE INDEX IF NOT EXISTS idx_account_groups_client_label" in sql for sql, _ in conn.executed))
        self.assertEqual(conn.commits, 1)
        printed.assert_called_once_with(
            "[postgres] existing schema verified; skipped bootstrap"
        )


class IsolatedMaintenanceTests(unittest.TestCase):
    class Connection(FakeConn):
        autocommit = False

        def __init__(self, **kwargs):
            super().__init__(fetchone_rows=[("precision_rehearsal_test", "127.0.0.1")], **kwargs)
            self.destination = {"host": "localhost", "dbname": "precision_rehearsal_test"}
            self.transaction_status = 0

        def get_dsn_parameters(self):
            return self.destination

        def get_transaction_status(self):
            return self.transaction_status

    def test_uses_only_caller_connection_and_discards_guard_snapshot_before_callback(self):
        conn = self.Connection()
        def operation(active):
            self.assertIs(active, conn)
            self.assertIs(database._pg_transaction.get(), conn)
            self.assertEqual(conn.rollbacks, 1)
            database.execute_write_pg("UPDATE rehearsal SET amount=1")
            return "checked"
        with patch.object(database, "get_pg_pool", side_effect=AssertionError("Must never borrow production pool")):
            self.assertEqual(database.run_isolated_pg_transaction(conn, operation), "checked")
        self.assertEqual(conn.executed[0][0], "SELECT current_database(), inet_server_addr()::text")
        self.assertEqual(conn.executed[1][0], "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        self.assertEqual(conn.commits, 1)
        self.assertIsNone(database._pg_transaction.get())

    def test_rejects_remote_or_production_database_before_any_sql(self):
        for host, name in (("db.example.com", "precision_rehearsal_test"), ("localhost", "postgres"),
                           ("localhost", "precision_rehearsal_"), ("localhost", "precision_rehearsal_a;DROP")):
            with self.subTest(host=host, name=name):
                conn = self.Connection()
                conn.destination = {"host": host, "dbname": name}
                with self.assertRaisesRegex(RuntimeError, "restricted"):
                    database.run_isolated_pg_transaction(conn, lambda active: self.fail("Callback ran"))
                self.assertEqual(conn.executed, [])

    def test_checks_actual_server_identity_and_connection_state(self):
        for identity in (("postgres", "127.0.0.1"), ("precision_rehearsal_test", "10.0.0.1"),
                         ("precision_rehearsal_test", "10.0.0.1/32"),
                         ("precision_rehearsal_test", None)):
            conn = self.Connection()
            conn.fetchone_rows = [identity]
            with self.subTest(identity=identity), self.assertRaisesRegex(RuntimeError, "server identity"):
                database.run_isolated_pg_transaction(conn, lambda active: self.fail("Callback ran"))
            self.assertEqual(conn.commits, 0)
        for attribute, value in (("closed", 1), ("autocommit", True), ("transaction_status", 2)):
            conn = self.Connection()
            setattr(conn, attribute, value)
            with self.subTest(attribute=attribute), self.assertRaisesRegex(RuntimeError, "idle"):
                database.run_isolated_pg_transaction(conn, lambda active: self.fail("Callback ran"))
            self.assertEqual(conn.executed, [])

    def test_accepts_postgresql_loopback_inet_host_masks(self):
        for address in ("127.0.0.1/32", "::1/128"):
            conn = self.Connection()
            conn.fetchone_rows = [("precision_rehearsal_test", address)]
            with self.subTest(address=address):
                self.assertEqual(database.run_isolated_pg_transaction(conn, lambda active: "local"), "local")
                self.assertEqual(conn.commits, 1)

    def test_never_replays_callback_after_write_or_commit_failure(self):
        from unittest.mock import Mock
        for lost_ack in (False, True):
            with self.subTest(lost_ack=lost_ack):
                error = psycopg2.OperationalError("SSL error: unexpected eof while reading")
                conn = self.Connection(commit_error=error if lost_ack else None)
                operation = Mock(return_value="done", side_effect=None if lost_ack else error)
                with self.assertRaises(psycopg2.OperationalError):
                    database.run_isolated_pg_transaction(conn, operation)
                operation.assert_called_once_with(conn)
                self.assertEqual(conn.commits, int(lost_ack))
                self.assertEqual(conn.rollbacks, 2)
                self.assertIsNone(database._pg_transaction.get())


if __name__ == "__main__":
    unittest.main()
