"""CSV import P1 tests (PO lines). Expected outcomes written by hand BEFORE running.
The CSV layout is a guess at an Excel sheet. All data here is synthetic.

Test files use this line layout (row_number = physical line):
  line 1 '# SYNTHETIC ...' comment, line 2 header, data rows start at line 3.
Seeded DB has 11 PO lines (PO-1001..PO-1005). Threshold 100,000 INR.

 clean:     3 rows -> 201 imported, 3 read/3 imported/0 rejected, DB 14 lines
            PO-2001 total 1000+1100=2100 -> open ; PO-2002 total 150000 -> pending_approval
 boundary:  qty10 x 10000.00 = 100000 -> open ; qty10 x 10000.01 = 100000.10 -> pending_approval
 bad row:   line 4 qty -5 -> 422 rejected, 3 read/0 imported/1 rejected, 2 valid not applied,
            DB still 11 lines, PO-3001 does not exist
 skip mode: line 3 good; line 4 bad qty; line 5 valid but same PO as line 4 -> skipped;
            line 6 unknown part -> 201 partial, 4 read/1 imported/3 rejected, DB 12 lines
 duplicates: line 4 duplicate of row 3 (field line_no); line 5 existing PO (field po_number)
 many bad fields on line 3: line_no, ordered_qty, unit_price_inr, need_by_date, promised_date
 file-level: missing column -> row 0; header only -> row 0 'no data rows'; empty -> row 0
"""
from decimal import Decimal

import pytest

HEADER = "po_number,line_no,part_number,supplier,ordered_qty,unit_price_inr,need_by_date,promised_date"
TAG = "# SYNTHETIC test file, not real data. Layout is a guess at an Excel sheet."


def csv_file(*rows, header=HEADER):
    return "\n".join([TAG, header, *rows]) + "\n"


@pytest.fixture()
def client(seeded, monkeypatch):
    from fastapi.testclient import TestClient

    from app import config
    from app.main import app
    monkeypatch.setattr(config, "APPROVAL_THRESHOLD_INR", Decimal("100000"))
    return TestClient(app)


def post(client, text, mode=None, raw=None):
    params = {"mode": mode} if mode else {}
    body = raw if raw is not None else text.encode("utf-8")
    return client.post("/import/po-lines", params=params, content=body,
                       headers={"Content-Type": "text/csv"})


def line_count():
    from sqlalchemy import text

    from app.db import get_session
    with get_session() as s:
        return s.execute(text("SELECT count(*) FROM po_lines")).scalar()


def errs(r):
    return [(e["row_number"], e["field"]) for e in r.json()["errors"]]


# ---- happy path --------------------------------------------------------
def test_clean_file(client):
    r = post(client, csv_file(
        "PO-2001,1,FAS-001,Test Supplier A,100,10.00,2026-12-01,2026-11-28",
        "PO-2001,2,FAS-002,Test Supplier A,200,5.50,2026-12-01,",
        "PO-2002,1,SEA-001,Test Supplier B,50,3000.00,2026-12-15,"))
    j = r.json()
    assert r.status_code == 201 and j["status"] == "imported"
    assert (j["rows_read"], j["rows_imported"], j["rows_rejected"], j["errors"]) == (3, 3, 0, [])
    assert j["imported_pos"] == [
        {"po_number": "PO-2001", "lines": 2, "po_total_inr": 2100.0, "status": "open"},
        {"po_number": "PO-2002", "lines": 1, "po_total_inr": 150000.0, "status": "pending_approval"}]
    assert line_count() == 14
    po = client.get("/purchase-orders/PO-2001").json()
    assert {ln["line_no"]: ln["promised_date"] for ln in po["lines"]} == {1: "2026-11-28", 2: None}
    assert client.get("/purchase-orders/PO-2002").json()["status"] == "pending_approval"


def test_threshold_boundary_uses_the_same_rule_as_the_api(client):
    r = post(client, csv_file(
        "PO-2003,1,FAS-001,S,10,10000.00,2026-12-01,",
        "PO-2004,1,FAS-001,S,10,10000.01,2026-12-01,"))
    st = {p["po_number"]: p["status"] for p in r.json()["imported_pos"]}
    assert st == {"PO-2003": "open", "PO-2004": "pending_approval"}


def test_imported_pending_po_cannot_receive_until_approved(client):
    post(client, csv_file("PO-2002,1,SEA-001,S,50,3000.00,2026-12-15,"))
    rcpt = {"idempotency_key": "imp-1", "po_number": "PO-2002", "line_no": 1, "received_qty": 5,
            "rejected_qty": 0, "lot_batch": "L", "coc_received": True}
    assert client.post("/receipts", json=rcpt).status_code == 409
    assert client.post("/purchase-orders/PO-2002/approve").status_code == 200
    assert client.post("/receipts", json=rcpt).status_code == 201


def test_excel_style_bom_crlf_and_loose_headers(client):
    text = ("﻿" + csv_file(
        "PO-2005,1,FAS-001,S,10,10.00,2026-12-01",       # trailing blank promised_date omitted
        header=" PO_Number , LINE_NO ,Part_Number,Supplier,Ordered_Qty,Unit_Price_INR,"
               "Need_By_Date,Promised_Date ")).replace("\n", "\r\n")
    r = post(client, "", raw=text.encode("utf-8"))
    assert r.status_code == 201 and r.json()["rows_imported"] == 1, r.text


def test_extra_columns_are_ignored_and_reported(client):
    r = post(client, csv_file("PO-2006,1,FAS-001,S,10,10.00,2026-12-01,,hello",
                              header=HEADER + ",notes"))
    assert r.status_code == 201 and r.json()["ignored_columns"] == ["notes"]


# ---- all-or-nothing ----------------------------------------------------
def test_bad_row_leaves_database_unchanged(client):
    r = post(client, csv_file(
        "PO-3001,1,FAS-001,S,10,10.00,2026-12-01,",
        "PO-3001,2,FAS-002,S,-5,10.00,2026-12-01,",
        "PO-3002,1,FAS-001,S,5,10.00,2026-12-01,"))
    j = r.json()
    assert r.status_code == 422 and j["status"] == "rejected"
    assert (j["rows_read"], j["rows_imported"], j["rows_rejected"], j["valid_rows_not_applied"]) == (3, 0, 1, 2)
    assert errs(r) == [(4, "ordered_qty")] and "greater than 0" in j["errors"][0]["problem"]
    assert line_count() == 11
    assert client.get("/purchase-orders/PO-3001").status_code == 404


# ---- skip_bad_rows -----------------------------------------------------
SKIP_FILE = csv_file(
    "PO-3001,1,FAS-001,S,10,10.00,2026-12-01,",
    "PO-3002,1,FAS-002,S,-1,10.00,2026-12-01,",
    "PO-3002,2,FAS-001,S,5,10.00,2026-12-01,",
    "PO-3003,1,NOPE-9,S,5,10.00,2026-12-01,")


def test_skip_mode_imports_good_pos_and_skips_whole_bad_po(client):
    r = post(client, SKIP_FILE, mode="skip_bad_rows")
    j = r.json()
    assert r.status_code == 201 and j["status"] == "partial"
    assert (j["rows_read"], j["rows_imported"], j["rows_rejected"]) == (4, 1, 3)
    assert errs(r) == [(4, "ordered_qty"), (5, "po_number"), (6, "part_number")]
    assert "skipped" in j["errors"][1]["problem"] and "unknown part" in j["errors"][2]["problem"]
    assert line_count() == 12
    assert client.get("/purchase-orders/PO-3001").status_code == 200
    assert client.get("/purchase-orders/PO-3002").status_code == 404


def test_same_file_in_default_mode_imports_nothing(client):
    r = post(client, SKIP_FILE)
    assert r.status_code == 422 and r.json()["rows_imported"] == 0 and line_count() == 11


def test_skip_mode_with_nothing_valid_is_422(client):
    r = post(client, csv_file("PO-3001,1,NOPE-1,S,10,10.00,2026-12-01,"), mode="skip_bad_rows")
    assert r.status_code == 422 and r.json()["status"] == "rejected" and line_count() == 11


# ---- duplicates --------------------------------------------------------
def test_duplicate_line_in_file_and_existing_po(client):
    r = post(client, csv_file(
        "PO-4001,1,FAS-001,S,10,10.00,2026-12-01,",
        "PO-4001,1,FAS-002,S,10,10.00,2026-12-01,",
        "PO-1001,9,FAS-001,S,10,10.00,2026-12-01,"))
    assert r.status_code == 422 and errs(r) == [(4, "line_no"), (5, "po_number")]
    e = r.json()["errors"]
    assert "duplicate of row 3" in e[0]["problem"] and "already exists" in e[1]["problem"]
    assert r.json()["rows_rejected"] == 2 and line_count() == 11


def test_existing_po_line_cannot_be_reimported(client):
    r = post(client, csv_file("PO-1001,1,FAS-001,S,10,10.00,2026-12-01,"))
    assert r.status_code == 422 and errs(r) == [(3, "po_number")]


# ---- field validation --------------------------------------------------
def test_one_row_many_bad_fields_one_error_each(client):
    r = post(client, csv_file("PO-5001,x,FAS-001,S,abc,₹5,01/12/2026,2026-13-40"))
    assert r.status_code == 422
    assert {f for _, f in errs(r)} == {"line_no", "ordered_qty", "unit_price_inr",
                                       "need_by_date", "promised_date"}
    assert r.json()["rows_rejected"] == 1                     # five errors, still one row
    p = {e["field"]: e["problem"] for e in r.json()["errors"]}
    assert "not a plain number" in p["ordered_qty"] and "not an ISO date" in p["need_by_date"]
    assert "not a real calendar date" in p["promised_date"]


@pytest.mark.parametrize("qty,price,field,fragment", [
    ("0", "10", "ordered_qty", "greater than 0"),
    ("1.2345", "10", "ordered_qty", "too many decimal places"),
    ('"1,000"', "10", "ordered_qty", "not a plain number"),
    ("", "10", "ordered_qty", "required"),
    ("5", "-1", "unit_price_inr", "must not be negative"),
    ("5", "10.999", "unit_price_inr", "too many decimal places"),
    ("5", "", "unit_price_inr", "required"),
])
def test_numeric_rules(client, qty, price, field, fragment):
    r = post(client, csv_file(f"PO-5002,1,FAS-001,S,{qty},{price},2026-12-01,"))
    assert r.status_code == 422
    e = [x for x in r.json()["errors"] if x["field"] == field]
    assert e and fragment in e[0]["problem"], r.text


def test_wrong_cell_count_is_a_row_error(client):
    r = post(client, csv_file("PO-5003,1,FAS-001,S,10"))
    assert errs(r) == [(3, "(row)")] and "expected 8 columns, got 5" in r.json()["errors"][0]["problem"]


# ---- file level --------------------------------------------------------
def test_missing_required_column_rejects_file(client):
    r = post(client, csv_file("PO-6001,1,FAS-001,10,10.00,2026-12-01,",
                              header="po_number,line_no,part_number,ordered_qty,unit_price_inr,"
                                     "need_by_date,promised_date"))
    assert r.status_code == 422 and errs(r) == [(0, "supplier")]
    assert r.json()["rows_read"] == 0 and line_count() == 11


def test_header_only_and_empty_file(client):
    r1 = post(client, csv_file())
    assert r1.status_code == 422 and errs(r1) == [(0, "(file)")]
    assert "no data rows" in r1.json()["errors"][0]["problem"]
    r2 = post(client, "")
    assert r2.status_code == 422 and errs(r2) == [(0, "(file)")]


def test_invalid_mode_is_rejected_by_the_api(client):
    assert post(client, csv_file("PO-7001,1,FAS-001,S,1,1,2026-12-01,"), mode="yolo").status_code == 422


def test_not_utf8_is_rejected(client):
    r = post(client, "", raw=b"po_number,line_no\n\xff\xfe\x00bad")
    assert r.status_code == 422 and "UTF-8" in r.json()["errors"][0]["problem"]
