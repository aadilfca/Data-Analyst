import pytest
from pathlib import Path

from playwright.sync_api import sync_playwright


TEMPLATES_HTML = Path(__file__).resolve().parents[1] / "static" / "templates.html"
TEMPLATES_JS = Path(__file__).resolve().parents[1] / "static" / "templates.js"


@pytest.mark.parametrize('choice,base,role', [
    ('Text', 'Text', 'document_id'), ('Numeric', 'Numeric', 'footing'),
    ('Text', 'Text', None), ('Numeric', 'Numeric', None), ('Date', 'Date', None),
])
def test_reconciliation_column_types_preserve_base_types(choice, base, role):
    from database import resolve_column_type
    assert resolve_column_type(choice, role) == (base, role)


def test_invalid_role_base_combinations_are_rejected():
    from database import resolve_column_type
    with pytest.raises(ValueError):
        resolve_column_type('Numeric', 'document_id')
    with pytest.raises(ValueError):
        resolve_column_type('Text', 'footing')


def test_reactivating_shared_id_column_cannot_introduce_two_document_keys(monkeypatch):
    from fastapi import HTTPException
    from routers import templates
    columns = [dict(id=i, column_name=name, data_type='Text', reconciliation_role='document_id', is_active=i==1)
               for i, name in [(1, 'Voucher'), (2, 'Reference')]]
    def query(sql, params=None):
        if 'SELECT DISTINCT template_id' in sql:
            return [{'template_id': 7}]
        if 'FROM template_column_mappings p' in sql:
            return columns
        return [{'id': 7}]
    monkeypatch.setattr(templates, 'execute_query_pg', query)
    with pytest.raises(HTTPException) as error:
        templates._guard_column_role_change(2, {'is_active': True})
    assert 'one Document ID column' in error.value.detail


def test_role_edit_blocks_changed_stored_keys_and_accepts_equivalent_values(monkeypatch):
    from fastapi import HTTPException
    from routers import templates
    before = [{'id': 1, 'column_name': 'Voucher', 'data_type': 'Text'},
              {'id': 2, 'column_name': 'Document', 'data_type': 'Text'}]
    after = [before[0], dict(before[1], reconciliation_role='document_id')]
    queries = []
    def query(sql, params=None):
        queries.append((sql, params))
        return [{'data_purpose': 'standard'}] if 'FROM templates' in sql else [{'id': 99}]
    monkeypatch.setattr(templates, 'execute_query_pg', query)
    with pytest.raises(HTTPException) as error:
        templates._guard_document_configuration(7, after, before)
    assert error.value.status_code == 409
    assert queries[-1][1] == (7, 'Voucher', 'Document', 'Document')
    monkeypatch.setattr(templates, 'execute_query_pg', lambda sql, params=None: [] )
    templates._guard_document_configuration(7, after, before)


def test_template_roles_keep_shared_po_column_independent():
    from routers.templates import _assign_template_roles, _master_column_type
    from fastapi import HTTPException
    masters = {1: dict(id=1, column_name='PO No', data_type='Text'),
               2: dict(id=2, column_name='GRN No', data_type='Text'),
               3: dict(id=3, column_name='Amount', data_type='Numeric')}
    po = _assign_template_roles([1, 3], masters, column_roles={1:'document_id', 3:'footing'})
    grn = _assign_template_roles([2, 1, 3], masters, column_roles={2:'document_id', 1:None})
    assert po[0]['reconciliation_role'] == 'document_id'
    assert grn[1]['reconciliation_role'] is None
    assert grn[0]['reconciliation_role'] == 'document_id'
    assert all('reconciliation_role' not in c for c in masters.values())
    assert _assign_template_roles([1,3], masters, po)[0]['reconciliation_role'] == 'document_id'
    with pytest.raises(HTTPException):
        _master_column_type('Text','document_id')
    with pytest.raises(HTTPException):
        _assign_template_roles([1,3], masters, column_roles={2:'document_id'})
    with pytest.raises(HTTPException):
        _assign_template_roles([1,2], masters, column_roles={1:'document_id',2:'document_id'})


def test_column_roles_sqlite_upgrade_is_idempotent(tmp_path, monkeypatch):
    import sqlite3
    import database
    path = tmp_path / 'role-test.db'
    with sqlite3.connect(path) as connection:
        connection.execute('CREATE TABLE master_columns (id INTEGER PRIMARY KEY, column_name TEXT UNIQUE, data_type TEXT, category TEXT, is_active INTEGER, notes TEXT)')
        connection.execute("INSERT INTO master_columns VALUES (100, 'Test key', 'Text', 'General', 1, NULL)")
    monkeypatch.setattr(database, 'DB_NAME', str(path))
    database.init_db()
    database.init_db()
    with sqlite3.connect(path) as connection:
        assert connection.execute('SELECT data_type FROM master_columns WHERE id=100').fetchone() == ('Text',)
        assert 'reconciliation_role' in {row[1] for row in connection.execute('PRAGMA table_info(template_column_mappings)')}
        assert 'reconciliation_role' not in {row[1] for row in connection.execute('PRAGMA table_info(master_columns)')}


def test_column_roles_postgres_upgrade_skips_existing_column():
    from unittest.mock import MagicMock
    from database import _ensure_template_column_roles
    conn = MagicMock()
    cursor = conn.cursor.return_value
    cursor.fetchone.return_value = (1,)
    _ensure_template_column_roles(conn, postgres=True)
    assert cursor.execute.call_count == 1
    cursor.reset_mock()
    cursor.fetchone.side_effect = [None, None, (None,)]
    _ensure_template_column_roles(conn, postgres=True)
    assert any('ALTER TABLE template_column_mappings' in call.args[0] for call in cursor.execute.call_args_list)
    assert not any('ALTER TABLE master_columns' in call.args[0] for call in cursor.execute.call_args_list)


def test_comparison_defaults_check_every_field_except_ledger_balance():
    from database import resolve_column_comparison
    ledger = dict(data_purpose='account_ledger', ledger_columns={'balance': 7})
    for cid in range(1,8):
        column = dict(id=cid, column_name='Balance' if cid==7 else 'Field')
        assert resolve_column_comparison(column,ledger) is (cid!=7)
        assert resolve_column_comparison(column,{'data_purpose':'standard'}) is True
    assert resolve_column_comparison({'id':7,'compare_for_changes':True},ledger) is True
    assert resolve_column_comparison({'id':1,'compare_for_changes':False},ledger) is False


def test_comparison_upgrade_preserves_agreement_flags_conflicts_and_runs_once():
    import sqlite3
    from database import _ensure_template_comparisons
    conn=sqlite3.connect(':memory:')
    conn.executescript('''
        CREATE TABLE template_column_mappings(id INTEGER PRIMARY KEY,template_id INTEGER,column_id INTEGER,sort_order INTEGER,reconciliation_role TEXT);
        INSERT INTO template_column_mappings VALUES(1,10,1,0,'document_id'),(2,10,2,1,NULL),(3,10,3,2,'footing');
        CREATE TABLE saved_column_mappings(client_id INTEGER,template_id INTEGER,template_column_id INTEGER,compare_for_changes INTEGER);
        INSERT INTO saved_column_mappings VALUES(1,10,1,1),(2,10,1,1),(1,10,2,0),(2,10,2,1),(1,10,3,0);
        CREATE TABLE upload_validation_plans(status TEXT);
        INSERT INTO upload_validation_plans VALUES('ready');
    ''')
    saved=conn.execute('SELECT * FROM saved_column_mappings').fetchall()
    _ensure_template_comparisons(conn)
    assert conn.execute('SELECT compare_for_changes,comparison_review_required FROM template_column_mappings ORDER BY id').fetchall() == [(1,0),(None,1),(0,0)]
    assert conn.execute('SELECT * FROM saved_column_mappings').fetchall()==saved
    assert conn.execute('SELECT status FROM upload_validation_plans').fetchone()[0]=='superseded'
    conn.execute('UPDATE template_column_mappings SET compare_for_changes=0 WHERE id=1')
    _ensure_template_comparisons(conn)
    assert conn.execute('SELECT compare_for_changes FROM template_column_mappings WHERE id=1').fetchone()[0]==0
    conn.close()


def test_failed_comparison_upgrade_is_atomic():
    import sqlite3
    from database import _ensure_template_comparisons
    conn=sqlite3.connect(':memory:')
    conn.executescript('CREATE TABLE template_column_mappings(id INTEGER PRIMARY KEY); CREATE TABLE saved_column_mappings(compare_for_changes INTEGER);')
    with pytest.raises(sqlite3.OperationalError):
        _ensure_template_comparisons(conn)
    assert [r[1] for r in conn.execute('PRAGMA table_info(template_column_mappings)')]==['id']
    conn.close()


def test_explicit_comparison_edit_resolves_conflicts_and_omitted_edits_preserve_flags():
    from routers.templates import _assign_template_roles
    master={1:dict(id=1,column_name='Reference',data_type='Text')}
    before=[dict(master[1],compare_for_changes=None,comparison_review_required=True)]
    assert _assign_template_roles([1],master,before)[0]['comparison_review_required'] is True
    column=_assign_template_roles([1],master,before,column_comparisons={1:False})[0]
    assert column['compare_for_changes'] is False and column['comparison_review_required'] is False
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        _assign_template_roles([1],master,before,column_comparisons={99:True})


def test_backup_comparisons_preserve_new_settings_and_flag_legacy_disagreements():
    from types import SimpleNamespace
    from routers.templates import _backup_template_comparisons
    data=SimpleNamespace(mappings=[dict(id=1,template_id=10,column_id=1),
        dict(id=2,template_id=10,column_id=2),dict(id=3,template_id=10,column_id=3,compare_for_changes=None)],
        saved_column_mappings=[dict(template_id=10,template_column_id=1,compare_for_changes=False),
            dict(template_id=10,template_column_id=2,compare_for_changes=False),
            dict(template_id=10,template_column_id=2,compare_for_changes=True),
            dict(template_id=10,template_column_id=3,compare_for_changes=False)])
    assert _backup_template_comparisons(data)=={1:(False,False),2:(None,True),3:(None,False)}


def test_template_checkbox_column_defaults_and_cancel_preserve_saved_settings(ledger_template_page):
    page=ledger_template_page
    page.evaluate("""async()=>{
        await openModal('Edit Template',{id:10,template_name:'Ordinary template',columns:window.__columns});
    }""")
    assert page.locator('.selected-columns-heading').inner_text().count('Compare for changes')==1
    assert all(page.locator('.template-compare-cell input').evaluate_all('(items)=>items.map(e=>e.checked)'))
    page.get_by_label('Compare for changes: Voucher Detail',exact=True).uncheck()
    assert page.locator('.template-compare-cell').evaluate_all('(items)=>items.every(e=>e.innerText.trim()==="")')
    page.locator('#cancelTemplateBtn').click()
    page.evaluate("""async()=>{
        await openModal('Edit Template',{id:10,template_name:'Ordinary template',columns:window.__columns});
    }""")
    assert page.get_by_label('Compare for changes: Voucher Detail',exact=True).is_checked()
    page.locator('#cancelTemplateBtn').click()
    page.locator('#prepareLedgerBtn').click()
    page.wait_for_function("ledgerSetupDraft !== null")
    assert page.get_by_label('Compare for changes: Balance (create on Save)',exact=True).is_checked() is False
    page.locator('#saveTemplateBtn').click()
    page.wait_for_function("window.__calls.some(c=>c.url==='/api/templates/ledger-setup'&&c.method==='POST')")
    body=page.evaluate("window.__calls.find(c=>c.url==='/api/templates/ledger-setup'&&c.method==='POST').body")
    assert body['column_comparisons']=={'voucher':True,'date':True,'description':True,'debit':True,'credit':True,'balance':False}


def test_legacy_role_upgrade_preserves_data_without_marking_empty_reference_templates():
    import sqlite3
    from database import _ensure_template_column_roles
    conn = sqlite3.connect(':memory:')
    conn.executescript('''
        CREATE TABLE master_columns(id INTEGER PRIMARY KEY, column_name TEXT, data_type TEXT,
            is_active INTEGER, reconciliation_role TEXT);
        CREATE TABLE template_column_mappings(id INTEGER PRIMARY KEY,template_id INTEGER,column_id INTEGER,sort_order INTEGER);
        CREATE TABLE client_data(id INTEGER PRIMARY KEY,template_id INTEGER,is_deleted INTEGER,row_data TEXT,document_group_id TEXT);
        CREATE TABLE upload_validation_plans(status TEXT);
        INSERT INTO master_columns VALUES(1,'PO No','Text',1,'document_id'),(2,'GRN No','Text',1,NULL),(3,'Amount','Numeric',1,'footing');
        INSERT INTO template_column_mappings VALUES(1,10,1,0),(2,10,3,1),
            (3,20,2,0),(4,20,1,1),(5,30,2,0),(6,30,1,1);
        INSERT INTO client_data VALUES(99,30,0,'{"PO No":"PO-1","GRN No":"G-1"}','30-PO-1');
        INSERT INTO upload_validation_plans VALUES('ready');
    ''')
    before = conn.execute('SELECT * FROM client_data').fetchall()
    _ensure_template_column_roles(conn)
    assert conn.execute('SELECT id,reconciliation_role FROM template_column_mappings ORDER BY id').fetchall() == [
        (1,'document_id'),(2,None),(3,None),(4,None),(5,None),(6,'document_id')]
    assert conn.execute('SELECT * FROM client_data').fetchall() == before
    assert conn.execute('SELECT status FROM upload_validation_plans').fetchone()[0] == 'superseded'
    # Later template edits stay local and must not re-inherit the dormant global field.
    conn.execute('UPDATE template_column_mappings SET reconciliation_role=NULL WHERE id=1')
    _ensure_template_column_roles(conn)
    assert conn.execute('SELECT reconciliation_role FROM template_column_mappings WHERE id=1').fetchone()[0] is None
    assert conn.execute('SELECT reconciliation_role FROM master_columns WHERE id=1').fetchone()[0] == 'document_id'
    conn.close()


def test_backup_roles_preserve_scoped_choices_and_adapt_legacy_document_key():
    from types import SimpleNamespace
    from routers.templates import _backup_mapping_roles
    backup = SimpleNamespace(columns=[dict(id=1,column_name='PO No',data_type='Text',reconciliation_role='document_id'),
        dict(id=2,column_name='GRN No',data_type='Text')], client_data=[], mappings=[
        dict(id=1,template_id=10,column_id=1,sort_order=0),
        dict(id=2,template_id=20,column_id=2,sort_order=0,reconciliation_role='document_id'),
        dict(id=3,template_id=20,column_id=1,sort_order=1,reconciliation_role=None)])
    assert _backup_mapping_roles(backup) == {1:'document_id',2:'document_id',3:None}


def test_failed_role_upgrade_rolls_back_metadata_and_preserves_legacy_settings():
    import sqlite3
    from database import _ensure_template_column_roles
    conn = sqlite3.connect(':memory:')
    conn.executescript('''
        CREATE TABLE master_columns(id INTEGER PRIMARY KEY, reconciliation_role TEXT);
        INSERT INTO master_columns VALUES(1,'document_id');
        CREATE TABLE template_column_mappings(id INTEGER PRIMARY KEY,template_id INTEGER,column_id INTEGER,sort_order INTEGER);
        CREATE TABLE client_data(id INTEGER PRIMARY KEY);
        INSERT INTO client_data VALUES(99);
    ''')
    with pytest.raises(sqlite3.OperationalError):
        _ensure_template_column_roles(conn)
    assert 'reconciliation_role' not in {row[1] for row in conn.execute('PRAGMA table_info(template_column_mappings)')}
    assert conn.execute('SELECT * FROM client_data').fetchall() == [(99,)]
    assert conn.execute('SELECT reconciliation_role FROM master_columns').fetchone()[0] == 'document_id'
    conn.close()


def test_invalid_backup_roles_are_rejected_before_a_database_connection(monkeypatch):
    from routers import templates
    from fastapi import HTTPException
    monkeypatch.setattr(templates,'get_raw_conn_pg',lambda:pytest.fail('Must validate roles before opening a restore transaction'))
    backup = templates.BackupData(columns=[dict(id=1,column_name='PO No',data_type='Text'),
        dict(id=2,column_name='GRN No',data_type='Text')],templates=[],mappings=[
        dict(id=1,template_id=10,column_id=1,sort_order=0,reconciliation_role='document_id'),
        dict(id=2,template_id=10,column_id=2,sort_order=1,reconciliation_role='document_id')],clients=[],settings=[])
    with pytest.raises(HTTPException):
        templates.import_backup(backup)


def test_template_save_writes_mapping_roles_and_preserves_omitted_roles(monkeypatch):
    from contextlib import nullcontext
    from routers import templates
    masters = [dict(id=1,column_name='PO No',data_type='Text'),dict(id=2,column_name='Amount',data_type='Numeric')]
    before = [dict(masters[0],reconciliation_role='document_id'),dict(masters[1],reconciliation_role='footing')]
    writes = []
    def query(sql, params=None):
        if 'FROM template_column_mappings p' in sql:
            assert 'p.reconciliation_role' in sql
            return before
        if 'FROM master_columns' in sql:
            return masters
        if 'id !=' in sql or 'advisory' in sql:
            return []
        if 'FROM templates' in sql:
            return [dict(id=10,data_purpose='standard',ledger_columns={})]
        raise AssertionError(sql)
    monkeypatch.setattr(templates,'pg_transaction',nullcontext)
    monkeypatch.setattr(templates,'execute_query_pg',query)
    monkeypatch.setattr(templates,'execute_write_pg',lambda sql,params=None:writes.append((sql,params)))
    monkeypatch.setattr(templates,'log_audit',lambda *args:None)
    monkeypatch.setattr(templates,'invalidate_template_cache',lambda *args:None)
    for roles, expected in [(None,['document_id','footing']),({1:'document_id',2:None},['document_id',None])]:
        writes.clear()
        templates.update_template(10,templates.TemplateUpdate(template_name='PO',selected_columns=[1,2],column_roles=roles))
        mapping_writes = [(sql,params) for sql,params in writes if sql.startswith('INSERT INTO template_column_mappings')]
        assert [params[3] for sql,params in mapping_writes] == expected
        assert all(params[0] == 10 for sql,params in mapping_writes)
        assert not any('UPDATE master_columns' in sql or 'UPDATE client_data' in sql for sql,params in writes)


def test_template_role_editor_keeps_po_reference_independent_and_cancel_discards_draft():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(TEMPLATES_HTML.read_text(encoding='utf-8'))
        page.add_script_tag(content="""
            var API_BASE=''; window.LoadingUI={showSpinner(){},hideSpinner(){}};
            window.__requests=[];
            window.__masters=[{id:1,column_name:'PO No',data_type:'Text'},
                {id:2,column_name:'GRN No',data_type:'Text'}, {id:3,column_name:'Amount',data_type:'Numeric'}];
            window.fetch=async(url,options={})=>{
                if(options.method) window.__requests.push(JSON.parse(options.body));
                const value=url==='/api/columns'?window.__masters:options.method?{message:'Saved'}:[];
                return new Response(JSON.stringify(value),{status:200,headers:{'Content-Type':'application/json'}});
            };
            window.__po={id:10,template_name:'Purchase Order',columns:[
                {...window.__masters[0],reconciliation_role:'document_id'},
                {...window.__masters[2],reconciliation_role:'footing'}]};
            window.__grn={id:20,template_name:'GRN',columns:[
                {...window.__masters[1],reconciliation_role:'document_id'},window.__masters[0],window.__masters[2]]};
        """)
        page.add_script_tag(path=str(TEMPLATES_JS))
        try:
            page.evaluate("openModal('Edit Template',window.__po)")
            assert page.get_by_label('Reconciliation role for PO No',exact=True).input_value() == 'document_id'
            assert page.get_by_label('Reconciliation role for Amount',exact=True).input_value() == 'footing'
            page.get_by_label('Reconciliation role for PO No',exact=True).select_option('')
            page.locator('#cancelTemplateBtn').click()
            assert page.evaluate('window.__requests.length') == 0
            page.evaluate("openModal('Edit Template',window.__po)")
            assert page.get_by_label('Reconciliation role for PO No',exact=True).input_value() == 'document_id'
            page.locator('#cancelTemplateBtn').click()
            page.evaluate("openModal('Edit Template',window.__grn)")
            assert page.get_by_label('Reconciliation role for PO No',exact=True).input_value() == ''
            assert page.get_by_label('Reconciliation role for GRN No',exact=True).input_value() == 'document_id'
            page.get_by_label('Reconciliation role for Amount',exact=True).select_option('footing')
            page.locator('#saveTemplateBtn').click()
            page.wait_for_function('window.__requests.length===1')
            assert page.evaluate('window.__requests[0].column_roles') == {'1':None,'2':'document_id','3':'footing'}
            assert page.evaluate("window.__masters.every(c=>!('reconciliation_role' in c))")
        finally:
            browser.close()


def test_column_role_form_round_trip_uses_saved_role_and_guidance():
    root = TEMPLATES_HTML.parents[1]
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content((root / 'static/columns.html').read_text(encoding='utf-8'))
        page.add_script_tag(content="""
            var API_BASE = ''; window.LoadingUI = {showSpinner(){}, hideSpinner(){}};
            window.__saved = null;
            window.fetch = async (url, options={}) => {
                let value = [];
                if (options.method === 'PUT') { window.__saved = JSON.parse(options.body); value = {message:'Saved'}; }
                else if (String(url).includes('/usage')) value = {templates_count:2,documents_count:0,cross_checks_count:0};
                return new Response(JSON.stringify(value), {status:200,headers:{'Content-Type':'application/json'}});
            };
        """)
        page.add_script_tag(path=str(root / 'static/columns.js'))
        try:
            page.evaluate("openModal('Edit Column', {id:1,column_name:'Voucher',data_type:'Text',reconciliation_role:'document_id'})")
            assert page.locator('#colType').input_value() == 'Text'
            assert page.locator('#colType option').all_text_contents() == ['Text','Numeric','Date']
            page.locator('#colType').select_option('Numeric')
            page.evaluate("saveColumn(true, saveAndNewBtn)")
            assert page.evaluate('window.__saved.data_type') == 'Numeric'
            assert page.evaluate("!('reconciliation_role' in window.__saved)")
            assert page.locator('#colType').input_value() == 'Text'
        finally:
            browser.close()


def test_edit_template_keeps_column_draft_until_explicit_save():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        fetch_calls = []

        page.on("console", lambda message: None)
        page.set_content(TEMPLATES_HTML.read_text(encoding="utf-8"))
        page.add_script_tag(content="""
            var API_BASE = '';
            window.formatDate = value => value || '';
            window.LoadingUI = {
                showSpinner() {},
                hideSpinner() {}
            };
            window.fetch = async (url, options = {}) => {
                window.__fetchCalls.push({url, method: options.method || 'GET'});
                if (url === '/api/columns') {
                    return new Response(JSON.stringify([
                        {id: 1, column_name: 'Quantity', data_type: 'Numeric', notes: null}
                    ]), {status: 200, headers: {'Content-Type': 'application/json'}});
                }
                if (String(url).startsWith('/api/templates/')) {
                    return new Response(JSON.stringify({message: 'Template updated'}), {
                        status: 200, headers: {'Content-Type': 'application/json'}
                    });
                }
                return new Response(JSON.stringify([]), {
                    status: 200, headers: {'Content-Type': 'application/json'}
                });
            };
            window.__fetchCalls = [];
        """)
        page.add_script_tag(path=str(TEMPLATES_JS))
        try:
            page.evaluate("""async () => {
                await openModal('Edit Template', {
                    id: 7,
                    template_name: 'Disposable test template',
                    columns: []
                });
                searchInput.value = 'Quantity';
                renderSearchResults('Quantity');
                document.querySelector('#searchResults button').click();
            }""")

            assert page.locator("#selectedCountBadge").text_content() == "1 selected"
            assert page.locator("#templateModal").evaluate("node => node.style.display") == "block"
            assert page.locator("#templateModalOverlay").evaluate("node => node.style.display") == "block"
            assert page.evaluate("window.__fetchCalls.filter(call => call.method === 'PUT').length") == 0

            page.locator("#templateModalOverlay").click(position={"x": 5, "y": 5})
            assert page.locator("#templateModal").evaluate("node => node.style.display") == "block"
            page.keyboard.press("Escape")
            assert page.locator("#templateModal").evaluate("node => node.style.display") == "block"

            page.locator("#closeModalBtn").click()
            assert page.locator("#templateModal").evaluate("node => node.style.display") == "none"

            page.evaluate("""async () => {
                await openModal('Edit Template', {
                    id: 7,
                    template_name: 'Disposable test template',
                    columns: [{id: 1, column_name: 'Quantity', data_type: 'Numeric'}]
                });
            }""")
            assert page.locator("#selectedCountBadge").text_content() == "1 selected"
            assert page.locator("#selectedColumnsList .selected-item > span:nth-of-type(3) > span").text_content() == "Quantity"

            page.locator("#saveTemplateBtn").click()
            page.wait_for_timeout(900)
            assert page.evaluate("window.__fetchCalls.filter(call => call.method === 'PUT').length") == 1
        finally:
            browser.close()


@pytest.fixture
def ledger_template_page():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(TEMPLATES_HTML.read_text(encoding='utf-8'))
        page.add_script_tag(content="""
            var API_BASE = ''; window.formatDate = value => value || '';
            window.LoadingUI = {showSpinner(){}, hideSpinner(){}};
            window.__calls = []; window.__templates = [];
            window.__columns = ['Voucher ID','Transaction Date','Voucher Detail','Debit Amount','Credit Amount'].map((name,i)=>({
                id:i+1,column_name:name,data_type:['Text','Date','Text','Numeric','Numeric'][i],category:'Financial',notes:null}));
            window.fetch = async (url, options={}) => {
                window.__calls.push({url, method:options.method || 'GET', body: options.body ? JSON.parse(options.body) : null});
                let result = [];
                if(url === '/api/columns') result = window.__columns;
                else if(url === '/api/templates') result = options.method ? {message:'Saved',template_id:10} : window.__templates;
                else if(url === '/api/templates/ledger-setup') {
                    if(options.method && !window.__columns.some(c=>c.id===6)) window.__columns.push({id:6,column_name:'Balance',data_type:'Text',category:'Financial',notes:null});
                    result = options.method ? {message:'Ledger saved',template_id:10} : {
                        template_name:'Account Ledger',columns:['voucher','date','description','debit','credit','balance'].map((role,i)=>({
                            role,column_name:['Voucher ID','Transaction Date','Voucher Detail','Debit Amount','Credit Amount','Balance'][i],
                            data_type:['Text','Date','Text','Numeric','Numeric','Text'][i],column_id:i<5?i+1:null}))};
                } else if(url.startsWith('/api/templates/')) result = {message:'Saved'};
                return new Response(JSON.stringify(result),{status:200,headers:{'Content-Type':'application/json'}});
            };
        """)
        page.add_script_tag(content=TEMPLATES_JS.read_text(encoding='utf-8'))
        page.evaluate('async () => { await loadTemplates(); }')
        yield page
        browser.close()


def test_prepare_ledger_is_a_draft_until_save_and_reuses_existing_columns(ledger_template_page):
    page = ledger_template_page
    page.locator('#prepareLedgerBtn').click()
    assert page.locator('#templatePurpose').input_value() == 'account_ledger'
    assert page.locator('#templatePurpose').is_disabled()
    assert page.locator('#selectedCountBadge').inner_text() == '6 selected'
    assert page.locator('#ledgerRole-balance').input_value() == '-6'
    assert 'create on Save' in page.locator('#selectedColumnsList').inner_text()
    assert page.evaluate("window.__calls.filter(c => c.method === 'POST').length") == 0
    page.locator('#saveTemplateBtn').click()
    page.wait_for_function("window.__calls.some(c => c.method === 'POST')")
    request = page.evaluate("window.__calls.find(c => c.method === 'POST')")
    assert request['url'] == '/api/templates/ledger-setup'
    assert request['body'] == {'template_name':'Account Ledger', 'has_subtotals_by_default':False, 'ledger_columns':{
        'voucher':1,'date':2,'description':3,'debit':4,'credit':5,'balance':None},
        'column_comparisons':{'voucher':True,'date':True,'description':True,'debit':True,'credit':True,'balance':False}}
    assert page.evaluate("window.__calls.filter(c => c.url === '/api/columns' && c.method === 'POST').length") == 0
    page.wait_for_function("document.getElementById('templateModal').style.display === 'none'")
    page.locator('#addTemplateBtn').click()
    page.locator('#columnSearchInput').fill('Balance')
    assert page.locator('#searchResults').inner_text().startswith('Balance')


def test_cancelled_ledger_draft_has_no_writes_and_standard_editor_stays_standard(ledger_template_page):
    page = ledger_template_page
    page.locator('#prepareLedgerBtn').click()
    page.locator('#cancelTemplateBtn').click()
    page.locator('#addTemplateBtn').click()
    assert page.locator('#templatePurpose').input_value() == 'standard'
    assert page.locator('#templatePurpose').is_enabled()
    assert page.locator('#ledgerRoleFields').is_hidden()
    assert page.locator('#templateColumnSearch').is_visible()
    assert page.evaluate("window.__calls.filter(c => c.method === 'POST').length") == 0


def test_existing_ledger_roles_are_preserved_and_wrong_voucher_order_blocks_save(ledger_template_page):
    page = ledger_template_page
    page.evaluate("""async () => {
        const columns = [...window.__columns,{id:6,column_name:'Balance',data_type:'Text'}];
        await openModal('Edit ledger',{id:10,template_name:'Account Ledger',data_purpose:'account_ledger',
            ledger_columns:{voucher:1,date:2,description:3,debit:4,credit:5,balance:6},columns});
    }""")
    assert page.locator('#ledgerRole-balance').input_value() == '6'
    assert page.locator('#ledgerRole-voucher option[value="4"]').count() == 0
    page.locator('#saveTemplateBtn').click()
    page.wait_for_function("window.__calls.some(c => c.method === 'PUT')")
    request = page.evaluate("window.__calls.find(c => c.method === 'PUT')")
    assert request['body']['data_purpose'] == 'account_ledger'
    assert request['body']['ledger_columns']['balance'] == 6
    page.wait_for_function("document.getElementById('templateModal').style.display === 'none'")
    page.evaluate("""async () => {
        await openModal('Wrong order',{id:10,template_name:'Ledger',data_purpose:'account_ledger',
            ledger_columns:{voucher:1,date:2,description:3,debit:4,credit:5,balance:6},
            columns:[window.__columns[1],window.__columns[0],...window.__columns.slice(2),{id:6,column_name:'Balance',data_type:'Text'}]});
    }""")
    page.locator('#saveTemplateBtn').click()
    assert 'keep the Voucher column first' in page.locator('#modalStatus').inner_text()
    assert page.evaluate("window.__calls.filter(c => c.method === 'PUT').length") == 1
