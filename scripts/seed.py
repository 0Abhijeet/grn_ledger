import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.seed import seed  # noqa: E402

seed()
print("seeded: 8 parts, 11 PO lines")
