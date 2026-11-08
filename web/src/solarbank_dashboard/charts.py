from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go

from solarbank_dashboard.assets import design_tokens

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



def apply_chart_theme(figure: go.Figure) -> go.Figure:
    """Use the same packaged type and neutral colors as the surrounding UI."""
    tokens = design_tokens()
    figure.update_layout(
        font={"color": tokens["text"], "family": tokens["font-body-name"]},
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        hoverlabel={"bgcolor": tokens["surface"], "bordercolor": tokens["line"]},
    )
    figure.update_xaxes(gridcolor=tokens["soft"], linecolor=tokens["line"])
    figure.update_yaxes(gridcolor=tokens["soft"], linecolor=tokens["line"],
                       zeroline=True, zerolinecolor=tokens["line"])
    return figure

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
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        xaxis_title=None,
        yaxis_title=y_title,
        uirevision=revision,
        legend_uirevision=revision,
    )
    return apply_chart_theme(figure)
