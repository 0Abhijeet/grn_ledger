"""Apply all migrations/*.sql in filename order. Safe to re-run (statements are idempotent)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402

from app.db import get_engine  # noqa: E402

for f in sorted((Path(__file__).resolve().parent.parent / "migrations").glob("*.sql")):
    with get_engine().begin() as conn:
        conn.exec_driver_sql(f.read_text(encoding="utf-8"))
    print(f"applied {f.name}")

with get_engine().connect() as conn:
    n = conn.execute(text(
        "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'")).scalar()
    print(f"tables in public schema: {n}")
