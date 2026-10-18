"""CSV exports. Read-only: nothing here writes to the database.

Balances come from the same SQL aggregate the API serves (ledger.balances), so an export can
never disagree with GET /ledger/balances.
"""
import csv
import io
from decimal import Decimal

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.models import LedgerEvent, Part, POLine, ReceiptEvent
from app.services import ledger as ledger_svc

# A cell starting with one of these can be run as a formula when the file is opened in Excel.
_FORMULA_TRIGGERS = ("=", "+", "-", "@", "\t", "\r")

# Flags that make a receipt an "exception". over_delivery_within_tolerance is informational only.
EXCEPTION_FLAGS = ["rejected", "missing_coc", "over_delivery"]


def safe(value):
    """Neutralise spreadsheet formulas in text cells; format numbers plainly."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    if hasattr(value, "isoformat"):
        return value.isoformat()
    s = str(value)
    return "'" + s if s.startswith(_FORMULA_TRIGGERS) else s


def to_csv(header: list[str], rows: list[list]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(header)
    for r in rows:
        w.writerow([safe(c) for c in r])
    return buf.getvalue()


def ledger_events_csv(session: Session) -> str:
    q = (select(LedgerEvent, POLine, ReceiptEvent)
         .outerjoin(POLine, POLine.id == LedgerEvent.po_line_id)
         .outerjoin(ReceiptEvent, ReceiptEvent.id == LedgerEvent.receipt_event_id)
         .order_by(LedgerEvent.id))
    rows = [[ev.id, ev.created_at, ev.part_id, ev.event_type, ev.qty,
             ln.po_number if ln else None, ln.line_no if ln else None,
             ev.receipt_event_id, rc.lot_batch if rc else None]
            for ev, ln, rc in session.execute(q).all()]
    return to_csv(["event_id", "created_at", "part_id", "event_type", "qty", "po_number",
                   "line_no", "receipt_id", "lot_batch"], rows)


def balances_csv(session: Session) -> str:
    """One row per part in the parts master, zeros for parts with no events."""
    by_part = {b["part_id"]: b for b in ledger_svc.balances(session)}
    zero = {"available": Decimal(0), "quarantined": Decimal(0), "physical": Decimal(0),
            "rejected": Decimal(0)}
    rows = []
    for p in session.execute(select(Part).order_by(Part.part_id)).scalars():
        b = by_part.get(p.part_id, zero)
        rows.append([p.part_id, p.description, p.category, p.uom,
                     b["available"], b["quarantined"], b["physical"], b["rejected"]])
    return to_csv(["part_id", "description", "category", "uom", "available", "quarantined",
                   "physical", "rejected"], rows)


def exceptions_csv(session: Session) -> str:
    """One row per (receipt, flag). quarantine_remaining is derived from the ledger so a
    released exception is not presented as still stuck."""
    remaining = {rid: Decimal(q) for rid, q in session.execute(text(
        "SELECT receipt_event_id, COALESCE(SUM(CASE event_type WHEN 'quarantine' THEN qty "
        "WHEN 'release_from_quarantine' THEN -qty ELSE 0 END), 0) "
        "FROM ledger_events WHERE receipt_event_id IS NOT NULL GROUP BY receipt_event_id"))}
    q = (select(ReceiptEvent, POLine).join(POLine, POLine.id == ReceiptEvent.po_line_id)
         .order_by(ReceiptEvent.id))
    rows = []
    for rc, ln in session.execute(q).all():
        flags = [f for f in rc.exception_flags.split(",") if f]
        for flag in EXCEPTION_FLAGS:
            if flag in flags:
                rows.append([rc.id, rc.received_at, ln.po_number, ln.line_no, ln.part_id, ln.supplier,
                             rc.lot_batch, rc.coc_received, rc.received_qty, rc.accepted_qty,
                             rc.rejected_qty, rc.quarantined_qty, remaining.get(rc.id, Decimal(0)),
                             flag])
    return to_csv(["receipt_id", "received_at", "po_number", "line_no", "part_id", "supplier",
                   "lot_batch", "coc_received", "received_qty", "accepted_qty", "rejected_qty",
                   "quarantined_qty", "quarantine_remaining", "flag"], rows)
