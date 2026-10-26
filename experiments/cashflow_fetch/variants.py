"""Experimental SELECT transports; never imported by the application.

Both candidates wrap the unchanged canonical source SELECT. Amounts travel as
PostgreSQL text so JSON's numeric decoder cannot introduce a precision change.
"""

from datetime import date


COLUMNS = (
    "voucher_id", "date", "account_code", "account_name", "voucher_detail",
    "debit", "credit", "treasury_type", "category", "transaction_month",
    "transaction_fortnight", "source_line_count", "classification_status",
    "database_row_count", "database_voucher_count", "database_totals_only",
)
DESCRIPTORS = (
    "account_code", "account_name", "treasury_type", "category",
    "classification_status",
)
REMAINING = tuple(column for column in COLUMNS if column not in DESCRIPTORS)
TEXT_CASTS = {"date", "debit", "credit", "source_line_count",
              "database_row_count", "database_voucher_count"}
METHODS = ("baseline", "array_rows", "dictionary_rows")
ORDER = "b.date, b.voucher_id, b.account_code"

_CATEGORY_SELECT = "COALESCE(category.label, '') AS category"
_CATEGORY_JOINS = """            LEFT JOIN account_control_cashflow_category_assignments category_assignment
              ON category_assignment.client_id = cd.client_id
             AND category_assignment.account_code = cd.derived_account_code
            LEFT JOIN account_control_cashflow_categories category
              ON category.id = category_assignment.category_id
             AND category.client_id = cd.client_id
             AND category.is_active = TRUE
"""


def materialized_category_sql(source_sql):
    """Use the experiment-only client_data category field instead of live joins."""
    if source_sql.count(_CATEGORY_SELECT) != 1 or source_sql.count(_CATEGORY_JOINS) != 1:
        raise ValueError("Canonical Cashflows category SQL changed; review the experiment")
    candidate = source_sql.replace(
        _CATEGORY_SELECT, "COALESCE(cd.derived_category, '') AS category", 1
    ).replace(_CATEGORY_JOINS, "", 1)
    if "account_control_cashflow_category_assignments" in candidate or "account_control_cashflow_categories" in candidate:
        raise ValueError("Materialized-category query still contains live category joins")
    return candidate


def materialize_category_values(rows, account_to_label):
    """Return category labels for counter-account rows without changing inputs."""
    values = []
    for row in rows:
        status = str(row.get("derived_classification_status") or "").strip().casefold()
        account_code = str(row.get("derived_account_code") or "").strip()
        values.append(str(account_to_label.get(account_code) or "") if status == "non_treasury" else "")
    return values


def _values(columns, alias):
    return ", ".join(
        f"{alias}.{name}::text" if name in TEXT_CASTS else f"{alias}.{name}"
        for name in columns
    )


def transport_sql(source_sql, method):
    """Use only fixed column names and an internally captured production SELECT."""
    if method not in METHODS:
        raise ValueError("Unknown experiment method")
    if method == "baseline":
        return source_sql
    base = source_sql.strip().removesuffix(";")
    prefix = f"WITH benchmark_rows AS ({base})"
    if method == "array_rows":
        return prefix + f"""
            SELECT COALESCE(json_agg(json_build_array({_values(COLUMNS, 'b')})
                ORDER BY {ORDER}), '[]'::json) AS experiment_payload
            FROM benchmark_rows b
        """
    descriptors = ", ".join(f"b.{name}" for name in DESCRIPTORS)
    distinct_descriptors = ", ".join(DESCRIPTORS)
    return prefix + f""", benchmark_ranked AS (
            SELECT b.*, dense_rank() OVER (ORDER BY {descriptors}) AS descriptor_id
            FROM benchmark_rows b
        )
        SELECT json_build_object(
            'descriptors', (SELECT COALESCE(json_agg(
                json_build_array({_values(DESCRIPTORS, 'd')})
                ORDER BY d.descriptor_id), '[]'::json)
                FROM (SELECT DISTINCT descriptor_id, {distinct_descriptors}
                      FROM benchmark_ranked) d),
            'rows', (SELECT COALESCE(json_agg(json_build_array(
                b.descriptor_id, {_values(REMAINING, 'b')})
                ORDER BY {ORDER}), '[]'::json) FROM benchmark_ranked b)
        ) AS experiment_payload
    """


def _restore(name, value):
    if value is None:
        return None
    if name == "date":
        return date.fromisoformat(value)
    if name in {"debit", "credit"}:
        return float(value)
    if name in {"source_line_count", "database_row_count", "database_voucher_count"}:
        return int(value)
    if name == "database_totals_only" and type(value) is not bool:
        raise ValueError("Transport returned an invalid totals marker")
    return value


def _record(columns, values):
    if not isinstance(values, list) or len(values) != len(columns):
        raise ValueError("Transport returned an unexpected column count")
    return {name: _restore(name, value) for name, value in zip(columns, values)}


def decode_rows(method, rows):
    """Reconstruct the original ordered, typed source rows, including the sentinel."""
    if method not in METHODS:
        raise ValueError("Unknown experiment method")
    if method == "baseline":
        if any(set(row) != set(COLUMNS) for row in rows):
            raise ValueError("Canonical source columns changed; review the experiment")
        return rows
    if len(rows) != 1 or set(rows[0]) != {"experiment_payload"}:
        raise ValueError("Transport returned an unexpected envelope")
    payload = rows[0]["experiment_payload"]
    if method == "array_rows":
        if not isinstance(payload, list):
            raise ValueError("Transport returned an invalid array")
        return [_record(COLUMNS, values) for values in payload]
    if not isinstance(payload, dict) or set(payload) != {"descriptors", "rows"}:
        raise ValueError("Transport returned an invalid dictionary")
    if not isinstance(payload["descriptors"], list) or not isinstance(payload["rows"], list):
        raise ValueError("Transport returned an invalid dictionary array")
    descriptions = [_record(DESCRIPTORS, row) for row in payload["descriptors"]]
    result = []
    for values in payload["rows"]:
        if not isinstance(values, list) or len(values) != len(REMAINING) + 1:
            raise ValueError("Transport returned an invalid dictionary row")
        key = values[0]
        if type(key) is not int or not 1 <= key <= len(descriptions):
            raise ValueError("Transport returned an invalid dictionary reference")
        record = {**descriptions[key - 1], **_record(REMAINING, values[1:])}
        result.append({column: record[column] for column in COLUMNS})
    return result
