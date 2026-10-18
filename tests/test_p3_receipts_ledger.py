"""P3 tests. Expected outcomes written by hand BEFORE running. Tolerance = 5%.

classify_receipt, ordered=100, cap=105:
  a  received 100, coc            -> accepted 100, quar 0,  rej 0,  flags []
  b  received 60,  coc            -> accepted 60
  c  received 110, coc            -> accepted 105, quar 5,  flags [over_delivery]
  d  received 103, coc            -> accepted 103, quar 0,  flags [over_delivery_within_tolerance]
  e  received 100, rej 20, no coc -> accepted 0, quar 80, rej 20, flags [rejected, missing_coc]
  f  kept 90, received 20, coc    -> room 15: accepted 15, quar 5, flags [over_delivery]
  g  received 110, no coc         -> accepted 0, quar 110, flags [missing_coc, over_delivery]
  h  received 10, rej 10          -> accepted 0, quar 0, rej 10, flags [rejected]

Ledger sign model: available = receipt - quarantine - reject + release - issue.
"""
from decimal import Decimal as D

import pytest

from app.services.receipts import Invalid, classify_receipt


def cls(received, rejected=0, coc=True, kept=0, ordered=100):
    return classify_receipt(ordered=D(ordered), kept_so_far=D(kept), received=D(received),
                            rejected=D(rejected), coc_received=coc, tolerance_pct=D(5))


def shape(c):
    return (c["accepted"], c["quarantined"], c["rejected"], c["flags"])


# ---- pure rules --------------------------------------------------------
def test_a_clean_full():
    assert shape(cls(100)) == (100, 0, 0, [])


def test_b_partial():
    assert shape(cls(60)) == (60, 0, 0, [])


def test_c_over_delivery_beyond_tolerance():
    assert shape(cls(110)) == (105, 5, 0, ["over_delivery"])


def test_d_over_delivery_within_tolerance():
    assert shape(cls(103)) == (103, 0, 0, ["over_delivery_within_tolerance"])


def test_e_reject_and_missing_coc():
    assert shape(cls(100, rejected=20, coc=False)) == (0, 80, 20, ["rejected", "missing_coc"])


def test_f_over_delivery_on_second_receipt():
    assert shape(cls(20, kept=90)) == (15, 5, 0, ["over_delivery"])


def test_g_missing_coc_plus_over_delivery():
    assert shape(cls(110, coc=False)) == (0, 110, 0, ["missing_coc", "over_delivery"])


def test_h_everything_rejected():
    assert shape(cls(10, rejected=10)) == (0, 0, 10, ["rejected"])


def test_rejected_more_than_received_is_invalid():
    with pytest.raises(Invalid):
        cls(10, rejected=11)


def test_buckets_always_add_up():
    for rec, rej, coc, kept in [(100, 0, True, 0), (110, 5, False, 20), (7, 7, True, 3), (500, 50, True, 99)]:
        c = cls(rec, rej, coc, kept)
        assert c["accepted"] + c["quarantined"] + c["rejected"] == D(rec)


# ---- API + ledger (DB) -------------------------------------------------
@pytest.fixture()
def client(seeded):
    from fastapi.testclient import TestClient

    from app.main import app
    return TestClient(app)


def receive(client, key, po, line, qty, rej=0, coc=True, lot="LOT-1"):
    return client.post("/receipts", json={
        "idempotency_key": key, "po_number": po, "line_no": line, "received_qty": qty,
        "rejected_qty": rej, "lot_batch": lot, "coc_received": coc})


def bal(client, part):
    return client.get(f"/ledger/balances/{part}").json()


def events(r):
    return [(e["event_type"], e["qty"]) for e in r.json()["ledger_events"]]


def test_clean_full_receipt(client):
    r = receive(client, "k-clean", "PO-1001", 2, 500)            # FAS-002, ordered 500
    assert r.status_code == 201
    j = r.json()
    assert (j["accepted_qty"], j["quarantined_qty"], j["po_line_status"]) == (500, 0, "received")
    assert j["outstanding_qty"] == 0 and events(r) == [("receipt", 500)]
    b = bal(client, "FAS-002")
    assert (b["available"], b["quarantined"], b["physical"]) == (500, 0, 500)


def test_partial_receipt_sets_partially_received(client):
    r = receive(client, "k-part", "PO-1003", 1, 120)             # HAR-001, ordered 300
    j = r.json()
    assert j["po_line_status"] == "partially_received" and j["outstanding_qty"] == 180
    assert bal(client, "HAR-001")["available"] == 120


def test_over_delivery_excess_quarantined_and_flagged(client):
    r = receive(client, "k-over", "PO-1002", 2, 215)             # CON-002, ordered 200, cap 210
    j = r.json()
    assert (j["accepted_qty"], j["quarantined_qty"]) == (210, 5)
    assert j["exception_flags"] == ["over_delivery"] and j["po_line_status"] == "received"
    assert events(r) == [("receipt", 215), ("quarantine", 5)]
    b = bal(client, "CON-002")
    assert (b["available"], b["quarantined"], b["physical"]) == (210, 5, 215)


def test_demo_step4_reject_20pct_and_missing_coc(client):
    r = receive(client, "k-d4", "PO-1001", 1, 500, rej=100, coc=False)   # FAS-001, ordered 500
    j = r.json()
    assert (j["accepted_qty"], j["quarantined_qty"], j["rejected_qty"]) == (0, 400, 100)
    assert j["exception_flags"] == ["rejected", "missing_coc"]
    assert events(r) == [("receipt", 500), ("quarantine", 400), ("reject", 100)]
    b = bal(client, "FAS-001")
    assert (b["available"], b["quarantined"], b["physical"], b["rejected"]) == (0, 400, 400, 100)
    assert j["po_line_status"] == "partially_received" and j["outstanding_qty"] == 100


def test_duplicate_receipt_rejected_and_ledger_unchanged(client):
    assert receive(client, "k-dup", "PO-1003", 1, 50).status_code == 201
    before = len(client.get("/ledger/events").json())
    r = receive(client, "k-dup", "PO-1003", 1, 50)
    assert r.status_code == 409 and "duplicate receipt" in r.json()["detail"]
    assert len(client.get("/ledger/events").json()) == before
    assert bal(client, "HAR-001")["available"] == 50


def test_release_from_quarantine(client):
    rid = receive(client, "k-rel", "PO-1001", 1, 500, rej=100, coc=False).json()["id"]
    r = client.post("/ledger/release", json={"receipt_id": rid, "qty": 400})
    assert r.status_code == 201
    b = bal(client, "FAS-001")
    assert (b["available"], b["quarantined"]) == (400, 0)
    over = client.post("/ledger/release", json={"receipt_id": rid, "qty": 1})
    assert over.status_code == 409


def test_issue_cannot_go_negative(client):
    receive(client, "k-iss", "PO-1001", 2, 500)                  # FAS-002 available 500
    assert client.post("/ledger/issue", json={"part_id": "FAS-002", "qty": 200}).status_code == 201
    assert bal(client, "FAS-002")["available"] == 300
    r = client.post("/ledger/issue", json={"part_id": "FAS-002", "qty": 400})
    assert r.status_code == 409
    assert bal(client, "FAS-002")["available"] == 300


def test_quarantined_stock_cannot_be_issued(client):
    receive(client, "k-q", "PO-1001", 1, 500, rej=100, coc=False)   # FAS-001: 0 available, 400 quarantined
    r = client.post("/ledger/issue", json={"part_id": "FAS-001", "qty": 1})
    assert r.status_code == 409


def test_pending_approval_line_cannot_receive(client):
    assert receive(client, "k-pend", "PO-1004", 1, 10).status_code == 409


def test_invalid_and_unknown(client):
    assert receive(client, "k-bad", "PO-1001", 2, 10, rej=11).status_code == 422
    assert receive(client, "k-nf", "PO-9999", 1, 10).status_code == 404


def test_replay_equals_derived_after_mixed_activity(client):
    receive(client, "m1", "PO-1001", 2, 500)
    receive(client, "m2", "PO-1001", 1, 500, rej=100, coc=False)
    rid = receive(client, "m3", "PO-1002", 2, 215).json()["id"]
    receive(client, "m4", "PO-1003", 1, 120)
    client.post("/ledger/issue", json={"part_id": "FAS-002", "qty": 120})
    client.post("/ledger/release", json={"receipt_id": rid, "qty": 5})
    rows = client.get("/ledger/replay").json()
    assert {r["part_id"] for r in rows} == {"FAS-001", "FAS-002", "CON-002", "HAR-001"}
    assert all(r["match"] for r in rows)
    assert not any(r["ever_negative"] for r in rows)
    assert all(r["derived"]["available"] >= 0 for r in rows)
