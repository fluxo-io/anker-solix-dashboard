from __future__ import annotations

import time
from pathlib import Path


def main() -> int:
    try:
        age = time.time() - float(Path("/tmp/collector-heartbeat").read_text())
        return 0 if 0 <= age <= 45 else 1
    except (OSError, ValueError):
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
