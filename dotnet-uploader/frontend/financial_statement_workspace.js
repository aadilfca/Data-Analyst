/* Shared Financial Statement and Notes controls. Hosts supply account context and refresh callbacks. */
(function () {
'use strict';
function create(host) {
    host.state = host.state || {categories: [], calculations: [], editor: null, notes: [], financialLoaded: false, notesLoaded: false,
        dragging: null, noteDragging: null, noteAnchorDragging: null, profitLink: null};
    if (!document.getElementById('accFinancialSetupModal')) document.body.insertAdjacentHTML('beforeend', MODAL);
    const accElement = id => document.getElementById(id);
    const accEscape = value => String(value == null ? '' : value).replace(/[&<>'"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'})[c]);
    const accNormaliseCode = AccountAssignments.normaliseCode;
    const accAssignment = account => account.assignment || {};
    const accApi = (...args) => host.api(...args);
    const accLeavesBelow = code => host.leavesBelow(code);
    const accSelectedUploadPayload = () => host.uploadPayload();
    const accLoadMapping = (...args) => host.refreshMapping(...args);
    const accRenderFinancialRows = () => host.renderFinancialRows();
    const accRenderNotesRows = () => host.renderNotesRows();
    const accClearStatus = () => host.clearStatus();
    const accHideStatus = element => { if(element) element.hidden=true; };
    const accShowStatus = (message, type) => {
        const modal=accElement('accFinancialSetupModal');
        if(message && type !== 'loading' && host.showStatus) { host.showStatus(message,type||'info'); return; }
        if(modal.hidden) { if(message)host.showStatus(message,type||'info'); return; }
        let notice=modal.querySelector('[data-acc-popup-notice]');
        if(!notice) { notice=document.createElement('div'); notice.dataset.accPopupNotice=''; modal.querySelector('.acc2-modal-header').after(notice); }
        notice.className='acc2-status acc2-modal-notice '+(type||'');notice.textContent=message||'';notice.hidden=!message;
        if(message)notice.setAttribute('role',type==='error'?'alert':'status');
        window.clearTimeout(noticeTimer);
        if(message)noticeTimer=window.setTimeout(()=>{notice.hidden=true;},4200);
    };
    const accWithBusy = (message, operation) => host.withBusy(message,operation);
    const accWithTableBusy = (_id,message,operation) => accWithBusy(message,operation);
    const accSetSelectBusy = (select,busy) => { if(!select)return;select.disabled=busy||!host.isAdmin;const trigger=select.closest('[data-fs-picker]')?.querySelector('[data-fs-picker-trigger]');if(trigger)trigger.disabled=select.disabled; };
    const accSetInlineBusy = (select,result) => {accSetSelectBusy(select,true);if(result)result.textContent='Saving…';};
    const accSetInlineResult = (select,result,saved) => {accSetSelectBusy(select,false);if(result)result.textContent=saved?'Saved':'Retry';};
    const accWithButtonBusy = async (button,message,operation) => {const was=button.disabled;button.disabled=true;try{return await accWithBusy(message,operation);}finally{button.disabled=was;}};
    let returnFocus=null, previousOverflow='', noticeTimer=null, setupContext=null, setupLoading=null;
    let optionCategories=null, optionNotes=null;
    const noteOptionsCache=new Map();

    // TB keeps closed pickers small; only the open menu needs its complete options.
    function lazyPickerMarkup(account,selectedId,kind,statementCategoryId,options) {
        const note=kind==='note', key=String(account.account_code||'').replace(/[^a-zA-Z0-9_-]/g,'_');
        const id=(note?'accNotePicker_':'accFsPicker_')+key;
        if(note)selectedId=accEffectiveNoteSelection(selectedId,options);
        const label=note?(selectedId==='__mixed'?accMixedPickerLabel(account,'note'):accNotePickerLabel(statementCategoryId,selectedId,options)):(selectedId==='__mixed'?accMixedPickerLabel(account,'financial'):accFinancialPickerLabel(selectedId));
        const details=accPickerDetails(account,kind,selectedId,statementCategoryId,options||[]);
        const disabled=!host.isAdmin || (note && (!statementCategoryId || statementCategoryId==='__mixed' || !options.length));
        return '<div class="acc2-fs-picker'+(note?' acc2-note-picker':'')+'" data-fs-picker data-fs-picker-lazy="'+kind+'">'
            +'<button type="button" class="acc2-fs-picker-trigger'+(label==='Unassigned'?' is-unassigned':'')+'" data-fs-picker-trigger role="combobox" aria-haspopup="listbox" aria-expanded="false" aria-controls="'+id+'_menu" title="'+accEscape(details)+'" aria-label="'+(note?'Note':'Financial Statement category')+' for account '+accEscape(account.account_code)+': '+accEscape(details)+'"'+(disabled?' disabled':'')+'><span data-fs-picker-value-label>'+accEscape(label)+'</span><span class="acc2-fs-picker-chevron" aria-hidden="true">▾</span></button>'
            +'<div class="acc2-fs-picker-menu" id="'+id+'_menu" data-fs-picker-menu hidden></div>'
            +'<select class="'+(note?'acc2-note-category':'acc2-financial-category')+'" data-acc-code="'+accEscape(account.account_code)+'" data-acc-parent="'+(!account.is_leaf)+'" aria-hidden="true" tabindex="-1" hidden'+(disabled?' disabled':'')+'><option selected value="'+accEscape(selectedId||'')+'">'+accEscape(label)+'</option></select></div>';
    }
    function accEffectiveFinancialCategory(account) {
        const direct = accAssignment(account).financial_statement_category_id;
        if (account.is_leaf) return direct == null ? '' : String(direct);
        const values = Array.from(new Set(accLeavesBelow(account.account_code).map(function (leaf) {
            const value = accAssignment(leaf).financial_statement_category_id;
            return value == null ? '' : String(value);
        })));
        return values.length === 1 ? values[0] : (values.length > 1 ? '__mixed' : '');
    }

    function accEffectiveNoteCategory(account) {
        const direct = accAssignment(account).note_category_id;
        if (account.is_leaf) return direct == null ? '' : String(direct);
        const values = Array.from(new Set(accLeavesBelow(account.account_code).map(function (leaf) {
            const value = accAssignment(leaf).note_category_id;
            return value == null ? '' : String(value);
        })));
        return values.length === 1 ? values[0] : (values.length > 1 ? '__mixed' : '');
    }

    function accFinancialCategoryPath(categoryId) {
        const category = host.state.categories.find(function (item) { return Number(item.id) === Number(categoryId) && item.row_type !== 'heading'; });
        if (!category) return 'Unassigned';
        const labels = [];
        const visited = new Set();
        let current = category;
        while (current && !visited.has(Number(current.id))) {
            visited.add(Number(current.id));
            labels.unshift(current.label);
            current = current.parent_category_id == null ? null : host.state.categories.find(function (item) { return Number(item.id) === Number(current.parent_category_id); });
        }
        const section = category.statement_section === 'balance_sheet' ? 'Balance Sheet' : (category.statement_section === 'income_statement' ? 'Income Statement' : 'Unplaced');
        return [section].concat(labels).join(' › ');
    }

    function accAssignmentPathSummary(account, kind) {
        const counts = new Map();
        accLeavesBelow(account.account_code).forEach(function (leaf) {
            const assignment = accAssignment(leaf);
            let path = 'Unassigned';
            if (kind === 'financial') {
                if (assignment.financial_statement_category_id != null) path = accFinancialCategoryPath(assignment.financial_statement_category_id);
            } else {
                const statementCategoryId = assignment.financial_statement_category_id;
                if (statementCategoryId != null && assignment.note_category_id != null) {
                    const options = accGetEffectiveNoteOptions(String(statementCategoryId));
                    const selected = options.find(function (item) { return String(item.category.id) === String(assignment.note_category_id); });
                    if (selected) path = selected.path.join(' › ');
                }
            }
            counts.set(path, (counts.get(path) || 0) + 1);
        });
        return Array.from(counts.entries()).map(function (entry) { return entry[0] + ' (' + entry[1] + ' account' + (entry[1] === 1 ? '' : 's') + ')'; }).join('; ');
    }

    function accMixedPickerLabel(account, kind) {
        const values = new Set();
        accLeavesBelow(account.account_code).forEach(function (leaf) {
            const assignment = accAssignment(leaf);
            if (kind === 'financial') {
                values.add(assignment.financial_statement_category_id == null ? '' : String(assignment.financial_statement_category_id));
                return;
            }
            const statementCategoryId = assignment.financial_statement_category_id;
            const noteId = assignment.note_category_id;
            if (statementCategoryId == null || noteId == null) { values.add(''); return; }
            const options = accGetEffectiveNoteOptions(String(statementCategoryId));
            const selected = options.find(function (item) { return String(item.category.id) === String(noteId); });
            values.add(selected ? String(selected.category.id) : '');
        });
        return 'Mixed (' + Math.max(values.size, 2) + ')';
    }

    function accPickerDetails(account, kind, selectedId, statementCategoryId, options) {
        if (kind === 'financial') {
            return selectedId === '__mixed' ? accAssignmentPathSummary(account, kind) : (selectedId ? accFinancialCategoryPath(selectedId) : 'Unassigned');
        }
        if (statementCategoryId === '__mixed') return 'Descendant accounts have different Financial Statement categories.';
        if (!statementCategoryId) return 'Assign a Financial Statement category first.';
        if (!options.length) return 'No notes are set up for this category.';
        if (selectedId === '__mixed') return accAssignmentPathSummary(account, kind);
        if (!selectedId) return 'Unassigned';
        const selected = options.find(function (item) { return String(item.category.id) === String(selectedId); });
        return selected ? selected.path.join(' › ') : 'Unassigned';
    }

    function accCategoryOptionMarkupFor(categories, selected) {
        let html = '<option value="">Unassigned</option>';
        if (selected === '__mixed') html = '<option value="__mixed" selected>Mixed assignments - choose one</option>' + html;
        html += categories.filter(function (category) { return category.row_type !== 'heading'; }).map(function (category) {
            const value = String(category.id);
            const inactive = !category.is_active;
            const parent = categories.find(function (candidate) { return Number(candidate.id) === Number(category.parent_category_id); });
            const section = category.statement_section === 'balance_sheet' ? 'Balance Sheet' : (category.statement_section === 'income_statement' ? 'Income Statement' : 'Unplaced');
            const label = (inactive ? 'Inactive: ' : '') + (parent ? parent.label + ' › ' : '') + category.label + ' · ' + section;
            return '<option value="' + accEscape(value) + '"' + (value === String(selected) ? ' selected' : '') + (inactive && value !== String(selected) ? ' disabled' : '') + '>' + accEscape(label) + '</option>';
        }).join('');
        return html;
    }

    function accCategoryOptionMarkup(selected) { return accCategoryOptionMarkupFor(host.state.categories, selected); }

    function accFinancialPickerLabel(categoryId) {
        if (categoryId === '__mixed') return 'Mixed';
        if (!categoryId) return 'Unassigned';
        const category = host.state.categories.find(function (item) { return Number(item.id) === Number(categoryId) && item.row_type !== 'heading'; });
        if (!category) return 'Unassigned';
        return (category.is_active ? '' : 'Inactive: ') + category.label;
    }

    function accFinancialPickerOptionMarkup(category, parent, sectionLabel, selectedId) {
        const inactive = !category.is_active;
        const search = [category.label, parent && parent.label, sectionLabel].filter(Boolean).join(' ').toLowerCase();
        return '<button type="button" class="acc2-fs-picker-option' + (inactive ? ' inactive' : '') + '" role="option" data-fs-picker-option data-fs-picker-value="' + category.id + '" data-fs-picker-search="' + accEscape(search) + '" aria-selected="' + (String(selectedId) === String(category.id) ? 'true' : 'false') + '"' + (inactive ? ' disabled aria-disabled="true"' : '') + '><span>' + (inactive ? '<span class="acc2-fs-picker-inactive">Inactive</span> ' : '') + accEscape(category.label) + '</span><span class="acc2-fs-picker-check" aria-hidden="true">✓</span></button>';
    }

    function accFinancialPickerSectionMarkup(section, label, categories, selectedId) {
        const rows = categories.filter(function (item) { return item.statement_section === section && item.row_type !== 'heading'; });
        const headings = rows.filter(function (item) { return Number(item.parent_category_id) > 0; }).map(function (item) {
            return categories.find(function (candidate) { return Number(candidate.id) === Number(item.parent_category_id) && candidate.row_type === 'heading' && candidate.statement_section === section; });
        }).filter(Boolean).filter(function (item, index, items) { return items.findIndex(function (candidate) { return Number(candidate.id) === Number(item.id); }) === index; }).sort(function (a, b) { return Number(a.sort_order || 0) - Number(b.sort_order || 0); });
        let content = '';
        headings.forEach(function (heading) {
            const children = rows.filter(function (item) { return Number(item.parent_category_id) === Number(heading.id); }).sort(function (a, b) { return Number(a.sort_order || 0) - Number(b.sort_order || 0); });
            if (!children.length) return;
            content += '<div class="acc2-fs-picker-heading-group" data-fs-picker-heading-group><div class="acc2-fs-picker-heading" role="presentation">' + accEscape(heading.label) + '</div>' + children.map(function (item) { return accFinancialPickerOptionMarkup(item, heading, label, selectedId); }).join('') + '</div>';
        });
        const standalone = rows.filter(function (item) { return !headings.some(function (heading) { return Number(heading.id) === Number(item.parent_category_id); }); }).sort(function (a, b) { return Number(a.sort_order || 0) - Number(b.sort_order || 0); });
        if (standalone.length) content += '<div class="acc2-fs-picker-heading-group" data-fs-picker-heading-group><div class="acc2-fs-picker-heading" role="presentation">Other ' + accEscape(label) + ' categories</div>' + standalone.map(function (item) { return accFinancialPickerOptionMarkup(item, null, label, selectedId); }).join('') + '</div>';
        if (!content) content = '<div class="acc2-fs-picker-empty-group" role="presentation">No ' + accEscape(label) + ' categories yet.</div>';
        return '<div class="acc2-fs-picker-section" data-fs-picker-section data-fs-picker-section-key="' + section + '"><div class="acc2-fs-picker-section-title" role="presentation">' + accEscape(label) + '</div>' + content + '</div>';
    }

    function accFinancialPickerMenuMarkup(selectedId) {
        const categories = host.state.categories;
        let html = '<div class="acc2-fs-picker-option-wrap" data-fs-picker-option-wrap role="listbox" aria-label="Financial Statement categories">';
        html += '<button type="button" class="acc2-fs-picker-option" role="option" data-fs-picker-option data-fs-picker-value="" data-fs-picker-search="unassigned" aria-selected="' + (!selectedId ? 'true' : 'false') + '"><span>Unassigned</span><span class="acc2-fs-picker-check" aria-hidden="true">✓</span></button>';
        if (selectedId === '__mixed') html += '<div class="acc2-fs-picker-mixed" role="presentation">Mixed assignments — choose one</div>';
        html += accFinancialPickerSectionMarkup('balance_sheet', 'Balance Sheet', categories, selectedId);
        html += accFinancialPickerSectionMarkup('income_statement', 'Income Statement', categories, selectedId);
        const unplaced = categories.filter(function (item) { return item.row_type !== 'heading' && !item.statement_section; }).sort(function (a, b) { return Number(a.sort_order || 0) - Number(b.sort_order || 0); });
        if (unplaced.length) {
            html += '<div class="acc2-fs-picker-section" data-fs-picker-section data-fs-picker-section-key="unplaced"><div class="acc2-fs-picker-section-title muted" role="presentation">Unplaced / legacy categories</div>';
            html += '<div class="acc2-fs-picker-heading-group" data-fs-picker-heading-group>' + unplaced.map(function (item) { return accFinancialPickerOptionMarkup(item, null, 'Unplaced categories', selectedId); }).join('') + '</div></div>';
        }
        return html + '<div class="acc2-fs-picker-no-results" data-fs-picker-no-results hidden>No matching categories.</div></div>';
    }

    function accFinancialPickerMarkup(account, selectedId, eager=false) {
        if(host.lazyPickers && !eager)return lazyPickerMarkup(account,selectedId,'financial');
        const codeKey = String(account.account_code || '').replace(/[^a-zA-Z0-9_-]/g, '_');
        const pickerId = 'accFsPicker_' + codeKey;
        const label = selectedId === '__mixed' ? accMixedPickerLabel(account, 'financial') : accFinancialPickerLabel(selectedId);
        const details = accPickerDetails(account, 'financial', selectedId, null, []);
        return '<div class="acc2-fs-picker" data-fs-picker>' +
            '<button type="button" class="acc2-fs-picker-trigger' + (label === 'Unassigned' ? ' is-unassigned' : '') + '" data-fs-picker-trigger role="combobox" aria-haspopup="listbox" aria-expanded="false" aria-controls="' + pickerId + '_menu" title="' + accEscape(details) + '" aria-label="Financial Statement category for account ' + accEscape(account.account_code) + ': ' + accEscape(details) + '"' + (!host.isAdmin ? ' disabled' : '') + '><span data-fs-picker-value-label>' + accEscape(label) + '</span><span class="acc2-fs-picker-chevron" aria-hidden="true">▾</span></button>' +
            '<div class="acc2-fs-picker-menu" id="' + pickerId + '_menu" data-fs-picker-menu hidden><label class="acc2-fs-picker-search-label" for="' + pickerId + '_search">Find a category</label><input type="search" class="acc2-fs-picker-search" id="' + pickerId + '_search" data-fs-picker-search-input placeholder="Search categories or headings" autocomplete="off">' + accFinancialPickerMenuMarkup(selectedId) + '</div>' +
            '<select class="acc2-financial-category" data-acc-code="' + accEscape(account.account_code) + '" data-acc-parent="' + (!account.is_leaf ? 'true' : 'false') + '" aria-hidden="true" tabindex="-1" hidden' + (!host.isAdmin ? ' disabled' : '') + '>' + accCategoryOptionMarkup(selectedId) + '</select></div>';
    }

    function accGetEffectiveNoteOptions(statementCategoryId) {
        if(optionCategories!==host.state.categories || optionNotes!==host.state.notes){
            noteOptionsCache.clear();optionCategories=host.state.categories;optionNotes=host.state.notes;
        }
        const cacheKey=String(statementCategoryId);
        if(noteOptionsCache.has(cacheKey))return noteOptionsCache.get(cacheKey);
        const category = host.state.categories.find(function (item) { return Number(item.id) === Number(statementCategoryId) && item.row_type !== 'heading'; });
        if (!category) return [];
        const root = host.state.notes.find(function (item) { return Number(item.source_statement_category_id) === Number(category.id); });
        if (!root || root.is_active === false || Number(root.is_active) === 0) return [];
        const options = [];
        const visited=new Set();
        function visit(note, path, isLinkedRoot) {
            if(visited.has(note.id))return;
            visited.add(note.id);
            const children = host.state.notes.filter(function (item) {
                return Number(item.parent_category_id) === Number(note.id) && item.is_active !== false && Number(item.is_active) !== 0;
            }).sort(function (a, b) { return Number(a.sort_order || 0) - Number(b.sort_order || 0); });
            const nextPath = path.concat([isLinkedRoot ? category.label : note.label]);
            if (!children.length) {
                options.push({ category: note, path: nextPath });
                return;
            }
            children.forEach(function (child) { visit(child, nextPath, false); });
        }
        visit(root, [], true);
        noteOptionsCache.set(cacheKey,options);
        return options;
    }

    function accNotePickerLabel(statementCategoryId, selectedId, options) {
        selectedId = accEffectiveNoteSelection(selectedId, options);
        if (statementCategoryId === '__mixed') return 'Mixed categories';
        if (!statementCategoryId) return 'Assign a category first';
        if (!options.length) return 'No notes available';
        if (selectedId === '__mixed') return 'Mixed';
        if (!selectedId) return 'Unassigned';
        const selected = options.find(function (item) { return String(item.category.id) === String(selectedId); });
        return selected ? (selected.category.is_active === false ? 'Inactive: ' : '') + selected.category.label : 'Unassigned';
    }

    // Notes outside the selected statement category are not effective assignments.
    function accEffectiveNoteSelection(selectedId, options) {
        if (selectedId === '__mixed') return '__mixed';
        if (selectedId == null || selectedId === '') return '';
        return options.some(function (item) { return String(item.category.id) === String(selectedId); }) ? String(selectedId) : '';
    }

    function accNoteAssignmentSearchText(account) {
        const statementCategoryId = accEffectiveFinancialCategory(account);
        let noteCategoryId = accEffectiveNoteCategory(account);
        if (noteCategoryId === '__mixed') return 'Mixed assignments';
        const options = statementCategoryId && statementCategoryId !== '__mixed' ? accGetEffectiveNoteOptions(statementCategoryId) : [];
        noteCategoryId = accEffectiveNoteSelection(noteCategoryId, options);
        if (!noteCategoryId) return 'Unassigned';
        const selected = options.find(function (item) { return String(item.category.id) === String(noteCategoryId); });
        return selected ? selected.path.join(' ') : 'Unassigned';
    }

    function accNotePickerMarkup(account, selectedId, statementCategoryId, options, eager=false) {
        if(host.lazyPickers && !eager)return lazyPickerMarkup(account,selectedId,'note',statementCategoryId,options);
        selectedId = accEffectiveNoteSelection(selectedId, options);
        const codeKey = String(account.account_code || '').replace(/[^a-zA-Z0-9_-]/g, '_');
        const pickerId = 'accNotePicker_' + codeKey;
        const source = statementCategoryId && statementCategoryId !== '__mixed'
            ? host.state.categories.find(function (item) { return Number(item.id) === Number(statementCategoryId); })
            : null;
        const disabled = !host.isAdmin || !source || !options.length;
        const label = selectedId === '__mixed' ? accMixedPickerLabel(account, 'note') : accNotePickerLabel(statementCategoryId, selectedId, options);
        const details = accPickerDetails(account, 'note', selectedId, statementCategoryId, options);
        let optionMarkup = '';
        let selectMarkup = '<option value="">Unassigned</option>';
        if (selectedId === '__mixed') selectMarkup = '<option value="__mixed" selected>Mixed</option>' + selectMarkup;
        if (options.length) {
            optionMarkup += '<div class="acc2-fs-picker-section" data-fs-picker-section><div class="acc2-fs-picker-section-title" role="presentation">' + accEscape(source.label) + ' notes</div>';
            optionMarkup += '<div class="acc2-fs-picker-heading-group" data-fs-picker-heading-group><button type="button" class="acc2-fs-picker-option" role="option" data-fs-picker-option data-fs-picker-value="" data-fs-picker-search="unassigned" aria-selected="' + (!selectedId ? 'true' : 'false') + '"><span>Unassigned</span><span class="acc2-fs-picker-check" aria-hidden="true">✓</span></button>';
            options.forEach(function (item) {
                const id = String(item.category.id);
                const path = item.path.join(' › ');
                optionMarkup += '<button type="button" class="acc2-fs-picker-option" role="option" data-fs-picker-option data-fs-picker-value="' + id + '" data-fs-picker-search="' + accEscape(path.toLowerCase()) + '" aria-selected="' + (String(selectedId) === id ? 'true' : 'false') + '"><span>' + accEscape(path) + '</span><span class="acc2-fs-picker-check" aria-hidden="true">✓</span></button>';
                selectMarkup += '<option value="' + id + '"' + (String(selectedId) === id ? ' selected' : '') + '>' + accEscape(path) + '</option>';
            });
            optionMarkup += '</div></div>';
        } else {
            optionMarkup = '<div class="acc2-fs-picker-empty-group" role="presentation">' + accEscape(label) + '</div>';
        }
        return '<div class="acc2-fs-picker acc2-note-picker" data-fs-picker>' +
            '<button type="button" class="acc2-fs-picker-trigger' + (label === 'Unassigned' ? ' is-unassigned' : '') + '" data-fs-picker-trigger role="combobox" aria-haspopup="listbox" aria-expanded="false" aria-controls="' + pickerId + '_menu" title="' + accEscape(details) + '" aria-label="Note for account ' + accEscape(account.account_code) + ': ' + accEscape(details) + '"' + (disabled ? ' disabled' : '') + '><span data-fs-picker-value-label>' + accEscape(label) + '</span><span class="acc2-fs-picker-chevron" aria-hidden="true">▾</span></button>' +
            '<div class="acc2-fs-picker-menu" id="' + pickerId + '_menu" data-fs-picker-menu hidden><label class="acc2-fs-picker-search-label" for="' + pickerId + '_search">Find a note</label><input type="search" class="acc2-fs-picker-search" id="' + pickerId + '_search" data-fs-picker-search-input placeholder="Search notes" autocomplete="off"><div class="acc2-fs-picker-option-wrap" data-fs-picker-option-wrap role="listbox" aria-label="Effective notes under ' + accEscape(source ? source.label : 'the assigned category') + '">' + optionMarkup + '<div class="acc2-fs-picker-no-results" data-fs-picker-no-results hidden>No matching notes.</div></div></div>' +
            '<select class="acc2-note-category" data-acc-code="' + accEscape(account.account_code) + '" data-acc-parent="' + (!account.is_leaf ? 'true' : 'false') + '" aria-hidden="true" tabindex="-1" hidden' + (disabled ? ' disabled' : '') + '>' + selectMarkup + '</select></div>';
    }

    async function accLoadFinancialCategories() {
        const results = await Promise.all([
            accApi('/api/account-control-center/categories?client_id=' + encodeURIComponent(host.clientId) + '&category_type=financial_statement&include_inactive=true'),
            accApi('/api/account-control-center/statement-calculations?client_id=' + encodeURIComponent(host.clientId) + '&statement_section=balance_sheet'),
            accApi('/api/account-control-center/statement-calculations?client_id=' + encodeURIComponent(host.clientId) + '&statement_section=income_statement'),
        ]);
        const result = results[0];
        host.state.categories = result.categories || [];
        host.state.calculations = results.slice(1).flatMap(function (item) { return item.calculations || []; });
        host.state.profitLink = results[2].profit_link || null;
        host.state.financialLoaded = true;
        accRenderFinancialRows();
        if (host.state.notesLoaded) accRenderNotesRows();
        accRenderFinancialOutlines();
        accRenderNotesOutlines();
    }

    function accProfitLinkMarkup() {
        const totals = host.state.calculations.filter(function (item) { return item.statement_section === 'income_statement' && item.calculation_type === 'total'; });
        const targets = host.state.categories.filter(function (item) {
            if (item.statement_section !== 'balance_sheet' || item.row_type !== 'category' || !item.is_active) return false;
            const parent = item.parent_category_id == null ? null : host.state.categories.find(function (row) { return Number(row.id) === Number(item.parent_category_id); });
            return item.parent_category_id == null ? item.presentation_role === 'equity' : parent && parent.is_active && parent.presentation_role === 'equity';
        });
        function options(rows, key) { return '<option value="">Select...</option>' + rows.map(function (row) { return '<option value="' + row.id + '"' + (host.state.profitLink && Number(host.state.profitLink[key]) === Number(row.id) ? ' selected' : '') + '>' + accEscape(row.label) + '</option>'; }).join(''); }
        return '<section class="acc2-profit-link" aria-label="Profit to capital link"><h3>Profit to capital</h3><p>Select the final Profit after taxation total and its equity destination. Confirm closing status separately for each Trial Balance in Reports.</p><div><label>Final profit total<select id="accProfitSource" class="acc2-select">' + options(totals, 'source_calculation_id') + '</select></label><label>Capital category<select id="accProfitTarget" class="acc2-select">' + options(targets, 'target_category_id') + '</select></label></div><button type="button" class="acc2-action-btn" data-profit-link-action="save">Save profit link</button> <button type="button" class="acc2-action-btn secondary" data-profit-link-action="clear">Remove link</button></section>';
    }

    async function accHandleProfitLinkAction(event) {
        const button = event.target.closest('[data-profit-link-action]');
        if (!button) return;
        const source = Number(accElement('accProfitSource').value) || null, target = Number(accElement('accProfitTarget').value) || null;
        if (button.dataset.profitLinkAction === 'save' && (!source || !target)) { accShowStatus('Choose both the final profit total and an equity category.', 'error'); return; }
        button.disabled = true;
        try {
            await accApi('/api/account-control-center/profit-link', { method: 'PUT', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({client_id: host.clientId, source_calculation_id: button.dataset.profitLinkAction === 'clear' ? null : source, target_category_id: button.dataset.profitLinkAction === 'clear' ? null : target}) });
            await accLoadFinancialCategories();
            accShowStatus('Profit link saved. Reload the statements and confirm the selected TB closing status.', 'success');
        } catch (error) { accShowStatus(error.message, 'error'); }
        finally { button.disabled = false; }
    }

    function accPresentationMarkup(item, readOnly) {
        if (item.statement_section !== 'balance_sheet' || (item.row_type === 'category' && item.parent_category_id != null)) return '';
        const labels = { asset: 'Asset', liability: 'Liability', equity: 'Equity' };
        if (readOnly) return '<span class="acc2-presentation-reference">' + (labels[item.presentation_role] || 'Not set') + '</span>';
        return '<label class="acc2-presentation-control">Presentation <select class="acc2-select" data-fs-presentation="' + item.id + '" aria-label="Presentation for ' + accEscape(item.label) + '">' +
            ['', 'asset', 'liability', 'equity'].map(function (role) { return '<option value="' + role + '"' + ((item.presentation_role || '') === role ? ' selected' : '') + '>' + (labels[role] || 'Not set') + '</option>'; }).join('') + '</select></label>';
    }

    function accIncomeStatementRoleMarkup(item, readOnly) {
        if (item.statement_section !== 'income_statement' || (item.row_type === 'category' && item.parent_category_id != null)) return '';
        const labels = { income: 'Income (credit-normal)', expense: 'Expense (debit-normal)' };
        if (readOnly) return '<span class="acc2-presentation-reference">' + (labels[item.income_statement_role] || 'Income/Expense not set') + '</span>';
        return '<label class="acc2-presentation-control">Income/Expense <select class="acc2-select" data-fs-income-role="' + item.id + '" aria-label="Income/Expense role for ' + accEscape(item.label) + '">' +
            ['', 'income', 'expense'].map(function (role) { return '<option value="' + role + '"' + ((item.income_statement_role || '') === role ? ' selected' : '') + '>' + (labels[role] || 'Not set') + '</option>'; }).join('') + '</select></label>';
    }

    async function accHandlePresentationChange(event) {
        const select = event.target.closest('[data-fs-presentation]');
        if (!select) return;
        const item = host.state.categories.find(function (row) { return Number(row.id) === Number(select.dataset.fsPresentation); });
        if (!item) return;
        select.disabled = true;
        try {
            await accApi('/api/account-control-center/categories/' + item.id, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ client_id: host.clientId, category_type: 'financial_statement', label: item.label, statement_section: item.statement_section, row_type: item.row_type, parent_category_id: item.parent_category_id, sort_order: item.sort_order, is_active: item.is_active, presentation_role: select.value || null }) });
            item.presentation_role = select.value || null;
            accShowStatus('Presentation saved. Reload the Balance Sheet to apply it.', 'success');
        } catch (error) {
            select.value = item.presentation_role || '';
            accShowStatus(error.message, 'error');
        } finally { select.disabled = false; }
    }

    async function accHandleIncomeStatementRoleChange(event) {
        const select = event.target.closest('[data-fs-income-role]');
        if (!select) return;
        const item = host.state.categories.find(function (row) { return Number(row.id) === Number(select.dataset.fsIncomeRole); });
        if (!item) return;
        select.disabled = true;
        try {
            await accApi('/api/account-control-center/categories/' + item.id, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ client_id: host.clientId, category_type: 'financial_statement', label: item.label, statement_section: item.statement_section, row_type: item.row_type, parent_category_id: item.parent_category_id, sort_order: item.sort_order, is_active: item.is_active, income_statement_role: select.value || null }) });
            item.income_statement_role = select.value || null;
            accShowStatus('Income/Expense role saved. Reload Income Statement Notes to apply.', 'success');
        } catch (error) {
            select.value = item.income_statement_role || '';
            accShowStatus(error.message, 'error');
        } finally { select.disabled = false; }
    }

    function accRenderFinancialOutlines() {
        document.querySelectorAll('[data-fs-section]').forEach(function (panel) {
            const section = panel.dataset.fsSection;
            const readOnly = accElement('accFinancialSetupModal').dataset.entryMode === 'notes';
            const rows = host.state.categories.filter(function (item) { return item.statement_section === section; });
            const headings = rows.filter(function (item) { return item.row_type === 'heading'; });
            const leaves = rows.filter(function (item) { return item.row_type !== 'heading'; });
            const calculations = host.state.calculations.filter(function (item) { return item.statement_section === section; });
            const totals = calculations.filter(function (item) { return item.calculation_type === 'total'; }).sort(function (a, b) { return Number(a.sort_order) - Number(b.sort_order) || Number(a.id) - Number(b.id); });
            const totalsAfterSourceRow = new Map();
            const unplacedTotals = [];
            function actions(item) { return accPresentationMarkup(item, readOnly) + accIncomeStatementRoleMarkup(item, readOnly) + (readOnly ? '' : '<button type="button" class="acc2-fs-small" data-fs-action="delete" data-fs-id="' + item.id + '">Delete</button><button type="button" class="acc2-fs-small" data-fs-action="rename" data-fs-id="' + item.id + '">Rename</button><button type="button" class="acc2-fs-small" data-fs-action="toggle" data-fs-id="' + item.id + '" data-fs-active="' + item.is_active + '">' + (item.is_active ? 'Deactivate' : 'Restore') + '</button>'); }
            function dragHandle(item, kind) { return readOnly ? '' : '<button type="button" class="acc2-fs-drag-handle" draggable="true" data-fs-drag-handle data-fs-id="' + item.id + '" aria-label="Drag to reorder ' + kind + ' ' + accEscape(item.label) + '" title="Drag to reorder">☰</button>'; }
            function calculationMarkup(calculation, kind) {
                const summary = (calculation.components || []).map(function (component, index) {
                    return (component.operator === 'subtract' ? (index ? ' − ' : '− ') : (index ? ' + ' : '+ ')) + accEscape(component.label);
                }).join('');
                const actionsMarkup = readOnly ? '' : '<span><button type="button" class="acc2-fs-small" data-fs-calc-action="edit" data-fs-calc-id="' + calculation.id + '">Edit</button><button type="button" class="acc2-fs-small" data-fs-calc-action="delete" data-fs-calc-id="' + calculation.id + '">Delete</button></span>';
                return '<div class="acc2-fs-calculation-line ' + kind + '" data-fs-calculation-id="' + calculation.id + '"><span class="acc2-fs-calculation-label"><span class="acc2-fs-sigma" aria-hidden="true">Σ</span><span><strong>' + accEscape(calculation.label) + '</strong><small>' + (summary || 'No rows selected') + '</small></span></span>' + actionsMarkup + '</div>';
            }
            totals.forEach(function (total) {
                const anchor = accFinancialTotalAnchorRow(total.components || [], headings, calculations, host.state.categories);
                if (!anchor) { unplacedTotals.push(total); return; }
                const group = totalsAfterSourceRow.get(anchor.key) || [];
                group.push(total);
                totalsAfterSourceRow.set(anchor.key, group);
            });
            function totalsMarkupAfter(key) {
                return (totalsAfterSourceRow.get(key) || []).map(function (total) {
                    return calculationMarkup(total, 'total') + totalsMarkupAfter('calculation:' + total.id);
                }).join('');
            }
            const outline = headings.map(function (heading) {
                const children = leaves.filter(function (item) { return Number(item.parent_category_id) === Number(heading.id); });
                const childRows = children.map(function (item) { return '<div class="acc2-fs-category acc2-fs-sort-row' + (item.is_active ? '' : ' inactive') + '" data-fs-sort-row data-fs-sort-kind="category" data-fs-sort-group="children:' + heading.id + '" data-fs-id="' + item.id + '"><span class="acc2-fs-category-label">' + dragHandle(item, 'category') + '<span>' + accEscape(item.label) + '</span></span><span>' + actions(item) + '</span></div>' + totalsMarkupAfter('category:' + item.id); }).join('');
                const subtotal = calculations.find(function (item) { return item.calculation_type === 'subtotal' && Number(item.parent_heading_id) === Number(heading.id); });
                const subtotalButton = !readOnly && !subtotal && children.length ? '<button type="button" class="acc2-fs-small primary" data-fs-calc-action="add-subtotal" data-fs-heading-id="' + heading.id + '">+ Subtotal</button>' : '';
                const addCategoryButton = readOnly ? '' : '<button type="button" class="acc2-fs-small primary" data-fs-action="add-category" data-fs-parent="' + heading.id + '">+ Category</button>';
                const subtotalRow = subtotal ? calculationMarkup(subtotal, 'subtotal') : '';
                const subtotalTotalRows = subtotal ? totalsMarkupAfter('calculation:' + subtotal.id) : '';
                return '<article class="acc2-fs-heading acc2-fs-sort-row' + (heading.is_active ? '' : ' inactive') + '" data-fs-sort-row data-fs-sort-kind="heading" data-fs-sort-group="headings:' + accEscape(section) + '" data-fs-id="' + heading.id + '"><div class="acc2-fs-heading-title"><span class="acc2-fs-heading-label">' + dragHandle(heading, 'heading') + '<strong>' + accEscape(heading.label) + '</strong></span><div>' + actions(heading) + subtotalButton + addCategoryButton + '</div></div><div class="acc2-fs-children" data-fs-sort-group="children:' + heading.id + '">' + (childRows || '<div class="acc2-fs-empty">No categories in this heading yet.</div>') + subtotalRow + subtotalTotalRows + '</div></article>';
            }).join('');
            const standalone = leaves.filter(function (item) { return !item.parent_category_id; });
            const unplaced = section === 'balance_sheet' ? host.state.categories.filter(function (item) { return !item.statement_section && item.row_type !== 'heading'; }) : [];
            const standaloneRows = standalone.map(function (item) { return '<div class="acc2-fs-category acc2-fs-sort-row' + (item.is_active ? '' : ' inactive') + '" data-fs-sort-row data-fs-sort-kind="category" data-fs-sort-group="standalone:' + accEscape(section) + '" data-fs-id="' + item.id + '"><span class="acc2-fs-category-label">' + dragHandle(item, 'category') + '<span>' + accEscape(item.label) + '</span></span><span>' + actions(item) + '</span></div>' + totalsMarkupAfter('category:' + item.id); }).join('');
            const standaloneMarkup = standalone.length ? '<div class="acc2-fs-standalone acc2-fs-sort-list"><h3>Standalone categories</h3><div data-fs-sort-group="standalone:' + accEscape(section) + '">' + standaloneRows + '</div></div>' : '';
            const unplacedRows = unplaced.map(function (item) { return '<div class="acc2-fs-category acc2-fs-sort-row' + (item.is_active ? '' : ' inactive') + '" data-fs-sort-row data-fs-sort-kind="category" data-fs-sort-group="unplaced" data-fs-id="' + item.id + '"><span class="acc2-fs-category-label">' + dragHandle(item, 'category') + '<span>' + accEscape(item.label) + '</span></span><span>' + actions(item) + '</span></div>'; }).join('');
            const toolbarActions = readOnly ? '' : '<div><button type="button" class="acc2-action-btn" data-fs-action="add-heading">+ Add heading</button><button type="button" class="acc2-action-btn secondary" data-fs-action="add-category">+ Add category</button><button type="button" class="acc2-action-btn secondary" data-fs-calc-action="add-total">+ Add total</button></div>';
            const toolbarMessage = readOnly ? 'Read-only reference. Open Financial Statements setup to edit the statement structure.' : 'Drag the ☰ handle to reorder within a statement or parent.';
            const emptyMessage = readOnly ? 'No categories are available in this statement yet.' : 'Start with a heading, or add a standalone category.';
            const unplacedTotalMarkup = !readOnly && unplacedTotals.length ? '<section class="acc2-fs-total-list"><h3>Totals needing a source heading</h3>' + unplacedTotals.map(function (item) { return calculationMarkup(item, 'total'); }).join('') + '</section>' : '';
            panel.innerHTML = (section === 'income_statement' && !readOnly ? accProfitLinkMarkup() : '') + accFinancialCalculationEditorMarkup(section) + '<div class="acc2-fs-toolbar"><span>' + toolbarMessage + '</span><span class="acc2-fs-order-status" data-fs-order-status role="status" aria-live="polite"></span>' + toolbarActions + '</div>' + (outline ? '<div class="acc2-fs-heading-list" data-fs-sort-group="headings:' + accEscape(section) + '">' + outline + '</div>' : '') + standaloneMarkup + (unplaced.length ? '<div class="acc2-fs-standalone acc2-fs-unplaced acc2-fs-sort-list"><h3>Unplaced categories</h3><div data-fs-sort-group="unplaced">' + unplacedRows + '</div></div>' : '') + unplacedTotalMarkup + (!outline && !standalone.length && !unplaced.length ? '<div class="acc2-fs-empty-state">' + emptyMessage + '</div>' : '');
            accUpdateFinancialTotalChoiceAvailability(panel.querySelector('[data-fs-calc-editor]'));
        });
    }

    function accFinancialTotalAnchorRow(components, headings, calculations, categories) {
        const totals = calculations.filter(function (item) { return item.calculation_type === 'total'; })
            .sort(function (a, b) { return Number(a.sort_order) - Number(b.sort_order) || Number(a.id) - Number(b.id); });
        const sources = (components || []).map(function (component) {
            if (component.source_category_id != null) {
                const category = categories.find(function (item) { return Number(item.id) === Number(component.source_category_id) && item.row_type === 'category'; });
                return category ? { key: 'category:' + category.id, section: category.statement_section } : null;
            }
            if (component.source_calculation_id != null) {
                const calculation = calculations.find(function (item) { return Number(item.id) === Number(component.source_calculation_id) && ['subtotal', 'total'].includes(item.calculation_type); });
                return calculation ? { key: 'calculation:' + calculation.id, section: calculation.statement_section } : null;
            }
            return null;
        }).filter(Boolean);
        if (!sources.length) return null;
        const section = sources[0].section;
        const rowPositions = new Map();
        headings.forEach(function (heading, headingIndex) {
            const children = categories.filter(function (item) {
                return item.statement_section === section && item.row_type === 'category' && Number(item.parent_category_id) === Number(heading.id);
            });
            const base = headingIndex * 100000;
            children.forEach(function (category, childIndex) {
                rowPositions.set('category:' + category.id, { order: base + (childIndex + 1) * 10, label: category.label });
            });
            const subtotal = calculations.find(function (item) {
                return item.statement_section === section && item.calculation_type === 'subtotal' && Number(item.parent_heading_id) === Number(heading.id);
            });
            if (subtotal) rowPositions.set('calculation:' + subtotal.id, { order: base + (children.length + 1) * 10, label: subtotal.label });
        });
        const standalone = categories.filter(function (item) {
            return item.statement_section === section && item.row_type === 'category' && item.parent_category_id == null;
        });
        const standaloneBase = headings.length * 100000;
        standalone.forEach(function (category, index) {
            rowPositions.set('category:' + category.id, { order: standaloneBase + (index + 1) * 10, label: category.label });
        });
        const totalAnchorCounts = new Map();
        totals.forEach(function (total) {
            const anchorSources = (total.components || []).map(function (component) {
                if (component.source_category_id != null) return 'category:' + component.source_category_id;
                if (component.source_calculation_id != null) return 'calculation:' + component.source_calculation_id;
                return null;
            }).filter(Boolean);
            const anchor = anchorSources.map(function (key) {
                const row = rowPositions.get(key);
                return row ? { key: key, ...row } : null;
            }).filter(Boolean).sort(function (a, b) { return b.order - a.order; })[0];
            if (!anchor) return;
            const count = totalAnchorCounts.get(anchor.key) || 0;
            totalAnchorCounts.set(anchor.key, count + 1);
            rowPositions.set('calculation:' + total.id, { order: anchor.order + 1 + count, label: total.label });
        });
        let anchor = null;
        sources.forEach(function (source) {
            const row = rowPositions.get(source.key);
            if (row && (!anchor || row.order > anchor.order)) anchor = { key: source.key, label: row.label, order: row.order };
        });
        return anchor;
    }

    function accFinancialCalculationEditorMarkup(section) {
        const state = host.state.editor;
        if (!state || section !== state.statement_section || accElement('accFinancialSetupModal').dataset.entryMode === 'notes') return '';
        const calculations = host.state.calculations.filter(function (item) { return item.statement_section === section; });
        const heading = host.state.categories.find(function (item) { return Number(item.id) === Number(state.parent_heading_id); });
        const selected = new Map((state.components || []).map(function (component) {
            const key = component.source_category_id != null ? 'category:' + component.source_category_id : 'calculation:' + component.source_calculation_id;
            return [key, component];
        }));
        const choices = state.calculation_type === 'subtotal'
            ? host.state.categories.filter(function (item) { return item.statement_section === section && item.row_type === 'category' && Number(item.parent_category_id) === Number(state.parent_heading_id); }).map(function (item) { return { id: item.id, label: item.label, kind: 'category', headingId: state.parent_heading_id }; })
            : [];
        const totalGroups = state.calculation_type === 'total'
            ? host.state.categories.filter(function (item) { return item.statement_section === section && item.row_type === 'heading'; }).map(function (item) {
                const children = host.state.categories.filter(function (category) { return category.statement_section === section && category.row_type === 'category' && Number(category.parent_category_id) === Number(item.id); });
                if (!children.length) return null;
                const subtotal = calculations.find(function (calculation) { return calculation.calculation_type === 'subtotal' && Number(calculation.parent_heading_id) === Number(item.id); });
                const rows = (subtotal ? [{ id: subtotal.id, label: subtotal.label, kind: 'calculation', headingId: item.id }] : []).concat(children.map(function (category) {
                    return { id: category.id, label: category.label, kind: 'category', headingId: item.id };
                }));
                return { heading: item, choices: rows };
            }).filter(Boolean)
            : [];
        if (state.calculation_type === 'total' && section === 'income_statement') {
            const standaloneChoices = host.state.categories.filter(function (item) {
                return item.statement_section === section && item.row_type === 'category' && !item.parent_category_id;
            }).map(function (item) { return { id: item.id, label: item.label, kind: 'category', headingId: 'standalone' }; });
            if (standaloneChoices.length) totalGroups.push({ heading: { id: 'standalone', label: 'Standalone categories' }, choices: standaloneChoices });
        }
        if (state.calculation_type === 'total') {
            const earlierTotals = calculations.filter(function (calculation) {
                return calculation.calculation_type === 'total'
                    && Number(calculation.id) !== Number(state.calculation_id)
                    && Number(calculation.sort_order) < Number(state.sort_order);
            }).map(function (calculation) { return { id: calculation.id, label: calculation.label, kind: 'total', headingId: 'earlier-totals' }; });
            if (earlierTotals.length) totalGroups.unshift({ heading: { id: 'earlier-totals', label: 'Earlier totals' }, choices: earlierTotals });
        }
        const allChoices = state.calculation_type === 'total' ? totalGroups.flatMap(function (group) { return group.choices; }) : choices;
        function renderChoice(choice) {
            const key = choice.kind + ':' + choice.id;
            const component = selected.get(key);
            const totalChoice = state.calculation_type === 'total';
            const checked = !totalChoice || !!component;
            const sourceAttr = choice.kind === 'category' ? 'data-source-category-id="' : 'data-source-calculation-id="';
            const choiceKind = choice.kind === 'category' ? 'category' : choice.kind === 'total' ? 'total' : 'subtotal';
            const kindLabel = totalChoice && choiceKind !== 'category' ? '<small class="acc2-fs-calc-choice-type">' + (choiceKind === 'total' ? 'Total' : 'Subtotal') + '</small>' : '';
            return '<label class="acc2-fs-calc-choice ' + (totalChoice ? 'is-' + choiceKind : '') + '" data-fs-choice-row data-fs-choice-heading-id="' + choice.headingId + '" data-fs-choice-kind="' + choiceKind + '"><span><input type="' + (totalChoice ? 'checkbox' : 'hidden') + '" data-fs-calc-include data-fs-choice-heading-id="' + choice.headingId + '" data-fs-choice-kind="' + choiceKind + '" ' + (checked ? 'checked ' : '') + sourceAttr + choice.id + '"><span class="acc2-fs-calc-choice-name">' + accEscape(choice.label) + '</span>' + kindLabel + '<small data-fs-choice-overlap hidden></small></span><select data-fs-calc-operator aria-label="Add or subtract ' + accEscape(choice.label) + '"' + (totalChoice && !checked ? ' disabled' : '') + '><option value="add"' + (!component || component.operator === 'add' ? ' selected' : '') + '>+</option><option value="subtract"' + (component && component.operator === 'subtract' ? ' selected' : '') + '>−</option></select></label>';
        }
        const rows = state.calculation_type === 'total'
            ? totalGroups.map(function (group) { return '<section class="acc2-fs-calc-choice-group" data-fs-choice-group="' + group.heading.id + '"><h4>' + accEscape(group.heading.label) + '</h4>' + group.choices.map(renderChoice).join('') + '</section>'; }).join('')
            : choices.map(renderChoice).join('');
        const title = state.calculation_type === 'subtotal' ? 'Subtotal for ' + (heading ? heading.label : 'heading') : 'Statement total';
        const initialAnchor = state.calculation_type === 'total'
            ? accFinancialTotalAnchorRow(state.components || [], host.state.categories.filter(function (item) { return item.statement_section === section && item.row_type === 'heading'; }), calculations, host.state.categories)
            : null;
        const placementPreview = state.calculation_type === 'total'
            ? '<small class="acc2-fs-calc-placement" data-fs-calc-placement role="status">' + (initialAnchor ? 'Appears after: ' + accEscape(initialAnchor.label) : 'Select rows to choose where this total appears.') + '</small>'
            : '';
        return '<section class="acc2-fs-calc-editor" data-fs-calc-editor><div class="acc2-fs-calc-editor-head"><div><strong>' + accEscape(title) + '</strong><small>' + (state.calculation_type === 'subtotal' ? 'All categories under this heading are included.' : 'Choose a subtotal or individual categories within each heading.') + '</small>' + placementPreview + '</div></div><label class="acc2-fs-calc-label">Line label<input type="text" maxlength="120" data-fs-calc-label value="' + accEscape(state.label || '') + '"></label><div class="acc2-fs-calc-choices">' + (rows || '<div class="acc2-fs-empty">' + (state.calculation_type === 'subtotal' ? 'Add a category to this heading first.' : 'Add ' + (section === 'income_statement' ? 'Income Statement' : 'Balance Sheet') + ' categories before creating a total.')) + '</div><div class="acc2-fs-calc-editor-actions"><button type="button" class="acc2-action-btn" data-fs-calc-action="save"' + (!allChoices.length ? ' disabled' : '') + '>Save line</button><button type="button" class="acc2-action-btn secondary" data-fs-calc-action="cancel">Cancel</button></div></section>';
    }

    function accUpdateFinancialCalculationPlacementPreview(editor) {
        const preview = editor && editor.querySelector('[data-fs-calc-placement]');
        if (!preview) return;
        const components = Array.from(editor.querySelectorAll('[data-fs-calc-include]')).filter(function (input) {
            return input.type === 'hidden' || input.checked;
        }).map(function (input) { return {
            source_category_id: input.dataset.sourceCategoryId || null,
            source_calculation_id: input.dataset.sourceCalculationId || null,
        }; });
        const section = host.state.editor ? host.state.editor.statement_section : 'balance_sheet';
        const headings = host.state.categories.filter(function (item) { return item.statement_section === section && item.row_type === 'heading'; });
        const calculations = host.state.calculations.filter(function (item) { return item.statement_section === section; });
        const anchor = accFinancialTotalAnchorRow(components, headings, calculations, host.state.categories);
        preview.textContent = anchor ? 'Appears after: ' + anchor.label : 'Select rows to choose where this total appears.';
    }

    function accUpdateFinancialTotalChoiceAvailability(editor) {
        if (!editor) return;
        function lineageFor(kind, id, visiting) {
            const key = kind + ':' + id;
            if (kind === 'category') return new Set([key]);
            const calculation = host.state.calculations.find(function (item) { return Number(item.id) === Number(id); });
            if (!calculation) return new Set();
            visiting = visiting || new Set();
            if (visiting.has(Number(id))) return new Set();
            visiting.add(Number(id));
            const lineage = new Set();
            (calculation.components || []).forEach(function (component) {
                if (component.source_category_id != null) lineage.add('category:' + component.source_category_id);
                else if (component.source_calculation_id != null) {
                    const nested = host.state.calculations.find(function (item) { return Number(item.id) === Number(component.source_calculation_id); });
                    const nestedKind = nested && nested.calculation_type;
                    lineageFor(nestedKind, component.source_calculation_id, visiting).forEach(function (source) { lineage.add(source); });
                }
            });
            visiting.delete(Number(id));
            return lineage;
        }
        function inputLineage(input) {
            if (input.dataset.sourceCategoryId) return lineageFor('category', input.dataset.sourceCategoryId);
            if (input.dataset.sourceCalculationId) {
                const source = host.state.calculations.find(function (item) { return Number(item.id) === Number(input.dataset.sourceCalculationId); });
                return lineageFor(source && source.calculation_type, input.dataset.sourceCalculationId);
            }
            return new Set();
        }
        const selected = Array.from(editor.querySelectorAll('[data-fs-calc-include][type="checkbox"]')).filter(function (input) { return input.checked; }).map(function (input) {
            return { input: input, lineage: inputLineage(input), label: input.closest('[data-fs-choice-row]').querySelector('.acc2-fs-calc-choice-name').textContent.trim(), kind: input.dataset.fsChoiceKind };
        });
        const groups = new Map();
        editor.querySelectorAll('[data-fs-calc-include][type="checkbox"]').forEach(function (input) {
            const key = input.dataset.fsChoiceHeadingId;
            const choices = groups.get(key) || [];
            choices.push(input);
            groups.set(key, choices);
        });
        groups.forEach(function (choices) {
            const hasSubtotal = choices.some(function (input) { return input.dataset.fsChoiceKind === 'subtotal' && input.checked; });
            const hasCategory = choices.some(function (input) { return input.dataset.fsChoiceKind === 'category' && input.checked; });
            choices.forEach(function (input) {
                const row = input.closest('[data-fs-choice-row]');
                const operator = row.querySelector('[data-fs-calc-operator]');
                const directConflict = (input.dataset.fsChoiceKind === 'subtotal' && hasCategory) || (input.dataset.fsChoiceKind === 'category' && hasSubtotal);
                const lineage = inputLineage(input);
                const overlap = selected.find(function (item) {
                    if (item.input === input) return false;
                    return Array.from(lineage).some(function (source) { return item.lineage.has(source); });
                });
                const disabled = !input.checked && (directConflict || !!overlap);
                input.disabled = disabled;
                operator.disabled = !input.checked;
                row.classList.toggle('is-unavailable', disabled);
                const note = row.querySelector('[data-fs-choice-overlap]');
                note.textContent = overlap ? ((overlap.kind === 'total' || overlap.kind === 'subtotal') ? 'Included via ' : 'Overlaps ') + overlap.label : '';
                note.hidden = !overlap;
                if (overlap) row.title = note.textContent;
                else row.removeAttribute('title');
            });
        });
    }

    function accFinancialSortRows(group) {
        return Array.from(group.children).filter(function (node) { return node.matches('[data-fs-sort-row]'); });
    }

    function accClearFinancialDropState(panel) {
        panel.querySelectorAll('.acc2-fs-drop-before, .acc2-fs-drop-after, .acc2-fs-dragging').forEach(function (node) {
            node.classList.remove('acc2-fs-drop-before', 'acc2-fs-drop-after', 'acc2-fs-dragging');
        });
    }

    function accHandleFinancialDragStart(event) {
        const handle = event.target.closest('[data-fs-drag-handle]');
        const row = handle && handle.closest('[data-fs-sort-row]');
        const panel = row && row.closest('[data-fs-section]');
        if (!row || !event.dataTransfer || panel.getAttribute('aria-busy') === 'true') { event.preventDefault(); return; }
        host.state.dragging = row.dataset.fsId;
        event.dataTransfer.effectAllowed = 'move';
        event.dataTransfer.setData('text/plain', host.state.dragging);
        row.classList.add('acc2-fs-dragging');
    }

    function accHandleFinancialDragOver(event) {
        const target = event.target.closest('[data-fs-sort-row]');
        const source = host.state.dragging && document.querySelector('[data-fs-sort-row][data-fs-id="' + CSS.escape(host.state.dragging) + '"]');
        if (!target || !source || target === source || target.dataset.fsSortGroup !== source.dataset.fsSortGroup) return;
        event.preventDefault();
        if (event.dataTransfer) event.dataTransfer.dropEffect = 'move';
        const after = event.clientY > target.getBoundingClientRect().top + target.getBoundingClientRect().height / 2;
        target.classList.toggle('acc2-fs-drop-after', after);
        target.classList.toggle('acc2-fs-drop-before', !after);
    }

    async function accHandleFinancialDrop(event) {
        const target = event.target.closest('[data-fs-sort-row]');
        const source = host.state.dragging && document.querySelector('[data-fs-sort-row][data-fs-id="' + CSS.escape(host.state.dragging) + '"]');
        if (!target || !source || target === source || target.dataset.fsSortGroup !== source.dataset.fsSortGroup) return;
        event.preventDefault();
        const group = source.parentElement;
        const rows = accFinancialSortRows(group);
        const ids = rows.map(function (row) { return row.dataset.fsId; });
        const sourceIndex = ids.indexOf(source.dataset.fsId);
        const targetIndex = ids.indexOf(target.dataset.fsId);
        if (sourceIndex < 0 || targetIndex < 0) return;
        ids.splice(sourceIndex, 1);
        const after = event.clientY > target.getBoundingClientRect().top + target.getBoundingClientRect().height / 2;
        const adjustedTargetIndex = targetIndex - (sourceIndex < targetIndex ? 1 : 0);
        const insertAt = adjustedTargetIndex + (after ? 1 : 0);
        ids.splice(Math.min(insertAt, ids.length), 0, source.dataset.fsId);
        if (ids.every(function (id, index) { return id === rows[index].dataset.fsId; })) return;
        const panel = source.closest('[data-fs-section]');
        const orderStatus = panel && panel.querySelector('[data-fs-order-status]');
        if (panel) panel.setAttribute('aria-busy', 'true');
        if (orderStatus && window.LoadingUI) window.LoadingUI.showSpinner(orderStatus, 'Saving order', { escalate: false });
        try {
            await accApi('/api/account-control-center/categories/order', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ client_id: host.clientId, category_ids: ids.map(Number) }) });
            await accLoadFinancialCategories();
            accShowStatus('Financial Statement order updated.', 'success');
        } finally {
            if (orderStatus && window.LoadingUI) window.LoadingUI.hideSpinner(orderStatus);
            if (panel) panel.removeAttribute('aria-busy');
        }
    }

    function accFinancialKey(label) { return String(label || '').normalize('NFKD').toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '').slice(0, 70) || 'category'; }

    function accOpenFinancialCalculationEditor(section, calculationType, headingId, calculationId) {
        const calculation = calculationId ? host.state.calculations.find(function (item) { return Number(item.id) === Number(calculationId); }) : null;
        const heading = headingId ? host.state.categories.find(function (item) { return Number(item.id) === Number(headingId); }) : null;
        const components = calculation ? calculation.components || [] : calculationType === 'subtotal'
            ? host.state.categories.filter(function (item) { return item.statement_section === section && item.row_type === 'category' && Number(item.parent_category_id) === Number(headingId); }).map(function (item) { return { source_category_id: item.id, operator: 'add' }; })
            : [];
        host.state.editor = {
            statement_section: calculation ? calculation.statement_section : section,
            calculation_id: calculation ? calculation.id : null,
            calculation_type: calculation ? calculation.calculation_type : calculationType,
            parent_heading_id: calculation ? calculation.parent_heading_id : (heading ? heading.id : null),
            label: calculation ? calculation.label : (calculationType === 'subtotal' ? 'Total ' + heading.label : ''),
            sort_order: calculation ? calculation.sort_order : host.state.calculations.filter(function (item) { return item.statement_section === section && item.calculation_type === 'total'; }).reduce(function (last, item) { return Math.max(last, Number(item.sort_order) + 1); }, 0),
            components: components.map(function (item) { return { ...item }; }),
        };
        accRenderFinancialOutlines();
        const labelInput = document.querySelector('[data-fs-section="' + host.state.editor.statement_section + '"] [data-fs-calc-label]');
        if (labelInput) { labelInput.focus(); labelInput.select(); }
    }

    async function accHandleFinancialCalculationAction(event) {
        const button = event.target.closest('[data-fs-calc-action]');
        if (!button) return;
        const action = button.dataset.fsCalcAction;
        const section = button.closest('[data-fs-section]')?.dataset.fsSection || host.state.editor?.statement_section || 'balance_sheet';
        if (action === 'cancel') {
            host.state.editor = null;
            accRenderFinancialOutlines();
            return;
        }
        if (action === 'add-subtotal') {
            accOpenFinancialCalculationEditor(section, 'subtotal', Number(button.dataset.fsHeadingId));
            return;
        }
        if (action === 'add-total') {
            accOpenFinancialCalculationEditor(section, 'total', null);
            return;
        }
        if (action === 'edit') {
            accOpenFinancialCalculationEditor(section, null, null, Number(button.dataset.fsCalcId));
            return;
        }
        if (action === 'delete') {
            const calculation = host.state.calculations.find(function (item) { return Number(item.id) === Number(button.dataset.fsCalcId); });
            if (!calculation || !window.confirm('Delete calculation line “' + calculation.label + '”?')) return;
            await accApi('/api/account-control-center/statement-calculations/' + calculation.id + '?client_id=' + encodeURIComponent(host.clientId), { method: 'DELETE' });
            await accLoadFinancialCategories();
            accShowStatus('Deleted calculation line “' + calculation.label + '”.', 'success');
            return;
        }
        if (action !== 'save') return;
        const editor = button.closest('[data-fs-calc-editor]');
        const label = editor.querySelector('[data-fs-calc-label]').value.trim();
        const components = Array.from(editor.querySelectorAll('[data-fs-calc-include]')).filter(function (input) {
            return input.type === 'hidden' || input.checked;
        }).map(function (input) {
            const row = input.closest('.acc2-fs-calc-choice');
            const component = { operator: row.querySelector('[data-fs-calc-operator]').value };
            if (input.dataset.sourceCategoryId) component.source_category_id = Number(input.dataset.sourceCategoryId);
            if (input.dataset.sourceCalculationId) component.source_calculation_id = Number(input.dataset.sourceCalculationId);
            return component;
        });
        if (!label) { accShowStatus('Enter a label for this calculation line.', 'error'); return; }
        if (!components.length) { accShowStatus('Select at least one row for this total.', 'error'); return; }
        const state = host.state.editor;
        const payload = {
            client_id: host.clientId,
            statement_section: state.statement_section,
            calculation_type: state.calculation_type,
            label: label,
            parent_heading_id: state.calculation_type === 'subtotal' ? Number(state.parent_heading_id) : null,
            sort_order: state.sort_order,
            components: components,
        };
        await accWithButtonBusy(button, 'Saving line', async function () {
            const method = state.calculation_id ? 'PUT' : 'POST';
            const url = '/api/account-control-center/statement-calculations' + (state.calculation_id ? '/' + state.calculation_id : '');
            await accApi(url, { method: method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
        });
        host.state.editor = null;
        await accLoadFinancialCategories();
        accShowStatus('Saved calculation line “' + label + '”.', 'success');
    }

    function accHandleCalculationIncludeChange(event) {
        const input = event.target.closest('[data-fs-calc-include]');
        if (!input || input.type !== 'checkbox') return;
        const editor = input.closest('[data-fs-calc-editor]');
        accUpdateFinancialTotalChoiceAvailability(editor);
        accUpdateFinancialCalculationPlacementPreview(editor);
    }

    async function accHandleFinancialOutlineAction(event) {
        const button = event.target.closest('[data-fs-action]');
        if (!button) return;
        const action = button.dataset.fsAction;
        const panel = button.closest('[data-fs-section]');
        const section = panel.dataset.fsSection;
        const item = host.state.categories.find(function (row) { return Number(row.id) === Number(button.dataset.fsId); });
        if (action === 'add-heading' || action === 'add-category') {
            const label = window.prompt(action === 'add-heading' ? 'Heading name' : 'Category name');
            if (!label || !label.trim()) return;
            const parentId = button.dataset.fsParent ? Number(button.dataset.fsParent) : null;
            const body = { client_id: host.clientId, category_type: 'financial_statement', category_key: accFinancialKey(label), label: label.trim(), statement_section: section, row_type: action === 'add-heading' ? 'heading' : 'category', parent_category_id: parentId, sort_order: host.state.categories.length };
            await accApi('/api/account-control-center/categories', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
        } else if (item && action === 'rename') {
            const label = window.prompt('Rename category', item.label);
            if (!label || !label.trim()) return;
            await accApi('/api/account-control-center/categories/' + item.id, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ client_id: host.clientId, category_type: 'financial_statement', label: label.trim(), statement_section: item.statement_section, row_type: item.row_type, parent_category_id: item.parent_category_id, sort_order: item.sort_order, is_active: item.is_active }) });
        } else if (item && action === 'toggle') {
            await accApi('/api/account-control-center/categories/' + item.id, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ client_id: host.clientId, category_type: 'financial_statement', label: item.label, statement_section: item.statement_section, row_type: item.row_type, parent_category_id: item.parent_category_id, sort_order: item.sort_order, is_active: !item.is_active }) });
        } else if (item && action === 'delete') {
            const children = item.row_type === 'heading' ? host.state.categories.filter(function (row) { return Number(row.parent_category_id) === Number(item.id); }) : [];
            const prompt = children.length
                ? 'Delete heading “' + item.label + '” and all ' + children.length + ' child categor' + (children.length === 1 ? 'y' : 'ies') + '? This cannot be undone. The heading and its children must have no Trial Balance account assignments.'
                : 'Permanently delete category “' + item.label + '”? It must have no Trial Balance account assignments. This cannot be undone.';
            if (!window.confirm(prompt)) return;
            const result = await accApi('/api/account-control-center/categories/' + item.id + '?client_id=' + encodeURIComponent(host.clientId), { method: 'DELETE' });
            await accLoadFinancialCategories();
            const childCount = Number(result.deleted_child_count || 0);
            accShowStatus(childCount ? 'Deleted heading and ' + childCount + ' child categor' + (childCount === 1 ? 'y.' : 'ies.') : 'Deleted category “' + item.label + '”.', 'success');
            return;
        } else return;
        await accLoadFinancialCategories();
    }

    function accRenderNotesOutlines() {
        document.querySelectorAll('[data-notes-section]').forEach(function (panel) {
            const section = panel.dataset.notesSection;
            const headings = host.state.categories.filter(function (row) { return row.statement_section === section && row.row_type === 'heading'; });
            const rows = host.state.categories.filter(function (row) { return row.statement_section === section && row.row_type !== 'heading'; });
            const roots = host.state.notes.filter(function (row) { return row.source_statement_category_id != null; });
            const legacy = host.state.notes.filter(function (row) { return row.source_statement_category_id == null && row.parent_category_id == null; });
            function breakdownMarkup(item) {
                const children = host.state.notes.filter(function (row) { return Number(row.parent_category_id) === Number(item.id); });
                return '<div class="acc2-note-breakdown" data-note-row data-note-id="' + item.id + '"><div class="acc2-note-row"><button type="button" class="acc2-fs-drag-handle" draggable="true" data-note-drag-handle aria-label="Move breakdown ' + accEscape(item.label) + '" title="Drag to reorder or move">☰</button><span>' + accEscape(item.label) + '</span><button type="button" class="acc2-fs-small" data-note-action="add" data-note-parent="' + item.id + '">+ Breakdown</button><button type="button" class="acc2-fs-small" data-note-action="rename" data-note-id="' + item.id + '">Rename</button><button type="button" class="acc2-fs-small" data-note-action="delete" data-note-id="' + item.id + '">Delete</button></div><div class="acc2-note-children" data-note-drop-parent="' + item.id + '">' + children.map(breakdownMarkup).join('') + '</div></div>';
            }
            function sourceRow(source) {
                const root = roots.find(function (item) { return Number(item.source_statement_category_id) === Number(source.id); });
                const drag = root ? '<button type="button" class="acc2-fs-drag-handle" draggable="true" data-note-anchor-drag-handle data-note-anchor-id="' + root.id + '" aria-label="Move note heading ' + accEscape(source.label) + '" title="Drag this note to another category in this statement">☰</button>' : '';
                return '<div class="acc2-notes-source-row" data-note-source-drop data-source-id="' + source.id + '"' + (root ? ' data-note-anchor-row data-note-anchor-id="' + root.id + '"' : '') + '>' + drag + '<span class="acc2-notes-source-label">' + accEscape(source.label) + '</span>' + (root ? '<button type="button" class="acc2-fs-small" data-note-action="add-child" data-note-parent="' + root.id + '">+ Breakdown</button><button type="button" class="acc2-fs-small" data-note-action="delete" data-note-id="' + root.id + '">Remove note</button><div class="acc2-note-children" data-note-drop-parent="' + root.id + '">' + host.state.notes.filter(function (item) { return Number(item.parent_category_id) === Number(root.id); }).map(breakdownMarkup).join('') + '</div>' : '<button type="button" class="acc2-fs-small primary" data-note-action="link" data-source-id="' + source.id + '">+ Add note</button>') + '</div>';
            }
            const groups = headings.map(function (heading) {
                const children = rows.filter(function (row) { return Number(row.parent_category_id) === Number(heading.id); });
                return '<section class="acc2-notes-group"><h3>' + accEscape(heading.label) + '</h3>' + (children.map(sourceRow).join('') || '<p class="acc2-fs-empty">No statement categories in this heading.</p>') + '</section>';
            }).join('');
            const standalone = rows.filter(function (row) { return !row.parent_category_id; }).map(sourceRow).join('');
            const legacyMarkup = legacy.map(breakdownMarkup).join('');
            panel.innerHTML = '<div class="acc2-fs-toolbar"><span>Notes follow their statement row. Add breakdowns as needed; drag ☰ to reorder or move them.</span><span class="acc2-fs-order-status" data-note-order-status role="status" aria-live="polite"></span></div>' + groups + (standalone ? '<section class="acc2-notes-group"><h3>Standalone statement rows</h3>' + standalone + '</section>' : '') + (legacyMarkup ? '<section class="acc2-notes-group legacy"><h3>Unlinked / legacy notes</h3>' + legacyMarkup + '</section>' : '') + (!groups && !standalone && !legacyMarkup ? '<div class="acc2-fs-empty-state">Add a Financial Statement category first.</div>' : '');
        });
    }

    async function accLoadNoteCategories() {
        const result = await accApi('/api/account-control-center/categories?client_id=' + encodeURIComponent(host.clientId) + '&category_type=note&include_inactive=true');
        host.state.notes = result.categories || [];
        host.state.notesLoaded = true;
        accRenderNotesRows();
        accRenderNotesOutlines();
    }

    async function accLoadNotesSetupData() {
        const loads = [];
        if (!host.state.financialLoaded) loads.push(accLoadFinancialCategories());
        if (!host.state.notesLoaded) loads.push(accLoadNoteCategories());
        await Promise.all(loads);
        accRenderNotesRows();
        accRenderNotesOutlines();
    }

    function accSaveFinancialLeaf(account,select) { return saveAssignment(account,select,'financial',false); }

    function accSaveNoteLeaf(account,select) { return saveAssignment(account,select,'note',false); }

    function accApplyNoteParent(account,select) { return saveAssignment(account,select,'note',true); }

    function accApplyFinancialParent(account,select) { return saveAssignment(account,select,'financial',true); }

    function accFilterFinancialPicker(picker) {
        const searchInput = picker.querySelector('[data-fs-picker-search-input]');
        const term = String(searchInput && searchInput.value || '').trim().toLowerCase();
        const options = Array.from(picker.querySelectorAll('[data-fs-picker-option]'));
        options.forEach(function (option) { option.hidden = !!term && String(option.dataset.fsPickerSearch || '').indexOf(term) === -1; });
        picker.querySelectorAll('[data-fs-picker-heading-group]').forEach(function (group) {
            group.hidden = !group.querySelector('[data-fs-picker-option]:not([hidden])');
        });
        picker.querySelectorAll('[data-fs-picker-section]').forEach(function (section) {
            section.hidden = !!term && !section.querySelector('[data-fs-picker-option]:not([hidden])');
        });
        const visibleCount = options.filter(function (option) { return !option.hidden && !option.disabled; }).length;
        const noResults = picker.querySelector('[data-fs-picker-no-results]');
        if (noResults) noResults.hidden = visibleCount > 0;
    }

    function accPositionFinancialPicker(picker) {
        const trigger = picker.querySelector('[data-fs-picker-trigger]');
        const menu = picker.querySelector('[data-fs-picker-menu]');
        if (!trigger || !menu) return;
        const rect = trigger.getBoundingClientRect();
        const maxHeight = Math.min(380, Math.max(180, window.innerHeight - 24));
        const width = Math.min(Math.max(rect.width, 340), window.innerWidth - 24);
        menu.style.width = width + 'px';
        menu.style.maxHeight = maxHeight + 'px';
        menu.style.visibility = 'hidden';
        menu.hidden = false;
        const height = Math.min(menu.scrollHeight, maxHeight);
        const left = Math.max(12, Math.min(rect.left, window.innerWidth - width - 12));
        let top = rect.bottom + 5;
        if (top + height > window.innerHeight - 10 && rect.top > height + 10) top = rect.top - height - 5;
        menu.style.left = left + 'px';
        menu.style.top = Math.max(10, Math.min(top, window.innerHeight - height - 10)) + 'px';
        menu.style.visibility = '';
    }

    function accCloseFinancialPicker(picker, returnFocus) {
        if (!picker) return;
        const trigger = picker.querySelector('[data-fs-picker-trigger]');
        const menu = picker.querySelector('[data-fs-picker-menu]');
        if (menu) {
            menu.hidden = true;
            if(picker.dataset.fsPickerLazy){
                menu.replaceChildren();
                const select=picker.querySelector('select');
                const selected=select?.selectedOptions[0]?.cloneNode(true);
                if(selected)select.replaceChildren(selected);
            }
        }
        if (trigger) {
            trigger.setAttribute('aria-expanded', 'false');
            if (returnFocus) trigger.focus();
        }
        picker.classList.remove('is-open');
    }

    function accCloseFinancialPickers(except) {
        document.querySelectorAll('[data-fs-picker].is-open').forEach(function (picker) { if (picker !== except) accCloseFinancialPicker(picker, false); });
    }

    function accOpenFinancialPicker(picker) {
        const trigger = picker && picker.querySelector('[data-fs-picker-trigger]');
        const menu = picker && picker.querySelector('[data-fs-picker-menu]');
        if (!trigger || !menu || trigger.disabled) return;
        accCloseFinancialPickers(picker);
        if(picker.dataset.fsPickerLazy){
            const select=picker.querySelector('select');
            const account=host.accounts.find(item=>accNormaliseCode(item.account_code)===accNormaliseCode(select.dataset.accCode));
            if(!account)return;
            const categoryId=accEffectiveFinancialCategory(account);
            const template=document.createElement('template');
            template.innerHTML=picker.dataset.fsPickerLazy==='note'
                ?accNotePickerMarkup(account,select.value,categoryId,accGetEffectiveNoteOptions(categoryId),true)
                :accFinancialPickerMarkup(account,select.value,true);
            menu.innerHTML=template.content.querySelector('[data-fs-picker-menu]').innerHTML;
            select.innerHTML=template.content.querySelector('select').innerHTML;
        }
        picker.classList.add('is-open');
        trigger.setAttribute('aria-expanded', 'true');
        const searchInput = picker.querySelector('[data-fs-picker-search-input]');
        if (searchInput) searchInput.value = '';
        accFilterFinancialPicker(picker);
        accPositionFinancialPicker(picker);
        if (searchInput) searchInput.focus();
    }

    function accUpdateFinancialPickerSelection(select) {
        const picker = select && select.closest('[data-fs-picker]');
        if (!picker) return;
        const account = host.accounts.find(function (item) { return accNormaliseCode(item.account_code) === accNormaliseCode(select.dataset.accCode); });
        const label = picker.querySelector('[data-fs-picker-value-label]');
        const trigger = picker.querySelector('[data-fs-picker-trigger]');
        let details = 'Unassigned';
        if (label) {
            if (select.matches('.acc2-note-category')) {
                const statementCategoryId = account ? accEffectiveFinancialCategory(account) : '';
                const options = statementCategoryId && statementCategoryId !== '__mixed' ? accGetEffectiveNoteOptions(statementCategoryId) : [];
                label.textContent = account && select.value === '__mixed' ? accMixedPickerLabel(account, 'note') : accNotePickerLabel(statementCategoryId, select.value, options);
                details = account ? accPickerDetails(account, 'note', select.value, statementCategoryId, options) : label.textContent;
            } else {
                label.textContent = account && select.value === '__mixed' ? accMixedPickerLabel(account, 'financial') : accFinancialPickerLabel(select.value);
                details = account ? accPickerDetails(account, 'financial', select.value, null, []) : label.textContent;
            }
        }
        if (trigger) {
            const kind = select.matches('.acc2-note-category') ? 'Note' : 'Financial Statement category';
            trigger.classList.toggle('is-unassigned', !!label && label.textContent.trim() === 'Unassigned');
            trigger.title = details;
            trigger.setAttribute('aria-label', kind + ' for account ' + select.dataset.accCode + ': ' + details);
        }
        picker.querySelectorAll('[data-fs-picker-option]').forEach(function (option) {
            option.setAttribute('aria-selected', String(option.dataset.fsPickerValue) === String(select.value) ? 'true' : 'false');
        });
    }

    function accHandleFinancialPickerClick(event) {
        const trigger = event.target.closest('[data-fs-picker-trigger]');
        if (trigger) {
            const picker = trigger.closest('[data-fs-picker]');
            if (picker.classList.contains('is-open')) accCloseFinancialPicker(picker, false);
            else accOpenFinancialPicker(picker);
            return;
        }
        const option = event.target.closest('[data-fs-picker-option]');
        if (!option || option.disabled || option.hidden) return;
        const picker = option.closest('[data-fs-picker]');
        const select = picker && picker.querySelector('.acc2-financial-category, .acc2-note-category');
        if (!select) return;
        const value = option.dataset.fsPickerValue || '';
        if (select.value === value && select.dataset.accParent !== 'true') { accCloseFinancialPicker(picker, true); return; }
        select.value = value;
        accUpdateFinancialPickerSelection(select);
        accCloseFinancialPicker(picker, true);
        select.dispatchEvent(new Event('change', { bubbles: true }));
    }

    function accHandleFinancialPickerInput(event) {
        const searchInput = event.target.closest('[data-fs-picker-search-input]');
        if (searchInput) accFilterFinancialPicker(searchInput.closest('[data-fs-picker]'));
    }

    function accHandleFinancialPickerKeydown(event) {
        const trigger = event.target.closest('[data-fs-picker-trigger]');
        if (trigger && (event.key === 'ArrowDown' || event.key === 'ArrowUp')) {
            event.preventDefault();
            accOpenFinancialPicker(trigger.closest('[data-fs-picker]'));
            const options = trigger.closest('[data-fs-picker]').querySelectorAll('[data-fs-picker-option]:not([hidden]):not(:disabled)');
            if (options.length) options[event.key === 'ArrowUp' ? options.length - 1 : 0].focus();
            return;
        }
        const picker = event.target.closest('[data-fs-picker]');
        if (!picker || !picker.classList.contains('is-open')) return;
        if (event.key === 'Escape') {
            event.preventDefault();
            accCloseFinancialPicker(picker, true);
            return;
        }
        const option = event.target.closest('[data-fs-picker-option]');
        if (!option) {
            if (event.target.matches('[data-fs-picker-search-input]') && event.key === 'ArrowDown') {
                const first = picker.querySelector('[data-fs-picker-option]:not([hidden]):not(:disabled)');
                if (first) { event.preventDefault(); first.focus(); }
            }
            return;
        }
        if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return;
        const options = Array.from(picker.querySelectorAll('[data-fs-picker-option]:not([hidden]):not(:disabled)'));
        const index = options.indexOf(option);
        const nextIndex = event.key === 'Home' ? 0 : (event.key === 'End' ? options.length - 1 : (index + (event.key === 'ArrowDown' ? 1 : -1) + options.length) % options.length);
        if (options[nextIndex]) { event.preventDefault(); options[nextIndex].focus(); }
    }

    async function accHandleFinancialChange(event) {
        const select = event.target.closest('.acc2-financial-category');
        if (!select) return;
        accUpdateFinancialPickerSelection(select);
        const account = host.accounts.find(function (item) { return accNormaliseCode(item.account_code) === accNormaliseCode(select.dataset.accCode); });
        if (!account) return;
        accClearStatus();
        try {
            if (select.dataset.accParent === 'true') await accApplyFinancialParent(account, select);
            else await accSaveFinancialLeaf(account, select);
        } catch (error) {
            accShowStatus(error.message, 'error');
            await accLoadMapping(true).catch(function () {});
        }
    }

    async function accHandleNoteChange(event) {
        const select = event.target.closest('.acc2-note-category');
        if (!select) return;
        const account = host.accounts.find(function (item) { return accNormaliseCode(item.account_code) === accNormaliseCode(select.dataset.accCode); });
        if (!account) return;
        accClearStatus();
        try {
            if (select.dataset.accParent === 'true') await accApplyNoteParent(account, select);
            else await accSaveNoteLeaf(account, select);
        } catch (error) {
            accShowStatus(error.message, 'error');
            await accLoadMapping(true).catch(function () {});
        }
    }

    async function accUpdateNoteCategory(category, patch, message) {
        await accApi('/api/account-control-center/categories/' + category.id, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ client_id: host.clientId, category_type: 'note', label: patch.label == null ? category.label : patch.label, parent_category_id: category.parent_category_id, source_statement_category_id: category.source_statement_category_id, sort_order: patch.sort_order == null ? category.sort_order : patch.sort_order, is_active: patch.is_active == null ? category.is_active : patch.is_active }) });
        await accLoadNoteCategories();
        accShowStatus(message, 'success');
    }

    async function accHandleNotesOutlineAction(event) {
        const button = event.target.closest('[data-note-action]');
        if (!button) return;
        const action = button.dataset.noteAction;
        if (action === 'link') {
            const source = host.state.categories.find(function (item) { return Number(item.id) === Number(button.dataset.sourceId); });
            if (!source) return;
            await accApi('/api/account-control-center/categories', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ client_id: host.clientId, category_type: 'note', category_key: 'statement_' + source.id, label: source.label, source_statement_category_id: source.id, sort_order: source.sort_order }) });
            await accLoadNoteCategories();
            accShowStatus('Note heading linked to ' + source.label + '.', 'success');
            return;
        }
        const item = host.state.notes.find(function (row) { return Number(row.id) === Number(button.dataset.noteId || button.dataset.noteParent); });
        if (action === 'add' || action === 'add-child') {
            const parentId = Number(button.dataset.noteParent);
            const label = window.prompt('Breakdown name');
            if (!label || !label.trim()) return;
            await accApi('/api/account-control-center/categories', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ client_id: host.clientId, category_type: 'note', category_key: 'note_' + Date.now(), label: label.trim(), parent_category_id: parentId, sort_order: host.state.notes.length }) });
            await accLoadNoteCategories();
            accShowStatus('Note breakdown added.', 'success');
        } else if (item && action === 'rename') {
            const label = window.prompt('Rename breakdown', item.label);
            if (!label || !label.trim() || label.trim() === item.label) return;
            await accUpdateNoteCategory(item, { label: label.trim() }, 'Note breakdown renamed.');
        } else if (item && action === 'delete') {
            const descendants = (function count(id) { const children = host.state.notes.filter(function (row) { return Number(row.parent_category_id) === Number(id); }); return children.reduce(function (total, child) { return total + 1 + count(child.id); }, 0); })(item.id);
            if (!window.confirm('Delete “' + item.label + '”' + (descendants ? ' and its ' + descendants + ' child breakdown(s)' : '') + '? Assigned note categories cannot be deleted.')) return;
            const result = await accApi('/api/account-control-center/categories/' + item.id + '?client_id=' + encodeURIComponent(host.clientId), { method: 'DELETE' });
            await accLoadNoteCategories();
            accShowStatus(result.deleted_child_count ? 'Note breakdown and child rows deleted.' : 'Note breakdown deleted.', 'success');
        }
    }

    function accNoteSubtreeAssignedCount(categoryId) {
        const category = host.state.notes.find(function (item) { return Number(item.id) === Number(categoryId); });
        const own = Number(category && category.assigned_account_count || 0);
        const children = host.state.notes.filter(function (item) { return Number(item.parent_category_id) === Number(categoryId); });
        return own + children.reduce(function (total, child) { return total + accNoteSubtreeAssignedCount(child.id); }, 0);
    }

    function accHandleNotesDragStart(event) {
        const anchorHandle = event.target.closest('[data-note-anchor-drag-handle]');
        if (anchorHandle) {
            const anchorRow = anchorHandle.closest('[data-note-anchor-row]');
            if (!anchorRow || !event.dataTransfer) { event.preventDefault(); return; }
            host.state.noteAnchorDragging = anchorRow.dataset.noteAnchorId;
            host.state.noteDragging = null;
            event.dataTransfer.effectAllowed = 'move';
            event.dataTransfer.setData('text/plain', host.state.noteAnchorDragging);
            anchorRow.classList.add('acc2-note-dragging');
            return;
        }
        const handle = event.target.closest('[data-note-drag-handle]');
        const row = handle && handle.closest('[data-note-row]');
        if (!row || !event.dataTransfer) { event.preventDefault(); return; }
        host.state.noteDragging = row.dataset.noteId;
        host.state.noteAnchorDragging = null;
        event.dataTransfer.effectAllowed = 'move';
        event.dataTransfer.setData('text/plain', host.state.noteDragging);
        row.classList.add('acc2-note-dragging');
    }

    async function accHandleNotesDrop(event) {
        const panel = event.currentTarget;
        if (host.state.noteAnchorDragging) {
            const targetSource = event.target.closest('[data-note-source-drop]');
            const note = host.state.notes.find(function (item) { return Number(item.id) === Number(host.state.noteAnchorDragging); });
            const source = targetSource && host.state.categories.find(function (item) { return Number(item.id) === Number(targetSource.dataset.sourceId); });
            if (!note || !targetSource || !source || Number(note.source_statement_category_id) === Number(source.id)) return;
            event.preventDefault();
            if (source.statement_section !== panel.dataset.notesSection) return;
            const assigned = accNoteSubtreeAssignedCount(note.id);
            const warning = assigned
                ? 'This note has ' + assigned + ' currently assigned Trial Balance account' + (assigned === 1 ? '' : 's') + '. Move it to “' + source.label + '”? The note and its assignments will stay together, and future statements will report them under the new category.'
                : 'Move this note to “' + source.label + '”? Its breakdowns will move with it.';
            if (!window.confirm(warning)) return;
            const status = panel.querySelector('[data-note-order-status]');
            panel.setAttribute('aria-busy', 'true');
            if (status && window.LoadingUI) window.LoadingUI.showSpinner(status, 'Moving note', { escalate: false });
            try {
                await accApi('/api/account-control-center/categories/' + note.id, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ client_id: host.clientId, category_type: 'note', label: note.label, statement_section: null, row_type: 'category', parent_category_id: null, source_statement_category_id: source.id, sort_order: source.sort_order, is_active: note.is_active }) });
                await accLoadNoteCategories();
                accShowStatus('Note moved. Its account assignments remain linked.', 'success');
            } finally {
                if (status && window.LoadingUI) window.LoadingUI.hideSpinner(status);
                panel.removeAttribute('aria-busy');
            }
            return;
        }
        const source = host.state.noteDragging && panel.querySelector('[data-note-row][data-note-id="' + CSS.escape(host.state.noteDragging) + '"]');
        const target = event.target.closest('[data-note-row]');
        if (!source || (target === source) || (!target && !event.target.closest('[data-note-drop-parent]'))) return;
        event.preventDefault();
        const targetParentContainer = event.target.closest('[data-note-drop-parent]');
        let parentId = targetParentContainer ? Number(targetParentContainer.dataset.noteDropParent) : Number(target && target.parentElement.closest('[data-note-drop-parent]')?.dataset.noteDropParent || 0) || null;
        let group;
        let before = false;
        if (target && event.clientY > target.getBoundingClientRect().top + target.getBoundingClientRect().height * .34 && event.clientY < target.getBoundingClientRect().bottom - target.getBoundingClientRect().height * .34) {
            parentId = Number(target.dataset.noteId);
            group = target.querySelector(':scope > [data-note-drop-parent]');
        } else {
            group = target ? target.parentElement : targetParentContainer;
            if (target) before = event.clientY < target.getBoundingClientRect().top + target.getBoundingClientRect().height / 2;
        }
        if (!group) return;
        const ids = Array.from(group.children).filter(function (node) { return node.matches('[data-note-row]'); }).map(function (node) { return node.dataset.noteId; }).filter(function (id) { return id !== host.state.noteDragging; });
        const insertIndex = target && target.parentElement === group ? ids.indexOf(target.dataset.noteId) + (before ? 0 : 1) : ids.length;
        ids.splice(Math.max(0, insertIndex), 0, host.state.noteDragging);
        const status = panel.querySelector('[data-note-order-status]');
        panel.setAttribute('aria-busy', 'true');
        if (status && window.LoadingUI) window.LoadingUI.showSpinner(status, 'Saving note order', { escalate: false });
        try {
            await accApi('/api/account-control-center/categories/order', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ client_id: host.clientId, category_type: 'note', category_ids: ids.map(Number), parent_category_id: parentId }) });
            await accLoadNoteCategories();
            accShowStatus('Note breakdown order updated.', 'success');
        } finally {
            if (status && window.LoadingUI) window.LoadingUI.hideSpinner(status);
            panel.removeAttribute('aria-busy');
        }
    }

    function accSetFinancialSetupModal(isOpen, entryMode) {
        const modal = accElement('accFinancialSetupModal');
        if (!modal) return;
        modal.hidden = !isOpen;
        if (isOpen) {
            const mode = entryMode === 'notes' ? 'notes' : 'financial';
            modal.dataset.entryMode = mode;
            returnFocus=host.focusTarget?host.focusTarget(mode):document.activeElement;previousOverflow=document.body.style.overflow;document.body.style.overflow='hidden';
            if(host.modalChanged)host.modalChanged(true);
            accElement('accFinancialSetupModalTitle').textContent = mode === 'notes' ? 'Notes to Financial Statements' : 'Financial Statements';
            accHideStatus(accElement('accStatus'));
            accRenderFinancialOutlines();
            accSetFinancialSetupTab(mode === 'notes' ? 'notes-balance-sheet' : 'balance-sheet');
            accElement('accFinancialSetupClose').focus();
            accLoadNotesSetupData().catch(function (error) { accShowStatus(error.message, 'error'); });
        } else {
            document.body.style.overflow=previousOverflow;
            if(host.modalChanged)host.modalChanged(false);
            if(returnFocus?.isConnected)returnFocus.focus();
        }
    }

    function accSetFinancialSetupTab(tabName) {
        document.querySelectorAll('[data-acc-financial-setup-tab]').forEach(function (tab) {
            const active = tab.dataset.accFinancialSetupTab === tabName;
            tab.classList.toggle('active', active);
            tab.setAttribute('aria-selected', active ? 'true' : 'false');
        });
        document.querySelectorAll('[id^="accFinancialSetupPanel"]').forEach(function (panel) {
            panel.hidden = panel.getAttribute('aria-labelledby') !== document.querySelector('[data-acc-financial-setup-tab="' + CSS.escape(tabName) + '"]').id;
        });
    }

    const pendingSaves=new Set();
    function noteBelongs(noteId, categoryId) {
        if(noteId==null)return true;
        let note=host.state.notes.find(item=>Number(item.id)===Number(noteId));
        const seen=new Set();
        while(note && !seen.has(note.id)){
            seen.add(note.id);
            if(note.is_active===false || Number(note.is_active)===0)return false;
            if(note.parent_category_id==null)return categoryId!=null && Number(note.source_statement_category_id)===Number(categoryId);
            note=host.state.notes.find(item=>Number(item.id)===Number(note.parent_category_id));
        }
        return false;
    }
    function refreshRows(){accRenderFinancialRows();if(host.renderFinancialRows!==host.renderNotesRows)accRenderNotesRows();}
    async function saveAssignment(account,select,kind,parent) {
        if(!host.isAdmin || select.value==='__mixed')return;
        const key=accNormaliseCode(account.account_code);
        if(pendingSaves.has(key))return;
        const value=select.value?Number(select.value):null;
        const targets=parent?accLeavesBelow(key):[account];
        if(!targets.length){accShowStatus('No linked child accounts. Double-click this parent’s account code to correct it, then retry.','error');refreshRows();return;}
        const clearing=kind==='financial'?targets.filter(item=>accAssignment(item).note_category_id!=null && !noteBelongs(accAssignment(item).note_category_id,value)):[];
        if(kind==='note' && value!=null && targets.some(item=>!noteBelongs(value,accAssignment(item).financial_statement_category_id))){
            accShowStatus('Choose a Note belonging to the account’s Financial Statement category.','error');refreshRows();return;
        }
        const warning=(parent?'Apply this '+(kind==='financial'?'Financial Statement category':'Note')+' to '+targets.length+' leaf account(s) below '+key+'? ':'')
            +(clearing.length?'This will clear '+clearing.length+' incompatible Note assignment(s). Continue?':'');
        if(warning && !window.confirm(warning)){refreshRows();return;}
        const fields=kind==='financial'?{financial_statement_category_id:value}:{note_category_id:value};
        if(!parent && clearing.length)fields.note_category_id=null;
        const context={clientId:host.clientId,uploadId:accSelectedUploadPayload().trial_balance_upload_id,accountCode:key};
        pendingSaves.add(key);accSetSelectBusy(select,true);
        try{
            const busy=host.assignmentBusy?operation=>host.assignmentBusy(kind,parent,account,select,operation):operation=>accWithBusy('Saving assignment…',operation);
            await busy(async()=>{
                const result=await AccountAssignments.saveMapping(context,fields,accApi,parent,clearing.length>0);
                if(String(host.clientId)!==String(context.clientId) || String(accSelectedUploadPayload().trial_balance_upload_id)!==String(context.uploadId))return;
                if(host.applyConfirmedChanges && host.applyConfirmedChanges(result,context,targets)){
                    refreshRows();accShowStatus('Saved '+(kind==='financial'?'Financial Statement category':'Note')+' for '+key+'.','success');return;
                }
                // Older servers lack change acknowledgements. A malformed scoped acknowledgement
                // requires a fresh read; never apply its values or guess the changed accounts.
                if(!host.applyConfirmedChanges || !Object.hasOwn(result,'assignment_changes'))targets.forEach(item=>{
                    item.assignment=Object.assign({},accAssignment(item),fields);
                    if(kind==='financial' && clearing.includes(item))item.assignment.note_category_id=null;
                });
                refreshRows();
                try{await accLoadMapping(true);accShowStatus('Saved '+(kind==='financial'?'Financial Statement category':'Note')+' for '+key+'.','success');}
                catch(error){accShowStatus('Assignment saved. Refresh could not complete: '+error.message,'error');}
            });
        }catch(error){refreshRows();accShowStatus(error.message,'error');}
        finally{
            pendingSaves.delete(key);accSetSelectBusy(select,false);
            if(document.activeElement===document.body){
                const selector=kind==='financial'?'.acc2-financial-category':'.acc2-note-category';
                const current=Array.from(document.querySelectorAll(selector)).find(item=>accNormaliseCode(item.dataset.accCode)===key && item.closest('[data-fs-picker]')?.getClientRects().length);
                current?.closest('[data-fs-picker]')?.querySelector('[data-fs-picker-trigger]')?.focus({preventScroll:true});
            }
        }
    }

    function bindPickers(root) {
        root.addEventListener('click', accHandleFinancialPickerClick);
        root.addEventListener('input', accHandleFinancialPickerInput);
        root.addEventListener('keydown', accHandleFinancialPickerKeydown);
        root.addEventListener('change', event => {
            if(event.target.matches('.acc2-financial-category')) accHandleFinancialChange(event);
            if(event.target.matches('.acc2-note-category')) accHandleNoteChange(event);
        });
    }
    function bindSetup() {
        accElement('accFinancialSetupClose').addEventListener('click', function () { accSetFinancialSetupModal(false); });
        document.querySelectorAll('[data-acc-financial-setup-tab]').forEach(function (tab) { tab.addEventListener('click', function () { accSetFinancialSetupTab(tab.dataset.accFinancialSetupTab); }); });
        document.querySelectorAll('[data-fs-section]').forEach(function (panel) {
            panel.addEventListener('click', function (event) { accHandleFinancialCalculationAction(event).catch(function (error) { accShowStatus(error.message, 'error'); }); });
            panel.addEventListener('change', accHandleCalculationIncludeChange);
            panel.addEventListener('change', accHandlePresentationChange);
            panel.addEventListener('change', accHandleIncomeStatementRoleChange);
            panel.addEventListener('click', accHandleProfitLinkAction);
            panel.addEventListener('click', function (event) { accHandleFinancialOutlineAction(event).catch(function (error) { accShowStatus(error.message, 'error'); }); });
            panel.addEventListener('dragstart', accHandleFinancialDragStart);
            panel.addEventListener('dragover', accHandleFinancialDragOver);
            panel.addEventListener('drop', function (event) {
                accHandleFinancialDrop(event).catch(function (error) { accShowStatus(error.message, 'error'); accLoadFinancialCategories().catch(function () {}); }).finally(function () { accClearFinancialDropState(panel); host.state.dragging = null; });
            });
            panel.addEventListener('dragend', function () { accClearFinancialDropState(panel); host.state.dragging = null; });
        });
        document.querySelectorAll('[data-notes-section]').forEach(function (panel) {
            panel.addEventListener('click', function (event) { accHandleNotesOutlineAction(event).catch(function (error) { accShowStatus(error.message, 'error'); }); });
            panel.addEventListener('dragstart', accHandleNotesDragStart);
            panel.addEventListener('dragover', function (event) { if ((host.state.noteDragging || host.state.noteAnchorDragging) && event.target.closest('[data-note-row], [data-note-drop-parent], [data-note-source-drop]')) { event.preventDefault(); if (event.dataTransfer) event.dataTransfer.dropEffect = 'move'; } });
            panel.addEventListener('drop', function (event) { accHandleNotesDrop(event).catch(function (error) { accShowStatus(error.message, 'error'); accLoadNoteCategories().catch(function () {}); }).finally(function () { panel.querySelectorAll('.acc2-note-dragging').forEach(function (row) { row.classList.remove('acc2-note-dragging'); }); host.state.noteDragging = null; host.state.noteAnchorDragging = null; }); });
            panel.addEventListener('dragend', function () { panel.querySelectorAll('.acc2-note-dragging').forEach(function (row) { row.classList.remove('acc2-note-dragging'); }); host.state.noteDragging = null; host.state.noteAnchorDragging = null; });
        });
        document.addEventListener('click', event => {if(!event.target.closest('[data-fs-picker]')) accCloseFinancialPickers();});
        document.addEventListener('scroll', event => {if(!event.target.closest || !event.target.closest('[data-fs-picker-menu]')) accCloseFinancialPickers();},true);
        window.addEventListener('resize',()=>accCloseFinancialPickers());
        document.addEventListener('keydown',event=>{
            const modal=accElement('accFinancialSetupModal');
            if(event.defaultPrevented || modal.hidden)return;
            if(event.key==='Escape'){event.preventDefault();accSetFinancialSetupModal(false);return;}
            if(event.key!=='Tab')return;
            const nodes=Array.from(modal.querySelectorAll('button:not(:disabled),input:not(:disabled),select:not(:disabled),[tabindex="0"]')).filter(node=>node.getClientRects().length);
            const first=nodes[0],last=nodes[nodes.length-1];
            if(!modal.contains(document.activeElement)){event.preventDefault();(event.shiftKey?last:first)?.focus();}
            else if(event.shiftKey && document.activeElement===first){event.preventDefault();last?.focus();}
            else if(!event.shiftKey && document.activeElement===last){event.preventDefault();first?.focus();}
        });
    }


    bindSetup();
    return Object.freeze({STATE: host.state, bindPickers, noteBelongs, open: async function(mode) {
        if(!host.isAdmin) return;
        const context=String(host.clientId)+':'+String(accSelectedUploadPayload().trial_balance_upload_id);
        if(setupContext!==context){host.state.financialLoaded=false;host.state.editor=null;if(setupContext!==null)host.state.notesLoaded=false;setupContext=context;}
        if(!host.state.financialLoaded || !host.state.notesLoaded){
            if(!setupLoading)setupLoading=accWithBusy('Loading Financial Statements setup…',accLoadNotesSetupData).finally(()=>{setupLoading=null;});
            await setupLoading;
        }
        accSetFinancialSetupModal(true,mode);
    }, accEffectiveFinancialCategory, accEffectiveNoteCategory, accCategoryOptionMarkupFor, accCategoryOptionMarkup, accFinancialPickerLabel, accFinancialPickerOptionMarkup, accFinancialPickerSectionMarkup, accFinancialPickerMenuMarkup, accFinancialPickerMarkup, accGetEffectiveNoteOptions, accNotePickerLabel, accNoteAssignmentSearchText, accNotePickerMarkup, accLoadFinancialCategories, accProfitLinkMarkup, accHandleProfitLinkAction, accPresentationMarkup, accIncomeStatementRoleMarkup, accHandlePresentationChange, accHandleIncomeStatementRoleChange, accRenderFinancialOutlines, accFinancialTotalAnchorRow, accFinancialCalculationEditorMarkup, accUpdateFinancialCalculationPlacementPreview, accUpdateFinancialTotalChoiceAvailability, accFinancialSortRows, accClearFinancialDropState, accHandleFinancialDragStart, accHandleFinancialDragOver, accHandleFinancialDrop, accFinancialKey, accOpenFinancialCalculationEditor, accHandleFinancialCalculationAction, accHandleCalculationIncludeChange, accHandleFinancialOutlineAction, accRenderNotesOutlines, accLoadNoteCategories, accLoadNotesSetupData, accSaveFinancialLeaf, accSaveNoteLeaf, accApplyNoteParent, accApplyFinancialParent, accFilterFinancialPicker, accPositionFinancialPicker, accCloseFinancialPicker, accCloseFinancialPickers, accOpenFinancialPicker, accUpdateFinancialPickerSelection, accHandleFinancialPickerClick, accHandleFinancialPickerInput, accHandleFinancialPickerKeydown, accHandleFinancialChange, accHandleNoteChange, accUpdateNoteCategory, accHandleNotesOutlineAction, accNoteSubtreeAssignedCount, accHandleNotesDragStart, accHandleNotesDrop, accSetFinancialSetupModal, accSetFinancialSetupTab});
}
const MODAL = "<div id=\"accFinancialSetupModal\" class=\"acc2-modal-backdrop\" hidden><section class=\"acc2-modal acc2-modal-wide\" role=\"dialog\" aria-modal=\"true\" aria-labelledby=\"accFinancialSetupModalTitle\"><div class=\"acc2-modal-header\"><div><h2 id=\"accFinancialSetupModalTitle\">Financial Statements</h2><p>Organise statement headings and account categories.</p></div><button type=\"button\" id=\"accFinancialSetupClose\" class=\"acc2-modal-close\" aria-label=\"Close Financial Statements setup\">&times;</button></div><nav class=\"acc2-modal-tabs\" role=\"tablist\" aria-label=\"Financial Statements sections\"><button type=\"button\" class=\"acc2-modal-tab active\" id=\"accFinancialSetupTabBalanceSheet\" data-acc-financial-setup-tab=\"balance-sheet\" role=\"tab\" aria-selected=\"true\" aria-controls=\"accFinancialSetupPanelBalanceSheet\">Balance Sheet</button><button type=\"button\" class=\"acc2-modal-tab\" id=\"accFinancialSetupTabNotesBalanceSheet\" data-acc-financial-setup-tab=\"notes-balance-sheet\" role=\"tab\" aria-selected=\"false\" aria-controls=\"accFinancialSetupPanelNotesBalanceSheet\">Notes to Balance Sheet</button><button type=\"button\" class=\"acc2-modal-tab\" id=\"accFinancialSetupTabIncomeStatement\" data-acc-financial-setup-tab=\"income-statement\" role=\"tab\" aria-selected=\"false\" aria-controls=\"accFinancialSetupPanelIncomeStatement\">Income Statement</button><button type=\"button\" class=\"acc2-modal-tab\" id=\"accFinancialSetupTabNotesIncomeStatement\" data-acc-financial-setup-tab=\"notes-income-statement\" role=\"tab\" aria-selected=\"false\" aria-controls=\"accFinancialSetupPanelNotesIncomeStatement\">Notes to Income Statement</button></nav><section id=\"accFinancialSetupPanelBalanceSheet\" class=\"acc2-fs-outline\" data-fs-section=\"balance_sheet\" role=\"tabpanel\" aria-labelledby=\"accFinancialSetupTabBalanceSheet\"></section><section id=\"accFinancialSetupPanelNotesBalanceSheet\" class=\"acc2-notes-outline\" data-notes-section=\"balance_sheet\" role=\"tabpanel\" aria-labelledby=\"accFinancialSetupTabNotesBalanceSheet\" hidden></section><section id=\"accFinancialSetupPanelIncomeStatement\" class=\"acc2-fs-outline\" data-fs-section=\"income_statement\" role=\"tabpanel\" aria-labelledby=\"accFinancialSetupTabIncomeStatement\" hidden></section><section id=\"accFinancialSetupPanelNotesIncomeStatement\" class=\"acc2-notes-outline\" data-notes-section=\"income_statement\" role=\"tabpanel\" aria-labelledby=\"accFinancialSetupTabNotesIncomeStatement\" hidden></section></section></div>";
window.FinancialStatementWorkspace=Object.freeze({create});
})();
