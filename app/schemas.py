"""Response shapes. Money and quantities are floats here for readable Swagger output only;
all arithmetic is done in Decimal inside the services."""
from datetime import date

from pydantic import BaseModel


class POLineOut(BaseModel):
    po_number: str
    line_no: int
    part_id: str
    supplier: str
    ordered_qty: float
    unit_price_inr: float
    line_value_inr: float
    need_by_date: date
    promised_date: date | None
    status: str
    accepted_qty: float
    quarantined_qty: float
    outstanding_qty: float
    overdue: bool
    at_risk: bool
    no_promise_date: bool


class POSummaryOut(BaseModel):
    po_number: str
    po_total_inr: float
    line_count: int
    status: str
    suppliers: list[str]


class PODetailOut(POSummaryOut):
    approval_threshold_inr: float
    lines: list[POLineOut]


class ApproveOut(BaseModel):
    po_number: str
    lines_approved: int
    status: str
