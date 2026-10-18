# Project details: GRN Ledger

Synthetic data. A guess at the workflow, built to be corrected.

Running log of what was built, why, bugs hit, and decisions. Updated at the end of every phase.
Short decision lines also live in DECISIONS.md.

## Scope in one paragraph
PO -> delivery tracking -> goods receipt (GRN) with exceptions -> stock ledger -> invoice 3-way match,
plus a chase-draft agent that only drafts. FastAPI + Swagger, PostgreSQL, pytest. Max 5 tables.
INR only. Nothing is ever sent.

## Environment
- Windows 11, Python 3.12.6 (spec says 3.11+), venv at `.venv`.
- Database is Neon (hosted Postgres), not local. Connection string in `.env` (git-ignored). Data is synthetic only, so hosting it externally is fine. Never commit `.env`.
- Neon's pooled endpoint (pgbouncer) is in use. Fine for this prototype; keep transactions short.

## Phase log

### P1: data model, migrations, seed (status: DONE, 8/8 tests pass on Neon)
What was built
- `migrations/001_init.sql`: 5 tables (parts, po_lines, receipt_events, ledger_events, invoices).
- `app/models.py`: SQLAlchemy 2.x models mirroring the SQL.
- `app/fixtures.py`: 8 parts, 5 fictional suppliers, 11 PO lines across 5 POs. Only PO-1004 (172,500 INR) is above the 100,000 threshold.
- `app/seed.py`, `scripts/migrate.py`, `scripts/seed.py`.
- `tests/test_p1_seed.py`: 4 no-DB tests, 4 DB tests.

Key concepts
- Derived vs stored: only `po_lines.status` is stored state. On-hand, flags, match results are computed.
- Append-only ledger enforced in the database by a trigger.
- CHECK constraints push invariants into the database so bad data cannot get in even if code is wrong.

Test results
- 8 passed (4 no-DB, 4 DB) against Neon. Migrate created exactly 5 tables, seed loaded 8 parts and 11 lines.

### P2: PO endpoints, approval rule, delivery flags (status: DONE, 22/22 tests pass)
Built
- `app/main.py` (FastAPI + 5 Swagger tags), `app/routers/purchase_orders.py`, `app/services/po.py`, `app/schemas.py`.
- Endpoints: GET /purchase-orders, GET /purchase-orders/lines?flag=&as_of=, GET /purchase-orders/{po}, POST /purchase-orders/{po}/approve.
- Pure function `delivery_flags` holds the rules, so they are unit-tested without a database.

Key concepts
- Derived on read: flags and outstanding qty are computed from dates and receipts, never stored.
- Separate pure rules from IO so rules are testable in milliseconds.
- Clock injection (`as_of`) so tests do not depend on today's date.

Tests (expected values written by hand before running): 8 rule tests, 6 API tests, all passed first run.

Run it: `.venv\Scripts\uvicorn app.main:app --reload` then open http://localhost:8000/docs

### P3: GRN exceptions, ledger, derived on-hand, replay (status: DONE, 43/43 tests pass)
Built
- `app/services/receipts.py`: `classify_receipt` (pure rules) and `record_receipt` (lock, idempotency, insert, status).
- `app/services/ledger.py`: SQL-derived `balances`, Python-fold `replay`, `compare`, `issue`, `release_from_quarantine`.
- Endpoints: POST/GET /receipts; GET /ledger/events, /balances, /balances/{part}, /replay; POST /ledger/issue, /ledger/release.

Key concepts
- Events are facts; balances are folds over facts. Two independent computations (SQL aggregate vs Python replay) must agree.
- Pure classification function holds the exception policy; IO wrapper stays thin.
- Idempotency key with a UNIQUE constraint: duplicates are blocked by the database even if the code check loses a race.
- Row lock (SELECT FOR UPDATE) serialises writers per part.

Failure cases handled: duplicate key, unknown line, pending/closed line, rejected > received, issue beyond available, release beyond quarantined.

Known gaps
- Concurrency lock is untested.
- Replay and SQL balance share the same sign semantics (written twice by hand); a wrong semantic would be wrong in both. Only scenario review by the Purchase head can catch that.

Tests (expected values written by hand first): 10 pure-rule tests, 11 API/ledger tests, all passed first run.

### P4: invoices and 3-way match (status: DONE, 59/59 tests pass)
Built
- `app/services/invoices.py` (`match_line` pure rule, `evaluate`, `create_invoice`), `app/routers/invoices.py`.
- Endpoints: POST /invoices (returns per-line match), GET /invoices, GET /invoices/{number}.

Key concepts
- Three-way match compares three independent records: what was ordered, what was accepted, what was billed.
- Cumulative comparison per line prevents double billing.
- Derived-on-read: results change when the underlying facts change.

Tests: 4 pure-rule, 12 API. 1 failed first run (see B4), then all passed.

### P5: chase-draft agent + rule validator (status: DONE, 26 P5 tests pass)
Built
- `app/services/chase.py` (template, `validate_draft`, LLM polish, fallback, JSONL log), `app/routers/chase_drafts.py`.
- Endpoints: GET /chase-drafts/eligible, POST /chase-drafts, GET /chase-drafts.

Key concepts
- Treat the LLM as untrusted: deterministic facts in, validated text out, safe fallback if it misbehaves.
- Dependency injection: the LLM client is a parameter, so tests use a fake and never touch the network.
- "Cannot send" is enforced structurally (no send code exists) and checked by a test, not just promised.

LIVE LLM NOT EXERCISED: ANTHROPIC_API_KEY and ANTHROPIC_MODEL are empty in .env. The LLM path is tested only with fakes. Real model behaviour (tone, how often it breaks the rules) is unknown until a key is set.

### P6: scenario suite + invariants (status: DONE, 13 P6 tests, 98 total pass)
Built
- `tests/test_p6_scenarios.py`: 12 named scenarios (S01 to S12), expected outcomes written in the file header before the first run, plus a seeded random-operations run.
- Invariants I1 to I5 (replay == derived, never negative, buckets add up, over-receipt cap, physical == available + quarantined) run in a fixture teardown after every scenario.
- Added PATCH promised-date endpoint (D44) so scenario 10 is demonstrable.

Mutation check (to see whether the invariants actually bite)
- Deliberately changed the replay sign for `reject` (available multiplier -1 to 0). S04, S12 and the random run errored on I1, the other 10 stayed green. Reverted; 98/98 green again.

Honest limits
- All scenarios passed on the first run. The mutation check is why that is believable rather than reassuring.
- Scenarios are mine. They cannot reveal where the workflow itself is wrong.
- Concurrency lock still untested. Live LLM still unexercised.

### P7: README + demo script (status: DONE)
- `README.md` with the required top line, 5-line summary, "What it doesn't do", assumptions table, honest test table, run steps.
- `DEMO.md`: 7 Swagger steps with exact JSON. The payloads were executed end to end against the app before being documented; all 7 steps produced the documented results.
- Neon database reseeded to a clean state afterwards.

## Final state
- 98 tests pass (P1 8, P2 14, P3 21, P4 16, P5 26, P6 13).
- Known gaps: concurrency lock untested; live LLM never run (no key/model set); all scenarios are mine.
- Scope additions beyond the original endpoint list: one (PATCH promised-date, D44).
- Bugs: B1 to B3 (P1 driver and placeholder issues), B4 (P4, a wrong test expectation, not a code bug).

### Post-P7 change: LLM provider Anthropic -> Groq (user-directed, D47)
- `app/services/chase.py` now uses the `groq` SDK (`chat.completions.create`), env vars `GROQ_API_KEY` and `GROQ_MODEL`.
- `requirements.txt`, tests, README, DEMO.md, `.env.example` updated. `.env.example` had gone missing (not deleted by me) and was recreated.
- P5 tests pass (26) against a fake Groq-shaped client. Live Groq call still not run until a key and model are in `.env`.

### Post-P7: first live LLM run (Groq, openai/gpt-oss-20b)
- Run 1 (4 lines): 2 of 4 fell back to the template. Safety net worked; the cause was real.
- B5 Look-alike Unicode hyphens: the model rewrote "-" as U+2011 ("PO-1001", "2026-10-07"). Invisible to a human, but a search for the PO number would miss it, and the literal-match validator correctly rejected it. Fix: normalise typography before validating (D48), and ask for ASCII hyphens in the prompt.
- B6 Empty reply: a reasoning model spent the whole 2000-token budget thinking (finish_reason=length) and returned "". The validator reported four misleading "missing X" codes. Fix: reasoning_effort="low", max_tokens 3000, and an explicit `empty_reply` code.
- Run 2 (12 calls, 3 rounds x 4 lines): 12/12 valid LLM drafts on the first attempt, 0 template fallbacks.
- Lesson: the fake client proved the control flow, not the model. Only the live run could reveal B5 and B6.
- Limits: 12 calls, no receipt-history case, not in the automated suite, output nearly identical to the template.
- Tests: 100 pass (P5 now 28, +2 regression tests for B5 and B6).

## CSV import and export feature (second build, rules unchanged)
Why: a Purchase head said their stores tracking is an Excel sheet. The CSV layout is my GUESS at such a sheet; the real one has not been seen.

### CSV P1: import PO lines (DONE, 24 new tests)
- `app/services/imports.py` (CSV reading, field parsers, `import_po_lines`), `app/routers/imports.py` (POST /import/po-lines?mode=all_or_nothing|skip_bad_rows). Swagger tags `import` and `export` added.
- Reuses `seed.initial_status` so the approval rule is not duplicated. No new tables, no new dependencies (csv module, raw text/csv body).
- Decisions D50 to D63. Tests: clean file, threshold boundary, bad row leaves DB unchanged, skip mode, duplicates in file and in DB, per-field errors, file-level errors, BOM and CRLF, imported pending PO blocks receipts until approved.
- Heads-up for CSV P2: `record_receipt` commits internally; all-or-nothing over a file needs an optional `commit` parameter (default keeps current behaviour).

### CSV P2: import receipts (DONE, 31 new tests)
- `import_receipts` in `app/services/imports.py`, POST /import/receipts?mode=. Uses `record_receipt(commit=False)` inside a SAVEPOINT per row, one transaction per file.
- Proof of "same rules, no bypass": import and API produce identical receipts and ledger events for the same inputs; imports.py has no LedgerEvent reference; replay equals balances after an import.
- Mutation check: changed the all-or-nothing rollback to a commit. Exactly 3 tests failed (apply-time failure on last row, mixed errors, duplicate key in file). Reverted.
- Decisions D64 to D71.
- Known limits: a very large file is processed in one long transaction holding part row locks; order of rows matters in skip mode.

### CSV P3: exports + round trip (DONE, 13 new tests)
- `app/services/exports.py`, `app/routers/exports.py`: GET /export/ledger-events.csv, /balances.csv, /exceptions.csv. Read-only.
- Round trip: import 4 receipts, export, and every quantity matches; events re-folded with an independent sign table equal balances.csv.
- Security: CSV/formula injection guard (D73). Mutation check: disabling it fails exactly the injection test.
- Decisions D72 to D78. One addition beyond the ask: quarantine_remaining (D77).

### CSV P4: samples, README, DEMO, full run (DONE, 9 new tests, 177 total)
- `samples/synthetic_po_lines.csv` (6 lines) and `samples/synthetic_receipts.csv` (6 rows: clean, partial, rejection, missing CoC, duplicate key, bad coc), both labelled synthetic. `samples/README.md` holds the expected results, written before the first run.
- First run matched every expectation (no divergence), via `tests/test_i4_samples.py`.
- README gets a "CSV import and export" section (layout is a guess, same rules as the API). DEMO.md gets step 8 and the step 6 invoice date fix.
- DEMO.md steps 1 to 8 were executed with the exact payloads and the real sample files on a fresh seed: all passed.
- Decisions D79 to D83.

### CSV feature summary
- New endpoints: POST /import/po-lines, POST /import/receipts, GET /export/ledger-events.csv, /balances.csv, /exceptions.csv.
- No new tables, no new dependencies. Existing code changed in one place only: `record_receipt(commit=True)` plus a `DuplicateReceipt` subclass (D65, D67).
- Tests: 100 before, 177 after (+24 PO import, +31 receipt import, +13 exports, +9 samples).
- Mutation checks done for: whole-file rollback (3 tests caught it) and export formula guard (1 test caught it).
- Limits: layout is a guess; .xlsx unsupported; large files untested; skip mode is order-dependent; concurrency lock still untested.

## Bugs log
- B5, B6: see "Post-P7: first live LLM run" above.
- (no P5 or P6 bugs)
- B4 (P4, test bug, not code bug) My hand-written expectation in test_2 missed that cumulative invoiced qty (500 + 300 = 800) also exceeds the PO qty, so `qty_exceeds_po` fires too. Code was right, prediction was wrong. Lesson: when predicting, apply every rule, not just the one you are thinking about.
- (no P2 or P3 bugs)
- B1 (P1) Wrong driver. Neon gives `postgresql://...`, SQLAlchemy defaults that to psycopg2, which is not installed. Fix: `app/config.py` rewrites the scheme to `postgresql+psycopg://`. Lesson: the URL scheme picks the driver.
- B2 (P1) Migration failed with "incomplete placeholder '%'". Root cause: `%` inside the trigger's RAISE message. psycopg 3 treats `%` as a parameter marker whenever SQLAlchemy sends a statement. Fix: build the message with `||` instead.
- B3 (P1) My fix for B2 added a comment containing a literal percent sign, which failed the same way. psycopg scans comments too. Fix: reworded the comment. Lesson: re-run after every fix, never assume.

## Open assumptions to confirm with the Purchase head
- Approval threshold 100,000 INR.
- Over-delivery tolerance 5% (P3).
- Quarantine / reject / CoC handling.
