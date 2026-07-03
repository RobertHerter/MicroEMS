"""Grafische Aufbereitung: heutiger Tag + Vorhersage inkl. Steuerbefehle.

Selbst-enthaltenes interaktives HTML (Plotly) mit zwei gekoppelten Panels:

  Panel 1 (oben): Leistungen (W, links) + Haus-/Auto-SoC (%, rechts) +
                  Strompreis (ct/kWh, zweite rechte Achse) in EINER Grafik.
  Panel 2 (unten): Akku-Leistung (Balken) + Steuerbefehl-Limits (Linien).

Aktive EMS-Eingriffe (mode != "auto") werden über beide Panels farblich
hinterlegt (Entladen gesperrt / Netzladen / Laden gesperrt / gedrosselt).
"""
from __future__ import annotations

import logging

import pandas as pd

from .config import Config

log = logging.getLogger("ems.dashboard")

# Farben für die Hervorhebung aktiver Eingriffe
_MODE_FILL = {
    "hold": "rgba(255,140,0,0.16)",
    "grid_charge": "rgba(31,119,180,0.16)",
    "grid_discharge": "rgba(148,0,211,0.16)",
    "block_charge": "rgba(214,39,40,0.13)",
    "limit": "rgba(255,205,0,0.20)",
}
_MODE_LABEL = {
    "hold": "Eingriff: Entladen gesperrt",
    "grid_charge": "Eingriff: Netzladen",
    "grid_discharge": "Eingriff: Netz-Entladen",
    "block_charge": "Eingriff: Laden gesperrt",
    "limit": "Eingriff: gedrosselt",
}
_MODE_LEGEND_COLOR = {
    "hold": "rgba(255,140,0,0.6)",
    "grid_charge": "rgba(31,119,180,0.6)",
    "grid_discharge": "rgba(148,0,211,0.6)",
    "block_charge": "rgba(214,39,40,0.5)",
    "limit": "rgba(255,205,0,0.8)",
}


def build_dashboard(config: Config, table: pd.DataFrame, total_cost_ct: float) -> str:
    """Baut das HTML-Dashboard, speichert es und gibt den Pfad zurück."""
    import plotly.graph_objects as go

    has_car = "car_soc_percent" in table.columns
    has_mode = "mode" in table.columns
    x = table.index
    fig = go.Figure()

    # ================= Panel 1: Leistungen + SoC + Preis ================= #
    fig.add_trace(go.Scatter(x=x, y=table["pv_w"], name="PV-Erzeugung", yaxis="y",
                             fill="tozeroy", line=dict(color="#ff7f0e")))
    fig.add_trace(go.Scatter(x=x, y=table["house_load_w"], name="Hausverbrauch",
                             yaxis="y", line=dict(color="#d62728")))
    fig.add_trace(go.Scatter(x=x, y=table["grid_import_w"], name="Netzbezug",
                             yaxis="y", line=dict(color="#1f77b4")))
    fig.add_trace(go.Scatter(x=x, y=-table["grid_export_w"], name="Einspeisung",
                             yaxis="y", line=dict(color="#2ca02c")))
    # SoC auf rechter Achse (y2)
    fig.add_trace(go.Scatter(x=x, y=table["house_soc_percent"], name="Haus-Akku SoC",
                             yaxis="y2", line=dict(color="#111111", width=3)))
    if has_car:
        fig.add_trace(go.Scatter(x=x, y=table["car_soc_percent"], name="Auto SoC",
                                 yaxis="y2", line=dict(color="#9467bd", width=2, dash="dot")))
    # Strompreis auf zweiter rechter Achse (y3)
    fig.add_trace(go.Scatter(x=x, y=table["price_ct_kwh"], name="Strompreis",
                             yaxis="y3", line=dict(color="#8c564b", width=2, dash="dash")))

    # ================= Panel 2: Steuerbefehle ================= #
    fig.add_trace(go.Bar(x=x, y=table["batt_dc_charge_w"], name="Akku DC-Laden (PV)",
                         marker_color="#2ca02c", yaxis="y4"))
    fig.add_trace(go.Bar(x=x, y=table["batt_ac_charge_w"], name="Akku Netzladen",
                         marker_color="#1f77b4", yaxis="y4"))
    fig.add_trace(go.Bar(x=x, y=-table["batt_discharge_w"], name="Akku Entladen (Eigenverbrauch)",
                         marker_color="#d62728", yaxis="y4"))
    if "batt_grid_discharge_w" in table.columns:
        fig.add_trace(go.Bar(x=x, y=-table["batt_grid_discharge_w"], name="Akku Netz-Entladen",
                             marker_color="#9400d3", yaxis="y4"))
    if has_car:
        fig.add_trace(go.Bar(x=x, y=table["car_charge_w"], name="Auto-Laden",
                             marker_color="#9467bd", yaxis="y4"))
    if "batt_charge_limit_w" in table.columns:
        fig.add_trace(go.Scatter(x=x, y=table["batt_charge_limit_w"], name="Ladelimit (Befehl)",
                                 yaxis="y4", mode="lines",
                                 line=dict(color="#2ca02c", width=1, dash="dot")))
        fig.add_trace(go.Scatter(x=x, y=-table["batt_discharge_limit_w"], name="Entladelimit (Befehl)",
                                 yaxis="y4", mode="lines",
                                 line=dict(color="#d62728", width=1, dash="dot")))

    # ================= Eingriffe hervorheben ================= #
    if has_mode:
        modes = list(table["mode"].values)
        slotw = (x[1] - x[0]) if len(x) > 1 else pd.Timedelta(hours=1)
        seen = set()
        i = 0
        while i < len(modes):
            m = modes[i]
            if m and m != "auto":
                j = i
                while j + 1 < len(modes) and modes[j + 1] == m:
                    j += 1
                fig.add_vrect(x0=x[i], x1=x[j] + slotw, layer="below", line_width=0,
                              fillcolor=_MODE_FILL.get(m, "rgba(120,120,120,0.15)"))
                seen.add(m)
                i = j + 1
            else:
                i += 1
        # Legenden-Platzhalter für die Eingriffsfarben
        for m in seen:
            fig.add_trace(go.Scatter(
                x=[x[0]], y=[None], mode="markers", name=_MODE_LABEL.get(m, m),
                marker=dict(size=11, symbol="square", color=_MODE_LEGEND_COLOR.get(m, "gray")),
                yaxis="y", hoverinfo="skip", showlegend=True))

    # "Jetzt"-Linie über beide Panels
    now = pd.Timestamp.now(tz=x.tz)
    fig.add_vline(x=now, line=dict(color="rgba(0,0,0,0.45)", dash="dot"))

    n_eingriffe = int((table["mode"] != "auto").sum()) if has_mode else 0
    fig.update_layout(
        height=940,
        barmode="relative",
        template="plotly_white",
        hovermode="x unified",
        title=dict(
            text=(f"EMS Steuertabelle & Vorhersage — Netto-Kosten Horizont: "
                  f"{total_cost_ct/100:.2f} €  ·  {n_eingriffe} aktive Eingriffe  ·  "
                  f"Stand {now.strftime('%Y-%m-%d %H:%M')}"),
            x=0.5, xanchor="center", y=0.985, yanchor="top",
        ),
        # Legende UNTEN, damit sie nicht in den Titel läuft
        legend=dict(orientation="h", yanchor="top", y=-0.14, xanchor="left", x=0,
                    font=dict(size=10)),
        margin=dict(l=65, r=120, t=70, b=150),
        # gemeinsame Zeitachse, rechts Platz für zwei zusätzliche Y-Achsen
        xaxis=dict(domain=[0.0, 0.86], anchor="y4"),
        yaxis=dict(title="Leistung (W)", domain=[0.40, 0.95], zeroline=True),
        yaxis2=dict(title="SoC (%)", domain=[0.40, 0.95], overlaying="y", side="right",
                    range=[0, 101], showgrid=False),
        yaxis3=dict(title="Preis (ct/kWh)", domain=[0.40, 0.95], overlaying="y",
                    side="right", anchor="free", position=0.93, showgrid=False),
        yaxis4=dict(title="Steuerung (W)", domain=[0.0, 0.28], zeroline=True),
    )

    out = config.dashboard.output_path
    fig.write_html(out, include_plotlyjs="cdn")
    log.info("Dashboard geschrieben: %s (%d Eingriffe)", out, n_eingriffe)
    return out
