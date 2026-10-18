# GRN Ledger

**Synthetic data. A guess at the workflow, built to be corrected.**

A prototype of PO -> delivery tracking -> goods receipt (GRN) with exceptions -> stock ledger -> invoice
3-way match, plus a chase-draft agent. Meant to be shown to a Purchase head as "my guess, tell me where
it is wrong". It is not a finished product and does not reflect any real company's process.

## What it does

1. Seeds fictional PO lines (8 parts, 5 suppliers, 11 lines). A PO whose total is above the approval threshold stays `pending_approval` until `POST approve`.
2. Tracks delivery per line: need-by vs promised date, with derived flags `overdue`, `at_risk`, `no_promise_date`.
3. Records goods receipts with exceptions (partial, over-delivery, reject, missing CoC, duplicate attempt) and appends stock-ledger events. On-hand is always derived from events; a replay rebuilds it independently.
4. Enters invoices and matches each line against PO qty/price and GRN accepted qty, returning a match flag and reason codes.
5. Drafts supplier follow-ups for overdue or at-risk lines from a deterministic template, optionally tone-polished by an LLM and validated in code. Drafts only.

## What it doesn't do

- **Synthetic data only.** Fictional suppliers and parts. It does not reflect any real process.
- **No PR, RFQ, quotes, comparison, negotiation or supplier selection.** POs are seeded from a fixture standing in for an "approved selection". No payments.
- **Threshold and currency are assumptions.** Approval threshold is 100,000 INR (config `APPROVAL_THRESHOLD_INR`). Currency is INR only. No GST or tax logic.
- **Sends nothing.** Chase messages are drafts, stored only in the API response and `drafts_log/chase_drafts.jsonl`. No email, WhatsApp or any other sending code exists.
- **No auth, no UI beyond Swagger, no ERP integration.**
- **Tested only on scenarios I wrote**, so a passing test says nothing about real receiving.
- **Quarantine, reject and CoC handling are guesses**, as are the over-delivery tolerance (5%) and exact-match invoice pricing.

## CSV import and export

**The CSV layout is my guess at what a plain Excel sheet might look like. I have not seen the real
sheet, so never read it as a match for anyone's.**

- Import: `POST /import/po-lines` and `POST /import/receipts`. Send the CSV as the raw request body
  (`Content-Type: text/csv`); in Swagger, paste it into the body box. Default mode is all-or-nothing:
  if any row is bad, nothing is recorded and you get a per-row error report
  (`row_number`, `field`, `problem`, plus `rows_read`, `rows_imported`, `rows_rejected`).
  `?mode=skip_bad_rows` imports the valid rows and reports the rest.
- **Same rules as the API, no bypass.** PO import applies the same approval-threshold rule. Receipt
  import calls the same receipt service as `POST /receipts`, so partial, over-delivery, reject,
  quarantine, missing CoC and idempotency behave identically, and nothing writes to the ledger directly.
  A test shows the same inputs give identical receipts and ledger events either way.
- Strict on purpose: ISO dates (`YYYY-MM-DD`) only, plain numbers, PO import creates new POs only,
  a PO with any bad line is skipped whole in skip mode.
- Export: `GET /export/ledger-events.csv`, `/export/balances.csv`, `/export/exceptions.csv`. Read-only.
  Text cells that start with `=`, `+`, `-` or `@` get a leading `'` so Excel cannot run them as formulas.
- Samples are in [samples/](samples/) (labelled synthetic), with expected results written before the
  first run in [samples/README.md](samples/README.md).
- Not supported: `.xlsx` (save as CSV, UTF-8), very large files (untested, one transaction per file),
  any column layout other than the one documented.

## Assumptions to correct (the questions for the Purchase head)

| # | Assumption | Where |
|---|---|---|
| 1 | PO above 100,000 INR needs approval; equal is fine | D2 |
| 2 | Over-delivery is accepted up to 5% above ordered qty; beyond that goes to quarantine and is flagged | D22 |
| 3 | Missing CoC quarantines the whole receipt's otherwise-good units | D24 |
| 4 | Rejected units stay outstanding on the PO (need replacing) | D25 |
| 5 | Only accepted (or released) units can be invoiced; quarantined cannot | D32 |
| 6 | Invoice price must equal PO price exactly | D31 |
| 7 | A late line is one past need-by, or past its own promised date | D14 |

All decisions are one-liners in [DECISIONS.md](DECISIONS.md). Build log and bugs are in
[project_details.md](project_details.md).

## Test results

Run on the Neon-hosted Postgres, full suite: **177 passed, 0 failed.**
Each scenario below was written with its expected outcome before it was first run. Invariants are
checked after every scenario.

| # | Scenario | Result |
|---|---|---|
| S01 | Clean full receipt | pass |
| S02 | Partial receipt | pass |
| S03 | Over-delivery (excess quarantined, flagged) | pass |
| S04 | Partial plus reject | pass |
| S05 | Missing CoC (auto-quarantine) | pass |
| S06 | Duplicate receipt attempt (rejected, ledger unchanged) | pass |
| S07 | Invoice price differs from PO | pass |
| S08 | Invoice qty exceeds accepted qty | pass |
| S09 | Invoice before any receipt | pass |
| S10 | Late, promised date moved | pass |
| S11 | Late, no promised date | pass |
| S12 | Release from quarantine | pass |
| INV | Invariants after each scenario and after a seeded 80-operation random run: replay equals derived balance, on-hand never negative, receipt buckets add up, accepted never above tolerance cap | pass |

Suite breakdown: P1 data/seed 8, P2 PO and flags 14, P3 receipts and ledger 21, P4 invoices 16,
P5 chase agent 28, P6 scenarios 13, CSV import of PO lines 24, CSV import of receipts 31, CSV exports 13,
sample files 9.

**What this does not cover (honest gaps):**
- The row lock that serialises concurrent ledger writes is in the code but has no concurrency test.
- The automated tests use a fake LLM client. The real Groq model (`openai/gpt-oss-20b`) was exercised by hand in one session: 12 live drafts on 4 lines, all valid on the first attempt, none fell back to the template. That is a small sample, it is not part of the test suite, and it did not include a line with receipt history. The live run found two real problems the fake could not (look-alike Unicode hyphens, and empty replies from a reasoning model); both are fixed and have regression tests.
- The LLM adds little: its drafts are nearly identical to the template.
- A mutation check was done once: breaking the replay sign for `reject` made the invariant checks fail exactly where expected, then was reverted.
- A passing run only shows the code matches my own scenarios.

## How to run

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy .env.example .env          # then edit .env
.venv\Scripts\python scripts\migrate.py
.venv\Scripts\python scripts\seed.py
.venv\Scripts\uvicorn app.main:app --reload
```

Open http://localhost:8000/docs (Swagger is the only UI). Follow [DEMO.md](DEMO.md) for the 7-step demo.

Configuration (`.env`):

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | PostgreSQL. `postgresql://` URLs (e.g. Neon) are accepted and routed to psycopg 3. |
| `APPROVAL_THRESHOLD_INR` | Assumption. Default 100000. |
| `OVER_DELIVERY_TOLERANCE_PCT` | Assumption. Default 5. |
| `GROQ_API_KEY`, `GROQ_MODEL` | Optional. Without both, drafts use the template only. |
| `CHASE_LOG_PATH` | Optional. Default `drafts_log/chase_drafts.jsonl`. |

Tests:

```powershell
.venv\Scripts\python -m pytest tests -v
```

**Warning:** `seed.py` and the tests wipe and reseed the database they point at. Use a throwaway database.
Tests run against the same `DATABASE_URL`, so a full run takes about 2 minutes on a hosted DB.

## Layout

- `migrations/001_init.sql`: the 5 tables (parts, po_lines, receipt_events, ledger_events, invoices).
- `app/services/`: rules and DB logic (`po`, `receipts`, `ledger`, `invoices`, `chase`).
- `app/routers/`: Swagger-tagged endpoints (purchase-orders, receipts, ledger, invoices, chase-drafts).
- `tests/`: one file per phase; `test_p6_scenarios.py` is the scenario suite.
