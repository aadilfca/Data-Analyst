from fastapi import APIRouter
from datetime import datetime
from database import execute_query, execute_query_pg, estimate_client_data_size
from routers.verification import _rollup_voucher_status
from routers.data import compute_overdue_uploads

router = APIRouter()

@router.get("/")
def root():
    return {"message": "System Analyst Dashboard", "version": "1.0.0"}

@router.get("/api/test")
def test():
    return {"status": "Server is running!", "timestamp": datetime.now().isoformat()}

@router.get("/api/dashboard/stats")
def get_dashboard_stats():
    try:
        uploaded = execute_query_pg("SELECT COUNT(*) as count FROM uploaded_files")[0]['count']
    except:
        uploaded = 0
    try:
        pending = execute_query_pg("SELECT COUNT(*) as count FROM verifications WHERE status = 'Pending'")[0]['count']
    except:
        pending = 0
    try:
        total_records = execute_query_pg("SELECT COUNT(*) as count FROM client_data WHERE is_deleted = FALSE")[0]['count']
    except:
        total_records = 0
    try:
        last_upload = execute_query_pg("SELECT upload_date FROM uploaded_files ORDER BY upload_date DESC LIMIT 1")
        last_upload = last_upload[0]['upload_date'] if last_upload else None
    except:
        last_upload = None
    return {
        "total_uploads": uploaded,
        "pending_verifications": pending,
        "active_modules": 2,
        "total_records": total_records,
        "last_upload": last_upload
    }

@router.get("/api/dashboard/db-size")
def get_dashboard_db_size():
    """
    Database size visibility (Main Dashboard): estimated storage footprint
    aggregated across every client's template data (client_data table only -
    not error_log/app_settings/etc). See database.py's estimate_client_data_size()
    for the estimation formula/caveats.
    """
    return estimate_client_data_size()


@router.get("/api/dashboard/kpi-stats")
def get_dashboard_kpi_stats():
    """
    Main Dashboard Quick Overview KPIs (Phase B1, backend only - B2 wires this into the
    frontend card): Open Queries, Verification Progress %, Unresolved Errors. Live-computed,
    no caching, same as db-size above.
    """
    # Open Queries: literal row count of Query-status lines, active rows only (range_status
    # 'active' avoids double-counting stale rows left behind by a reopened/orphaned session -
    # see routers/verification.py's dedup notes for why this matters).
    open_queries = execute_query_pg(
        "SELECT COUNT(*) as count FROM verification_details WHERE cleared_status = 'Query' AND range_status = 'active'"
    )[0]['count']

    # Verification Progress %: cleared vouchers / total vouchers, across all clients. Mirrors
    # verification.py's get_voucher_stats() (per-session) and the cross-session dedup pattern
    # used elsewhere in that file (keep the highest verification_details.id per client_data_id,
    # since a reopened cashbook inserts a fresh row per line every time), just without a
    # session_id filter.
    rows = execute_query_pg("""
        SELECT vd.id, vd.client_data_id, vd.transaction_ref, vd.cleared_status, vs.client_id
        FROM verification_details vd
        JOIN verification_sessions vs ON vd.session_id = vs.id
        WHERE vd.range_status = 'active' AND vs.status != 'Cancelled'
        ORDER BY vd.id ASC
    """)
    latest_by_cdid = {}
    voucher_key_by_cdid = {}
    for r in rows:
        latest_by_cdid[r['client_data_id']] = r['cleared_status']
        voucher_key_by_cdid[r['client_data_id']] = (r['client_id'], r['transaction_ref'])

    statuses_by_voucher = {}
    for cdid, status in latest_by_cdid.items():
        key = voucher_key_by_cdid[cdid]
        if key[1]:
            statuses_by_voucher.setdefault(key, []).append(status)

    total_vouchers = len(statuses_by_voucher)
    cleared_vouchers = sum(
        1 for statuses in statuses_by_voucher.values()
        if _rollup_voucher_status(statuses) == 'Cleared'
    )
    progress_pct = round((cleared_vouchers / total_vouchers) * 100, 1) if total_vouchers else 0.0

    # Unresolved Errors: same total-minus-resolved logic as error_log.py's get_error_stats().
    total_errors = execute_query("SELECT COUNT(*) as count FROM error_log")[0]['count']
    resolved_errors = execute_query("SELECT COUNT(*) as count FROM error_log WHERE is_resolved = 1")[0]['count']
    unresolved_errors = total_errors - resolved_errors

    return {
        "open_queries": open_queries,
        "verification_progress_pct": progress_pct,
        "total_vouchers": total_vouchers,
        "cleared_vouchers": cleared_vouchers,
        "unresolved_errors": unresolved_errors,
    }


@router.get("/api/dashboard/overdue-uploads")
def get_dashboard_overdue_uploads():
    """
    Main Dashboard: overdue-upload list across every client, sorted
    most-overdue-first. See routers/data.py's compute_overdue_uploads() for
    the business-day/holiday detection logic (Phase D1).
    """
    return {"overdue": compute_overdue_uploads()}


@router.get("/api/health")
def health_check():
    return {"status": "healthy", "timestamp": datetime.now().isoformat()}

@router.get("/favicon.ico")
async def favicon():
    return {"message": "No favicon available"}