"""Chase-draft agent. DRAFTS ONLY: nothing in this module (or the app) sends anything.

Flow: deterministic template builds the facts -> optional LLM polishes tone ->
validate_draft() in code -> regenerate once with feedback -> fall back to the template.
The LLM is untrusted; the validator and the template are the safety net.
"""
import json
import os
import re
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Part, POLine, ReceiptEvent
from app.services.po import Conflict, NotFound, load_lines

NO_PROMISE_PHRASE = "no promised date on record"

# Commitments, threats, penalties, legal language. Conservative on purpose: a false positive
# only costs one regeneration and then the template, which is itself clean.
FORBIDDEN = [
    r"penalt(?:y|ies)", r"liquidated", r"damages?", r"legal(?:ly)?", r"lawyers?", r"attorneys?",
    r"litigat\w*", r"lawsuit", r"\bsue\b", r"\bsuing\b", r"breach\w*", r"terminat\w*",
    r"cancel\w*", r"blacklist\w*", r"liab\w*", r"hold you (?:responsible|accountable)",
    r"chargeback", r"consequences?", r"ultimatum", r"recover(?:y of)? costs?",
    r"\b(?:we|i) (?:will|shall|promise|guarantee|commit|agree|assure)\b", r"\bwill pay\b",
]
_FORBIDDEN_RE = [re.compile(p, re.IGNORECASE) for p in FORBIDDEN]


def fmt_qty(q: Decimal) -> str:
    return format(Decimal(q).normalize(), "f")


def _num_present(text: str, number: str) -> bool:
    return re.search(rf"(?<![\d.]){re.escape(number)}(?![\d])", text) is not None


def validate_draft(text: str, facts: dict) -> list[str]:
    """Return failure codes; empty list means the draft passes."""
    fails = []
    if facts["po_number"] not in text:
        fails.append("missing_po_number")
    if not _num_present(text, facts["outstanding_qty"]):
        fails.append("missing_outstanding_qty")
    if facts["need_by_date"] not in text:
        fails.append("missing_need_by_date")
    promised = facts["promised_date"]
    if promised is None:
        if NO_PROMISE_PHRASE not in text.lower():
            fails.append("missing_promised_date")
    elif promised not in text:
        fails.append("missing_promised_date")
    for rx in _FORBIDDEN_RE:
        m = rx.search(text)
        if m:
            fails.append(f"forbidden_language:{m.group(0).lower()}")
    return fails


def _history(session: Session, line_id: int) -> dict:
    r = session.execute(select(
        func.count(ReceiptEvent.id), func.coalesce(func.sum(ReceiptEvent.received_qty), 0),
        func.coalesce(func.sum(ReceiptEvent.accepted_qty), 0),
        func.coalesce(func.sum(ReceiptEvent.rejected_qty), 0),
        func.coalesce(func.sum(ReceiptEvent.quarantined_qty), 0),
    ).where(ReceiptEvent.po_line_id == line_id)).one()
    return {"receipts": r[0], "received": Decimal(r[1]), "accepted": Decimal(r[2]),
            "rejected": Decimal(r[3]), "quarantined": Decimal(r[4])}


def build_template(facts: dict) -> str:
    h = facts["history"]
    if h["receipts"] == 0:
        history = "no receipts recorded yet"
    else:
        history = (f"{h['receipts']} receipt(s): {fmt_qty(h['received'])} received, "
                   f"{fmt_qty(h['accepted'])} accepted, {fmt_qty(h['rejected'])} rejected, "
                   f"{fmt_qty(h['quarantined'])} held for checks")
    promised = facts["promised_date"] or NO_PROMISE_PHRASE
    if facts["promised_date"] is None:
        ask = "Could you please confirm a delivery date for the outstanding quantity?"
    elif facts["overdue"]:
        ask = ("Could you please confirm the current status and when the outstanding quantity "
               "can be expected?")
    else:
        ask = ("The promised date is later than our need-by date. Could you please confirm "
               "whether delivery can be brought forward?")
    return (
        f"Subject: Follow-up on {facts['po_number']} line {facts['line_no']}\n\n"
        f"Dear {facts['supplier']} team,\n\n"
        f"We are following up on purchase order {facts['po_number']}, line {facts['line_no']} "
        f"({facts['part_id']}, {facts['description']}).\n\n"
        f"- Outstanding quantity: {facts['outstanding_qty']} {facts['uom']}\n"
        f"- Need-by date: {facts['need_by_date']}\n"
        f"- Promised date: {promised}\n"
        f"- Receipts so far: {history}\n\n"
        f"{ask}\n\nThank you,\nPurchasing\n"
    )


SYSTEM_PROMPT = (
    "You polish purchasing follow-up messages. Rewrite the draft so it is polite, brief and "
    "professional. Keep every fact exactly: PO number, line number, quantities, and dates in "
    "YYYY-MM-DD format, and keep the phrase 'no promised date on record' if present. Do not add "
    "facts, deadlines, commitments, promises, threats, penalties or legal language. Do not "
    "commit the buyer to anything. Use plain ASCII hyphens (-) exactly as in the draft, never "
    "non-breaking or typographic hyphens. Output only the message text."
)


def _llm_polish(client, model: str, template: str, feedback: list[str] | None) -> str:
    content = f"Draft to polish:\n\n{template}"
    if feedback:
        content += ("\n\nYour previous attempt failed these checks: " + ", ".join(feedback) +
                    ". Fix them. Keep all facts exactly as in the draft.")
    resp = client.chat.completions.create(
        # reasoning models (e.g. gpt-oss) spend tokens thinking before they answer: keep effort
        # low and the budget generous, or the reply can come back empty (finish_reason=length)
        model=model, max_tokens=3000, temperature=0.3, reasoning_effort="low",
        messages=[{"role": "system", "content": SYSTEM_PROMPT},
                  {"role": "user", "content": content}])
    return normalise_typography(resp.choices[0].message.content or "").strip()


# Models like to "improve" ASCII hyphens into look-alike Unicode ones. A human cannot see it,
# but a search for "PO-1001" would miss it. Mapping them back cannot change a fact.
_TYPO_MAP = {**{cp: "-" for cp in (0x2010, 0x2011, 0x2012, 0x2013, 0x2014, 0x2015, 0x2212)},
             0x00A0: " ", 0x202F: " "}


def normalise_typography(text: str) -> str:
    return text.translate(_TYPO_MAP)


def _default_client():
    """Groq client, only if both the key and the model name are set. Reads GROQ_API_KEY itself."""
    if not (os.getenv("GROQ_API_KEY") and os.getenv("GROQ_MODEL")):
        return None
    from groq import Groq
    return Groq()


def log_path() -> Path:
    return Path(os.getenv("CHASE_LOG_PATH", "drafts_log/chase_drafts.jsonl"))


def _append_log(record: dict) -> None:
    p = log_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, default=str) + "\n")


def eligible_lines(session: Session, as_of: date | None = None) -> list[dict]:
    return [ln for ln in load_lines(session, as_of=as_of) if ln["overdue"] or ln["at_risk"]]


def generate_draft(session: Session, po_number: str, line_no: int, *, use_llm: bool = True,
                   client=None, as_of: date | None = None) -> dict:
    lines = [ln for ln in load_lines(session, po_number, as_of) if ln["line_no"] == line_no]
    if not lines:
        raise NotFound(f"PO line {po_number}/{line_no} not found")
    ln = lines[0]
    if not (ln["overdue"] or ln["at_risk"]):
        raise Conflict(f"PO line {po_number}/{line_no} is neither overdue nor at_risk; "
                       f"no chase draft needed")

    po = session.execute(select(POLine).where(
        POLine.po_number == po_number, POLine.line_no == line_no)).scalar_one()
    part = session.get(Part, po.part_id)
    facts = {
        "po_number": po_number, "line_no": line_no, "part_id": ln["part_id"],
        "description": part.description, "uom": part.uom, "supplier": ln["supplier"],
        "outstanding_qty": fmt_qty(ln["outstanding_qty"]),
        "need_by_date": ln["need_by_date"].isoformat(),
        "promised_date": ln["promised_date"].isoformat() if ln["promised_date"] else None,
        "overdue": ln["overdue"], "at_risk": ln["at_risk"],
        "history": _history(session, po.id),
    }
    template = build_template(facts)
    assert not validate_draft(template, facts), "template must always pass its own validator"

    text, source, attempts, note = template, "template", 0, None
    first_failures: list[str] = []
    if use_llm:
        client = client or _default_client()
        model = os.getenv("GROQ_MODEL", "")
        if client is None:
            note = "LLM not configured (GROQ_API_KEY / GROQ_MODEL); template used"
        else:
            try:
                feedback = None
                for attempts in (1, 2):
                    candidate = _llm_polish(client, model, template, feedback)
                    failures = validate_draft(candidate, facts) if candidate else ["empty_reply"]
                    if attempts == 1:
                        first_failures = failures
                    if not failures:
                        text, source = candidate, "llm"
                        break
                    feedback = failures
                else:
                    note = f"LLM output failed validation twice ({', '.join(failures)}); template used"
            except Exception as e:  # noqa: BLE001
                note = f"LLM error ({type(e).__name__}); template used"

    record = {
        "draft_id": str(uuid.uuid4()),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "po_number": po_number, "line_no": line_no, "part_id": ln["part_id"],
        "supplier": ln["supplier"], "reasons": [k for k in ("overdue", "at_risk") if ln[k]],
        "text": text, "source": source, "llm_attempts": attempts,
        "first_attempt_failures": first_failures, "note": note,
        "validation_failures": validate_draft(text, facts),
        "sent": False,
        "status": "draft_not_sent",
    }
    _append_log(record)
    return record


def read_log(limit: int = 50) -> list[dict]:
    p = log_path()
    if not p.exists():
        return []
    rows = [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]
    return rows[-limit:]
