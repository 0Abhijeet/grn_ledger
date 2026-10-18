"""Runs the real files in samples/ and checks the results written in samples/README.md
BEFORE the first run. Synthetic data. The layout is a guess at an Excel sheet."""
import csv
import io
from pathlib import Path

import pytest
from sqlalchemy import text

SAMPLES = Path("samples")
PO_FILE = SAMPLES / "synthetic_po_lines.csv"
RCPT_FILE = SAMPLES / "synthetic_receipts.csv"


@pytest.fixture()
def client(seeded):
    from fastapi.testclient import TestClient

    from app.main import app
    return TestClient(app)


def upload(client, path, endpoint, mode=None):
    return client.post(f"/import/{endpoint}", params={"mode": mode} if mode else {},
                       content=path.read_bytes(), headers={"Content-Type": "text/csv"})


def scalar(sql):
    from app.db import get_session
    with get_session() as s:
        return s.execute(text(sql)).scalar()


def errs(r):
    return [(e["row_number"], e["field"]) for e in r.json()["errors"]]


@pytest.fixture()
def with_pos(client):
    assert upload(client, PO_FILE, "po-lines").status_code == 201
    return client


# ---- the files themselves ------------------------------------------------
@pytest.mark.parametrize("path", [PO_FILE, RCPT_FILE])
def test_samples_are_labelled_synthetic(path):
    assert path.name.startswith("synthetic_")
    first = path.read_text(encoding="utf-8").splitlines()[0]
    assert first.startswith("# SYNTHETIC")


def test_sample_sizes():
    def data_rows(p):
        lines = [x for x in p.read_text(encoding="utf-8").splitlines() if x and not x.startswith("#")]
        return len(lines) - 1
    assert data_rows(PO_FILE) == 6 and data_rows(RCPT_FILE) == 6


# ---- 1. PO sample --------------------------------------------------------
def test_po_sample_expected_result(client):
    r = upload(client, PO_FILE, "po-lines")
    j = r.json()
    assert r.status_code == 201 and j["status"] == "imported" and j["errors"] == []
    assert (j["rows_read"], j["rows_imported"], j["rows_rejected"]) == (6, 6, 0)
    assert j["imported_pos"] == [
        {"po_number": "PO-9001", "lines": 2, "po_total_inr": 4100.0, "status": "open"},
        {"po_number": "PO-9002", "lines": 2, "po_total_inr": 87500.0, "status": "open"},
        {"po_number": "PO-9003", "lines": 2, "po_total_inr": 172500.0, "status": "pending_approval"}]


def test_po_sample_twice_is_rejected_and_changes_nothing(with_pos):
    before = scalar("SELECT count(*) FROM po_lines")
    r = upload(with_pos, PO_FILE, "po-lines")
    assert r.status_code == 422 and r.json()["rows_rejected"] == 6
    assert all("already exists" in e["problem"] for e in r.json()["errors"])
    assert scalar("SELECT count(*) FROM po_lines") == before


# ---- 2. receipts sample, default mode ------------------------------------
def test_receipts_sample_default_mode_rejects_whole_file(with_pos):
    r = upload(with_pos, RCPT_FILE, "receipts")
    j = r.json()
    assert r.status_code == 422 and j["status"] == "rejected"
    assert (j["rows_read"], j["rows_imported"], j["rows_rejected"], j["valid_rows_not_applied"]) == (6, 0, 2, 4)
    assert errs(r) == [(7, "idempotency_key"), (8, "coc_received")]
    assert "duplicate of row 3" in j["errors"][0]["problem"]
    assert "not a recognised yes/no value: 'maybe'" in j["errors"][1]["problem"]
    assert scalar("SELECT count(*) FROM receipt_events") == 0
    assert scalar("SELECT count(*) FROM ledger_events") == 0


# ---- 3. receipts sample, skip_bad_rows -----------------------------------
def test_receipts_sample_skip_mode_expected_result(with_pos):
    r = upload(with_pos, RCPT_FILE, "receipts", mode="skip_bad_rows")
    j = r.json()
    assert r.status_code == 201 and j["status"] == "partial"
    assert (j["rows_read"], j["rows_imported"], j["rows_rejected"]) == (6, 4, 2)
    assert errs(r) == [(7, "idempotency_key"), (8, "coc_received")]
    got = {x["row_number"]: (x["po_number"], x["line_no"], x["part_id"], x["accepted_qty"],
                             x["rejected_qty"], x["quarantined_qty"], x["exception_flags"],
                             x["po_line_status"], x["outstanding_qty"]) for x in j["imported_receipts"]}
    assert got == {
        3: ("PO-9001", 1, "FAS-001", 200, 0, 0, [], "received", 0),
        4: ("PO-9001", 2, "FAS-002", 120, 0, 0, [], "partially_received", 80),
        5: ("PO-9002", 1, "CON-001", 32, 8, 0, ["rejected"], "partially_received", 8),
        6: ("PO-9002", 2, "HAR-001", 0, 0, 300, ["missing_coc"], "received", 0)}
    assert scalar("SELECT count(*) FROM receipt_events") == 4
    assert scalar("SELECT count(*) FROM ledger_events") == 6
    bal = {b["part_id"]: (b["available"], b["quarantined"], b["physical"], b["rejected"])
           for b in with_pos.get("/ledger/balances").json()}
    assert bal == {"FAS-001": (200, 0, 200, 0), "FAS-002": (120, 0, 120, 0),
                   "CON-001": (32, 0, 32, 8), "HAR-001": (0, 300, 300, 0)}
    replay = with_pos.get("/ledger/replay").json()
    assert all(x["match"] and not x["ever_negative"] for x in replay)


# ---- 4. exceptions export -------------------------------------------------
def test_exceptions_export_after_skip_import(with_pos):
    upload(with_pos, RCPT_FILE, "receipts", mode="skip_bad_rows")
    rows = list(csv.DictReader(io.StringIO(with_pos.get("/export/exceptions.csv").text)))
    got = [(x["po_number"], x["line_no"], x["part_id"], x["lot_batch"], x["coc_received"],
            x["received_qty"], x["accepted_qty"], x["rejected_qty"], x["quarantined_qty"],
            x["quarantine_remaining"], x["flag"]) for x in rows]
    assert got == [
        ("PO-9002", "1", "CON-001", "LOT-S3", "yes", "40", "32", "8", "0", "0", "rejected"),
        ("PO-9002", "2", "HAR-001", "LOT-S4", "no", "300", "0", "0", "300", "300", "missing_coc")]


# ---- 5. idempotency --------------------------------------------------------
def test_rerunning_skip_import_applies_nothing_twice(with_pos):
    upload(with_pos, RCPT_FILE, "receipts", mode="skip_bad_rows")
    events_before = scalar("SELECT count(*) FROM ledger_events")
    r = upload(with_pos, RCPT_FILE, "receipts", mode="skip_bad_rows")
    j = r.json()
    assert r.status_code == 422 and (j["rows_imported"], j["rows_rejected"]) == (0, 6)
    probs = {e["row_number"]: e["problem"] for e in j["errors"] if e["field"] == "idempotency_key"}
    assert all("duplicate receipt" in probs[n] for n in (3, 4, 5, 6)) and "duplicate of row 3" in probs[7]
    assert scalar("SELECT count(*) FROM ledger_events") == events_before
