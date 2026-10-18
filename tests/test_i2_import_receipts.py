"""CSV import P2 tests (receipts). Expected outcomes written by hand BEFORE running.
Synthetic data. The CSV layout is a guess at an Excel sheet.

Line layout: line 1 comment, line 2 header, data from line 3. Seeded lines used:
 PO-1001/1 FAS-001 500 | PO-1001/2 FAS-002 500 | PO-1002/2 CON-002 200 (cap 210)
 PO-1003/1 HAR-001 300 | PO-1004/1 pending approval

 clean (4 rows): r-1 PO-1001/2 500 rej0 yes  -> accepted 500, status received
                 r-2 PO-1003/1 120 rej0 TRUE -> accepted 120, partially_received, outstanding 180
                 r-3 PO-1001/1 500 rej100 no -> quarantined 400, rejected 100, flags rejected,missing_coc
                 r-4 PO-1002/2 215 rej0 1    -> accepted 210, quarantined 5, flag over_delivery
   201, 4 read/4 imported/0 rejected, 4 receipts, 7 ledger events (1+1+3+2)
   balances: FAS-002 avail 500 | HAR-001 avail 120 | FAS-001 avail 0 quar 400 rej 100
             CON-002 avail 210 quar 5 ; replay matches everywhere
 same inputs via POST /receipts on a fresh seed -> identical receipts and ledger events
 apply-time failure on the LAST row (unknown line) in all_or_nothing:
   422, earlier applied rows are rolled back: 0 receipts, 0 ledger events, PO-1001/2 still open
 skip mode, same file: 201 partial, good rows stay, bad rows reported
 cumulative inside one file: PO-1002/2 150 then 70 -> second: accepted 60, quarantined 10
 dup key in file: second occurrence flagged 'duplicate of row 3', applied once
 dup key already in DB: reported as duplicate, ledger unchanged
"""
import pytest
from sqlalchemy import text

HEADER = "idempotency_key,po_number,line_no,received_qty,rejected_qty,lot_batch,coc_received"
TAG = "# SYNTHETIC test file, not real data. Layout is a guess at an Excel sheet."


def csv_file(*rows, header=HEADER):
    return "\n".join([TAG, header, *rows]) + "\n"


@pytest.fixture()
def client(seeded):
    from fastapi.testclient import TestClient

    from app.main import app
    return TestClient(app)


def post(client, body, mode=None):
    return client.post("/import/receipts", params={"mode": mode} if mode else {},
                       content=body.encode("utf-8"), headers={"Content-Type": "text/csv"})


def scalar(sql):
    from app.db import get_session
    with get_session() as s:
        return s.execute(text(sql)).scalar()


def counts():
    return scalar("SELECT count(*) FROM receipt_events"), scalar("SELECT count(*) FROM ledger_events")


def errs(r):
    return [(e["row_number"], e["field"]) for e in r.json()["errors"]]


def bal(client, part):
    b = client.get(f"/ledger/balances/{part}").json()
    return b["available"], b["quarantined"], b["rejected"]


CLEAN = csv_file(
    "r-1,PO-1001,2,500,0,LOT-A,yes",
    "r-2,PO-1003,1,120,0,LOT-B,TRUE",
    "r-3,PO-1001,1,500,100,LOT-C,no",
    "r-4,PO-1002,2,215,0,LOT-D,1")


def snapshot():
    from app.db import get_session
    with get_session() as s:
        rec = s.execute(text(
            "SELECT l.po_number, l.line_no, r.received_qty, r.accepted_qty, r.rejected_qty, "
            "r.quarantined_qty, r.lot_batch, r.coc_received, r.exception_flags "
            "FROM receipt_events r JOIN po_lines l ON l.id = r.po_line_id ORDER BY r.id")).all()
        led = s.execute(text(
            "SELECT part_id, event_type, qty FROM ledger_events ORDER BY id")).all()
        st = s.execute(text("SELECT po_number, line_no, status FROM po_lines "
                            "ORDER BY po_number, line_no")).all()
    return [tuple(x) for x in rec], [tuple(x) for x in led], [tuple(x) for x in st]


# ---- happy path and shared rules ----------------------------------------
def test_clean_file_applies_receipt_rules_and_ledger(client):
    r = post(client, CLEAN)
    j = r.json()
    assert r.status_code == 201 and j["status"] == "imported", r.text
    assert (j["rows_read"], j["rows_imported"], j["rows_rejected"]) == (4, 4, 0)
    by_key = {x["row_number"]: x for x in j["imported_receipts"]}
    assert (by_key[3]["accepted_qty"], by_key[3]["po_line_status"]) == (500, "received")
    assert (by_key[4]["accepted_qty"], by_key[4]["po_line_status"], by_key[4]["outstanding_qty"]) == (
        120, "partially_received", 180)
    assert (by_key[5]["quarantined_qty"], by_key[5]["rejected_qty"], by_key[5]["exception_flags"]) == (
        400, 100, ["rejected", "missing_coc"])
    assert (by_key[6]["accepted_qty"], by_key[6]["quarantined_qty"], by_key[6]["exception_flags"]) == (
        210, 5, ["over_delivery"])
    assert counts() == (4, 7)
    assert bal(client, "FAS-002") == (500, 0, 0)
    assert bal(client, "HAR-001") == (120, 0, 0)
    assert bal(client, "FAS-001") == (0, 400, 100)
    assert bal(client, "CON-002") == (210, 5, 0)


def test_replay_equals_balances_after_import(client):
    post(client, CLEAN)
    rows = client.get("/ledger/replay").json()
    assert {r["part_id"] for r in rows} == {"FAS-001", "FAS-002", "HAR-001", "CON-002"}
    assert all(r["match"] and not r["ever_negative"] for r in rows)
    assert all(b["available"] >= 0 for b in client.get("/ledger/balances").json())


def test_import_equals_api_for_the_same_inputs(client):
    from app.seed import seed
    assert post(client, CLEAN).status_code == 201
    via_import = snapshot()
    seed()                                              # fresh DB, same fixture
    api_rows = [("r-1", "PO-1001", 2, 500, 0, "LOT-A", True), ("r-2", "PO-1003", 1, 120, 0, "LOT-B", True),
                ("r-3", "PO-1001", 1, 500, 100, "LOT-C", False), ("r-4", "PO-1002", 2, 215, 0, "LOT-D", True)]
    for k, po, ln, q, rej, lot, coc in api_rows:
        assert client.post("/receipts", json={"idempotency_key": k, "po_number": po, "line_no": ln,
                                              "received_qty": q, "rejected_qty": rej, "lot_batch": lot,
                                              "coc_received": coc}).status_code == 201
    assert snapshot() == via_import


def test_import_code_never_touches_the_ledger_directly():
    from pathlib import Path
    src = Path("app/services/imports.py").read_text(encoding="utf-8")
    assert "LedgerEvent" not in src and "INSERT INTO ledger_events" not in src


# ---- all-or-nothing and savepoints --------------------------------------
def test_apply_time_failure_on_last_row_rolls_back_earlier_rows(client):
    r = post(client, csv_file(
        "a-1,PO-1001,2,500,0,LOT-A,yes",
        "a-2,PO-1003,1,100,0,LOT-B,yes",
        "a-3,PO-9999,1,10,0,LOT-C,yes"))                # passes field checks, fails when applied
    j = r.json()
    assert r.status_code == 422 and j["status"] == "rejected"
    assert (j["rows_read"], j["rows_imported"], j["rows_rejected"], j["valid_rows_not_applied"]) == (3, 0, 1, 2)
    assert errs(r) == [(5, "po_number")] and "not found" in j["errors"][0]["problem"]
    assert counts() == (0, 0)
    assert client.get("/purchase-orders/PO-1001").json()["lines"][1]["status"] == "open"
    assert bal(client, "FAS-002") == (0, 0, 0)


MIXED = csv_file(
    "b-1,PO-1001,2,500,0,LOT-A,yes",
    "b-2,PO-1003,1,100,0,LOT-B,maybe",
    "b-3,PO-9999,1,10,0,LOT-C,yes")


def test_field_error_and_apply_error_reported_together(client):
    r = post(client, MIXED)
    assert r.status_code == 422 and errs(r) == [(4, "coc_received"), (5, "po_number")]
    assert r.json()["rows_rejected"] == 2 and counts() == (0, 0)


def test_skip_mode_keeps_good_rows(client):
    r = post(client, MIXED, mode="skip_bad_rows")
    j = r.json()
    assert r.status_code == 201 and j["status"] == "partial"
    assert (j["rows_read"], j["rows_imported"], j["rows_rejected"]) == (3, 1, 2)
    assert counts() == (1, 1) and bal(client, "FAS-002") == (500, 0, 0)


def test_skip_mode_later_rows_still_see_earlier_rows_after_a_bad_row(client):
    r = post(client, csv_file(
        "c-1,PO-1003,1,100,0,LOT-A,yes",
        "c-2,PO-9999,1,10,0,LOT-B,yes",
        "c-3,PO-1003,1,50,0,LOT-C,yes"), mode="skip_bad_rows")
    last = r.json()["imported_receipts"][-1]
    assert r.status_code == 201 and last["outstanding_qty"] == 150      # 300 - 100 - 50
    assert bal(client, "HAR-001") == (150, 0, 0)


def test_skip_mode_with_nothing_applied_is_422(client):
    r = post(client, csv_file("d-1,PO-9999,1,10,0,LOT,yes"), mode="skip_bad_rows")
    assert r.status_code == 422 and r.json()["status"] == "rejected" and counts() == (0, 0)


# ---- cumulative rules across rows of one file ----------------------------
def test_tolerance_is_cumulative_across_rows_in_one_file(client):
    r = post(client, csv_file("e-1,PO-1002,2,150,0,LOT-A,yes", "e-2,PO-1002,2,70,0,LOT-B,yes"))
    a, b = r.json()["imported_receipts"]
    assert (a["accepted_qty"], a["quarantined_qty"]) == (150, 0)
    assert (b["accepted_qty"], b["quarantined_qty"], b["exception_flags"]) == (60, 10, ["over_delivery"])
    assert bal(client, "CON-002") == (210, 10, 0)


def test_partial_then_rest_completes_the_line(client):
    r = post(client, csv_file("f-1,PO-1003,1,100,0,LOT-A,yes", "f-2,PO-1003,1,200,0,LOT-B,yes"))
    a, b = r.json()["imported_receipts"]
    assert a["po_line_status"] == "partially_received" and b["po_line_status"] == "received"
    assert b["outstanding_qty"] == 0


# ---- idempotency ---------------------------------------------------------
def test_duplicate_key_inside_file(client):
    body = csv_file("k-1,PO-1003,1,50,0,LOT-A,yes", "k-1,PO-1003,1,50,0,LOT-A,yes")
    r = post(client, body)
    assert r.status_code == 422 and errs(r) == [(4, "idempotency_key")]
    assert "duplicate of row 3" in r.json()["errors"][0]["problem"] and counts() == (0, 0)
    r2 = post(client, body, mode="skip_bad_rows")
    assert r2.status_code == 201 and r2.json()["rows_imported"] == 1
    assert counts() == (1, 1) and bal(client, "HAR-001") == (50, 0, 0)     # applied once, not twice


def test_duplicate_key_already_in_database(client):
    assert client.post("/receipts", json={"idempotency_key": "k-db", "po_number": "PO-1003",
                                          "line_no": 1, "received_qty": 50, "rejected_qty": 0,
                                          "lot_batch": "L", "coc_received": True}).status_code == 201
    before = counts()
    r = post(client, csv_file("k-db,PO-1003,1,50,0,LOT-A,yes"))
    assert r.status_code == 422 and errs(r) == [(3, "idempotency_key")]
    assert "duplicate receipt" in r.json()["errors"][0]["problem"]
    assert counts() == before and bal(client, "HAR-001") == (50, 0, 0)


# ---- field rules ---------------------------------------------------------
def test_coc_accepts_the_documented_values(client):
    lines = [("PO-1001", 1), ("PO-1001", 2), ("PO-1002", 1), ("PO-1002", 2), ("PO-1003", 1), ("PO-1003", 2)]
    values = ["Yes", "NO", "True", "false", " 1 ", "0"]
    rows = [f"v-{i},{po},{ln},10,0,LOT,{v}" for i, ((po, ln), v) in enumerate(zip(lines, values))]
    r = post(client, csv_file(*rows))
    assert r.status_code == 201 and r.json()["rows_imported"] == 6, r.text
    assert scalar("SELECT count(*) FROM receipt_events WHERE coc_received = false") == 3
    assert scalar("SELECT count(*) FROM receipt_events WHERE coc_received = true") == 3


@pytest.mark.parametrize("value", ["maybe", "", "2", "yess", "y"])
def test_bad_coc_value(client, value):
    r = post(client, csv_file(f"x-1,PO-1003,1,10,0,LOT,{value}"))
    assert r.status_code == 422 and errs(r) == [(3, "coc_received")] and counts() == (0, 0)


def test_blank_rejected_qty_means_zero(client):
    r = post(client, csv_file("y-1,PO-1003,1,10,,LOT,yes"))
    assert r.status_code == 201 and r.json()["imported_receipts"][0]["rejected_qty"] == 0


@pytest.mark.parametrize("row,field,fragment", [
    ("z-1,PO-1003,1,0,0,LOT,yes", "received_qty", "greater than 0"),
    ("z-2,PO-1003,1,10,11,LOT,yes", "rejected_qty", "rejected_qty must be between"),
    ("z-3,PO-1003,1,10,-1,LOT,yes", "rejected_qty", "must not be negative"),
    ("z-4,PO-1003,1,abc,0,LOT,yes", "received_qty", "not a plain number"),
    ("z-5,PO-1003,1,10,0,,yes", "lot_batch", "required"),
    (",PO-1003,1,10,0,LOT,yes", "idempotency_key", "required"),
    ("z-7,PO-1003,0,10,0,LOT,yes", "line_no", "1 or more"),
])
def test_field_rules(client, row, field, fragment):
    r = post(client, csv_file(row))
    assert r.status_code == 422
    e = [x for x in r.json()["errors"] if x["field"] == field]
    assert e and fragment in e[0]["problem"], r.text
    assert counts() == (0, 0)


def test_line_awaiting_approval_cannot_receive(client):
    r = post(client, csv_file("p-1,PO-1004,1,10,0,LOT,yes"))
    assert r.status_code == 422 and errs(r) == [(3, "po_number")]
    assert "pending_approval" in r.json()["errors"][0]["problem"] and counts() == (0, 0)


# ---- file level ----------------------------------------------------------
def test_missing_column_rejects_file(client):
    r = post(client, csv_file("q-1,PO-1003,1,10,0,LOT",
                              header="idempotency_key,po_number,line_no,received_qty,rejected_qty,lot_batch"))
    assert r.status_code == 422 and errs(r) == [(0, "coc_received")] and counts() == (0, 0)


def test_header_only_file_rejected(client):
    r = post(client, csv_file())
    assert r.status_code == 422 and "no data rows" in r.json()["errors"][0]["problem"]


def test_wrong_cell_count(client):
    r = post(client, csv_file("w-1,PO-1003,1,10"))
    assert errs(r) == [(3, "(row)")] and "expected 7 columns, got 4" in r.json()["errors"][0]["problem"]
