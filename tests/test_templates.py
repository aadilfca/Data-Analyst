from pathlib import Path

from playwright.sync_api import sync_playwright


TEMPLATES_HTML = Path(__file__).resolve().parents[1] / "static" / "templates.html"
TEMPLATES_JS = Path(__file__).resolve().parents[1] / "static" / "templates.js"


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
