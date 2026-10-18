from typing import Literal

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import imports as svc

router = APIRouter(prefix="/import", tags=["import"])

CSV_BODY = {"requestBody": {"required": True, "content": {"text/csv": {"schema": {"type": "string"}}}}}

EXAMPLE = ("Body is raw CSV text (Content-Type: text/csv). In Swagger, paste the CSV into the body box; "
           "with curl use --data-binary @file.csv. Layout is a GUESS at an Excel sheet, not a copy of "
           "any real one.")


class RowError(BaseModel):
    row_number: int
    field: str
    problem: str


class ImportedPO(BaseModel):
    po_number: str
    lines: int
    po_total_inr: float
    status: str


class ImportReport(BaseModel):
    mode: str
    status: str
    rows_read: int
    rows_imported: int
    rows_rejected: int
    valid_rows_not_applied: int
    ignored_columns: list[str]
    errors: list[RowError]
    imported_pos: list[ImportedPO]


class ImportedReceipt(BaseModel):
    row_number: int
    receipt_id: int
    po_number: str
    line_no: int
    part_id: str
    accepted_qty: float
    rejected_qty: float
    quarantined_qty: float
    exception_flags: list[str]
    po_line_status: str | None
    outstanding_qty: float


class ReceiptImportReport(BaseModel):
    mode: str
    status: str
    rows_read: int
    rows_imported: int
    rows_rejected: int
    valid_rows_not_applied: int
    ignored_columns: list[str]
    errors: list[RowError]
    imported_receipts: list[ImportedReceipt]


async def read_text(request: Request) -> tuple[str | None, dict | None]:
    raw = await request.body()
    try:
        return raw.decode("utf-8-sig"), None
    except UnicodeDecodeError:
        return None, {"row_number": 0, "field": "(file)", "problem": "file is not valid UTF-8"}


@router.post("/po-lines", response_model=ImportReport, status_code=201, openapi_extra=CSV_BODY,
             summary="Import PO lines from CSV (all-or-nothing by default)", description=EXAMPLE)
async def import_po_lines(request: Request, response: Response,
                          mode: Literal["all_or_nothing", "skip_bad_rows"] = Query("all_or_nothing"),
                          db: Session = Depends(get_db)):
    text, err = await read_text(request)
    if err:
        response.status_code = 422
        return {"mode": mode, "status": "rejected", "rows_read": 0, "rows_imported": 0,
                "rows_rejected": 0, "valid_rows_not_applied": 0, "ignored_columns": [],
                "errors": [err], "imported_pos": []}
    report, code = svc.import_po_lines(db, text, mode)
    response.status_code = code
    return report


RECEIPT_DESC = (EXAMPLE + " Columns: idempotency_key, po_number, line_no, received_qty, rejected_qty "
                "(blank = 0), lot_batch, coc_received (yes/no/true/false/1/0). Every row goes through "
                "the same receipt service as POST /receipts; nothing writes to the ledger directly.")


@router.post("/receipts", response_model=ReceiptImportReport, status_code=201, openapi_extra=CSV_BODY,
             summary="Import goods receipts from CSV (same rules as POST /receipts)",
             description=RECEIPT_DESC)
async def import_receipts(request: Request, response: Response,
                          mode: Literal["all_or_nothing", "skip_bad_rows"] = Query("all_or_nothing"),
                          db: Session = Depends(get_db)):
    text, err = await read_text(request)
    if err:
        response.status_code = 422
        return {"mode": mode, "status": "rejected", "rows_read": 0, "rows_imported": 0,
                "rows_rejected": 0, "valid_rows_not_applied": 0, "ignored_columns": [],
                "errors": [err], "imported_receipts": []}
    report, code = svc.import_receipts(db, text, mode)
    response.status_code = code
    return report
