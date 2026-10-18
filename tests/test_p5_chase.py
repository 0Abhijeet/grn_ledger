"""P5 tests. Expected outcomes written by hand BEFORE running. No network: the LLM is a fake.

Seeded (relative to today):
  PO-1001/1 FAS-001 500, need_by today-5, no promise     -> overdue + no_promise_date
  PO-1003/1 HAR-001 300, need_by today-3, promised today+4 -> overdue + at_risk
Eligible (overdue or at_risk) = PO-1001/1, PO-1002/1, PO-1003/1, PO-1005/2   (4 lines)
Not eligible: PO-1001/2 (on track), PO-1004/1 (pending approval)
"""
import re
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.chase import FORBIDDEN, validate_draft

FACTS = {"po_number": "PO-1001", "outstanding_qty": "300", "need_by_date": "2026-01-05",
         "promised_date": "2026-01-15"}
GOOD = "Re PO-1001: 300 units outstanding, need-by 2026-01-05, promised 2026-01-15. Please confirm."


# ---- validator (pure) --------------------------------------------------
def test_valid_draft_passes():
    assert validate_draft(GOOD, FACTS) == []


def test_missing_po_number():
    assert validate_draft(GOOD.replace("PO-1001", "the order"), FACTS) == ["missing_po_number"]


def test_outstanding_qty_is_not_matched_inside_a_bigger_number():
    text = GOOD.replace("300 units", "3000 units")
    assert validate_draft(text, FACTS) == ["missing_outstanding_qty"]


def test_missing_either_date():
    assert validate_draft(GOOD.replace("2026-01-05", "soon"), FACTS) == ["missing_need_by_date"]
    assert validate_draft(GOOD.replace("2026-01-15", "later"), FACTS) == ["missing_promised_date"]


def test_no_promise_requires_the_phrase():
    facts = {**FACTS, "promised_date": None}
    ok = "PO-1001: 300 outstanding, need-by 2026-01-05, no promised date on record."
    assert validate_draft(ok, facts) == []
    assert validate_draft(ok.replace("no promised date on record", "unknown"), facts) == [
        "missing_promised_date"]


@pytest.mark.parametrize("bad", [
    "We will cancel the order.", "A penalty may apply.", "Our legal team is aware.",
    "We will pay on receipt.", "We guarantee fast payment.", "You are in breach.",
    "We hold you responsible.", "Liquidated damages apply.",
])
def test_forbidden_language_is_caught(bad):
    fails = validate_draft(GOOD + " " + bad, FACTS)
    assert any(f.startswith("forbidden_language") for f in fails)


def test_forbidden_list_is_non_empty():
    assert len(FORBIDDEN) >= 10


# ---- fake LLM ----------------------------------------------------------
class FakeClient:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []
        # mimics the Groq/OpenAI shape: client.chat.completions.create(...).choices[0].message.content
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.calls.append(kw)
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=r))])


@pytest.fixture()
def env(seeded, monkeypatch, tmp_path):
    monkeypatch.setenv("CHASE_LOG_PATH", str(tmp_path / "drafts.jsonl"))
    monkeypatch.setenv("GROQ_MODEL", "fake-model")
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    return tmp_path


def draft(po, line, **kw):
    from app.db import get_session
    from app.services.chase import generate_draft
    with get_session() as s:
        return generate_draft(s, po, line, **kw)


def iso(delta):
    return (date.today() + timedelta(days=delta)).isoformat()


def good_polish(po="PO-1001", qty="500", nb=-5):
    return f"Hello, a gentle follow-up on {po}: {qty} units are outstanding. Our need-by date " \
           f"was {iso(nb)} and there is no promised date on record. Could you confirm a date?"


# ---- agent (DB, fake LLM) ----------------------------------------------
def test_eligible_lines(env):
    from fastapi.testclient import TestClient

    from app.main import app
    r = TestClient(app).get("/chase-drafts/eligible")
    assert {(x["po_number"], x["line_no"]) for x in r.json()} == {
        ("PO-1001", 1), ("PO-1002", 1), ("PO-1003", 1), ("PO-1005", 2)}


def test_not_eligible_lines_refused(env):
    from app.services.po import Conflict, NotFound
    with pytest.raises(Conflict):
        draft("PO-1001", 2, use_llm=False)        # on track
    with pytest.raises(Conflict):
        draft("PO-1004", 1, use_llm=False)        # pending approval
    with pytest.raises(NotFound):
        draft("PO-9999", 1, use_llm=False)


def test_template_draft_has_required_facts_and_is_not_sent(env):
    d = draft("PO-1001", 1, use_llm=False)
    assert d["source"] == "template" and d["sent"] is False and d["status"] == "draft_not_sent"
    assert d["validation_failures"] == []
    t = d["text"]
    assert "PO-1001" in t and "500" in t and iso(-5) in t
    assert "no promised date on record" in t and "no receipts recorded yet" in t
    assert d["reasons"] == ["overdue"]


def test_draft_is_logged_to_file_only(env):
    draft("PO-1001", 1, use_llm=False)
    lines = (env / "drafts.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1 and '"sent": false' in lines[0]


def test_no_llm_configured_falls_back_with_note(env):
    d = draft("PO-1001", 1, use_llm=True)       # model set, key removed, no client passed
    assert d["source"] == "template" and "not configured" in d["note"]


def test_llm_valid_first_try(env):
    fake = FakeClient([good_polish()])
    d = draft("PO-1001", 1, client=fake)
    assert (d["source"], d["llm_attempts"], len(fake.calls)) == ("llm", 1, 1)
    assert d["validation_failures"] == []


def test_llm_fails_then_regenerates_once(env):
    bad = good_polish() + " Otherwise we will cancel the order."
    fake = FakeClient([bad, good_polish()])
    d = draft("PO-1001", 1, client=fake)
    assert (d["source"], d["llm_attempts"]) == ("llm", 2)
    assert any(f.startswith("forbidden_language") for f in d["first_attempt_failures"])
    assert "forbidden_language" in fake.calls[1]["messages"][-1]["content"]   # feedback was sent


def test_llm_fails_twice_falls_back_to_template(env):
    bad = "Pay up or face legal action."
    fake = FakeClient([bad, bad])
    d = draft("PO-1001", 1, client=fake)
    assert (d["source"], d["llm_attempts"], len(fake.calls)) == ("template", 2, 2)
    assert "failed validation twice" in d["note"] and d["validation_failures"] == []


def test_unicode_hyphens_from_model_are_normalised_not_rejected(env):
    # found with the live model: it rewrites "-" as U+2011, which looks identical but breaks search
    from app.services.chase import normalise_typography
    assert normalise_typography("PO‑1001 on 2026‑10‑07") == "PO-1001 on 2026-10-07"
    fancy = good_polish().replace("PO-1001", "PO‑1001").replace(iso(-5), iso(-5).replace("-", "‑"))
    d = draft("PO-1001", 1, client=FakeClient([fancy]))
    assert (d["source"], d["llm_attempts"]) == ("llm", 1)
    assert "PO-1001" in d["text"] and "‑" not in d["text"]


def test_empty_llm_reply_is_reported_as_empty_and_falls_back(env):
    # found with the live model: a reasoning model can burn the whole token budget and return ""
    d = draft("PO-1001", 1, client=FakeClient(["", ""]))
    assert d["source"] == "template" and d["first_attempt_failures"] == ["empty_reply"]
    assert "empty_reply" in d["note"]


def test_llm_exception_falls_back_to_template(env):
    d = draft("PO-1001", 1, client=FakeClient([RuntimeError("boom")]))
    assert d["source"] == "template" and "LLM error" in d["note"]


def test_receipt_history_and_outstanding_in_draft(env):
    from fastapi.testclient import TestClient

    from app.main import app
    c = TestClient(app)
    assert c.post("/receipts", json={"idempotency_key": "h1", "po_number": "PO-1003", "line_no": 1,
                                     "received_qty": 100, "rejected_qty": 0, "lot_batch": "L",
                                     "coc_received": True}).status_code == 201
    d = draft("PO-1003", 1, use_llm=False)
    assert "Outstanding quantity: 200 metre" in d["text"]
    assert "1 receipt(s): 100 received, 100 accepted, 0 rejected, 0 held for checks" in d["text"]
    assert iso(4) in d["text"] and d["reasons"] == ["overdue", "at_risk"]


def test_api_endpoint_returns_draft(env):
    from fastapi.testclient import TestClient

    from app.main import app
    r = TestClient(app).post("/chase-drafts", json={"po_number": "PO-1001", "line_no": 1,
                                                    "use_llm": False})
    assert r.status_code == 201 and r.json()["sent"] is False
    assert TestClient(app).post("/chase-drafts", json={"po_number": "PO-1001", "line_no": 2,
                                                       "use_llm": False}).status_code == 409


def test_no_send_code_path_in_app():
    banned = re.compile(r"smtplib|sendgrid|twilio|whatsapp|slack_sdk|boto3|sendmail|send_email|"
                        r"requests\.(?:get|post)|urllib\.request|httpx\.(?:get|post)", re.I)
    hits = [str(p) for p in Path("app").rglob("*.py") if banned.search(p.read_text(encoding="utf-8"))]
    assert hits == []
