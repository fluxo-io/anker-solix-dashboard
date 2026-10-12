from __future__ import annotations

from urllib.request import urlopen


def main() -> int:
    try:
        with urlopen("http://127.0.0.1:8000/healthz", timeout=5) as response:
            if response.status != 200:
                return 1
    except Exception:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
