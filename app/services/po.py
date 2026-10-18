"""PO logic. Pure rules (delivery_flags) are kept separate from DB access so they can be
tested without a database."""
from collections import defaultdict
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import POLine, ReceiptEvent

ACTIVE_STATUSES = ("open", "partially_received")


class NotFound(Exception):
    pass


class Conflict(Exception):
    pass


def delivery_flags(*, need_by: date, promised: date | None, outstanding: Decimal,
                   status: str, as_of: date) -> dict[str, bool]:
    """Derived, never stored. See DECISIONS.md D13 to D16 for the definitions."""
    none = {"overdue": False, "at_risk": False, "no_promise_date": False}
    if status not in ACTIVE_STATUSES or outstanding <= 0:
        return none
    overdue = need_by < as_of or (promised is not None and promised < as_of)
    at_risk = promised is not None and promised > need_by
    return {"overdue": overdue, "at_risk": at_risk, "no_promise_date": promised is None}


def _received_by_line(session: Session) -> dict[int, tuple[Decimal, Decimal]]:
    rows = session.execute(
        select(ReceiptEvent.po_line_id,
               func.coalesce(func.sum(ReceiptEvent.accepted_qty), 0),
               func.coalesce(func.sum(ReceiptEvent.quarantined_qty), 0))
        .group_by(ReceiptEvent.po_line_id)).all()
    return {r[0]: (Decimal(r[1]), Decimal(r[2])) for r in rows}


def load_lines(session: Session, po_number: str | None = None,
               as_of: date | None = None) -> list[dict]:
    as_of = as_of or date.today()
    q = select(POLine).order_by(POLine.po_number, POLine.line_no)
    if po_number:
        q = q.where(POLine.po_number == po_number)
    received = _received_by_line(session)
    out = []
    for ln in session.execute(q).scalars():
        accepted, quarantined = received.get(ln.id, (Decimal(0), Decimal(0)))
        outstanding = max(Decimal(0), ln.ordered_qty - accepted - quarantined)
        out.append({
            "po_number": ln.po_number, "line_no": ln.line_no, "part_id": ln.part_id,
            "supplier": ln.supplier, "ordered_qty": ln.ordered_qty,
            "unit_price_inr": ln.unit_price_inr,
            "line_value_inr": ln.ordered_qty * ln.unit_price_inr,
            "need_by_date": ln.need_by_date, "promised_date": ln.promised_date,
            "status": ln.status,
            "accepted_qty": accepted, "quarantined_qty": quarantined,
            "outstanding_qty": outstanding,
            **delivery_flags(need_by=ln.need_by_date, promised=ln.promised_date,
                             outstanding=outstanding, status=ln.status, as_of=as_of),
        })
    return out


def summarise(lines: list[dict]) -> list[dict]:
    by_po: dict[str, list[dict]] = defaultdict(list)
    for ln in lines:
        by_po[ln["po_number"]].append(ln)
    result = []
    for po, ls in by_po.items():
        statuses = {x["status"] for x in ls}
        result.append({
            "po_number": po,
            "po_total_inr": sum((x["line_value_inr"] for x in ls), Decimal(0)),
            "line_count": len(ls),
            "status": statuses.pop() if len(statuses) == 1 else "mixed",
            "suppliers": sorted({x["supplier"] for x in ls}),
        })
    return result


def set_promised_date(session: Session, po_number: str, line_no: int,
                      promised: date | None) -> None:
    """Record a supplier's new promise. Needed so 'promised date moved' can be shown."""
    ln = session.execute(select(POLine).where(
        POLine.po_number == po_number, POLine.line_no == line_no)).scalar_one_or_none()
    if ln is None:
        raise NotFound(f"PO line {po_number}/{line_no} not found")
    if ln.status in ("received", "closed"):
        raise Conflict(f"PO line {po_number}/{line_no} is '{ln.status}'; promised date is frozen")
    ln.promised_date = promised
    session.commit()


def approve_po(session: Session, po_number: str) -> int:
    """Flip every line of a pending PO to open. Returns number of lines changed."""
    lines = session.execute(select(POLine).where(POLine.po_number == po_number)).scalars().all()
    if not lines:
        raise NotFound(f"PO {po_number} not found")
    pending = [ln for ln in lines if ln.status == "pending_approval"]
    if not pending:
        raise Conflict(f"PO {po_number} is not pending approval "
                       f"(status: {sorted({ln.status for ln in lines})})")
    for ln in pending:
        ln.status = "open"
    session.commit()
    return len(pending)
