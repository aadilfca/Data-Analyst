"""
Standalone backfill script for the extract_columns_from_row() fix (2026-08-09).

extract_columns_from_row() previously used a single shared `break` for both its
Numeric and Date branches, so on every template that lists a Date column before
its Amount column (i.e. nearly all of them, including the Transaction Report
cashbook template) the loop exited the moment it found the date -- before it
ever reached a Numeric column. extract_amount ended up NULL on every real row,
regardless of template, which left the Physical Bill evidence-type gate's
Amount-match check permanently inert (nothing to compare a bill's amount
against).

This script recomputes extract_amount for every already-imported row using the
fixed extraction logic. extract_date is deliberately left untouched -- it was
already correctly populated on the vast majority of rows by the old code (the
bug only ever blocked amount, never date, given every template's real column
order), and guessing a delimiter/order to re-parse the handful of rows that did
fail would risk overwriting already-correct historical dates with a wrong guess.

Deliberately scans EVERY client_data row with a NULL extract_amount, regardless
of is_deleted/change_detection_status -- confirmed with the user, 2026-08-09.
Unlike backfill_rate.py (which only cares about the current live dataset used
for future change-detection comparisons), a voucher line's own client_data row
is referenced by a fixed foreign key (verification_details.client_data_id) that
stays valid even after is_deleted=1 is set on it by a later whole-dataset
soft-delete/re-import-replace (routers/data.py) -- confirmed live that real,
currently-open voucher lines routinely point at such rows. Restricting to
is_deleted=0 would leave most real, still-referenced vouchers unfixed.

Not wired into any API endpoint -- run manually on demand:
    python backfill_extract_amount.py            (dry-run report only, no writes)
    python backfill_extract_amount.py --run      (actually perform the backfill)
"""
import argparse
import json

from database import execute_query, execute_write, log_audit, log_data_audit
from routers.import_export import extract_columns_from_row


def get_active_templates() -> list:
    return execute_query("SELECT id, template_name FROM templates WHERE is_active = 1")


def get_template_columns(template_id: int) -> list:
    """Same column-fetch shape used throughout routers/import_export.py."""
    return execute_query(
        """
        SELECT m.id, m.column_name, m.data_type
        FROM template_column_mappings p
        JOIN master_columns m ON p.column_id = m.id
        WHERE p.template_id = ? AND m.is_active = 1
        ORDER BY p.sort_order ASC
        """,
        (template_id,)
    )


def scan(dry_run: bool) -> dict:
    """
    Walks every active template's qualifying client_data rows (extract_amount IS
    NULL) and either reports (dry_run=True) or performs (dry_run=False) the
    backfill. Returns per-template counts plus a grand total.
    """
    report = {"templates": [], "grand_total": {
        "total_candidates": 0,
        "backfilled": 0,
        "skipped_no_numeric_value": 0,
        "skipped_no_columns": 0,
    }}

    for template in get_active_templates():
        template_id = template['id']
        template_name = template['template_name']
        counts = {
            "template_id": template_id,
            "template_name": template_name,
            "total_candidates": 0,
            "backfilled": 0,
            "skipped_no_numeric_value": 0,
            "skipped_no_columns": 0,
        }

        template_cols = get_template_columns(template_id)
        if not template_cols:
            report["templates"].append(counts)
            continue  # this template has no active columns at all -- nothing to backfill

        rows = execute_query(
            """
            SELECT id, row_data FROM client_data
            WHERE template_id = ? AND extract_amount IS NULL
            """,
            (template_id,)
        )

        for row in rows:
            counts["total_candidates"] += 1
            try:
                row_data = json.loads(row['row_data']) if isinstance(row['row_data'], str) else row['row_data']
            except Exception:
                counts["skipped_no_numeric_value"] += 1
                continue

            amount, _date_str = extract_columns_from_row(row_data, template_cols)
            if amount is None:
                counts["skipped_no_numeric_value"] += 1
                continue

            counts["backfilled"] += 1
            if not dry_run:
                execute_write(
                    "UPDATE client_data SET extract_amount = ? WHERE id = ?",
                    (amount, row['id'])
                )
                log_data_audit(row['id'], "Analyst", "UPDATE",
                                {"extract_amount": None}, {"extract_amount": amount})

        report["templates"].append(counts)
        for key in report["grand_total"]:
            report["grand_total"][key] += counts[key]

    if not dry_run and report["grand_total"]["backfilled"] > 0:
        log_audit(
            "Analyst", "Backfill", "Upload",
            f"extract_amount backfill: {report['grand_total']['backfilled']} rows backfilled "
            f"across {len(report['templates'])} template(s)"
        )

    return report


def print_report(report: dict, dry_run: bool):
    verb = "Would backfill" if dry_run else "Backfilled"
    print()
    print("=" * 70)
    print(f"extract_amount Backfill Report ({'DRY RUN' if dry_run else 'LIVE RUN'})")
    print("=" * 70)
    for t in report["templates"]:
        print(f"\n{t['template_name']} (template_id={t['template_id']}):")
        print(f"  Rows scanned (amount NULL): {t['total_candidates']}")
        print(f"  No numeric value found:     {t['skipped_no_numeric_value']}")
        print(f"  {verb}:{' ' * (26 - len(verb))}{t['backfilled']}")

    gt = report["grand_total"]
    print("\n" + "-" * 70)
    print("GRAND TOTAL")
    print(f"  Rows scanned (amount NULL): {gt['total_candidates']}")
    print(f"  No numeric value found:     {gt['skipped_no_numeric_value']}")
    print(f"  {verb}:{' ' * (26 - len(verb))}{gt['backfilled']}")
    print("=" * 70)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill extract_amount for historical client_data rows.")
    parser.add_argument("--run", action="store_true", help="Actually write changes. Omit for a dry-run report only.")
    args = parser.parse_args()

    dry_run = not args.run
    result = scan(dry_run=dry_run)
    print_report(result, dry_run)

    if dry_run:
        print("\nDry run only -- no changes written. Re-run with --run to apply.")
    else:
        print("\nBackfill complete.")
