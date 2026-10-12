from __future__ import annotations

from collections.abc import Sequence

import pandas as pd
import plotly.graph_objects as go

DEVICE_POWER_COLUMNS = [
    "pv_total_w",
    "pv_1_w",
    "pv_2_w",
    "pv_3_w",
    "pv_4_w",
    "battery_power_w",
    "output_power_w",
]

SITE_POWER_COLUMNS = [
    "home_load_w",
    "grid_power_w",
    "grid_import_w",
    "grid_export_w",
    "grid_to_home_w",
    "grid_to_battery_w",
]

LABELS = {
    "pv_total_w": "PV gesamt",
    "pv_1_w": "PV1",
    "pv_2_w": "PV2",
    "pv_3_w": "PV3",
    "pv_4_w": "PV4",
    "battery_power_w": "Akku",
    "output_power_w": "Ausgang",
    "home_load_w": "Haus",
    "grid_power_w": "Netz",
    "grid_import_w": "Netzbezug",
    "grid_export_w": "Einspeisung",
    "grid_to_home_w": "Netz → Haus",
    "grid_to_battery_w": "Netz → Akku",
    "battery_soc_pct": "Ladezustand Ø",
}

COLORS = {
    "pv_total_w": "#d79b22",
    "pv_1_w": "#eab94f",
    "pv_2_w": "#c77b24",
    "pv_3_w": "#8da33b",
    "pv_4_w": "#5f8f45",
    "battery_power_w": "#3e7cb1",
    "output_power_w": "#6c63a8",
    "home_load_w": "#2f7d4c",
    "grid_power_w": "#b05252",
    "grid_import_w": "#b05252",
    "grid_export_w": "#d79b22",
    "grid_to_home_w": "#2f7d4c",
    "grid_to_battery_w": "#3e7cb1",
    "battery_soc_pct": "#3e7cb1",
}


def aggregate_measurements(
    frame: pd.DataFrame, *, expected_devices: Sequence[tuple[str, str]] | None = None
) -> pd.DataFrame:
    if frame.empty:
        return frame.copy()

    working = frame.copy()
    working["pv_total_w"] = working["pv_reported_total_w"].combine_first(
        working["pv_calculated_total_w"]
    )
    device_power = [column for column in DEVICE_POWER_COLUMNS if column in working]
    device_count = len(set(expected_devices)) if expected_devices else 1
    result = (
        working.groupby("bucket_start", as_index=False)[device_power]
        .sum(min_count=device_count)
        .sort_values("bucket_start")
    )

    site_power = [column for column in SITE_POWER_COLUMNS if column in working]
    if site_power:
        if "site_id" in working:
            site_values = working.groupby(["bucket_start", "site_id"], as_index=False)[
                site_power
            ].first()
        else:
            site_values = working[["bucket_start", *site_power]]
        site_totals = (
            site_values.groupby("bucket_start", as_index=False)[site_power]
            .sum(
                min_count=len({site for site, _ in expected_devices})
                if expected_devices
                else 1
            )
            .sort_values("bucket_start")
        )
        result = result.merge(site_totals, on="bucket_start", how="left")

    if "battery_soc_pct" in working:
        state_of_charge = (
            working.groupby("bucket_start", as_index=False)["battery_soc_pct"]
            .mean()
            .sort_values("bucket_start")
        )
        counts = working.groupby("bucket_start")["battery_soc_pct"].count()
        state_of_charge.loc[
            state_of_charge["bucket_start"].map(counts) < device_count,
            "battery_soc_pct",
        ] = float("nan")
        result = result.merge(state_of_charge, on="bucket_start", how="left")
    return result


def line_figure(
    frame: pd.DataFrame,
    columns: list[str],
    *,
    y_title: str,
    height: int = 340,
    revision: str = "default",
    interval_seconds: int = 300,
) -> go.Figure:
    # Missing rows need explicit nulls: connectgaps=False alone only handles
    # null values already present in the series.
    if not frame.empty:
        frame = frame.sort_values("bucket_start").copy()
        timestamps = pd.to_datetime(frame["bucket_start"], utc=True)
        gaps = timestamps.diff().dt.total_seconds() > interval_seconds * 1.5
        if gaps.any():
            missing = frame.loc[gaps].copy()
            missing["bucket_start"] = frame["bucket_start"].shift(1).loc[
                gaps
            ] + pd.Timedelta(seconds=interval_seconds)
            for column in columns:
                if column in missing:
                    missing[column] = float("nan")
            frame = pd.concat([frame, missing]).sort_values("bucket_start")
    figure = go.Figure()
    for column in columns:
        if column not in frame or frame[column].isna().all():
            continue
        figure.add_trace(
            go.Scatter(
                x=frame["bucket_start"],
                y=frame[column],
                mode="lines",
                name=LABELS[column],
                uid=column,
                connectgaps=False,
                line={"color": COLORS[column], "width": 2},
                hovertemplate="%{y:,.0f}<extra>%{fullData.name}</extra>",
            )
        )
    figure.update_layout(
        height=height,
        margin={"l": 10, "r": 10, "t": 20, "b": 10},
        hovermode="x unified",
        legend={"orientation": "h", "yanchor": "bottom", "y": 1.02},
        font={"color": "#17231c", "family": "system-ui, sans-serif"},
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        xaxis_title=None,
        yaxis_title=y_title,
        uirevision=revision,
        legend_uirevision=revision,
    )
    figure.update_xaxes(gridcolor="#eef3f0", linecolor="#dce5df")
    figure.update_yaxes(
        gridcolor="#eef3f0",
        linecolor="#dce5df",
        zeroline=True,
        zerolinecolor="#c7d2cb",
    )
    return figure
