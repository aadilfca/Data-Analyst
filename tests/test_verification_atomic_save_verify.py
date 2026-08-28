from contextlib import contextmanager

import pytest

import routers.verification as verification


def _patch_atomic_dependencies(monkeypatch, save_result=None, verify_result=None,
                               save_error=None, verify_error=None):
    calls = []

    @contextmanager
    def transaction():
        calls.append("begin")
        try:
            yield object()
        except Exception:
            calls.append("rollback")
            raise
        else:
            calls.append("commit")

    monkeypatch.setattr(verification, "pg_transaction", transaction)
    def save(*args, **kwargs):
        if save_error:
            raise save_error
        return save_result

    monkeypatch.setattr(verification, "_save_line_items_impl", save)

    def verify(*args, **kwargs):
        calls.append("verify")
        if verify_error:
            raise verify_error
        return verify_result

    monkeypatch.setattr(verification, "_verify_voucher_checks_impl", verify)
    return calls


def test_atomic_save_and_verify_commits_only_after_both_operations(monkeypatch):
    calls = _patch_atomic_dependencies(
        monkeypatch,
        save_result={"cleared_status": "Cleared"},
        verify_result={"verified": True},
    )

    result = verification.save_and_verify_voucher(
        session_id=10,
        current_detail_id=7,
        detail_id=7,
        line_items=[],
    )

    assert result == {"verified": True}
    assert calls == ["begin", "verify", "commit"]


@pytest.mark.parametrize("stage", ["save", "verify"])
def test_atomic_save_and_verify_rolls_back_when_either_stage_fails(monkeypatch, stage):
    failure = RuntimeError(f"{stage} failed")
    kwargs = {"save_error": failure} if stage == "save" else {"verify_error": failure}
    calls = _patch_atomic_dependencies(monkeypatch, **kwargs)

    with pytest.raises(RuntimeError, match=str(failure)):
        verification.save_and_verify_voucher(
            session_id=10,
            current_detail_id=7,
            detail_id=7,
            line_items=[],
        )

    expected = ["begin", "rollback"] if stage == "save" else ["begin", "verify", "rollback"]
    assert calls == expected


def test_atomic_save_and_verify_rejects_mismatched_detail_ids_before_transaction(monkeypatch):
    calls = _patch_atomic_dependencies(monkeypatch)

    with pytest.raises(verification.HTTPException) as error:
        verification.save_and_verify_voucher(
            session_id=10,
            current_detail_id=7,
            detail_id=8,
            line_items=[],
        )

    assert error.value.status_code == 400
    assert calls == []
