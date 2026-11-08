import os

# Isolated defaults must be present before importing the Dash module.
os.environ["AUTH_ENABLED"] = "false"
os.environ["API_URL"] = "http://api:8001"
os.environ["API_TOKEN"] = "test-token-" + "a" * 32
os.environ["SECRET_KEY"] = "test-secret-" + "b" * 32
os.environ["CANONICAL_ORIGIN"] = ""


import pytest


@pytest.fixture
def dashboard_data():
    """API contract fixture, deliberately containing no local calculations."""
    return {
        "rows": [],
        "aggregated": [],
        "energy": [],
        "loaded_count": 0,
        "filtered_count": 0,
        "truncated": False,
        "unknown_source_age": False,
        "energy_status": "empty",
        "poll_interval_seconds": 300,
        "kpis": {
            key: None
            for key in ("pv_total_w", "home_load_w", "grid_power_w", "battery_soc_pct")
        },
        "collector": {
            "status": "missing",
            "last_success_at": None,
            "consecutive_failures": 0,
        },
        "freshness": {"rows": [], "warning": True, "oldest_timestamp": None},
    }
