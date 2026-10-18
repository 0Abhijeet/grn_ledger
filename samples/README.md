# Sample import files

**SYNTHETIC. Invented data, not from any real company or spreadsheet.**
The column layout is my guess at what a plain Excel sheet might look like. I have not seen a real one,
so do not read these files as a copy of anyone's process.

| File | Rows | Purpose |
|---|---|---|
| `synthetic_po_lines.csv` | 6 PO lines, 3 POs | Creates PO-9001, PO-9002, PO-9003. All valid. PO-9003 is over the 100,000 INR threshold. |
| `synthetic_receipts.csv` | 6 receipt rows | Targets the POs above. Contains a partial, a rejection, a missing CoC, a duplicate key and a bad row on purpose. |

Dates are fixed (December 2026), so they go stale over time. They only matter for delivery flags.

## How to run (fresh seed, API on localhost:8000)

```powershell
.venv\Scripts\python scripts\seed.py
curl.exe -X POST --data-binary "@samples/synthetic_po_lines.csv" -H "Content-Type: text/csv" "http://localhost:8000/import/po-lines"
curl.exe -X POST --data-binary "@samples/synthetic_receipts.csv" -H "Content-Type: text/csv" "http://localhost:8000/import/receipts"
curl.exe -X POST --data-binary "@samples/synthetic_receipts.csv" -H "Content-Type: text/csv" "http://localhost:8000/import/receipts?mode=skip_bad_rows"
curl.exe "http://localhost:8000/export/exceptions.csv"
```

In Swagger, paste the file contents into the request body box of the same endpoints.

---

## Expected results (written BEFORE the first run)

Row numbers are physical line numbers in the file: line 1 is the comment, line 2 is the header,
data starts at line 3.

### 1. `synthetic_po_lines.csv` into a fresh seed (default mode)

- HTTP 201, `status: imported`, `rows_read 6`, `rows_imported 6`, `rows_rejected 0`, no errors.
- `imported_pos`:
  - PO-9001: 2 lines, total 4,100 INR, `open` (2,500 + 1,600)
  - PO-9002: 2 lines, total 87,500 INR, `open` (74,000 + 13,500)
  - PO-9003: 2 lines, total 172,500 INR, `pending_approval` (144,000 + 28,500)
- Importing the same file a second time: HTTP 422, `rows_rejected 6`, every row says the PO already exists,
  nothing changes.

### 2. `synthetic_receipts.csv`, default mode (all-or-nothing)

- HTTP 422, `status: rejected`, `rows_read 6`, `rows_imported 0`, `rows_rejected 2`,
  `valid_rows_not_applied 4`.
- Errors:
  - row 7, `idempotency_key`: duplicate of row 3 (same key `s-001` twice)
  - row 8, `coc_received`: not a recognised yes/no value: 'maybe'
- Database unchanged: 0 receipts, 0 ledger events. (The 4 good rows were applied inside the transaction,
  then rolled back.)

### 3. `synthetic_receipts.csv`, `?mode=skip_bad_rows`

- HTTP 201, `status: partial`, `rows_read 6`, `rows_imported 4`, `rows_rejected 2`. Same two errors as above.
- Imported rows:

| Row | PO / line | Part | Outcome | Line status | Outstanding |
|---|---|---|---|---|---|
| 3 | PO-9001 / 1 | FAS-001 | accepted 200, clean | received | 0 |
| 4 | PO-9001 / 2 | FAS-002 | accepted 120 (partial) | partially_received | 80 |
| 5 | PO-9002 / 1 | CON-001 | received 40, rejected 8, accepted 32, flag `rejected` | partially_received | 8 |
| 6 | PO-9002 / 2 | HAR-001 | received 300, no CoC: accepted 0, quarantined 300, flag `missing_coc` | received | 0 |

- 6 ledger events in total: receipt 200 | receipt 120 | receipt 40 + reject 8 | receipt 300 + quarantine 300.
- Balances (available / quarantined / physical / rejected):
  - FAS-001 200 / 0 / 200 / 0
  - FAS-002 120 / 0 / 120 / 0
  - CON-001 32 / 0 / 32 / 8
  - HAR-001 0 / 300 / 300 / 0
- Replay of the ledger equals these balances; nothing is negative.

### 4. `GET /export/exceptions.csv` after step 3 (fresh seed, nothing else imported)

Two rows:

| PO / line | Part | Lot | CoC | received / accepted / rejected / quarantined | quarantine_remaining | flag |
|---|---|---|---|---|---|---|
| PO-9002 / 1 | CON-001 | LOT-S3 | yes | 40 / 32 / 8 / 0 | 0 | rejected |
| PO-9002 / 2 | HAR-001 | LOT-S4 | no | 300 / 0 / 0 / 300 | 300 | missing_coc |

If you ran DEMO.md steps 1 to 7 first, the two rows for PO-1001 line 1 from demo step 4 also appear.

### 5. Re-running step 3 (idempotency)

- HTTP 422, `rows_imported 0`, `rows_rejected 6`.
- Rows 3 to 6 are reported as duplicate receipts (keys already in the database), row 7 as a duplicate
  of row 3, row 8 as the bad CoC value. Nothing is applied twice.

### Things to know

- Receipt and event ids are not predictable: a rolled-back attempt (step 2) still consumes database ids,
  so the successful run in step 3 does not start at 1. The expected results above therefore refer to
  PO, line and lot, never to ids.
- The PO sample can only be imported once per database, because import creates NEW POs only.
