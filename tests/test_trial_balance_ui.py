"""Hermetic browser coverage for Trial Balance mapping and validation views."""

from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import sync_playwright  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as playwright:
        try:
            instance = playwright.chromium.launch(headless=True)
        except Exception as exc:
            pytest.skip("Chromium not available: {}".format(exc))
        try:
            yield instance
        finally:
            instance.close()


def test_validation_grid_shows_mapped_columns_only_and_preserves_source_preview(browser):
    page = browser.new_page()
    page.set_content("""
        <table><thead id="mappingPreviewHead"></thead><tbody id="mappingPreviewBody"></tbody></table>
        <table><thead id="tbValidationHead"></thead><tbody id="tbValidationBody"></tbody></table>
    """)
    page.add_script_tag(path=str(PROJECT_ROOT / "static" / "trial_balance.js"))

    result = page.evaluate("""() => {
        fileHeaders = [
            'Account Code', 'Account Name', 'Opening Debit', 'Opening Credit',
            'Period Debit', 'Period Credit', 'Closing Debit', 'Closing Credit',
            'Start Date', 'End Date', 'Unnamed: 10'
        ];
        tbColumnMapping = {
            account_code_column: 'Account Code',
            account_name_column: 'Account Name',
            opening_dr_column: 'Opening Debit',
            opening_cr_column: 'Opening Credit',
            period_dr_column: 'Period Debit',
            period_cr_column: 'Period Credit',
            closing_dr_column: 'Closing Debit',
            closing_cr_column: 'Closing Credit'
        };
        const sourceRow = [
            '1', 'ASSETS', '100', '0', '50', '0', '150', '0',
            '2025-07-01', '2026-06-30', '3222222.22'
        ];
        filePreviewRows = [sourceRow];
        fileAllRows = [sourceRow];
        renderMappingPreviewTable();
        renderTrialBalanceValidationGrid();
        return {
            previewHeaders: Array.from(document.querySelectorAll('#mappingPreviewHead th')).map(cell => cell.textContent),
            previewExtraValues: Array.from(document.querySelectorAll('#mappingPreviewBody td')).slice(8).map(cell => cell.textContent),
            validationHeaders: Array.from(document.querySelectorAll('#tbValidationHead th')).map(cell => cell.textContent),
            validationValues: Array.from(document.querySelectorAll('#tbValidationBody tr:first-child td')).map(cell => cell.textContent),
            validationExcelRow: document.querySelector('#tbValidationBody tr')?.dataset.excelRow
        };
    }""")

    assert result["previewHeaders"][-3:] == ["Start Date", "End Date", "Unnamed: 10"]
    assert result["previewExtraValues"] == ["2025-07-01", "2026-06-30", "3222222.22"]
    assert result["validationHeaders"] == [
        "Excel Row", "Account Code", "Account Name", "Classification",
        "Opening Debit", "Opening Credit", "Period Debit", "Period Credit",
        "Closing Debit", "Closing Credit", "Validation Issues",
    ]
    assert not {"2025-07-01", "2026-06-30", "3222222.22"}.intersection(result["validationValues"])
    assert result["validationExcelRow"] == "2"

    page.close()


def test_rollforward_differences_render_as_red_section_without_changing_source_row(browser):
    page = browser.new_page()
    page.set_content("""
        <table><thead id="tbValidationHead"></thead><tbody id="tbValidationBody"></tbody></table>
        <table><tbody id="availableAccountsTableBody"></tbody></table>
    """)
    page.add_script_tag(path=str(PROJECT_ROOT / "static" / "trial_balance.js"))

    result = page.evaluate("""() => {
        fileHeaders = [
            'Account Code', 'Account Name', 'Opening Debit', 'Opening Credit',
            'Period Debit', 'Period Credit', 'Closing Debit', 'Closing Credit'
        ];
        tbColumnMapping = {
            account_code_column: 'Account Code', account_name_column: 'Account Name',
            opening_dr_column: 'Opening Debit', opening_cr_column: 'Opening Credit',
            period_dr_column: 'Period Debit', period_cr_column: 'Period Credit',
            closing_dr_column: 'Closing Debit', closing_cr_column: 'Closing Credit'
        };
        fileAllRows = [['A1', 'Cash', '100', '0', '20', '0', '110', '0']];
        validationResult = {rollforward_differences: [{
            excel_row: 2, account_code: 'A1', account_name: 'Cash',
            closing_dr_adjustment: 10, closing_cr_adjustment: 0
        }]};
        validationIssueSnapshot = {errors: [], warnings: [{
            excel_row: 2, field: 'Roll-forward',
            message: 'Opening balance plus period movement does not equal closing balance within 0.01.'
        }]};
        autoClassificationByRow = {'2': 'leaf'};
        renderTrialBalanceValidationGrid();
        const sourceRow = document.querySelector('#tbValidationBody tr[data-excel-row="2"]');
        const originalClosingDebit = sourceRow.children[8].textContent;
        const differenceRow = document.querySelector('#tbValidationBody tr.tb-generated-difference');
        renderAvailableAccountsTable([], validationResult.rollforward_differences);
        return {
            originalClosingDebit,
            differenceLabel: differenceRow.children[2].textContent,
            differenceClosingDebit: differenceRow.children[8].textContent,
            validationSection: document.querySelector('#tbValidationBody tr.tb-difference-section')?.textContent,
            savedSection: document.querySelector('#availableAccountsTableBody tr.tb-difference-section')?.textContent,
            savedLabel: document.querySelector('#availableAccountsTableBody tr.tb-generated-difference td:nth-child(2)')?.textContent,
            savedDebit: document.querySelector('#availableAccountsTableBody tr.tb-generated-difference td:nth-child(7)')?.textContent
        };
    }""")

    assert result["originalClosingDebit"] == "110"
    assert result["differenceLabel"] == "Difference — Cash"
    assert result["differenceClosingDebit"] == "10"
    assert "System-generated differences (1)" in result["validationSection"]
    assert "System-generated differences (1)" in result["savedSection"]
    assert result["savedLabel"] == "Difference — Cash"
    assert result["savedDebit"] == "10"

    page.close()
