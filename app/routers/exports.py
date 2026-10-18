from fastapi import APIRouter, Depends, Response
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import exports as svc

router = APIRouter(prefix="/export", tags=["export"])

NOTE = (" Read-only. Text cells that start with = + - @ are prefixed with a single quote so Excel "
        "cannot run them as formulas. Layout is a guess at an Excel sheet.")


def _csv(body: str, filename: str) -> Response:
    return Response(content=body, media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.get("/ledger-events.csv", summary="Every ledger event, oldest first", description=NOTE)
def ledger_events(db: Session = Depends(get_db)):
    return _csv(svc.ledger_events_csv(db), "ledger-events.csv")


@router.get("/balances.csv", summary="Derived balance per part (all parts in the master)",
            description="available excludes quarantined and rejected; physical = available + "
                        "quarantined." + NOTE)
def balances(db: Session = Depends(get_db)):
    return _csv(svc.balances_csv(db), "balances.csv")


@router.get("/exceptions.csv", summary="Receipts with rejected, missing_coc or over_delivery flags",
            description="One row per receipt and flag. quarantine_remaining is what is still held "
                        "after any releases." + NOTE)
def exceptions(db: Session = Depends(get_db)):
    return _csv(svc.exceptions_csv(db), "exceptions.csv")
