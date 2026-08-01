"""
Standalone backfill script for Rate-derivation Phase 9 of 9 (final phase).

Historical client_data rows imported before the Rate-derivation feature
existed have a blank Rate and rate_source = NULL, even for Purchase Order /
Store Receiving / Vendor Bill templates where Rate = Amount / Quantity could
now be derived. This script derives and writes Rate for exactly those rows,
stamping rate_source = 'derived' -- never touching a row that already has a
real Rate value.

Not wired into any API endpoint -- run manually on demand:
    python backfill_rate.py            (dry-run report only, no writes)
    python backfill_rate.py --run      (actually perform the backfill)
"""
import argparse
import json

from database import execute_query, execute_write, log_audit, log_data_audit
from routers.import_export import get_rate_precision, find_named_column

# Mirrors routers/templates.py's RATE_SOURCE_ELIGIBLE_TEMPLATES exactly --
# these are the only templates that can ever carry a Rate column.
RATE_SOURCE_ELIGIBLE_TEMPLATES = {'purchase order', 'store receiving', 'vendor bill'}


def get_eligible_templates() -> list:
    """Active templates whose name matches the Rate-derivation eligible set."""
    templates = execute_query("SELECT id, template_name FROM templates WHERE is_active = 1")
    return [t for t in templates if t['template_name'].strip().lower() in RATE_SOURCE_ELIGIBLE_TEMPLATES]


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


def is_rate_blank(value) -> bool:
    """None, missing, or an empty/whitespace string counts as blank."""
    if value is None:
        return True
    if isinstance(value, str) and value.strip() == '':
        return True
    return False


def derive_rate(row_data: dict, qty_col_name: str, amount_col_name: str, rate_precision: int):
    """
    Mirrors apply_rate_derivation()'s 'derived' branch exactly (routers/import_export.py),
    byte-for-byte, so the backfill can never disagree with what live import-time derivation
    would have produced for the same row:
      - Qty missing/blank/non-numeric/0 -> None (nothing to derive, not an error).
      - Amount missing/blank coerces to 0.0 (a real Rate of 0.0 is still derived).
      - Amount non-numeric (unparseable) -> None (truly can't derive).
    Returns None only for the two "can't derive" cases above; caller must not set
    rate_source in that case.
    """
    qty_raw = row_data.get(qty_col_name)
    try:
        qty = float(qty_raw) if qty_raw not in (None, '') else 0.0
    except (TypeError, ValueError):
        qty = 0.0
    if qty == 0:
        return None

    amount_raw = row_data.get(amount_col_name)
    try:
        amount = float(amount_raw) if amount_raw not in (None, '') else 0.0
    except (TypeError, ValueError):
        return None

    return round(amount / qty, rate_precision)


def scan(dry_run: bool) -> dict:
    """
    Walks every eligible template's qualifying client_data rows and either reports
    (dry_run=True) or performs (dry_run=False) the backfill. Returns per-template
    counts plus a grand total.
    """
    rate_precision = get_rate_precision()
    report = {"templates": [], "grand_total": {
        "total_candidates": 0,
        "backfilled": 0,
        "skipped_already_has_rate": 0,
        "skipped_qty_zero_or_blank": 0,
        "skipped_no_amount": 0,
        "skipped_no_rate_column": 0,
    }}

    for template in get_eligible_templates():
        template_id = template['id']
        template_name = template['template_name']
        counts = {
            "template_id": template_id,
            "template_name": template_name,
            "total_candidates": 0,
            "backfilled": 0,
            "skipped_already_has_rate": 0,
            "skipped_qty_zero_or_blank": 0,
            "skipped_no_amount": 0,
            "skipped_no_rate_column": 0,
        }

        template_cols = get_template_columns(template_id)
        rate_col = find_named_column(template_cols, 'Rate')
        qty_col = find_named_column(template_cols, 'Quantity')
        amount_col = find_named_column(template_cols, 'Amount')

        if not rate_col:
            report["templates"].append(counts)
            continue  # this template has no Rate column at all -- nothing to backfill

        rows = execute_query(
            """
            SELECT id, row_data FROM client_data
            WHERE template_id = ? AND is_deleted = 0 AND change_detection_status != 'deleted'
            """,
            (template_id,)
        )

        for row in rows:
            counts["total_candidates"] += 1
            try:
                row_data = json.loads(row['row_data']) if isinstance(row['row_data'], str) else row['row_data']
            except Exception:
                continue

            if not is_rate_blank(row_data.get(rate_col['column_name'])):
                counts["skipped_already_has_rate"] += 1
                continue

            if not qty_col or not amount_col:
                counts["skipped_no_amount"] += 1
                continue

            new_rate = derive_rate(row_data, qty_col['column_name'], amount_col['column_name'], rate_precision)
            if new_rate is None:
                # derive_rate() only returns None for a qty that's missing/blank/
                # non-numeric/zero, or (qty being fine) an Amount that's genuinely
                # non-numeric -- distinguish which, purely for the report.
                qty_raw = row_data.get(qty_col['column_name'])
                try:
                    qty_ok_and_nonzero = qty_raw not in (None, '') and float(qty_raw) != 0
                except (TypeError, ValueError):
                    qty_ok_and_nonzero = False
                if qty_ok_and_nonzero:
                    counts["skipped_no_amount"] += 1
                else:
                    counts["skipped_qty_zero_or_blank"] += 1
                continue

            counts["backfilled"] += 1
            if not dry_run:
                old_row_data = dict(row_data)
                row_data[rate_col['column_name']] = new_rate
                execute_write(
                    "UPDATE client_data SET row_data = ?, rate_source = 'derived' WHERE id = ?",
                    (json.dumps(row_data), row['id'])
                )
                log_data_audit(row['id'], "Analyst", "UPDATE", old_row_data, row_data)

        report["templates"].append(counts)
        for key in report["grand_total"]:
            report["grand_total"][key] += counts[key]

    if not dry_run and report["grand_total"]["backfilled"] > 0:
        log_audit(
            "Analyst", "Backfill", "Upload",
            f"Rate-derivation backfill: {report['grand_total']['backfilled']} rows backfilled "
            f"across {len(report['templates'])} template(s)"
        )

    return report


def print_report(report: dict, dry_run: bool):
    verb = "Would backfill" if dry_run else "Backfilled"
    print()
    print("=" * 70)
    print(f"Rate Backfill Report ({'DRY RUN' if dry_run else 'LIVE RUN'})")
    print("=" * 70)
    for t in report["templates"]:
        print(f"\n{t['template_name']} (template_id={t['template_id']}):")
        print(f"  Rows scanned:              {t['total_candidates']}")
        print(f"  Already had Rate (skip):   {t['skipped_already_has_rate']}")
        print(f"  Qty zero/blank (skip):     {t['skipped_qty_zero_or_blank']}")
        print(f"  No Amount/Qty col (skip):  {t['skipped_no_amount']}")
        print(f"  {verb}:{' ' * (24 - len(verb))}{t['backfilled']}")

    gt = report["grand_total"]
    print("\n" + "-" * 70)
    print("GRAND TOTAL")
    print(f"  Rows scanned:              {gt['total_candidates']}")
    print(f"  Already had Rate (skip):   {gt['skipped_already_has_rate']}")
    print(f"  Qty zero/blank (skip):     {gt['skipped_qty_zero_or_blank']}")
    print(f"  No Amount/Qty col (skip):  {gt['skipped_no_amount']}")
    print(f"  {verb}:{' ' * (24 - len(verb))}{gt['backfilled']}")
    print("=" * 70)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill derived Rate values for historical PO/SR/VB rows.")
    parser.add_argument("--run", action="store_true", help="Actually write changes. Omit for a dry-run report only.")
    args = parser.parse_args()

    dry_run = not args.run
    result = scan(dry_run=dry_run)
    print_report(result, dry_run)

    if dry_run:
        print("\nDry run only -- no changes written. Re-run with --run to apply.")
    else:
        print("\nBackfill complete.")
