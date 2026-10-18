"""Goods receipt with exceptions.

classify_receipt() is a pure function holding the exception rules (decisions D22 to D26).
record_receipt() does the IO: lock, idempotency check, insert receipt + ledger events, update status.
"""
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import config
from app.models import LedgerEvent, Part, POLine, ReceiptEvent
from app.services.po import Conflict, NotFound
from app.services.ledger import _lock_part


class Invalid(Exception):
    pass


class DuplicateReceipt(Conflict):
    """Same idempotency key seen before. Still a Conflict, so the API keeps returning 409."""


def _release(session: Session, commit: bool) -> None:
    """Standalone calls roll back to drop the row lock early. When a caller (the CSV import)
    owns the transaction, it must not be rolled back from in here; the caller's SAVEPOINT does it."""
    if commit:
        session.rollback()


FLAG_ORDER = ["rejected", "missing_coc", "over_delivery", "over_delivery_within_tolerance"]


def classify_receipt(*, ordered: Decimal, kept_so_far: Decimal, received: Decimal,
                     rejected: Decimal, coc_received: bool, tolerance_pct: Decimal) -> dict:
    """Split a delivery into accepted / quarantined / rejected.

    kept_so_far = units already accepted or quarantined on earlier receipts of this line.
    Accepted stock may reach ordered * (1 + tolerance). Beyond that is quarantined and flagged.
    Missing CoC quarantines every otherwise-acceptable unit.
    """
    if received <= 0:
        raise Invalid("received_qty must be greater than 0")
    if rejected < 0 or rejected > received:
        raise Invalid("rejected_qty must be between 0 and received_qty")

    good = received - rejected
    cap = ordered * (1 + tolerance_pct / Decimal(100))
    room = max(Decimal(0), cap - kept_so_far)
    within = min(good, room)
    excess = good - within

    if coc_received:
        accepted, quarantined = within, excess
    else:
        accepted, quarantined = Decimal(0), within + excess

    flags = set()
    if rejected > 0:
        flags.add("rejected")
    if not coc_received:
        flags.add("missing_coc")
    if excess > 0:
        flags.add("over_delivery")
    elif kept_so_far + within > ordered:
        flags.add("over_delivery_within_tolerance")

    return {"accepted": accepted, "quarantined": quarantined, "rejected": rejected,
            "flags": [f for f in FLAG_ORDER if f in flags]}


def record_receipt(session: Session, *, idempotency_key: str, po_number: str, line_no: int,
                   received_qty: Decimal, rejected_qty: Decimal, lot_batch: str,
                   coc_received: bool, commit: bool = True) -> dict:
    line = session.execute(select(POLine).where(
        POLine.po_number == po_number, POLine.line_no == line_no)).scalar_one_or_none()
    if line is None:
        raise NotFound(f"PO line {po_number}/{line_no} not found")

    _lock_part(session, line.part_id)      # serialise writers for this part

    dup = session.execute(select(ReceiptEvent).where(
        ReceiptEvent.idempotency_key == idempotency_key)).scalar_one_or_none()
    if dup is not None:
        _release(session, commit)
        raise DuplicateReceipt(f"duplicate receipt: idempotency key '{idempotency_key}' was already "
                               f"used (receipt_id={dup.id}). Nothing was recorded.")

    if line.status in ("pending_approval", "closed"):
        _release(session, commit)
        raise Conflict(f"PO line {po_number}/{line_no} is '{line.status}' and cannot receive goods")

    kept = Decimal(session.execute(select(func.coalesce(
        func.sum(ReceiptEvent.accepted_qty + ReceiptEvent.quarantined_qty), 0)
    ).where(ReceiptEvent.po_line_id == line.id)).scalar_one())

    try:
        c = classify_receipt(ordered=line.ordered_qty, kept_so_far=kept, received=received_qty,
                             rejected=rejected_qty, coc_received=coc_received,
                             tolerance_pct=config.OVER_DELIVERY_TOLERANCE_PCT)
    except Invalid:
        _release(session, commit)
        raise

    rec = ReceiptEvent(idempotency_key=idempotency_key, po_line_id=line.id,
                       received_qty=received_qty, accepted_qty=c["accepted"],
                       rejected_qty=c["rejected"], quarantined_qty=c["quarantined"],
                       lot_batch=lot_batch, coc_received=coc_received,
                       exception_flags=",".join(c["flags"]))
    session.add(rec)
    try:
        session.flush()
    except IntegrityError:                  # lost a race on the unique key
        _release(session, commit)
        raise DuplicateReceipt(
            f"duplicate receipt: idempotency key '{idempotency_key}' was already used")

    events = [("receipt", received_qty)]
    if c["quarantined"] > 0:
        events.append(("quarantine", c["quarantined"]))
    if c["rejected"] > 0:
        events.append(("reject", c["rejected"]))
    for etype, qty in events:
        session.add(LedgerEvent(part_id=line.part_id, po_line_id=line.id,
                                receipt_event_id=rec.id, event_type=etype, qty=qty))

    kept_total = kept + c["accepted"] + c["quarantined"]
    if kept_total >= line.ordered_qty:
        line.status = "received"
    elif kept_total > 0:
        line.status = "partially_received"

    if commit:
        session.commit()
    else:
        session.flush()                     # caller owns the transaction
    return {
        "id": rec.id, "idempotency_key": idempotency_key, "po_number": po_number,
        "line_no": line_no, "part_id": line.part_id, "received_qty": received_qty,
        "accepted_qty": c["accepted"], "rejected_qty": c["rejected"],
        "quarantined_qty": c["quarantined"], "lot_batch": lot_batch, "coc_received": coc_received,
        "exception_flags": c["flags"], "po_line_status": line.status,
        "outstanding_qty": max(Decimal(0), line.ordered_qty - kept_total),
        "ledger_events": [{"event_type": e, "qty": q} for e, q in events],
    }


def list_receipts(session: Session, po_number: str | None = None) -> list[dict]:
    q = (select(ReceiptEvent, POLine).join(POLine, POLine.id == ReceiptEvent.po_line_id)
         .order_by(ReceiptEvent.id))
    if po_number:
        q = q.where(POLine.po_number == po_number)
    return [{
        "id": r.id, "idempotency_key": r.idempotency_key, "po_number": ln.po_number,
        "line_no": ln.line_no, "part_id": ln.part_id, "received_qty": r.received_qty,
        "accepted_qty": r.accepted_qty, "rejected_qty": r.rejected_qty,
        "quarantined_qty": r.quarantined_qty, "lot_batch": r.lot_batch,
        "coc_received": r.coc_received,
        "exception_flags": [f for f in r.exception_flags.split(",") if f],
    } for r, ln in session.execute(q).all()]
