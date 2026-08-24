"""
One-off cleanup script: deactivates 6 leftover Playwright test rows in
audit_rule_voucher_checks (ids 16-21, "PW Test VC <timestamp>") that leaked
into real client data (Leather Crop, client_id 5) via voucher_check_status.

Never deletes -- ON DELETE CASCADE on voucher_check_status.check_id would
destroy the 13 real tick/cross records tied to these ids, erasing audit
trail history. Deactivation (is_active = FALSE) hides them from every
client's Voucher-level Checks bar and from the "all active checks ticked"
Cleared/Query gate, while leaving voucher_check_status untouched.

Not wired into any API endpoint -- run manually on demand:
    python deactivate_pw_test_voucher_checks.py            (dry-run report only)
    python deactivate_pw_test_voucher_checks.py --run      (export, deactivate, re-verify)
"""
import argparse
import csv
import sys

from database import execute_query_pg, execute_write_pg
from routers.verification import compute_and_persist_cleared_status

PW_TEST_CHECK_IDS = [16, 17, 18, 19, 20, 21]
EXPORT_PATH = "pw_test_voucher_checks_audit_export_2026-08-24.csv"

# Latest-session representative verification_details.id for each affected voucher
# (confirmed live: each cashbook reopen creates a fresh sibling row set sharing the
# same transaction_ref, per this app's documented no-session-resume design -- these
# are the current, live lines for each voucher as of this script's investigation).
VOUCHER_LINE_IDS = {
    "CRV-0126-102": [143872],
    "CRV-0126-103": [143873, 143874],
    "CRV-0126-104": [143875],
    "CRV-0126-105": [143876],
}


def export_voucher_check_status(path: str) -> int:
    """Step 1: preserve the 13 real Leather Crop rows referencing these check ids
    to a durable CSV before they become harder to find (deactivation doesn't
    remove them, but this is the confirmed audit-trail record either way)."""
    rows = execute_query_pg(
        """
        SELECT vcs.client_id, c.client_name, vcs.transaction_ref, vcs.check_id,
               arvc.name AS check_name, vcs.ticked, vcs.state,
               vcs.performed_by_name, vcs.updated_at
        FROM voucher_check_status vcs
        JOIN clients c ON c.id = vcs.client_id
        JOIN audit_rule_voucher_checks arvc ON arvc.id = vcs.check_id
        WHERE vcs.check_id = ANY(%s)
        ORDER BY vcs.transaction_ref, vcs.check_id
        """,
        (PW_TEST_CHECK_IDS,)
    )
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "client_id", "client_name", "transaction_ref", "check_id", "check_name",
            "ticked", "state", "performed_by_name", "updated_at"
        ])
        writer.writeheader()
        for r in rows:
            writer.writerow(r)
    return len(rows)


def snapshot_cleared_status() -> dict:
    """Steps 2/4: cleared_status + approval_authority/voucher-checks gate detail
    for every representative line, via the exact same read-only path the app's
    own line_status_reason popover uses (persist=False)."""
    snapshot = {}
    for ref, line_ids in VOUCHER_LINE_IDS.items():
        snapshot[ref] = []
        for line_id in line_ids:
            result = compute_and_persist_cleared_status(line_id, detail=True, persist=False)
            snapshot[ref].append({"line_id": line_id, "result": result})
    return snapshot


def print_snapshot(label: str, snapshot: dict):
    print(f"\n--- {label} ---")
    for ref, lines in snapshot.items():
        for entry in lines:
            r = entry["result"]
            status = r.get("cleared_status") if r else None
            print(f"  {ref} (line {entry['line_id']}): cleared_status = {status}")
            if r and r.get("gates"):
                for gate in r["gates"]:
                    if gate.get("key") in ("voucher_checks", "approval_authority"):
                        print(f"      gate[{gate['key']}]: status={gate.get('status')} - {gate.get('detail')}")


def diff_snapshots(before: dict, after: dict):
    print("\n--- Flip report (before -> after) ---")
    any_flip = False
    for ref, lines in before.items():
        for b_entry, a_entry in zip(lines, after[ref]):
            b_status = (b_entry["result"] or {}).get("cleared_status")
            a_status = (a_entry["result"] or {}).get("cleared_status")
            if b_status != a_status:
                any_flip = True
                print(f"  *** FLIP *** {ref} (line {b_entry['line_id']}): "
                      f"{b_status} -> {a_status}")
    if not any_flip:
        print("  No cleared_status flips detected on the 4 checked lines.")


def check_untouched_for_ref(transaction_ref: str) -> list:
    """Reproduces verify_voucher_checks()'s own untouched-check logic (routers/verification.py,
    ~line 1497-1509) read-only -- no write to real client data, unlike calling the live endpoint."""
    line_row = execute_query_pg(
        """
        SELECT vs.client_id FROM verification_details vd
        JOIN verification_sessions vs ON vs.id = vd.session_id
        WHERE vd.transaction_ref = %s
        ORDER BY vd.id DESC LIMIT 1
        """,
        (transaction_ref,)
    )
    if not line_row:
        return []
    client_id = line_row[0]["client_id"]

    active_checks = execute_query_pg(
        "SELECT id, name FROM audit_rule_voucher_checks WHERE is_active = TRUE ORDER BY sort_order"
    )
    status_rows = execute_query_pg(
        "SELECT check_id, state FROM voucher_check_status WHERE client_id = %s AND transaction_ref = %s",
        (client_id, transaction_ref)
    )
    state_by_check_id = {r["check_id"]: r["state"] for r in status_rows}
    return [c for c in active_checks if state_by_check_id.get(c["id"], "untouched") == "untouched"]


def main():
    parser = argparse.ArgumentParser(
        description="Deactivate leftover Playwright test voucher-checks (ids 16-21)."
    )
    parser.add_argument("--run", action="store_true",
                         help="Actually export, deactivate, and re-verify. Omit for a dry-run report only.")
    args = parser.parse_args()
    dry_run = not args.run

    print("=" * 70)
    print(f"PW Test Voucher-Checks Cleanup ({'DRY RUN' if dry_run else 'LIVE RUN'})")
    print("=" * 70)

    # Step 1: export (always runs -- read-only against the DB, safe either way)
    exported = export_voucher_check_status(EXPORT_PATH)
    print(f"\nExported {exported} voucher_check_status row(s) to {EXPORT_PATH}")

    # Step 2: pre-deactivation baseline (always runs)
    before = snapshot_cleared_status()
    print_snapshot("BEFORE deactivation", before)

    before_untouched_104 = check_untouched_for_ref("CRV-0126-104")
    print(f"\nCRV-0126-104 untouched active checks BEFORE: "
          f"{[c['name'] for c in before_untouched_104]}")

    if dry_run:
        print("\nDry run only -- no changes written. Re-run with --run to apply.")
        return

    # Step 3: deactivate
    execute_write_pg(
        "UPDATE audit_rule_voucher_checks SET is_active = FALSE WHERE id = ANY(%s)",
        (PW_TEST_CHECK_IDS,)
    )
    print(f"\nDeactivated {len(PW_TEST_CHECK_IDS)} row(s): ids {PW_TEST_CHECK_IDS}")

    # Step 4: re-verify + flip report
    after = snapshot_cleared_status()
    print_snapshot("AFTER deactivation", after)
    diff_snapshots(before, after)

    # Step 5: CRV-0126-104 diagnostic
    after_untouched_104 = check_untouched_for_ref("CRV-0126-104")
    print(f"\nCRV-0126-104 untouched active checks AFTER: "
          f"{[c['name'] for c in after_untouched_104]}")
    if before_untouched_104 and not after_untouched_104:
        print("  -> Deactivation cleared the only untouched checks for this voucher; "
              "the 400 on verify_voucher_checks was very likely caused by these test rows.")
    elif after_untouched_104:
        print("  -> Still untouched active checks remain for this voucher after deactivation; "
              "the 400 was NOT (solely) caused by these test rows -- separate investigation needed.")
    else:
        print("  -> No untouched checks before or after -- test rows were not the cause here.")

    print("\nCleanup complete.")


if __name__ == "__main__":
    main()
