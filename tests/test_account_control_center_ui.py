"""Legacy Account Control Centre bookmark and API-cutover contracts."""

from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from main import app


@pytest.mark.parametrize(
    ("query", "workflow", "upload_id"),
    [
        ("tab=financial&upload_id=77", "financial", "77"),
        ("tab=notes&upload_id=77", "financial", "77"),
        ("tab=cashflow&upload_id=88", "cashflow", "88"),
        ("tab=verification&upload_id=99", "verification", "99"),
        ("", "verification", None),
        ("tab=unknown&upload_id=bad", "verification", None),
    ],
)
def test_legacy_acc_bookmarks_redirect_to_scoped_trial_balance(query, workflow, upload_id):
    response = TestClient(app).get(
        "/static/account_control_center.html" + ("?" + query if query else ""),
        follow_redirects=False,
    )

    assert response.status_code == 302
    destination = urlparse(response.headers["location"])
    values = parse_qs(destination.query)
    assert destination.path == "/static/trial_balance.html"
    assert values["view"] == ["available"]
    assert values["workflow"] == [workflow]
    if upload_id:
        assert values["upload_id"] == [upload_id]
    else:
        assert "upload_id" not in values


@pytest.mark.parametrize(
    ("setup", "workflow"),
    [
        ("financial", "financial"),
        ("notes", "financial"),
        ("cashflow", "cashflow"),
        ("treasury", "cashflow"),
        ("template", "cashflow"),
        ("verification", "verification"),
    ],
)
def test_legacy_acc_setup_bookmarks_keep_their_workspace(setup, workflow):
    response = TestClient(app).get(
        "/static/account_control_center.html?setup=" + setup + "&upload_id=77",
        follow_redirects=False,
    )

    values = parse_qs(urlparse(response.headers["location"]).query)
    assert response.status_code == 302
    assert values["workflow"] == [workflow]
    assert values["setup"] == [setup]
    assert values["upload_id"] == ["77"]


def test_retired_acc_assets_are_not_shipped_and_api_router_remains_available():
    root = Path(__file__).resolve().parents[1]
    assert not (root / "static" / "account_control_center.html").exists()
    assert not (root / "static" / "account_control_center.js").exists()

    paths = set(app.openapi()["paths"])
    assert "/api/account-control-center/mapping" in paths
    assert "/api/account-control-center/categories" in paths
    assert "/api/account-control-center/cashflow/setup" in paths
    assert "/api/account-control-center/profit-status" in paths
