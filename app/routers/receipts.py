from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import receipts as svc
from app.services.po import Conflict, NotFound

router = APIRouter(prefix="/receipts", tags=["receipts"])


class ReceiptIn(BaseModel):
    idempotency_key: str = Field(min_length=1, description="Same key twice = second is rejected")
    po_number: str
    line_no: int
    received_qty: Decimal = Field(gt=0)
    rejected_qty: Decimal = Field(default=Decimal(0), ge=0, description="Failed inspection")
    lot_batch: str = Field(min_length=1)
    coc_received: bool = Field(description="Certificate of conformance received? false = auto-quarantine")


class LedgerEventOut(BaseModel):
    event_type: str
    qty: float


class ReceiptOut(BaseModel):
    id: int
    idempotency_key: str
    po_number: str
    line_no: int
    part_id: str
    received_qty: float
    accepted_qty: float
    rejected_qty: float
    quarantined_qty: float
    lot_batch: str
    coc_received: bool
    exception_flags: list[str]
    po_line_status: str | None = None
    outstanding_qty: float | None = None
    ledger_events: list[LedgerEventOut] | None = None


@router.post("", response_model=ReceiptOut, status_code=201,
             summary="Record a goods receipt for one PO line")
def create_receipt(body: ReceiptIn, db: Session = Depends(get_db)):
    try:
        return svc.record_receipt(db, **body.model_dump())
    except NotFound as e:
        raise HTTPException(404, str(e))
    except Conflict as e:
        raise HTTPException(409, str(e))
    except svc.Invalid as e:
        raise HTTPException(422, str(e))


@router.get("", response_model=list[ReceiptOut], summary="List receipts, optionally by PO")
def list_receipts(po_number: str | None = None, db: Session = Depends(get_db)):
    return svc.list_receipts(db, po_number)
