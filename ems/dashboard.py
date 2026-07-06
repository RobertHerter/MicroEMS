"""Dashboard: sauberes 3-Panel-Layout (Plotly make_subplots).

  Panel 1: Leistung (W, links) + Ladezustand (%, rechts) – Ist durchgezogen,
           Prognose gestrichelt (gleiche Farbe).
  Panel 2: Strompreis (ct/kWh) – Börsenpreis durchgezogen, Schätzung gestrichelt.
  Panel 3: Steuerung (W) – Lade-/Entladebefehle, Ist-Akkuleistung, Limits.

Aktive EMS-Eingriffe (mode != auto) sind über alle Panels farblich hinterlegt,
die aktuelle Uhrzeit als blaue Linie markiert.
"""
from __future__ import annotations

import logging
import os

import pandas as pd

from .config import Config

log = logging.getLogger("ems.dashboard")

_MODE_FILL = {"hold": "rgba(255,140,0,0.14)", "grid_charge": "rgba(31,119,180,0.14)",
              "grid_discharge": "rgba(148,0,211,0.14)", "block_charge": "rgba(214,39,40,0.12)",
              "limit": "rgba(255,205,0,0.18)"}
_MODE_LABEL = {"hold": "Eingriff: Entladen gesperrt", "grid_charge": "Eingriff: Netzladen",
               "grid_discharge": "Eingriff: Netz-Entladen", "block_charge": "Eingriff: Laden gesperrt",
               "limit": "Eingriff: gedrosselt"}
_MODE_LEG = {"hold": "rgba(255,140,0,0.7)", "grid_charge": "rgba(31,119,180,0.7)",
             "grid_discharge": "rgba(148,0,211,0.7)", "block_charge": "rgba(214,39,40,0.6)",
             "limit": "rgba(255,205,0,0.9)"}


def build_dashboard(config: Config, table: pd.DataFrame, total_cost_ct: float,
                    export_line_w=None) -> str:
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots

    t = table
    x = t.index
    has_car = "car_soc_percent" in t.columns
    has_mode = "mode" in t.columns

    fig = make_subplots(
        rows=3, cols=1, shared_xaxes=True, vertical_spacing=0.055,
        row_heights=[0.46, 0.20, 0.34],
        specs=[[{"secondary_y": True}], [{"secondary_y": False}], [{"secondary_y": False}]],
        subplot_titles=("<b>Leistung &amp; Ladezustand</b>", "<b>Strompreis</b>",
                        "<b>Steuerung</b>"),
    )

    def line(col, name, color, dash=None, width=2, row=1, sec=False):
        if col in t.columns:
            fig.add_trace(go.Scatter(x=x, y=t[col], name=name, mode="lines",
                                     line=dict(color=color, width=width, dash=dash)),
                          row=row, col=1, secondary_y=sec)

    # ---------- Panel 1: Leistung (links) ----------
    line("pv_w", "PV (Prognose)", "#ff7f0e", dash="dash")
    line("actual_pv_w", "PV (Ist)", "#ff7f0e")
    line("house_load_w", "Verbrauch (Prognose)", "#d62728", dash="dash")
    line("actual_load_w", "Verbrauch (Ist)", "#d62728")
    # Netz netto (Prognose = Bezug - Einspeisung) als eine Linie
    if "grid_import_w" in t.columns and "grid_export_w" in t.columns:
        net = t["grid_import_w"].fillna(0) - t["grid_export_w"].fillna(0)
        net = net.where(t["grid_import_w"].notna() | t["grid_export_w"].notna())
        fig.add_trace(go.Scatter(x=x, y=net, name="Netz (Prognose)", mode="lines",
                                 line=dict(color="#1f77b4", width=1.5, dash="dot")), row=1, col=1)
    line("actual_grid_w", "Netz (Ist)", "#1f77b4", width=1.8)
    # ---------- Panel 1: SoC (rechts) ----------
    line("house_soc_percent", "SoC (Prognose)", "#111111", dash="dash", width=2.5, sec=True)
    line("actual_soc_percent", "SoC (Ist)", "#111111", width=3, sec=True)
    if has_car:
        line("car_soc_percent", "Auto SoC", "#9467bd", dash="dot", sec=True)

    # ---------- Panel 2: Strompreis (Ist / Schätzung) ----------
    if "price_ct_kwh" in t.columns:
        price = t["price_ct_kwh"]
        if "price_estimated" in t.columns:
            est = t["price_estimated"].fillna(0) > 0.5
            fig.add_trace(go.Scatter(x=x, y=price.mask(est), name="Strompreis", mode="lines",
                                     line=dict(color="#8c564b", width=2, shape="hv")), row=2, col=1)
            fig.add_trace(go.Scatter(x=x, y=price.where(est | est.shift(-1, fill_value=False)),
                                     name="Strompreis (Schätzung)", mode="lines",
                                     line=dict(color="#8c564b", width=2, shape="hv", dash="dash")),
                          row=2, col=1)
        else:
            fig.add_trace(go.Scatter(x=x, y=price, name="Strompreis", mode="lines",
                                     line=dict(color="#8c564b", width=2, shape="hv")), row=2, col=1)

    # ---------- Panel 3: Steuerung ----------
    def bar(col, name, color, sign=1):
        if col in t.columns:
            fig.add_trace(go.Bar(x=x, y=sign * t[col], name=name, marker_color=color),
                          row=3, col=1)
    bar("batt_dc_charge_w", "Akku DC-Laden (PV)", "#2ca02c")
    bar("batt_ac_charge_w", "Akku Netzladen", "#1f77b4")
    bar("batt_discharge_w", "Akku Entladen", "#d62728", sign=-1)
    bar("batt_grid_discharge_w", "Akku Netz-Entladen", "#9400d3", sign=-1)
    if has_car:
        bar("car_charge_w", "Auto-Laden", "#9467bd")
    if "actual_battery_w" in t.columns:
        fig.add_trace(go.Scatter(x=x, y=t["actual_battery_w"], name="Akku-Leistung (Ist)",
                                 mode="lines", line=dict(color="#111111", width=1.8)), row=3, col=1)

    # ---------- Eingriffe als umschaltbare Bänder (per Legende ausblendbar) ----
    # Als gefüllte Traces (nicht als Shapes), damit sie wie die Kurven per Klick
    # ein-/ausgeblendet werden können. Pro Modus je ein Band in Panel 1 (SoC-Achse,
    # 0..100 %) und Panel 3 (Steuerung), über legendgroup gekoppelt.
    slotw = (x[1] - x[0]) if len(x) > 1 else pd.Timedelta(hours=1)
    if has_mode:
        modes = list(t["mode"].values)
        segs = {}
        i = 0
        while i < len(modes):
            m = modes[i]
            if m and m != "auto":
                j = i
                while j + 1 < len(modes) and modes[j + 1] == m:
                    j += 1
                segs.setdefault(m, []).append((x[i], x[j] + slotw))
                i = j + 1
            else:
                i += 1
        ctrl_cols = ["batt_dc_charge_w", "batt_ac_charge_w", "batt_discharge_w",
                     "batt_grid_discharge_w", "car_charge_w"]
        cmax = max([float(t[c].abs().max()) for c in ctrl_cols if c in t.columns
                    and pd.notna(t[c].abs().max())] + [1.0])

        def band(segments, y0, y1, color, name, group, show, row, sec=False):
            xs, ys = [], []
            for a, b in segments:
                xs += [a, a, b, b, None]
                ys += [y0, y1, y1, y0, None]
            fig.add_trace(go.Scatter(x=xs, y=ys, fill="toself", fillcolor=color,
                          line=dict(width=0), mode="lines", name=name, legendgroup=group,
                          showlegend=show, hoverinfo="skip"), row=row, col=1, secondary_y=sec)

        for m, segments in segs.items():
            col = _MODE_FILL.get(m, "rgba(120,120,120,0.12)")
            lbl = _MODE_LABEL.get(m, m)
            band(segments, 0, 101, col, lbl, m, True, row=1, sec=True)     # Panel 1 (SoC)
            band(segments, -cmax, cmax, col, lbl, m, False, row=3)         # Panel 3 (Steuerung)

    now = pd.Timestamp.now(tz=x.tz)
    fig.add_vline(x=now, line=dict(color="#0d6efd", width=2), row="all", col=1)
    fig.add_annotation(x=now, y=1.0, xref="x", yref="paper", yanchor="bottom",
                       text=f"● Jetzt {now.strftime('%H:%M')}", showarrow=False,
                       font=dict(color="#0d6efd", size=12), bgcolor="rgba(255,255,255,0.7)")

    # Einspeise-Linie (Peak-Modus): pro Tag ein Wert -> als Treppenlinie zeichnen.
    if "export_line_w" in t.columns and t["export_line_w"].notna().any():
        fig.add_trace(go.Scatter(x=x, y=t["export_line_w"], name="Einspeise-Linie",
                                 mode="lines", line=dict(color="#2ca02c", width=1.5,
                                 dash="dash", shape="hv")), row=1, col=1)
    elif export_line_w is not None and export_line_w > 0:
        fig.add_hline(y=float(export_line_w), row=1, col=1,
                      line=dict(color="#2ca02c", width=1.5, dash="dash"),
                      annotation_text=f"Einspeise-Linie {export_line_w:.0f} W",
                      annotation_position="top left",
                      annotation_font=dict(color="#2ca02c", size=11))

    # Aktuelle Sollwerte der drei Akku-Steuergrößen gut lesbar (Slot ab jetzt)
    try:
        pos = t.index.get_indexer([now], method="bfill")[0]
        if pos < 0:
            pos = len(t.index) - 1
        r = t.iloc[pos]

        def _g(c):
            return float(r[c]) if (c in t.columns and pd.notna(r[c])) else 0.0
        mode_now = str(r["mode"]) if "mode" in t.columns else ""
        txt = (f"<b>Sollwerte jetzt</b>  ·  DC-Laden (PV): {_g('batt_dc_charge_w'):.0f} W"
               f"  |  Entladen: {_g('batt_discharge_w'):.0f} W"
               f"  |  Netzladen (AC): {_g('batt_ac_charge_w'):.0f} W")
        gd = _g("batt_grid_discharge_w")
        if gd > 1:
            txt += f"  |  Netz-Entladen: {gd:.0f} W"
        if mode_now:
            txt += f"  |  Modus: {mode_now}"
        fig.add_annotation(xref="paper", yref="paper", x=0.0, y=1.05, xanchor="left",
                           yanchor="bottom", showarrow=False, text=txt,
                           font=dict(size=12, color="#222"),
                           bgcolor="rgba(235,235,235,0.95)", bordercolor="#bbb", borderwidth=1)
    except Exception:  # pragma: no cover
        pass

    n_eingriffe = int((t["mode"] != "auto").sum()) if has_mode else 0
    fig.update_yaxes(title_text="Leistung (W)", row=1, col=1, secondary_y=False)
    fig.update_yaxes(title_text="SoC (%)", range=[0, 101], row=1, col=1, secondary_y=True,
                     showgrid=False)
    fig.update_yaxes(title_text="ct/kWh", row=2, col=1)
    fig.update_yaxes(title_text="W", row=3, col=1)
    fig.update_layout(
        height=880, template="plotly_white", hovermode="x unified", barmode="relative",
        title=dict(text=(f"<b>EMS – Ist vs. Prognose & Steuerung</b>  ·  Netto-Kosten Horizont "
                         f"{total_cost_ct/100:.2f} €  ·  {n_eingriffe} Eingriffe  ·  "
                         f"{now.strftime('%Y-%m-%d %H:%M')}"),
                   x=0.5, xanchor="center", font=dict(size=22)),
        legend=dict(orientation="h", yanchor="top", y=-0.08, xanchor="left", x=0, font=dict(size=10)),
        margin=dict(l=60, r=30, t=120, b=110),
        bargap=0,
    )
    # Auto-Reload: pollt /version (mtime der HTML-Datei) und lädt die Seite nur
    # neu, wenn eine neue Berechnung die Datei geschrieben hat.
    reload_js = (
        "(function(){var base=null;function chk(){"
        "fetch('version?_='+Date.now(),{cache:'no-store'})"
        ".then(function(r){return r.ok?r.text():null;})"
        ".then(function(v){if(v===null)return;"
        "if(base===null){base=v;}else if(v!==base){location.reload();}})"
        ".catch(function(){});}"
        "chk();setInterval(chk,30000);})();"
    )
    out = config.dashboard.output_path
    # Atomar schreiben (Temp-Datei + os.replace): der Dashboard-Server könnte
    # sonst eine halb geschriebene Datei ausliefern bzw. das /version-Polling
    # ein Reload mitten im Schreibvorgang auslösen.
    # include_plotlyjs="directory": plotly.min.js liegt lokal neben der HTML
    # (wird vom Dashboard-Server mit ausgeliefert) -> funktioniert ohne Internet.
    tmp = out + ".tmp"
    fig.write_html(tmp, include_plotlyjs="directory", post_script=reload_js)
    os.replace(tmp, out)
    log.info("Dashboard geschrieben: %s (%d Eingriffe)", out, n_eingriffe)
    return out
