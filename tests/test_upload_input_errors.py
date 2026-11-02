"""Source-cell validation before pandas coercion; no working database access."""
from unittest.mock import Mock

import openpyxl
import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers import import_export as upload


COLUMNS = [{'id': i, 'column_name': name, 'data_type': kind} for i, (name, kind) in enumerate([
    ('Voucher ID', 'Text'), ('Transaction Date', 'Date'), ('Account Name', 'Text'),
    ('Debit Amount', 'Numeric'), ('Credit Amount', 'Numeric'),
], 1)]
MAPPING = {c['column_name']: c['column_name'] for c in COLUMNS}


def workbook(tmp_path, value, column=4, *, literal=False):
    path = tmp_path / 'source.xlsx'
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = 'Data'
    sheet.append(list(MAPPING))
    sheet.append(['V1', '01/01/2026', 'Cash', 125, 0])
    sheet.cell(2, column).value = value
    if literal:
        sheet.cell(2, column).data_type = 's'
    book.save(path)
    book.close()
    return path


def request(**changes):
    return upload.ImportRequest(client_id=1, template_id=1, file_id=1, sheet_name='Data',
        mappings=[dict(template_column_id=c['id'], template_column_name=c['column_name'],
                       excel_column_name=c['column_name']) for c in COLUMNS], **changes)


@pytest.mark.parametrize('value', ['Infinity', '-Infinity', 'inf', 'NaN', 'nan', '1e9999'])
def test_nonfinite_numbers_report_original_row_and_column(tmp_path, value):
    path = workbook(tmp_path, value)
    result = upload._workbook_input_errors(path, request(), COLUMNS, MAPPING, pd.read_excel(path))
    assert len(result) == 1 and result[0]['row_number'] == 2
    assert result[0]['errors'] == [{'column': 'Debit Amount', 'excel_column': 'Debit Amount',
                                  'error_type': 'data_type', 'message': 'Enter a valid number.'}]


@pytest.mark.parametrize('value', ['#REF!', '#DIV/0!', '#VALUE!', '#N/A'])
def test_excel_errors_in_optional_text_cells_are_not_silently_blank(tmp_path, value):
    path = workbook(tmp_path, value, column=3)
    frame = pd.read_excel(path)
    assert pd.isna(frame.iloc[0]['Account Name'])
    result = upload._workbook_input_errors(path, request(), COLUMNS, MAPPING, frame)
    assert result[0]['row_data']['Account Name'] == value
    assert result[0]['errors'][0]['column'] == 'Account Name'
    assert result[0]['errors'][0]['message'] == 'Fix the Excel formula error.'


@pytest.mark.parametrize('value,column,literal', [(-125,4,False),(0,4,False),('125.25',4,False),
                                                ('#REF!',3,True),(None,3,False),('NaN',3,True)])
def test_valid_values_literal_text_and_optional_blanks_are_unchanged(tmp_path, value, column, literal):
    path = workbook(tmp_path, value, column, literal=literal)
    before = path.read_bytes()
    frame = pd.read_excel(path)
    original = frame.copy(deep=True)
    assert upload._workbook_input_errors(path, request(), COLUMNS, MAPPING, frame) == []
    pd.testing.assert_frame_equal(frame, original)
    assert path.read_bytes() == before


def test_exclusions_only_apply_to_partial_extracts_and_unmapped_cells_are_ignored(tmp_path):
    path = workbook(tmp_path, '#REF!', 3)
    frame = pd.read_excel(path)
    assert not upload._workbook_input_errors(path, request(excluded_rows=[2]), COLUMNS, MAPPING, frame)
    assert upload._workbook_input_errors(path, request(comparison_mode='replace_range', excluded_rows=[2]), COLUMNS, MAPPING, frame)
    assert not upload._workbook_input_errors(path, request(), COLUMNS, {k:v for k,v in MAPPING.items() if k!='Account Name'}, frame)


def test_legacy_xls_reader_uses_error_cell_type_and_releases_workbook(monkeypatch):
    import xlrd
    sheet = Mock(nrows=2, ncols=5)
    values = ['V1','01/01/2026',23,125,0]
    sheet.cell_value.side_effect = lambda row,col: values[col]
    sheet.cell_type.side_effect = lambda row,col: xlrd.XL_CELL_ERROR if col==2 else xlrd.XL_CELL_TEXT
    book = Mock()
    book.sheet_by_name.return_value = sheet
    monkeypatch.setattr(xlrd, 'open_workbook', lambda *a,**kw:book)
    result = upload._workbook_input_errors('source.xls', request(), COLUMNS, MAPPING, pd.DataFrame(columns=list(MAPPING)))
    assert result[0]['errors'][0]['error_type'] == 'excel_error'
    book.release_resources.assert_called_once()


def test_reusing_a_source_column_does_not_hide_its_numeric_validation(tmp_path):
    path=workbook(tmp_path,'Infinity')
    mapping={**MAPPING,'Account Name':'Debit Amount'}
    result=upload._workbook_input_errors(path,request(),COLUMNS,mapping,pd.read_excel(path))
    assert result[0]['errors'][0]['column']=='Debit Amount'


@pytest.mark.parametrize('endpoint', ['/api/upload/validate','/api/upload/import'])
@pytest.mark.parametrize('value,column', [('Infinity',4),('NaN',4),('#REF!',3)])
def test_http_validation_and_direct_import_block_bad_cells_before_comparison_or_writes(tmp_path, monkeypatch, endpoint, value, column):
    path = workbook(tmp_path, value, column)
    def query(sql, params=None, **kwargs):
        if 'FROM templates' in sql:
            return [dict(id=1,template_name='Test',data_purpose='standard',rate_source=None)]
        if 'FROM template_column_mappings' in sql:
            return COLUMNS
        if 'FROM uploaded_files' in sql:
            return [dict(id=1,file_path=str(path),file_name=path.name,status='Pending',file_sha256='hash')]
        if 'FROM clients' in sql:
            return [dict(id=1)]
        raise AssertionError('Unexpected database query: '+sql)
    monkeypatch.setattr(upload,'execute_query_pg',query)
    monkeypatch.setattr(upload,'_ensure_uploaded_file_hash',lambda *a:'hash')
    monkeypatch.setattr(upload,'_find_prior_import_with_same_hash',lambda *a:None)
    monkeypatch.setattr(upload,'get_database_storage_budget_pg',lambda *a:dict(blocked=False,warning=False))
    writes, comparison, plan = Mock(), Mock(), Mock()
    monkeypatch.setattr(upload,'execute_write_pg',writes)
    monkeypatch.setattr(upload,'compare_transactions',comparison)
    monkeypatch.setattr(upload,'_create_validation_plan',plan)
    app=FastAPI();app.include_router(upload.router)
    response=TestClient(app).post(endpoint,json=request().model_dump())
    assert response.status_code==200
    result=response.json()
    assert result['status']=='validation_failed' and not result['can_import']
    assert result['errors'][0]['row_number']==2
    assert result['errors'][0]['errors'][0]['column']==COLUMNS[column-1]['column_name']
    writes.assert_not_called();comparison.assert_not_called();plan.assert_not_called()
