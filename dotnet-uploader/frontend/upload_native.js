// Native-host coordinator. Source cells stay on the server; browser state is
// bounded, disposable and never authorizes an import without fresh server checks.
(() => {
    'use strict';
    const $ = id => document.getElementById(id);
    const uuid = () => crypto.randomUUID();
    const terminal = new Set(['succeeded', 'cancelled', 'failed', 'expired', 'review_required']);
    const controllers = new Set();
    const state = { generation: 0, client: null, template: null, catalog: [], columns: [], file: null,
        staging: null, sheet: '', context: null, mappings: [], headers: [], excluded: new Set(), forced: new Set(),
        page: 1, pages: 1, busy: false, review: null, coverage: null, plan: null, pending: null,
        job: null, handle: null, receipt: null, timer: null, pollFailures: 0, issuePage: 1 };
    const cacheKey = 'native-upload-handle-v1';
    let notice, reviewPanel, jobPanel, receiptPanel, detailPanel;

    function node(tag, text, attrs = {}) {
        const el = document.createElement(tag);
        if (text !== null) el.textContent = text;
        Object.entries(attrs).forEach(([key, value]) => el.setAttribute(key, value));
        return el;
    }
    function button(id, label, action, parent, primary = false) {
        const el = node('button', label, { id, type: 'button' });
        if (primary) el.className = 'native-primary';
        el.addEventListener('click', () => void guarded(el, action)); parent.append(el); return el;
    }
    function panel(id, title) {
        const section = node('section', null, { id, class: 'native-upload-panel' });
        section.append(node('h2', title)); $('mainUploadPage').append(section); return section;
    }
    function message(text, severity = 'info') {
        notice.textContent = text; notice.dataset.severity = severity;
        if (severity === 'error') notice.closest('section').scrollIntoView({ block: 'start' });
    }
    function step(number) {
        document.querySelectorAll('#uploadStepIndicator .step-item').forEach((item, index) => {
            item.classList.toggle('active', index + 1 === number);
            if (index + 1 === number) item.setAttribute('aria-current', 'step'); else item.removeAttribute('aria-current');
        });
    }
    function displayTime(value) {
        const date = new Date(value); return Number.isNaN(date.getTime()) ? 'unavailable' : date.toLocaleString();
    }
    function countLabel(key) {
        return { incoming: 'Incoming rows', existing: 'Previously stored rows', unchanged: 'Unchanged', changed: 'Changed',
            added: 'Added', removed: 'Removed', retained: 'Retained', informationalUpdates: 'Informational updates', sourceOrderUpdates: 'Source order updates' }[key] || key;
    }
    function errorText(data, status) {
        const code = data?.detail?.code || data?.code || data?.error;
        const explanations = {
            saved_mapping_needs_review: 'The saved mapping needs review. Save the current mapping or explicitly clear the saved mapping, then prepare again.',
            preparation_context_changed: 'The template or saved mapping changed. Reload the template and review the mapping again.',
            job_finishing_or_busy: 'The job is finishing or busy. Check its status; cancellation has not been confirmed.',
            job_authorization_required: 'Select the original workspace and client, then explicitly resume this job.',
            job_not_found: 'The submission is not yet confirmed. Check again before starting another upload.',
            plan_expired: 'The review expired. Validate the current workbook again.',
            plan_payload_capacity: 'This report exceeds the supported review size. Request a smaller complete reporting period or an appropriately scoped Partial extract.',
            job_expired_or_attempts_exhausted: 'This job expired or exhausted its attempts. Review the current source before starting again.'
        };
        return explanations[code] || (typeof data?.detail === 'string' ? data.detail : data?.detail?.message)
            || (code ? `Review required: ${String(code).replaceAll('_', ' ')}.` : `Request failed (${status}). Check the current status before trying again.`);
    }
    async function bytes(response, maximum) {
        if (Number(response.headers.get('Content-Length')) > maximum) throw new Error('The server response exceeds the supported size.');
        const reader = response.body.getReader(); const parts = []; let total = 0;
        try {
            while (true) {
                const { done, value } = await reader.read(); if (done) break;
                total += value.length; if (total > maximum) throw new Error('The server response exceeds the supported size.');
                parts.push(value);
            }
        } catch (error) { await reader.cancel().catch(() => {}); throw error; }
        const output = new Uint8Array(total); let offset = 0;
        parts.forEach(part => { output.set(part, offset); offset += part.length; }); return output;
    }
    async function request(path, { method = 'GET', body, headers = {}, timeout = 75000, maximum = 1048576, binary = false } = {}) {
        const controller = new AbortController(); controllers.add(controller);
        const timer = setTimeout(() => controller.abort(), timeout);
        try {
            const options = { method, cache: 'no-store', credentials: 'same-origin', signal: controller.signal, headers: { ...headers } };
            if (body instanceof FormData) options.body = body;
            else if (body !== undefined) {
                options.body = JSON.stringify(body);
                if (new TextEncoder().encode(options.body).length > 1048576) throw new Error('Too many review choices. Reduce the selection before continuing.');
                options.headers['Content-Type'] = 'application/json';
            }
            const response = await (method === 'GET' ? window.ApiUtils.apiFetch(path, options) : window.ApiUtils.apiFetchMutate(path, options));
            const raw = await bytes(response, maximum);
            if (binary && response.ok) return { ok: true, data: raw, status: response.status };
            let data = null;
            try { data = JSON.parse(new TextDecoder().decode(raw)); } catch (_) { /* Empty errors have a bounded generic message. */ }
            if (response.ok && (data === null || typeof data !== 'object')) throw new Error('The server response could not be confirmed. Check its status before trying again.');
            return { ok: response.ok, data, status: response.status };
        } catch (error) {
            if (error.name === 'AbortError') throw new Error('The request was interrupted or timed out. Check its status before trying again.');
            throw error;
        } finally { clearTimeout(timer); controllers.delete(controller); }
    }
    function requireOk(result) { if (!result.ok) throw new Error(errorText(result.data, result.status)); return result.data; }
    function query() {
        if (!Number.isSafeInteger(state.client) || state.client < 1 || !Number.isSafeInteger(state.template) || state.template < 1)
            throw new Error('The selected client and template must be confirmed before continuing.');
        return `client_id=${state.client}&template_id=${state.template}`;
    }
    function filePath(suffix) { return `/api/upload/file/${state.file}/${suffix}`; }
    function activeJob() { return !!state.handle && (!state.job || !terminal.has(state.job.state)); }
    function invalidate() {
        ++state.generation;
        controllers.forEach(controller => controller.abort());
        state.review = null; state.coverage = null; state.plan = null; state.receipt = null;
        reviewPanel.hidden = true; receiptPanel.hidden = true;
        $('nativeVarianceLabel').hidden = true; $('nativeConfirmVariance').checked = false;
        $('nativeImportConfirmation').checked = false;
        step(state.context ? 3 : state.template ? 2 : 1);
    }
    function controls() {
        const locked = state.busy || activeJob();
        ['templateDropdown', 'fileInput', 'uploadFileBtn', 'sheetSelector', 'clearBtn', 'resetBtn', 'saveMappingBtn', 'clearMappingBtn',
            'dateDelimiterDropdown', 'dateOrderDropdown', 'comparisonModeDropdown', 'sourceControlRow', 'sourceControlConfirmed',
            'uploadConfirmCoverage', 'uploadConfirmEmpty', 'nativeConfirmVariance'].forEach(id => { if ($(id)) $(id).disabled = locked; });
        ['uploadCoverageStart', 'uploadCoverageEnd'].forEach(id => $(id).disabled = locked || $('uploadConfirmCoverage').checked);
        $('uploadFileBtn').disabled = locked || !state.template;
        $('downloadBtn').disabled = state.busy || !state.columns.length;
        $('importBtn').disabled = locked || !state.file || !state.context || state.catalog.find(t => t.id === state.template)?.data_purpose === 'account_ledger';
        $('previewBtn').disabled = locked || !state.context;
        $('mappingBody').querySelectorAll('select').forEach(el => el.disabled = locked);
        $('rowExclusionBody').querySelectorAll('input').forEach(el => el.disabled = locked || $('comparisonModeDropdown').value === 'replace_range');
        $('nativeSubmit').disabled = locked || !state.review || !$('nativeImportConfirmation').checked;
        $('nativeCancel').disabled = state.busy || !activeJob();
        $('nativeResume').disabled = state.busy || state.job?.state !== 'authorization_required';
        $('nativeCheckJob').disabled = state.busy || !state.handle;
        $('nativeNewUpload').disabled = state.busy || (activeJob() && !state.lookupNotFound);
        $('nativeNewUpload').textContent = state.lookupNotFound ? 'Select source for a fresh review' : 'Start another upload';
        $('nativeCheckStaging').hidden = !state.staging || !!state.file;
    }
    async function guarded(target, action) {
        if (state.busy) return;
        state.busy = true; controls();
        const token = window.LoadingUI?.startGlobalLoading(target);
        try { await action(); }
        catch (error) { message(error.message, 'error'); }
        finally { state.busy = false; if (token) window.LoadingUI.stopGlobalLoading(token); controls(); }
    }
    function cellValue(cell) {
        // Numeric tokens remain strings; never pass financial evidence through Number().
        return cell?.numberToken ?? cell?.text ?? cell?.dateTime ?? cell?.time ?? (cell?.boolean === undefined || cell?.boolean === null ? '' : String(cell.boolean));
    }
    function table(parent, headers, rows) {
        const output = node('table'); const head = node('thead'); const line = node('tr');
        headers.forEach(label => line.append(node('th', label))); head.append(line); output.append(head);
        const body = node('tbody'); rows.forEach(values => {
            const tr = node('tr'); values.forEach(value => tr.append(node('td', String(value ?? ''), { class: 'native-upload-number' }))); body.append(tr);
        }); output.append(body); parent.append(output); return output;
    }
    function source(operation = 'rows', page = state.page) {
        if (!state.context) throw new Error('Prepare the current workbook first.');
        if (!Number.isSafeInteger(state.context.mappingRevision)) throw new Error('The mapping revision exceeds the browser limit. Configuration review is required.');
        return { clientId: state.client, templateId: state.template, sheet: state.sheet, operation,
            sha256: state.context.sha256, configurationKey: state.context.configurationKey, mappingRevision: state.context.mappingRevision,
            mappings: state.mappings.map(m => ({ columnId: m.columnId, sourcePosition: m.sourcePosition })),
            dateDelimiter: $('dateDelimiterDropdown').value, dateOrder: $('dateOrderDropdown').value,
            excludedRows: [...state.excluded].sort((a, b) => a - b), forceIncludeRows: [...state.forced].sort((a, b) => a - b),
            page, pageSize: 10, requestId: uuid() };
    }
    function coverage() {
        const row = $('sourceControlRow').value;
        if (row && (!/^\d+$/.test(row) || Number(row) < 2 || !Number.isSafeInteger(Number(row)))) throw new Error('Choose a valid Excel total row number.');
        const prior = state.coverage;
        return { footing: { validation: { source: source(), comparisonMode: $('comparisonModeDropdown').value, controlTotalRow: row ? Number(row) : null },
            controlTotalConfirmed: $('sourceControlConfirmed').checked,
            confirmVariance: $('nativeConfirmVariance').checked, expectedCheckKey: $('nativeConfirmVariance').checked ? prior?.footing?.checkKey : null },
            coverageStart: $('uploadCoverageStart').value || null, coverageEnd: $('uploadCoverageEnd').value || null,
            confirmReportCoverage: $('uploadConfirmCoverage').checked, confirmEmptyPeriod: $('uploadConfirmEmpty').checked,
            expectedCoverageKey: $('nativeConfirmVariance').checked ? prior?.coverageKey : null };
    }
    function clearSource() {
        invalidate(); state.file = null; state.staging = null; state.context = null; state.mappings = []; state.headers = [];
        state.excluded.clear(); state.forced.clear(); state.sheet = ''; state.page = 1;
        $('sheetSelector').replaceChildren(); $('mappingBody').replaceChildren(); $('rowExclusionBody').replaceChildren();
        ['mappingPanel', 'leftPanel', 'modeActions', 'sheetField', 'fileInfo', 'dateFormatGroup', 'rowExclusionContainer'].forEach(id => $(id).style.display = 'none');
        ['uploadCoverageStart', 'uploadCoverageEnd', 'sourceControlRow'].forEach(id => $(id).value = '');
        ['uploadConfirmCoverage', 'uploadConfirmEmpty', 'sourceControlConfirmed'].forEach(id => $(id).checked = false);
        $('dateFormatPopover').style.display = 'none'; detailPanel.hidden = true;
        step(state.template ? 2 : 1);
        if (!state.handle) remember(null);
    }
    async function selectTemplate() {
        clearSource(); state.template = Number($('templateDropdown').value) || null; state.columns = [];
        step(state.template ? 2 : 1);
        $('uploadFileGroup').style.display = state.template ? 'flex' : 'none';
        $('sheetDownloadGroup').style.display = state.template ? 'flex' : 'none';
        if (!state.template) return;
        const generation = state.generation;
        const data = requireOk(await request(`/api/templates/${state.template}/upload-columns`));
        if (generation !== state.generation) return;
        state.columns = data.columns || [];
        if (data.data_purpose === 'account_ledger') {
            message('Account Ledger import is not available in the C# app yet; the required balance proofs are still pending.', 'error'); return;
        }
        message('Choose the workbook. Its source rows will be previewed in bounded pages.');
    }
    async function stage(file) {
        if (!file || !state.template || activeJob()) return;
        if (!/\.(xlsx|xls)$/i.test(file.name) || file.size < 1 || file.size > window.AppNativeUpload.maximumFileBytes)
            throw new Error('Choose an XLS/XLSX workbook within the configured file-size limit.');
        clearSource(); const generation = state.generation;
        state.staging = uuid(); remember({ staging: state.staging, client: state.client, template: state.template });
        $('fileName').textContent = file.name; $('fileSize').textContent = `${Math.ceil(file.size / 1024)} KB`; $('fileInfo').style.display = 'flex';
        const data = new FormData(); data.append('file', file);
        // Staging has its own request header; no automatic multipart replay.
        const controller = new AbortController(); controllers.add(controller); const timer = setTimeout(() => controller.abort(), 130000);
        try {
            const response = await window.ApiUtils.apiFetchMutate(`/api/import-excel?${query()}`, {
                method: 'POST', body: data, headers: { 'X-Upload-Request': state.staging }, signal: controller.signal, credentials: 'same-origin'
            });
            const raw = await bytes(response, 1048576); const result = JSON.parse(new TextDecoder().decode(raw));
            if (generation !== state.generation) return;
            if (!response.ok) throw new Error(errorText(result, response.status));
            await received(result);
        } catch (error) {
            if (generation === state.generation) message('File receipt is uncertain. Use Check file receipt before selecting it again. ' + error.message, 'error');
        } finally { clearTimeout(timer); controllers.delete(controller); }
    }
    async function checkStaging() {
        const data = requireOk(await request(`/api/upload/staging/${state.staging}?${query()}`, { timeout: 30000 }));
        await received(data);
    }
    async function received(data) {
        if (data.state !== 'ready' || !Number.isSafeInteger(data.file_id) || !Array.isArray(data.sheets)) throw new Error('The workbook is not ready. Check its receipt again.');
        state.file = data.file_id; state.context = null;
        remember({ staging: state.staging, file: state.file, client: state.client, template: state.template });
        $('sheetSelector').replaceChildren();
        data.sheets.forEach(sheet => $('sheetSelector').append(node('option', sheet.name, { value: sheet.name })));
        state.sheet = data.sheets[0]?.name || ''; $('sheetField').style.display = 'flex';
        await prepare();
    }
    async function prepare() {
        invalidate(); const generation = state.generation; state.context = null; state.page = 1;
        state.excluded.clear(); state.forced.clear();
        $('mappingPanel').style.display = 'block'; $('leftPanel').style.display = 'block';
        const data = requireOk(await request(filePath('prepare'), { method: 'POST', body: {
            clientId: state.client, templateId: state.template, sheet: state.sheet, operation: 'map', requestId: uuid()
        } }));
        if (generation !== state.generation) return;
        state.context = data.context; state.mappings = data.mappings; state.headers = data.headers;
        step(3);
        renderMappings();
        ['modeActions', 'dateFormatGroup', 'rowExclusionContainer'].forEach(id => $(id).style.display = 'block');
        $('sourceControlPanel').hidden = !data.mappings.some(m => m.role === 'footing');
        message(data.warnings?.join(' ') || 'Review the mapped columns, total row and report coverage.');
        await rows(); await dates();
    }
    function renderMappings() {
        $('mappingBody').replaceChildren();
        state.mappings.forEach(mapping => {
            const tr = node('tr'); tr.append(node('td', mapping.name)); const td = node('td');
            const select = node('select', null, { 'aria-label': `${mapping.name}: Excel column`, 'data-column-id': mapping.columnId });
            select.append(node('option', '— None —', { value: '' }));
            state.headers.forEach(header => select.append(node('option', `${header.position}: ${header.label || '(blank header)'}`, { value: header.position })));
            select.value = mapping.sourcePosition ?? '';
            select.addEventListener('change', () => {
                invalidate(); mapping.sourcePosition = select.value ? Number(select.value) : null;
                state.excluded.clear(); state.forced.clear(); state.page = 1; controls();
                void guarded(select, async () => { await rows(); await dates(); });
            });
            td.append(select); tr.append(td, node('td', mapping.type), node('td', mapping.compare ? 'Compared' : 'Informational')); $('mappingBody').append(tr);
        });
        $('mappingCount').textContent = `${state.mappings.filter(m => m.sourcePosition !== null).length}/${state.mappings.length} columns mapped`;
    }
    async function rows() {
        if (!state.context) return; const generation = state.generation;
        const data = requireOk(await request(filePath('prepare'), { method: 'POST', body: source('rows') }));
        if (generation !== state.generation) return;
        state.pages = Math.max(1, Math.ceil(data.totalRows / 10));
        $('rowExclusionHeadRow').replaceChildren(node('th', 'Include'), node('th', 'Excel row'), ...state.headers.map(h => node('th', h.label || `Column ${h.position}`)));
        $('rowExclusionBody').replaceChildren();
        (data.rows || []).forEach(row => {
            const tr = node('tr'); const td = node('td'); const check = node('input', null, { type: 'checkbox', 'aria-label': `Include Excel row ${row.number}` });
            check.checked = !state.excluded.has(row.number);
            check.addEventListener('change', () => {
                invalidate();
                if (check.checked) state.excluded.delete(row.number);
                else if (state.excluded.size < 10000) { state.excluded.add(row.number); state.forced.delete(row.number); }
                else { check.checked = true; message('At most 10,000 explicit row choices are supported.', 'error'); }
                controls(); void guarded(check, rows);
            }); td.append(check); tr.append(td, node('td', String(row.number) + (row.flagged ? ' ⚠' : '')));
            state.headers.forEach(header => tr.append(node('td', cellValue(row.cells.find(c => c.column === header.position)), { class: 'native-upload-number' })));
            $('rowExclusionBody').append(tr);
        });
        $('rowExclusionCounter').textContent = `${data.selectedRows} of ${data.totalRows} physical rows selected; validation determines eligible detail rows`;
        $('rowExclusionPageInfo').textContent = `Page ${state.page} of ${state.pages}`;
        $('rowExclusionRange').textContent = `${data.totalRows} source rows; total rows and notes are classified separately`;
        controls();
    }
    async function dates() {
        if (!state.context) return; const generation = state.generation;
        const data = requireOk(await request(filePath('prepare'), { method: 'POST', body: source('dates') }));
        if (generation !== state.generation) return;
        $('dateFormatPreview').replaceChildren(...(data.samples || []).map(sample => node('span', `Row ${sample.row}: ${cellValue(sample.source)} → ${sample.parsed || sample.error || 'blank'}`)));
        $('dateFormatPreview').style.display = 'flex';
        updateDateFormatLabel();
        $('documentDateSuggestion').textContent = data.dateSuggestion?.status === 'await_footing_validation'
            ? 'Validated transaction-date suggestions are available after checking Footing.'
            : `Suggested source dates: ${data.dateSuggestion?.start || 'unavailable'} to ${data.dateSuggestion?.end || 'unavailable'}. Confirm the full report period separately.`;
    }
    function updateDateFormatLabel() {
        const delimiter = $('dateDelimiterDropdown').value;
        $('dateFormatTriggerLabel').textContent = `${$('dateOrderDropdown').value} (${delimiter || 'none'})`;
    }
    function markDatePreviewStale() {
        const preview = $('dateFormatPreview');
        preview.replaceChildren(node('span', 'Format changed. Select “Preview dates” to check it.'));
        preview.style.display = 'flex';
    }
    function showCoverage(output) {
        if (!output) return; state.coverage = output;
        const container = $('sourceControlResults'); container.replaceChildren();
        const footing = output.footing;
        if (footing) {
            table(container, ['Field', 'Excel total', 'Detail total', 'Difference', 'Result'], footing.checks.map(c => [c.column, c.excelTotal, c.detailTotal, c.difference, c.status]));
            container.append(node('p', `Full source: ${footing.fullSheet.rowCount} detail rows / ${footing.fullSheet.documentCount} documents. Selected: ${footing.selectedDetail.rowCount} detail rows / ${footing.selectedDetail.documentCount} documents.`));
            const counts = footing.inputValidation.counts;
            container.append(node('p', `Input errors: ${counts.errorRows}; rejected: ${counts.rejectedRows}; eligible: ${counts.eligibleRows}.`));
        }
        const suggestion = output.dateSuggestion;
        if (suggestion?.start && suggestion?.end) $('documentDateSuggestion').textContent = `Checked transaction dates: ${suggestion.start} to ${suggestion.end}. Confirm the full ERP report period separately.`;
        $('nativeVarianceLabel').hidden = !footing?.checks.some(c => c.status === 'confirm' || c.status === 'confirmation_required' || c.status === 'acknowledgement_required');
        // Server statuses vary by boundary; only non-passing differences <=1 can offer an acknowledgement.
        if (footing && !footing.footingPassed && footing.checks.some(c => c.status.includes('confirm') || c.status.includes('acknowledg'))) $('nativeVarianceLabel').hidden = false;
        const issues = [...(footing?.inputValidation?.issues || []).map(i => `Excel row ${i.row}: ${[...i.errors, ...i.missing].map(e => e.message).join(' ')}`),
            ...(footing?.issues || []).map(i => `Excel row ${i.row}: ${i.message}`), ...(output.issues || []).map(i => `Excel row ${i.row}: ${i.message}`)];
        renderDetails('Validation details', issues, footing?.inputValidation?.detailsTruncated || footing?.detailsTruncated || output.detailsTruncated);
    }
    function renderDetails(title, lines, truncated = false) {
        $('nativeDetailsTitle').textContent = title; $('nativeDetailsContent').replaceChildren();
        lines.slice(0, 100).forEach(line => $('nativeDetailsContent').append(node('p', line)));
        if (truncated) $('nativeDetailsContent').append(node('p', 'More issues exist. Use the paged input diagnostics; counts remain authoritative.'));
        detailPanel.hidden = !lines.length && !truncated;
    }
    async function validate() {
        const generation = state.generation;
        message('Checking source, Footing, coverage and stored data…');
        state.review = null; state.plan = null; reviewPanel.hidden = true;
        const input = coverage();
        const checked = await request(filePath('coverage'), { method: 'POST', body: input });
        if (generation !== state.generation) return;
        showCoverage(checked.ok ? checked.data : checked.data?.result);
        if (!checked.ok) throw new Error(errorText(checked.data, checked.status));
        if (!checked.data.coverageChecksPassed || !checked.data.footing.footingPassed) throw new Error('Resolve the input, Footing and coverage checks before continuing.');
        const review = { coverage: input, page: 1, pageSize: 50 };
        const corrected = await request(filePath('corrections'), { method: 'POST', body: review });
        if (generation !== state.generation) return;
        const output = corrected.data?.counts ? corrected.data : corrected.data?.result;
        if (output) {
            renderDetails('Correction review', (output.issues || []).map(i => `${i.kind}: ${i.documentGroup} (${i.fields.join(', ')})`), output.detailsTruncated);
        }
        if (!corrected.ok) throw new Error(errorText(corrected.data, corrected.status));
        if (!output?.correctionChecksPassed) throw new Error('Stored data requires review before this upload can continue.');
        review.expectedCorrectionKey = output.correctionKey;
        state.review = structuredClone(review); state.correction = output;
        if (!output.noOp) {
            const plan = requireOk(await request(filePath('validation-plan'), { method: 'POST', body: { requestId: uuid(), review: state.review } }));
            if (generation !== state.generation) return;
            if (plan.status !== 'ready' || !plan.planId || !plan.signature || plan.correctionKey !== output.correctionKey) throw new Error('The reviewed data changed. Validate again.');
            state.plan = plan;
        }
        renderReview(); message('Review the counts and scope, then confirm the upload.');
    }
    function renderReview() {
        const result = state.correction;
        $('nativeReviewScope').textContent = `Client ${state.client} · ${state.catalog.find(t => t.id === state.template)?.template_name || state.template} · ${state.sheet}`;
        $('nativeReviewPeriod').textContent = $('comparisonModeDropdown').value === 'replace_range'
            ? `Complete report: ${$('uploadCoverageStart').value} to ${$('uploadCoverageEnd').value}. Removed rows retain history.`
            : 'Partial extract: absent rows are retained. This does not certify a complete period.';
        $('nativeReviewCounts').replaceChildren();
        table($('nativeReviewCounts'), ['Result', 'Rows'], Object.entries(result.counts).map(([key, value]) => [countLabel(key), value]));
        $('nativeReviewActions').replaceChildren();
        table($('nativeReviewActions'), ['Action', 'Excel row', 'Stored row', 'Changed fields'], result.rows.map(r => [r.action, r.row, r.existingId, r.changedFields.join(', ')]));
        $('nativeReviewPage').textContent = `Showing ${result.rows.length} of ${result.totalRows} actions. Counts cover the full review.`;
        $('nativeNextActions').disabled = result.page * result.pageSize >= result.totalRows;
        $('nativeSubmit').textContent = result.noOp ? 'Verify unchanged data' : 'Import reviewed data';
        $('nativeImportConfirmation').checked = false; reviewPanel.hidden = false; step(4);
        reviewPanel.scrollIntoView({ block: 'start' });
    }
    async function submit() {
        if (!state.review || !$('nativeImportConfirmation').checked) return;
        if (state.plan) {
            try { requireOk(await request(filePath('validation-plan/check'), { method: 'POST', body: {
                planId: state.plan.planId, signature: state.plan.signature, review: state.review
            } })); } catch (error) { invalidate(); throw error; }
        }
        const input = { requestId: uuid(), kind: state.correction.noOp ? 'no_changes' : 'import', review: state.review,
            planId: state.plan?.planId || null, signature: state.plan?.signature || null, format: 1 };
        state.pending = input;
        state.handle = { client: state.client, template: state.template, file: state.file, job: input.requestId };
        state.job = null; state.lookupNotFound = false; remember(state.handle); renderJob(); controls();
        jobPanel.scrollIntoView({ block: 'start' });
        // The handle is saved before POST. Lost acknowledgement is resolved with GET.
        try {
            const result = await request(filePath('jobs'), { method: 'POST', body: input, timeout: 35000 });
            if (result.ok && result.data?.job) acceptJob(result.data.job);
            else if (result.status === 400) {
                state.handle = null; state.pending = null; remember(null); jobPanel.hidden = true; invalidate();
                message(errorText(result.data, result.status), 'error');
            } else { message(errorText(result.data, result.status), 'error'); await checkJob(); }
        } catch (error) { message('Submission is uncertain. Checking the original job. ' + error.message, 'error'); await checkJob(); }
        finally { schedule(); }
    }
    function remember(handle) {
        try { if (handle) sessionStorage.setItem(cacheKey, JSON.stringify(handle)); else sessionStorage.removeItem(cacheKey); } catch (_) { /* URL job handles still allow explicit recovery. */ }
        if (handle?.job) {
            const url = new URL(location.href); url.searchParams.set('upload_file', handle.file); url.searchParams.set('upload_job', handle.job);
            url.searchParams.set('upload_client', handle.client); url.searchParams.set('upload_template', handle.template); history.replaceState(null, '', url);
        } else if (!handle) {
            const url = new URL(location.href); ['upload_file', 'upload_job', 'upload_client', 'upload_template'].forEach(k => url.searchParams.delete(k)); history.replaceState(null, '', url);
        }
    }
    function acceptJob(job) {
        if (!job || job.fileId !== state.file || !/^[0-9a-f-]{36}$/i.test(job.id)) throw new Error('The server returned an invalid job reference.');
        if (state.job?.id === job.id && state.job.state === 'succeeded' && job.state !== 'succeeded') return;
        if (state.job?.id === job.id && terminal.has(state.job.state) && !terminal.has(job.state)) return;
        state.job = job; state.handle.job = job.id; state.pollFailures = 0; state.pending = null; remember(state.handle); renderJob();
        state.lookupNotFound = false;
        if (terminal.has(job.state)) { state.review = null; state.plan = null; reviewPanel.hidden = true; }
        if (job.state === 'succeeded') { reviewPanel.hidden = true; void loadReceipt(); }
    }
    async function checkJob() {
        if (!state.handle) return;
        const handle = state.handle; const sequence = state.jobSequence = (state.jobSequence || 0) + 1;
        const result = await request(filePath(`jobs/${handle.job}`), { timeout: 15000 });
        if (state.handle !== handle || state.jobSequence !== sequence) return;
        if (!result.ok) {
            state.lookupNotFound = result.status === 404;
            state.pollFailures++;
            message(errorText(result.data, result.status), 'error'); renderJob();
            if ([401, 403, 404, 409].includes(result.status)) state.pollFailures = 3;
            return;
        }
        acceptJob(result.data.job); schedule();
    }
    function schedule() {
        clearTimeout(state.timer);
        if (!activeJob() || state.job?.state === 'authorization_required' || state.pollFailures >= 3 || document.hidden) return;
        state.timer = setTimeout(async () => {
            if (state.busy) { schedule(); return; }
            try { await checkJob(); } catch (error) { state.pollFailures++; message(error.message + ' Use Check status to reconnect.', 'error'); }
            finally { controls(); schedule(); }
        }, Math.min(10000, 2000 * (state.pollFailures + 1)));
    }
    function renderJob() {
        jobPanel.hidden = !state.handle;
        if (state.handle) step(4);
        $('nativeJobState').textContent = state.job ? `${state.job.state.replaceAll('_', ' ')} · ${state.job.stage.replaceAll('_', ' ')}` : 'Submission not yet confirmed';
        $('nativeJobAttempts').textContent = state.job ? `Attempt ${state.job.attempts} of ${state.job.maximumAttempts} · expires ${displayTime(state.job.expiresAt)}` : 'Check the original job before submitting any other import.';
        $('nativeJobSource').textContent = state.job?.sourceReady
            ? (terminal.has(state.job.state) ? 'Private source persistence was verified for this job; temporary source retention remains bounded.' : 'Private source persistence verified. The job can recover its source after restart within its expiry.')
            : 'Private source persistence is not yet verified. An interruption may require selecting the source again.';
        $('nativeJobCode').textContent = state.job?.code ? errorText({ code: state.job.code }, 409) : '';
        $('nativeRetrySubmission').hidden = !state.pending || !!state.job;
        controls();
    }
    async function jobAction(action) {
        const handle = state.handle; state.jobSequence = (state.jobSequence || 0) + 1;
        const data = requireOk(await request(filePath(`jobs/${handle.job}/${action}`), { method: 'POST', body: {}, timeout: 35000 }));
        if (state.handle !== handle) return;
        acceptJob(data.job); schedule();
    }
    async function loadReceipt() {
        const handle = state.handle; const receiptId = state.job?.completion?.receiptId; if (!receiptId) return;
        try {
            const result = requireOk(await request(filePath(`receipt/${receiptId}`), { timeout: 30000 }));
            if (handle !== state.handle) return;
            state.receipt = result.receipt; const receipt = state.receipt;
            $('nativeReceiptSummary').textContent = `Completed ${displayTime(receipt.completedAt)} · ${receipt.kind.replaceAll('_', ' ')} · reconciliation: ${receipt.reconciliation.status.replaceAll('_', ' ')}. ${receipt.reconciliation.message}`;
            $('nativeReceiptMetrics').replaceChildren();
            table($('nativeReceiptMetrics'), ['Completion result', 'Rows'], Object.entries(receipt.counts).map(([key, value]) => [countLabel(key), value]));
            table($('nativeReceiptMetrics'), ['Control', 'Source', 'Stored', 'Difference', 'Matches'], receipt.reconciliation.metrics.map(m => [m.label, m.source, m.stored, m.difference, m.matches === null ? 'Unavailable' : m.matches ? 'Yes' : 'No']));
            $('nativeReceiptWarning').textContent = `This is completion-time evidence. Detail export expires ${displayTime(receipt.evidenceExpiresAt)} and can be refused if the source or stored evidence changed. It is not audit approval.`;
            receiptPanel.hidden = false;
            message(`Job completed. Reconciliation: ${receipt.reconciliation.status}.`, receipt.reconciliation.status.includes('mismatch') || receipt.reconciliation.status === 'unavailable' ? 'error' : 'success');
        } catch (error) { if (handle === state.handle) message('The job completed; its receipt could not be loaded. ' + error.message, 'error'); }
    }
    function download(data, name, type) {
        const url = URL.createObjectURL(new Blob([data], { type })); const anchor = node('a', null, { href: url, download: name });
        document.body.append(anchor); anchor.click(); anchor.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000);
    }
    async function downloadReceipt() {
        const data = requireOk(await request(filePath(`receipt/${state.receipt.id}/workbook`), { binary: true, maximum: 8388608, timeout: 65000 }));
        download(data, `upload-receipt-${state.receipt.id}.xlsx`, 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet');
    }
    async function mappingSave(clear = false) {
        const snapshot = requireOk(await request(`/api/upload/saved-mapping?${query()}`));
        if (!Number.isSafeInteger(snapshot.mapping_revision)) throw new Error('The saved mapping revision exceeds the browser limit. Configuration review is required.');
        if (clear) {
            // Use the existing guarded revision protocol without automatic write retry.
            requireOk(await request(`/api/upload/saved-mapping?${query()}`, { method: 'DELETE', timeout: 30000, headers: {
                'X-Mapping-Revision': String(snapshot.mapping_revision), 'X-Template-Revision': snapshot.configuration_key
            } }));
        } else {
            const mappings = state.mappings.filter(m => m.sourcePosition !== null).map(m => {
                const header = state.headers.find(h => h.position === m.sourcePosition);
                if (!header || header.ambiguous) throw new Error('Blank or duplicate source names cannot be saved by name. Keep explicit positions for this upload.');
                return { template_column_id: m.columnId, excel_column: header.label };
            });
            if (!mappings.length) throw new Error('Map at least one source column before saving.');
            requireOk(await request('/api/upload/save-mapping', { method: 'POST', body: { client_id: state.client, template_id: state.template,
                mappings, mapping_revision: snapshot.mapping_revision, configuration_key: snapshot.configuration_key } }));
        }
        await prepare(); message(clear ? 'Saved mapping cleared. Review the prepared columns.' : 'Mapping saved. Review the prepared columns.');
    }
    async function inputIssues(next = false) {
        if (next) ++state.issuePage; else state.issuePage = 1;
        const result = requireOk(await request(filePath('validate-input'), { method: 'POST', body: { ...coverage().footing.validation, issuePage: state.issuePage, issuePageSize: 50 } }));
        const output = result.result;
        renderDetails(`Input diagnostics · page ${output.issuePage} · ${output.issueRows} issue rows`, output.issues.map(i => `Excel row ${i.row}: ${[...i.errors, ...i.missing].map(e => `${e.column || ''} ${e.message}`).join(' ')}`), output.detailsTruncated);
        $('nativeNextIssues').disabled = output.issuePage * output.issuePageSize >= output.issueRows;
    }
    async function overrideRow() {
        const row = Number($('nativeForceRow').value);
        if (!Number.isSafeInteger(row) || row < 2 || row > 1048576 || $('comparisonModeDropdown').value !== 'correction') throw new Error('Blank-field exceptions require a valid Excel row in a Partial extract. Source errors cannot be overridden.');
        if (!state.forced.has(row) && state.forced.size >= 10000) throw new Error('At most 10,000 explicit row choices are supported.');
        invalidate(); state.excluded.delete(row); state.forced.has(row) ? state.forced.delete(row) : state.forced.add(row);
        await rows(); message(`Requested blank-field exceptions: ${[...state.forced].join(', ') || 'none'}. Validation remains authoritative.`);
    }
    function buildPanels() {
        document.body.classList.add('native-upload');
        const status = panel('nativeUploadStatus', 'Upload status'); notice = node('p', '', { id: 'nativeUploadNotice', role: 'status', 'aria-live': 'polite' }); status.append(notice);
        button('nativeCheckStaging', 'Check file receipt', checkStaging, status).hidden = true;
        const variance = node('label', null, { id: 'nativeVarianceLabel', hidden: '' });
        variance.append(node('input', null, { id: 'nativeConfirmVariance', type: 'checkbox' }), document.createTextNode(' Acknowledge this checked Footing variance')); status.append(variance);
        detailPanel = panel('nativeUploadDetails', 'Validation details'); detailPanel.querySelector('h2').id = 'nativeDetailsTitle';
        detailPanel.append(node('div', null, { id: 'nativeDetailsContent', class: 'native-upload-scroll' }));
        button('nativeInputIssues', 'Input diagnostics', () => inputIssues(), detailPanel);
        button('nativeNextIssues', 'Next diagnostic page', () => inputIssues(true), detailPanel);
        const force = node('label', 'Excel row for blank-field exception '); force.append(node('input', null, { id: 'nativeForceRow', type: 'number', min: '2', max: '1048576' })); detailPanel.append(force);
        button('nativeOverride', 'Toggle requested exception', overrideRow, detailPanel); detailPanel.hidden = true;
        reviewPanel = panel('nativeUploadReview', 'Review upload');
        reviewPanel.append(node('p', '', { id: 'nativeReviewScope' }), node('p', '', { id: 'nativeReviewPeriod' }), node('div', null, { id: 'nativeReviewCounts' }),
            node('div', null, { id: 'nativeReviewActions', class: 'native-upload-scroll' }), node('p', '', { id: 'nativeReviewPage' }));
        button('nativeNextActions', 'Next action page', async () => {
            if (!state.review) return;
            const output = requireOk(await request(filePath('corrections'), { method: 'POST', body: { ...state.review, page: state.correction.page + 1 } }));
            if (output.correctionKey !== state.correction.correctionKey) throw new Error('The review changed. Validate again.');
            state.correction = output; renderReview(); $('nativeNextActions').disabled = output.page * output.pageSize >= output.totalRows;
        }, reviewPanel);
        const confirmation = node('label', null); confirmation.append(node('input', null, { id: 'nativeImportConfirmation', type: 'checkbox' }), document.createTextNode(' I reviewed these counts, choices and scope.'));
        reviewPanel.append(confirmation); button('nativeSubmit', 'Import reviewed data', submit, reviewPanel, true); reviewPanel.hidden = true;
        jobPanel = panel('nativeUploadJob', 'Upload job');
        ['nativeJobState', 'nativeJobAttempts', 'nativeJobSource', 'nativeJobCode'].forEach(id => jobPanel.append(node('p', '', { id, role: id === 'nativeJobState' ? 'status' : 'note' })));
        button('nativeCheckJob', 'Check status', async () => { state.pollFailures = 0; await checkJob(); }, jobPanel);
        button('nativeCancel', 'Cancel job', () => jobAction('cancel'), jobPanel);
        button('nativeResume', 'Resume as owner', () => jobAction('resume'), jobPanel);
        button('nativeRetrySubmission', 'Retry same submission', async () => {
            // Explicit recovery only; immutable ID/options are retained in memory.
            const result = requireOk(await request(filePath('jobs'), { method: 'POST', body: state.pending, timeout: 35000 })); acceptJob(result.job); schedule();
        }, jobPanel);
        button('nativeNewUpload', 'Start another upload', async () => { clearTimeout(state.timer); state.handle = null; state.job = null; state.pending = null; state.lookupNotFound = false; remember(null); jobPanel.hidden = true; clearSource(); }, jobPanel);
        jobPanel.hidden = true;
        receiptPanel = panel('nativeUploadReceipt', 'Completion evidence');
        receiptPanel.append(node('p', '', { id: 'nativeReceiptSummary' }), node('div', null, { id: 'nativeReceiptMetrics' }), node('p', '', { id: 'nativeReceiptWarning' }));
        button('nativeDownloadReceipt', 'Download evidence workbook', downloadReceipt, receiptPanel);
        button('nativeReloadReceipt', 'Reload receipt', loadReceipt, receiptPanel); receiptPanel.hidden = true;
    }
    async function init() {
        buildPanels();
        $('importBtn').textContent = 'Validate'; $('previewBtn').textContent = 'Refresh source rows';
        $('previewSheet').hidden = true; $('uploadReplacementReview').hidden = true;
        $('goToDataBankBtn').hidden = !window.AppNativeDataBank?.enabled;
        $('goToDataBankBtn').addEventListener('click', () => { if (window.AppNativeDataBank?.enabled) location.href = '/static/databank.html'; });
        $('uploadBackupHelp').textContent = 'Current/history preservation and completion evidence are handled by the native import. No external archive is created.';
        $('ledgerUploadContext').hidden = true;
        $('clientSelectionWarning').style.display = 'none';
        ['uploadCoverageStart', 'uploadCoverageEnd'].forEach(id => { $(id).type = 'date'; $(id).removeAttribute('placeholder'); });
        $('dateOrderDropdown').replaceChildren(...['DMY', 'MDY', 'YMD'].map(order => node('option', order, { value: order })));
        button('nativePreviewDateFormat', 'Preview dates', dates, $('dateFormatPopover'));
        if (!window.AppNativeUpload?.enabled) {
            $('nativeCheckStaging').hidden = true;
            document.querySelectorAll('#mainUploadPage input, #mainUploadPage select, #mainUploadPage button').forEach(el => el.disabled = true);
            message('Uploads are not enabled in the C# app yet. Contact your administrator when this feature is ready.'); return;
        }
        if (window.clientContextReady) {
            let timeout;
            await Promise.race([
                window.clientContextReady,
                new Promise(resolve => { timeout = setTimeout(resolve, 10000); })
            ]);
            clearTimeout(timeout);
        }
        let selectedClientId = window.getSelectedClientId?.();
        if ((!Number.isSafeInteger(Number(selectedClientId)) || Number(selectedClientId) < 1)
            && typeof window.refreshSelectedClientContext === 'function') {
            selectedClientId = await window.refreshSelectedClientContext();
        }
        state.client = Number(selectedClientId);
        if (!Number.isSafeInteger(state.client) || state.client < 1) { message('Select a client before uploading.', 'error'); return; }
        $('clientSelectionWarning').style.display = 'none';
        const catalog = requireOk(await request('/api/templates', { maximum: 2097152 }));
        if (!Array.isArray(catalog)) throw new Error('Template metadata is unavailable.'); state.catalog = catalog;
        $('templateDropdown').replaceChildren(node('option', '-- Select Template --', { value: '' }), ...catalog.map(t => node('option', t.template_name, { value: t.id })));
        const events = { templateDropdown: ['change', selectTemplate], fileInput: ['change', async () => { const file = $('fileInput').files[0]; $('fileInput').value = ''; await stage(file); }],
            clearBtn: ['click', async () => { clearSource(); message('Source selection cleared.'); }],
            resetBtn: ['click', async () => { clearSource(); message('Review reset.'); }],
            sheetSelector: ['change', async () => { state.sheet = $('sheetSelector').value; await prepare(); }], importBtn: ['click', validate],
            saveMappingBtn: ['click', () => mappingSave()], clearMappingBtn: ['click', () => mappingSave(true)], previewBtn: ['click', rows],
            downloadBtn: ['click', async () => {
                const book = XLSX.utils.book_new(); const sheet = XLSX.utils.aoa_to_sheet([state.columns.map(c => c.column_name)]);
                XLSX.utils.book_append_sheet(book, sheet, 'Template');
                download(XLSX.write(book, { bookType: 'xlsx', type: 'array' }), `template-${state.template}.xlsx`, 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet');
            }], dateFormatTriggerBtn: ['click', async () => $('dateFormatPopover').style.display = $('dateFormatPopover').style.display === 'none' ? 'block' : 'none'] };
        Object.entries(events).forEach(([id, [event, action]]) => $(id).addEventListener(event, () => void guarded($(id), action)));
        // Open the picker before guarded() marks the page busy and disables the
        // hidden input. Browsers ignore programmatic clicks on disabled inputs.
        $('uploadFileBtn').addEventListener('click', () => {
            if (state.busy || activeJob() || !state.template) return;
            $('fileInput').click();
        });
        ['dateDelimiterDropdown', 'dateOrderDropdown', 'comparisonModeDropdown', 'sourceControlRow', 'sourceControlConfirmed', 'uploadCoverageStart', 'uploadCoverageEnd', 'uploadConfirmCoverage', 'uploadConfirmEmpty'].forEach(id => {
            $(id).addEventListener('change', () => {
                invalidate();
                if (id === 'dateDelimiterDropdown' || id === 'dateOrderDropdown') {
                    updateDateFormatLabel();
                    markDatePreviewStale();
                    controls();
                    return;
                }
                if (id === 'comparisonModeDropdown') { state.excluded.clear(); state.forced.clear(); $('uploadConfirmCoverage').checked = false; $('uploadConfirmEmpty').checked = false; }
                if (id === 'sourceControlRow') $('sourceControlConfirmed').checked = false;
                controls();
                if (state.context && id === 'comparisonModeDropdown') void guarded($(id), async () => { await rows(); await dates(); });
            });
        });
        ['uploadCoverageStart', 'uploadCoverageEnd', 'sourceControlRow'].forEach(id => $(id).addEventListener('input', () => { invalidate(); if (id === 'sourceControlRow') $('sourceControlConfirmed').checked = false; controls(); }));
        $('nativeImportConfirmation').addEventListener('change', controls);
        $('nativeConfirmVariance').addEventListener('change', () => { state.review = null; state.plan = null; reviewPanel.hidden = true; controls(); });
        [['rowExclusionFirstPage', () => 1], ['rowExclusionPrevPage', () => Math.max(1, state.page - 1)], ['rowExclusionNextPage', () => Math.min(state.pages, state.page + 1)], ['rowExclusionLastPage', () => state.pages]].forEach(([id, page]) => $(id).addEventListener('click', () => void guarded($(id), async () => { state.page = page(); await rows(); })));
        let handle; const url = new URL(location.href);
        if (url.searchParams.has('upload_job')) handle = { job: url.searchParams.get('upload_job'), file: Number(url.searchParams.get('upload_file')), client: Number(url.searchParams.get('upload_client')), template: Number(url.searchParams.get('upload_template')) };
        else try { handle = JSON.parse(sessionStorage.getItem(cacheKey) || 'null'); } catch (_) {}
        if (handle && handle.client === state.client && Number.isSafeInteger(handle.template) && catalog.some(t => t.id === handle.template)) {
            state.template = handle.template; $('templateDropdown').value = String(handle.template);
            const metadata = requireOk(await request(`/api/templates/${state.template}/upload-columns`));
            state.columns = metadata.columns || []; $('sheetDownloadGroup').style.display = 'flex';
            if (/^[0-9a-f-]{36}$/i.test(handle.job || '') && Number.isSafeInteger(handle.file) && handle.file > 0) {
                state.file = handle.file; state.handle = handle; renderJob(); await checkJob();
            } else if (/^[0-9a-f-]{36}$/i.test(handle.staging || '')) { state.staging = handle.staging; message('An earlier file receipt is available. Check it before selecting the workbook again.'); }
        } else message('Select a template to begin a native upload.');
        document.addEventListener('visibilitychange', () => { if (document.hidden) clearTimeout(state.timer); else schedule(); });
        controls();
    }
    init().catch(error => { if (notice) message(error.message, 'error'); });
})();
