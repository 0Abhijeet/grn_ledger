from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import LedgerEvent
from app.services import ledger as svc
from app.services.po import Conflict, NotFound

router = APIRouter(prefix="/ledger", tags=["ledger"])


class BalanceOut(BaseModel):
    part_id: str
    available: float = Field(description="On-hand available. Excludes quarantined and rejected.")
    quarantined: float
    physical: float = Field(description="available + quarantined")
    rejected: float = Field(description="Cumulative rejected, informational")


class EventOut(BaseModel):
    id: int
    part_id: str
    po_line_id: int | None
    receipt_event_id: int | None
    event_type: str
    qty: float


class ReplayOut(BaseModel):
    part_id: str
    replayed: dict[str, float]
    derived: dict[str, float] | None
    match: bool
    ever_negative: bool


class IssueIn(BaseModel):
    part_id: str
    qty: Decimal = Field(gt=0)


class ReleaseIn(BaseModel):
    receipt_id: int
    qty: Decimal = Field(gt=0)


@router.get("/events", response_model=list[EventOut], summary="Raw ledger events, oldest first")
def events(part_id: str | None = None, db: Session = Depends(get_db)):
    q = select(LedgerEvent).order_by(LedgerEvent.id)
    if part_id:
        q = q.where(LedgerEvent.part_id == part_id)
    return db.execute(q).scalars().all()


@router.get("/balances", response_model=list[BalanceOut], summary="Derived balances for all parts")
def all_balances(db: Session = Depends(get_db)):
    return svc.balances(db)


@router.get("/balances/{part_id}", response_model=BalanceOut, summary="Derived balance for one part")
def one_balance(part_id: str, db: Session = Depends(get_db)):
    try:
        return svc.balances(db, part_id)[0]
    except NotFound as e:
        raise HTTPException(404, str(e))


@router.get("/replay", response_model=list[ReplayOut],
            summary="Rebuild balances from events and compare to the derived balance")
def replay(part_id: str | None = None, db: Session = Depends(get_db)):
    return svc.compare(db, part_id)


@router.post("/issue", status_code=201, summary="Issue stock out (blocked if it would go negative)")
def issue(body: IssueIn, db: Session = Depends(get_db)):
    try:
        return svc.issue(db, body.part_id, body.qty)
    except NotFound as e:
        raise HTTPException(404, str(e))
    except Conflict as e:
        raise HTTPException(409, str(e))


@router.post("/release", status_code=201, summary="Release quarantined units from one receipt")
def release(body: ReleaseIn, db: Session = Depends(get_db)):
    try:
        return svc.release_from_quarantine(db, body.receipt_id, body.qty)
    except NotFound as e:
        raise HTTPException(404, str(e))
    except Conflict as e:
        raise HTTPException(409, str(e))
