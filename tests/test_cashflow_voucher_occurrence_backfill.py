import json
import tempfile

import database
from scripts.backfill_cashflow_voucher_occurrences import run_sqlite_backfill


def _insert_source_fixture(conn):
    conn.execute("INSERT INTO clients (id, client_code, client_name) VALUES (1, 'C1', 'Client One')")
    template_id = conn.execute(
        "SELECT id FROM templates WHERE template_name = 'Transaction Report'"
    ).fetchone()[0]
    columns = {
        "voucher_id": "DV2 Voucher No",
        "date": "DV2 Date",
        "account_code": "DV2 Account Detail",
        "account_name": "DV2 Account Name",
        "debit": "DV2 Debit Amount",
        "credit": "DV2 Credit Amount",
    }
    column_ids = {}
    for role, column_name in columns.items():
        column_ids[role] = conn.execute(
            "INSERT INTO master_columns (column_name, data_type) VALUES (?, 'Text')",
            (column_name,),
        ).lastrowid
    for role, column_id in column_ids.items():
        conn.execute(
            """INSERT INTO account_control_cashflow_source_mappings
               (client_id, template_id, role, template_column_id)
               VALUES (1, ?, ?, ?)""",
            (template_id, role, column_id),
        )
    conn.execute(
        """INSERT INTO account_control_cashflow_settings (client_id, source_template_id)
           VALUES (1, ?)""",
        (template_id,),
    )
    for upload_id, file_name, uploaded_at in (
        (10, "January complete.xlsx", "2026-02-01 09:00:00"),
        (20, "1-15 January.xlsx", "2026-02-02 09:00:00"),
    ):
        conn.execute(
            """INSERT INTO uploaded_files (id, file_name, file_path, client_id, upload_date)
               VALUES (?, ?, ?, 1, ?)""",
            (upload_id, file_name, file_name, uploaded_at),
        )
    rows = (
        ("10-15-115-15-0001", "Meezan Bank", 100, 0),
        ("10-15-55-10-10-0001", "RDX Inc", 0, 100),
    )
    for upload_id in (10, 20):
        for account_code, account_name, debit, credit in rows:
            row_data = {
                columns["voucher_id"]: "BRV-0126-159",
                columns["date"]: "23/01/2026",
                columns["account_code"]: account_code,
                columns["account_name"]: account_name if upload_id == 10 else account_name.upper(),
                columns["debit"]: debit,
                columns["credit"]: credit,
            }
            conn.execute(
                """INSERT INTO client_data
                   (client_id, template_id, upload_id, row_data, extract_date)
                   VALUES (1, ?, ?, ?, '2026-01-23')""",
                (template_id, upload_id, json.dumps(row_data)),
            )
    conn.commit()


def test_historical_backfill_is_dry_by_default_and_apply_is_re_run_safe(monkeypatch):
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = f"{temp_dir}/cashflows.sqlite"
        monkeypatch.setattr(database, "DB_NAME", db_path)
        database.init_db()
        conn = database.get_db()
        try:
            _insert_source_fixture(conn)
            raw_before = conn.execute("SELECT COUNT(*) FROM client_data").fetchone()[0]

            dry_run = run_sqlite_backfill(conn, client_id=1)
            assert dry_run["mode"] == "dry_run"
            assert dry_run["written_occurrence_count"] == 0
            assert dry_run["decision_counts"] == {"current": 1, "exact_duplicate": 1}
            assert conn.execute("SELECT COUNT(*) FROM cashflow_voucher_occurrences").fetchone()[0] == 0

            applied = run_sqlite_backfill(conn, client_id=1, apply=True)
            assert applied["mode"] == "apply"
            assert applied["written_occurrence_count"] == 2
            occurrences = conn.execute(
                """SELECT upload_id, decision_status, canonical_occurrence_id, source_extract_as_of
                   FROM cashflow_voucher_occurrences ORDER BY upload_id"""
            ).fetchall()
            assert [row["decision_status"] for row in occurrences] == ["exact_duplicate", "current"]
            assert occurrences[0]["canonical_occurrence_id"] == occurrences[1]["canonical_occurrence_id"]
            assert occurrences[1]["source_extract_as_of"] == "2026-02-02 09:00:00"
            assert conn.execute("SELECT COUNT(*) FROM client_data").fetchone()[0] == raw_before

            rerun = run_sqlite_backfill(conn, client_id=1, apply=True)
            assert rerun["written_occurrence_count"] == 2
            assert conn.execute("SELECT COUNT(*) FROM cashflow_voucher_occurrences").fetchone()[0] == 2
        finally:
            conn.close()