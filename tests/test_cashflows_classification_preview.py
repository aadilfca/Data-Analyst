from datetime import date

from services.cash_bank_classification import load_cashflow_classification_preview


def test_cashflows_classification_preview_uses_current_derived_rows_and_formats_fields():
    calls = []

    def query_fn(sql, params):
        calls.append((sql, params))
        if "SELECT COUNT(*)" in sql:
            return [{"total": 1}]
        return [{
            "id": 41,
            "row_data": {"Voucher ID": "CPV-1", "Amount": "1250"},
            "extract_amount": 1250,
            "extract_date": date(2026, 1, 5),
            "data_quality_flag": None,
            "rate_source": None,
            "voucher_id": "CPV-1",
            "account_code": "60-10",
            "transaction_date": date(2026, 1, 5),
            "debit": 1250,
            "credit": 0,
            "treasury_type": None,
            "treasury_account_code": None,
            "configuration_version": 3,
            "classification_status": "non_treasury",
            "assigned_category": "Office expense",
            "source_account_name": "Office supplies",
        }]

    result = load_cashflow_classification_preview(
        5,
        9,
        date(2026, 1, 1),
        date(2026, 1, 31),
        page=1,
        limit=50,
        classification_type="counter",
        account_code="60-10",
        category="Office",
        query_fn=query_fn,
    )

    assert result["total_rows"] == 1
    assert result["rows"][0]["row_data"]["Voucher ID"] == "CPV-1"
    assert result["rows"][0]["classification"] == {
        "type": "Counter-account",
        "account_code": "60-10",
        "account_name": "Office supplies",
        "assigned_category": "Office expense",
        "classification_status": "non_treasury",
        "voucher_id": "CPV-1",
        "transaction_date": date(2026, 1, 5),
        "debit": 1250,
        "credit": 0,
        "configuration_version": 3,
    }
    assert len(calls) == 2
    assert all("c.is_current = TRUE" in sql for sql, _ in calls)
    assert "cd.row_data" in calls[1][0]
    assert calls[0][1][:4] == (5, 9, date(2026, 1, 1), date(2026, 1, 31))


def test_cashflows_classification_preview_rejects_unknown_type():
    def query_fn(sql, params):
        raise AssertionError("invalid filters should be rejected before SQL")

    try:
        load_cashflow_classification_preview(
            5,
            9,
            date(2026, 1, 1),
            date(2026, 1, 31),
            classification_type="other",
            query_fn=query_fn,
        )
    except ValueError as exc:
        assert "Invalid classification type" in str(exc)
    else:
        raise AssertionError("invalid classification type was accepted")

def test_cashflows_classification_preview_filters_unassigned_categories():
    calls = []

    def query_fn(sql, params):
        calls.append((sql, params))
        return [{"total": 0}] if "SELECT COUNT(*)" in sql else []

    result = load_cashflow_classification_preview(
        5,
        9,
        date(2026, 1, 1),
        date(2026, 1, 31),
        category="__unassigned__",
        query_fn=query_fn,
    )

    assert result["total_rows"] == 0
    assert len(calls) == 2
    assert all("NULLIF(BTRIM(COALESCE(category.label, '')), '') IS NULL" in sql for sql, _ in calls)
    assert all("__unassigned__" not in params for _, params in calls)