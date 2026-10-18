"""P4 tests. Expected outcomes written by hand BEFORE running.

Lines used (seeded): PO-1001/2 FAS-002 500 @ 8.00 | PO-1001/1 FAS-001 500 @ 12.50
                     PO-1002/1 CON-001 40 @ 1850 (cap 42) | PO-1003/1 HAR-001 300 @ 45

Reason order: no_receipt, qty_exceeds_accepted, qty_exceeds_po, price_differs_from_po
  1 clean: receive 500, invoice 500 @ 8.00              -> match, []
  2 partial: receive 300, invoice 500 @ 8.00            -> [qty_exceeds_accepted]; invoice 300 -> match
  3 price: receive 500, invoice 500 @ 8.50              -> [price_differs_from_po]
  4 before receipt: invoice 100 @ 8.00                  -> [no_receipt]
  5 combo: receive 300, invoice 400 @ 9.00              -> [qty_exceeds_accepted, price_differs_from_po]
  6 quarantine: FAS-001 rec 500 rej 100 no coc; invoice 400 @ 12.50 -> [qty_exceeds_accepted];
    after release 400 the SAME invoice reads match
  7 cumulative: receive 500; inv A 300 -> match; inv B 300 -> [qty_exceeds_accepted, qty_exceeds_po]
  8 over PO: CON-001 receive 42 (accepted 42), invoice 42 @ 1850 -> [qty_exceeds_po]
  9 same invoice+line twice -> 409
 10 unknown line in a 2-line invoice -> 404 and nothing recorded
 11 multi-line: line A matches, line B no receipt -> overall_match False
"""
from decimal import Decimal as D

import pytest

from app.services.invoices import match_line


# ---- pure rule ---------------------------------------------------------
def ml(po_qty=500, po_price=8, has_receipt=True, accepted=500, cum=500, price=8):
    return match_line(po_qty=D(po_qty), po_price=D(po_price), has_receipt=has_receipt,
                      accepted_qty=D(accepted), cumulative_invoiced=D(cum), invoice_price=D(price))


def test_pure_clean():
    assert ml() == []


def test_pure_no_receipt_suppresses_qty_exceeds_accepted():
    assert ml(has_receipt=False, accepted=0, cum=100) == ["no_receipt"]


def test_pure_price_only():
    assert ml(price="8.50") == ["price_differs_from_po"]


def test_pure_equal_to_accepted_is_fine():
    assert ml(accepted=300, cum=300) == []


# ---- API (DB) ----------------------------------------------------------
@pytest.fixture()
def client(seeded):
    from fastapi.testclient import TestClient

    from app.main import app
    return TestClient(app)


def receive(client, key, po, line, qty, rej=0, coc=True):
    r = client.post("/receipts", json={"idempotency_key": key, "po_number": po, "line_no": line,
                                       "received_qty": qty, "rejected_qty": rej,
                                       "lot_batch": "L1", "coc_received": coc})
    assert r.status_code == 201, r.text
    return r.json()


def invoice(client, number, lines):
    return client.post("/invoices", json={"invoice_number": number, "invoice_date": "2026-01-15",
                                          "lines": [{"po_number": p, "line_no": l, "invoice_qty": q,
                                                     "invoice_unit_price_inr": pr} for p, l, q, pr in lines]})


def only_line(r):
    return r.json()["lines"][0]


def test_1_clean_match(client):
    receive(client, "r1", "PO-1001", 2, 500)
    r = invoice(client, "INV-1", [("PO-1001", 2, 500, 8)])
    assert r.status_code == 201
    assert r.json()["overall_match"] is True and only_line(r)["reasons"] == []


def test_2_partial_receipt_invoice_full_qty(client):
    receive(client, "r2", "PO-1001", 2, 300)
    r = invoice(client, "INV-2", [("PO-1001", 2, 500, 8)])
    assert only_line(r)["reasons"] == ["qty_exceeds_accepted"] and r.json()["overall_match"] is False
    r2 = invoice(client, "INV-2B", [("PO-1001", 2, 300, 8)])
    # cumulative 500 + 300 = 800 exceeds accepted (300) AND the PO qty (500)
    assert only_line(r2)["reasons"] == ["qty_exceeds_accepted", "qty_exceeds_po"]


def test_2b_partial_receipt_invoice_matching_qty(client):
    receive(client, "r2b", "PO-1001", 2, 300)
    r = invoice(client, "INV-2C", [("PO-1001", 2, 300, 8)])
    assert only_line(r)["match"] is True


def test_3_price_differs(client):
    receive(client, "r3", "PO-1001", 2, 500)
    r = invoice(client, "INV-3", [("PO-1001", 2, 500, "8.50")])
    assert only_line(r)["reasons"] == ["price_differs_from_po"]


def test_4_invoice_before_receipt(client):
    r = invoice(client, "INV-4", [("PO-1001", 2, 100, 8)])
    assert only_line(r)["reasons"] == ["no_receipt"]


def test_5_combined_reasons_in_fixed_order(client):
    receive(client, "r5", "PO-1001", 2, 300)
    r = invoice(client, "INV-5", [("PO-1001", 2, 400, 9)])
    assert only_line(r)["reasons"] == ["qty_exceeds_accepted", "price_differs_from_po"]


def test_6_quarantined_not_invoiceable_until_released(client):
    rid = receive(client, "r6", "PO-1001", 1, 500, rej=100, coc=False)["id"]
    r = invoice(client, "INV-6", [("PO-1001", 1, 400, "12.50")])
    assert only_line(r)["reasons"] == ["qty_exceeds_accepted"]
    assert client.post("/ledger/release", json={"receipt_id": rid, "qty": 400}).status_code == 201
    again = client.get("/invoices/INV-6").json()
    assert again["overall_match"] is True and again["lines"][0]["accepted_qty"] == 400


def test_7_cumulative_invoicing(client):
    receive(client, "r7", "PO-1001", 2, 500)
    a = invoice(client, "INV-7A", [("PO-1001", 2, 300, 8)])
    b = invoice(client, "INV-7B", [("PO-1001", 2, 300, 8)])
    assert only_line(a)["match"] is True
    assert only_line(b)["reasons"] == ["qty_exceeds_accepted", "qty_exceeds_po"]
    assert client.get("/invoices/INV-7A").json()["overall_match"] is True   # earlier one unaffected


def test_8_invoice_above_po_qty_within_delivery_tolerance(client):
    receive(client, "r8", "PO-1002", 1, 42)
    r = invoice(client, "INV-8", [("PO-1002", 1, 42, 1850)])
    assert only_line(r)["reasons"] == ["qty_exceeds_po"]


def test_9_duplicate_invoice_line_rejected(client):
    receive(client, "r9", "PO-1001", 2, 500)
    assert invoice(client, "INV-9", [("PO-1001", 2, 100, 8)]).status_code == 201
    assert invoice(client, "INV-9", [("PO-1001", 2, 100, 8)]).status_code == 409


def test_10_unknown_line_records_nothing(client):
    r = invoice(client, "INV-10", [("PO-1001", 2, 10, 8), ("PO-9999", 1, 10, 8)])
    assert r.status_code == 404
    assert client.get("/invoices/INV-10").status_code == 404


def test_11_multi_line_overall_mismatch(client):
    receive(client, "r11", "PO-1001", 2, 500)
    r = invoice(client, "INV-11", [("PO-1001", 2, 500, 8), ("PO-1003", 1, 300, 45)])
    j = r.json()
    assert [ln["match"] for ln in j["lines"]] == [True, False]
    assert j["lines"][1]["reasons"] == ["no_receipt"] and j["overall_match"] is False
