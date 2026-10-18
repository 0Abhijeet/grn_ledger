"""Stock ledger. Events are append-only facts; balances are always derived.

Sign model (decision D21). qty is always positive; the event type gives direction.
  available   = receipt - quarantine - reject + release_from_quarantine - issue
  quarantined = quarantine - release_from_quarantine
  physical    = receipt - reject - issue          (== available + quarantined)
  rejected    = reject (cumulative, informational)

Two independent implementations on purpose:
  balances()  one SQL aggregate (what the API normally serves)
  replay()    a Python fold over events in order, which also checks nothing went negative
Tests assert they agree.
"""
from decimal import Decimal

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.models import LedgerEvent, Part
from app.services.po import Conflict, NotFound

# event_type -> (available, quarantined, physical, rejected) multipliers
EFFECTS = {
    "receipt": (1, 0, 1, 0),
    "quarantine": (-1, 1, 0, 0),
    "reject": (-1, 0, -1, 1),
    "release_from_quarantine": (1, -1, 0, 0),
    "issue": (-1, 0, -1, 0),
}

_BALANCE_SQL = """
SELECT part_id,
  COALESCE(SUM(CASE event_type
    WHEN 'receipt' THEN qty WHEN 'release_from_quarantine' THEN qty
    WHEN 'quarantine' THEN -qty WHEN 'reject' THEN -qty WHEN 'issue' THEN -qty ELSE 0 END), 0) AS available,
  COALESCE(SUM(CASE event_type
    WHEN 'quarantine' THEN qty WHEN 'release_from_quarantine' THEN -qty ELSE 0 END), 0) AS quarantined,
  COALESCE(SUM(CASE event_type
    WHEN 'receipt' THEN qty WHEN 'reject' THEN -qty WHEN 'issue' THEN -qty ELSE 0 END), 0) AS physical,
  COALESCE(SUM(CASE event_type WHEN 'reject' THEN qty ELSE 0 END), 0) AS rejected
FROM ledger_events
{where}
GROUP BY part_id
ORDER BY part_id
"""


def _row(part_id, available, quarantined, physical, rejected) -> dict:
    return {"part_id": part_id, "available": Decimal(available), "quarantined": Decimal(quarantined),
            "physical": Decimal(physical), "rejected": Decimal(rejected)}


def balances(session: Session, part_id: str | None = None) -> list[dict]:
    """SQL-derived balances. Parts with no events are absent unless asked for by id."""
    if part_id:
        sql = _BALANCE_SQL.format(where="WHERE part_id = :p")
        rows = session.execute(text(sql), {"p": part_id}).all()
        if not rows:
            if session.get(Part, part_id) is None:
                raise NotFound(f"part {part_id} not found")
            return [_row(part_id, 0, 0, 0, 0)]
    else:
        rows = session.execute(text(_BALANCE_SQL.format(where=""))).all()
    return [_row(*r) for r in rows]


def replay(session: Session, part_id: str | None = None) -> list[dict]:
    """Rebuild balances by folding events in id order. Flags any moment available went negative."""
    q = select(LedgerEvent).order_by(LedgerEvent.id)
    if part_id:
        q = q.where(LedgerEvent.part_id == part_id)
    state: dict[str, list[Decimal]] = {}
    negative: dict[str, bool] = {}
    for ev in session.execute(q).scalars():
        s = state.setdefault(ev.part_id, [Decimal(0)] * 4)
        for i, mult in enumerate(EFFECTS[ev.event_type]):
            s[i] += mult * ev.qty
        if s[0] < 0 or s[1] < 0 or s[2] < 0:
            negative[ev.part_id] = True
    return [{**_row(p, *vals), "ever_negative": negative.get(p, False)}
            for p, vals in sorted(state.items())]


def compare(session: Session, part_id: str | None = None) -> list[dict]:
    """Replay vs SQL-derived, per part. This is the invariant check."""
    derived = {b["part_id"]: b for b in balances(session, part_id)}
    out = []
    for r in replay(session, part_id):
        d = derived.get(r["part_id"])
        keys = ("available", "quarantined", "physical", "rejected")
        out.append({"part_id": r["part_id"],
                    "replayed": {k: r[k] for k in keys},
                    "derived": {k: d[k] for k in keys} if d else None,
                    "match": bool(d) and all(r[k] == d[k] for k in keys),
                    "ever_negative": r["ever_negative"]})
    return out


def _lock_part(session: Session, part_id: str) -> None:
    """Row lock so concurrent ledger writes for one part are serialised."""
    if session.execute(select(Part).where(Part.part_id == part_id).with_for_update()
                       ).scalar_one_or_none() is None:
        raise NotFound(f"part {part_id} not found")


def _available(session: Session, part_id: str) -> Decimal:
    return balances(session, part_id)[0]["available"]


def issue(session: Session, part_id: str, qty: Decimal) -> dict:
    _lock_part(session, part_id)
    avail = _available(session, part_id)
    if qty > avail:
        session.rollback()
        raise Conflict(f"cannot issue {qty} of {part_id}: only {avail} available "
                       f"(quarantined and rejected stock is excluded)")
    ev = LedgerEvent(part_id=part_id, event_type="issue", qty=qty)
    session.add(ev)
    session.commit()
    return {"event_id": ev.id, "part_id": part_id, "issued": qty, "available_after": avail - qty}


def release_from_quarantine(session: Session, receipt_event_id: int, qty: Decimal) -> dict:
    from app.models import ReceiptEvent
    rec = session.get(ReceiptEvent, receipt_event_id)
    if rec is None:
        raise NotFound(f"receipt {receipt_event_id} not found")
    line = rec.po_line_id
    part_id = session.execute(text("SELECT part_id FROM po_lines WHERE id = :i"),
                              {"i": line}).scalar_one()
    _lock_part(session, part_id)
    remaining = session.execute(text(
        "SELECT COALESCE(SUM(CASE event_type WHEN 'quarantine' THEN qty "
        "WHEN 'release_from_quarantine' THEN -qty ELSE 0 END), 0) "
        "FROM ledger_events WHERE receipt_event_id = :r"), {"r": receipt_event_id}).scalar_one()
    remaining = Decimal(remaining)
    if qty > remaining:
        session.rollback()
        raise Conflict(f"cannot release {qty}: receipt {receipt_event_id} has only "
                       f"{remaining} still in quarantine")
    ev = LedgerEvent(part_id=part_id, po_line_id=line, receipt_event_id=receipt_event_id,
                     event_type="release_from_quarantine", qty=qty)
    session.add(ev)
    session.commit()
    return {"event_id": ev.id, "part_id": part_id, "released": qty,
            "quarantine_remaining_for_receipt": remaining - qty}
