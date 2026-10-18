import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture(scope="session")
def db_ready():
    """Skip DB tests (loudly) if the database is unreachable, instead of erroring."""
    from sqlalchemy import text

    from app.db import get_engine
    try:
        with get_engine().connect() as c:
            c.execute(text("SELECT 1"))
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"database unreachable: {type(e).__name__}")


@pytest.fixture()
def seeded(db_ready):
    from app.seed import seed
    seed()
