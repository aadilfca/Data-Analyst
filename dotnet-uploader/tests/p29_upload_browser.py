"""Real Chromium acceptance against the owned Kestrel host supplied by xUnit."""
import json
import re
import sys
import time
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlparse

from openpyxl import load_workbook
from playwright.sync_api import expect, sync_playwright


def disabled_check(directory):
    """Exercise the retained DOM with no backend or network access."""
    root = Path(__file__).resolve().parents[2]
    html = (root / 'static' / 'upload.html').read_text(encoding='utf-8')
    html = re.sub(r'<script\b[^>]*>.*?</script>', '', html, flags=re.S)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width': 1450, 'height': 1000})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.route('**/*', lambda route: route.abort())
        page.set_content(html)
        page.add_style_tag(content=(root / 'dotnet-uploader/frontend/upload_native.css').read_text())
        page.evaluate('window.AppNativeUpload = {enabled:false}')
        page.add_script_tag(content=(root / 'dotnet-uploader/frontend/upload_native.js').read_text())
        assert 'disabled' in page.locator('#nativeUploadNotice').inner_text()
        assert page.locator('#mainUploadPage button:enabled, #mainUploadPage select:enabled, #mainUploadPage input:enabled').count() == 0
        assert not errors, errors
        page.screenshot(path=str(directory / 'disabled.png'), full_page=True)
        browser.close()


def main():
    directory = Path(sys.argv[1]).resolve()
    if len(sys.argv) > 2 and sys.argv[2] == '--disabled':
        directory.mkdir(parents=True, exist_ok=True)
        disabled_check(directory)
        print('Disabled native DOM check passed without a backend.')
        return
    address = json.loads((directory / 'host.json').read_text())['address']
    assert urlparse(address).hostname == '127.0.0.1'
    oracle = json.loads((directory / 'oracle.json').read_text())
    errors, requests, measurements, responses = [], [], [], []
    imports = 0
    repeat_receipts = 0
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(viewport={'width': 1450, 'height': 1000}, accept_downloads=True)
        context.add_cookies([{'name': 'session_token', 'value': 'a' * 64, 'url': address}])
        page = context.new_page()
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('request', lambda request: requests.append((request.method, urlparse(request.url).path)))
        def finished(request):
            if '/api/' not in request.url:
                return
            try:
                response = request.response()
                responses.append({'path': urlparse(request.url).path, 'bytes': len(response.body()),
                                  'milliseconds': request.timing['responseEnd']})
            except Exception:
                pass  # Deliberately interrupted responses are separately asserted.
        page.on('requestfinished', finished)

        def ready():
            page.goto(address + '/static/upload.html')
            expect(page.locator('#templateDropdown option')).to_have_count(2)
            assert page.locator('script[src*="/static/upload.js"]').count() == 0
            assert page.locator('script[src*="/shell/upload_native.js"]').count() == 1
            assert page.evaluate('NAV_MANIFEST.some(g => (g.items || []).some(i => (i.children || []).some(c => String(c.url || "").includes("feature=upload-jobs") && (c.keywords || []).includes("receipt"))))')

        def stage(name, partial=False):
            if page.locator('#nativeNewUpload').is_visible():
                page.locator('#nativeNewUpload').click()
            page.locator('#templateDropdown').select_option('1')
            expect(page.locator('#uploadFileBtn')).to_be_enabled()
            page.locator('#fileInput').set_input_files(directory / f'{name}.xlsx')
            expect(page.locator('#mappingBody tr')).to_have_count(6, timeout=60000)
            expect(page.locator('#importBtn')).to_be_enabled(timeout=60000)
            page.locator('#sourceControlRow').fill(str(oracle[name]['control_row']))
            page.locator('#sourceControlRow').blur()
            page.locator('#sourceControlConfirmed').check()
            if partial:
                page.locator('#comparisonModeDropdown').select_option('correction')
                expect(page.locator('#importBtn')).to_be_enabled()
            else:
                page.locator('#comparisonModeDropdown').select_option('replace_range')
                expect(page.locator('#importBtn')).to_be_enabled()
                page.locator('#uploadCoverageStart').fill('2026-04-01')
                page.locator('#uploadCoverageEnd').fill('2026-04-30')
                page.locator('#uploadConfirmCoverage').check()
                if name == 'empty':
                    page.locator('#uploadConfirmEmpty').check()

        def review():
            page.locator('#importBtn').click()
            try:
                page.wait_for_function('''() => !document.getElementById('nativeUploadReview').hidden
                    || document.getElementById('nativeUploadNotice').dataset.severity === 'error' ''', timeout=90000)
                expect(page.locator('#nativeUploadReview')).to_be_visible()
            except Exception:
                raise AssertionError(page.locator('#nativeUploadNotice').inner_text())

        def wait_file(name):
            deadline = time.monotonic() + 40
            while not (directory / name).exists():
                assert time.monotonic() < deadline, f'Owned coordination {name} did not complete'
                page.wait_for_timeout(100)

        def checkpoint(name):
            (directory / f'checkpoint-{name}').write_text('ready')
            wait_file(f'checked-{name}')

        def start_held_job():
            stage('repeat')
            review()
            expect(page.locator('#uploadStepIndicator .step-item[aria-current="step"]')).to_contain_text('Review and import')
            (directory / 'hold-source').write_text('owned storage gate')
            page.locator('#nativeImportConfirmation').check()
            page.locator('#nativeSubmit').click()
            page.wait_for_function('''async () => {
                const h = Object.fromEntries(new URL(location.href).searchParams);
                const r = await fetch(`/api/upload/file/${h.upload_file}/jobs/${h.upload_job}`);
                const j = (await r.json()).job;
                return j && j.attempts > 0 && !j.sourceReady;
            }''', polling=500, timeout=20000)

        def cancellation_and_resume():
            start_held_job()
            try:
                # UI presentation for late cancellation; actual pinned-transaction
                # behavior is separately exercised by the existing P28 regression.
                page.route('**/cancel', lambda route: route.fulfill(status=409, content_type='application/json',
                           body=json.dumps({'status': 'busy', 'code': 'job_finishing_or_busy'})), times=1)
                page.locator('#nativeCancel').click()
                expect(page.locator('#nativeUploadNotice')).to_contain_text('finishing')
                assert 'cancelled' not in page.locator('#nativeJobState').inner_text()
                page.locator('#nativeCancel').click()
                expect(page.locator('#nativeJobState')).to_contain_text('cancelled')
            finally:
                (directory / 'hold-source').unlink(missing_ok=True)
            checkpoint('cancelled')
            start_held_job()
            try:
                (directory / 'rotate-session').write_text('invalidate original attached session')
                wait_file('rotated-session')
                context.add_cookies([{'name': 'session_token', 'value': 'd' * 64, 'url': address}])
            finally:
                (directory / 'hold-source').unlink(missing_ok=True)
            page.reload()
            expect(page.locator('#nativeJobState')).to_contain_text('authorization required', timeout=30000)
            page.locator('#nativeResume').click()
            expect(page.locator('#nativeUploadReceipt')).to_be_visible(timeout=60000)
            checkpoint('resumed')

        def stale_review():
            stage('correction')
            review()
            status = page.evaluate('''async () => {
                const s = await (await fetch('/api/upload/saved-mapping?client_id=10&template_id=1')).json();
                const r = await fetch('/api/upload/save-mapping', {method:'POST', headers:{'Content-Type':'application/json'},
                    body:JSON.stringify({client_id:10, template_id:1, mapping_revision:s.mapping_revision, configuration_key:s.configuration_key,
                        mappings:['Document','Date','Amount','Memo','Balance','Extra'].map((h,i)=>({template_column_id:i+1,excel_column:h}))})});
                return r.status;
            }''')
            assert status == 200
            before = sum(path.endswith('/jobs') and method == 'POST' for method, path in requests)
            page.locator('#nativeImportConfirmation').check()
            page.locator('#nativeSubmit').click()
            expect(page.locator('#nativeUploadReview')).to_be_hidden()
            expect(page.locator('#nativeUploadNotice')).to_contain_text('changed')
            assert sum(path.endswith('/jobs') and method == 'POST' for method, path in requests) == before
            checkpoint('stale')

        def foreign_job(url):
            handle = dict(part.split('=', 1) for part in url.split('?', 1)[1].split('&'))
            for token in ('b', 'c'):
                foreign = browser.new_context()
                foreign.add_cookies([{'name':'session_token', 'value':token*64, 'url':address}])
                other = foreign.new_page()
                other.goto(address + '/static/upload.html')
                other.wait_for_function('typeof window.AppNativeUpload !== "undefined"')
                status = other.evaluate('''async h => (await fetch(`/api/upload/file/${h.upload_file}/jobs/${h.upload_job}`)).status''', handle)
                assert status == 404, (token, status)
                assert not other.locator('#nativeUploadReceipt').is_visible()
                foreign.close()

        def complete(name, partial=False, drop_response=False):
            nonlocal imports, repeat_receipts
            start = time.perf_counter()
            stage(name, partial)
            staged = time.perf_counter()
            review()
            reviewed = time.perf_counter()
            # Source numbers and evidence stay string-valued, preserving exact meaning.
            if name == 'original':
                assert page.locator('#rowExclusionBody').inner_text().count('001') == 2
                page.screenshot(path=str(directory / 'review.png'), full_page=True)
            page.locator('#nativeImportConfirmation').check()
            if drop_response:
                def lose_response(route):
                    route.fetch()
                    route.abort('failed')
                page.route('**/jobs', lose_response, times=1)
            page.locator('#nativeSubmit').dblclick()
            expect(page.locator('#nativeUploadJob')).to_be_visible()
            # Reload while execution may be in progress; recover the saved job handle.
            if name == 'original':
                page.reload()
            try:
                page.wait_for_function('''() => !document.getElementById('nativeUploadReceipt').hidden
                    || /review required|failed|expired|cancelled/.test(document.getElementById('nativeJobState').textContent)''', timeout=120000)
                expect(page.locator('#nativeUploadReceipt')).to_be_visible()
            except Exception:
                raise AssertionError(page.locator('#nativeJobState').inner_text() + ': ' + page.locator('#nativeJobCode').inner_text() + ' ' + page.locator('#nativeUploadNotice').inner_text())
            receipt_url = page.url
            handle = page.evaluate('Object.fromEntries(new URL(location.href).searchParams)')
            receipt_id = page.locator('#nativeDownloadReceipt').get_attribute('id')
            assert receipt_id
            assert 'succeeded' in page.locator('#nativeJobState').inner_text()
            # Fetch original immutable receipt through the owner/context-protected API.
            result = page.evaluate('''async h => {
                const r = await fetch(`/api/upload/file/${h.upload_file}/jobs/${h.upload_job}`);
                const j = await r.json();
                const rr = await fetch(`/api/upload/file/${h.upload_file}/receipt/${j.job.completion.receiptId}`);
                return {job:j.job, receipt:(await rr.json()).receipt};
            }''', handle)
            receipt = result['receipt']
            assert receipt['source']['approvedDetail']['rowCount'] == oracle[name]['rows']
            assert Decimal(receipt['source']['approvedDetail']['totals']['3']) == Decimal(oracle[name]['total'])
            assert receipt['source']['approvedDetail']['documentCount'] == oracle[name]['documents']
            assert receipt['reconciliation']['status'] not in ('mismatch', 'unavailable')
            if receipt['kind'] == 'no_changes':
                repeat_receipts += 1
            else:
                imports += 1
            measurements.append({'case': name, 'seconds': round(time.perf_counter() - start, 3),
                                 'stagingAndPreviewSeconds': round(staged - start, 3),
                                 'reviewSeconds': round(reviewed - staged, 3),
                                 'submissionAndCompletionSeconds': round(time.perf_counter() - reviewed, 3),
                                 'rows': oracle[name]['rows'], 'job': result['job']['id'],
                                 'receiptBytes': len(json.dumps(receipt).encode()), 'counts': receipt['counts']})
            (directory / f'{name}-receipt.json').write_text(json.dumps(receipt))
            # The parent independently checks the canonical rows/history before
            # the next browser scenario can overwrite this scope.
            checkpoint(name)
            if name == 'original':
                with page.expect_download() as pending:
                    page.locator('#nativeDownloadReceipt').click()
                workbook = directory / 'evidence.xlsx'
                pending.value.save_as(workbook)
                evidence = load_workbook(workbook, read_only=True, data_only=True)
                assert len(evidence.sheetnames) == 5
                evidence.close()
                with page.expect_download() as pending:
                    page.locator('#downloadBtn').click()
                pending.value.save_as(directory / 'template.xlsx')
                template = load_workbook(directory / 'template.xlsx', read_only=True)
                assert list(next(template.active.values)) == ['Document', 'Date', 'Amount', 'Memo', 'Balance', 'Extra']
                template.close()
                for control_status in ('mismatch', 'unavailable'):
                    def altered_receipt(route):
                        status = control_status
                        response = route.fetch()
                        payload = response.json()
                        payload['receipt']['reconciliation']['status'] = status
                        payload['receipt']['reconciliation']['message'] = f'Synthetic UI {status} presentation check.'
                        payload['receipt']['reconciliation']['metrics'][0]['matches'] = False if status == 'mismatch' else None
                        route.fulfill(response=response, json=payload)
                    page.route('**/receipt/*', altered_receipt, times=1)
                    page.locator('#nativeReloadReceipt').click()
                    expect(page.locator('#nativeUploadNotice')).to_contain_text(control_status)
                    expect(page.locator('#nativeUploadNotice')).to_have_attribute('data-severity', 'error')
                page.locator('#nativeReloadReceipt').click()
                expect(page.locator('#nativeUploadNotice')).to_have_attribute('data-severity', 'success')
                (directory / 'restart-host').write_text('restart owned Kestrel')
                wait_file('restarted-host')
                page.reload()
                expect(page.locator('#nativeUploadReceipt')).to_be_visible(timeout=30000)
                assert page.evaluate('new URL(location.href).searchParams.get("upload_job")') == handle['upload_job']
            return receipt_url

        try:
            ready()
            complete('original', drop_response=True)
            complete('repeat')
            cancellation_and_resume()
            stale_review()
            complete('correction')
            complete('partial', partial=True)
            complete('empty')
            capacity_start = time.perf_counter()
            stage('oversize')
            page.locator('#importBtn').click()
            expect(page.locator('#nativeUploadNotice')).to_contain_text('exceeds the supported review size', timeout=90000)
            assert not page.locator('#nativeUploadReview').is_visible()
            checkpoint('oversize')
            measurements.append({'case': 'oversize', 'rows': 10000, 'seconds': round(time.perf_counter() - capacity_start, 3), 'result': 'bounded refusal; no accounting writes'})
            last_job = complete('large')
            page.screenshot(path=str(directory / 'completion.png'), full_page=True)
            foreign_job(last_job)
            stage('invalid')
            page.locator('#importBtn').click()
            expect(page.locator('#nativeUploadNotice')).to_have_attribute('data-severity', 'error', timeout=30000)
            assert not page.locator('#nativeUploadReview').is_visible()
            checkpoint('invalid')
            (directory / 'disable-ui').write_text('disable test UI only')
            wait_file('disabled-ui')
            before = len(requests)
            page.goto(address + '/static/upload.html')
            expect(page.locator('#nativeUploadNotice')).to_contain_text('disabled')
            assert page.locator('#mainUploadPage button:enabled, #mainUploadPage select:enabled, #mainUploadPage input:enabled').count() == 0
            assert not any(path.startswith(('/api/upload/', '/api/import-excel')) for _, path in requests[before:])
            checkpoint('disabled')
            assert not errors, errors
            forbidden = {'/api/upload/validate', '/api/upload/import', '/api/upload/auto-map', '/api/upload/backup-policy', '/api/upload/import-progress'}
            assert not any(path in forbidden for _, path in requests)
            (directory / 'browser-results.json').write_text(json.dumps({'accepted': True, 'imports': imports,
                'repeatReceipts': repeat_receipts, 'measurements': measurements, 'responses': responses,
                'requests': len(requests), 'errors': errors}))
        except Exception:
            page.screenshot(path=str(directory / 'failure.png'), full_page=True)
            (directory / 'failure.txt').write_text(page.locator('body').inner_text() + '\n' + repr(errors), encoding='utf-8')
            raise
        finally:
            browser.close()


if __name__ == '__main__':
    main()
