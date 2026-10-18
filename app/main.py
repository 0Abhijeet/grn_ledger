from fastapi import FastAPI

from app.routers import (chase_drafts, exports, imports, invoices, ledger, purchase_orders,
                         receipts)

DESCRIPTION = (
    "**Synthetic data. A guess at the workflow, built to be corrected.** "
    "Fictional suppliers and parts. Not a real company's process. "
    "Threshold, currency (INR) and exception handling are assumptions. Nothing is ever sent."
)

TAGS = [
    {"name": "purchase-orders", "description": "PO lines, approval rule, delivery tracking flags."},
    {"name": "receipts", "description": "Goods receipt (GRN) with exceptions."},
    {"name": "ledger", "description": "Append-only stock events, derived on-hand, replay."},
    {"name": "invoices", "description": "Invoice entry and per-line 3-way match."},
    {"name": "chase-drafts", "description": "Follow-up drafts only. Never sent."},
    {"name": "import", "description": "CSV import (layout is a guess at an Excel sheet). "
                                       "Same rules as the API; all-or-nothing by default."},
    {"name": "export", "description": "CSV exports of ledger events, balances and exceptions."},
]

app = FastAPI(title="GRN Ledger (synthetic prototype)", description=DESCRIPTION,
              openapi_tags=TAGS)
app.include_router(purchase_orders.router)
app.include_router(receipts.router)
app.include_router(ledger.router)
app.include_router(invoices.router)
app.include_router(chase_drafts.router)
app.include_router(imports.router)
app.include_router(exports.router)
