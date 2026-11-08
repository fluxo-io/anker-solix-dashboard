from datetime import UTC, datetime, timedelta

import pandas as pd
from solarbank_dashboard.charts import line_figure


def test_chart_skips_empty_series() -> None:
    frame = pd.DataFrame(
        {
            "bucket_start": [datetime(2026, 9, 21, 12, tzinfo=UTC)],
            "pv_total_w": [100.0],
            "pv_1_w": [None],
        }
    )

    figure = line_figure(frame, ["pv_total_w", "pv_1_w"], y_title="W")

    assert [trace.name for trace in figure.data] == ["PV gesamt"]


def test_chart_preserves_interaction_and_breaks_missing_time_intervals() -> None:
    start = datetime(2026, 9, 21, 12, tzinfo=UTC)
    frame = pd.DataFrame(
        {
            "bucket_start": [
                start,
                start + timedelta(minutes=5),
                start + timedelta(hours=1),
            ],
            "pv_total_w": [100, 200, 300],
        }
    )
    figure = line_figure(frame, ["pv_total_w"], y_title="W", revision="same-filter")
    assert figure.layout.uirevision == "same-filter"
    assert figure.data[0].uid == "pv_total_w"
    assert len(figure.data[0].y) == 4
    assert pd.isna(figure.data[0].y[2])
