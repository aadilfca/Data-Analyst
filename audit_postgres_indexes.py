"""
Read-only PostgreSQL index review with an optional additive-index check.

The default command inventories client_data indexes, reports usage and physical
size, identifies possible overlap, and counts unresolved active-identity
duplicate groups. It never changes PostgreSQL. The legacy ``--run`` option is
kept only for the previously approved additive missing-index check; it does not
drop or rebuild indexes.

Usage:
    python audit_postgres_indexes.py              (read-only review)
    python audit_postgres_indexes.py --json       (machine-readable review)
    python audit_postgres_indexes.py --run        (create approved missing indexes)
"""

import argparse
import json

from psycopg2.extras import RealDictCursor

from database import get_raw_conn_pg, release_raw_conn_pg
from services.storage_cleanup import (
    INDEX_REVIEW_PLAN_QUERIES,
    _client_data_index_bloat_evidence,
    _plan_index_names,
)


# Each entry: (index_name, table, CREATE INDEX statement). Add future additive
# findings here rather than starting a second script.
CANDIDATE_INDEXES = [
    (
        "idx_client_data_client_template_deleted",
        "client_data",
        "CREATE INDEX IF NOT EXISTS idx_client_data_client_template_deleted "
        "ON client_data (client_id, template_id, is_deleted)",
    ),
]

REQUIRED_CLIENT_DATA_INDEXES = (
    "client_data_pkey",
    "idx_client_data_active_identity_unique",
)


def get_existing_index_names(cur) -> set:
    cur.execute("SELECT indexname FROM pg_indexes WHERE schemaname = 'public'")
    return {row["indexname"] for row in cur.fetchall()}


def _key_columns(row) -> list[str]:
    """Normalize PostgreSQL's array result for JSON and test doubles."""
    values = row.get("key_columns") or []
    return [str(value) for value in values]


def get_client_data_index_inventory(cur) -> list[dict]:
    """Return client_data index facts without making any database changes."""
    cur.execute(
        """
        SELECT s.indexrelname AS index_name,
               pg_relation_size(s.indexrelid) AS index_bytes,
               COALESCE(s.idx_scan, 0) AS idx_scan,
               COALESCE(s.idx_tup_read, 0) AS idx_tup_read,
               COALESCE(s.idx_tup_fetch, 0) AS idx_tup_fetch,
               ix.indisunique AS is_unique,
               ix.indisprimary AS is_primary,
               pg_get_expr(ix.indpred, ix.indrelid) AS predicate,
               pg_get_indexdef(s.indexrelid) AS index_definition,
               ARRAY(
                   SELECT COALESCE(att.attname, '<expression>')
                   FROM unnest(ix.indkey) WITH ORDINALITY AS keys(attnum, ord)
                   LEFT JOIN pg_attribute att
                     ON att.attrelid = ix.indrelid
                    AND att.attnum = keys.attnum
                   ORDER BY keys.ord
               ) AS key_columns
        FROM pg_stat_user_indexes s
        JOIN pg_index ix ON ix.indexrelid = s.indexrelid
        WHERE s.schemaname = 'public'
          AND s.relname = 'client_data'
        ORDER BY pg_relation_size(s.indexrelid) DESC, s.indexrelname
        """
    )

    inventory = []
    for row in cur.fetchall():
        name = row["index_name"]
        is_unique = bool(row.get("is_unique"))
        is_primary = bool(row.get("is_primary"))
        protected = (
            is_primary
            or is_unique
            or name in REQUIRED_CLIENT_DATA_INDEXES
        )
        scans = int(row.get("idx_scan") or 0)
        inventory.append(
            {
                "index_name": name,
                "index_bytes": int(row.get("index_bytes") or 0),
                "idx_scan": scans,
                "idx_tup_read": int(row.get("idx_tup_read") or 0),
                "idx_tup_fetch": int(row.get("idx_tup_fetch") or 0),
                "is_unique": is_unique,
                "is_primary": is_primary,
                "protected": protected,
                "classification": (
                    "protected"
                    if protected
                    else "evidence-required-zero-scan"
                    if scans == 0
                    else "used"
                ),
                "key_columns": _key_columns(row),
                "predicate": row.get("predicate"),
                "index_definition": row.get("index_definition"),
            }
        )
    return inventory


def get_active_identity_duplicate_groups(cur) -> list[dict]:
    """Return aggregate duplicate groups without exposing source identities."""
    cur.execute(
        """
        SELECT client_id, template_id, COUNT(*) AS row_count
        FROM client_data
        WHERE is_deleted = FALSE
          AND change_detection_status <> 'deleted'
          AND source_identity IS NOT NULL
          AND source_identity <> ''
        GROUP BY client_id, template_id, source_identity
        HAVING COUNT(*) > 1
        ORDER BY row_count DESC, client_id, template_id
        LIMIT 100
        """
    )
    return [
        {
            "client_id": int(row["client_id"]),
            "template_id": int(row["template_id"]),
            "row_count": int(row["row_count"]),
        }
        for row in cur.fetchall()
    ]


def get_possible_overlap_pairs(inventory: list[dict]) -> list[dict]:
    """Flag same-predicate duplicate/prefix keys for human review only."""
    overlaps = []
    for position, left in enumerate(inventory):
        left_keys = tuple(left.get("key_columns") or ())
        left_predicate = left.get("predicate") or ""
        if not left_keys:
            continue
        for right in inventory[position + 1:]:
            right_keys = tuple(right.get("key_columns") or ())
            if not right_keys or left_predicate != (right.get("predicate") or ""):
                continue
            same_keys = left_keys == right_keys
            prefix = (
                left_keys[: min(len(left_keys), len(right_keys))] ==
                right_keys[: min(len(left_keys), len(right_keys))]
            )
            if same_keys or (prefix and len(left_keys) != len(right_keys)):
                overlaps.append(
                    {
                        "indexes": [left["index_name"], right["index_name"]],
                        "reason": "same key definition" if same_keys else "same leading key columns",
                        "protected_indexes": [
                            name
                            for name in (left["index_name"], right["index_name"])
                            if next(
                                item["protected"]
                                for item in inventory
                                if item["index_name"] == name
                            )
                        ],
                    }
                )
    return overlaps


def get_query_plan_evidence(cur) -> dict:
    """Capture planner-only evidence for the approved fixed-shape probes."""
    plans = []
    for query_name, query in INDEX_REVIEW_PLAN_QUERIES:
        try:
            cur.execute(query)
            rows = cur.fetchall()
            raw_plan = rows[0].get("QUERY PLAN") if rows else None
            if raw_plan is None and rows:
                raw_plan = rows[0].get("query plan")
            plan_tree = raw_plan[0] if isinstance(raw_plan, list) and raw_plan else raw_plan
            plans.append(
                {
                    "query_name": query_name,
                    "available": bool(plan_tree),
                    "index_names": _plan_index_names(plan_tree),
                }
            )
        except Exception as exc:
            return {
                "available": False,
                "reason": f"EXPLAIN failed for {query_name}: {exc}",
                "plans": plans,
            }
    return {
        "available": len(plans) == len(INDEX_REVIEW_PLAN_QUERIES),
        "probe": "Planner-only fixed-shape probes; no query execution or row reads.",
        "plans": plans,
        "index_names": sorted({name for plan in plans for name in plan["index_names"]}),
    }


def build_review(cur, include_plans: bool = False, include_bloat: bool = False) -> dict:
    existing = get_existing_index_names(cur)
    inventory = get_client_data_index_inventory(cur)
    duplicate_groups = get_active_identity_duplicate_groups(cur)
    review = {
        "table_name": "client_data",
        "existing_index_count": len(existing),
        "required_indexes": list(REQUIRED_CLIENT_DATA_INDEXES),
        "missing_required_indexes": [
            name for name in REQUIRED_CLIENT_DATA_INDEXES if name not in existing
        ],
        "active_identity_duplicate_group_count": len(duplicate_groups),
        "active_identity_duplicate_groups_sample": duplicate_groups,
        "indexes": inventory,
        "possible_overlap_pairs": get_possible_overlap_pairs(inventory),
        "policy": [
            "Primary, unique, and active-identity indexes are protected.",
            "Zero scans are evidence for review, not permission to drop an index.",
            "Possible overlap is a human-review signal and never an automatic decision.",
            "Unresolved active-identity duplicate groups block uniqueness changes.",
        ],
    }
    if include_plans:
        review["plan_evidence"] = get_query_plan_evidence(cur)
    if include_bloat:
        review["bloat_evidence"] = _client_data_index_bloat_evidence()
    return review


def main():
    parser = argparse.ArgumentParser(description="Review PostgreSQL client_data indexes safely.")
    parser.add_argument(
        "--run",
        action="store_true",
        help="Create approved missing additive indexes after the read-only review.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the complete review as JSON.",
    )
    parser.add_argument(
        "--plans",
        action="store_true",
        help="Also capture planner-only EXPLAIN evidence for fixed review probes.",
    )
    parser.add_argument(
        "--bloat",
        action="store_true",
        help="Also capture optional pgstattuple index-bloat evidence.",
    )
    args = parser.parse_args()

    conn = get_raw_conn_pg()
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        review = build_review(cur, include_plans=args.plans, include_bloat=args.bloat)
        existing = get_existing_index_names(cur)
        missing_candidates = [
            candidate for candidate in CANDIDATE_INDEXES if candidate[0] not in existing
        ]

        if args.json:
            review["missing_additive_candidates"] = [
                {"index_name": name, "table": table, "sql": sql}
                for name, table, sql in missing_candidates
            ]
            print(json.dumps(review, indent=2, sort_keys=True, default=str))
        else:
            print("Postgres Index Review (read-only)")
            print(f"Table: {review['table_name']}")
            print(f"Indexes found in schema: {review['existing_index_count']}")
            print(f"client_data indexes inspected: {len(review['indexes'])}")
            print(
                "Active-identity duplicate groups: "
                f"{review['active_identity_duplicate_group_count']}"
            )
            print(
                "Missing required indexes: "
                f"{', '.join(review['missing_required_indexes']) or 'none'}"
            )
            print("\nIndex evidence:")
            for index in review["indexes"]:
                print(
                    f"- {index['index_name']}: {index['index_bytes']} bytes, "
                    f"scans={index['idx_scan']}, classification={index['classification']}"
                )
            print(
                "\nPossible overlap pairs requiring EXPLAIN review: "
                f"{len(review['possible_overlap_pairs'])}"
            )
            if missing_candidates:
                print("\nMissing additive candidates:")
                for name, table, sql in missing_candidates:
                    print(f"- {name} on {table}: {sql}")
            else:
                print("\nMissing additive candidates: none")

        if args.run and missing_candidates:
            for name, table, sql in missing_candidates:
                print(f"Creating approved additive index: {name} on {table}")
                cur.execute(sql)
                conn.commit()
    finally:
        release_raw_conn_pg(conn)


if __name__ == "__main__":
    main()
