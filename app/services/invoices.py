"""Invoice entry and per-line 3-way match (PO vs GRN accepted vs invoice).

match_line() is a pure function. Match results are derived on every read, never stored,
so a later receipt or quarantine release can turn an old mismatch into a match.
"""
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Invoice, LedgerEvent, POLine, ReceiptEvent
from app.services.po import Conflict, NotFound

REASON_ORDER = ["no_receipt", "qty_exceeds_accepted", "qty_exceeds_po", "price_differs_from_po"]


def match_line(*, po_qty: Decimal, po_price: Decimal, has_receipt: bool, accepted_qty: Decimal,
               cumulative_invoiced: Decimal, invoice_price: Decimal) -> list[str]:
    """Return reason codes; empty list means the line matches.

    cumulative_invoiced includes this invoice line and all earlier invoice lines on the PO line.
    """
    reasons = []
    if not has_receipt:
        reasons.append("no_receipt")
    elif cumulative_invoiced > accepted_qty:
        reasons.append("qty_exceeds_accepted")
    if cumulative_invoiced > po_qty:
        reasons.append("qty_exceeds_po")
    if invoice_price != po_price:
        reasons.append("price_differs_from_po")
    return reasons


def _accepted_and_receipts(session: Session) -> tuple[dict[int, Decimal], set[int]]:
    """accepted per line = receipt accepted_qty + units released from quarantine."""
    acc: dict[int, Decimal] = {}
    has_receipt: set[int] = set()
    for line_id, qty in session.execute(
            select(ReceiptEvent.po_line_id, func.sum(ReceiptEvent.accepted_qty))
            .group_by(ReceiptEvent.po_line_id)):
        acc[line_id] = Decimal(qty)
        has_receipt.add(line_id)
    for line_id, qty in session.execute(
            select(LedgerEvent.po_line_id, func.sum(LedgerEvent.qty))
            .where(LedgerEvent.event_type == "release_from_quarantine")
            .group_by(LedgerEvent.po_line_id)):
        acc[line_id] = acc.get(line_id, Decimal(0)) + Decimal(qty)
    return acc, has_receipt


def evaluate(session: Session, invoice_number: str | None = None) -> list[dict]:
    accepted, has_receipt = _accepted_and_receipts(session)
    rows = session.execute(
        select(Invoice, POLine).join(POLine, POLine.id == Invoice.po_line_id)
        .order_by(Invoice.id)).all()
    cumulative: dict[int, Decimal] = {}
    out = []
    for inv, ln in rows:
        cumulative[ln.id] = cumulative.get(ln.id, Decimal(0)) + inv.invoice_qty
        reasons = match_line(
            po_qty=ln.ordered_qty, po_price=ln.unit_price_inr,
            has_receipt=ln.id in has_receipt, accepted_qty=accepted.get(ln.id, Decimal(0)),
            cumulative_invoiced=cumulative[ln.id], invoice_price=inv.invoice_unit_price_inr)
        if invoice_number and inv.invoice_number != invoice_number:
            continue
        out.append({
            "invoice_number": inv.invoice_number, "invoice_date": inv.invoice_date,
            "po_number": ln.po_number, "line_no": ln.line_no, "part_id": ln.part_id,
            "invoice_qty": inv.invoice_qty, "invoice_unit_price_inr": inv.invoice_unit_price_inr,
            "invoice_value_inr": inv.invoice_qty * inv.invoice_unit_price_inr,
            "po_ordered_qty": ln.ordered_qty, "po_unit_price_inr": ln.unit_price_inr,
            "accepted_qty": accepted.get(ln.id, Decimal(0)),
            "cumulative_invoiced_qty": cumulative[ln.id],
            "match": not reasons,
            "reasons": [r for r in REASON_ORDER if r in reasons],
        })
    return out


def group(lines: list[dict]) -> list[dict]:
    by: dict[str, list[dict]] = {}
    for ln in lines:
        by.setdefault(ln["invoice_number"], []).append(ln)
    return [{"invoice_number": n, "invoice_date": ls[0]["invoice_date"],
             "overall_match": all(x["match"] for x in ls), "lines": ls} for n, ls in by.items()]


def create_invoice(session: Session, invoice_number: str, invoice_date: date,
                   lines: list[dict]) -> dict:
    """All-or-nothing: any unknown line or duplicate rolls back the whole invoice."""
    for ln in lines:
        po = session.execute(select(POLine).where(
            POLine.po_number == ln["po_number"], POLine.line_no == ln["line_no"])).scalar_one_or_none()
        if po is None:
            session.rollback()
            raise NotFound(f"PO line {ln['po_number']}/{ln['line_no']} not found; invoice not recorded")
        session.add(Invoice(invoice_number=invoice_number, po_line_id=po.id,
                            invoice_qty=ln["invoice_qty"],
                            invoice_unit_price_inr=ln["invoice_unit_price_inr"],
                            invoice_date=invoice_date))
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise Conflict(f"invoice {invoice_number} already has a line for one of these PO lines "
                       f"(or the request repeats a line); nothing was recorded")
    return group(evaluate(session, invoice_number))[0]
