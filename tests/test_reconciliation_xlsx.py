from pathlib import Path
from contextlib import closing

import openpyxl
import pandas as pd
import pytest
from test_reconciliation_balance_source import independent_year_boundary_case, horizontal_reconciliation_case, _horizontal_summary, selected_account_case, _selected_summary
from test_reconciliation_reports import independent_year_boundary_report


@pytest.mark.parametrize('codes', [['C1'], ['C2'], ['C1','C2'], ['B1','C1']])
def test_selected_reconciliation_workbook_preserves_complete_context_but_scopes_ledgers(selected_account_case,codes):
    from io import BytesIO
    from services.reconciliation_balance_source import build_cashflow_reconciliation
    from services.reconciliation_xlsx import export_cashflow_reconciliation_xlsx
    case=selected_account_case
    result=build_cashflow_reconciliation(_selected_summary(case,codes),case['source'],case['accounts'],'split',
        include_posting_comparisons=True)
    support=result.pop('_support_entries');comparisons=result.pop('_posting_comparisons')
    result.update(client_id=7,period_start='2026-01-01',period_end='2026-01-31')
    content=export_cashflow_reconciliation_xlsx(result,source=case['source'],support=support,
        account_types=case['accounts'],comparisons=comparisons)
    workbook=openpyxl.load_workbook(BytesIO(content),data_only=True)
    # Workbook must defend its own scope even when passed the full contextual source.
    ledger=list(workbook['Ledger Evidence'].iter_rows(min_row=5,values_only=True))
    assert {r[2] for r in ledger} <= set(codes)
    voucher=list(workbook['Voucher Evidence'].iter_rows(min_row=5,values_only=True))
    references={r['voucher_id'] for r in case['source']['transaction_rows'] if r['account_code'] in codes}
    expected=[r for r in case['source']['transaction_rows'] if r['voucher_id'] in references]
    assert {r[0] for r in voucher}=={r['id'] for r in expected}
    for row in voucher:
        assert isinstance(row[5],str) and isinstance(row[6],str)
        assert row[-1]==('Selected Cash/Bank account' if row[2] in codes else
            'Other Cash/Bank account — voucher context' if row[2] in case['accounts'] else 'Counter-account — voucher context')
    controls=dict(workbook['Report Controls'].iter_rows(min_row=5,values_only=True))
    assert {v for k,v in controls.items() if k.startswith('Account selection / account codes / ')}==set(codes)
    for kind,paper in result['views'].items():
        values={r[0]:r[1] for r in workbook['Working Paper '+kind.title()].values}
        for side in ('receipts','payments'):
            assert values[side.title()+' as per ledgers']==paper['overall'][side]['ledger']
            assert values[side.title()+' as per Cashflow report']==paper['overall'][side]['cashflow']
        assert values['Closing difference (ledger − calculated)']==0


def test_selected_cashflow_workbook_discloses_transfer_amounts_and_allocation_reviews(selected_account_case):
    from routers.reconciliation import _cashflow_preview
    from services.reconciliation_xlsx import build_cashflow_workbook
    case=selected_account_case
    preview=_cashflow_preview(_selected_summary(case,['C2']))
    workbook=build_cashflow_workbook(preview)
    transfer=list(workbook['Internal Transfers'].iter_rows(min_row=4,values_only=True))
    rows=[dict(zip(transfer[0],row)) for row in transfer[1:]]
    assert len(rows)==1
    assert rows[0]['Selected-account transfer receipts']==20
    assert rows[0]['Selected-account transfer payments']==0
    assert rows[0]['Selected-account transfer net']==20
    assert 'Complete-voucher amount (context)' in rows[0]
    assert 'context' in rows[0]['Evidence scope']
    # Competing sources cannot assign an expense to one selected funding account.
    frame=pd.DataFrame([['AMB','2026-01-05','B1',0,60],['AMB','2026-01-05','C1',0,40],
        ['AMB','2026-01-05','EXP1',100,0]],columns=['voucher_id','date','account_code','debit','credit'])
    ambiguous=dict(case,journal=frame)
    summary=_selected_summary(ambiguous,['C1'])
    preview=_cashflow_preview(summary)
    assert preview['selection_review_count']==1
    workbook=build_cashflow_workbook(preview)
    review=list(workbook['Review Required'].iter_rows(min_row=4,values_only=True))
    rows=[dict(zip(review[0],row)) for row in review[1:]]
    assert rows[0]['Voucher']=='AMB'
    assert rows[0]['Selected payments under review']==40
    assert rows[0]['Selection review reason']
    assert all('selected Cash/Bank accounts' in ws['A2'].value for ws in workbook)


def _working_paper_export(case, view='combined', presentation='gross', end='2026-01-31'):
    from services.reconciliation_balance_source import build_cashflow_reconciliation, build_cashflow_differences
    from services.reconciliation_xlsx import export_cashflow_reconciliation_xlsx
    result = build_cashflow_reconciliation(_horizontal_summary(case, presentation, end=end),
        case['source'], case['accounts'], view)
    support = result.pop('_support_entries')
    result.update(client_id=7, template_id=1, period_start='2026-01-01', period_end=end,
        cache_identity='a'*64, evidence_fingerprint='b'*64)
    comparisons = {kind: build_cashflow_differences(case['source'],
        {code: value for code, value in case['accounts'].items() if kind == 'combined' or value.lower() == kind},
        '2026-01-01', end) for kind in result['views']}
    content = export_cashflow_reconciliation_xlsx(result, source=case['source'], support=support,
        account_types=case['accounts'], comparisons=comparisons)
    from io import BytesIO
    return openpyxl.load_workbook(BytesIO(content)), result, support, comparisons


def test_streaming_reconciliation_export_keeps_65000_voucher_and_12000_ledger_rows(horizontal_reconciliation_case, tmp_path):
    import copy
    import time
    from services.reconciliation_balance_source import build_cashflow_reconciliation
    from services.reconciliation_xlsx import export_cashflow_reconciliation_xlsx
    case = horizontal_reconciliation_case
    source = copy.deepcopy(case['source'])
    for identity in range(12000-len(source['ledger_rows'])):
        source['ledger_rows'].append(dict(source['ledger_rows'][0],id=10000+identity,debit='0',credit='0',
            source_fingerprint=f'ledger-{identity}'))
    for identity in range(65000-len(source['transaction_rows'])):
        source['transaction_rows'].append(dict(source['transaction_rows'][0],id=100000+identity,
            account_code='CONTEXT',debit='0',credit='0',description=f'Original occurrence {identity}'))
    source['uploads'] = case['refresh_uploads'](source['ledger_rows'])
    result = build_cashflow_reconciliation(_horizontal_summary(case), source, case['accounts'], 'split',
                                          include_posting_comparisons=True)
    support = result.pop('_support_entries'); comparisons = result.pop('_posting_comparisons')
    result.update(client_id=7, period_start='2026-01-01', period_end='2026-01-31')
    progress=[];start=time.perf_counter();destination=tmp_path/'large.xlsx'
    export_cashflow_reconciliation_xlsx(result,source=source,support=support,account_types=case['accounts'],
        comparisons=comparisons,destination=destination,temporary_directory=tmp_path,
        progress=lambda *values:progress.append(values))
    seconds=time.perf_counter()-start
    print(f'Large reconciliation generation: {seconds:.2f}s, {destination.stat().st_size} bytes')
    with closing(openpyxl.load_workbook(destination,read_only=True,data_only=True)) as workbook:
        for title,count in [('Voucher Evidence',65000),('Ledger Evidence',12000)]:
            records=workbook[title].iter_rows(min_row=5,values_only=True)
            identities=[row[0] for row in records]
            assert len(identities)==len(set(identities))==count
        paper=list(workbook['Working Paper Combined'].values)
        assert next(row[1] for row in paper if row and row[0]=='Receipts as per Cashflow report')==900
        assert next(row[1] for row in paper if row and row[0]=='Payments as per Cashflow report')==350
    assert any(stage=='Writing Voucher Evidence' and total==65000 for stage,current,total in progress)
    assert list(tmp_path.iterdir())==[destination]
    print(f'Large reconciliation fixture: {seconds:.2f}s, {destination.stat().st_size} bytes, 65000 voucher / 12000 ledger rows')


@pytest.mark.parametrize('cancel_stage', ['Writing Voucher Evidence', 'Finalizing Excel file'])
def test_streaming_reconciliation_cancellation_cleans_owned_xml_files(horizontal_reconciliation_case,tmp_path,cancel_stage):
    from services.reconciliation_balance_source import build_cashflow_reconciliation, build_cashflow_differences
    from services.reconciliation_xlsx import export_cashflow_reconciliation_xlsx
    case=horizontal_reconciliation_case
    result=build_cashflow_reconciliation(_horizontal_summary(case),case['source'],case['accounts'])
    support=result.pop('_support_entries');result.update(client_id=7,period_start='2026-01-01',period_end='2026-01-31')
    comparisons={'combined':build_cashflow_differences(case['source'],case['accounts'],'2026-01-01','2026-01-31')}
    def cancel(stage,*counts):
        if stage==cancel_stage:raise RuntimeError('Cancelled by test')
    with pytest.raises(RuntimeError,match='Cancelled by test'):
        export_cashflow_reconciliation_xlsx(result,source=case['source'],support=support,account_types=case['accounts'],
            comparisons=comparisons,temporary_directory=tmp_path,progress=cancel)
    assert not list(tmp_path.iterdir())


def _export_records(sheet):
    headers = [cell.value for cell in sheet[4]]
    return [dict(zip(headers, row)) for row in sheet.iter_rows(min_row=5, values_only=True)]


@pytest.mark.parametrize('view', ['combined', 'bank', 'cash', 'split'])
@pytest.mark.parametrize('presentation,expected', [('gross', (900, 350)), ('net', (850, 300))])
def test_working_paper_export_matches_independent_books_and_screen(horizontal_reconciliation_case, view, presentation, expected):
    import copy
    case = horizontal_reconciliation_case
    before = copy.deepcopy(case)
    workbook, result, support, comparisons = _working_paper_export(case, view, presentation)
    assert workbook.sheetnames == ['Working Paper ' + kind.title() for kind in result['views']] + [
        'Adjustment Support', 'Posting Comparison', 'Ledger Evidence', 'Voucher Evidence', 'Report Controls']
    for kind, part in result['views'].items():
        sheet = workbook['Working Paper ' + kind.title()]
        rows = {row[0].value: row[1].value for row in sheet.iter_rows(min_row=5)}
        for side, title in [('receipts', 'Receipts'), ('payments', 'Payments')]:
            values = part['overall'][side]
            assert rows[title + ' as per ledgers'] == values['ledger']
            assert rows['Adjusted ' + side + ' as per ledgers'] == values['adjusted_ledger']
            assert rows[title + ' as per Cashflow report'] == (0 if kind == 'cash' else expected[side == 'payments'])
        differences = [row[1].value for row in sheet if str(row[0].value).startswith('Unexplained difference')]
        assert differences == [0, 0]
        assert rows['Closing balance as per ledgers'] == {'combined': 10550, 'bank': 8350, 'cash': 2200}[kind]
        assert rows['Closing difference (ledger − calculated)'] == 0
        for cells in sheet:
            if isinstance(cells[1].value, (int, float)):
                assert cells[1].number_format == '#,##0.00;[Red](#,##0.00);-'
    records = _export_records(workbook['Adjustment Support'])
    assert len(records) == sum(len(entries) for entries in support.values())
    for kind, part in result['views'].items():
        for adjustment in part['adjustments']:
            selected = [row for row in records if row['Support reference'] == adjustment['key']]
            assert sum(row['Receipts adjustment'] for row in selected) == pytest.approx(adjustment['total']['receipts'])
            assert sum(row['Payments adjustment'] for row in selected) == pytest.approx(adjustment['total']['payments'])
    assert len(_export_records(workbook['Posting Comparison'])) == sum(len(c['findings']) for c in comparisons.values())
    for sheet in workbook:
        assert sheet.freeze_panes == 'B5' and sheet.print_title_rows == '$1:$4'
        assert sheet.page_setup.fitToWidth == 1
        assert sheet['A1'].font.name == 'Arial'
    assert case['source'] == before['source']
    pd.testing.assert_frame_equal(case['journal'], before['journal'])


@pytest.mark.parametrize('view', ['combined', 'bank', 'cash', 'split'])
def test_working_paper_export_keeps_complete_repeated_and_literal_source_rows(horizontal_reconciliation_case, view):
    case = horizontal_reconciliation_case
    for identity in range(251):
        case['source']['transaction_rows'].append(dict(id=9000+identity, voucher_id='RECEIPT',
            account_code='CONTEXT', date='2025-12-31', debit='0.0000000000001234567', credit='0',
            description='=HYPERLINK("https://example.invalid", "literal")', uploaded_at='2026-02-01T12:00:00Z'))
    case['source']['ledger_rows'][0]['description'] = '=1+1'
    workbook, result, _, _ = _working_paper_export(case, view)
    records = _export_records(workbook['Voucher Evidence'])
    # Cash participates in TRANSFER only; other modes retain RECEIPT and every counterpart.
    expected = 0 if view == 'cash' else 251
    context = [row for row in records if row['Account code'] == 'CONTEXT']
    assert len(context) == expected
    assert len({row['Source row ID'] for row in context}) == expected
    for row in context:
        assert row['Debit (original)'] == '0.0000000000001234567'
        assert row['Within report period'] == 'Context outside period / invalid date'
        assert row['Imported at'] == '2026-02-01T12:00:00Z'
    assert all(cell.data_type != 'f' for sheet in workbook for row in sheet for cell in row)
    ledger = _export_records(workbook['Ledger Evidence'])
    assert {row['Account code'] for row in ledger} == ({'B1', 'C1'} if view in ('combined', 'split') else {'B1' if view == 'bank' else 'C1'})
    controls = {r['Control']: r['Value'] for r in _export_records(workbook['Report Controls'])}
    assert controls['Voucher source rows'] == len(records)
    assert controls['Transaction Coverage Status'] == 'Not independently certified'


def test_working_paper_export_preserves_unavailable_evidence(horizontal_reconciliation_case):
    case = horizontal_reconciliation_case
    case['source']['ledger_rows'] = []
    workbook, result, _, _ = _working_paper_export(case)
    records = {row[0].value: row[1].value for row in workbook['Working Paper Combined'].iter_rows(min_row=5)}
    assert records['Receipts as per ledgers'] == 'Unavailable'
    assert records['Calculated closing balance'] == 'Unavailable'
    assert records['Receipts as per Cashflow report'] == 900
    assert result['views']['combined']['amount_status'] == 'Unavailable'


def test_working_paper_export_rejects_oversized_text_instead_of_silent_excel_truncation(horizontal_reconciliation_case):
    horizontal_reconciliation_case['source']['transaction_rows'][0]['description'] = 'x'*32768
    with pytest.raises(ValueError, match='without truncating'):
        _working_paper_export(horizontal_reconciliation_case)


@pytest.mark.parametrize('length', [32767, 32768, 100003])
def test_working_paper_export_preserves_long_serialized_control_metadata(horizontal_reconciliation_case, length):
    import copy
    import json
    case = horizontal_reconciliation_case
    # PostgreSQL upload metadata may be returned as JSON text rather than a dictionary.
    value = json.dumps({'source_notes': '=Exact text\n' + 'معلومات / source details 🧾 '*length}, ensure_ascii=False)[:length]
    case['source']['uploads'][0]['source_notes'] = value
    before = copy.deepcopy(case['source'])
    workbook, _, _, _ = _working_paper_export(case)
    rows = _export_records(workbook['Report Controls'])
    parts = [row for row in rows if row['Control'].startswith('Ledger upload 1 / source notes')]
    assert len(parts) > 1
    assert ''.join(row['Value'] for row in parts) == value
    assert [row['Control'].rsplit(' / ', 1)[-1] for row in parts] == [f'Part {i} of {len(parts)}' for i in range(1, len(parts)+1)]
    assert all(len(str(cell.value or '')) <= 32767 and cell.data_type != 'f'
               for sheet in workbook for row in sheet for cell in row)
    assert case['source'] == before


@pytest.mark.parametrize('view', ['combined', 'bank', 'cash', 'split'])
def test_working_paper_export_long_control_metadata_does_not_change_accounting(horizontal_reconciliation_case, view):
    import json
    case = horizontal_reconciliation_case
    baseline, _, _, _ = _working_paper_export(case, view)
    # A long free-text control and a nested serialized upload value use the same lossless path.
    case['source']['uploads'][0]['archive_receipt'] = json.dumps({'notes': 'details '*6000})
    case['source']['uploads'][1]['archive_receipt'] = 'Cash source details '*3000
    workbook, _, _, _ = _working_paper_export(case, view)
    assert workbook.sheetnames == baseline.sheetnames
    for sheet in baseline:
        if sheet.title == 'Report Controls':
            continue
        actual = workbook[sheet.title]
        assert list(actual.values) == list(sheet.values)
        assert actual.freeze_panes == sheet.freeze_panes
    controls = _export_records(workbook['Report Controls'])
    for identity, code in [(1, 'B1'), (2, 'C1')]:
        value = case['source']['uploads'][identity-1]['archive_receipt']
        parts = [r for r in controls if r['Control'].startswith(f'Ledger upload {identity} / archive receipt')]
        selected = view in ('combined', 'split') or code == ('B1' if view == 'bank' else 'C1')
        assert ''.join(r['Value'] for r in parts) == (value if selected else '')


@pytest.mark.parametrize('end,expected', [('2026-01-31', (1000, 200)), ('2026-06-30', (1100, 50))])
def test_working_paper_export_keeps_period_dependent_loan_category_totals(horizontal_reconciliation_case, end, expected):
    case = horizontal_reconciliation_case
    entries = [('RECEIPT', '2026-01-05', 'B1', 1000, 0), ('RECEIPT', '2026-01-05', 'AR', 0, 1000),
        ('REPAY', '2026-01-10', 'B1', 0, 200), ('REPAY', '2026-01-10', 'LOAN', 200, 0),
        ('DRAW', '2026-02-05', 'B1', 300, 0), ('DRAW', '2026-02-05', 'LOAN', 0, 300),
        ('PAY', '2026-02-10', 'B1', 0, 50), ('PAY', '2026-02-10', 'AP', 50, 0)]
    case['journal'] = pd.DataFrame(entries, columns=['voucher_id', 'date', 'account_code', 'debit', 'credit'])
    case['coa'] = pd.concat([case['coa'], pd.DataFrame([dict(account_code='LOAN', category='Bank loan drawn (net)')])])
    case['source'] = dict(uploads=[], ledger_rows=[], transaction_rows=[dict(row, id=i)
        for i, row in enumerate(case['journal'].to_dict('records'), 1)])
    workbook, result, _, _ = _working_paper_export(case, end=end)
    records = {row[0].value: row[1].value for row in workbook['Working Paper Combined'] if len(row) > 1}
    assert records['Receipts as per Cashflow report'] == expected[0]
    assert records['Payments as per Cashflow report'] == expected[1]
    assert result['views']['combined']['category_sections']['Bank loan drawn (net)'] == ('payments' if end == '2026-01-31' else 'receipts')


def test_working_paper_export_retains_offsetting_postings_as_differences(horizontal_reconciliation_case):
    case = horizontal_reconciliation_case
    row = dict(case['source']['ledger_rows'][0], id=999, voucher_id='LEDGER-ONLY', debit='50', credit='50')
    case['source']['ledger_rows'].append(row)
    case['source']['uploads'] = case['refresh_uploads'](case['source']['ledger_rows'])
    workbook, result, _, _ = _working_paper_export(case)
    part = result['views']['combined']
    assert part['overall']['receipts']['difference'] == 50
    assert part['overall']['payments']['difference'] == 50
    assert part['overall_balance_proof']['difference'] == 0
    findings = _export_records(workbook['Posting Comparison'])
    assert next(row for row in findings if row['Voucher'] == 'LEDGER-ONLY')['Posting status'] == 'ledger_only'


@pytest.mark.parametrize('view', ['combined', 'bank', 'cash', 'split'])
@pytest.mark.parametrize('presentation', ['gross', 'net'])
def test_working_paper_export_handles_partial_dates_year_boundary_and_mixed_movements(independent_year_boundary_case, view, presentation):
    from io import BytesIO
    from services.ledger_group_summary import build_cashflow_summary
    from services.reconciliation_balance_source import build_cashflow_reconciliation, build_cashflow_differences
    from services.reconciliation_xlsx import export_cashflow_reconciliation_xlsx
    case = independent_year_boundary_case
    transactions = case['journal'].to_dict('records')
    treasury = [r for r in transactions if r['account_code'] in case['accounts']]
    source = dict(ledger_rows=[dict(row, date=row['extract_date'], voucher_id=original['voucher_id'])
        for row, original in zip(case['rows'], treasury, strict=True)],
        transaction_rows=[dict(row, id=i+100) for i, row in enumerate(transactions)],
        uploads=[dict(row, comparison_roles_available=True) for row in case['uploads']])
    summary = build_cashflow_summary(case['journal'], case['coa'], ['C1'], ['B1', 'B0'], case['start'], case['end'],
        group_by='month', receipt_presentation=presentation, cashflow_role_categories=case['roles'], include_reconciliation_evidence=True)
    result = build_cashflow_reconciliation(summary, source, case['accounts'], view)
    support = result.pop('_support_entries')
    result.update(client_id=7, period_start=case['start'], period_end=case['end'])
    comparisons = {kind: build_cashflow_differences(source,
        {code: value for code, value in case['accounts'].items() if kind == 'combined' or value.lower() == kind},
        case['start'], case['end']) for kind in result['views']}
    workbook = openpyxl.load_workbook(BytesIO(export_cashflow_reconciliation_xlsx(result,
        source=source, support=support, account_types=case['accounts'], comparisons=comparisons)))
    for kind, part in result['views'].items():
        rows = {row[0].value: row[1].value for row in workbook['Working Paper ' + kind.title()] if len(row) > 1}
        assert rows['Closing balance as per ledgers'] == pytest.approx({'combined': 26.70, 'bank': -9.56, 'cash': 36.26}[kind])
        assert rows['Closing difference (ledger − calculated)'] == 0
        assert rows['Separate mixed-voucher movement (signed)'] == part['overall_balance_proof']['separate_mixed_movement']
        assert '2025-12-15 to 2026-02-10' in workbook['Working Paper ' + kind.title()]['A2'].value
        for side, title in [('receipts', 'Receipts'), ('payments', 'Payments')]:
            assert rows[title + ' as per Cashflow report'] == part['overall'][side]['cashflow']


@pytest.mark.parametrize('view', ['combined', 'bank', 'cash', 'split'])
def test_independent_books_survive_xlsx_roundtrip_with_full_scope_controls(
        independent_year_boundary_case, independent_year_boundary_report, view):
    from io import BytesIO
    from services.reconciliation_xlsx import export_cashflow_xlsx
    case = independent_year_boundary_case
    preview = independent_year_boundary_report(view)['preview']
    # A filtered visible category set must not narrow the accounting proof.
    preview = dict(preview, category_totals=[], treasury_view_totals=[],
                   export_filters={'summary_filter': 'nothing matches', 'hide_zero_rows': True})
    workbook = openpyxl.load_workbook(BytesIO(export_cashflow_xlsx(preview,
        period_start=case['start'], period_end=case['end'], group_by='month')))
    proof = workbook['Treasury Movement Proof']
    records = list(proof.iter_rows(min_row=5, values_only=True))
    assert len(records) == 9
    for values in records:
        month, kind = values[:2]
        index = ['2025-12', '2026-01', '2026-02'].index(month)
        movement, opening, closing = map(float, case['expected'][kind][index])
        assert values[5] == values[6] == pytest.approx(movement)
        assert values[7] == pytest.approx(0, abs=1e-9)
        if kind == view or view == 'split':
            assert values[11:15] == pytest.approx([opening, opening, closing, closing])
            assert values[15] == pytest.approx(0, abs=1e-9)
            assert values[16] == 'Matched'
        else:
            assert values[11:17] == (None,) * 6
    assert records[0][9] == 'Review required'
    summary = workbook['Cashflows Summary']
    row = next(r for r in summary.iter_rows() if r[0].value == 'Net cash movement')
    expected = ([47, 50, 0, 97, -26.56, -13.74, 0, -40.30, 0, 0, 0, 0, 20.44, 36.26, 0, 56.70]
                if view == 'split' else [float(r[0]) for r in case['expected'][view]] +
                [{'combined': 56.70, 'bank': 20.44, 'cash': 36.26}[view]])
    assert [cell.value for cell in row[1:1+len(expected)]] == pytest.approx(expected)
    assert all(cell.data_type == 'n' for cell in row[1:1+len(expected)] if cell.value is not None)

from services.ledger_group_summary import build_cross_group_check, build_group_summary
from services.reconciliation_xlsx import (
    build_reconciliation_workbook,
    export_reconciliation_xlsx,
)


@pytest.mark.parametrize('view', ['combined', 'bank', 'cash', 'split'])
def test_cashflow_export_proves_source_transfers_and_mixed_movements_without_weighting(view):
    from services.ledger_group_summary import build_cashflow_summary
    from routers.reconciliation import _cashflow_preview
    from services.reconciliation_xlsx import build_cashflow_workbook
    journal = pd.DataFrame([
        ['T', '2026-01-05', 'B', 0, 100], ['T', '2026-01-05', 'C', 100, 0],
        ['M', '2026-01-06', 'B', 0, 105], ['M', '2026-01-06', 'C', 100, 0], ['M', '2026-01-06', 'E', 5, 0],
    ], columns=['voucher_id', 'date', 'account_code', 'debit', 'credit'])
    coa = pd.DataFrame([dict(account_code=code, account_title=code, category='Charges' if code == 'E' else '') for code in ['B', 'C', 'E']])
    summary = build_cashflow_summary(journal, coa, ['C'], ['B'], '2026-01-01', '2026-01-31')
    preview = _cashflow_preview(summary, {'transfers'}, view)
    workbook = build_cashflow_workbook(preview)
    proof = workbook['Treasury Movement Proof']
    assert [proof.cell(5, column).value for column in range(2, 11)] == ['bank', 0, -100, -105, -205, -205, 0, 'Matched', 'Review required']
    assert proof.cell(6, 7).value == 200
    assert proof.cell(7, 7).value == -5
    assert proof.cell(5, 12).value is None  # Missing ledger evidence is never invented.
    assert proof.cell(5, 7).number_format == '#,##0.00;[Red](#,##0.00);-'
    sheet = workbook['Cashflows Summary']
    rows = {sheet.cell(row, 1).value: row for row in range(1, sheet.max_row + 1)}
    movement = rows['Net cash movement']
    expected = {'combined': [-5], 'bank': [-205], 'cash': [200], 'split': [-205, 200, 0, -5]}[view]
    assert [sheet.cell(movement, column).value for column in range(2, 2+len(expected))] == expected
    if view == 'split':
        mixed = rows['Mixed voucher Treasury bridge — allocation under review']
        assert [sheet.cell(mixed, column).value for column in range(2, 6)] == [-105, 100, 5, 0]
    transfers = workbook['Internal Transfers']
    assert transfers.cell(5, 9).value == -100
    assert transfers.cell(5, 10).value == 100


def _fixture_data() -> tuple[pd.DataFrame, pd.DataFrame]:
    journal = pd.DataFrame(
        [
            ["V1", "2026-01-05", "B1", "Bank A", "Customer receipt", 100, 0],
            ["V1", "2026-01-05", "CUST", "Customer", "Customer receipt", 0, 100],
            ["V2", "2026-01-12", "B1", "Bank A", "Expense payment", 0, 40],
            ["V2", "2026-01-12", "EXP", "Expense", "Expense payment", 40, 0],
            ["V3", "2026-02-02", "B1", "Bank A", "Bank transfer", 50, 0],
            ["V3", "2026-02-02", "B2", "Bank B", "Bank transfer", 0, 50],
            ["V4", "2026-02-10", "B2", "Bank B", "Unmapped receipt", 30, 0],
            ["V4", "2026-02-10", "UNKNOWN", "Unmapped customer", "Unmapped receipt", 0, 30],
            ["V5", "2026-02-15", "B1", "Bank A", "Cash funding", 0, 20],
            ["V5", "2026-02-15", "C1", "Cashbook", "Cash funding", 20, 0],
            ["V6", "2026-02-20", "B1", "Bank A", "Charge", 0, 5],
            ["V6", "2026-02-20", "CHG", "Bank Charges", "Charge", 5, 0],
        ],
        columns=["voucher_id", "date", "account_code", "account_name", "voucher_detail", "debit", "credit"],
    )
    coa = pd.DataFrame(
        [
            ["B1", "Bank A", "Bank", "Deposits to Bank"],
            ["B2", "Bank B", "Bank", "Deposits to Bank"],
            ["C1", "Cashbook", "Cash", "Cash Balance"],
            ["CUST", "Customers", "Receivable", "Customers"],
            ["EXP", "Expense", "Expense", "Operating Expense"],
            ["CHG", "Bank Charges", "Expense", "Bank Charges and Taxes"],
        ],
        columns=["account_code", "account_title", "account_type", "category"],
    )
    return journal, coa


def _summary(with_balances: bool = False):
    journal, coa = _fixture_data()
    return build_group_summary(
        journal,
        coa,
        ["B1", "B2"],
        "2026-01-01",
        "2026-02-28",
        group_label="Bank",
        related_group_codes=["C1"],
        related_group_label="Cash",
        related_group_category="Cash Balance",
        opening_balances={"B1": 1000, "B2": 500} if with_balances else None,
        actual_closing_balances=(
            {"B1": {"2026-01": 1060, "2026-02": 1078}, "B2": {"2026-01": 500, "2026-02": 480}}
            if with_balances
            else None
        ),
    )


def test_bank_export_keeps_nonzero_categories_and_professional_structure():
    summary = _summary()
    workbook = build_reconciliation_workbook(summary)

    assert workbook.sheetnames == [
        "Bank Monthly Summary",
        "Transfer Schedule",
        "Review Schedule",
        "Reconciliation Proof",
        "Reconciliation Proof - By Bank",
    ]
    sheet = workbook["Bank Monthly Summary"]
    assert sheet["A6"].value == "Customers"
    assert sheet["B6"].value == 100
    assert sheet["B6"].number_format == '#,##0;[Red](#,##0);-'
    labels = {sheet.cell(row, 1).value: row for row in range(1, sheet.max_row + 1)}
    assert any(str(value).startswith("Total BANK INFLOWS") for value in labels)
    assert labels["Operating Expense"] > 0
    assert sheet.cell(labels["Operating Expense"], 2).value == 40
    assert sheet.cell(labels["Net Movement (Inflows - Payments)"], 2).value == 60
    assert sheet["A1"].font.name == "Arial"
    assert sheet["A5"].fill.fgColor.rgb.endswith("1F4E78")
    assert sheet.freeze_panes == "B6"


def test_cash_export_includes_transfer_review_cross_check_and_unavailable_proof():
    summary = _summary()
    cash_summary = dict(summary)
    cash_summary["group_label"] = "Cash"
    cash_summary["group_account_codes"] = ["B1", "B2"]
    cross = build_cross_group_check(summary, summary, "Cash Balance", "Cash Balance")
    workbook = build_reconciliation_workbook(cash_summary, cross_group_check=cross)

    assert "Inter-Cashbook Transfers" in workbook.sheetnames
    assert "Cross-check vs Bank Summary" in workbook.sheetnames
    assert workbook["Reconciliation Proof"]["A5"].value.startswith("Balance inputs not provided")
    assert workbook["Reconciliation Proof"]["A5"].fill.fgColor.rgb.endswith("FCE4D6")
    assert workbook["Review Schedule"]["A5"].value in {"Jan-26", "Feb-26"}


def test_reconciliation_proofs_are_written_when_balances_are_available():
    summary = _summary(with_balances=True)
    workbook = build_reconciliation_workbook(summary, opening_balances={"B1": 1000, "B2": 500})

    combined = workbook["Reconciliation Proof"]
    assert combined["A5"].value == "Opening 01-Jan-26"
    assert combined["C5"].value == 1500
    assert combined["F6"].value == "Matched"
    assert workbook["Reconciliation Proof - By Bank"]["A4"].value == "Bank A [B1]"


def test_exported_xlsx_round_trips_as_typed_numbers(tmp_path):
    output = tmp_path / "bank_report.xlsx"
    output.write_bytes(export_reconciliation_xlsx(_summary()))

    workbook = openpyxl.load_workbook(output, data_only=True)
    sheet = workbook["Bank Monthly Summary"]
    assert isinstance(sheet["B6"].value, (int, float))
    assert sheet["B6"].value == 100
    assert workbook.calculation.calcMode == "auto"


def test_reference_workbooks_define_the_golden_tab_contract_when_present():
    bank_path = Path("uploads/Bank_Monthly_Summary v1.xlsx")
    cash_path = Path("uploads/Cash_Monthly_Summary.xlsx")
    if not bank_path.exists() or not cash_path.exists():
        return

    bank = openpyxl.load_workbook(bank_path, read_only=True, data_only=False)
    cash = openpyxl.load_workbook(cash_path, read_only=True, data_only=False)
    assert bank.sheetnames == ["Bank Monthly Summary", "Reconciliation Proof", "Reconciliation Proof - By Bank"]
    assert cash.sheetnames == [
        "Cash Monthly Summary",
        "Inter company",
        "Cross-check vs Bank Summary",
        "Inter-Cashbook Transfers",
        "Reconciliation Proof",
        "Reconciliation Proof - By Book",
    ]
    assert [bank["Bank Monthly Summary"].cell(5, column).value for column in range(1, 9)] == [
        "Category", "Jan-26", "Feb-26", "Mar-26", "Apr-26", "May-26", "Jun-26", "Total"
    ]
    assert [cash["Cash Monthly Summary"].cell(5, column).value for column in range(1, 9)] == [
        "Category", "Jan-26", "Feb-26", "Mar-26", "Apr-26", "May-26", "Jun-26", "Total"
    ]


def test_generated_golden_movements_match_reference_workbooks_when_source_is_present():
    source_path = Path("uploads/1. All Vouchers (Jan To June) - Copy.xlsx")
    bank_path = Path("uploads/Bank_Monthly_Summary v1.xlsx")
    cash_path = Path("uploads/Cash_Monthly_Summary.xlsx")
    if not source_path.exists() or not bank_path.exists() or not cash_path.exists():
        return

    journal = pd.read_excel(source_path, sheet_name="All vouchers").rename(
        columns={
            "Voucher ID": "voucher_id",
            "Date": "date",
            "Account Code": "account_code",
            "Account Name": "account_name",
            "Voucher Detail": "voucher_detail",
            "Debit": "debit",
            "Credit": "credit",
        }
    )
    coa = pd.read_excel(source_path, sheet_name="Categories assigned").rename(
        columns={"Account #": "account_code", "Account Title": "account_title", "Account Type": "account_type", "Cat": "category"}
    )
    bank = build_group_summary(
        journal,
        coa,
        [
            "10-15-115-15-0001",
            "10-15-115-15-0002",
            "10-15-115-15-0003",
            "10-15-115-15-0004",
            "10-15-115-15-0005",
            "10-15-115-15-0006",
        ],
        "2026-01-01",
        "2026-06-30",
        group_label="Bank",
        related_group_codes=["10-15-110-0001", "10-15-115-10-0001", "10-15-115-10-0003"],
        related_group_category="Cash Balance",
    )
    cash = build_group_summary(
        journal,
        coa,
        ["10-15-110-0001", "10-15-115-10-0001", "10-15-115-10-0003"],
        "2026-01-01",
        "2026-06-30",
        group_label="Cash",
        related_group_codes=[
            "10-15-115-15-0001",
            "10-15-115-15-0002",
            "10-15-115-15-0003",
            "10-15-115-15-0004",
            "10-15-115-15-0005",
            "10-15-115-15-0006",
        ],
        related_group_category="Received/Paid to Bank",
    )

    def net_values(summary):
        workbook = build_reconciliation_workbook(summary)
        sheet = workbook[f"{summary['group_label']} Monthly Summary"]
        row = next(row for row in range(1, sheet.max_row + 1) if sheet.cell(row, 1).value == "Net Movement (Inflows - Payments)")
        return [sheet.cell(row, column).value for column in range(2, 8)]

    bank_reference = openpyxl.load_workbook(bank_path, data_only=True)
    cash_reference = openpyxl.load_workbook(cash_path, data_only=True)
    expected_bank = [bank_reference["Bank Monthly Summary"].cell(29, column).value for column in range(2, 8)]
    expected_cash = [cash_reference["Cash Monthly Summary"].cell(37, column).value for column in range(2, 8)]
    assert net_values(bank) == pytest.approx(expected_bank)
    assert net_values(cash) == pytest.approx(expected_cash)


def test_signed_party_export_keeps_reversals_and_negative_net_as_numbers():
    from services.reconciliation_xlsx import build_cashflow_workbook
    preview = dict(periods=[dict(key='2026-01', label='Jan-26')], category_totals=[],
        customer_payment_account_period_totals=[dict(period_label='Jan-26', customer_account_name='Customer',
            customer_account_code='AR', customer_receipts=100, customer_reversals=140,
            customer_gross_amount=-40, cashflow_amount=-40)],
        vendor_payment_period_totals=[dict(period_label='Jan-26', account_name='Vendor', account_code='AP',
            category='Suppliers', payments=90, reversals=120, net_payments=-30)])
    workbook = build_cashflow_workbook(preview)
    customers = [r for r in workbook['Customer Period Totals'].iter_rows(values_only=True) if 'Customer' in r]
    vendors = [r for r in workbook['Vendor Period Totals'].iter_rows(values_only=True) if 'Vendor' in r]
    assert any(100 in r and 140 in r and -40 in r for r in customers)
    assert any(90 in r and 120 in r and -30 in r for r in vendors)
