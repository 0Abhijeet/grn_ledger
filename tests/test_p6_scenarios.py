"""P6: 12 scenario tests + invariant checks. Expected outcomes were written by hand BEFORE running.
These are scenarios I invented. Passing says nothing about real receiving practice.

Tolerance 5%. Seeded lines (relative to today):
 PO-1001/1 FAS-001 500 @12.50 need_by -5 no promise | PO-1001/2 FAS-002 500 @8.00 need +10 prom +7
 PO-1002/1 CON-001  40 @1850  need +14 prom +21      | PO-1002/2 CON-002 200 @6.50 (cap 210)
 PO-1003/1 HAR-001 300 @45    need -3 prom +4        | PO-1003/2 HAR-002 1000 @2.20
 PO-1005/1 FAS-001 1000 @13   need +30 prom +28      | PO-1005/3 HAR-002 500 @2.30

 S01 clean full receipt     PO-1001/2 recv 500 coc
     accepted 500, quar 0, rej 0, flags [], status received, outstanding 0,
     events [receipt 500], FAS-002 available 500
 S02 partial receipt        PO-1003/1 recv 120 coc
     accepted 120, status partially_received, outstanding 180, available 120, flags []
 S03 over-delivery          PO-1002/2 recv 215 coc (cap 210)
     accepted 210, quar 5, flags [over_delivery], status received,
     events [receipt 215, quarantine 5], CON-002 available 210 quarantined 5
 S04 partial plus reject    PO-1005/1 recv 600 rej 120 coc
     accepted 480, rej 120, quar 0, flags [rejected], status partially_received,
     outstanding 520, events [receipt 600, reject 120], FAS-001 available 480
 S05 missing CoC            PO-1005/3 recv 500 no coc
     accepted 0, quar 500, flags [missing_coc], status received, outstanding 0,
     events [receipt 500, quarantine 500], HAR-002 available 0 quarantined 500 physical 500
 S06 duplicate receipt      PO-1003/2 recv 400 twice, same key
     1st 201, 2nd 409 "duplicate receipt", ledger still 1 event, available 400, 1 receipt row
 S07 invoice price differs  PO-1001/2 recv 500; invoice 500 @ 8.40
     reasons [price_differs_from_po], overall_match False
 S08 invoice qty > accepted PO-1002/1 recv 30; invoice 40 @1850
     reasons [qty_exceeds_accepted] only (40 is not above PO qty 40)
 S09 invoice before receipt PO-1005/1 invoice 100 @13.00
     reasons [no_receipt]
 S10 late, promise moved    PO-1003/1 starts overdue+at_risk, promise +4; move to +20
     before: draft has date(+4); after: promised +20, overdue True, at_risk True,
     draft has date(+20) and not date(+4)
 S11 late, no promise       PO-1001/1 flags overdue True, at_risk False, no_promise_date True;
     eligible for chase; draft says "no promised date on record"
 S12 release from quarantine PO-1001/1 recv 500 rej 100 no coc, release 400, then release 1 more
     before: available 0 quarantined 400 rejected 100; after: available 400 quarantined 0 physical 400;
     extra release 409; events [receipt 500, quarantine 400, reject 100, release 400]

 Invariants (checked after every scenario and after a seeded random run):
  I1 replay of events == SQL-derived balance, for every part
  I2 available, quarantined, physical never negative (replay watches every step)
  I3 per receipt: accepted + rejected + quarantined == received
  I4 per PO line: sum(accepted) <= ordered * (1 + tolerance)   (over-receipt rule)
  I5 physical == available + quarantined
"""
import random
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text


# ---- harness -----------------------------------------------------------
def iso(delta):
    return (date.today() + timedelta(days=delta)).isoformat()


class Api:
    def __init__(self, client):
        self.c = client
        self.n = 0

    def receive(self, po, line, qty, rej=0, coc=True, key=None, lot="LOT-1"):
        self.n += 1
        return self.c.post("/receipts", json={
            "idempotency_key": key or f"k{self.n}", "po_number": po, "line_no": line,
            "received_qty": qty, "rejected_qty": rej, "lot_batch": lot, "coc_received": coc})

    def invoice(self, number, po, line, qty, price):
        return self.c.post("/invoices", json={
            "invoice_number": number, "invoice_date": iso(0),
            "lines": [{"po_number": po, "line_no": line, "invoice_qty": qty,
                       "invoice_unit_price_inr": price}]})

    def bal(self, part):
        return self.c.get(f"/ledger/balances/{part}").json()

    def events(self, part):
        return [(e["event_type"], e["qty"]) for e in self.c.get("/ledger/events",
                                                                 params={"part_id": part}).json()]

    def line(self, po, line_no):
        return [x for x in self.c.get("/purchase-orders/lines").json()
                if x["po_number"] == po and x["line_no"] == line_no][0]

    def draft(self, po, line_no):
        return self.c.post("/chase-drafts", json={"po_number": po, "line_no": line_no,
                                                  "use_llm": False})


def check_invariants(api: Api):
    from app import config
    from app.db import get_session
    rows = api.c.get("/ledger/replay").json()
    for r in rows:                                              # I1, I2
        assert r["match"], f"I1 replay != derived for {r['part_id']}: {r}"
        assert not r["ever_negative"], f"I2 went negative for {r['part_id']}"
    for b in api.c.get("/ledger/balances").json():              # I2, I5
        assert b["available"] >= 0 and b["quarantined"] >= 0 and b["physical"] >= 0, b
        assert b["physical"] == pytest.approx(b["available"] + b["quarantined"]), b
    with get_session() as s:
        bad = s.execute(text(                                   # I3
            "SELECT id FROM receipt_events "
            "WHERE accepted_qty + rejected_qty + quarantined_qty <> received_qty")).all()
        assert bad == [], f"I3 violated by receipts {bad}"
        cap_pct = Decimal(1) + config.OVER_DELIVERY_TOLERANCE_PCT / Decimal(100)
        over = s.execute(text(                                  # I4
            "SELECT l.po_number, l.line_no, l.ordered_qty, SUM(r.accepted_qty) AS acc "
            "FROM receipt_events r JOIN po_lines l ON l.id = r.po_line_id "
            "GROUP BY l.id, l.po_number, l.line_no, l.ordered_qty")).all()
        for po, ln, ordered, acc in over:
            assert acc <= ordered * cap_pct, f"I4 {po}/{ln} accepted {acc} > cap {ordered * cap_pct}"


@pytest.fixture()
def api(seeded, monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from app.main import app
    monkeypatch.setenv("CHASE_LOG_PATH", str(tmp_path / "drafts.jsonl"))
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    a = Api(TestClient(app))
    yield a
    check_invariants(a)          # runs after every scenario, even if the scenario body passed


# ---- the 12 scenarios --------------------------------------------------
def test_s01_clean_full_receipt(api):
    j = api.receive("PO-1001", 2, 500).json()
    assert (j["accepted_qty"], j["quarantined_qty"], j["rejected_qty"]) == (500, 0, 0)
    assert j["exception_flags"] == [] and j["po_line_status"] == "received"
    assert j["outstanding_qty"] == 0 and api.events("FAS-002") == [("receipt", 500)]
    assert api.bal("FAS-002")["available"] == 500


def test_s02_partial_receipt(api):
    j = api.receive("PO-1003", 1, 120).json()
    assert j["accepted_qty"] == 120 and j["po_line_status"] == "partially_received"
    assert j["outstanding_qty"] == 180 and j["exception_flags"] == []
    assert api.bal("HAR-001")["available"] == 120


def test_s03_over_delivery(api):
    j = api.receive("PO-1002", 2, 215).json()
    assert (j["accepted_qty"], j["quarantined_qty"]) == (210, 5)
    assert j["exception_flags"] == ["over_delivery"] and j["po_line_status"] == "received"
    assert api.events("CON-002") == [("receipt", 215), ("quarantine", 5)]
    b = api.bal("CON-002")
    assert (b["available"], b["quarantined"]) == (210, 5)


def test_s04_partial_plus_reject(api):
    j = api.receive("PO-1005", 1, 600, rej=120).json()
    assert (j["accepted_qty"], j["rejected_qty"], j["quarantined_qty"]) == (480, 120, 0)
    assert j["exception_flags"] == ["rejected"] and j["po_line_status"] == "partially_received"
    assert j["outstanding_qty"] == 520
    assert api.events("FAS-001") == [("receipt", 600), ("reject", 120)]
    assert api.bal("FAS-001")["available"] == 480


def test_s05_missing_coc(api):
    j = api.receive("PO-1005", 3, 500, coc=False).json()
    assert (j["accepted_qty"], j["quarantined_qty"]) == (0, 500)
    assert j["exception_flags"] == ["missing_coc"] and j["po_line_status"] == "received"
    assert j["outstanding_qty"] == 0
    assert api.events("HAR-002") == [("receipt", 500), ("quarantine", 500)]
    b = api.bal("HAR-002")
    assert (b["available"], b["quarantined"], b["physical"]) == (0, 500, 500)


def test_s06_duplicate_receipt_attempt(api):
    assert api.receive("PO-1003", 2, 400, key="DUP-1").status_code == 201
    r = api.receive("PO-1003", 2, 400, key="DUP-1")
    assert r.status_code == 409 and "duplicate receipt" in r.json()["detail"]
    assert api.events("HAR-002") == [("receipt", 400)]
    assert api.bal("HAR-002")["available"] == 400
    assert len(api.c.get("/receipts", params={"po_number": "PO-1003"}).json()) == 1


def test_s07_invoice_price_differs(api):
    api.receive("PO-1001", 2, 500)
    j = api.invoice("INV-S07", "PO-1001", 2, 500, "8.40").json()
    assert j["lines"][0]["reasons"] == ["price_differs_from_po"] and j["overall_match"] is False


def test_s08_invoice_qty_exceeds_accepted(api):
    api.receive("PO-1002", 1, 30)
    j = api.invoice("INV-S08", "PO-1002", 1, 40, 1850).json()
    assert j["lines"][0]["reasons"] == ["qty_exceeds_accepted"]


def test_s09_invoice_before_any_receipt(api):
    j = api.invoice("INV-S09", "PO-1005", 1, 100, "13.00").json()
    assert j["lines"][0]["reasons"] == ["no_receipt"]


def test_s10_late_with_promised_date_moved(api):
    before = api.line("PO-1003", 1)
    assert (before["overdue"], before["at_risk"], before["no_promise_date"]) == (True, True, False)
    assert iso(4) in api.draft("PO-1003", 1).json()["text"]
    r = api.c.patch("/purchase-orders/PO-1003/lines/1/promised-date", json={"promised_date": iso(20)})
    assert r.status_code == 200
    after = api.line("PO-1003", 1)
    assert after["promised_date"] == iso(20)
    assert (after["overdue"], after["at_risk"]) == (True, True)
    text_ = api.draft("PO-1003", 1).json()["text"]
    assert iso(20) in text_ and iso(4) not in text_


def test_s11_late_with_no_promised_date(api):
    ln = api.line("PO-1001", 1)
    assert (ln["overdue"], ln["at_risk"], ln["no_promise_date"]) == (True, False, True)
    elig = {(x["po_number"], x["line_no"]) for x in api.c.get("/chase-drafts/eligible").json()}
    assert ("PO-1001", 1) in elig
    d = api.draft("PO-1001", 1).json()
    assert "no promised date on record" in d["text"] and d["sent"] is False


def test_s12_release_from_quarantine(api):
    j = api.receive("PO-1001", 1, 500, rej=100, coc=False).json()
    b = api.bal("FAS-001")
    assert (b["available"], b["quarantined"], b["rejected"]) == (0, 400, 100)
    assert api.c.post("/ledger/release", json={"receipt_id": j["id"], "qty": 400}).status_code == 201
    b = api.bal("FAS-001")
    assert (b["available"], b["quarantined"], b["physical"]) == (400, 0, 400)
    assert api.c.post("/ledger/release", json={"receipt_id": j["id"], "qty": 1}).status_code == 409
    assert api.events("FAS-001") == [("receipt", 500), ("quarantine", 400), ("reject", 100),
                                     ("release_from_quarantine", 400)]


# ---- invariants under a seeded random run ------------------------------
def test_invariants_hold_under_random_operations(api):
    rng = random.Random(42)
    lines = [("PO-1001", 1), ("PO-1001", 2), ("PO-1002", 1), ("PO-1002", 2), ("PO-1003", 1),
             ("PO-1003", 2), ("PO-1005", 1), ("PO-1005", 2), ("PO-1005", 3)]
    parts = ["FAS-001", "FAS-002", "CON-001", "CON-002", "HAR-001", "HAR-002"]
    receipt_ids, codes = [], []
    for _ in range(80):
        op = rng.choice(["receive", "receive", "receive", "issue", "release"])
        if op == "receive":
            po, ln = rng.choice(lines)
            qty = rng.randint(1, 300)
            r = api.receive(po, ln, qty, rej=rng.randint(0, qty), coc=rng.random() > 0.3)
            if r.status_code == 201:
                receipt_ids.append(r.json()["id"])
        elif op == "issue":
            r = api.c.post("/ledger/issue", json={"part_id": rng.choice(parts),
                                                  "qty": rng.randint(1, 150)})
        elif receipt_ids:
            r = api.c.post("/ledger/release", json={"receipt_id": rng.choice(receipt_ids),
                                                    "qty": rng.randint(1, 100)})
        else:
            continue
        codes.append(r.status_code)
    assert set(codes) <= {201, 409, 422}, f"unexpected status codes: {set(codes)}"
    assert codes.count(201) > 10 and 409 in codes     # run did real work and hit guards
    # invariants are checked by the api fixture teardown
