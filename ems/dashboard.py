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


def _tile(label: str, value: str, sub: str = "", color: str = "") -> str:
    style = f' style="color:{color}"' if color else ""
    return (f'<div class="tile"><div class="v"{style}>{value}</div>'
            f'<div class="l">{label}</div><div class="s">{sub}</div></div>')


def _esc(s: str) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _alert_banner(violations) -> str:
    """HTML-Banner der Planprüfung (ems/validate). Grün wenn sauber, sonst
    rot (Fehler) bzw. gelb (nur Warnungen) mit den einzelnen Meldungen."""
    if violations is None:
        return ""
    errs = [v for v in violations if getattr(v, "severity", "") == "error"]
    warns = [v for v in violations if getattr(v, "severity", "") == "warning"]
    if not errs and not warns:
        return ('<div class="banner ok">✓ Planprüfung: keine Verstöße – '
                'alle Invarianten erfüllt.</div>')
    cls = "err" if errs else "warn"
    head = (f"✗ Planprüfung: {len(errs)} Fehler"
            + (f", {len(warns)} Warnungen" if warns else "")) if errs \
        else f"⚠ Planprüfung: {len(warns)} Warnungen"
    items = "".join(f"<li>{_esc(v)}</li>" for v in (errs + warns))
    return (f'<div class="banner {cls}"><b>{head}</b><ul>{items}</ul></div>')


def _ensure_plotlyjs(out_path: str) -> None:
    """Legt plotly.min.js neben die HTML (einmalig) -> läuft ohne Internet."""
    bundle = os.path.join(os.path.dirname(os.path.abspath(out_path)) or ".",
                          "plotly.min.js")
    if not os.path.exists(bundle):
        from plotly.offline import get_plotlyjs
        with open(bundle, "w", encoding="utf-8") as fh:
            fh.write(get_plotlyjs())


def _report_block(config: Config, now, violations) -> str:
    """Debug-Button (nur wenn report.enabled): lädt den Schnappschuss und
    öffnet das Mailprogramm vorausgefüllt. mailto kann keine Datei anhängen ->
    Nutzer hängt die geladene last_run_debug.json manuell an."""
    import urllib.parse
    if not getattr(config, "report", None) or not config.report.enabled:
        return ""
    errs = sum(1 for v in (violations or []) if getattr(v, "severity", "") == "error")
    warns = sum(1 for v in (violations or []) if getattr(v, "severity", "") == "warning")
    hot = "hot" if errs else ""
    subj = f"EMS Debug-Report {now.strftime('%Y-%m-%d %H:%M')} ({errs} Fehler, {warns} Warn.)"
    body = ("Auffälligkeit im EMS.\n\n"
            "Bitte die zuvor heruntergeladene Datei last_run_debug.json an "
            "diese Mail anhängen (sie enthält Eingaben + Plan zum Reproduzieren, "
            "keine Zugangsdaten).\n\nNotiz:\n")
    mailto = "mailto:" + urllib.parse.quote(config.report.mail_to) + "?" + \
        urllib.parse.urlencode({"subject": subj, "body": body})
    return (
        '<div class="report">'
        f'<button class="{hot}" onclick="emsReport()">'
        '✉ Debug-Daten herunterladen &amp; Mail öffnen</button>'
        '<span class="msg" id="rmsg">bei Implausibilität: JSON laden, dann im '
        'Mailprogramm anhängen</span></div>'
        '<script>function emsReport(){'
        "var a=document.createElement('a');a.href='report.json';"
        "a.download='last_run_debug.json';document.body.appendChild(a);a.click();"
        "a.remove();"
        "document.getElementById('rmsg').textContent="
        "'JSON geladen – Mailprogramm öffnet sich, Datei bitte anhängen.';"
        f"setTimeout(function(){{window.location.href={_js_str(mailto)};}},600);"
        '}</script>')


def _js_str(s: str) -> str:
    import json as _j
    return _j.dumps(s)


def _controls_block(config) -> str:
    """Interaktives Steuerpanel (nur bei dashboard.controls_enabled): Lasten
    an/aus + Kernparameter, Optimierungsmodus, manuelles Laden/Entladen.
    Sendet an /api/control/* (Basic-Auth wie das Dashboard)."""
    if not getattr(config.dashboard, "controls_enabled", False):
        return ""
    import json as _j
    from .loads import _slug as _lslug

    def _num(idp, val, step="1", width=64):
        return (f"<input type='number' step='{step}' id='{idp}' "
                f"value='{val:g}' style='width:{width}px'>")

    meta, rows = {}, []
    for ld in getattr(config, "controllable_loads", []):
        sg = _lslug(ld.name)
        if ld.type == "thermal":
            keys = ["target_c", "min_c", "max_c"]
            fields = (f"Ziel {_num(f'p_{sg}_target_c', ld.target_c, '0.1')}°C "
                      f"Min {_num(f'p_{sg}_min_c', ld.min_c, '0.1')}°C "
                      f"Max {_num(f'p_{sg}_max_c', ld.max_c, '0.1')}°C")
        else:
            keys = ["power_w", "runtime_minutes", "window_from_hour", "window_to_hour"]
            fields = (f"Leistung {_num(f'p_{sg}_power_w', ld.power_w)}W "
                      f"Laufzeit {_num(f'p_{sg}_runtime_minutes', ld.runtime_minutes)}min "
                      f"Fenster {_num(f'p_{sg}_window_from_hour', ld.window_from_hour)}"
                      f"–{_num(f'p_{sg}_window_to_hour', ld.window_to_hour)} h")
        meta[sg] = {"name": ld.name, "keys": keys}
        chk = "checked" if ld.enabled else ""
        rows.append(
            f"<div class='ctl-row'><label class='sw'>"
            f"<input type='checkbox' id='en_{sg}' {chk}> <b>{_esc(ld.name)}</b></label> "
            f"<span class='fld'>{fields}</span> "
            f"<button onclick=\"emsLoad('{sg}')\">Speichern</button></div>")

    strat = getattr(config.optimization, "charge_strategy", "auto")
    mode_btns = "".join(
        f"<button class='mode{' on' if strat == m else ''}' "
        f"onclick=\"emsMode('{m}')\">{m}</button>" for m in ("auto", "asap", "peak"))
    battery = (
        "<div class='ctl-row'><b>Akku manuell</b> "
        "<input type='number' id='bat_w' value='3000' style='width:72px'>W "
        "<input type='number' id='bat_min' value='30' style='width:56px'>min "
        "<button onclick=\"emsBat('charge')\">Laden</button> "
        "<button onclick=\"emsBat('discharge')\">Entladen</button> "
        "<button onclick=\"emsBat('auto')\">Auto/Stop</button></div>")

    js = """
const EMS_LOADS=%s;
async function emsPost(action,payload){
  const m=document.getElementById('ctl-msg'); m.textContent='… '+action;
  try{
    const r=await fetch('api/control/'+action,{method:'POST',
      headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)});
    if(!r.ok){ throw new Error((await r.text()).slice(0,200)); }
    m.textContent='✓ übernommen – Neuberechnung läuft …';
    setTimeout(()=>location.reload(), 2500);
  }catch(e){ m.textContent='✗ '+e.message; }
}
function emsLoad(sg){
  const L=EMS_LOADS[sg], p={};
  L.keys.forEach(k=>{const el=document.getElementById('p_'+sg+'_'+k);
    if(el&&el.value!=='') p[k]=parseFloat(el.value);});
  emsPost('load',{name:L.name, enabled:document.getElementById('en_'+sg).checked, params:p});
}
function emsMode(s){ emsPost('mode',{strategy:s}); }
function emsBat(a){ emsPost('battery',{action:a,
  watts:parseFloat(document.getElementById('bat_w').value),
  minutes:parseFloat(document.getElementById('bat_min').value)}); }
""" % _j.dumps(meta)

    return (
        "<div class='controls'>"
        "<div class='ctl-h'>Steuerung</div>"
        f"{''.join(rows)}"
        f"<div class='ctl-row'><b>Optimierungsmodus</b> {mode_btns}</div>"
        f"{battery}"
        "<div id='ctl-msg' class='ctl-msg'></div>"
        f"</div><script>{js}</script>")


def build_dashboard(config: Config, table: pd.DataFrame, total_cost_ct: float,
                    export_line_w=None, savings_eur=None, violations=None,
                    load_temp_actual=None) -> str:
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots

    t = table
    x = t.index
    now = pd.Timestamp.now(tz=x.tz)
    load_temp_actual = load_temp_actual or {}

    loads_cfg = list(getattr(config, "controllable_loads", []) or [])
    has_loads = len(loads_cfg) > 0
    thermal_cfg = [ld for ld in loads_cfg if ld.type == "thermal"]
    has_thermal = len(thermal_cfg) > 0
    if has_loads:
        titles = ["<b>Leistung</b>", "<b>Ladezustand</b>", "<b>Strompreis</b>",
                  "<b>Steuerung</b>", "", "<b>Steuerbare Lasten</b>"]
        row_heights = [0.33, 0.14, 0.14, 0.24, 0.045, 0.105]
    else:
        titles = ["<b>Leistung</b>", "<b>Ladezustand</b>", "<b>Strompreis</b>",
                  "<b>Steuerung</b>", ""]
        row_heights = [0.36, 0.15, 0.15, 0.26, 0.045]
    temp_row = None
    if has_thermal:
        temp_row = len(titles) + 1
        titles.append("<b>Pool-Temperatur</b>")
        row_heights.append(0.12)
    n_rows = len(titles)
    fig = make_subplots(
        rows=n_rows, cols=1, shared_xaxes=True, vertical_spacing=0.035,
        row_heights=row_heights, subplot_titles=titles,
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
    # Steuerbare Lasten (Pool etc.): geplante Gesamt-Leistung als eigener Verlauf.
    if has_loads:
        _cl_cols = [c for c in t.columns if c.startswith("load_") and c.endswith("_w")]
        if _cl_cols:
            cl_sum = t[_cl_cols].sum(axis=1)
            if float(cl_sum.abs().sum()) > 0:
                fig.add_trace(go.Scatter(
                    x=x, y=cl_sum, name="Steuerb. Lasten (Prognose)", mode="lines",
                    line=dict(color="#9467bd", width=1.6, dash="dot"),
                    hovertemplate=HOVER_W, legendgroup="prog",
                    legendgrouptitle_text=_GROUPS["prog"]), row=1, col=1)
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

    # ---------- Panel 6: Steuerbare Lasten (on/off je Slot) ----------
    if has_loads:
        from .loads import _slug as _lslug
        lanes = []   # (label, column, enabled)
        for ld in loads_cfg:
            if ld.type == "thermal":
                sg = _lslug(ld.name)
                for st in ld.stages:
                    lanes.append((f"{ld.name} / {st.name}",
                                  f"load_{sg}_{_lslug(st.name)}_w", ld.enabled))
            else:
                lanes.append((ld.name, f"load_{_lslug(ld.name)}_w", ld.enabled))
        ylabels, z = [], []
        for label, col, enabled in lanes:
            ylabels.append(label)
            if enabled and col in t.columns:
                z.append([1 if float(v) > 5.0 else 0 for v in t[col].fillna(0.0)])
            else:                                   # deaktiviert -> graue Leiste
                z.append([2] * len(x))
        _lab = {0: "aus", 1: "AN", 2: "deaktiviert"}
        fig.add_trace(go.Heatmap(
            x=x, y=ylabels, z=z, zmin=-0.5, zmax=2.5, showscale=False,
            colorscale=[[0.0, "#e9ecef"], [0.33, "#e9ecef"],      # 0 = aus
                        [0.34, "#2ca02c"], [0.66, "#2ca02c"],     # 1 = AN
                        [0.67, "#adb5bd"], [1.0, "#adb5bd"]],     # 2 = deaktiviert
            customdata=[[_lab[v] for v in row] for row in z],
            hovertemplate="%{y}  %{x|%H:%M} – %{customdata}<extra></extra>"),
            row=6, col=1)

    # ---------- Panel 7: Pool-/Thermo-Temperatur (erwartet vs. echt) ----------
    if temp_row is not None:
        from .loads import _slug as _lslug
        _tcol = ["#d62728", "#1f77b4", "#2ca02c", "#9467bd"]
        for i, ld in enumerate(thermal_cfg):
            sg = _lslug(ld.name)
            c = _tcol[i % len(_tcol)]
            # Komfortband [min_c, max_c] als hellgrüner Bereich
            fig.add_hrect(y0=ld.min_c, y1=ld.max_c, line_width=0,
                          fillcolor="rgba(44,160,44,0.10)", row=temp_row, col=1)
            col = f"load_{sg}_temp_c"
            if col in t.columns and t[col].notna().any():
                fig.add_trace(go.Scatter(
                    x=x, y=t[col], name=f"{ld.name} erwartet", mode="lines",
                    line=dict(color=c, width=2, dash="dash"),
                    hovertemplate="%{y:.1f} °C", legendgroup="temp",
                    legendgrouptitle_text="Temperatur"), row=temp_row, col=1)
            act = load_temp_actual.get(ld.name)
            if act is not None and len(act) > 0:
                fig.add_trace(go.Scatter(
                    x=act.index, y=act.values, name=f"{ld.name} echt", mode="lines",
                    line=dict(color=c, width=2),
                    hovertemplate="%{y:.1f} °C", legendgroup="temp",
                    legendgrouptitle_text="Temperatur"), row=temp_row, col=1)

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
    if has_loads:
        fig.update_yaxes(row=6, col=1, autorange="reversed", tickfont=dict(size=10))
    if temp_row is not None:
        fig.update_yaxes(title_text="°C", row=temp_row, col=1)

    # Mini-Legende der Modus-Farben DIREKT unter der Zeitleiste (Annotation,
    # unterhalb der Zeit-Beschriftung; die Trace-Legende rückt weiter nach
    # unten). Nur Modi zeigen, die im Horizont VORKOMMEN - eine vollständige
    # Farbtafel liest sich sonst wie eine Status-Anzeige.
    present = set(t["mode"].fillna("auto")) if "mode" in t.columns else {"auto"}
    present.add("auto")
    mode_leg = "<b>Modus:</b>  " + "   ".join(
        f"<span style='color:{_MODE_SWATCH[m]}'>■</span> "
        f"{_MODE_LABEL[m].replace(' (kein Eingriff)', '')}"
        for m in _MODES if m in present)
    fig.add_annotation(xref="paper", yref="paper", x=0, y=-0.05,
                       xanchor="left", yanchor="top", showarrow=False,
                       text=mode_leg, font=dict(size=11, color="#555"))
    fig.update_layout(
        height=(1120 if has_loads else 980) + (180 if temp_row else 0),
        autosize=True, template="plotly_white",
        hovermode="x unified", barmode="relative", bargap=0,
        # Deutsche Zahlenformate in Hover/Achsen: Dezimal-Komma, Tausender-Punkt
        separators=",.",
        margin=dict(l=60, r=30, t=80, b=140),
        legend=dict(orientation="h", yanchor="top", y=-0.09, xanchor="left",
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
              f"Limit Laden {ch_lim:,.0f} W · Entladen {dis_lim:,.0f} W"
              .replace(",", ".")),
        _tile("Eingriffe im Plan", f"{n_eingriffe}", "Slots ≠ auto"),
    ]
    # Prüf-Status als eigene Kachel (grün/gelb/rot)
    if violations is not None:
        v_err = sum(1 for v in violations if getattr(v, "severity", "") == "error")
        v_warn = sum(1 for v in violations if getattr(v, "severity", "") == "warning")
        if v_err:
            tiles.append(_tile("Planprüfung", f"{v_err} Fehler",
                              f"{v_warn} Warnungen", color="#d62728"))
        elif v_warn:
            tiles.append(_tile("Planprüfung", f"{v_warn} Warnungen",
                              "keine Fehler", color="#e6a700"))
        else:
            tiles.append(_tile("Planprüfung", "✓ OK", "alle Invarianten erfüllt",
                              color="#2ca02c"))

    plot_html = fig.to_html(full_html=False, include_plotlyjs=False,
                            default_width="100%",
                            config={"responsive": True, "displaylogo": False})
    report_html = _report_block(config, now, violations)
    controls_html = _controls_block(config)
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
 .banner {{ border-radius: 8px; padding: 8px 14px; margin-bottom: 10px;
           font-size: 13px; border: 1px solid; }}
 .banner ul {{ margin: 6px 0 0; padding-left: 20px; }}
 .banner li {{ margin: 2px 0; }}
 .banner.ok {{ background: #eafaf0; border-color: #b6e2c6; color: #1e7e46; }}
 .banner.warn {{ background: #fff8e1; border-color: #f0d98a; color: #8a6d00; }}
 .banner.err {{ background: #fdecea; border-color: #f5b5ae; color: #b3261e; }}
 .report {{ margin: 4px 0 12px; }}
 .report button {{ font-size: 13px; padding: 7px 14px; border-radius: 7px;
        border: 1px solid #c9ccd1; background: #f0f1f3; cursor: pointer; }}
 .report button.hot {{ background: #fdecea; border-color: #f5b5ae; color: #b3261e;
        font-weight: 600; }}
 .report .msg {{ margin-left: 10px; font-size: 12px; color: #555; }}
 .controls {{ margin: 4px 0 14px; padding: 10px 14px; background: #f6f7f9;
        border: 1px solid #e3e5e8; border-radius: 8px; font-size: 13px; }}
 .controls .ctl-h {{ font-weight: 700; margin-bottom: 8px; }}
 .controls .ctl-row {{ display: flex; align-items: center; flex-wrap: wrap;
        gap: 8px; padding: 5px 0; border-top: 1px solid #ececef; }}
 .controls .ctl-row:first-of-type {{ border-top: none; }}
 .controls .sw {{ min-width: 150px; }}
 .controls .fld {{ color: #555; }}
 .controls button {{ font-size: 13px; padding: 5px 12px; border-radius: 7px;
        border: 1px solid #c9ccd1; background: #f0f1f3; cursor: pointer; }}
 .controls button.mode.on {{ background: #0d6efd; color: #fff; border-color: #0d6efd; }}
 .controls .ctl-msg {{ margin-top: 8px; font-size: 12px; color: #555; min-height: 1em; }}
</style></head><body>
<h1>EMS – Ist vs. Prognose &amp; Steuerung
 <span class="ts">{now.strftime('%Y-%m-%d %H:%M')}</span></h1>
<div class="tiles">{''.join(tiles)}</div>
{_alert_banner(violations)}
{report_html}
{controls_html}
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
