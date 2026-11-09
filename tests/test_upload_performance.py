"""Guard measured upload optimizations without network/database writes or timing thresholds."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from decimal import Decimal

import pandas as pd
import pytest
from openpyxl import Workbook

from routers import import_export as upload


@pytest.fixture(autouse=True)
def isolated_cache_and_database(monkeypatch):
    upload._cached_date_text.cache_clear()
    upload._cached_comparison_text_date.cache_clear()
    def unexpected(*args, **kwargs):
        raise AssertionError('Unexpected database access in a pure upload performance check')
    monkeypatch.setattr(upload,'execute_query_pg',unexpected)
    monkeypatch.setattr(upload,'execute_write_pg',unexpected)
    yield
    upload._cached_date_text.cache_clear()
    upload._cached_comparison_text_date.cache_clear()


def test_repeated_dates_reuse_parsing_without_mixing_component_order(monkeypatch):
    calls=[]
    original=upload._parse_date_text
    def parse(*args):
        calls.append(args)
        return original(*args)
    monkeypatch.setattr(upload,'_parse_date_text',parse)
    for _ in range(100):
        assert upload.parse_date_flexible('04/05/2026','/','DMY')==datetime(2026,5,4)
        assert upload.parse_date_flexible('04/05/2026','/','MDY')==datetime(2026,4,5)
    assert len(calls)==2


@pytest.mark.parametrize('text,separator,order,expected',[
    ('29/02/2024','/','DMY',datetime(2024,2,29)),
    ('2026-11-09','-','YMD',datetime(2026,11,9)),
    ('09112026','','DMY',datetime(2026,11,9)),
    (' 11.09.2026 ','.','MDY',datetime(2026,11,9)),
])
def test_cached_parser_retains_supported_formats(text,separator,order,expected):
    assert upload.parse_date_flexible(text,separator,order)==expected
    assert upload.parse_date_flexible(text,separator,order)==expected


@pytest.mark.parametrize('text',['29/02/2026','31/04/2026','00/01/2026','11-02-2026'])
def test_cached_parser_still_rejects_invalid_calendar_or_delimiter(text):
    for _ in range(2):
        with pytest.raises(ValueError,match='is not a valid date'):
            upload.parse_date_flexible(text,'/','DMY')


def test_stored_ambiguity_and_native_dates_survive_cache_warmup():
    for _ in range(2):
        assert upload._comparison_calendar_date('04/05/2026','/','DMY')=='2026-05-04'
        assert upload._comparison_calendar_date('04/05/2026','/','MDY')=='2026-04-05'
        assert upload._comparison_calendar_date('04/05/2026','/','DMY',stored=True) is None
        assert upload._comparison_calendar_date('13-04-2026','/','MDY',stored=True)=='2026-04-13'
        assert upload._comparison_calendar_date(datetime(2026,5,4),'-','MDY')=='2026-05-04'
    assert upload._cached_comparison_text_date.cache_info().hits>=4
    assert upload._comparison_calendar_date(Decimal('20260101'),'','YMD')=='2026-01-01'


def test_date_cache_is_bounded_and_long_invalid_values_are_not_retained():
    for number in range(4200):
        day=datetime(2000,1,1)+timedelta(days=number)
        upload.parse_date_flexible(day.strftime('%Y-%m-%d'),'-','YMD')
    assert upload._cached_date_text.cache_info().currsize==4096
    before=upload._cached_comparison_text_date.cache_info().currsize
    assert upload._comparison_calendar_date('x'*32767,'/','DMY',stored=True) is None
    assert upload._cached_comparison_text_date.cache_info().currsize==before


def test_concurrent_picker_orders_do_not_share_an_interpretation():
    orders=['DMY','MDY']*30
    with ThreadPoolExecutor(max_workers=8) as executor:
        values=list(executor.map(lambda order:upload.parse_date_flexible('04/05/2026','/',order),orders))
    assert values==[datetime(2026,5,4) if order=='DMY' else datetime(2026,4,5) for order in orders]


@pytest.mark.parametrize('selected_sheet',['Data','Missing'])
def test_mapping_reads_headers_only_with_the_same_header_names_and_fallback(monkeypatch,tmp_path,selected_sheet):
    book=Workbook();sheet=book.active;sheet.title='Data'
    sheet.append(['Voucher','Date','Amount','Amount',None]);sheet.append(['V1','11/02/2026',10,10,None])
    path=tmp_path/'headers.xlsx';book.save(path);book.close()
    expected=pd.read_excel(path).columns.tolist()
    columns=[dict(column_id=1,column_name='Voucher',data_type='Text',sort_order=0)]
    def query(sql,params=None):
        if 'FROM template_column_mappings' in sql:return columns
        if 'FROM uploaded_files' in sql:return [dict(file_path=str(path))]
        raise AssertionError(sql)
    monkeypatch.setattr(upload,'execute_query_pg',query)
    calls=[];read_excel=pd.read_excel
    def read(*args,**kwargs):
        calls.append(kwargs.copy())
        return read_excel(*args,**kwargs)
    monkeypatch.setattr(upload.pd,'read_excel',read)
    result=upload.auto_map_headers(upload.AutoMapRequest(file_id=99,template_id=1,sheet_name=selected_sheet))
    assert result['excel_headers']==expected
    assert result['mappings'][0]['excel_column']=='Voucher'
    assert all(call['nrows']==0 for call in calls)
    assert len(calls)==(1 if selected_sheet=='Data' else 2)


def test_fingerprint_reuse_preserves_repeated_occurrences_and_blank_versus_zero(monkeypatch):
    columns=[dict(id=1,column_name='Voucher',data_type='Text',reconciliation_role='document_id'),
             dict(id=2,column_name='Date',data_type='Date',reconciliation_role='document_date'),
             dict(id=3,column_name='Amount',data_type='Numeric')]
    rows=[dict(Voucher='V1',Date='11/02/2026',Amount=value) for value in [0,None,0,None]]
    existing=[dict(id=i,row_data=dict(row),extract_date='2026-02-11',version=1,
                   source_row_number=i+1,source_order_upload_id=1) for i,row in enumerate(rows,1)]
    monkeypatch.setattr(upload,'execute_query_pg',lambda *args,**kwargs:existing)
    result=upload.compare_transactions(1,1,columns,rows,{c['column_name']:c['column_name'] for c in columns},{},
        comparison_mode='replace_range',coverage_start='2026-02-11',coverage_end='2026-02-11')
    assert result['unchanged_count']==4
    assert set(result['unchanged'])=={1,2,3,4}
    assert result['new_count']==result['changed_count']==result['missing_count']==0
    assert len(result['source_order'])==4
    assert not result['source_order_refresh_required']


def preview_fixture(monkeypatch,path):
    columns=[dict(id=1,column_name='Voucher',data_type='Text',reconciliation_role='document_id'),
             dict(id=2,column_name='Date',data_type='Date',reconciliation_role='document_date'),
             dict(id=3,column_name='Amount',data_type='Numeric',reconciliation_role='footing')]
    def query(sql,params=None):
        if 'FROM uploaded_files' in sql:return [dict(file_path=str(path))]
        if 'template_column_mappings' in sql:return columns
        raise AssertionError(sql)
    monkeypatch.setattr(upload,'execute_query_pg',query)
    return dict(file_id=1,template_id=1,sheet_name='Data',
        date_mappings=[dict(template_column_name='Date',excel_column_name='Date')],
        column_mappings=[dict(template_column_name=c['column_name'],excel_column_name=c['column_name']) for c in columns])


def test_unconfirmed_samples_stop_early_but_confirmed_coverage_scans_all_rows(monkeypatch,tmp_path):
    path=tmp_path/'irregular.xlsx';book=Workbook();sheet=book.active;sheet.title='Data'
    sheet.append(['Voucher','Date','Amount'])
    for number in range(200):sheet.append([f'V{number}','15/04/2026',1])
    sheet.cell(150,2,'01/01/2025');sheet.cell(201,2,'31/12/2027')
    sheet.append(['TOTAL',None,200]);book.save(path);book.close()
    payload=preview_fixture(monkeypatch,path)
    observed=[];load=upload.openpyxl.load_workbook
    def load_book(*args,**kwargs):
        workbook=load(*args,**kwargs);worksheet=workbook['Data'];iterate=worksheet.iter_rows
        def rows(*args,**kwargs):
            for row in iterate(*args,**kwargs):observed.append(1);yield row
        worksheet.iter_rows=rows
        return workbook
    monkeypatch.setattr(upload.openpyxl,'load_workbook',load_book)
    response=upload.preview_date_format(upload.DatePreviewRequest(**payload))
    assert len(observed)==4
    assert len(response['samples'])==3
    assert response['date_suggestion']['status']=='confirm_footing'
    assert response['date_suggestion']['start'] is response['date_suggestion']['end'] is None
    observed.clear()
    response=upload.preview_date_format(upload.DatePreviewRequest(**payload,control_total_row=202,control_total_confirmed=True))
    assert len(observed)>=202
    assert response['date_suggestion']['start']=='2025-01-01'
    assert response['date_suggestion']['end']=='2027-12-31'
    assert response['date_suggestion']['transaction_dates']==200


def test_unconfirmed_sparse_columns_find_later_samples_without_suggesting_coverage(monkeypatch,tmp_path):
    path=tmp_path/'sparse.xlsx';book=Workbook();sheet=book.active;sheet.title='Data'
    sheet.append(['Voucher','Date','Amount'])
    sheet.cell(90,2,'13/04/2026');sheet.cell(199,2,'14/04/2026')
    book.save(path);book.close()
    payload=preview_fixture(monkeypatch,path)
    response=upload.preview_date_format(upload.DatePreviewRequest(**payload))
    assert len(response['samples'])==2
    assert response['samples'][0]['parsed']=='13 April 2026'
    assert response['date_suggestion']['start'] is None


def test_legacy_xls_unconfirmed_preview_retains_full_reader(monkeypatch):
    payload=preview_fixture(monkeypatch,'legacy.xls');reads=[]
    def read(*args,**kwargs):
        reads.append(kwargs)
        return pd.DataFrame([dict(Voucher='V1',Date='13/04/2026',Amount=1)])
    monkeypatch.setattr(upload.pd,'read_excel',read)
    monkeypatch.setattr(upload,'_unconfirmed_date_samples',lambda *args:pytest.fail('XLS must use its existing reader'))
    response=upload.preview_date_format(upload.DatePreviewRequest(**payload))
    assert len(reads)==1 and 'nrows' not in reads[0]
    assert response['samples'][0]['valid'] is True
    assert response['date_suggestion']['status']=='confirm_footing'


def test_duplicate_date_headers_retain_pandas_fallback_without_suggesting_coverage(monkeypatch,tmp_path):
    path=tmp_path/'duplicate-date-headers.xlsx'
    book=Workbook();sheet=book.active;sheet.title='Data'
    sheet.append(['Voucher','Date','Date','Amount'])
    sheet.append(['V1','13/04/2026','14/04/2026',1]);book.save(path);book.close()
    payload=preview_fixture(monkeypatch,path)
    response=upload.preview_date_format(upload.DatePreviewRequest(**payload))
    assert response['samples'][0]['parsed']=='13 April 2026'
    assert response['date_suggestion']['status']=='confirm_footing'
    assert response['date_suggestion']['start'] is response['date_suggestion']['end'] is None
