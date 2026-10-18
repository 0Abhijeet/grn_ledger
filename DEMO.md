# Demo script (Swagger calls, in order)

Synthetic data. A guess at the workflow, built to be corrected.

Open http://localhost:8000/docs. Start from a clean seed: `python scripts/seed.py`.
Expected values assume the default 5% over-delivery tolerance and 100,000 INR approval threshold.

## 0. Orientation (optional)
- `GET /purchase-orders` : 5 POs. PO-1004 shows `pending_approval`, total 172,500 INR.
- `GET /purchase-orders/lines` : delivery flags per line.

## 1. Approve the over-threshold PO
`POST /purchase-orders/PO-1004/approve`
- Expect: `lines_approved: 2`, `status: open`.
- Try it again: 409 "not pending approval".

## 2. Clean full receipt, ledger updates
`POST /receipts`
```json
{"idempotency_key": "demo-2", "po_number": "PO-1004", "line_no": 2,
 "received_qty": 30, "rejected_qty": 0, "lot_batch": "SEA2-LOT-A", "coc_received": true}
```
- Expect: accepted 30, quarantined 0, `po_line_status: received`, events `receipt 30`.
- `GET /ledger/balances/SEA-002` : available 30.

## 3. Partial receipt shows outstanding qty and partially_received
`POST /receipts`
```json
{"idempotency_key": "demo-3", "po_number": "PO-1004", "line_no": 1,
 "received_qty": 25, "rejected_qty": 0, "lot_batch": "SEA1-LOT-A", "coc_received": true}
```
- Expect: `po_line_status: partially_received`, `outstanding_qty: 35`.

## 4. 20% rejected and missing CoC
`POST /receipts`
```json
{"idempotency_key": "demo-4", "po_number": "PO-1001", "line_no": 1,
 "received_qty": 500, "rejected_qty": 100, "lot_batch": "FAS1-LOT-A", "coc_received": false}
```
- Expect: accepted 0, quarantined 400, rejected 100, flags `rejected, missing_coc`.
- Events: `receipt 500`, `quarantine 400`, `reject 100`.
- `GET /ledger/balances/FAS-001` : available 0, quarantined 400, physical 400, rejected 100.
- Optional: send the same body again. Expect 409 "duplicate receipt".

## 5. Replay equals balance
`GET /ledger/replay`
- Expect: every row `match: true`, `ever_negative: false`.

## 6. Invoice for full qty against a partial receipt
`POST /invoices`
```json
{"invoice_number": "INV-DEMO-1", "invoice_date": "2026-10-16",
 "lines": [{"po_number": "PO-1004", "line_no": 1, "invoice_qty": 60,
            "invoice_unit_price_inr": 2400}]}
```
- Expect: `overall_match: false`, reasons `["qty_exceeds_accepted"]`, `accepted_qty: 25`.
- The date is not checked by the app; use today's date.

## 7. Chase draft on an overdue line, nothing sent
`GET /chase-drafts/eligible` : PO-1001 line 1 appears (overdue).

`POST /chase-drafts`
```json
{"po_number": "PO-1001", "line_no": 1, "use_llm": false}
```
- Expect: a draft with the PO number, outstanding qty 100, need-by date, and
  "no promised date on record". `sent: false`, `status: draft_not_sent`.
- The draft is only in the response and in `drafts_log/chase_drafts.jsonl`.
- With `use_llm: true` and `GROQ_API_KEY` + `GROQ_MODEL` set, the tone is polished by the
  model; the draft is validated in code and falls back to the template if it breaks the rules.

## 8. CSV import, then export the exceptions
The layout is a GUESS at an Excel sheet, not a copy of any real one. Files are in `samples/`
(synthetic, expected results in `samples/README.md`). The receipts sample targets the POs created by
the PO-lines sample, so import that first. In Swagger paste the file contents into the body box; with
curl use `--data-binary "@file"` and `-H "Content-Type: text/csv"`.

a. `POST /import/po-lines` with `samples/synthetic_po_lines.csv`
   - Expect 201, 6 rows imported. PO-9003 (172,500 INR) is `pending_approval`; PO-9001 and PO-9002 are `open`.

b. `POST /import/receipts` with `samples/synthetic_receipts.csv` (default mode, all-or-nothing)
   - Expect 422 `rejected`: row 7 is a duplicate key, row 8 has coc value 'maybe'.
     `rows_imported: 0`, and nothing is recorded (check `GET /ledger/events`).

c. `POST /import/receipts?mode=skip_bad_rows` with the same file
   - Expect 201 `partial`: 4 imported, 2 rejected. One partial, one rejection (8 of 40), one missing CoC
     (300 quarantined).

d. `GET /export/exceptions.csv`
   - Expect rows for PO-9002 line 1 (`rejected`) and PO-9002 line 2 (`missing_coc`, 300 still in
     quarantine). If you ran steps 1 to 7 first, the two PO-1001 line 1 rows from step 4 appear too.

e. Optional: repeat (c). Expect 422 with every row reported as a duplicate. Nothing is applied twice.

## Bonus: the "promised date moved" case
`PATCH /purchase-orders/PO-1003/lines/1/promised-date` with `{"promised_date": "2026-12-31"}`,
then `POST /chase-drafts` for PO-1003 line 1 and compare the dates in the draft.
