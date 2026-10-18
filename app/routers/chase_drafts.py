from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas import POLineOut
from app.services import chase as svc
from app.services.po import Conflict, NotFound

router = APIRouter(prefix="/chase-drafts", tags=["chase-drafts"])


class DraftIn(BaseModel):
    po_number: str
    line_no: int
    use_llm: bool = True


class DraftOut(BaseModel):
    draft_id: str
    created_at: str
    po_number: str
    line_no: int
    part_id: str
    supplier: str
    reasons: list[str]
    text: str
    source: str
    llm_attempts: int
    first_attempt_failures: list[str]
    note: str | None
    validation_failures: list[str]
    sent: bool
    status: str


@router.get("/eligible", response_model=list[POLineOut],
            summary="Lines that are overdue or at_risk (chase candidates)")
def eligible(db: Session = Depends(get_db)):
    return svc.eligible_lines(db)


@router.post("", response_model=DraftOut, status_code=201,
             summary="Generate a chase DRAFT for one line. Nothing is sent.")
def create_draft(body: DraftIn, db: Session = Depends(get_db)):
    try:
        return svc.generate_draft(db, body.po_number, body.line_no, use_llm=body.use_llm)
    except NotFound as e:
        raise HTTPException(404, str(e))
    except Conflict as e:
        raise HTTPException(409, str(e))


@router.get("", response_model=list[DraftOut], summary="Drafts generated so far (from the log file)")
def list_drafts(limit: int = 50):
    return svc.read_log(limit)
