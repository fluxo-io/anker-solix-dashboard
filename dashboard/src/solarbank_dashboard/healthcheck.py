from __future__ import annotations

import os
import sys
from urllib.request import Request, urlopen


def main() -> int:
    try:
        headers = {}
        token = os.getenv("DASHBOARD_HEALTH_TOKEN", "")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = Request("http://127.0.0.1:8501/health", headers=headers)
        with urlopen(request, timeout=3) as response:
            if response.status != 200:
                raise RuntimeError("Dashboard antwortet nicht")
    except Exception as error:  # noqa: BLE001 - healthcheck must report every failure
        print(f"unhealthy: {type(error).__name__}: {error}")
        return 1
    print("healthy")
    return 0


if __name__ == "__main__":
    sys.exit(main())
