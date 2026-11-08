"""The deployed app and its charts must share the pinned design artifact."""
import json

import pandas as pd
import pytest
from flask import Flask

from solarbank_dashboard import assets
from solarbank_dashboard.app import empty_figure
from solarbank_dashboard.charts import COLORS, line_figure


def test_chart_theme_uses_built_tokens_without_changing_series_colors():
    tokens = assets.design_tokens()
    figure = line_figure(pd.DataFrame({"bucket_start": ["2026-10-31T12:00:00Z"],
                                      "pv_total_w": [450.0]}), ["pv_total_w"], y_title="W")
    assert figure.layout.font.family == tokens["font-body-name"]
    assert figure.layout.font.color == tokens["text"]
    assert figure.layout.xaxis.gridcolor == tokens["soft"]
    assert figure.layout.yaxis.zerolinecolor == tokens["line"]
    assert figure.data[0].line.color == COLORS["pv_total_w"]
    empty = empty_figure()
    assert empty.layout.annotations[0].font.family == tokens["font-body-name"]
    assert empty.layout.annotations[0].font.color == tokens["muted"]


@pytest.mark.parametrize("failure", ["missing", "outside", "malformed", "non_object"])
def test_startup_rejects_missing_or_invalid_design_artifact(tmp_path, monkeypatch, failure):
    manifest = {}
    for key in ("public.js", "public.css", "dashboard.js", "dashboard.css"):
        (tmp_path / key).write_text("/* fixture */")
        manifest[key] = key
    manifest["design-tokens.json"] = "../outside.json" if failure == "outside" else "design-tokens.fixture.json"
    if failure in {"malformed", "non_object"}:
        (tmp_path / manifest["design-tokens.json"]).write_text('[]' if failure == "non_object" else '{"text": "#111111"}')
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    monkeypatch.setattr(assets, "DIST", tmp_path)
    with pytest.raises(RuntimeError, match="Frontend fehlt"):
        assets.install_assets(Flask(__name__))
