"""Canonical Cashflows voucher identity and duplicate-resolution helpers.

DV-1 deliberately has no database writes or report-path dependency.  It produces
stable occurrence decisions that later phases can persist after an upload and
consume through an indexed canonical-voucher join.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
from typing import Any, Iterable, Mapping


FINGERPRINT_VERSION = 1
STATUS_CURRENT = "current"
STATUS_CURRENT_REVISED = "current_revised"
STATUS_EXACT_DUPLICATE = "exact_duplicate"
STATUS_SUPERSEDED = "superseded"
STATUS_REVIEW_REQUIRED = "review_required"


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def normalize_voucher_id(value: Any) -> str:
    """Return the configured-source voucher identifier in a stable form."""
    return re.sub(r"\s+", "", _text(value)).upper()


def _normalize_account_code(value: Any) -> str:
    return re.sub(r"\s+", "", _text(value)).upper()


def _date_text(value: Any) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return _text(value).replace("T", " ").split(" ", 1)[0]


def _amount_text(value: Any) -> str:
    try:
        amount = Decimal(str(0 if value in (None, "") else value))
    except (InvalidOperation, ValueError, TypeError):
        amount = Decimal("0")
    if not amount.is_finite():
        raise ValueError("Voucher fingerprint amounts must be finite")
    text = format(amount.normalize(), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _source_priority(value: Any) -> tuple[int, Decimal | str]:
    try:
        return (1, Decimal(str(value)))
    except (InvalidOperation, ValueError, TypeError):
        return (0, _text(value))


def voucher_fingerprint(lines: Iterable[Mapping[str, Any]]) -> str:
    """Hash the accounting substance of one complete voucher occurrence.

    Display labels, narration, Cashflows categories, and treasury classifications
    are intentionally excluded: configuration or spelling changes must not create
    another accounting voucher.
    """
    canonical_lines = [
        {
            "account_code": _normalize_account_code(line.get("account_code")),
            "credit": _amount_text(line.get("credit")),
            "date": _date_text(line.get("transaction_date", line.get("date"))),
            "debit": _amount_text(line.get("debit")),
        }
        for line in lines
    ]
    canonical_lines.sort(key=lambda line: (
        line["date"], line["account_code"], line["debit"], line["credit"]
    ))
    payload = json.dumps(
        {"fingerprint_version": FINGERPRINT_VERSION, "lines": canonical_lines},
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_voucher_occurrences(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Group classified source lines into one immutable voucher occurrence per upload."""
    grouped: dict[tuple[Any, Any, Any, str], list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        client_id = row.get("client_id")
        template_id = row.get("template_id")
        upload_id = row.get("upload_id")
        identity = normalize_voucher_id(row.get("voucher_id"))
        supplied_key = _text(row.get("source_occurrence_key"))
        group_key = identity or supplied_key or "__missing_voucher_id__"
        grouped[(client_id, template_id, upload_id, group_key)].append(row)

    occurrences: list[dict[str, Any]] = []
    for (client_id, template_id, upload_id, group_key), lines in grouped.items():
        identity = normalize_voucher_id(lines[0].get("voucher_id"))
        dates = sorted({_date_text(line.get("transaction_date", line.get("date"))) for line in lines if _date_text(line.get("transaction_date", line.get("date")))})
        fingerprint = voucher_fingerprint(lines)
        occurrences.append(
            {
                "client_id": client_id,
                "template_id": template_id,
                "upload_id": upload_id,
                "voucher_id": _text(lines[0].get("voucher_id")),
                "voucher_identity": identity or None,
                "occurrence_key": f"upload:{upload_id}:voucher:{identity}" if identity else f"upload:{upload_id}:missing:{group_key}:{fingerprint}",
                "voucher_fingerprint": fingerprint,
                "fingerprint_version": FINGERPRINT_VERSION,
                "transaction_date": dates[0] if len(dates) == 1 else None,
                "source_line_count": len(lines),
                "has_multiple_dates": len(dates) > 1,
                "source_sequence": max((line.get("source_sequence", upload_id) for line in lines), key=_source_priority),
            }
        )
    return occurrences


def resolve_voucher_occurrences(occurrences: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Assign deterministic, audit-preserving statuses to voucher occurrences.

    The latest accepted source sequence wins only where the voucher identity is
    present. Missing identifiers and internally inconsistent voucher dates remain
    review-required rather than being matched by a weak heuristic.
    """
    indexed = [dict(occurrence, _input_index=index) for index, occurrence in enumerate(occurrences)]
    by_identity: dict[tuple[Any, Any, str], list[dict[str, Any]]] = defaultdict(list)
    resolved: list[dict[str, Any]] = []

    for occurrence in indexed:
        identity = _text(occurrence.get("voucher_identity"))
        if not identity or occurrence.get("has_multiple_dates"):
            occurrence["decision_status"] = STATUS_REVIEW_REQUIRED
            occurrence["decision_reason"] = "missing_voucher_id" if not identity else "multiple_dates_for_same_voucher_id"
            occurrence["canonical_occurrence_key"] = None
            resolved.append(occurrence)
            continue
        by_identity[(occurrence.get("client_id"), occurrence.get("template_id"), identity)].append(occurrence)

    for group in by_identity.values():
        ordered = sorted(
            group,
            key=lambda occurrence: (
                _source_priority(occurrence.get("source_sequence", occurrence.get("upload_id"))),
                int(occurrence.get("upload_id") or 0),
                _text(occurrence.get("occurrence_key")),
            ),
        )
        canonical = ordered[-1]
        signatures = {str(occurrence.get("voucher_fingerprint") or "") for occurrence in ordered}
        canonical_key = _text(canonical.get("occurrence_key"))
        for occurrence in ordered:
            occurrence["canonical_occurrence_key"] = canonical_key
            if occurrence is canonical:
                occurrence["decision_status"] = STATUS_CURRENT_REVISED if len(signatures) > 1 else STATUS_CURRENT
                occurrence["decision_reason"] = "newer_source_version_replaces_prior_fingerprint" if len(signatures) > 1 else "first_or_latest_exact_voucher_version"
            elif occurrence.get("voucher_fingerprint") == canonical.get("voucher_fingerprint"):
                occurrence["decision_status"] = STATUS_EXACT_DUPLICATE
                occurrence["decision_reason"] = "same_voucher_identity_and_fingerprint"
            else:
                occurrence["decision_status"] = STATUS_SUPERSEDED
                occurrence["decision_reason"] = "older_voucher_fingerprint_replaced_by_newer_source"
            resolved.append(occurrence)

    resolved.sort(key=lambda occurrence: occurrence["_input_index"])
    for occurrence in resolved:
        occurrence.pop("_input_index", None)
    return resolved