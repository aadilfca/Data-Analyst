"""Shared ERP-closing financial statements; no writes or persisted report copy."""

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
        return result


def _build(accounts, assignments, categories, calculations, components, controls, adjustment_count,
           section='balance_sheet', linked_profit=None):
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
    opening_profit_count = 0
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
        debit, credit = _amount(account.get("closing_dr")), _amount(account.get("closing_cr"))
        net = debit - credit
        total_dr += debit
        total_cr += credit
        expected = _amount(account.get("opening_dr")) - _amount(account.get("opening_cr")) + _amount(account.get("period_dr")) - _amount(account.get("period_cr"))
        if abs(expected - net) > Decimal("0.01"):
            rollforward_count += 1
        category_id = mapping.get(code)
        category = by_category.get(int(category_id)) if category_id is not None else None
        if not category or category.get("row_type") != "category" or category.get("statement_section") not in {"balance_sheet", "income_statement"}:
            unmapped.append(code)
            if net:
                unmapped_nonzero_count += 1
            continue
        if category.get("is_active") is False or category.get("is_active") == 0:
            inactive.append(code)
        if category["statement_section"] == "income_statement":
            profit -= net
            if _amount(account.get("opening_dr")) - _amount(account.get("opening_cr")):
                opening_profit_count += 1
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
        for part in parts:
            category_id, source_id = part.get("source_category_id"), part.get("source_calculation_id")
            if (category_id is None) == (source_id is None) or part.get("operator") not in {"add", "subtract"}:
                raise BalanceSheetInputError(f"{statement_name}: a total has an invalid source or operator")
            if category_id is not None:
                category_id = int(category_id)
                if category_id not in values:
                    raise BalanceSheetInputError(f"{statement_name}: a total references an unavailable category")
                value, source_members = values[category_id], {category_id}
                if value is not None and value < 0 and part["operator"] == "subtract":
                    subtraction_reviews.add(by_category[category_id]["label"])
            else:
                source_id = int(source_id)
                value = evaluate(source_id)
                source_members = members[source_id]
            if covered.intersection(source_members):
                raise BalanceSheetInputError(f"{statement_name}: a total includes an account category more than once")
            covered.update(source_members)
            if value is None:
                complete = False
            else:
                subtotal += value * (-1 if part["operator"] == "subtract" else 1)
        members[key] = covered
        visiting.remove(key)
        computed[key] = subtotal if complete else None
        if not complete:
            problems.append(calculation["label"])
        return computed[key]

    for key, calculation in calc_by_id.items():
        calculation["amount"] = _text(evaluate(key))
        calculation["source_category_ids"] = sorted(members[key])

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
        notice("source_imbalance", "ERP closing debits and credits do not balance. Difference: " + _text(difference))
    global_count = len(controls.get("global_adjustments") or [])
    if adjustment_count or global_count or rollforward_count:
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
    return {"basis": "erp_closing_before_audit_adjustments", "statement_section": section, "categories": statement,
            "calculations": list(calc_by_id.values()), "diagnostics": diagnostics,
            "controls": {"closing_dr": _text(total_dr), "closing_cr": _text(total_cr),
                         "closing_difference": _text(difference), "leaf_count": leaf_count,
                         "unmapped_count": len(unmapped), "unmapped_nonzero_count": unmapped_nonzero_count, "unmapped_account_codes": unmapped[:50],
                         "income_statement_net": _text(profit), "audit_adjustment_net": "0"}}
