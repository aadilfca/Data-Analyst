from services.cashflow_voucher_canonicalization import (
    STATUS_CURRENT,
    STATUS_CURRENT_REVISED,
    STATUS_EXACT_DUPLICATE,
    STATUS_REVIEW_REQUIRED,
    STATUS_SUPERSEDED,
    build_voucher_occurrences,
    resolve_voucher_occurrences,
    voucher_fingerprint,
)


def _voucher_rows(upload_id, voucher_id, *, amount=100, source_sequence=None, names=("Bank", "Customer")):
    return [
        {
            "client_id": 5,
            "template_id": 1,
            "upload_id": upload_id,
            "source_sequence": source_sequence if source_sequence is not None else upload_id,
            "voucher_id": voucher_id,
            "transaction_date": "2026-01-23",
            "account_code": "10-15-115-15-0001",
            "account_name": names[0],
            "debit": amount,
            "credit": 0,
            "treasury_type": "bank",
            "voucher_detail": "Bank receipt",
        },
        {
            "client_id": 5,
            "template_id": 1,
            "upload_id": upload_id,
            "source_sequence": source_sequence if source_sequence is not None else upload_id,
            "voucher_id": voucher_id,
            "transaction_date": "2026-01-23",
            "account_code": "10-15-55-10-10-0001",
            "account_name": names[1],
            "debit": 0,
            "credit": amount,
            "treasury_type": None,
            "voucher_detail": "Customer receipt",
        },
    ]


def _resolved(*row_groups):
    return resolve_voucher_occurrences(build_voucher_occurrences(row for rows in row_groups for row in rows))


def test_exact_voucher_copy_is_retained_for_audit_but_only_one_occurrence_is_current():
    result = _resolved(
        _voucher_rows(100, " BRV-0126-159 ", names=("Meezan Bank", "RDX Inc")),
        _voucher_rows(200, "brv-0126-159", names=("Leathercrop (Meezan)", "RDX INC LIMITED")),
    )

    first, latest = result
    assert first["decision_status"] == STATUS_EXACT_DUPLICATE
    assert latest["decision_status"] == STATUS_CURRENT
    assert first["voucher_fingerprint"] == latest["voucher_fingerprint"]
    assert first["canonical_occurrence_key"] == latest["occurrence_key"]
    assert latest["source_line_count"] == 2


def test_changed_voucher_version_supersedes_prior_version_without_deleting_it():
    result = _resolved(
        _voucher_rows(100, "BRV-0126-159", amount=100),
        _voucher_rows(200, "BRV-0126-159", amount=125),
    )

    first, latest = result
    assert first["decision_status"] == STATUS_SUPERSEDED
    assert latest["decision_status"] == STATUS_CURRENT_REVISED
    assert first["voucher_fingerprint"] != latest["voucher_fingerprint"]
    assert first["canonical_occurrence_key"] == latest["occurrence_key"]


def test_missing_voucher_identifier_requires_review_and_is_not_matched_weakly():
    result = _resolved(_voucher_rows(100, ""))

    assert result[0]["decision_status"] == STATUS_REVIEW_REQUIRED
    assert result[0]["decision_reason"] == "missing_voucher_id"
    assert result[0]["canonical_occurrence_key"] is None


def test_different_voucher_ids_are_not_auto_suppressed_even_when_economics_match():
    result = _resolved(
        _voucher_rows(100, "BRV-0126-107"),
        _voucher_rows(200, "BRV-0126-135"),
    )

    assert [item["decision_status"] for item in result] == [STATUS_CURRENT, STATUS_CURRENT]
    assert {item["voucher_identity"] for item in result} == {"BRV-0126-107", "BRV-0126-135"}


def test_fingerprint_uses_accounting_substance_not_display_or_cashflow_labels():
    first = _voucher_rows(100, "BRV-0126-159", names=("Bank one", "Customer one"))
    second = _voucher_rows(200, "BRV-0126-159", names=("Bank renamed", "Customer renamed"))
    for row in second:
        row["treasury_type"] = "cash" if row["debit"] else "bank"
        row["voucher_detail"] = "Different narration"

    assert voucher_fingerprint(first) == voucher_fingerprint(second)