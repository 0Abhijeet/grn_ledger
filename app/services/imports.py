"""CSV import. The layout is a GUESS at what a plain Excel sheet might look like; it has not been
compared with any real sheet.

Imports reuse the SAME rules as the API (approval rule via seed.initial_status; later phases call
the same receipt service). They never insert into ledger_events directly.
"""
import csv
import io
import re
from collections import defaultdict
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import config
from app.models import Part, POLine
from app.seed import initial_status

MODES = ("all_or_nothing", "skip_bad_rows")

PO_REQUIRED = ["po_number", "line_no", "part_number", "supplier", "ordered_qty",
               "unit_price_inr", "need_by_date"]
PO_OPTIONAL = ["promised_date"]

_NUM = re.compile(r"^-?\d+(\.\d+)?$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_INT = re.compile(r"^\d+$")


# ---- generic CSV reading -------------------------------------------------
def read_csv(text: str) -> tuple[list[str] | None, list[tuple[int, list[str]]]]:
    """Return (normalised header or None, [(physical_line_number, cells)]).
    Blank lines and lines whose first cell starts with '#' are skipped."""
    reader = csv.reader(io.StringIO(text))
    header, rows = None, []
    for raw in reader:
        if not raw or all(not c.strip() for c in raw):
            continue
        if raw[0].lstrip().startswith("#"):
            continue
        if header is None:
            header = [c.strip().lower() for c in raw]
            continue
        rows.append((reader.line_num, raw))
    return header, rows


def check_header(header: list[str] | None, required: list[str],
                 optional: list[str]) -> tuple[list[dict], list[str]]:
    """File-level problems (row_number 0) and the list of ignored extra columns."""
    if header is None:
        return [{"row_number": 0, "field": "(file)",
                 "problem": "file is empty or has no header row"}], []
    errs = []
    dupes = {h for h in header if header.count(h) > 1}
    for d in sorted(dupes):
        errs.append({"row_number": 0, "field": d, "problem": "column appears more than once"})
    for col in required:
        if col not in header:
            errs.append({"row_number": 0, "field": col, "problem": "missing required column"})
    ignored = [h for h in header if h not in required and h not in optional]
    return errs, ignored


# ---- field parsers (return (value, problem)) -----------------------------
def _decimal(raw: str, *, places: int, max_int_digits: int, positive: bool):
    if raw == "":
        return None, "required"
    if not _NUM.match(raw):
        return None, f"not a plain number: '{raw}' (no currency symbols, commas or text)"
    d = Decimal(raw)
    if positive and d <= 0:
        return None, f"must be greater than 0, got '{raw}'"
    if not positive and d < 0:
        return None, f"must not be negative, got '{raw}'"
    if -d.as_tuple().exponent > places:
        return None, f"too many decimal places (max {places}): '{raw}'"
    if len(str(int(abs(d)))) > max_int_digits:
        return None, f"too large: '{raw}'"
    return d, None


def _iso_date(raw: str, *, required: bool):
    if raw == "":
        return None, ("required" if required else None)
    if not _DATE.match(raw):
        return None, f"not an ISO date (YYYY-MM-DD): '{raw}'"
    try:
        return date.fromisoformat(raw), None
    except ValueError:
        return None, f"not a real calendar date: '{raw}'"


def validate_po_row(cells: dict[str, str], known_parts: set[str]) -> tuple[dict, list[tuple[str, str]]]:
    errs: list[tuple[str, str]] = []
    out: dict = {}

    po = cells.get("po_number", "")
    if po == "":
        errs.append(("po_number", "required"))
    out["po_number"] = po

    ln = cells.get("line_no", "")
    if not _INT.match(ln) or int(ln) < 1 or int(ln) > 2_000_000_000:
        errs.append(("line_no", "required" if ln == "" else
                     f"must be a whole number of 1 or more, got '{ln}'"))
    else:
        out["line_no"] = int(ln)

    part = cells.get("part_number", "")
    if part == "":
        errs.append(("part_number", "required"))
    elif part not in known_parts:
        errs.append(("part_number", f"unknown part '{part}' (not in parts master)"))
    else:
        out["part_id"] = part

    sup = cells.get("supplier", "")
    if sup == "":
        errs.append(("supplier", "required"))
    out["supplier"] = sup

    for field, kw in (("ordered_qty", dict(places=3, max_int_digits=9, positive=True)),
                      ("unit_price_inr", dict(places=2, max_int_digits=10, positive=False))):
        val, prob = _decimal(cells.get(field, ""), **kw)
        if prob:
            errs.append((field, prob))
        else:
            out[field] = val

    nb, prob = _iso_date(cells.get("need_by_date", ""), required=True)
    if prob:
        errs.append(("need_by_date", prob))
    out["need_by_date"] = nb

    pr, prob = _iso_date(cells.get("promised_date", ""), required=False)
    if prob:
        errs.append(("promised_date", prob))
    out["promised_date"] = pr
    return out, errs


# ---- receipt import ------------------------------------------------------
RECEIPT_REQUIRED = ["idempotency_key", "po_number", "line_no", "received_qty", "rejected_qty",
                    "lot_batch", "coc_received"]

_BOOL = {"yes": True, "true": True, "1": True, "no": False, "false": False, "0": False}


def validate_receipt_row(cells: dict[str, str]) -> tuple[dict, list[tuple[str, str]]]:
    errs: list[tuple[str, str]] = []
    out: dict = {}
    for f in ("idempotency_key", "po_number", "lot_batch"):
        out[f] = cells.get(f, "")
        if out[f] == "":
            errs.append((f, "required"))

    ln = cells.get("line_no", "")
    if not _INT.match(ln) or int(ln) < 1 or int(ln) > 2_000_000_000:
        errs.append(("line_no", "required" if ln == "" else
                     f"must be a whole number of 1 or more, got '{ln}'"))
    else:
        out["line_no"] = int(ln)

    qty, prob = _decimal(cells.get("received_qty", ""), places=3, max_int_digits=9, positive=True)
    if prob:
        errs.append(("received_qty", prob))
    out["received_qty"] = qty

    raw_rej = cells.get("rejected_qty", "")
    if raw_rej == "":
        out["rejected_qty"] = Decimal(0)           # blank means none rejected
    else:
        rej, prob = _decimal(raw_rej, places=3, max_int_digits=9, positive=False)
        if prob:
            errs.append(("rejected_qty", prob))
        out["rejected_qty"] = rej

    raw_coc = cells.get("coc_received", "").lower()
    if raw_coc == "":
        errs.append(("coc_received", "required (yes/no/true/false/1/0)"))
    elif raw_coc not in _BOOL:
        errs.append(("coc_received",
                     f"not a recognised yes/no value: '{cells['coc_received']}' "
                     f"(use yes/no/true/false/1/0)"))
    else:
        out["coc_received"] = _BOOL[raw_coc]
    return out, errs


def import_receipts(session: Session, text: str, mode: str = "all_or_nothing") -> tuple[dict, int]:
    """Every row goes through receipts.record_receipt, the same service as POST /receipts.
    Nothing here inserts into ledger_events. One transaction for the file; each row is a
    SAVEPOINT so a failing row undoes only itself and later rows still see earlier ones."""
    from app.services.po import Conflict, NotFound
    from app.services.receipts import DuplicateReceipt, Invalid, record_receipt

    header, raw_rows = read_csv(text)
    file_errs, ignored = check_header(header, RECEIPT_REQUIRED, [])
    if not file_errs and not raw_rows:
        file_errs = [{"row_number": 0, "field": "(file)", "problem": "no data rows after the header"}]
    if file_errs:
        return _receipt_report(mode, "rejected", 0, [], file_errs, ignored), 422

    errors: list[dict] = []
    applied: list[dict] = []
    seen_keys: dict[str, int] = {}

    def add_err(row_no: int, field: str, problem: str) -> None:
        errors.append({"row_number": row_no, "field": field, "problem": problem})

    for row_no, raw in raw_rows:
        if len(raw) != len(header):
            add_err(row_no, "(row)", f"expected {len(header)} columns, got {len(raw)}")
            continue
        cells = {h: c.strip() for h, c in zip(header, raw)}
        parsed, errs = validate_receipt_row(cells)
        key = parsed["idempotency_key"]
        if key:
            if key in seen_keys:
                errs.append(("idempotency_key", f"duplicate of row {seen_keys[key]} "
                                                f"(same key twice in this file)"))
            else:
                seen_keys[key] = row_no
        if errs:
            for f, p in errs:
                add_err(row_no, f, p)
            continue
        try:
            with session.begin_nested():            # SAVEPOINT for this row only
                res = record_receipt(session, idempotency_key=key, po_number=parsed["po_number"],
                                     line_no=parsed["line_no"], received_qty=parsed["received_qty"],
                                     rejected_qty=parsed["rejected_qty"], lot_batch=parsed["lot_batch"],
                                     coc_received=parsed["coc_received"], commit=False)
        except DuplicateReceipt as e:
            add_err(row_no, "idempotency_key", str(e))
        except NotFound as e:
            add_err(row_no, "po_number", str(e))
        except Conflict as e:
            add_err(row_no, "po_number", str(e))
        except Invalid as e:
            add_err(row_no, "rejected_qty" if "rejected_qty" in str(e) else "received_qty", str(e))
        else:
            applied.append({
                "row_number": row_no, "receipt_id": res["id"], "po_number": res["po_number"],
                "line_no": res["line_no"], "part_id": res["part_id"],
                "accepted_qty": float(res["accepted_qty"]), "rejected_qty": float(res["rejected_qty"]),
                "quarantined_qty": float(res["quarantined_qty"]),
                "exception_flags": res["exception_flags"], "po_line_status": res["po_line_status"],
                "outstanding_qty": float(res["outstanding_qty"])})

    rows_read = len(raw_rows)
    if mode == "all_or_nothing" and errors:
        session.rollback()                          # undo the whole file, applied rows included
        return _receipt_report(mode, "rejected", rows_read, [], errors, ignored,
                               not_applied=len(applied)), 422
    if not applied:
        session.rollback()
        return _receipt_report(mode, "rejected", rows_read, [], errors, ignored), 422
    session.commit()
    errors.sort(key=lambda e: (e["row_number"], e["field"]))
    status = "partial" if errors else "imported"
    return _receipt_report(mode, status, rows_read, applied, errors, ignored), 201


def _receipt_report(mode, status, rows_read, applied, errors, ignored, not_applied=0) -> dict:
    r = _report(mode, status, rows_read, len(applied), errors, ignored, not_applied=not_applied)
    r.pop("imported_pos")
    r["imported_receipts"] = applied
    return r


# ---- PO line import ------------------------------------------------------
def _report(mode: str, status: str, rows_read: int, imported: int, errors: list[dict],
            ignored: list[str], not_applied: int = 0, imported_pos: list | None = None) -> dict:
    bad_rows = {e["row_number"] for e in errors if e["row_number"] > 0}
    return {"mode": mode, "status": status, "rows_read": rows_read, "rows_imported": imported,
            "rows_rejected": len(bad_rows), "valid_rows_not_applied": not_applied,
            "ignored_columns": ignored, "errors": errors, "imported_pos": imported_pos or []}


def import_po_lines(session: Session, text: str, mode: str = "all_or_nothing") -> tuple[dict, int]:
    """Returns (report, http_status). One transaction, one commit."""
    header, raw_rows = read_csv(text)
    file_errs, ignored = check_header(header, PO_REQUIRED, PO_OPTIONAL)
    if not file_errs and not raw_rows:
        file_errs = [{"row_number": 0, "field": "(file)", "problem": "no data rows after the header"}]
    if file_errs:
        return _report(mode, "rejected", 0, 0, file_errs, ignored), 422

    # Trailing optional columns may be left off a row entirely (hand-typed CSVs often do this).
    optional_tail = 0
    for h in reversed(header):
        if h in PO_OPTIONAL:
            optional_tail += 1
        else:
            break

    known_parts = {p for (p,) in session.execute(select(Part.part_id))}
    existing_pos = {p for (p,) in session.execute(select(POLine.po_number).distinct())}

    errors: list[dict] = []
    valid: list[tuple[int, dict]] = []          # (row_number, parsed) rows with no problems
    po_of_row: dict[int, str] = {}
    bad_row_numbers: set[int] = set()
    seen: dict[tuple[str, int], int] = {}

    for row_no, raw in raw_rows:
        if len(header) - optional_tail <= len(raw) < len(header):
            raw = raw + [""] * (len(header) - len(raw))
        if len(raw) != len(header):
            errors.append({"row_number": row_no, "field": "(row)",
                           "problem": f"expected {len(header)} columns, got {len(raw)}"})
            bad_row_numbers.add(row_no)
            continue
        cells = {h: c.strip() for h, c in zip(header, raw)}
        parsed, errs = validate_po_row(cells, known_parts)
        if parsed["po_number"]:
            po_of_row[row_no] = parsed["po_number"]
            if parsed["po_number"] in existing_pos:
                errs.append(("po_number", f"PO '{parsed['po_number']}' already exists in the "
                                          f"database; import creates new POs only"))
        if parsed["po_number"] and "line_no" in parsed:
            key = (parsed["po_number"], parsed["line_no"])
            if key in seen:
                errs.append(("line_no", f"duplicate of row {seen[key]} "
                                        f"({key[0]} line {key[1]})"))
            else:
                seen[key] = row_no
        for field, problem in errs:
            errors.append({"row_number": row_no, "field": field, "problem": problem})
        if errs:
            bad_row_numbers.add(row_no)
        else:
            valid.append((row_no, parsed))

    rows_read = len(raw_rows)

    if mode == "all_or_nothing":
        if errors:
            return _report(mode, "rejected", rows_read, 0, errors, ignored,
                           not_applied=len(valid)), 422
        to_import = valid
    else:
        bad_pos = {po_of_row[r] for r in bad_row_numbers if r in po_of_row}
        to_import = []
        for row_no, parsed in valid:
            if parsed["po_number"] in bad_pos:
                errors.append({"row_number": row_no, "field": "po_number",
                               "problem": f"skipped: another line of PO '{parsed['po_number']}' "
                                          f"is invalid, and a PO is imported whole or not at all"})
            else:
                to_import.append((row_no, parsed))
        errors.sort(key=lambda e: (e["row_number"], e["field"]))

    totals: dict[str, Decimal] = defaultdict(Decimal)
    for _, p in to_import:
        totals[p["po_number"]] += p["ordered_qty"] * p["unit_price_inr"]
    status_by_po = {po: initial_status(t, config.APPROVAL_THRESHOLD_INR) for po, t in totals.items()}

    for _, p in to_import:
        session.add(POLine(po_number=p["po_number"], line_no=p["line_no"], part_id=p["part_id"],
                           supplier=p["supplier"], ordered_qty=p["ordered_qty"],
                           unit_price_inr=p["unit_price_inr"], need_by_date=p["need_by_date"],
                           promised_date=p["promised_date"], status=status_by_po[p["po_number"]]))
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        err = [{"row_number": 0, "field": "(database)",
                "problem": "conflict while saving (a line may have been created concurrently); "
                           "nothing was imported"}]
        return _report(mode, "rejected", rows_read, 0, err, ignored, not_applied=len(valid)), 422

    imported_pos = [{"po_number": po, "lines": sum(1 for _, p in to_import if p["po_number"] == po),
                     "po_total_inr": float(totals[po]), "status": status_by_po[po]}
                    for po in sorted(totals)]
    if not to_import:
        status, code = "rejected", 422
    elif errors:
        status, code = "partial", 201
    else:
        status, code = "imported", 201
    return _report(mode, status, rows_read, len(to_import), errors, ignored,
                   imported_pos=imported_pos), code
