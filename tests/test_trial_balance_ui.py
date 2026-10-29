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
