"""Synthetic P11 fixture: existing Chromium plus pure Python date-default oracle."""
import sys
from pathlib import Path
from datetime import date
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright, expect

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from routers.settings import _resolved_cashflow_defaults

base = sys.argv[1]
assert urlparse(base).hostname in ("127.0.0.1", "localhost")
with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    context = browser.new_context()
    page = context.new_page()
    page.on("dialog", lambda dialog: dialog.accept())
    page.goto(base + "/static/login.html")
    page.locator("#usernameInput").fill("settings-user")
    page.locator("#passwordInput").fill("p11-synthetic")
    page.locator("#loginSubmitBtn").click()
    expect(page).to_have_url(base + "/shell/tenant_select.html")
    page.get_by_role("button", name="Firm A — CA firm", exact=True).click()
    expect(page).to_have_url(base + "/static/client_select.html")
    page.goto(base + "/static/settings.html")
    expect(page.locator("#saveSettingsBtn")).to_be_enabled()
    expect(page.locator("#googleDrivePanel")).to_be_hidden()
    page.locator("#setting_date_format").select_option("DD/MMM/YYYY")
    page.locator("#saveSettingsBtn").click()
    expect(page.locator("#statusMessage")).to_contain_text("saved successfully")
    page.reload()
    expect(page.locator("#setting_date_format")).to_have_value("DD/MMM/YYYY")
    page.locator("#holidayDateInput").fill("2026-04-01")
    payload = '<img src=x onerror="window.p11Injected=true">'
    page.locator("#holidayLabelInput").fill(payload)
    page.locator("#holidayRepeatsYearly").check()
    page.locator("#addHolidayBtn").click()
    expect(page.locator(".holiday-label")).to_have_text(payload)
    expect(page.locator("#holidayListContainer img")).to_have_count(0)
    expect(page.locator(".holiday-yearly-badge")).to_be_visible()
    assert page.evaluate("window.p11Injected === undefined")
    page.locator(".delete-holiday-btn").click()
    expect(page.locator(".holiday-row")).to_have_count(0)
    page.locator("#uploadBackupRequired").check()
    page.locator("#saveUploadBackupsBtn").click()
    expect(page.locator("#uploadBackupsStatus")).to_contain_text("Saved choice: backups ON")

    # Same pure reference function as Python's API, without invoking its SQL helpers.
    for month, period, start, end in [(1, "ytd", None, None), (7, "financial_year", None, None),
                                     (4, "ytd", None, None), (1, "last_12_months", None, None),
                                     (1, "custom", "2024-02-29", "2026-03-31")]:
        update = dict(fiscal_year_start_month=month, default_period=period, custom_start=start, custom_end=end)
        actual = page.evaluate("""async input => {
            const response = await fetch('/api/settings/cashflows-defaults', {method:'POST',
                headers:{'Content-Type':'application/json'},body:JSON.stringify(input)});
            if (!response.ok) throw new Error('defaults save rejected');
            return await response.json();
        }""", update)
        values = dict(cashflow_fiscal_year_start_month=str(month), cashflow_default_period=period,
                      cashflow_custom_start=start or "", cashflow_custom_end=end or "")
        assert actual == _resolved_cashflow_defaults(values, date(2026, 3, 31)), (actual, values)
    # Exercise the actual report-default controls and saved custom range.
    page.reload()
    expect(page.locator("#cashflowDefaultPeriod")).to_have_value("custom")
    page.locator("#cashflowCustomStart").fill("2025-07-01")
    page.locator("#saveCashflowDefaultsBtn").click()
    expect(page.locator("#statusMessage")).to_contain_text("Cashflows report defaults saved")
    page.reload()
    expect(page.locator("#cashflowCustomStart")).to_have_value("2025-07-01")
    page.get_by_role("link", name="Change workspace", exact=True).click()
    page.get_by_role("button", name="Business B — Business", exact=True).click()
    expect(page).to_have_url(base + "/static/client_select.html")
    page.goto(base + "/static/settings.html")
    expect(page.locator("#settingsReadOnlyNotice")).to_be_visible()
    expect(page.locator("#saveSettingsBtn")).to_be_disabled()
    expect(page.locator("#addHolidayBtn")).to_be_disabled()
    expect(page.locator("#saveCashflowDefaultsBtn")).to_be_disabled()
    expect(page.locator("#uploadBackupsPanel")).to_be_hidden()
    expect(page.locator("#setting_date_format")).to_have_value("MM/DD/YYYY")
    result = page.evaluate("""async () => {
        const r = await fetch('/api/settings/date_format', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({value:'denied'})});
        return r.status;
    }""")
    assert result == 403
    # The shared script's original Python mode has no shell capability object.
    # It retains editable controls while using the same safe text rendering.
    page.evaluate("delete window.AppTenantSettings; renderSettings(); renderCashflowDefaults();")
    expect(page.locator("#saveSettingsBtn")).to_be_enabled()
    expect(page.locator("#saveCashflowDefaultsBtn")).to_be_enabled()
    browser.close()
print("P11 browser and Python date-default comparison passed")
