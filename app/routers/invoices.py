from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import invoices as svc
from app.services.po import Conflict, NotFound

router = APIRouter(prefix="/invoices", tags=["invoices"])


class InvoiceLineIn(BaseModel):
    po_number: str
    line_no: int
    invoice_qty: Decimal = Field(gt=0)
    invoice_unit_price_inr: Decimal = Field(ge=0)


class InvoiceIn(BaseModel):
    invoice_number: str = Field(min_length=1)
    invoice_date: date
    lines: list[InvoiceLineIn] = Field(min_length=1)


class MatchLineOut(BaseModel):
    invoice_number: str
    invoice_date: date
    po_number: str
    line_no: int
    part_id: str
    invoice_qty: float
    invoice_unit_price_inr: float
    invoice_value_inr: float
    po_ordered_qty: float
    po_unit_price_inr: float
    accepted_qty: float
    cumulative_invoiced_qty: float
    match: bool
    reasons: list[str]


class InvoiceOut(BaseModel):
    invoice_number: str
    invoice_date: date
    overall_match: bool
    lines: list[MatchLineOut]


@router.post("", response_model=InvoiceOut, status_code=201,
             summary="Enter an invoice; returns the per-line 3-way match")
def create_invoice(body: InvoiceIn, db: Session = Depends(get_db)):
    try:
        return svc.create_invoice(db, body.invoice_number, body.invoice_date,
                                  [ln.model_dump() for ln in body.lines])
    except NotFound as e:
        raise HTTPException(404, str(e))
    except Conflict as e:
        raise HTTPException(409, str(e))


@router.get("", response_model=list[InvoiceOut], summary="All invoices with current match results")
def list_invoices(db: Session = Depends(get_db)):
    return svc.group(svc.evaluate(db))


@router.get("/{invoice_number}", response_model=InvoiceOut,
            summary="One invoice, matched against current receipts")
def get_invoice(invoice_number: str, db: Session = Depends(get_db)):
    g = svc.group(svc.evaluate(db, invoice_number))
    if not g:
        raise HTTPException(404, f"invoice {invoice_number} not found")
    return g[0]
