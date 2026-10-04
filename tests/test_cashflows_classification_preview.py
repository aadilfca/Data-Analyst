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
    assert result["duplicate_upload_suppression"] is True
    assert result["source_upload_selection"] == "latest upload per filename"
    assert all("c.is_current = TRUE" in sql for sql, _ in calls)
    assert all("selected_template_uploads" in sql for sql, _ in calls)
    assert "cd.row_data" in calls[1][0]
    assert calls[0][1][:3] == (5, 5, 9)
    assert calls[0][1][3:7] == (5, 9, date(2026, 1, 1), date(2026, 1, 31))


def test_cashflows_classification_preview_searches_all_displayed_classification_values():
    calls = []

    def query_fn(sql, params):
        calls.append((sql, params))
        return [{"total": 0}] if "SELECT COUNT(*)" in sql else []

    result = load_cashflow_classification_preview(
        5,
        9,
        date(2026, 1, 1),
        date(2026, 1, 31),
        search="Cash",
        query_fn=query_fn,
    )

    assert result["total_rows"] == 0
    assert len(calls) == 2
    for sql, params in calls:
        assert "c.treasury_account_code" in sql
        assert "cd.row_data ->> 'Account Name'" in sql
        assert "WHEN c.treasury_type = 'cash' THEN 'Cash'" in sql
        assert params.count("%Cash%") == 7

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

def test_cashflows_classification_preview_uses_latest_upload_per_filename_cte():
    calls = []

    def query_fn(sql, params):
        calls.append((sql, params))
        return [{"total": 0}] if "SELECT COUNT(*)" in sql else []

    result = load_cashflow_classification_preview(
        5,
        9,
        date(2026, 1, 1),
        date(2026, 8, 20),
        page=2,
        limit=50,
        query_fn=query_fn,
    )

    assert result["total_rows"] == 0
    assert len(calls) == 2
    assert all("ROW_NUMBER() OVER" in sql for sql, _ in calls)
    assert all("PARTITION BY COALESCE" in sql for sql, _ in calls)
    assert all("JOIN selected_template_uploads" in sql for sql, _ in calls)
    assert calls[0][1][:3] == (5, 5, 9)
    assert calls[1][1][-2:] == (50, 50)

def test_cashflows_classification_preview_filters_vouchers_by_treasury_view():
    calls = []

    def query_fn(sql, params):
        calls.append((sql, params))
        return [{'total': 0}] if 'SELECT COUNT(*)' in sql else []

    result = load_cashflow_classification_preview(
        5,
        9,
        date(2026, 1, 1),
        date(2026, 1, 31),
        classification_type='counter',
        treasury_view='cash',
        query_fn=query_fn,
    )

    assert result['total_rows'] == 0
    assert len(calls) == 2
    assert all('FROM cash_bank_transaction_classifications treasury' in sql for sql, _ in calls)
    assert all('JOIN selected_template_uploads treasury_upload' in sql for sql, _ in calls)
    assert all('treasury.treasury_type IN (%s)' in sql for sql, _ in calls)
    assert all('treasury.upload_id = c.upload_id' not in sql for sql, _ in calls)
    assert all('cash' in params for _, params in calls)

def test_cashflows_classification_preview_rejects_unknown_treasury_view():
    def query_fn(sql, params):
        raise AssertionError('invalid filters should be rejected before SQL')

    try:
        load_cashflow_classification_preview(
            5,
            9,
            date(2026, 1, 1),
            date(2026, 1, 31),
            treasury_view='wire',
            query_fn=query_fn,
        )
    except ValueError as exc:
        assert 'Invalid treasury view' in str(exc)
    else:
        raise AssertionError('invalid treasury view was accepted')

def test_cashflows_classification_preview_matches_summary_fallback_category():
    calls = []

    def query_fn(sql, params):
        calls.append((sql, params))
        return [{'total': 0}] if 'SELECT COUNT(*)' in sql else []

    result = load_cashflow_classification_preview(
        5,
        1,
        date(2026, 7, 1),
        date(2026, 7, 31),
        classification_type='counter',
        account_code='20-15-50-15-0061',
        category='HTY YOUNAS GROUP',
        treasury_view='cash',
        query_fn=query_fn,
    )

    assert result['total_rows'] == 0
    assert len(calls) == 2
    assert all('NULLIF(BTRIM(category.label)' in sql for sql, _ in calls)
    assert all('Account Name' in sql for sql, _ in calls)
    assert all('%HTY YOUNAS GROUP%' in params for _, params in calls)

def test_cashflows_classification_preview_requires_cash_or_bank_leg_for_combined_view():
    calls = []

    def query_fn(sql, params):
        calls.append((sql, params))
        return [{'total': 0}] if 'SELECT COUNT(*)' in sql else []

    result = load_cashflow_classification_preview(
        5,
        1,
        date(2026, 7, 1),
        date(2026, 7, 31),
        classification_type='counter',
        account_code='20-15-50-15-0061',
        category='HTY YOUNAS GROUP',
        treasury_view='combined',
        query_fn=query_fn,
    )

    assert result['total_rows'] == 0
    assert len(calls) == 2
    assert all('treasury.treasury_type IN (%s, %s)' in sql for sql, _ in calls)
    assert all('JOIN selected_template_uploads treasury_upload' in sql for sql, _ in calls)
    assert all('treasury.upload_id = c.upload_id' not in sql for sql, _ in calls)
    assert all('cash' in params and 'bank' in params for _, params in calls)
