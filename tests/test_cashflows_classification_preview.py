from datetime import date
import json
import sqlite3
from types import SimpleNamespace

import pytest

from services.cash_bank_classification import load_cashflow_classification_preview, normalize_cashflow_account_selection


@pytest.mark.parametrize('codes,view,expected', [
    (None,'bank',None), (['B1','B2','C1'],'bank',None),
    ([' C1 ','B2','C1'],'bank',['B2']), (['C1','B2'],'cash',['C1']),
    (['B2','B1','B1'],'combined',['B1','B2']), (['C1'],' BANK ',ValueError),
    (['UNKNOWN'],'combined',ValueError), (['c1'],'cash',ValueError),
    ([], 'combined',ValueError), ('C1','cash',ValueError),
])
def test_selection_normalizes_view_and_codes_without_changing_configured_identity(codes,view,expected):
    accounts={'B1':'Bank','B2':'Bank','C1':'Cash'}
    before=dict(accounts)
    if expected is ValueError:
        with pytest.raises(ValueError): normalize_cashflow_account_selection(codes,accounts,view)
    else:
        assert normalize_cashflow_account_selection(codes,accounts,view)==expected
    assert accounts==before


def test_explicit_all_of_a_single_type_cannot_enable_an_incompatible_view():
    with pytest.raises(ValueError,match='compatible'):
        normalize_cashflow_account_selection(['C1'],{'C1':'Cash'},'bank')


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
    assert result["duplicate_upload_suppression"] is False
    assert result["source_mode"] == "client_data_derived"
    assert all("cd.is_deleted = FALSE" in sql for sql, _ in calls)
    assert all("cash_bank_transaction_classifications" not in sql for sql, _ in calls)
    assert "cd.row_data" in calls[1][0]
    assert calls[0][1][:4] == (5, 9, date(2026, 1, 1), date(2026, 1, 31))


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
        assert "cd.derived_treasury_account_code" in sql
        assert "cd.row_data ->> 'Account Name'" in sql
        assert "WHEN cd.derived_treasury_type = 'cash' THEN 'Cash'" in sql
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

def test_cashflows_classification_preview_reads_active_source_without_upload_reselection():
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
    assert all("ROW_NUMBER() OVER" not in sql for sql, _ in calls)
    assert all("selected_report_vouchers" not in sql for sql, _ in calls)
    assert all("FROM client_data cd" in sql for sql, _ in calls)
    assert calls[0][1][:2] == (5, 9)
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
    assert all('FROM client_data treasury' in sql for sql, _ in calls)
    assert all('treasury.derived_voucher_id = cd.derived_voucher_id' in sql for sql, _ in calls)
    assert all('treasury.derived_treasury_type IN (%s)' in sql for sql, _ in calls)
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
    assert all('cd.derived_account_name' in sql for sql, _ in calls)
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
    assert all('treasury.derived_treasury_type IN (%s, %s)' in sql for sql, _ in calls)
    assert all('treasury.derived_voucher_id = cd.derived_voucher_id' in sql for sql, _ in calls)
    assert all('treasury.upload_id = c.upload_id' not in sql for sql, _ in calls)
    assert all('cash' in params and 'bank' in params for _, params in calls)


@pytest.fixture
def canonical_preview_db():
    """Execute preview SQL in memory, without legacy copies or a live connection.

    Only placeholder and JSON-function spellings differ from PostgreSQL; joins,
    predicates, ordering and pagination execute against real fixture rows.
    """
    connection = sqlite3.connect(':memory:', check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.create_function('BTRIM', 1, lambda value: value.strip() if value is not None else None)
    connection.executescript('''
        CREATE TABLE client_data (
            id INTEGER PRIMARY KEY, client_id INTEGER, template_id INTEGER,
            upload_id INTEGER, is_deleted INTEGER, change_detection_status TEXT,
            row_data TEXT, extract_amount REAL, extract_date TEXT,
            data_quality_flag TEXT, rate_source TEXT,
            derived_voucher_id TEXT, derived_account_code TEXT,
            derived_account_name TEXT, derived_transaction_date TEXT,
            derived_debit REAL, derived_credit REAL, derived_treasury_type TEXT,
            derived_treasury_account_code TEXT, derived_version INTEGER,
            derived_classification_status TEXT, derived_transaction_month TEXT,
            derived_transaction_fortnight TEXT
            , source_row_number INTEGER, source_order_upload_id INTEGER
        );
        CREATE TABLE account_control_cashflow_category_assignments (
            client_id INTEGER, account_code TEXT, category_id INTEGER
        );
        CREATE TABLE account_control_cashflow_categories (
            id INTEGER, client_id INTEGER, label TEXT, is_active INTEGER
        );
        INSERT INTO account_control_cashflow_categories VALUES (1, 5, 'Office expense', 1);
        INSERT INTO account_control_cashflow_category_assignments VALUES (5, 'EXP', 1);
    ''')
    calls = []

    def insert(row_id, voucher='V1', account='EXP', debit=60.125, credit=0, **overrides):
        values = dict(
            id=row_id, client_id=5, template_id=9, upload_id=11,
            is_deleted=0, change_detection_status=None,
            row_data=json.dumps({'Voucher ID': voucher, 'Voucher Detail': 'Stationery'}),
            extract_amount=1, extract_date='2026-01-05',
            derived_voucher_id=voucher, derived_account_code=account,
            derived_account_name='Office supplies', derived_transaction_date='2026-01-05',
            derived_debit=debit, derived_credit=credit, derived_treasury_type=None,
            derived_version=3, derived_classification_status='non_treasury',
            derived_transaction_month='2026-01', derived_transaction_fortnight='2026-01-H1',
        )
        values.update(overrides)
        connection.execute(
            f"INSERT INTO client_data ({', '.join(values)}) VALUES ({', '.join('?' for _ in values)})",
            tuple(values.values()),
        )

    def query(sql, params):
        calls.append((sql, params))
        executable = sql.replace('%s', '?').replace('jsonb_each_text', 'json_each').replace('::double precision', '')
        bound = []
        for value in params:
            if isinstance(value, list):
                executable = executable.replace('= ANY(?)', 'IN (' + ','.join('?' for _ in value) + ')', 1)
                bound.extend(value)
            else:
                bound.append(value.isoformat() if isinstance(value, date) else value)
        rows = [dict(row) for row in connection.execute(executable, bound)]
        for row in rows:
            if 'row_data' in row:
                row['row_data'] = json.loads(row['row_data'])
        return rows

    insert(1, account='CASH', debit=0, credit=130.25, upload_id=10,
           derived_treasury_type='cash', derived_classification_status='cash')
    insert(2)
    insert(3)  # Legitimate repeated source line must remain a separate transaction.
    insert(4, voucher='V2', account='BANK', debit=25, derived_treasury_type='bank',
           derived_classification_status='bank')
    insert(5, voucher='V2', account='SALES', debit=0, credit=25)
    insert(6, voucher='NO-TREASURY')
    insert(7, is_deleted=1)
    insert(8, change_detection_status='deleted')
    insert(9, derived_transaction_date='2026-02-01')
    insert(10, client_id=6)
    insert(11, template_id=8)
    insert(12, debit=0)
    insert(13, account='')
    insert(14, voucher='')
    insert(15, account='HIST', debit=10, derived_account_name='Historical supplier')
    try:
        yield SimpleNamespace(query=query, insert=insert, connection=connection, calls=calls)
    finally:
        connection.close()


def _preview(db, **filters):
    return load_cashflow_classification_preview(
        5, 9, date(2026, 1, 1), date(2026, 1, 31), query_fn=db.query, **filters,
    )


def test_selected_preview_filters_vouchers_separately_from_counter_account_and_keeps_repeats(canonical_preview_db):
    db = canonical_preview_db
    before = list(db.connection.execute('SELECT * FROM client_data ORDER BY id'))
    selected = _preview(db, selected_treasury_account_codes=[' CASH ', 'CASH'],
        classification_type='counter', account_code='EXP', category='Office expense')
    assert [row['id'] for row in selected['rows']] == [2, 3]
    assert _preview(db, selected_treasury_account_codes=['BANK'], account_code='EXP')['total_rows'] == 0
    assert [row['id'] for row in _preview(db, selected_treasury_account_codes=['BANK'])['rows']] == [4, 5]
    assert len(db.calls) == 6  # One count/page pair per request, never per voucher/account.
    assert list(db.connection.execute('SELECT * FROM client_data ORDER BY id')) == before


def test_selected_preview_retains_other_treasury_legs_as_context_not_eligibility(canonical_preview_db):
    db = canonical_preview_db
    db.insert(20, voucher='V1', account='OTHERBANK', debit=0, credit=5,
        derived_treasury_type='bank', derived_classification_status='bank')
    db.insert(21, voucher='V1', account='DORMANT', debit=0, credit=0,
        derived_treasury_type='bank', derived_classification_status='bank')
    result = _preview(db, selected_treasury_account_codes=['CASH'])
    row = next(row for row in result['rows'] if row['id'] == 20)
    assert row['classification']['account_scope'] == 'Other Cash/Bank account — voucher context'
    assert _preview(db, selected_treasury_account_codes=['DORMANT'])['total_rows'] == 0
    assert _preview(db, selected_treasury_account_codes=['OTHERBANK'], treasury_view='cash')['total_rows'] == 0


@pytest.mark.parametrize('selection', [[], [''], ['  '], 'CASH', [None], [123]])
def test_selected_preview_service_rejects_malformed_scopes(selection):
    with pytest.raises(ValueError, match='Select'):
        load_cashflow_classification_preview(5, 9, '2026-01-01', '2026-01-31',
            selected_treasury_account_codes=selection, query_fn=lambda *args: pytest.fail('Unexpected read'))


def test_preview_executes_without_retired_tables_and_preserves_source_lines(canonical_preview_db):
    db = canonical_preview_db
    before = list(db.connection.execute('SELECT * FROM client_data ORDER BY id'))
    result = _preview(db, classification_type='counter', account_code='exp', category='Office expense')

    assert result['total_rows'] == 2
    assert [row['id'] for row in result['rows']] == [2, 3]
    assert [row['classification']['debit'] for row in result['rows']] == [60.125, 60.125]
    assert all(row['extract_amount'] == 1 for row in result['rows'])
    assert result['rows'][0]['row_data']['Voucher Detail'] == 'Stationery'
    assert result['rows'][0]['classification']['assigned_category'] == 'Office expense'
    assert list(db.connection.execute('SELECT * FROM client_data ORDER BY id')) == before


@pytest.mark.parametrize(('filters', 'expected_ids'), [
    ({'classification_type': 'counter'}, [2, 3, 5, 15]),
    ({'classification_type': 'counter', 'treasury_view': 'cash'}, [2, 3, 15]),
    ({'classification_type': 'counter', 'treasury_view': 'bank'}, [5]),
    ({'classification_type': 'cash'}, [1]),
    ({'classification_type': 'bank'}, [4]),
    ({'account_code': 'EXP', 'search': 'stationery'}, [2, 3]),
    ({'category': 'historical supplier'}, [15]),
    ({'category': '__unassigned__', 'classification_type': 'counter'}, [5, 15]),
    ({'account_code': 'EXP', 'search': 'missing'}, []),
    ({'account_code': 'EXP', 'page': 2, 'limit': 1}, [3]),
    ({'account_code': 'EXP', 'page': 3, 'limit': 1}, []),
])
def test_canonical_preview_filters_and_pagination(canonical_preview_db, filters, expected_ids):
    result = _preview(canonical_preview_db, **filters)
    assert [row['id'] for row in result['rows']] == expected_ids
    if filters.get('page'):
        assert result['total_rows'] == 2


def test_preview_sorts_dates_then_original_source_rows_before_page_slicing(canonical_preview_db):
    db = canonical_preview_db
    db.connection.execute('UPDATE client_data SET source_row_number=8,source_order_upload_id=70 WHERE id=2')
    db.connection.execute('UPDATE client_data SET source_row_number=3,source_order_upload_id=70 WHERE id=3')
    db.insert(90, voucher='EARLIER', account='EXP', derived_transaction_date='2026-01-01',
              source_row_number=99, source_order_upload_id=80)
    db.insert(91, voucher='EARLIER', account='CASH', debit=0, credit=60.125,
              derived_transaction_date='2026-01-01', derived_treasury_type='cash', derived_classification_status='cash')
    before = list(db.connection.execute('SELECT * FROM client_data ORDER BY id'))
    pages = [_preview(db, account_code='EXP', page=p, limit=1) for p in (1,2,3)]
    assert [p['rows'][0]['id'] for p in pages] == [90,3,2]
    assert all(p['total_rows'] == 3 for p in pages)
    assert [p['rows'][0]['classification']['transaction_date'] for p in pages] == ['2026-01-01','2026-01-05','2026-01-05']
    assert list(db.connection.execute('SELECT * FROM client_data ORDER BY id')) == before


@pytest.mark.parametrize('overrides', [
    {'is_deleted': 1}, {'change_detection_status': 'deleted'},
    {'client_id': 6}, {'template_id': 8},
    {'derived_transaction_date': '2026-02-01'},
    {'derived_credit': 0}, {'derived_account_code': ''},
])
def test_ineligible_treasury_leg_cannot_admit_counter_rows(canonical_preview_db, overrides):
    db = canonical_preview_db
    db.insert(20, voucher='ISOLATED')
    leg = dict(derived_treasury_type='cash', derived_classification_status='cash', **overrides)
    db.insert(21, voucher='ISOLATED', account='CASH', debit=0,
              credit=10, **leg)
    result = _preview(db, search='ISOLATED')
    assert result['total_rows'] == 0


def test_preview_matches_canonical_report_population(canonical_preview_db):
    from services.ledger_report_source import load_cashflow_client_data_source

    db = canonical_preview_db
    source = load_cashflow_client_data_source(
        5, journal_template_id=9, period_start='2026-01-01', period_end='2026-01-31',
        query_fn=db.query,
    )
    preview = _preview(db)
    assert preview['total_rows'] == source.metadata['source_row_count']
    for field in ('debit', 'credit'):
        assert sum(row['classification'][field] for row in preview['rows']) == source.journal[field].sum()


def test_preview_http_route_reads_canonical_rows_and_validates_filters(canonical_preview_db, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routers import data

    db = canonical_preview_db

    def query(sql, params):
        if 'FROM clients' in sql:
            return [{'id': 5}]
        if 'FROM templates' in sql:
            return [{'id': 9, 'template_name': 'Transaction Report'}]
        if 'FROM account_control_cashflow_settings' in sql:
            return [{'source_template_id': 9}]
        if 'FROM account_control_cashflow_treasury_accounts' in sql:
            return [{'account_code':'CASH','treasury_type':'cash'}, {'account_code':'BANK','treasury_type':'bank'}]
        # Any unexpected prepared/copy-table query fails against this database.
        return db.query(sql, params)

    monkeypatch.setattr(data, 'execute_query_pg', query)
    monkeypatch.setattr(data, 'get_template_columns_ordered', lambda template_id: [])
    snapshots=[]
    def snapshot(callback):
        snapshots.append(True)
        return callback(None)
    monkeypatch.setattr(data.database, 'run_read_snapshot_pg', snapshot)
    app = FastAPI()
    app.include_router(data.router)
    client = TestClient(app)
    params = dict(client_id=5, template_id=9, date_start='2026-01-01', date_end='2026-01-31',
                  account_code='EXP', classification_type='counter', treasury_view='cash')
    response = client.get('/api/data/cashflows-classification-preview', params=params)
    assert response.status_code == 200
    assert response.json()['total_rows'] == 2
    assert response.json()['rows'][0]['classification']['debit'] == 60.125
    assert response.json()['source_mode'] == 'client_data_derived'
    assert snapshots==[]
    response = client.get('/api/data/cashflows-classification-preview',params=params|
        dict(selected_treasury_account_codes=['CASH']))
    assert response.status_code==200,response.text
    assert response.json()['total_rows']==2
    assert response.json()['account_selection']['account_codes']==['CASH']
    assert snapshots==[True]
    assert client.get('/api/data/cashflows-classification-preview',params=params|
        dict(selected_treasury_account_codes=['BANK'])).status_code==400
    for codes in (['UNKNOWN'],[''],['CASH','UNKNOWN']):
        assert client.get('/api/data/cashflows-classification-preview',params=params|
            dict(selected_treasury_account_codes=codes)).status_code==400
    assert client.get('/api/data/cashflows-classification-preview',params=params|
        dict(selected_treasury_account_codes=['BANK','CASH'])).json()['total_rows']==2
    for invalid_filter in ({'classification_type': 'other'}, {'treasury_view': 'wire'}):
        response = client.get('/api/data/cashflows-classification-preview', params=params | invalid_filter)
        assert response.status_code == 400
