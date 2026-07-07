"""Dashboard: KPI-Kacheln + 4 Panels + Modus-Zeitleiste (Plotly).

  KPI-Zeile:  Netto-Kosten, Ersparnis, Akku-SoC, Modus jetzt, Eingriffe.
  Panel 1: Leistung (W) - PV (mit p10-p90-Band), Verbrauch, Netz,
           Einspeise-Linie. Ist durchgezogen, Prognose gestrichelt.
  Panel 2: Ladezustand (%) - Haus + Auto (eigenes Panel, keine Doppelachse).
  Panel 3: Strompreis (ct/kWh) + Einspeisevergütung.
  Panel 4: Steuerung (W) - Lade-/Entladebefehle, Abregelung, Ist-Akkuleistung.
  Panel 5: Modus-Zeitleiste - Eingriffe als schmaler Farbstreifen mit Hover.

Orientierung: Vergangenheit grau hinterlegt, Tagesgrenzen mit Wochentag,
aktuelle Uhrzeit als blaue Linie. Legende gruppiert (Ist/Prognose/SoC/Steuerung).
"""
from __future__ import annotations

import logging
import os

import pandas as pd

from .config import Config

log = logging.getLogger("ems.dashboard")

_MODES = ["auto", "peak", "limit_charge", "limit_discharge", "hold",
          "block_charge", "grid_charge", "grid_discharge"]
_MODE_LABEL = {"auto": "auto (kein Eingriff)",
               "peak": "Peak-Laden (Linie)",
               "limit_charge": "Laden gedrosselt",
               "limit_discharge": "Entladen gedrosselt",
               "hold": "Entladen gesperrt", "block_charge": "Laden gesperrt",
               "grid_charge": "Netzladen", "grid_discharge": "Netz-Entladen"}
_MODE_COLOR = {"auto": "#f0f0f0", "peak": "#74c476",
               "limit_charge": "#ffd92f", "limit_discharge": "#e377c2",
               "hold": "#ff8c00", "block_charge": "#d62728",
               "grid_charge": "#1f77b4", "grid_discharge": "#9400d3"}
# Legenden-Swatch: auto wäre auf Weiß unsichtbar
_MODE_SWATCH = dict(_MODE_COLOR, auto="#c8c8c8")
_GROUPS = {"ist": "Ist", "prog": "Prognose", "soc": "Ladezustand",
           "ctrl": "Steuerung"}
_WD = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]

# Auto-Reload: pollt /version (mtime der HTML-Datei) und lädt die Seite nur
# neu, wenn eine neue Berechnung die Datei geschrieben hat.
_RELOAD_JS = (
    "(function(){var base=null;function chk(){"
    "fetch('version?_='+Date.now(),{cache:'no-store'})"
    ".then(function(r){return r.ok?r.text():null;})"
    ".then(function(v){if(v===null)return;"
    "if(base===null){base=v;}else if(v!==base){location.reload();}})"
    ".catch(function(){});}"
    "chk();setInterval(chk,30000);})();"
)


def _tile(label: str, value: str, sub: str = "") -> str:
    return (f'<div class="tile"><div class="v">{value}</div>'
            f'<div class="l">{label}</div><div class="s">{sub}</div></div>')


def _ensure_plotlyjs(out_path: str) -> None:
    """Legt plotly.min.js neben die HTML (einmalig) -> läuft ohne Internet."""
    bundle = os.path.join(os.path.dirname(os.path.abspath(out_path)) or ".",
                          "plotly.min.js")
    if not os.path.exists(bundle):
        from plotly.offline import get_plotlyjs
        with open(bundle, "w", encoding="utf-8") as fh:
            fh.write(get_plotlyjs())


def build_dashboard(config: Config, table: pd.DataFrame, total_cost_ct: float,
                    export_line_w=None, savings_eur=None) -> str:
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots

    t = table
    x = t.index
    now = pd.Timestamp.now(tz=x.tz)

    fig = make_subplots(
        rows=5, cols=1, shared_xaxes=True, vertical_spacing=0.035,
        row_heights=[0.36, 0.15, 0.15, 0.26, 0.045],
        subplot_titles=("<b>Leistung</b>", "<b>Ladezustand</b>",
                        "<b>Strompreis</b>", "<b>Steuerung</b>", ""),
    )

    # Hover: deutsche Zahlen (1.234,5 via layout.separators) + Einheit.
    HOVER_W = "%{y:,.0f} W"
    HOVER_PCT = "%{y:.0f} %"
    HOVER_CT = "%{y:.1f} ct/kWh"

    def line(col, name, color, row, group, dash=None, width=2, shape=None,
             hover=HOVER_W):
        if col in t.columns and t[col].notna().any():
            fig.add_trace(go.Scatter(
                x=x, y=t[col], name=name, mode="lines",
                line=dict(color=color, width=width, dash=dash,
                          shape=shape or "linear"),
                hovertemplate=hover,
                legendgroup=group, legendgrouptitle_text=_GROUPS[group]),
                row=row, col=1)

    # ---------- Panel 1: Leistung ----------
    # Reihenfolge = Reihenfolge im Unified-Hover: je Signal Ist vor Prognose.
    if {"pv10_w", "pv90_w"} <= set(t.columns) and t["pv10_w"].notna().any():
        fig.add_trace(go.Scatter(x=x, y=t["pv90_w"], mode="lines",
                                 line=dict(width=0), legendgroup="prog",
                                 showlegend=False, hoverinfo="skip"),
                      row=1, col=1)
        fig.add_trace(go.Scatter(x=x, y=t["pv10_w"], mode="lines",
                                 line=dict(width=0), fill="tonexty",
                                 fillcolor="rgba(255,127,14,0.14)",
                                 name="PV p10–p90", legendgroup="prog",
                                 hoverinfo="skip"), row=1, col=1)
    line("actual_pv_w", "PV (Ist)", "#ff7f0e", 1, "ist")
    line("pv_w", "PV (Prognose)", "#ff7f0e", 1, "prog", dash="dash")
    line("actual_load_w", "Verbrauch (Ist)", "#d62728", 1, "ist")
    line("house_load_w", "Verbrauch (Prognose)", "#d62728", 1, "prog", dash="dash")
    line("actual_grid_w", "Netz (Ist)", "#1f77b4", 1, "ist", width=1.8)
    if "grid_import_w" in t.columns and "grid_export_w" in t.columns:
        net = t["grid_import_w"].fillna(0) - t["grid_export_w"].fillna(0)
        net = net.where(t["grid_import_w"].notna() | t["grid_export_w"].notna())
        fig.add_trace(go.Scatter(x=x, y=net, name="Netz (Prognose)", mode="lines",
                                 line=dict(color="#1f77b4", width=1.5, dash="dot"),
                                 hovertemplate=HOVER_W,
                                 legendgroup="prog"), row=1, col=1)
    if "export_line_w" in t.columns and t["export_line_w"].notna().any():
        line("export_line_w", "Einspeise-Linie", "#2ca02c", 1, "prog",
             dash="dash", width=1.5, shape="hv")
    elif export_line_w is not None and export_line_w > 0:
        fig.add_hline(y=float(export_line_w), row=1, col=1,
                      line=dict(color="#2ca02c", width=1.5, dash="dash"),
                      annotation_text=f"Einspeise-Linie {export_line_w:.0f} W",
                      annotation_position="top left",
                      annotation_font=dict(color="#2ca02c", size=11))

    # ---------- Panel 2: SoC (eigenes Panel, keine Doppelachse) ----------
    line("actual_soc_percent", "Haus-SoC (Ist)", "#111111", 2, "soc", width=3,
         hover=HOVER_PCT)
    line("house_soc_percent", "Haus-SoC (Prognose)", "#111111", 2, "soc",
         dash="dash", width=2.5, hover=HOVER_PCT)
    line("car_soc_percent", "Auto-SoC", "#9467bd", 2, "soc", dash="dot",
         hover=HOVER_PCT)

    # ---------- Panel 3: Preis + Vergütung ----------
    if "price_ct_kwh" in t.columns:
        price = t["price_ct_kwh"]
        est = (t["price_estimated"].fillna(0) > 0.5) \
            if "price_estimated" in t.columns else pd.Series(False, index=x)
        fig.add_trace(go.Scatter(x=x, y=price.mask(est), name="Börsenpreis",
                                 mode="lines", legendgroup="prog",
                                 hovertemplate=HOVER_CT,
                                 line=dict(color="#8c564b", width=2, shape="hv")),
                      row=3, col=1)
        if est.any():
            fig.add_trace(go.Scatter(
                x=x, y=price.where(est | est.shift(-1, fill_value=False)),
                name="Preis (Schätzung)", mode="lines", legendgroup="prog",
                hovertemplate=HOVER_CT,
                line=dict(color="#8c564b", width=2, shape="hv", dash="dash")),
                row=3, col=1)
    line("feedin_ct_kwh", "Einspeisevergütung", "#2ca02c", 3, "prog",
         width=1.2, shape="hv", hover=HOVER_CT)

    # ---------- Panel 4: Steuerung ----------
    def bar(col, name, color, sign=1):
        if col in t.columns and t[col].abs().max() > 1:
            fig.add_trace(go.Bar(x=x, y=sign * t[col], name=name,
                                 marker_color=color, hovertemplate=HOVER_W,
                                 legendgroup="ctrl",
                                 legendgrouptitle_text=_GROUPS["ctrl"]),
                          row=4, col=1)

    bar("batt_dc_charge_w", "Akku Laden (PV)", "#2ca02c")
    bar("batt_ac_charge_w", "Akku Netzladen", "#1f77b4")
    bar("batt_discharge_w", "Akku Entladen", "#d62728", sign=-1)
    bar("batt_grid_discharge_w", "Akku Netz-Entladen", "#9400d3", sign=-1)
    bar("car_charge_w", "Auto-Laden", "#9467bd")
    bar("pv_curtail_w", "PV-Abregelung", "#7f7f7f", sign=-1)
    if "actual_battery_w" in t.columns:
        fig.add_trace(go.Scatter(x=x, y=t["actual_battery_w"],
                                 name="Akku-Leistung (Ist)", mode="lines",
                                 line=dict(color="#111111", width=1.8),
                                 hovertemplate=HOVER_W,
                                 legendgroup="ctrl"), row=4, col=1)

    # ---------- Panel 5: Modus-Zeitleiste ----------
    n_eingriffe = 0
    if "mode" in t.columns:
        modes = t["mode"].fillna("auto")
        # "peak" ist geformtes Normalverhalten, kein Eingriff
        n_eingriffe = int((~modes.isin(["auto", "peak"])).sum())
        z = [[_MODES.index(m) if m in _MODES else 0 for m in modes]]
        colorscale = []
        for i, m in enumerate(_MODES):
            colorscale += [[i / len(_MODES), _MODE_COLOR[m]],
                           [(i + 1) / len(_MODES), _MODE_COLOR[m]]]
        fig.add_trace(go.Heatmap(
            x=x, y=[""], z=z, zmin=-0.5, zmax=len(_MODES) - 0.5,
            colorscale=colorscale, showscale=False,
            customdata=[[_MODE_LABEL.get(m, m) for m in modes]],
            hovertemplate="%{x|%H:%M} – %{customdata}<extra>Modus</extra>"),
            row=5, col=1)

    # ---------- Orientierung: Vergangenheit, Jetzt, Tagesgrenzen ----------
    if x[0] < now:
        fig.add_vrect(x0=x[0], x1=min(now, x[-1]), fillcolor="rgba(0,0,0,0.05)",
                      line_width=0, layer="below", row="all", col=1)
    fig.add_vline(x=now, line=dict(color="#0d6efd", width=2), row="all", col=1)
    # "Jetzt"-Label INNERHALB von Panel 1 (oben), damit es weder die
    # Panel-Titel noch die Datums-Zeile überlappt.
    fig.add_annotation(x=now, y=1.0, xref="x", yref="paper", yanchor="top",
                       text=f"● Jetzt {now.strftime('%H:%M')}", showarrow=False,
                       font=dict(color="#0d6efd", size=12),
                       bgcolor="rgba(255,255,255,0.8)")
    # Datums-Zeile OBERHALB der Panel-Titel (eigene Ebene, kein Überlappen)
    day = x[0].normalize()
    while day <= x[-1]:
        if day > x[0]:
            fig.add_vline(x=day, line=dict(color="#bbbbbb", width=1, dash="dot"),
                          row="all", col=1)
        fig.add_annotation(x=day + pd.Timedelta(hours=12), y=1.06, xref="x",
                           yref="paper", showarrow=False,
                           text=f"<b>{_WD[day.weekday()]} "
                                f"{day.strftime('%d.%m.')}</b>",
                           font=dict(size=12, color="#666"))
        day += pd.Timedelta(days=1)

    fig.update_yaxes(title_text="W", row=1, col=1)
    fig.update_yaxes(title_text="%", range=[0, 101], row=2, col=1)
    fig.update_yaxes(title_text="ct/kWh", row=3, col=1)
    fig.update_yaxes(title_text="W", row=4, col=1)
    fig.update_yaxes(visible=False, row=5, col=1)

    # Mini-Legende der Modus-Farben DIREKT unter der Zeitleiste (Annotation,
    # unterhalb der Zeit-Beschriftung; die Trace-Legende rückt weiter nach unten)
    mode_leg = "<b>Modus:</b>  " + "   ".join(
        f"<span style='color:{_MODE_SWATCH[m]}'>■</span> "
        f"{_MODE_LABEL[m].replace(' (kein Eingriff)', '')}"
        for m in _MODES)
    fig.add_annotation(xref="paper", yref="paper", x=0, y=-0.035,
                       xanchor="left", yanchor="top", showarrow=False,
                       text=mode_leg, font=dict(size=11, color="#555"))
    fig.update_layout(
        height=980, autosize=True, template="plotly_white",
        hovermode="x unified", barmode="relative", bargap=0,
        # Deutsche Zahlenformate in Hover/Achsen: Dezimal-Komma, Tausender-Punkt
        separators=",.",
        margin=dict(l=60, r=30, t=80, b=130),
        legend=dict(orientation="h", yanchor="top", y=-0.075, xanchor="left",
                    x=0, font=dict(size=11), groupclick="toggleitem"),
    )

    # ---------- KPI-Kacheln ----------
    pos = t.index.get_indexer([now], method="bfill")[0]
    row_now = t.iloc[pos if pos >= 0 else -1]
    soc_now = row_now.get("actual_soc_percent")
    if pd.isna(soc_now):
        soc_now = row_now.get("house_soc_percent", float("nan"))
    mode_now = str(row_now.get("mode", "auto"))
    ch_lim = row_now.get("batt_charge_limit_w", float("nan"))
    dis_lim = row_now.get("batt_discharge_limit_w", float("nan"))
    tiles = [
        _tile("Netto-Kosten Horizont", f"{total_cost_ct / 100:.2f} €",
              f"bis {_WD[x[-1].weekday()]} {x[-1].strftime('%d.%m.')}"),
        _tile("Ersparnis gesamt",
              "–" if savings_eur is None else f"{savings_eur:.2f} €",
              "vs. ohne EMS"),
        _tile("Akku-SoC",
              "–" if pd.isna(soc_now) else f"{soc_now:.0f} %",
              f"{config.house_battery.capacity_wh / 1000:.0f} kWh Speicher"),
        _tile("Modus jetzt", _MODE_LABEL.get(mode_now, mode_now),
              "" if pd.isna(dis_lim) else
              f"Limits {ch_lim:.0f} / {dis_lim:.0f} W"),
        _tile("Eingriffe im Plan", f"{n_eingriffe}", "Slots ≠ auto"),
    ]

    plot_html = fig.to_html(full_html=False, include_plotlyjs=False,
                            default_width="100%",
                            config={"responsive": True, "displaylogo": False})
    html = f"""<!DOCTYPE html>
<html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>EMS Dashboard</title>
<script src="plotly.min.js"></script>
<style>
 body {{ font-family: -apple-system, 'Segoe UI', Roboto, sans-serif;
        margin: 12px; color: #222; background: #fff; }}
 h1 {{ font-size: 20px; margin: 4px 0 10px; }}
 h1 .ts {{ color: #888; font-weight: normal; font-size: 14px; }}
 .tiles {{ display: flex; gap: 10px; flex-wrap: wrap; margin-bottom: 10px; }}
 .tile {{ flex: 1 1 150px; background: #f6f7f9; border: 1px solid #e3e5e8;
         border-radius: 8px; padding: 10px 14px; }}
 .tile .v {{ font-size: 22px; font-weight: 700; }}
 .tile .l {{ font-size: 12px; color: #555; margin-top: 2px; }}
 .tile .s {{ font-size: 11px; color: #999; }}
</style></head><body>
<h1>EMS – Ist vs. Prognose &amp; Steuerung
 <span class="ts">{now.strftime('%Y-%m-%d %H:%M')}</span></h1>
<div class="tiles">{''.join(tiles)}</div>
{plot_html}
<script>{_RELOAD_JS}</script>
</body></html>"""

    # Atomar schreiben (Temp + os.replace): der Dashboard-Server könnte sonst
    # eine halb geschriebene Datei ausliefern. plotly.min.js liegt lokal
    # daneben -> funktioniert ohne Internet.
    out = config.dashboard.output_path
    _ensure_plotlyjs(out)
    tmp = out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(html)
    os.replace(tmp, out)
    log.info("Dashboard geschrieben: %s (%d Eingriffe)", out, n_eingriffe)
    return out
