from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import text

from app.config import APPROVAL_THRESHOLD_INR
from app.db import get_session
from app.fixtures import PARTS, PO_LINES
from app.models import POLine, Part


def initial_status(po_total: Decimal, threshold: Decimal) -> str:
    """Approval rule: strictly above threshold waits for approval, otherwise open."""
    return "pending_approval" if po_total > threshold else "open"


def build_po_lines(today: date, threshold: Decimal) -> list[POLine]:
    totals: dict[str, Decimal] = defaultdict(Decimal)
    for po, _ln, _part, _sup, qty, price, _nb, _pr in PO_LINES:
        totals[po] += Decimal(qty) * Decimal(price)

    rows = []
    for po, ln, part, sup, qty, price, nb, pr in PO_LINES:
        rows.append(POLine(
            po_number=po, line_no=ln, part_id=part, supplier=sup,
            ordered_qty=Decimal(qty), unit_price_inr=Decimal(price),
            need_by_date=today + timedelta(days=nb),
            promised_date=None if pr is None else today + timedelta(days=pr),
            status=initial_status(totals[po], threshold),
        ))
    return rows


def seed(today: date | None = None) -> None:
    """Wipe and reseed. TRUNCATE is used because ledger_events blocks DELETE by design."""
    today = today or date.today()
    with get_session() as s:
        s.execute(text(
            "TRUNCATE invoices, ledger_events, receipt_events, po_lines, parts RESTART IDENTITY CASCADE"))
        s.add_all([Part(part_id=p, description=d, category=c, uom=u) for p, d, c, u in PARTS])
        s.flush()
        s.add_all(build_po_lines(today, APPROVAL_THRESHOLD_INR))
        s.commit()
