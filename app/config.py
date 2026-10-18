import os
from decimal import Decimal

from dotenv import load_dotenv

load_dotenv()


def database_url() -> str:
    url = os.getenv("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not set. Copy .env.example to .env and fill it in.")
    # Hosts like Neon give "postgresql://"; SQLAlchemy would then pick psycopg2.
    # We ship psycopg v3, so force that driver.
    for prefix in ("postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


# ASSUMPTION (README must label this): PO total above this needs approval.
APPROVAL_THRESHOLD_INR = Decimal(os.getenv("APPROVAL_THRESHOLD_INR", "100000"))

# ASSUMPTION (used in P3): over-delivery tolerance as % of ordered qty.
OVER_DELIVERY_TOLERANCE_PCT = Decimal(os.getenv("OVER_DELIVERY_TOLERANCE_PCT", "5"))
