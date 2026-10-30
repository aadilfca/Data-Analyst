"""Shared ERP financial statements; no writes or persisted report copy."""

import hashlib
import json
from collections import defaultdict
from decimal import Decimal, InvalidOperation, localcontext

from routers.trial_balance import build_account_code_hierarchy


class BalanceSheetInputError(ValueError):
    """Source/configuration cannot safely produce a financial amount."""


def _amount(value):
    # Stored TB amounts predate this engine and use REAL. Decimal(str(...)) avoids
    # adding binary arithmetic drift; it cannot restore precision already lost upstream.
    try:
        number = Decimal(str(value)) if value is not None else Decimal(0)
    except (InvalidOperation, ValueError):
        raise BalanceSheetInputError("A Trial Balance amount is invalid") from None
    if not number.is_finite():
        raise BalanceSheetInputError("A Trial Balance amount is not finite")
    return number


def _text(number):
    return format(number, "f") if number is not None else None


def financial_statement_context_key(accounts, assignments, categories, calculations, components, profit_link):
    """Compact identity invalidates confirmations after source/configuration changes."""
    groups = [accounts, assignments, categories, calculations, components]
    payload = [sorted((dict(row) for row in group), key=lambda row: json.dumps(row, sort_keys=True, default=str)) for group in groups]
    payload.append(profit_link or {})
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str, separators=(',', ':')).encode()).hexdigest()


def _equity_destination(categories, category_id):
    by_id = {int(row['id']): row for row in categories}
    row = by_id.get(category_id)
    if not row or row.get('statement_section') != 'balance_sheet' or row.get('row_type') != 'category' or not row.get('is_active'):
        return False
    parent_id = row.get('parent_category_id')
    parent = by_id.get(int(parent_id)) if parent_id is not None else None
    if parent_id is not None and (not parent or parent.get('row_type') != 'heading' or parent.get('statement_section') != 'balance_sheet' or not parent.get('is_active')):
        return False
    return (parent.get('presentation_role') if parent else row.get('presentation_role')) == 'equity'


def _profit_reconciliation(income, categories, profit_link, closing_status, context_key, confirmed_context_key):
    link = profit_link or {}
    source_id, destination_id = link.get('source_calculation_id'), link.get('target_category_id')
    source = next((row for row in income['calculations'] if row['id'] == source_id and row['calculation_type'] == 'total'), None)
    final = _amount(source['amount']) if source and source.get('amount') is not None else None
    independent = _amount(income['controls']['income_statement_net'])
    difference = final - independent if final is not None else None
    reasons = []
    if not source or final is None:
        reasons.append('Select a complete final Profit after taxation total in ACC.')
    if not _equity_destination(categories, destination_id):
        reasons.append('Select an active Balance Sheet equity category in ACC.')
    required = {int(row['id']) for row in income['categories'] if row.get('row_type') == 'category' and _amount(row.get('raw_net')) != 0}
    covered = set(source.get('source_category_ids', [])) if source else set()
    if not required.issubset(covered):
        reasons.append('The final profit total omits nonzero Income Statement categories.')
    if difference is not None and abs(difference) > Decimal('0.01'):
        reasons.append('The final Income Statement result does not reconcile to mapped TB P&L accounts.')
    if income['controls']['unmapped_nonzero_count']:
        reasons.append('Resolve nonzero unmapped Trial Balance accounts before linking profit.')
    confirmed = bool(context_key) and context_key == confirmed_context_key
    status = closing_status if confirmed else 'not_confirmed'
    if status == 'not_confirmed':
        reasons.append('Confirm this TB closing status for the current source and configuration.')
    elif status == 'already_in_capital':
        reasons.append('Profit is already included in ERP capital; no additional amount is linked.')
    elif status != 'not_in_capital':
        reasons.append('The TB closing status is invalid.')
    return dict(configured=bool(link), source_calculation_id=source_id, target_category_id=destination_id,
                final_profit=_text(final), tb_profit=_text(independent), difference=_text(difference),
                reconciled=difference is not None and abs(difference) <= Decimal('0.01'),
                closing_status=status, applied=not reasons, reasons=reasons)


def build_income_statement(accounts, assignments, categories, calculations, components,
                           controls=None, automatic_adjustment_count=0, *, profit_link=None,
                           closing_status='not_confirmed', context_key=None, confirmed_context_key=None):
    with localcontext() as context:
        context.prec = 50
        result = _build(accounts, assignments, categories, calculations, components,
                        controls or {}, automatic_adjustment_count, section='income_statement')
        result['profit_link'] = _profit_reconciliation(result, categories, profit_link, closing_status, context_key, confirmed_context_key)
        return result


def build_balance_sheet(accounts, assignments, categories, calculations, components,
                        controls=None, automatic_adjustment_count=0, *, profit_link=None,
                        closing_status='not_confirmed', context_key=None, confirmed_context_key=None):
    """Presentation precedes ACC operators; linked profit is a report-only addition."""
    with localcontext() as context:
        context.prec = 50
        reconciliation = None
        income = None
        linked_profit = None
        if profit_link:
            income = build_income_statement(accounts, assignments, categories, calculations, components,
                                            controls, automatic_adjustment_count, profit_link=profit_link,
                                            closing_status=closing_status, context_key=context_key,
                                            confirmed_context_key=confirmed_context_key)
            reconciliation = income['profit_link']
            if reconciliation['applied']:
                linked_profit = (reconciliation['target_category_id'], _amount(reconciliation['final_profit']))
        result = _build(accounts, assignments, categories, calculations, components,
                        controls or {}, automatic_adjustment_count, linked_profit=linked_profit)
        if reconciliation is not None:
            result['profit_link'] = reconciliation
            result['diagnostics'] = [item for item in result['diagnostics'] if item['code'] != 'unclosed_profit_loss']
            message = ('Current-period profit/loss linked once to capital: ' + reconciliation['final_profit']) if reconciliation['applied'] else ' '.join(reconciliation['reasons'])
            result['diagnostics'].append(dict(code='profit_link', message=message))
        result['transfer_reconciliation'] = _build_transfer_reconciliation(result, income)
        opening_items = result['opening_income_items']
        if opening_items:
            for row in result['transfer_reconciliation']['rows']:
                if row['key'] == 'profit':
                    row['label'] = 'Closing-balance profit/(loss), includes opening I/S'
            if reconciliation and reconciliation['applied']:
                for diagnostic in result['diagnostics']:
                    if diagnostic['code'] == 'profit_link':
                        diagnostic['message'] = 'Closing-balance profit/loss (including opening I/S) linked once to capital: ' + reconciliation['final_profit']
        opening_net = _amount(result['controls']['opening_income_statement_net'])
        destination = (profit_link or {}).get('target_category_id')
        valid_destination = _equity_destination(categories, destination)
        # Opening I/S is a separate draft equity line, carried through the existing
        # destination's formula lineage. It never changes the current profit link.
        opening = _build(accounts, assignments, categories, calculations, components,
                         controls or {}, automatic_adjustment_count, basis='opening',
                         linked_profit=(destination, opening_net) if opening_items and valid_destination else None)
        opening['opening_review'] = dict(amount=_text(opening_net), account_count=len(opening_items),
                                        target_category_id=destination if valid_destination else None,
                                        included=bool(opening_items) and valid_destination, accounts=opening_items)
        opening['diagnostics'] = [item for item in opening['diagnostics'] if item['code'] != 'unclosed_profit_loss']
        if opening_items:
            opening['diagnostics'].append(dict(code='opening_income_review',
                message='Opening I/S items need review; the separate draft equity line is included once.' if valid_destination
                        else 'Opening I/S items are not included in totals: select an active equity destination in ACC.'))
        opening['transfer_reconciliation'] = _build_transfer_reconciliation(opening, None, opening=True)
        bridge = dict(opening=_text(opening_net),
                      movement=_text(_amount(result['controls']['income_statement_net']) - opening_net),
                      erp_period=_text(_amount(result['controls']['period_income_statement_net'])),
                      closing=result['controls']['income_statement_net'])
        bridge['difference'] = _text(_amount(bridge['movement']) - _amount(bridge['erp_period']))
        bridge['reconciled'] = abs(_amount(bridge['difference'])) <= Decimal('0.01')
        result['transfer_reconciliation']['profit_bridge'] = bridge
        for proof in (result['transfer_reconciliation'], opening['transfer_reconciliation']):
            proof['opening_review_required'] = bool(opening_items)
            if opening_items and proof['reconciled']:
                proof['label'] += ' — opening I/S items need review'
        result['comparative'] = opening
        return result


def _build_transfer_reconciliation(balance, income, *, opening=False):
    """Compare source Leaves with actual final formula contributions, never a plug.

    Both columns use debit-positive accounting signs. Capital excludes the
    separately disclosed current result, so linked profit appears exactly once.
    """
    tolerance = Decimal('0.01')
    categories = {int(row['id']): row for row in balance['categories'] if row.get('row_type') == 'category'}
    role_ids = {role: {key for key, row in categories.items() if row.get('effective_presentation_role') == role}
                for role in ('asset', 'liability', 'equity')}
    issues = []
    tb = {role: sum((_amount(categories[key]['raw_net']) for key in keys), Decimal(0)) for role, keys in role_ids.items()}
    unknown_ids = set(categories).difference(set().union(*role_ids.values()))
    unknown_count = sum(categories[key]['account_count'] for key in unknown_ids)
    tb['unmapped'] = _amount(balance['controls']['unmapped_net']) + sum((_amount(categories[key]['raw_net']) for key in unknown_ids), Decimal(0))
    tb['profit'] = -_amount(balance['controls']['income_statement_net'])
    source_net = sum(tb.values(), Decimal(0))
    source_field = 'opening_difference' if opening else 'closing_difference'
    if source_net != _amount(balance['controls'][source_field]):
        issues.append('The source account breakdown does not match the selected TB balance control.')
    if balance['controls']['unmapped_count'] or unknown_count:
        issues.append('Some leaf accounts are unmapped, unclassified or missing a presentation role, even if their net amount is zero.')
    if not balance['controls']['leaf_count']:
        issues.append('The selected TB has no leaf accounts.')

    def select_total(roles, label):
        allowed = set().union(*(role_ids[role] for role in roles))
        required = {key for key in allowed if categories[key]['account_count'] or _amount(categories[key]['raw_net']) or _amount(categories[key].get('linked_profit'))}
        # Empty sides still need a complete, declared total rather than choosing
        # an arbitrary zero subtotal. Names/labels never identify closing totals.
        expected = required or allowed
        candidates = []
        for row in balance['calculations']:
            weights = {int(key): value for key, value in row['source_category_coefficients'].items()}
            if row['calculation_type'] == 'total' and row['amount'] is not None and weights and set(weights).issubset(allowed) and expected.issubset(weights):
                candidates.append((row, weights))
        signatures = {(tuple(sorted(weights.items())), _amount(row['amount'])) for row, weights in candidates}
        if not expected or not candidates:
            issues.append(f'No complete final {label} total can be identified from ACC category coverage.')
            return None
        if len(signatures) != 1:
            issues.append(f'The final {label} total is ambiguous; review the competing ACC total formulas.')
            return None
        row, weights = candidates[-1]
        if any(weight != 1 for weight in weights.values()):
            issues.append(f'The final {label} total subtracts or reverses a category; each signed category must contribute once.')
        # Validate the reported total independently of the lineage metadata.
        expected_amount = sum((_amount(categories[key]['amount']) * weight for key, weight in weights.items()), Decimal(0))
        if expected_amount != _amount(row['amount']):
            issues.append(f'The reported {label} total differs from its category contributions.')
        return {'id': row['id'], 'label': row['label'], 'weights': weights}

    assets_total = select_total(('asset',), 'Assets')
    equity_liabilities_total = select_total(('liability', 'equity'), 'Equity and Liabilities')
    bs = {key: None for key in ('asset', 'liability', 'equity', 'profit', 'unmapped')}
    if assets_total is not None:
        bs['asset'] = sum((_amount(categories[key]['amount']) * weight for key, weight in assets_total['weights'].items()), Decimal(0))
    if equity_liabilities_total is not None:
        weights = equity_liabilities_total['weights']
        bs['liability'] = -sum((_amount(categories[key]['amount']) * weight for key, weight in weights.items() if key in role_ids['liability']), Decimal(0))
        bs['equity'] = -sum(((_amount(categories[key]['amount']) - _amount(categories[key].get('linked_profit'))) * weight for key, weight in weights.items() if key in role_ids['equity']), Decimal(0))
        bs['profit'] = -sum((_amount(categories[key].get('linked_profit')) * weight for key, weight in weights.items() if key in role_ids['equity']), Decimal(0))

    link = balance.get('profit_link')
    if opening:
        review = balance['opening_review']
        if review['account_count'] and not review['included']:
            issues.append('Select an active Balance Sheet equity destination in ACC for the opening I/S review line.')
    elif balance['controls']['income_statement_account_count']:
        if income is not None and link and link.get('reconciled'):
            final = next((row for row in income['calculations'] if row['id'] == link['source_calculation_id']), None)
            required = {int(row['id']) for row in income['categories'] if row.get('row_type') == 'category' and row.get('account_count')}
            weights = {int(key): value for key, value in final['source_category_coefficients'].items()} if final else {}
            if not required.issubset(weights) or any(value != 1 for value in weights.values()):
                issues.append('The final Income Statement result omits or reverses mapped P&L category contributions.')
            if not link.get('applied') and not (link.get('closing_status') == 'already_in_capital' and tb['profit'] == 0):
                issues.append('Current-period profit treatment is incomplete or the closing confirmation is missing/stale.')
        else:
            issues.append('The final Income Statement result is not configured and reconciled for the selected TB.')

    for role in ('asset', 'liability', 'equity', 'profit'):
        if bs[role] is not None and abs(bs[role] - tb[role]) > tolerance:
            issues.append(f'The {role.capitalize()} transfer differs from its original TB balance.')
    if any(row['amount'] is None for row in categories.values()):
        issues.append('Some statement category amounts are unavailable; review ACC presentation settings.')
    if any(row['code'] == 'stale_controls' for row in balance['diagnostics']):
        issues.append('Stored TB validation controls are stale; revalidate before certifying completeness.')
    available = all(bs[role] is not None for role in ('asset', 'liability', 'equity', 'profit'))
    statement_net = sum((bs[role] for role in ('asset', 'liability', 'equity', 'profit')), Decimal(0)) if available else None
    unexplained = statement_net - source_net if statement_net is not None else None
    if unexplained is not None and abs(unexplained) > tolerance:
        issues.append('An unexplained difference remains between the statement and the original TB.')
    reconciled = available and unexplained is not None and abs(unexplained) <= tolerance and not issues
    source_imbalance = abs(source_net) > tolerance
    status = 'reconciled_to_tb' if reconciled and source_imbalance else 'reconciled' if reconciled else 'not_reconciled'
    labels = {'reconciled_to_tb': 'Reconciled to TB — source imbalance remains', 'reconciled': 'Reconciled', 'not_reconciled': 'Not reconciled'}
    rows = [{'key': key, 'label': ('Opening I/S items — need review' if opening and key == 'profit' else label), 'tb': _text(tb[key]), 'bs': _text(bs[key])}
            for key, label in [('asset', 'Assets'), ('liability', 'Liabilities'), ('equity', 'Capital, excluding separately shown profit'), ('profit', 'Current-period profit/(loss)'), ('unmapped', 'Unmapped/unclassified balances')]]
    rows.extend([
        {'key': 'net', 'label': 'Net difference', 'tb': _text(source_net), 'bs': _text(statement_net)},
        {'key': 'source', 'label': 'Original TB imbalance (reversed)', 'tb': _text(-source_net), 'bs': _text(-source_net)},
        {'key': 'unexplained', 'label': 'Unexplained transfer difference', 'tb': '0', 'bs': _text(unexplained)},
    ])
    return {'status': status, 'label': labels[status], 'reconciled': reconciled,
            'equation_balanced': available and abs(statement_net) <= tolerance,
            'source_imbalance': _text(source_net), 'statement_difference': _text(statement_net),
            'unexplained_difference': _text(unexplained), 'tolerance': _text(tolerance),
            'rows': rows, 'issues': list(dict.fromkeys(issues)),
            'totals': {key: {field: total[field] for field in ('id', 'label')} if total else None
                       for key, total in [('assets', assets_total), ('equity_liabilities', equity_liabilities_total)]}}


def _build(accounts, assignments, categories, calculations, components, controls, adjustment_count,
           section='balance_sheet', linked_profit=None, basis='closing'):
    statement_name = "Income Statement" if section == "income_statement" else "Balance Sheet"
    hierarchy = build_account_code_hierarchy(row["account_code"] for row in accounts)
    by_category = {int(row["id"]): dict(row) for row in categories}
    mapping = {str(row["account_code"]).strip(): row.get("financial_statement_category_id") for row in assignments}
    if len(mapping) != len(assignments):
        raise BalanceSheetInputError("Duplicate ACC account mappings require review")
    seen = set()
    raw = defaultdict(Decimal)
    counts = defaultdict(int)
    total_dr = Decimal(0)
    total_cr = Decimal(0)
    profit = Decimal(0)
    unmapped = []
    unmapped_nonzero_count = 0
    unmapped_net = Decimal(0)
    income_account_count = 0
    opening_profit_count = 0
    opening_income_items = []
    opening_profit = Decimal(0)
    period_profit = Decimal(0)
    inactive = []
    rollforward_count = 0
    leaf_count = 0
    current_classifications = []
    for account in accounts:
        code = str(account["account_code"]).strip()
        if code in seen:
            raise BalanceSheetInputError("Duplicate Trial Balance account codes require review")
        seen.add(code)
        classification = account["classification"] if account.get("manually_overridden") else hierarchy["classification"][code]
        current_classifications.append([code, classification])
        if classification != "leaf":
            continue
        leaf_count += 1
        debit, credit = _amount(account.get(basis + "_dr")), _amount(account.get(basis + "_cr"))
        net = debit - credit
        total_dr += debit
        total_cr += credit
        expected = _amount(account.get("opening_dr")) - _amount(account.get("opening_cr")) + _amount(account.get("period_dr")) - _amount(account.get("period_cr"))
        if basis == 'closing' and abs(expected - net) > Decimal("0.01"):
            rollforward_count += 1
        category_id = mapping.get(code)
        category = by_category.get(int(category_id)) if category_id is not None else None
        if not category or category.get("row_type") != "category" or category.get("statement_section") not in {"balance_sheet", "income_statement"}:
            unmapped.append(code)
            unmapped_net += net
            if net:
                unmapped_nonzero_count += 1
            continue
        if category.get("is_active") is False or category.get("is_active") == 0:
            inactive.append(code)
        if category["statement_section"] == "income_statement":
            income_account_count += 1
            profit -= net
            opening_dr, opening_cr = _amount(account.get('opening_dr')), _amount(account.get('opening_cr'))
            opening_profit += opening_cr - opening_dr
            period_profit += _amount(account.get('period_cr')) - _amount(account.get('period_dr'))
            if opening_dr - opening_cr:
                opening_profit_count += 1
                opening_income_items.append(dict(account_code=code, category=category['label'],
                                                 opening_dr=_text(opening_dr), opening_cr=_text(opening_cr),
                                                 equity_effect=_text(opening_cr - opening_dr)))
        raw[int(category["id"])] += net
        counts[int(category["id"])] += 1

    statement = [dict(row) for row in categories if row.get("statement_section") == section]
    values = {}
    missing_roles = []
    abnormal = []
    for row in statement:
        if row.get("row_type") == "heading":
            continue
        key = int(row["id"])
        parent_id = row.get("parent_category_id")
        parent = by_category.get(int(parent_id)) if parent_id is not None else None
        role = parent.get("presentation_role") if parent and parent.get("row_type") == "heading" and parent.get("statement_section") == "balance_sheet" else row.get("presentation_role") if parent_id is None else None
        value = raw[key] * (1 if role == "asset" else -1) if role in {"asset", "liability", "equity"} else None
        if section == "income_statement":
            value = -raw[key]
        erp_amount = value
        if linked_profit and key == linked_profit[0] and value is not None:
            value += linked_profit[1]
        if value is None:
            missing_roles.append(row["label"])
        elif value < 0:
            abnormal.append(row["label"])
        values[key] = value
        row.update(raw_net=_text(raw[key]), amount=_text(value), account_count=counts[key], effective_presentation_role=role, erp_amount=_text(erp_amount),
                   linked_profit=_text(linked_profit[1]) if linked_profit and key == linked_profit[0] else None)

    calc_by_id = {int(row["id"]): dict(row) for row in calculations if row.get("statement_section") == section}
    saved = defaultdict(list)
    for part in components:
        saved[int(part["calculation_id"])].append(dict(part))
    computed = {}
    visiting = set()
    problems = []
    subtraction_reviews = set()
    members = {}
    coefficients = {}

    def evaluate(key):
        if key in computed:
            return computed[key]
        if key in visiting or key not in calc_by_id:
            raise BalanceSheetInputError(f"{statement_name}: a total has a circular or missing source")
        visiting.add(key)
        calculation = calc_by_id[key]
        parts = saved[key]
        if calculation["calculation_type"] == "subtotal":
            operators = {int(part["source_category_id"]): part["operator"] for part in parts if part.get("source_category_id") is not None}
            parts = [{"source_category_id": row["id"], "source_calculation_id": None, "operator": operators.get(int(row["id"]), "add")}
                     for row in statement if row.get("row_type") == "category" and row.get("parent_category_id") == calculation.get("parent_heading_id")]
        calculation["components"] = parts
        subtotal = Decimal(0)
        complete = bool(parts)
        covered = set()
        weights = {}
        for part in parts:
            category_id, source_id = part.get("source_category_id"), part.get("source_calculation_id")
            if (category_id is None) == (source_id is None) or part.get("operator") not in {"add", "subtract"}:
                raise BalanceSheetInputError(f"{statement_name}: a total has an invalid source or operator")
            if category_id is not None:
                category_id = int(category_id)
                if category_id not in values:
                    raise BalanceSheetInputError(f"{statement_name}: a total references an unavailable category")
                value, source_members = values[category_id], {category_id}
                source_weights = {category_id: 1}
                if value is not None and value < 0 and part["operator"] == "subtract":
                    subtraction_reviews.add(by_category[category_id]["label"])
            else:
                source_id = int(source_id)
                value = evaluate(source_id)
                source_members = members[source_id]
                source_weights = coefficients[source_id]
            if covered.intersection(source_members):
                raise BalanceSheetInputError(f"{statement_name}: a total includes an account category more than once")
            covered.update(source_members)
            factor = -1 if part["operator"] == "subtract" else 1
            weights.update({category: weight * factor for category, weight in source_weights.items()})
            if value is None:
                complete = False
            else:
                subtotal += value * (-1 if part["operator"] == "subtract" else 1)
        members[key] = covered
        coefficients[key] = weights
        visiting.remove(key)
        computed[key] = subtotal if complete else None
        if not complete:
            problems.append(calculation["label"])
        return computed[key]

    for key, calculation in calc_by_id.items():
        calculation["amount"] = _text(evaluate(key))
        calculation["source_category_ids"] = sorted(members[key])
        calculation["source_category_coefficients"] = {str(category): weight for category, weight in sorted(coefficients[key].items())}

    diagnostics = []
    def notice(code, message, count=None):
        diagnostics.append({"code": code, "message": message, "count": count})
    if not leaf_count:
        notice("no_leaf_accounts", "The selected Trial Balance has no leaf accounts; amounts are unavailable.")
        for row in statement:
            if row.get("row_type") != "heading":
                row["amount"] = None
        for row in calc_by_id.values():
            row["amount"] = None
    if unmapped:
        notice("unmapped_accounts", f"{len(unmapped)} leaf accounts have no usable Financial Statement assignment. Review them in ACC.", len(unmapped))
    if inactive:
        notice("inactive_assignments", f"{len(inactive)} accounts use inactive categories; their balances remain included for review.", len(inactive))
    if missing_roles:
        notice("missing_presentation", "Set Asset, Liability or Equity in ACC for: " + "; ".join(missing_roles[:20]), len(missing_roles))
    difference = total_dr - total_cr
    if abs(difference) > Decimal("0.01"):
        notice("source_imbalance", "ERP " + basis + " debits and credits do not balance. Difference: " + _text(difference))
    global_count = len(controls.get("global_adjustments") or [])
    if basis == 'closing' and (adjustment_count or global_count or rollforward_count):
        notice("excluded_differences", f"Automatic TB differences are excluded: {adjustment_count} saved leaf entries, {global_count} global entries; {rollforward_count} current leaf roll-forward differences require review.")
    saved_classifications = controls.get("account_classifications")
    if controls.get("stale") or (saved_classifications and sorted(current_classifications) != sorted(saved_classifications)):
        notice("stale_controls", "Stored TB validation controls are stale; revalidate the source.")
    if profit and section == "balance_sheet":
        notice("unclosed_profit_loss", "Income Statement account net (credit-positive): " + _text(profit) + ". Review closing status; this amount is not automatically added to equity.")
    if abnormal and section == "balance_sheet":
        notice("negative_presented_balances", "Negative presented balances retained for review: " + "; ".join(abnormal[:20]), len(abnormal))
    if subtraction_reviews:
        notice("subtract_negative", "ACC subtracts already-negative categories; review these signs: " + "; ".join(sorted(subtraction_reviews)[:20]))
    if opening_profit_count and section == "income_statement":
        notice("opening_profit_balances", f"{opening_profit_count} P&L accounts have nonzero opening balances. Closing-balance profit may include earlier periods; review the reporting period.", opening_profit_count)
    if problems:
        notice("incomplete_totals", "Some totals are unavailable because their sources are incomplete or unconfigured.", len(problems))
    return {"basis": "erp_" + basis + "_before_audit_adjustments", "statement_section": section, "categories": statement,
            "calculations": list(calc_by_id.values()), "diagnostics": diagnostics,
            "opening_income_items": opening_income_items,
            "controls": {basis + "_dr": _text(total_dr), basis + "_cr": _text(total_cr),
                         basis + "_difference": _text(difference), "leaf_count": leaf_count,
                         "opening_income_statement_net": _text(opening_profit), "period_income_statement_net": _text(period_profit),
                         "unmapped_net": _text(unmapped_net), "income_statement_account_count": income_account_count,
                         "unmapped_count": len(unmapped), "unmapped_nonzero_count": unmapped_nonzero_count, "unmapped_account_codes": unmapped[:50],
                         "income_statement_net": _text(profit), "audit_adjustment_net": "0"}}
