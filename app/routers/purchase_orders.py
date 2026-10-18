from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.config import APPROVAL_THRESHOLD_INR
from app.db import get_db
from app.schemas import ApproveOut, PODetailOut, POLineOut, POSummaryOut
from app.services import po as svc

router = APIRouter(prefix="/purchase-orders", tags=["purchase-orders"])


@router.get("", response_model=list[POSummaryOut], summary="List POs with totals and status")
def list_pos(as_of: date | None = None, db: Session = Depends(get_db)):
    return svc.summarise(svc.load_lines(db, as_of=as_of))


@router.get("/lines", response_model=list[POLineOut],
            summary="All PO lines with delivery flags; filter by flag")
def list_lines(flag: Literal["overdue", "at_risk", "no_promise_date"] | None = Query(None),
               as_of: date | None = Query(None, description="Evaluate flags as of this date; default today"),
               db: Session = Depends(get_db)):
    lines = svc.load_lines(db, as_of=as_of)
    return [ln for ln in lines if ln[flag]] if flag else lines


@router.get("/{po_number}", response_model=PODetailOut, summary="One PO with its lines")
def get_po(po_number: str, as_of: date | None = None, db: Session = Depends(get_db)):
    lines = svc.load_lines(db, po_number, as_of)
    if not lines:
        raise HTTPException(404, f"PO {po_number} not found")
    return {**svc.summarise(lines)[0], "approval_threshold_inr": APPROVAL_THRESHOLD_INR,
            "lines": lines}


class PromisedDateIn(BaseModel):
    promised_date: date | None = Field(description="New supplier promise; null clears it")


@router.patch("/{po_number}/lines/{line_no}/promised-date", response_model=POLineOut,
              summary="Move (or clear) the supplier's promised date on one line")
def move_promised_date(po_number: str, line_no: int, body: PromisedDateIn,
                       db: Session = Depends(get_db)):
    try:
        svc.set_promised_date(db, po_number, line_no, body.promised_date)
    except svc.NotFound as e:
        raise HTTPException(404, str(e))
    except svc.Conflict as e:
        raise HTTPException(409, str(e))
    return [ln for ln in svc.load_lines(db, po_number) if ln["line_no"] == line_no][0]


@router.post("/{po_number}/approve", response_model=ApproveOut,
             summary="Approve a PO that is pending approval")
def approve(po_number: str, db: Session = Depends(get_db)):
    try:
        n = svc.approve_po(db, po_number)
    except svc.NotFound as e:
        raise HTTPException(404, str(e))
    except svc.Conflict as e:
        raise HTTPException(409, str(e))
    return {"po_number": po_number, "lines_approved": n, "status": "open"}
