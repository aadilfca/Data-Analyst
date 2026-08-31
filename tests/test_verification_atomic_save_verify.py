import os
import tempfile
from contextlib import contextmanager

import pytest

import database
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
        if kwargs.get("compute_status") is False:
            calls.append("save_without_status")
        return save_result

    monkeypatch.setattr(verification, "_save_line_items_impl", save)

    def verify(*args, **kwargs):
        calls.append("verify")
        if verify_error:
            raise verify_error
        return verify_result

    monkeypatch.setattr(verification, "_verify_voucher_checks_impl", verify)
    monkeypatch.setattr(verification, "load_verification_rulebook_for_session",
                        lambda session_id: {"voucher_checks": []})
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

    assert result["verified"] is True
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


def test_voucher_bundle_saves_all_lines_then_verifies_once(monkeypatch):
    calls = _patch_atomic_dependencies(monkeypatch, verify_result={"verified": True})
    monkeypatch.setattr(verification, "execute_query_pg", lambda query, params=None: (
        [{"client_id": 1}] if "FROM verification_sessions" in query
        else ([{"id": detail_id, "status": "Pending"} for detail_id in (params[0] if params else [])]
              if "SELECT id, status" in query else [])
    ))

    result = verification.save_and_verify_voucher_bundle(
        session_id=10,
        current_detail_id=7,
        lines=[
            {"detail_id": 7, "line_items": [], "procedure_data": {}},
            {"detail_id": 8, "line_items": [], "procedure_data": {}},
        ],
    )

    assert result["verified"] is True
    assert calls == ["begin", "save_without_status", "save_without_status", "verify", "commit"]


# --- HTTP-level coverage -----------------------------------------------------------------------
# The tests above call save_and_verify_voucher_bundle() as a plain Python function, which cannot
# catch a JSON-key mismatch between the frontend payload and the endpoint's Body(embed=True)
# params — exactly the gap that let the camelCase/snake_case `voucher_checks` bug ship (root-caused
# 2026-08-28: voucher_checks always arrived empty, so every active check read as untouched and the
# verify gate 400'd unconditionally). These push a real JSON body through the FastAPI HTTP layer.

BUNDLE_URL = "/api/verification/save_and_verify_voucher_bundle"


@pytest.fixture()
def bundle_http_client(monkeypatch):
    """TestClient over a throwaway temp DB (never the real test.db) — same isolation pattern as
    tests/test_app_smoke.py. All Postgres access in routers.verification is stubbed per-test."""
    with tempfile.TemporaryDirectory() as tmpdir:
        monkeypatch.setattr(database, "DB_NAME", os.path.join(tmpdir, "test.db"))
        database.init_db()
        from fastapi.testclient import TestClient
        import main
        yield TestClient(main.app)


def _stub_bundle_pg(monkeypatch, active_checks):
    """Stub the PG helpers the bundle endpoint touches. Returns the `reached` list, which gets a
    'verify' entry only if the endpoint runs past the voucher-check gate into the verify call."""
    reached = []

    def execute_query_pg(query, params=None):
        if "FROM verification_details" in query:
            if "client_data_id" in query:
                return [{"id": 7, "client_data_id": 100}]
            return [{"id": 7, "status": "Pending"}]
        if "FROM verification_sessions" in query:
            return [{"client_id": 1}]
        if "FROM audit_rule_voucher_checks" in query:
            return active_checks
        return []

    @contextmanager
    def transaction():
        yield object()

    monkeypatch.setattr(verification, "execute_query_pg", execute_query_pg)
    monkeypatch.setattr(verification, "load_verification_rulebook_for_session",
                        lambda session_id: {"voucher_checks": active_checks})
    monkeypatch.setattr(verification, "execute_write_pg", lambda *a, **k: None)
    monkeypatch.setattr(verification, "pg_transaction", transaction)
    monkeypatch.setattr(verification, "_save_line_items_impl", lambda *a, **k: {"cleared_status": "Cleared"})
    monkeypatch.setattr(verification, "compute_and_persist_cleared_status",
                        lambda *a, **k: {"cleared_status": "Cleared",
                                         "matrix_version_applied": 1})
    monkeypatch.setattr(verification, "save_voucher_checks_batch", lambda *a, **k: None)

    def verify_impl(*a, **k):
        reached.append("verify")
        return {"verified": True}

    monkeypatch.setattr(verification, "_verify_voucher_checks_impl", verify_impl)
    return reached


def _bundle_body(**overrides):
    body = {
        "session_id": 10,
        "current_detail_id": 7,
        "lines": [{"detail_id": 7, "line_items": [], "procedure_data": {},
                   "transaction_type": "Purchases", "transaction_ref": "CPV-1"}],
    }
    body.update(overrides)
    return body


def test_bundle_endpoint_success_with_snake_case_voucher_checks(bundle_http_client, monkeypatch):
    reached = _stub_bundle_pg(monkeypatch, active_checks=[{"id": 1, "name": "Check A"}])

    response = bundle_http_client.post(BUNDLE_URL, json=_bundle_body(
        voucher_checks=[{"check_id": 1, "state": "pass"}]))

    assert response.status_code == 200
    assert response.json() == {
        "verified": True,
        "cleared_status": "Cleared",
        "matrix_version_applied": 1,
        "line_statuses": {"7": "Cleared"},
    }
    assert reached == ["verify"]


def test_bundle_endpoint_400_when_voucher_checks_missing(bundle_http_client, monkeypatch):
    reached = _stub_bundle_pg(monkeypatch, active_checks=[{"id": 1, "name": "Check A"}])

    response = bundle_http_client.post(BUNDLE_URL, json=_bundle_body())

    assert response.status_code == 400
    assert "Cannot verify" in response.json()["detail"]
    assert reached == []


def test_bundle_endpoint_400_when_voucher_checks_use_wrong_key(bundle_http_client, monkeypatch):
    # camelCase `checkId` instead of `check_id` — the endpoint reads c.get('check_id'), so the
    # submitted state never matches an active check and the check reads as untouched. This is the
    # exact regression the frontend fix addressed; pin it at the HTTP layer.
    reached = _stub_bundle_pg(monkeypatch, active_checks=[{"id": 1, "name": "Check A"}])

    response = bundle_http_client.post(BUNDLE_URL, json=_bundle_body(
        voucher_checks=[{"checkId": 1, "state": "pass"}]))

    assert response.status_code == 400
    assert "Cannot verify" in response.json()["detail"]
    assert reached == []


def test_voucher_bundle_accepts_modal_state(monkeypatch):
    calls = _patch_atomic_dependencies(monkeypatch, verify_result={"verified": True})
    monkeypatch.setattr(verification, "execute_query_pg", lambda query, params=None: (
        [{"client_id": 1}] if "FROM verification_sessions" in query
        else ([{"id": detail_id, "status": "Pending"} for detail_id in (params[0] if params else [])]
              if "SELECT id, status" in query else [])
    ))
    monkeypatch.setattr(verification, "execute_write_pg", lambda *args, **kwargs: None)
    monkeypatch.setattr(verification, "_resolve_sibling_voucher_line_ids", lambda detail_id: [detail_id])
    monkeypatch.setattr(verification, "sync_line_observations_to_voucher", lambda *args, **kwargs: None)
    result = verification.save_and_verify_voucher_bundle(
        session_id=10, current_detail_id=7,
        lines=[{"detail_id": 7, "line_items": [], "procedure_data": {}}],
        observations=[{"voucher_line_id": 7, "document_id": 3, "observation_text": "note"}],
        account_head_notes=[{"voucher_line_id": 7, "observation_text": "head note"}],
        physical_bills=[]
    )
    assert result["verified"] is True
    assert calls == ["begin", "save_without_status", "verify", "commit"]


def test_status_engine_uses_session_rulebook_loader_when_not_preloaded(monkeypatch):
    loaded_sessions = []

    monkeypatch.setattr(
        verification,
        "load_verification_rulebook_for_session",
        lambda session_id: loaded_sessions.append(session_id) or {
            "categories": [],
            "voucher_checks": [],
        },
    )
    monkeypatch.setattr(
        verification,
        "execute_query_pg",
        lambda query, params=None: [{
            "id": 7,
            "session_id": 10,
            "transaction_ref": "CPV-1",
            "row_data": "{}",
            "extract_amount": None,
            "client_id": 1,
            "category_id_applied": None,
            "category_name_applied": None,
        }],
    )

    result = verification.compute_and_persist_cleared_status(7, sibling_ids=[7])

    assert result is None
    assert loaded_sessions == [10]
