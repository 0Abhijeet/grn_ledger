"""P1 tests. Expected values were worked out by hand before running:
PO-1001 = 10,250   PO-1002 = 75,300   PO-1003 = 15,700
PO-1004 = 172,500 (only one above the 100,000 threshold)   PO-1005 = 17,550
"""
from collections import defaultdict
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.fixtures import PARTS, PO_LINES
from app.seed import build_po_lines, initial_status

THRESHOLD = Decimal("100000")


# ---- no database needed -------------------------------------------------
def test_fixture_shape():
    assert len(PARTS) == 8
    assert len({row[3] for row in PO_LINES}) == 5          # 5 suppliers
    assert 10 <= len(PO_LINES) <= 12
    assert {p[2] for p in PARTS} == {"fasteners", "connectors", "harness_consumables", "sealants"}


def test_po_totals_by_hand():
    totals = defaultdict(Decimal)
    for po, _l, _p, _s, qty, price, _n, _pr in PO_LINES:
        totals[po] += Decimal(qty) * Decimal(price)
    assert totals == {
        "PO-1001": Decimal("10250"), "PO-1002": Decimal("75300"), "PO-1003": Decimal("15700"),
        "PO-1004": Decimal("172500"), "PO-1005": Decimal("17550"),
    }


def test_exactly_one_po_over_threshold():
    rows = build_po_lines(date(2026, 1, 1), THRESHOLD)
    pending_pos = {r.po_number for r in rows if r.status == "pending_approval"}
    assert pending_pos == {"PO-1004"}


def test_threshold_is_strictly_greater_than():
    assert initial_status(Decimal("100000"), THRESHOLD) == "open"
    assert initial_status(Decimal("100000.01"), THRESHOLD) == "pending_approval"


# ---- database needed ----------------------------------------------------
def test_five_tables_only(seeded):
    from app.db import get_engine
    with get_engine().connect() as c:
        names = {r[0] for r in c.execute(text(
            "SELECT table_name FROM information_schema.tables WHERE table_schema='public'"))}
    assert names == {"parts", "po_lines", "receipt_events", "ledger_events", "invoices"}


def test_seed_counts_and_statuses(seeded):
    from app.db import get_session
    with get_session() as s:
        assert s.execute(text("SELECT count(*) FROM parts")).scalar() == 8
        assert s.execute(text("SELECT count(*) FROM po_lines")).scalar() == 11
        pending = s.execute(text(
            "SELECT count(*) FROM po_lines WHERE status='pending_approval'")).scalar()
        assert pending == 2          # both lines of PO-1004


def test_ledger_is_append_only(seeded):
    from app.db import get_session
    with get_session() as s:
        s.execute(text("INSERT INTO ledger_events (part_id, event_type, qty) "
                       "VALUES ('FAS-001','receipt',10)"))
        with pytest.raises(DBAPIError):
            s.execute(text("UPDATE ledger_events SET qty = 99"))
        s.rollback()
    with get_session() as s:
        s.execute(text("INSERT INTO ledger_events (part_id, event_type, qty) "
                       "VALUES ('FAS-001','receipt',10)"))
        with pytest.raises(DBAPIError):
            s.execute(text("DELETE FROM ledger_events"))
        s.rollback()


def test_receipt_buckets_must_add_up(seeded):
    from app.db import get_session
    with get_session() as s:
        with pytest.raises(IntegrityError):
            s.execute(text(
                "INSERT INTO receipt_events (idempotency_key, po_line_id, received_qty, "
                "accepted_qty, rejected_qty, quarantined_qty, lot_batch, coc_received) "
                "VALUES ('k1', 1, 100, 50, 10, 10, 'L1', true)"))   # 70 != 100
        s.rollback()
