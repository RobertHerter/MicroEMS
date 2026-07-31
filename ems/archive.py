"""Archivierte Optimierer-Läufe mit den tatsächlich eingetretenen Werten
vergleichen.

Jeder Zyklus legt einen Debug-Schnappschuss ab (``ems/debugdump.py`` ->
``debug_snapshot``, rollierend die letzten 1000 Läufe ~ 10 Tage). Darin stehen die
Eingaben (PV-/Last-Prognose, Preis, Start-SoC) UND der beschlossene Plan.
Weil die Ist-Werte separat mitgeschrieben werden, laesst sich jeder alte Lauf
nachtraeglich gegen die Realitaet legen - genau die Ansicht, die das Dashboard
sonst nur fuer den AKTUELLEN Lauf zeigt.

Rein lesend; die Aufbereitung passiert hier, gezeichnet wird im Browser
(``/archiv``).
"""
from __future__ import annotations

import logging
import re
from typing import Optional

import numpy as np
import pandas as pd

log = logging.getLogger("ems.archive")

# Ist-Signal -> Kurvenname der Ansicht. Die Vorzeichen folgen der Konvention der
# actuals-Tabelle: Akku positiv = laden, Netz positiv = Bezug.
_ACTUAL_FIELDS = {"pv_w": "pv_w", "house_w": "house_w", "battery_w": "battery_w",
                  "grid_w": "grid_w", "soc": "soc_percent"}


def list_runs(config, limit: int = 1200) -> list[dict]:
    """Kopfdaten der archivierten Laeufe (neueste zuerst) fuer die Auswahlliste."""
    from .local_history import list_debug_snapshots
    return list_debug_snapshots(config.e3dc_rscp.history_db_path,
                                config.general.timezone, limit=limit)


def _series(values, index) -> list:
    """Zahlenliste JSON-fest machen (NaN -> None)."""
    out = []
    for v in np.asarray(values, dtype="float64")[:len(index)]:
        out.append(None if not np.isfinite(v) else round(float(v), 2))
    return out


def _mae(plan, actual) -> Optional[float]:
    p = np.asarray(plan, dtype="float64")
    a = np.asarray(actual, dtype="float64")
    n = min(len(p), len(a))
    if n == 0:
        return None
    mask = np.isfinite(p[:n]) & np.isfinite(a[:n])
    if not mask.any():
        return None
    return round(float(np.mean(np.abs(p[:n][mask] - a[:n][mask]))), 1)


def run_detail(config, generated: Optional[str] = None) -> Optional[dict]:
    """Einen archivierten Lauf samt Ist-Werten aufbereiten.

    Plan und Ist liegen auf DEMSELBEN Zeitraster (dem des Laufs), damit die
    Kurven im Browser deckungsgleich sind. Ist-Werte gibt es nur bis "jetzt" -
    bei einem alten Lauf also ueber den ganzen Horizont, bei einem frischen nur
    am linken Rand. None, wenn zu diesem Zeitpunkt kein Lauf archiviert ist.
    """
    from .local_history import (read_actual, read_debug_snapshot,
                                read_optimizer_forecast_asof, read_spot)
    from .tariff import apply_tariff

    db = config.e3dc_rscp.history_db_path
    tz = config.general.timezone
    snap = read_debug_snapshot(db, generated)
    if not snap and generated:
        # In einem Query-String wird "+" als Leerzeichen dekodiert - der
        # Zeitzonen-Offset "+02:00" kommt dann als " 02:00" an. Ein nicht
        # kodierter Zeitstempel soll trotzdem funktionieren.
        fixed = re.sub(r"\s(\d{2}:?\d{2})$", r"+\1", str(generated))
        if fixed != generated:
            snap = read_debug_snapshot(db, fixed)
    if not snap:
        return None
    inputs = snap.get("inputs") or {}
    raw_index = inputs.get("index") or []
    if not raw_index:
        return None
    index = pd.DatetimeIndex(pd.to_datetime(raw_index, utc=True,
                                            format="ISO8601")).tz_convert(tz)
    plan = snap.get("plan") or {}
    step = pd.Timedelta(minutes=config.general.slot_minutes)

    def plan_col(name):
        col = plan.get(name)
        return None if col is None else np.asarray(col, dtype="float64")

    def plan_sum(*names):
        """Mehrere Plan-Spalten addieren; None, wenn keine davon existiert."""
        parts = [plan_col(n) for n in names]
        parts = [p for p in parts if p is not None]
        if not parts:
            return None
        n = min(len(p) for p in parts)
        return np.sum([np.nan_to_num(p[:n]) for p in parts], axis=0)

    # Akku: laden positiv, entladen negativ - wie das Ist-Signal battery_w.
    charge = plan_sum("batt_dc_charge_w", "batt_ac_charge_w")
    discharge = plan_col("batt_discharge_w")
    battery = None
    if charge is not None or discharge is not None:
        n = len(index)
        c = charge if charge is not None else np.zeros(n)
        d = discharge if discharge is not None else np.zeros(n)
        m = min(len(c), len(d), n)
        battery = c[:m] - d[:m]
    grid = None
    imp, exp = plan_col("grid_import_w"), plan_col("grid_export_w")
    if imp is not None or exp is not None:
        n = len(index)
        i = imp if imp is not None else np.zeros(n)
        e = exp if exp is not None else np.zeros(n)
        m = min(len(i), len(e), n)
        grid = i[:m] - e[:m]

    # War der Preis eines Slots zur Laufzeit nur geschaetzt? Steht im
    # Prognose-Archiv desselben Laufs (Folgetagspreise sind erst ab ~13:00 da).
    price_estimated = None
    try:
        _, frame = read_optimizer_forecast_asof(
            db, pd.Timestamp(snap.get("generated")) + pd.Timedelta(minutes=1),
            index[0], index[-1] + step, tz)
        cols = [] if frame is None else list(frame.columns)
        # Das Prognose-Archiv fuehrt die Maske je Signal als
        # "<signal>_estimated"; der aeltere Name bleibt als Fallback drin.
        name = next((c for c in ("price_ct_kwh_estimated", "price_estimated")
                     if c in cols), None)
        if name:
            col = frame[name].reindex(index)
            price_estimated = [None if pd.isna(v) else int(float(v) > 0.5)
                               for v in col]
    except Exception as exc:                                # pragma: no cover
        log.debug("Schaetz-Maske des Preises nicht lesbar (%s).", exc)

    plan_out = {
        "pv_w": _series(inputs.get("pv_w") or [], index),
        "pv10_w": (_series(inputs.get("pv10_w"), index)
                   if inputs.get("pv10_w") else None),
        "house_w": _series(inputs.get("house_load_w") or [], index),
        "price_ct_kwh": _series(inputs.get("price_ct_kwh") or [], index),
        "price_estimated": price_estimated,
        "battery_w": None if battery is None else _series(battery, index),
        "grid_w": None if grid is None else _series(grid, index),
        "soc_percent": (None if plan_col("house_soc_percent") is None
                        else _series(plan_col("house_soc_percent"), index)),
    }

    # Ist-Werte auf das Raster des Laufs bringen (nur vorhandene Slots).
    actual_out, coverage = {}, 0
    start, end = index[0], index[-1] + step
    for field, key in _ACTUAL_FIELDS.items():
        try:
            series = read_actual(db, field, start, end, tz)
        except Exception as exc:                            # pragma: no cover
            log.debug("Ist-Werte %s nicht lesbar (%s).", field, exc)
            series = pd.Series(dtype="float64")
        if series is None or series.empty:
            actual_out[key] = None
            continue
        aligned = series.reindex(index)
        coverage = max(coverage, int(aligned.notna().sum()))
        actual_out[key] = _series(aligned.to_numpy(dtype="float64"), index)

    # Ist-Preis: der inzwischen veroeffentlichte Boersenpreis, durch dasselbe
    # Tarifmodell gerechnet wie im Lauf. Ohne Auffuellen - wo (noch) nichts
    # veroeffentlicht ist, bleibt die Kurve leer.
    try:
        spot = read_spot(db, start, end, tz, config.general.slot_minutes)
        real_price = (apply_tariff(spot, config).reindex(index)
                      if spot is not None and not spot.empty else None)
    except Exception as exc:                                # pragma: no cover
        log.debug("Ist-Preis nicht lesbar (%s).", exc)
        real_price = None
    actual_out["price_ct_kwh"] = (
        None if real_price is None
        else _series(real_price.to_numpy(dtype="float64"), index))

    deviation = {
        "pv_mae_w": _mae(plan_out["pv_w"], actual_out.get("pv_w") or []),
        "house_mae_w": _mae(plan_out["house_w"], actual_out.get("house_w") or []),
        "soc_mae_pp": _mae(plan_out["soc_percent"] or [],
                           actual_out.get("soc_percent") or []),
        "battery_mae_w": _mae(plan_out["battery_w"] or [],
                              actual_out.get("battery_w") or []),
        "price_mae_ct": _mae(plan_out["price_ct_kwh"],
                             actual_out.get("price_ct_kwh") or []),
    }
    # Nur der geschaetzte Teil ist fuer die Preisguete interessant - bei den
    # bereits veroeffentlichten Slots muessen Plan und Ist ohnehin gleich sein.
    if price_estimated and actual_out.get("price_ct_kwh"):
        pe = [i for i, v in enumerate(price_estimated) if v]
        deviation["price_estimated_slots"] = len(pe)
        deviation["price_estimated_mae_ct"] = _mae(
            [plan_out["price_ct_kwh"][i] for i in pe],
            [actual_out["price_ct_kwh"][i] for i in pe])
    return {
        "generated": str(snap.get("generated") or ""),
        "ts_local": str(pd.Timestamp(snap.get("generated")).tz_convert(tz))
        if snap.get("generated") else "",
        "status": str(snap.get("status") or ""),
        "infeasible": bool(snap.get("infeasible")),
        "infeasible_reason": snap.get("infeasible_reason") or "",
        "solver_hit_limit": bool(snap.get("solver_hit_limit")),
        "total_cost_eur": snap.get("total_cost_eur"),
        "drift_soc_mae_pp": snap.get("drift_soc_mae_pp"),
        "violations": [v.get("rule") for v in (snap.get("violations") or [])],
        "mode": (snap.get("plan_mode") or [None])[0],
        "slots": len(index),
        "actual_slots": coverage,
        "index": [ts.isoformat() for ts in index],
        "plan": plan_out,
        "actual": actual_out,
        "deviation": deviation,
    }


def archive_html() -> bytes:
    """Eigenstaendige Seite ``/archiv``: Lauf auswaehlen, Plan + Ist zeichnen."""
    return _ARCHIVE_HTML.encode("utf-8")


# Die Kurven werden im Browser gezeichnet (plotly.min.js liegt lokal neben der
# Dashboard-HTML), damit das Umschalten zwischen Laeufen ohne Server-Rendering
# geht. Farben/Look bewusst wie im Dashboard.
_ARCHIVE_HTML = r"""<!doctype html>
<html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#1769c2">
<title>EMS Lauf-Archiv</title>
<script>(function(){const s=localStorage.getItem('ems-theme');document.documentElement.classList.toggle('dark',s==='dark'||(!s&&matchMedia('(prefers-color-scheme:dark)').matches));})();</script>
<style>
:root{color-scheme:light;--bg:#eef2f6;--card:#fff;--text:#20252b;--muted:#6b7480;--line:#e3e8ee;--r-card:14px;--r-ctl:9px;--s0:2px;--s1:4px;--s2:8px;--s3:12px;--s4:16px;--s5:24px;--font-num:ui-monospace,'SF Mono','Cascadia Mono','Segoe UI Mono','Roboto Mono','DejaVu Sans Mono',monospace;--t0:10px;--t1:12px;--t2:14px;--t3:17px;--t4:22px;--shadow:0 1px 2px rgba(20,35,55,.05),0 4px 16px rgba(20,35,55,.06);--input:#fff;--blue:#1769c2;--soft:#f5f7f9;--focus:#1769c2;--bad:#b52d28;--ok:#258448;--warn:#8a6d1f}
html.dark{color-scheme:dark;--bg:#10171e;--card:#18212b;--text:#e7edf4;--muted:#9aa7b4;--line:#33414f;--shadow:0 1px 2px rgba(0,0,0,.30),0 4px 16px rgba(0,0,0,.28);--input:#202e3a;--blue:#338be0;--soft:#202e3a;--focus:#4ea1f0;--bad:#ff8c87;--ok:#75ce91;--warn:#e5cb74}
:where(a,button,summary,input,select,textarea,[tabindex]):focus-visible{outline:2px solid var(--focus);outline-offset:2px;border-radius:var(--r-ctl)}
@media(prefers-reduced-motion:reduce){*,*::before,*::after{animation-duration:.01ms!important;animation-iteration-count:1!important;transition-duration:.01ms!important;scroll-behavior:auto!important}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
body{padding:0 var(--s4)}
.app-header{display:flex;align-items:center;gap:var(--s3);margin:var(--s4) 0;padding:var(--s3) var(--s4);
 background:var(--card);border:1px solid var(--line);border-radius:var(--r-card);box-shadow:var(--shadow)}
.app-header h1{flex:1;font-size:var(--t4);margin:0;min-width:0}
.app-header h1 .ts{color:var(--muted);font-weight:normal;font-size:var(--t2)}
.header-actions{display:flex;gap:var(--s2)}
button,.button{border:1px solid var(--line);border-radius:var(--r-ctl);background:var(--soft);color:var(--text);
 padding:var(--s2) var(--s3);font:inherit;cursor:pointer;text-decoration:none;display:inline-flex;
 align-items:center;justify-content:center;gap:var(--s1);min-width:42px;min-height:38px}
button:disabled{opacity:.55;cursor:wait}
select,input{border:1px solid var(--line);border-radius:var(--r-ctl);background:var(--input);color:var(--text);padding:var(--s2) var(--s2);font:inherit;max-width:100%}
main{max-width:1500px;margin:auto;padding:0 0 60px}.app-header{max-width:1500px;margin:var(--s4) auto}
.card{background:var(--card);border:1px solid var(--line);border-radius:var(--r-card);padding:var(--s4) var(--s4);margin-bottom:var(--s3);box-shadow:var(--shadow)}
.pick{display:grid;grid-template-columns:minmax(240px,1fr) auto auto auto;gap:var(--s2);align-items:center}
.pick label{grid-column:1/-1;color:var(--muted);font-size:var(--t1)}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(132px,1fr));gap:var(--s2)}
.tile{background:var(--soft);border:1px solid var(--line);border-radius:var(--r-ctl);padding:var(--s2) var(--s3)}
.tile .v{font-size:var(--t4);font-weight:750;font-family:var(--font-num);font-variant-numeric:tabular-nums slashed-zero}.tile .l{color:var(--muted);font-size:var(--t1);margin-top:var(--s0)}.tile .s{color:var(--muted);font-size:var(--t1);margin-top:var(--s0)}
.tile.warn{border-color:#e1b74a}.tile.bad{border-color:#d56b67}
.hint{color:var(--muted);font-size:var(--t1);margin-top:var(--s2);line-height:1.45}
.chart-controls{grid-column:1/-1;display:flex;align-items:center;justify-content:space-between;gap:var(--s2);flex-wrap:wrap}
.horizon-switch,.view-switch{display:flex;gap:var(--s1)}
.horizon-switch button,.view-switch button{min-width:0;padding:var(--s2) var(--s3);font-size:var(--t1)}
.horizon-switch button.on,.view-switch button.on{background:var(--blue);border-color:var(--blue);color:#fff}
.chart{height:720px}
@media(max-width:700px){.chart{height:600px}body{padding:0 var(--s2)}
 .app-header{padding:var(--s3) var(--s3);margin:var(--s2) auto}.app-header h1{font-size:var(--t3);line-height:1.25}
 .app-header h1 .ts{display:block;font-size:var(--t1);margin-top:var(--s0)}
 .header-actions .button-label{display:none}.pick{grid-template-columns:1fr 1fr}
 .chart-controls{align-items:stretch}.horizon-switch,.view-switch{flex:1}
 .horizon-switch button,.view-switch button{flex:1}}
.err{color:var(--bad);font-weight:600}
</style></head><body>
<header class="app-header"><h1>EMS Lauf-Archiv
 <span class="ts">Archivierten Optimierer-Lauf gegen die eingetretenen Werte legen</span></h1>
 <div class="header-actions"><a class="button" href="/" title="Zurück zum Dashboard">⌂ <span class="button-label">Dashboard</span></a>
 <button id="theme" title="Darstellung wechseln">◐ <span class="button-label">Darstellung</span></button></div></header>
<main>
<div class="card"><div class="pick">
 <label for="run">Tag und Optimierer-Lauf (Vorauswahl: heute 00:00)</label>
 <input type="date" id="day" title="Tag wählen – die Liste zeigt dann nur dessen Läufe">
 <select id="run"><option>lade …</option></select>
 <button id="prev" title="älterer Lauf">◀ <span class="button-label">älter</span></button>
 <button id="next" title="neuerer Lauf"><span class="button-label">neuer</span> ▶</button>
 <div class="chart-controls">
  <div class="horizon-switch" id="horizon" aria-label="Zeitraum">
   <button type="button" data-hours="24">24 h</button><button type="button" data-hours="48">48 h</button><button type="button" data-hours="all">Alles</button>
  </div>
  <div class="view-switch" id="archive-view" aria-label="Vergleichsdarstellung">
   <button type="button" data-view="lines">Plan / Ist</button>
   <button type="button" data-view="delta">Differenzfläche</button>
  </div>
 </div>
</div><div class="hint" id="meta"></div></div>
<div class="card"><div class="tiles" id="kpi"></div></div>
<div class="card"><div id="chart" class="chart"></div>
<div class="hint"><span id="lines-hint">Durchgezogen = <b>Plan</b> dieses Laufs,
gestrichelt = <b>Ist</b>.</span><span id="delta-hint" hidden>Die Fläche zeigt
<b>Ist − Plan</b>: oberhalb der Nulllinie war der Ist-Wert höher, unterhalb
niedriger.</span>
Ist-Werte gibt es nur für die Zeit, die seit dem Lauf vergangen ist – bei einem
frischen Lauf also nur am linken Rand. Akku positiv = laden, Netz positiv = Bezug.
<br>Beim <b>Preis</b> umgekehrt: durchgezogen ist der tatsächliche Börsenpreis;
gestrichelt erscheint nur dort etwas, wo er zur Planung noch nicht
veröffentlicht war und der Plan schätzen musste (Folgetag vor ~13:00).</div></div>
</main>
<script src="plotly.min.js"></script>
<script>(function(){
 const g=id=>document.getElementById(id);
 const sel=g('run');
 let RUNS=[],IDX=0;
 const num=(v,d)=>(typeof v==='number'&&isFinite(v)?v.toLocaleString('de-DE',{maximumFractionDigits:d==null?0:d}):'–');
 g('theme').onclick=function(){const dark=!document.documentElement.classList.contains('dark');
  document.documentElement.classList.toggle('dark',dark);localStorage.setItem('ems-theme',dark?'dark':'light');
  if(window.LAST)draw(window.LAST);};
 function css(name){return getComputedStyle(document.documentElement).getPropertyValue(name).trim();}
 function tile(v,l,s,cls){return '<div class="tile'+(cls?' '+cls:'')+'"><div class="v">'+v+'</div><div class="l">'+l+'</div>'+(s?'<div class="s">'+s+'</div>':'')+'</div>';}
 function label(r){
  const t=(r.ts_local||r.generated||'').replace('T',' ').slice(0,16);
  let flag='';
  if(r.infeasible)flag=' ⚠ unlösbar';
  else if(r.n_violations)flag=' ⚠ '+r.n_violations+' Verstoß/Verstöße';
  return t+' · '+(r.status||'?')+flag;
 }
 const dayOf=r=>String(r.ts_local||r.generated||'').slice(0,10);
 const minOf=r=>{const t=String(r.ts_local||r.generated||'');
  return parseInt(t.slice(11,13),10)*60+parseInt(t.slice(14,16),10);};
 function fillDay(day,preferTs){
  // Nur die Läufe des gewählten Tages anbieten - bei 10 Tagen Vorhaltung wäre
  // eine Liste über alle Läufe nicht mehr bedienbar.
  const rows=RUNS.map((x,i)=>({x:x,i:i})).filter(e=>dayOf(e.x)===day);
  if(!rows.length){sel.innerHTML='<option>keine Läufe an diesem Tag</option>';
   g('meta').innerHTML='<span class="err">Für '+day+' ist kein Lauf archiviert.</span>';
   g('kpi').innerHTML='';Plotly.purge('chart');return false;}
  sel.innerHTML=rows.map(e=>'<option value="'+e.i+'">'+label(e.x)+'</option>').join('');
  let pick=rows.find(e=>e.x.generated===preferTs);
  // Vorauswahl: der Lauf, der 00:00 am nächsten liegt (Tagesplan), nicht der
  // aktuellste - der steht schon im Dashboard.
  if(!pick)pick=rows.reduce((b,e)=>minOf(e.x)<minOf(b.x)?e:b,rows[0]);
  IDX=pick.i;sel.value=String(IDX);
  return true;
 }
 async function loadRuns(){
  try{
   const r=await fetch('api/archive-runs.json?_='+Date.now(),{cache:'no-store'});
   if(!r.ok)throw Error(r.status);
   RUNS=(await r.json()).snapshots||[];
   if(!RUNS.length){sel.innerHTML='<option>keine archivierten Läufe</option>';g('meta').innerHTML='<span class="err">Noch kein Lauf im Verlauf – der erste Zyklus legt ihn an.</span>';return;}
   const days=[...new Set(RUNS.map(dayOf))].sort();
   const dayEl=g('day');
   dayEl.min=days[0];dayEl.max=days[days.length-1];
   const want=new URLSearchParams(location.search).get('ts');
   const wanted=RUNS.find(x=>x.generated===want);
   // Standard: heute; gibt es für heute nichts, der jüngste vorhandene Tag.
   let day=wanted?dayOf(wanted):days[days.length-1];
   // Ortszeit, NICHT toISOString: das rechnet nach UTC, und zwischen
   // Mitternacht und dem Zeitzonenversatz (im Sommer bis 02:00) waere "heute"
   // dann der Vortag - die Vorauswahl haette den falschen Tag getroffen.
   if(!wanted){const n=new Date(),z=v=>String(v).padStart(2,'0');
    const today=n.getFullYear()+'-'+z(n.getMonth()+1)+'-'+z(n.getDate());
    if(days.indexOf(today)>=0)day=today;}
   dayEl.value=day;
   if(fillDay(day,want))await show();
  }catch(e){g('meta').innerHTML='<span class="err">Lauf-Liste nicht erreichbar ('+e.message+').</span>';}
 }
 async function show(){
  const r=RUNS[IDX];if(!r)return;
  g('meta').textContent='wird geladen …';
  ['prev','next'].forEach(i=>g(i).disabled=true);
  try{
   const q=await fetch('api/archive-run.json?ts='+encodeURIComponent(r.generated),{cache:'no-store'});
   if(!q.ok)throw Error(q.status);
   const d=await q.json();
   window.LAST=d;
   kpis(d);draw(d);
   history.replaceState(null,'','?ts='+encodeURIComponent(r.generated));
  }catch(e){g('meta').innerHTML='<span class="err">Lauf nicht ladbar ('+e.message+').</span>';g('kpi').innerHTML='';}
  ['prev','next'].forEach(i=>g(i).disabled=false);
  g('prev').disabled=IDX>=RUNS.length-1;g('next').disabled=IDX<=0;
 }
 function kpis(d){
  const dv=d.deviation||{},cov=d.slots?Math.round(100*d.actual_slots/d.slots):0;
  g('meta').textContent='Lauf '+(d.ts_local||'').slice(0,19)+' · '+d.slots+' Slots'
   +(d.mode?' · Modus '+d.mode:'')+(d.violations&&d.violations.length?' · Verstöße: '+d.violations.join(', '):'');
  g('kpi').innerHTML=
    tile((d.infeasible?'unlösbar':(d.status||'–')),'Solver-Status',
         d.infeasible_reason||(d.solver_hit_limit?'Zeitlimit erreicht':''),
         d.infeasible?'bad':'')
   +tile(num(d.total_cost_eur,2)+' €','Plankosten','über den Horizont')
   +tile(num(dv.pv_mae_w)+' W','PV-Abweichung','MAE Plan gegen Ist')
   +tile(num(dv.house_mae_w)+' W','Last-Abweichung','MAE Plan gegen Ist')
   +tile(num(dv.battery_mae_w)+' W','Akku-Abweichung','MAE Plan gegen Ist')
   +tile(num(dv.soc_mae_pp,1)+' %','SoC-Abweichung','MAE in Prozentpunkten',
         (typeof dv.soc_mae_pp==='number'&&dv.soc_mae_pp>5?'warn':''))
   +tile((typeof dv.price_mae_ct==='number'?num(dv.price_mae_ct,2)+' ct':'–'),
         'Preis-Abweichung',
         (dv.price_estimated_slots
          ? 'geschätzter Teil: '+num(dv.price_estimated_mae_ct,2)+' ct in '
            +dv.price_estimated_slots+' Slots'
          : 'Plan gegen veröffentlichten Preis'))
   +tile(cov+' %','Ist-Abdeckung',d.actual_slots+' von '+d.slots+' Slots');
 }
 function viewPref(){return localStorage.getItem('ems-archive-view')||'lines';}
 function markView(){
  const mode=viewPref();
  document.querySelectorAll('#archive-view button').forEach(function(b){
   b.classList.toggle('on',b.dataset.view===mode);});
  g('lines-hint').hidden=mode!=='lines';
  g('delta-hint').hidden=mode!=='delta';
 }
 function draw(d){
  const x=d.index,T=[];
  const deltaMode=viewPref()==='delta';
  markView();
  const fg=css('--text')||'#20252b',mut=css('--muted')||'#697785',line=css('--line')||'#dce4eb';
  function add(y,name,color,row,dash,unit,noLegend){
   if(!y||!y.some(v=>v!==null))return;
   T.push({x:x,y:y,name:name,type:'scatter',mode:'lines',
     line:{color:color,width:dash?1.6:2,dash:dash||'solid',shape:'hv'},
     yaxis:row===1?'y':'y'+row,legendgroup:name.replace(/ (Plan|Ist)$/,''),
     showlegend:!noLegend,
     hovertemplate:name+': %{y:,.'+(unit==='%'?1:0)+'f} '+unit+'<extra></extra>'});
  }
  function transparent(color,alpha){
   const c=String(color||'').replace('#','');
   if(!/^[0-9a-f]{6}$/i.test(c))return 'rgba(23,105,194,'+alpha+')';
   return 'rgba('+parseInt(c.slice(0,2),16)+','+parseInt(c.slice(2,4),16)+','
    +parseInt(c.slice(4,6),16)+','+alpha+')';
  }
  function addDelta(plan,actual,name,color,row,unit){
   if(!plan||!actual)return;
   const y=x.map(function(_,i){
    const p=plan[i],a=actual[i];
    return (typeof p==='number'&&isFinite(p)&&typeof a==='number'&&isFinite(a))
     ?a-p:null;
   });
   if(!y.some(v=>v!==null))return;
   T.push({x:x,y:y,name:name+' Δ',type:'scatter',mode:'lines',
    line:{color:color,width:1.5,shape:'hv'},fill:'tozeroy',
    fillcolor:transparent(color,.22),yaxis:row===1?'y':'y'+row,
    legendgroup:name,hovertemplate:name+' Δ (Ist − Plan): %{y:,.'
     +(unit==='%'?1:0)+'f} '+unit+'<extra></extra>'});
  }
  const P=d.plan||{},A=d.actual||{};
  if(deltaMode){
   addDelta(P.pv_w,A.pv_w,'PV','#e8a33d',1,'W');
   addDelta(P.house_w,A.house_w,'Last','#c1554f',1,'W');
   addDelta(P.battery_w,A.battery_w,'Akku','#2f8f4e',1,'W');
   addDelta(P.grid_w,A.grid_w,'Netz','#6c7a89',1,'W');
   addDelta(P.soc_percent,A.soc_percent,'SoC','#1769c2',2,'%');
  }else{
   add(P.pv_w,'PV Plan','#e8a33d',1,null,'W');        add(A.pv_w,'PV Ist','#e8a33d',1,'dot','W');
   add(P.house_w,'Last Plan','#c1554f',1,null,'W');   add(A.house_w,'Last Ist','#c1554f',1,'dot','W');
   add(P.battery_w,'Akku Plan','#2f8f4e',1,null,'W'); add(A.battery_w,'Akku Ist','#2f8f4e',1,'dot','W');
   add(P.grid_w,'Netz Plan','#6c7a89',1,null,'W');    add(A.grid_w,'Netz Ist','#6c7a89',1,'dot','W');
   add(P.soc_percent,'SoC Plan','#1769c2',2,null,'%');add(A.soc_percent,'SoC Ist','#1769c2',2,'dot','%');
  }
  // Preis wie im Dashboard: EINE durchgezogene Linie mit dem tatsaechlichen
  // Boersenpreis. Wo er zur Planung schon veroeffentlicht war, ist das der
  // Planpreis selbst (Plan und Ist doppelt zu zeichnen zeigte denselben Wert
  // zweimal); wo der Plan schaetzen musste, der inzwischen bekannte Preis -
  // und dazu gestrichelt, was der Plan dort angenommen hatte.
  const est=P.price_estimated||[],pp=P.price_ct_kwh,ap=A.price_ct_kwh;
  const real=i=>(ap&&ap[i]!==null&&ap[i]!==undefined)?ap[i]:null;
  // Die Maske deckt nur den archivierten Prognosebereich ab; darueber hinaus
  // ist sie null. NULL heisst 'unbekannt', NICHT 'war veroeffentlicht' - sonst
  // wird der uebermorgige Schaetzwert als Boersenpreis ausgegeben, den es zur
  // Laufzeit gar nicht geben konnte. Ohne Maske entscheidet, ob heute ein
  // echter Preis vorliegt.
  const guessed=i=>((est[i]===0||est[i]===1)?!!est[i]:real(i)===null);
  // Durchgezogen NUR echter Boersenpreis: bekannt zur Laufzeit (dann ist der
  // Planwert genau dieser Preis) oder inzwischen veroeffentlicht.
  if(deltaMode){
   addDelta(pp,ap,'Preis','#7d5ba6',3,'ct/kWh');
  }else{
   add(pp.map((v,i)=>real(i)!==null?real(i):(guessed(i)?null:v)),
       'Börsenpreis','#7d5ba6',3,null,'ct/kWh');
   if(pp.some((v,i)=>guessed(i)))
    // Der Uebergangsslot gehoert mit dazu, sonst klafft eine Luecke.
    add(pp.map((v,i)=>(guessed(i)||(i+1<pp.length&&guessed(i+1)))?v:null),
        'Preis (Schätzung)','#b58fd6',3,'dash','ct/kWh');
  }
  const ax={gridcolor:line,zerolinecolor:line,linecolor:line,tickfont:{color:mut}};
  // Ohne diese beiden Bloecke bleiben Hover-Box und Werkzeugleiste im
  // Dark-Mode weiss auf weiss (Plotly-Standard ist hell).
  const card=css('--card')||'#fff',blue=css('--blue')||'#1769c2';
  const hoverlabel={bgcolor:card,bordercolor:line,font:{color:fg,size:12}};
  const modebar={bgcolor:'rgba(0,0,0,0)',color:mut,activecolor:blue};
  Plotly.react('chart',T,{
   separators:',.',paper_bgcolor:'rgba(0,0,0,0)',plot_bgcolor:'rgba(0,0,0,0)',
   font:{color:fg},margin:{l:58,r:16,t:26,b:36},hovermode:'x unified',
   hoverlabel:hoverlabel,modebar:modebar,
   legend:{orientation:'h',y:1.06,font:{size:11}},
   xaxis:Object.assign({},ax,{domain:[0,1],anchor:'y3'}),
   yaxis:Object.assign({},ax,{title:{text:(deltaMode?'Δ Leistung (W)':'Leistung (W)'),font:{size:11}},domain:[0.46,1]}),
   yaxis2:Object.assign({},ax,{title:{text:(deltaMode?'Δ SoC (pp)':'SoC (%)'),font:{size:11}},domain:[0.24,0.42]},
                        deltaMode?{}:{range:[0,100]}),
   yaxis3:Object.assign({},ax,{title:{text:(deltaMode?'Δ ct/kWh':'ct/kWh'),font:{size:11}},domain:[0,0.20]}),
   shapes:[{type:'line',x0:x[0],x1:x[0],yref:'paper',y0:0,y1:1,
            line:{color:mut,width:1,dash:'dot'}}]
  },{responsive:true,displaylogo:false,
     modeBarButtonsToRemove:['lasso2d','select2d','autoScale2d']});
  window.LAST_X=x;applyHours();
 }
 // Zeitraum-Schalter: der Bezugspunkt ist der ANFANG des gewaehlten Laufs,
 // nicht "jetzt" - ein archivierter Lauf von vorgestern soll seine ersten 24 h
 // zeigen, nicht die letzten 24 h vor der Gegenwart.
 function hoursPref(){return localStorage.getItem('ems-archive-hours')||'all';}
 function applyHours(){
  const x=window.LAST_X;if(!x||!x.length)return;
  const h=hoursPref();
  document.querySelectorAll('#horizon button').forEach(function(b){
   b.classList.toggle('on',b.dataset.hours===h);});
  if(h==='all'){Plotly.relayout('chart',{'xaxis.autorange':true,'xaxis.range':null});return;}
  // Grenzen aus der Zeitachse SELBST greifen statt sie zu rechnen. Die Werte
  // tragen den UTC-Versatz; ein toISOString() haette den Bereich um den Versatz
  // verschoben (derselbe Fehler, den der Dashboard-Test ausdruecklich verbietet).
  // Ueber die Slotzahl ist es zudem unempfindlich gegen die Zeitumstellung.
  const proSlot=x.length>1?(new Date(x[1])-new Date(x[0]))/3600000:0.25;
  const bis=Math.min(x.length-1,Math.round(parseInt(h,10)/(proSlot||0.25)));
  Plotly.relayout('chart',{'xaxis.autorange':false,'xaxis.range':[x[0],x[bis]]});
 }
 document.querySelectorAll('#horizon button').forEach(function(b){
  b.addEventListener('click',function(){
   localStorage.setItem('ems-archive-hours',b.dataset.hours);applyHours();});});
 document.querySelectorAll('#archive-view button').forEach(function(b){
  b.addEventListener('click',function(){
   localStorage.setItem('ems-archive-view',b.dataset.view);
   if(window.LAST)draw(window.LAST);else markView();});});
 markView();
 sel.onchange=function(){IDX=parseInt(this.value,10)||0;show();};
 g('day').onchange=function(){if(fillDay(this.value,null))show();};
 function step(delta){            // ueber Tagesgrenzen hinweg blaettern
  const j=IDX+delta;if(j<0||j>=RUNS.length)return;
  const day=dayOf(RUNS[j]);
  if(day!==g('day').value){g('day').value=day;fillDay(day,RUNS[j].generated);}
  else{IDX=j;sel.value=String(IDX);}
  show();
 }
 g('prev').onclick=function(){step(1);};   // groesserer Index = aelter
 g('next').onclick=function(){step(-1);};
 loadRuns();
})();</script>
</body></html>
"""
