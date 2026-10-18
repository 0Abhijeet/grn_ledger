"""P2 tests. Expected outcomes written by hand BEFORE running.

Rule tests use as_of = 2026-01-10.
API tests rely on the fixture offsets (seeded relative to today):
  overdue         -> PO-1001/1 (need_by -5) and PO-1003/1 (need_by -3)
  at_risk         -> PO-1002/1 (14 vs 21), PO-1003/1 (-3 vs 4), PO-1005/2 (30 vs 35)
  no_promise_date -> PO-1001/1 and PO-1003/2
  PO-1004 (172,500) pending, so it carries no flags until approved
"""
from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.services.po import delivery_flags

AS_OF = date(2026, 1, 10)
D = Decimal


def flags(need_by, promised, outstanding=100, status="open"):
    return delivery_flags(need_by=need_by, promised=promised, outstanding=D(outstanding),
                          status=status, as_of=AS_OF)


# ---- pure rule tests (no DB) -------------------------------------------
def test_overdue_and_no_promise():
    assert flags(date(2026, 1, 5), None) == {"overdue": True, "at_risk": False, "no_promise_date": True}


def test_at_risk_only():
    assert flags(date(2026, 1, 20), date(2026, 1, 25)) == {
        "overdue": False, "at_risk": True, "no_promise_date": False}


def test_on_track_has_no_flags():
    assert flags(date(2026, 1, 20), date(2026, 1, 15)) == {
        "overdue": False, "at_risk": False, "no_promise_date": False}


def test_late_with_promise_moved_is_overdue_and_at_risk():
    assert flags(date(2026, 1, 5), date(2026, 1, 15)) == {
        "overdue": True, "at_risk": True, "no_promise_date": False}


def test_missed_promise_is_overdue_even_if_need_by_in_future():
    assert flags(date(2026, 1, 20), date(2026, 1, 8))["overdue"] is True


def test_need_by_today_is_not_overdue():
    assert flags(date(2026, 1, 10), date(2026, 1, 10))["overdue"] is False


def test_fully_received_has_no_flags():
    assert flags(date(2026, 1, 5), None, outstanding=0, status="received") == {
        "overdue": False, "at_risk": False, "no_promise_date": False}


def test_pending_approval_has_no_flags():
    assert flags(date(2026, 1, 5), None, status="pending_approval") == {
        "overdue": False, "at_risk": False, "no_promise_date": False}


# ---- API tests (DB) -----------------------------------------------------
@pytest.fixture()
def client(seeded):
    from fastapi.testclient import TestClient

    from app.main import app
    return TestClient(app)


def keys(resp):
    return {(r["po_number"], r["line_no"]) for r in resp.json()}


def test_list_pos(client):
    r = client.get("/purchase-orders")
    assert r.status_code == 200
    by_po = {p["po_number"]: p for p in r.json()}
    assert len(by_po) == 5
    assert by_po["PO-1004"]["status"] == "pending_approval"
    assert by_po["PO-1004"]["po_total_inr"] == 172500
    assert all(p["status"] == "open" for k, p in by_po.items() if k != "PO-1004")


def test_flag_filters(client):
    assert keys(client.get("/purchase-orders/lines", params={"flag": "overdue"})) == {
        ("PO-1001", 1), ("PO-1003", 1)}
    assert keys(client.get("/purchase-orders/lines", params={"flag": "at_risk"})) == {
        ("PO-1002", 1), ("PO-1003", 1), ("PO-1005", 2)}
    assert keys(client.get("/purchase-orders/lines", params={"flag": "no_promise_date"})) == {
        ("PO-1001", 1), ("PO-1003", 2)}


def test_approve_flow(client):
    r = client.post("/purchase-orders/PO-1004/approve")
    assert r.status_code == 200 and r.json()["lines_approved"] == 2
    assert client.get("/purchase-orders/PO-1004").json()["status"] == "open"
    assert client.post("/purchase-orders/PO-1004/approve").status_code == 409   # second approve
    assert client.post("/purchase-orders/PO-1001/approve").status_code == 409   # was never pending
    assert client.post("/purchase-orders/PO-9999/approve").status_code == 404


def test_outstanding_equals_ordered_before_any_receipt(client):
    d = client.get("/purchase-orders/PO-1003").json()
    by_line = {ln["line_no"]: ln for ln in d["lines"]}
    assert by_line[1]["outstanding_qty"] == 300 and by_line[2]["outstanding_qty"] == 1000


def test_as_of_in_future_makes_all_open_lines_overdue(client):
    far = (date.today() + timedelta(days=40)).isoformat()
    r = client.get("/purchase-orders/lines", params={"flag": "overdue", "as_of": far})
    assert len(r.json()) == 9      # 11 lines minus the 2 still pending approval


def test_unknown_po_404(client):
    assert client.get("/purchase-orders/NOPE").status_code == 404
