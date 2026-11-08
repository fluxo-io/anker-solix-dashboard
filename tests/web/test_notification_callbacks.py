from solarbank_dashboard import app as module
from solarbank_dashboard.auth import AuthSettings
from solarbank_dashboard.config import Settings
from solarbank_dashboard.repository import ApiError


def test_real_dash_callback_publishes_event_store_and_preserves_context(
    monkeypatch, dashboard_data
):
    settings = Settings("http://synthetic", "t" * 32, "Europe/Berlin")
    app = module.create_app(settings, auth_config=AuthSettings(enabled=False))
    client = app.server.test_client()
    key, metadata = next(
        (key, value)
        for key, value in app.callback_map.items()
        if "collector-status.children" in key
    )
    values = {
        "period-filter": "24 Stunden",
        "custom-range": None,
        "site-filter": [],
        "device-filter": [],
        "valid-filter": ["valid"],
        "reload-button": 1,
        "auto-refresh-interval": 0,
    }
    payload = {
        "output": key,
        "outputs": [
            {"id": value.component_id, "property": value.component_property}
            for value in metadata["output"]
        ],
        "inputs": [
            {**value, "value": values[value["id"]]} for value in metadata["inputs"]
        ],
        "state": [],
        "changedPropIds": ["reload-button.n_clicks"],
    }

    def fail(*args, **kwargs):
        raise ApiError()

    monkeypatch.setattr(module, "fetch_dashboard", fail)
    failed = client.post("/_dash-update-component", json=payload)
    assert failed.status_code == 200
    data = failed.json["response"]
    event = data["notification-data"]["data"]
    assert event["level"] == "error"
    assert event["message"]
    assert data["data-message"]["children"] == ""
    assert data["collector-status"]["children"] == "Daten nicht verfügbar"
    repeated = client.post("/_dash-update-component", json=payload)
    assert repeated.json["response"]["notification-data"]["data"] == event

    monkeypatch.setattr(
        module, "fetch_dashboard", lambda *args, **kwargs: dashboard_data
    )
    recovered = client.post("/_dash-update-component", json=payload)
    assert recovered.status_code == 200
    assert recovered.json["response"]["notification-data"]["data"]["level"] == "success"
    assert recovered.json["response"]["data-message"]["children"]
    payload["changedPropIds"] = ["auto-refresh-interval.n_intervals"]
    polled = client.post("/_dash-update-component", json=payload)
    assert polled.json["response"]["notification-data"]["data"] is None

    dependencies = client.get("/_dash-dependencies").json
    consumer = next(
        item for item in dependencies if item["output"] == "notification-ack.data"
    )
    assert consumer["clientside_function"] == {
        "namespace": "fluxo",
        "function_name": "consumeNotifications",
    }
    assert [value["id"] for value in consumer["inputs"]] == [
        "notification-filter",
        "notification-data",
        "notification-snapshot",
    ]
