from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime

import asyncpg


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} fehlt")
    return value


async def check() -> None:
    interval = int(os.getenv("POLL_INTERVAL_SECONDS", "300"))
    connection = await asyncio.wait_for(
        asyncpg.connect(
            host=_required("DB_HOST"),
            port=int(os.getenv("DB_PORT", "5432")),
            database=_required("DB_NAME"),
            user=_required("DB_USER"),
            password=_required("DB_PASSWORD"),
            command_timeout=5,
        ),
        timeout=7,
    )
    try:
        last_success = await connection.fetchval(
            "SELECT last_success_at FROM collector_state WHERE id = 1"
        )
    finally:
        await connection.close()

    if last_success is None:
        raise RuntimeError("Noch keine erfolgreiche Messung")
    age = (datetime.now(UTC) - last_success).total_seconds()
    maximum_age = max(interval * 3, 180)
    if age > maximum_age:
        raise RuntimeError("Letzte erfolgreiche Messung ist zu alt")


def main() -> None:
    try:
        asyncio.run(check())
    except Exception as error:
        print(f"unhealthy: {type(error).__name__}: {error}")
        raise SystemExit(1) from error
    print("healthy")


if __name__ == "__main__":
    main()
