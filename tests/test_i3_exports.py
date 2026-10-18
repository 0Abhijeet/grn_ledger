"""CSV export tests + import->export round trip. Expected outcomes written by hand BEFORE running.
Synthetic data. The CSV layout is a guess at an Excel sheet.

Import (4 receipts):
  r-1 PO-1001/2 FAS-002 500 rej0 LOT-A yes     r-2 PO-1003/1 HAR-001 120 rej0 LOT-B yes
  r-3 PO-1001/1 FAS-001 500 rej100 LOT-C no    r-4 PO-1002/2 CON-002 215 rej0 LOT-D yes

ledger-events.csv, 7 rows in id order (part, type, qty, po, line, receipt_id, lot):
  FAS-002 receipt 500  PO-1001 2 1 LOT-A | HAR-001 receipt 120 PO-1003 1 2 LOT-B
  FAS-001 receipt 500  PO-1001 1 3 LOT-C | FAS-001 quarantine 400 | FAS-001 reject 100
  CON-002 receipt 215  PO-1002 2 4 LOT-D | CON-002 quarantine 5
balances.csv, all 8 parts (available, quarantined, physical, rejected):
  FAS-001 0,400,400,100 | FAS-002 500,0,500,0 | CON-001 0,0,0,0 | CON-002 210,5,215,0
  HAR-001 120,0,120,0 | HAR-002 0,0,0,0 | SEA-001 0,0,0,0 | SEA-002 0,0,0,0
exceptions.csv, 3 rows: receipt 3 'rejected' (remaining 400), receipt 3 'missing_coc' (400),
  receipt 4 'over_delivery' (remaining 5). Receipts 1 and 2 (clean/partial) absent.
after releasing 400 from receipt 3: its two rows show quarantine_remaining 0; receipt 4 still 5.
"""
import csv
import io
from decimal import Decimal

import pytest
from sqlalchemy import text

HEADER = "idempotency_key,po_number,line_no,received_qty,rejected_qty,lot_batch,coc_received"
IMPORT = "\n".join([
    "# SYNTHETIC test file, not real data. Layout is a guess at an Excel sheet.", HEADER,
    "r-1,PO-1001,2,500,0,LOT-A,yes",
    "r-2,PO-1003,1,120,0,LOT-B,TRUE",
    "r-3,PO-1001,1,500,100,LOT-C,no",
    "r-4,PO-1002,2,215,0,LOT-D,1"]) + "\n"


@pytest.fixture()
def client(seeded):
    from fastapi.testclient import TestClient

    from app.main import app
    return TestClient(app)


@pytest.fixture()
def loaded(client):
    r = client.post("/import/receipts", content=IMPORT.encode(), headers={"Content-Type": "text/csv"})
    assert r.status_code == 201, r.text
    return client


def rows_of(resp) -> list[dict]:
    assert resp.status_code == 200
    return list(csv.DictReader(io.StringIO(resp.text)))


def db_counts():
    from app.db import get_session
    with get_session() as s:
        return tuple(s.execute(text(f"SELECT count(*) FROM {t}")).scalar()
                     for t in ("receipt_events", "ledger_events", "po_lines", "invoices"))


# ---- ledger events -------------------------------------------------------
def test_ledger_events_export(loaded):
    r = loaded.get("/export/ledger-events.csv")
    assert r.headers["content-type"].startswith("text/csv")
    assert 'filename="ledger-events.csv"' in r.headers["content-disposition"]
    rows = rows_of(r)
    got = [(x["part_id"], x["event_type"], x["qty"], x["po_number"], x["line_no"],
            x["receipt_id"], x["lot_batch"]) for x in rows]
    assert got == [
        ("FAS-002", "receipt", "500", "PO-1001", "2", "1", "LOT-A"),
        ("HAR-001", "receipt", "120", "PO-1003", "1", "2", "LOT-B"),
        ("FAS-001", "receipt", "500", "PO-1001", "1", "3", "LOT-C"),
        ("FAS-001", "quarantine", "400", "PO-1001", "1", "3", "LOT-C"),
        ("FAS-001", "reject", "100", "PO-1001", "1", "3", "LOT-C"),
        ("CON-002", "receipt", "215", "PO-1002", "2", "4", "LOT-D"),
        ("CON-002", "quarantine", "5", "PO-1002", "2", "4", "LOT-D")]
    assert [int(x["event_id"]) for x in rows] == sorted(int(x["event_id"]) for x in rows)


def test_issue_and_release_events_have_no_po_context_when_unlinked(loaded):
    loaded.post("/ledger/issue", json={"part_id": "FAS-002", "qty": 50})
    last = rows_of(loaded.get("/export/ledger-events.csv"))[-1]
    assert (last["event_type"], last["qty"], last["po_number"], last["receipt_id"]) == ("issue", "50", "", "")


# ---- balances ------------------------------------------------------------
def test_balances_export_lists_every_part(loaded):
    rows = rows_of(loaded.get("/export/balances.csv"))
    got = {x["part_id"]: (x["available"], x["quarantined"], x["physical"], x["rejected"]) for x in rows}
    assert got == {
        "FAS-001": ("0", "400", "400", "100"), "FAS-002": ("500", "0", "500", "0"),
        "CON-001": ("0", "0", "0", "0"), "CON-002": ("210", "5", "215", "0"),
        "HAR-001": ("120", "0", "120", "0"), "HAR-002": ("0", "0", "0", "0"),
        "SEA-001": ("0", "0", "0", "0"), "SEA-002": ("0", "0", "0", "0")}
    assert [x["part_id"] for x in rows] == sorted(got)


def test_balances_export_equals_the_api(loaded):
    api = {b["part_id"]: b for b in loaded.get("/ledger/balances").json()}
    for x in rows_of(loaded.get("/export/balances.csv")):
        if x["part_id"] in api:
            b = api[x["part_id"]]
            assert (float(x["available"]), float(x["quarantined"]), float(x["physical"]),
                    float(x["rejected"])) == (b["available"], b["quarantined"], b["physical"], b["rejected"])


def test_balances_recomputed_from_the_events_export_match_the_balances_export(loaded):
    """Independent round trip: fold the exported events with my own sign table, compare."""
    loaded.post("/ledger/release", json={"receipt_id": 3, "qty": 150})
    loaded.post("/ledger/issue", json={"part_id": "FAS-002", "qty": 70})
    sign = {"receipt": 1, "release_from_quarantine": 1, "quarantine": -1, "reject": -1, "issue": -1}
    avail: dict[str, Decimal] = {}
    for e in rows_of(loaded.get("/export/ledger-events.csv")):
        avail[e["part_id"]] = avail.get(e["part_id"], Decimal(0)) + sign[e["event_type"]] * Decimal(e["qty"])
    exported = {x["part_id"]: Decimal(x["available"]) for x in rows_of(loaded.get("/export/balances.csv"))}
    for part, v in avail.items():
        assert exported[part] == v, part
    assert exported["FAS-001"] == 150 and exported["FAS-002"] == 430


# ---- exceptions ----------------------------------------------------------
def test_exceptions_export(loaded):
    rows = rows_of(loaded.get("/export/exceptions.csv"))
    got = [(x["receipt_id"], x["po_number"], x["line_no"], x["part_id"], x["lot_batch"], x["flag"],
            x["received_qty"], x["accepted_qty"], x["rejected_qty"], x["quarantined_qty"],
            x["quarantine_remaining"]) for x in rows]
    assert got == [
        ("3", "PO-1001", "1", "FAS-001", "LOT-C", "rejected", "500", "0", "100", "400", "400"),
        ("3", "PO-1001", "1", "FAS-001", "LOT-C", "missing_coc", "500", "0", "100", "400", "400"),
        ("4", "PO-1002", "2", "CON-002", "LOT-D", "over_delivery", "215", "210", "0", "5", "5")]
    assert rows[0]["coc_received"] == "no" and rows[2]["coc_received"] == "yes"
    assert {x["receipt_id"] for x in rows}.isdisjoint({"1", "2"})


def test_released_quarantine_shows_remaining_zero(loaded):
    loaded.post("/ledger/release", json={"receipt_id": 3, "qty": 400})
    rem = {(x["receipt_id"], x["flag"]): x["quarantine_remaining"]
           for x in rows_of(loaded.get("/export/exceptions.csv"))}
    assert rem == {("3", "rejected"): "0", ("3", "missing_coc"): "0", ("4", "over_delivery"): "5"}


def test_every_receipt_that_quarantined_something_appears_in_exceptions(loaded):
    quarantined = {x["receipt_id"] for x in rows_of(loaded.get("/export/ledger-events.csv"))
                   if x["event_type"] == "quarantine"}
    in_exceptions = {x["receipt_id"] for x in rows_of(loaded.get("/export/exceptions.csv"))}
    assert quarantined and quarantined <= in_exceptions


def test_within_tolerance_overage_is_not_an_exception_row(client):
    # 103 against ordered 100-style tolerance: PO-1002/2 ordered 200, cap 210; receive 205 -> accepted
    client.post("/receipts", json={"idempotency_key": "w-1", "po_number": "PO-1002", "line_no": 2,
                                   "received_qty": 205, "rejected_qty": 0, "lot_batch": "L",
                                   "coc_received": True})
    assert rows_of(client.get("/export/exceptions.csv")) == []


# ---- round trip ----------------------------------------------------------
def test_round_trip_import_then_export_preserves_every_quantity(loaded):
    imported = [r for r in csv.DictReader(io.StringIO(IMPORT.split("\n", 1)[1]))]
    events = rows_of(loaded.get("/export/ledger-events.csv"))
    for row in imported:
        rec = [e for e in events if e["event_type"] == "receipt" and e["po_number"] == row["po_number"]
               and e["line_no"] == row["line_no"] and e["lot_batch"] == row["lot_batch"]]
        assert len(rec) == 1
        assert Decimal(rec[0]["qty"]) == Decimal(row["received_qty"])
        rej = [e for e in events if e["event_type"] == "reject" and e["lot_batch"] == row["lot_batch"]]
        assert sum((Decimal(e["qty"]) for e in rej), Decimal(0)) == Decimal(row["rejected_qty"] or 0)
    replay = loaded.get("/ledger/replay").json()
    assert all(r["match"] and not r["ever_negative"] for r in replay)


# ---- safety and side effects ---------------------------------------------
def test_formula_text_is_neutralised(client):
    body = ("idempotency_key,po_number,line_no,received_qty,rejected_qty,lot_batch,coc_received\n"
            "inj-1,PO-1003,1,10,0,=1+1,yes\n"
            "inj-2,PO-1001,2,10,0,@SUM(A1),yes\n"
            "inj-3,PO-1002,1,10,0,-2+3,yes\n"
            "inj-4,PO-1005,1,10,0,+cmd,yes\n")
    assert client.post("/import/receipts", content=body.encode(),
                       headers={"Content-Type": "text/csv"}).status_code == 201
    lots = {x["receipt_id"]: x["lot_batch"] for x in rows_of(client.get("/export/ledger-events.csv"))}
    assert sorted(lots.values()) == sorted(["'=1+1", "'@SUM(A1)", "'-2+3", "'+cmd"])
    from app.services.exports import safe
    assert safe("LOT-9") == "LOT-9" and safe(Decimal("12.500")) == "12.5" and safe(None) == ""


def test_exports_are_read_only(loaded):
    before = db_counts()
    for path in ("ledger-events", "balances", "exceptions"):
        assert loaded.get(f"/export/{path}.csv").status_code == 200
    assert db_counts() == before


def test_empty_database_exports_only_headers(client):
    ev = client.get("/export/ledger-events.csv").text.strip().splitlines()
    ex = client.get("/export/exceptions.csv").text.strip().splitlines()
    assert len(ev) == 1 and ev[0].startswith("event_id,") and len(ex) == 1
    assert len(rows_of(client.get("/export/balances.csv"))) == 8
