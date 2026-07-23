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

_MODES = ["auto", "peak", "late", "limit_charge", "limit_discharge", "hold",
          "block_charge", "grid_charge", "grid_discharge"]
_MODE_LABEL = {"auto": "auto (kein Eingriff)",
               "peak": "Peak-Laden (Linie)",
               "late": "Spät laden",
               "limit_charge": "Laden gedrosselt",
               "limit_discharge": "Entladen gedrosselt",
               "hold": "Entladen gesperrt", "block_charge": "Laden gesperrt",
               "grid_charge": "Netzladen", "grid_discharge": "Netz-Entladen"}
_MODE_COLOR = {"auto": "#f0f0f0", "peak": "#74c476", "late": "#20a39e",
               "limit_charge": "#ffd92f", "limit_discharge": "#e377c2",
               "hold": "#ff8c00", "block_charge": "#d62728",
               "grid_charge": "#1f77b4", "grid_discharge": "#9400d3"}
# Legenden-Swatch: auto wäre auf Weiß unsichtbar
_MODE_SWATCH = dict(_MODE_COLOR, auto="#c8c8c8")
_GROUPS = {"ist": "Ist", "prog": "Prognose", "progb": "Netz/Preis",
           "soc": "Ladezustand", "ctrl": "Steuerung"}
# Spaltenreihenfolge der (nebeneinander stehenden) Legendengruppen. Die beiden
# Prognose-Spalten (prog + progb) stehen so direkt nebeneinander -> die früher
# sehr lange "Prognose"-Spalte wird auf zwei kürzere aufgeteilt (weniger Höhe).
_GROUP_RANK = {"ist": 30, "prog": 10, "progb": 20, "soc": 40, "ctrl": 50}
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


def _live_block(config: Config) -> str:
    """Kompakte E3/DC-Livekacheln; Werte kommen aus /api/live.json.

    Das Dashboard bleibt eine statische, atomar erzeugte HTML-Datei. Nur diese
    kleinen Kacheln werden im Browser nachgeführt; der Optimierer läuft dadurch
    nicht häufiger. Der HTTP-Server cached die zugrunde liegende RSCP-Abfrage.
    """
    seconds = max(0.0, float(getattr(
        config.dashboard, "live_refresh_seconds", 5.0) or 0.0))
    if seconds <= 0.0:
        return ""
    interval_ms = max(1000, round(seconds * 1000))
    # Pool-Kachel nur, wenn eine thermische Last mit Ist-Temperatur aktiv ist.
    pool = next((ld for ld in getattr(config, "controllable_loads", [])
                 if getattr(ld, "type", None) == "thermal" and ld.enabled
                 and ld.temp_signal), None)
    pool_tile = (
        f'<div class="tile live-temp"><div class="v" id="live-pool-temp">–</div>'
        f'<div class="l">{_esc(pool.name)}</div><div class="s">Ist-Temperatur</div></div>'
        if pool is not None else "")
    outdoor_tile = (
        '<div class="tile live-temp"><div class="v" id="live-outdoor-temp">–</div>'
        '<div class="l">Außentemperatur</div><div class="s">Open-Meteo</div></div>'
        if getattr(getattr(config, "weather", None), "enabled", False) else "")
    return f"""
<section class="live-panel" id="e3dc-live">
 <div class="live-head"><b>E3/DC live</b>
  <span id="live-status"><span class="live-dot"></span> wird geladen …</span>
 </div>
 <div class="tiles live-tiles">
  <div class="tile live-solar"><div class="v" id="live-pv">–</div><div class="l">Solarerzeugung</div><div class="s">PV aktuell</div></div>
  <div class="tile live-house"><div class="v" id="live-house">–</div><div class="l">Hauslast</div><div class="s">Verbrauch aktuell</div></div>
  <div class="tile live-flow" id="live-grid-tile"><div class="v" id="live-grid">–</div><div class="l">Netz</div><div class="s" id="live-grid-dir">–</div></div>
  <div class="tile live-flow" id="live-battery-tile"><div class="v" id="live-battery">–</div><div class="l">Batterie</div><div class="s" id="live-battery-dir">–</div></div>
  <div class="tile live-soc"><div class="v" id="live-soc">–</div><div class="l">Akku-SoC</div><div class="s">E3/DC Messwert</div></div>
  <div class="tile live-wallbox"><div class="v" id="live-wallbox">–</div><div class="l">Wallbox</div><div class="s">Ladeleistung aktuell</div></div>
  <div class="tile daily-price"><div class="v" id="live-price-now">–</div><div class="l">Aktueller Strompreis</div><div class="s">Bezugspreis pro kWh</div></div>
  {pool_tile}{outdoor_tile}
 </div>
 <details class="live-daily-panel" id="live-daily-panel" open>
 <summary><b>Heute</b><span id="live-day-status">E3/DC-Zähler werden geladen …</span></summary>
  <div class="tiles live-tiles live-daily">
  <div class="tile live-solar"><div class="v" id="live-pv-forecast-today">–</div><div class="l">PV-Vorhersage heute</div><div class="s">gesamter Prognosetag</div></div>
  <div class="tile live-solar"><div class="v" id="live-pv-yield-today">–</div><div class="l">PV-Ertrag</div><div class="s">gemessene Energie</div></div>
  <div class="tile daily-import"><div class="v" id="live-grid-import-today">–</div><div class="l">Netzbezug</div><div class="s">gemessene Energie</div></div>
  <div class="tile daily-export"><div class="v" id="live-grid-export-today">–</div><div class="l">Netzeinspeisung</div><div class="s">gemessene Energie</div></div>
  <div class="tile daily-charge"><div class="v" id="live-battery-charge-today">–</div><div class="l">Speicher geladen</div><div class="s">gemessene Energie</div></div>
  <div class="tile daily-discharge"><div class="v" id="live-battery-discharge-today">–</div><div class="l">Speicher entladen</div><div class="s">gemessene Energie</div></div>
  <div class="tile live-house"><div class="v" id="live-house-today">–</div><div class="l">Hausverbrauch</div><div class="s">gemessene Energie</div></div>
  <div class="tile live-solar"><div class="v" id="live-pv-forecast-now">–</div><div class="l">PV-Prognose bis jetzt</div><div class="s">zeitanteiliger Soll-Ertrag</div></div>
  <div class="tile daily-deviation" id="live-pv-deviation-tile"><div class="v" id="live-pv-deviation-today">–</div><div class="l">PV-Abweichung</div><div class="s" id="live-pv-deviation-detail">Ist gegen Prognose bis jetzt</div></div>
  <div class="tile daily-balance" id="live-energy-balance-tile"><div class="v" id="live-energy-balance">–</div><div class="l">Energiebilanz</div><div class="s" id="live-energy-balance-detail">Zähler werden geprüft</div></div>
 </div>
 </details>
</section>
<script>(function(){{
 var root=document.getElementById('e3dc-live'), status=document.getElementById('live-status');
 function num(v,d){{return (typeof v==='number'&&isFinite(v))?v.toLocaleString('de-DE',{{maximumFractionDigits:d||0}}):'–';}}
 function power(id,v,absolute){{var e=document.getElementById(id);e.textContent=(typeof v==='number'&&isFinite(v)?num(absolute?Math.abs(v):v,0)+' W':'–');}}
 function energy(id,v){{var e=document.getElementById(id);if(e)e.textContent=(typeof v==='number'&&isFinite(v)?num(v,2)+' kWh':'–');}}
 function temp(id,v){{var e=document.getElementById(id);if(e)e.textContent=(typeof v==='number'&&isFinite(v)?num(v,1)+' °C':'–');}}
 function direction(id,v,pos,neg,idle){{var e=document.getElementById(id);e.textContent=!(typeof v==='number'&&isFinite(v))?'–':(v>25?pos:(v < -25?neg:idle));}}
 function flow(id,cls){{var e=document.getElementById(id);e.classList.remove('flow-import','flow-export','flow-charge','flow-discharge','flow-idle');e.classList.add(cls);}}
 function render(d){{
  power('live-pv',d.pv_w,false); power('live-house',d.house_load_w,false);
  power('live-grid',d.grid_w,true); direction('live-grid-dir',d.grid_w,'Netzbezug','Einspeisung','nahezu ausgeglichen');
  flow('live-grid-tile',!(typeof d.grid_w==='number')?'flow-idle':(d.grid_w>25?'flow-import':(d.grid_w < -25?'flow-export':'flow-idle')));
  power('live-battery',d.battery_w,true); direction('live-battery-dir',d.battery_w,'Akku lädt','Akku entlädt','Akku ruht');
  flow('live-battery-tile',!(typeof d.battery_w==='number')?'flow-idle':(d.battery_w>25?'flow-charge':(d.battery_w < -25?'flow-discharge':'flow-idle')));
  power('live-wallbox',d.wallbox_w,false);
  temp('live-pool-temp',d.pool_temp_c); temp('live-outdoor-temp',d.outdoor_temp_c);
  document.getElementById('live-soc').textContent=(typeof d.soc_percent==='number'?num(d.soc_percent,1)+' %':'–');
  energy('live-pv-forecast-today',d.pv_forecast_today_kwh);
  energy('live-pv-forecast-now',d.pv_forecast_until_now_kwh);
  energy('live-pv-yield-today',d.pv_yield_today_kwh);
  energy('live-pv-deviation-today',d.pv_deviation_today_kwh);
  var dev=document.getElementById('live-pv-deviation-tile'),devDetail=document.getElementById('live-pv-deviation-detail');
  if(dev){{dev.classList.remove('positive','negative');if(typeof d.pv_deviation_today_kwh==='number')dev.classList.add(d.pv_deviation_today_kwh>=0?'positive':'negative');}}
  if(devDetail)devDetail.textContent=(typeof d.pv_deviation_today_percent==='number'?(d.pv_deviation_today_percent>0?'+':'')+num(d.pv_deviation_today_percent,1)+' % gegen Soll':'Ist gegen Prognose bis jetzt');
  energy('live-grid-import-today',d.grid_import_today_kwh);
  energy('live-grid-export-today',d.grid_export_today_kwh);
  energy('live-battery-charge-today',d.battery_charge_today_kwh);
  energy('live-battery-discharge-today',d.battery_discharge_today_kwh);
  energy('live-house-today',d.house_consumption_today_kwh);
  var balance=document.getElementById('live-energy-balance'),balanceTile=document.getElementById('live-energy-balance-tile'),balanceDetail=document.getElementById('live-energy-balance-detail');
  if(balanceTile)balanceTile.classList.remove('ok','bad');
  if(d.energy_balance_ok===true){{balance.textContent='✓ plausibel';balanceTile.classList.add('ok');}}
  else if(d.energy_balance_ok===false){{balance.textContent='⚠ Abweichung';balanceTile.classList.add('bad');}}
  else balance.textContent='nicht prüfbar';
  if(balanceDetail)balanceDetail.textContent=typeof d.energy_balance_residual_kwh==='number'?'Rest '+(d.energy_balance_residual_kwh>0?'+':'')+num(d.energy_balance_residual_kwh,3)+' kWh':'kein separater Verbrauchszähler';
  document.getElementById('live-price-now').textContent=(typeof d.current_price_ct_kwh==='number'?num(d.current_price_ct_kwh,2)+' ct':'–');
  var dayStatus=document.getElementById('live-day-status'),dayTs=d.daily_energy_updated?new Date(d.daily_energy_updated):null;
  if(dayStatus&&dayTs&&!isNaN(dayTs)){{var age=Math.max(0,(Date.now()-dayTs.getTime())/1000),ageText=age<90?'aktuell':(age<3600?'vor '+Math.round(age/60)+' min':'vor '+num(age/3600,1)+' h');dayStatus.textContent='Zählerstand '+dayTs.toLocaleTimeString('de-DE',{{hour:'2-digit',minute:'2-digit'}})+' · '+ageText;dayStatus.dataset.quality=age<=90?'current':(age<=300?'warning':'stale');}}
  var ctl=document.getElementById('e3dc-control-enabled'),ctlLabel=document.getElementById('e3dc-control-label');
  if(ctl&&!ctl.disabled&&typeof d.e3dc_control_enabled==='boolean'){{ctl.checked=d.e3dc_control_enabled;if(ctlLabel)ctlLabel.textContent=d.e3dc_control_enabled?'aktiv':'ausgeschaltet';}}
  var ts=d.updated?new Date(d.updated):null;
  status.innerHTML='<span class="live-dot ok"></span> '+(ts&&!isNaN(ts)?'Stand '+ts.toLocaleTimeString('de-DE',{{hour:'2-digit',minute:'2-digit',second:'2-digit'}}):'aktuell');
  root.classList.remove('stale');
 }}
 function fail(){{status.innerHTML='<span class="live-dot err"></span> nicht verfügbar';root.classList.add('stale');}}
 function tick(){{fetch('api/live.json?_='+Date.now(),{{cache:'no-store'}}).then(function(r){{if(!r.ok)throw Error(r.status);return r.json();}}).then(render).catch(fail);}}
 var daily=document.getElementById('live-daily-panel');
 if(daily){{var mobile=window.matchMedia('(max-width:620px)').matches,saved=localStorage.getItem('ems-live-daily-open');if(mobile&&saved!=='1')daily.removeAttribute('open');daily.addEventListener('toggle',function(){{localStorage.setItem('ems-live-daily-open',daily.open?'1':'0');}});}}
 tick();setInterval(tick,{interval_ms});
}})();</script>"""


def _mobile_plot_block(now, has_loads: bool, temp_row: int | None) -> str:
    """Touchfreundliche Einzeldiagramm-Ansicht für schmale Bildschirme."""
    import json as _j
    tabs = [
        ("power", "Leistung", "y"),
        ("soc", "Ladezustand", "y2"),
        ("price", "Strompreis", "y3"),
        ("control", "Steuerung", "y4"),
        ("mode", "Modus", "y5"),
    ]
    if has_loads:
        tabs.append(("loads", "Lasten", "y6"))
    if temp_row is not None:
        tabs.append(("temperature", "Temperaturen", f"y{temp_row}"))
    buttons = "".join(
        f'<button type="button" data-panel="{key}" data-axis="{axis}">{label}</button>'
        for key, label, axis in tabs)
    axes = _j.dumps({key: axis for key, _, axis in tabs})
    now_iso = _j.dumps(pd.Timestamp(now).isoformat())
    return f"""
<section class="mobile-plot-shell" aria-label="Prognose und Steuerung">
 <div class="mobile-plot-toolbar">
  <div class="mobile-plot-tabs" role="tablist">{buttons}</div>
  <div class="horizon-switch" aria-label="Zeitraum">
   <button type="button" data-hours="24">24 h</button><button type="button" data-hours="48">48 h</button>
  </div>
 </div>
 <div id="mobile-plot"></div>
</section>
<script>(function(){{
 var axes={axes}, now={now_iso}, current='power';
 var hours=parseInt(localStorage.getItem('ems-mobile-hours')||'24',10); if(hours!==48)hours=24;
 function source(){{return document.querySelector('.desktop-plot .plotly-graph-div');}}
 function colors(){{var dark=document.documentElement.classList.contains('dark');return dark?{{paper:'#18212b',plot:'#18212b',font:'#e7edf4',grid:'#354352'}}:{{paper:'#fff',plot:'#fff',font:'#20252b',grid:'#e7ebef'}};}}
 function render(){{
  if(!window.matchMedia('(max-width:620px)').matches||!window.Plotly)return;
  var src=source(); if(!src||!src.data)return; var axis=axes[current], c=colors();
  var traces=src.data.filter(function(t){{return (t.yaxis||'y')===axis;}}).map(function(t){{var n=Object.assign({{}},t);n.xaxis='x';n.yaxis='y';
   // Auf dem schmalen Handy-Screen die zweite Prognose-Spalte (progb) wieder in
   // die Prognose-Gruppe legen -> eine kompakte Gruppe statt zwei enger Spalten.
   if(n.legendgroup==='progb'){{n.legendgroup='prog';n.legendgrouptitle=undefined;}}
   return n;}});
  var btn=document.querySelector('.mobile-plot-tabs button[data-panel="'+current+'"]');
  if(!traces.length){{var fallback=document.querySelector('.mobile-plot-tabs button:not([hidden])');if(fallback&&fallback!==btn){{current=fallback.dataset.panel;render();}}return;}}
  var end=new Date(new Date(now).getTime()+hours*3600000).toISOString();
  var srcAxis=src.layout[axis==='y'?'yaxis':'yaxis'+axis.slice(1)]||{{}};
  var layout={{height:420,autosize:true,hovermode:'x unified',separators:',.',showlegend:true,
   paper_bgcolor:c.paper,plot_bgcolor:c.plot,font:{{color:c.font}},margin:{{l:48,r:12,t:18,b:85}},
   legend:{{orientation:'h',x:0,y:-.2,font:{{size:10}}}},
   xaxis:{{range:[now,end],gridcolor:c.grid,tickformat:'%a %H:%M'}},
   yaxis:{{title:srcAxis.title||'',gridcolor:c.grid,zerolinecolor:c.grid}}}};
  if(current==='soc')layout.yaxis.range=[0,101];
  if(current==='loads')layout.yaxis.autorange='reversed';
  Plotly.react('mobile-plot',traces,layout,{{responsive:true,displaylogo:false,displayModeBar:false,scrollZoom:false}});
  document.querySelectorAll('.mobile-plot-tabs button').forEach(function(b){{b.classList.toggle('on',b.dataset.panel===current);}});
  document.querySelectorAll('.horizon-switch button').forEach(function(b){{b.classList.toggle('on',parseInt(b.dataset.hours,10)===hours);}});
 }}
 document.querySelectorAll('.mobile-plot-tabs button').forEach(function(b){{b.addEventListener('click',function(){{current=b.dataset.panel;render();}});}});
 document.querySelectorAll('.horizon-switch button').forEach(function(b){{b.addEventListener('click',function(){{hours=parseInt(b.dataset.hours,10);localStorage.setItem('ems-mobile-hours',hours);render();}});}});
 window.addEventListener('resize',render);window.addEventListener('ems-theme-change',render);setTimeout(render,0);
}})();</script>"""


def _tile(label: str, value: str, sub: str = "", color: str = "") -> str:
    style = f' style="color:{color}"' if color else ""
    return (f'<div class="tile"><div class="v"{style}>{value}</div>'
            f'<div class="l">{label}</div><div class="s">{sub}</div></div>')


def _decision_block(table: pd.DataFrame, now: pd.Timestamp, limit: int = 6) -> str:
    """Naechste zusammenhaengende Planentscheidungen als lesbare Karten."""
    needed = {"mode", "decision_reason"}
    if table.empty or not needed <= set(table.columns):
        return ""
    future = table.loc[table.index >= now].copy()
    future = future[(future["mode"].fillna("auto") != "auto")
                    & future["decision_reason"].fillna("").ne("")]
    if future.empty:
        return ("<details class='decisions'><summary><span class='decision-head'>"
                "<b>Planentscheidungen erklärt</b><small>keine besonderen "
                "Eingriffe</small></span></summary><div class='decision-body'>"
                "<div class='decision-empty'>Keine besonderen Akku-Eingriffe "
                "geplant.</div></div></details>")

    slot = (table.index[1] - table.index[0]
            if len(table.index) > 1 else pd.Timedelta(minutes=15))
    blocks = []
    current = []
    previous = None
    previous_mode = None
    for ts, row in future.iterrows():
        mode = str(row["mode"])
        contiguous = (previous is not None and mode == previous_mode
                      and ts - previous <= slot * 1.5)
        if current and not contiguous:
            blocks.append(current)
            current = []
        current.append((ts, row))
        previous, previous_mode = ts, mode
    if current:
        blocks.append(current)

    cards = []
    for block in blocks[:limit]:
        start, first = block[0]
        end = block[-1][0] + slot
        mode = str(first["mode"])
        label = _MODE_LABEL.get(mode, mode)
        time_text = (f"{_WD[start.weekday()]} {start.strftime('%d.%m. %H:%M')}"
                     f" bis {end.strftime('%H:%M')}")
        reason = _esc(first.get("decision_reason", ""))
        energies = pd.to_numeric(
            pd.Series([r.get("decision_energy_kwh") for _, r in block]),
            errors="coerce")
        values = pd.to_numeric(
            pd.Series([r.get("decision_value_ct") for _, r in block]),
            errors="coerce")
        facts = []
        if energies.notna().any() and float(energies.fillna(0).sum()) >= 0.001:
            facts.append(f"{float(energies.fillna(0).sum()):.2f} kWh")
        if values.notna().any():
            facts.append(
                f"Modellschätzung {float(values.fillna(0).sum()):+.2f} ct")
        if first.get("execution_label"):
            facts.append(str(first.get("execution_label")))
        facts_html = "".join(f"<span>{_esc(v)}</span>" for v in facts)
        color = _MODE_COLOR.get(mode, "#7f8c99")
        cards.append(
            f"<article class='decision-item' style='--decision-color:{color}'>"
            f"<div class='decision-time'>{_esc(time_text)}</div>"
            f"<div class='decision-name'>{_esc(label)}</div>"
            f"<div class='decision-reason'>{reason}</div>"
            f"<div class='decision-facts'>{facts_html}</div></article>")
    count = len(blocks[:limit])
    return ("<details class='decisions'><summary><span class='decision-head'>"
            "<b>Planentscheidungen erklärt</b>"
            f"<small>{count} Entscheidungsblöcke</small></span></summary>"
            "<div class='decision-body'><div class='decision-list'>"
            f"{''.join(cards)}</div></div></details>")


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


def _control_banner(status) -> str:
    """Prominenter Alarm, wenn der E3DC den Steuerbefehl nicht bestätigt."""
    if not status or status.get("ok") is not False:
        return ""
    return ("<div class='banner err'><b>✗ E3DC-Steuer-Ausfall</b><ul><li>"
            f"{_esc(status.get('message', 'Keine Bestätigung vom E3DC.'))}"
            "</li></ul></div>")


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
        '</div>'
        '<script>function emsReport(){'
        "var a=document.createElement('a');a.href='report.json';"
        "a.download='last_run_debug.json';document.body.appendChild(a);a.click();"
        "a.remove();"
        f"setTimeout(function(){{window.location.href={_js_str(mailto)};}},600);"
        '}</script>')


def _js_str(s: str) -> str:
    import json as _j
    return _j.dumps(s)


def _runtime_block(controls_enabled: bool) -> str:
    button = ("<button id='recalc-plan' type='button' onclick='emsRecalc()'>"
              "↻ Plan neu berechnen</button>" if controls_enabled else "")
    return f"""
<section class="runtime-strip" id="runtime-strip">
 <div class="runtime-main"><span class="runtime-dot"></span>
  <div><b id="runtime-phase">EMS-Status wird geladen</b>
  <small id="runtime-message">Verbindung zum Dienst …</small></div></div>
 <div class="runtime-progress"><i id="runtime-progress"></i></div>
 <span id="runtime-meta"></span>{button}
</section>
<script>(function(){{
 let seen=null,busy=false,reloading=false;
 const strip=document.getElementById('runtime-strip'),btn=document.getElementById('recalc-plan');
 function fmt(s){{if(!s)return '';let d=new Date(s);return isNaN(d)?'':d.toLocaleString('de-DE',{{day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'}});}}
 function render(d){{
  strip.dataset.state=d.state||'unknown';
  document.getElementById('runtime-phase').textContent=d.phase||'EMS';
  document.getElementById('runtime-message').textContent=d.message||'';
  document.getElementById('runtime-progress').style.width=Math.max(0,Math.min(100,Number(d.progress)||0))+'%';
  document.getElementById('runtime-meta').textContent=d.state==='ready'?'Plan '+fmt(d.plan_generated)+(d.duration_seconds!=null?' · '+d.duration_seconds.toLocaleString('de-DE')+' s':''):(d.state==='running'?'läuft …':'');
  if(btn)btn.disabled=['queued','running'].includes(d.state);
  if(seen===null)seen=Number(d.sequence)||0;
  if(['queued','running'].includes(d.state))busy=true;
  // Nur neu laden, wenn WIRKLICH ein neuer Plan gelandet ist (Sequenz erhöht) und
  // der Nutzer gerade nichts im Steuerpanel eingibt – sonst würde ein turnusmäßiger
  // Recalc die Seite mitten in einer Eingabe neu laden und Eingaben verwerfen.
  var advanced=(Number(d.sequence)||0)>seen;
  var ae=document.activeElement,editing=ae&&['INPUT','SELECT','TEXTAREA'].includes(ae.tagName);
  if(busy&&d.state==='ready'&&advanced&&!editing&&!reloading){{reloading=true;setTimeout(()=>location.reload(),500);}}
 }}
 window.emsRuntimePoll=function(){{return fetch('api/status.json?_='+Date.now(),{{cache:'no-store'}}).then(r=>{{if(!r.ok)throw Error(r.status);return r.json();}}).then(d=>{{render(d);window.dispatchEvent(new CustomEvent('ems-status',{{detail:d}}));return d;}}).catch(()=>{{strip.dataset.state='error';document.getElementById('runtime-message').textContent='Laufzeitstatus nicht erreichbar';}});}};
 window.emsRecalc=async function(){{if(btn)btn.disabled=true;try{{let r=await fetch('api/control/recalc',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:'{{}}'}});if(!r.ok)throw Error((await r.text()).slice(0,160));busy=true;emsRuntimePoll();}}catch(e){{if(btn)btn.disabled=false;document.getElementById('runtime-message').textContent='Neuberechnung fehlgeschlagen: '+e.message;}}}};
 emsRuntimePoll();setInterval(emsRuntimePoll,2000);
}})();</script>"""


def _slot_detail_block() -> str:
    return """
<details class="info-panel slot-detail" id="slot-detail"><summary>⌖ Slot-Details <small>Kurve anklicken</small></summary>
 <div id="slot-detail-body" class="detail-grid"><p>Wähle einen Zeitpunkt in einem Diagramm aus.</p></div>
</details>
<script>(function(){
 let rows=null;
 const esc=s=>String(s??'–').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
 const num=(v,d=0)=>typeof v==='number'&&isFinite(v)?v.toLocaleString('de-DE',{maximumFractionDigits:d}):'–';
 async function data(){if(rows)return rows;let r=await fetch('api/data.json?_='+Date.now(),{cache:'no-store'});if(!r.ok)throw Error(r.status);rows=await r.json();return rows;}
 function render(x){data().then(a=>{let target=new Date(x).getTime(),best=null,dist=Infinity;a.forEach(r=>{let t=new Date(r.timestamp).getTime(),d=Math.abs(t-target);if(d<dist){dist=d;best=r;}});if(!best)return;let when=new Date(best.timestamp);let items=[['Hauslast',num(best.house_load_w)+' W'],['PV',num(best.pv_w)+' W'],['Preis',num(best.price_ct_kwh,2)+' ct/kWh'],['Akku-SoC',num(best.house_soc_percent,1)+' %'],['Akku laden',num((best.batt_dc_charge_w||0)+(best.batt_ac_charge_w||0))+' W'],['Akku entladen',num(best.batt_discharge_w)+' W'],['Netzbezug',num(best.grid_import_w)+' W'],['Einspeisung',num(best.grid_export_w)+' W'],['Modus',best.mode],['Entscheidung',best.decision_reason],['Ausführung',best.execution_label||'–'],['Ausführungsdetail',best.execution_detail||'–'],['verschobene Energie',num(best.decision_energy_kwh,2)+' kWh'],['Wert',num(best.decision_value_ct,1)+' ct'],['Referenz',best.decision_reference_time?new Date(best.decision_reference_time).toLocaleString('de-DE'):'–']];document.getElementById('slot-detail-body').innerHTML='<h3>'+when.toLocaleString('de-DE',{weekday:'short',day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'})+'</h3>'+items.map(i=>'<div><span>'+esc(i[0])+'</span><b>'+esc(i[1])+'</b></div>').join('');document.getElementById('slot-detail').open=true;}).catch(()=>{document.getElementById('slot-detail-body').innerHTML='<p>Detaildaten sind nicht verfügbar.</p>';});}
 function bind(){document.querySelectorAll('.plotly-graph-div').forEach(p=>{if(p.dataset.emsSlotBound||!p.on)return;p.dataset.emsSlotBound='1';p.on('plotly_click',e=>{let pt=e&&e.points&&e.points[0];if(pt&&pt.x)render(pt.x);});});}
 bind();setInterval(bind,2000);
})();</script>"""


def _events_block() -> str:
    return """
<details class="info-panel events-panel" id="events-panel"><summary>☷ Ereignisse &amp; Bedienverlauf <small>letzte 50 Einträge</small></summary>
 <div id="events-list" class="events-list">wird geladen …</div>
</details>
<script>(function(){
 const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
 async function load(){try{let r=await fetch('api/events.json?_='+Date.now(),{cache:'no-store'});if(!r.ok)throw Error(r.status);let a=(await r.json()).events||[];document.getElementById('events-list').innerHTML=a.length?a.map(e=>'<div class="event '+esc(e.level)+'"><time>'+new Date(e.ts).toLocaleString('de-DE',{day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'})+'</time><span>'+esc(e.message)+'</span></div>').join(''):'<p>Noch keine Ereignisse.</p>';}catch(e){document.getElementById('events-list').textContent='Ereignisverlauf nicht erreichbar.';}}
 document.getElementById('events-panel').addEventListener('toggle',function(){if(this.open)load();});load();setInterval(()=>{if(document.getElementById('events-panel').open)load();},10000);
})();</script>"""


def _savings_history_block() -> str:
    """Ersparnis-Verlauf (validiert gegen die Zähler) – Summe + Wochenreihe.
    Lädt lazy aus /api/savings-history.json beim Aufklappen."""
    return """
<details class="info-panel savings-panel" id="savings-panel"><summary>€ Ersparnis-Verlauf <small>validiert gegen die Zähler</small></summary>
 <div id="savings-summary" class="savings-summary">wird geladen …</div>
 <table id="savings-weekly" class="savings-table"></table>
</details>
<script>(function(){
 const eur=v=>(typeof v==='number'?v.toLocaleString('de-DE',{minimumFractionDigits:2,maximumFractionDigits:2})+' €':'–');
 async function load(){try{let r=await fetch('api/savings-history.json?_='+Date.now(),{cache:'no-store'});if(!r.ok)throw Error(r.status);let d=await r.json();
  document.getElementById('savings-summary').innerHTML='<b>'+eur(d.total_saved_eur)+'</b> gesamt · '+((d.days)||0)+' validierte Tage';
  let w=(d.weekly||[]).slice(-8).reverse();
  document.getElementById('savings-weekly').innerHTML='<tr><th>Woche</th><th>Ersparnis</th><th>Tage</th></tr>'+(w.length?w.map(x=>'<tr><td>'+x.period+'</td><td>'+eur(x.saved_eur)+'</td><td>'+x.days+'</td></tr>').join(''):'<tr><td colspan=3>noch keine Daten</td></tr>');
 }catch(e){document.getElementById('savings-summary').textContent='Ersparnis-Verlauf nicht erreichbar.';}}
 document.getElementById('savings-panel').addEventListener('toggle',function(){if(this.open)load();});
})();</script>"""


def _forecast_accuracy_block() -> str:
    """Prognosegüte (WAPE/Bias) für PV und Hauslast über 7/30 Tage. Lädt lazy
    aus /api/forecast-accuracy.json beim Aufklappen (compare_sources dauert kurz)."""
    return """
<details class="info-panel forecast-accuracy-panel" id="facc-panel"><summary>◴ Prognosegüte <small>PV &amp; Last · WAPE/Bias</small></summary>
 <div id="facc-body">beim Aufklappen wird gemessen …</div>
</details>
<script>(function(){
 const pct=v=>(typeof v==='number'?v.toLocaleString('de-DE',{maximumFractionDigits:1})+' %':'–');
 const w=v=>(typeof v==='number'?Math.round(v)+' W':'–');
 function row(label,m){m=m||{};return '<tr><td>'+label+'</td><td>'+pct(m.wape_pct)+'</td><td>'+w(m.bias_w)+'</td><td>'+((m.n)||0)+'</td></tr>';}
 function tbl(title,a){a=a||{};return '<h4>'+title+'</h4><table class="facc-table"><tr><th></th><th>WAPE</th><th>Bias</th><th>n</th></tr>'+row('PV',a.pv)+row('Last',a.load)+'</table>';}
 async function load(){try{document.getElementById('facc-body').textContent='wird gemessen …';let r=await fetch('api/forecast-accuracy.json?_='+Date.now(),{cache:'no-store'});if(!r.ok)throw Error(r.status);let d=await r.json();document.getElementById('facc-body').innerHTML=tbl('7 Tage',d['7d'])+tbl('30 Tage',d['30d']);}catch(e){document.getElementById('facc-body').textContent='Prognosegüte nicht erreichbar.';}}
 document.getElementById('facc-panel').addEventListener('toggle',function(){if(this.open)load();});
})();</script>"""


def _controls_block(config) -> str:
    """Interaktives Steuerpanel (nur bei dashboard.controls_enabled): Lasten
    an/aus + Kernparameter/Leistungskurve, Optimierungsmodus und Akku-Handbetrieb.
    Sendet an /api/control/* (Basic-Auth wie das Dashboard)."""
    if not getattr(config.dashboard, "controls_enabled", False):
        return ""
    import json as _j
    from .loads import _slug as _lslug

    def _num(idp, val, label, step="1", unit=""):
        return (f"<label class='ctl-field'><span>{label}</span><span class='ctl-input'>"
                f"<input type='number' step='{step}' id='{idp}' value='{val:g}'>"
                f"<small>{unit}</small></span></label>")

    meta, rows = {}, []
    for ld in getattr(config, "controllable_loads", []):
        sg = _lslug(ld.name)
        if ld.type == "thermal":
            keys = ["target_c", "min_c", "max_c", "surface_m2", "solar_absorption"]
            fields = (f"<div class='ctl-grid'>"
                      f"{_num(f'p_{sg}_target_c', ld.target_c, 'Zieltemperatur', '0.1', '°C')}"
                      f"{_num(f'p_{sg}_min_c', ld.min_c, 'Minimum', '0.1', '°C')}"
                      f"{_num(f'p_{sg}_max_c', ld.max_c, 'Maximum', '0.1', '°C')}"
                      f"{_num(f'p_{sg}_surface_m2', ld.surface_m2, 'Solarfläche', '0.1', 'm²')}"
                      f"{_num(f'p_{sg}_solar_absorption', ld.solar_absorption, 'Solar-Wirkungsgrad', '0.05')}"
                      f"</div>")
            type_label = "Thermische Last"
        else:
            keys = ["power_w", "runtime_minutes", "window_from_hour",
                    "window_to_hour", "deadline_hours"]
            profile = ", ".join(f"{float(x):g}" for x in (ld.power_profile_w or []))
            fields = (f"<div class='ctl-grid'>"
                      f"{_num(f'p_{sg}_power_w', ld.power_w, 'Konstante Leistung', '1', 'W')}"
                      f"{_num(f'p_{sg}_runtime_minutes', ld.runtime_minutes, 'Laufzeit', '15', 'min')}"
                      f"{_num(f'p_{sg}_window_from_hour', ld.window_from_hour, 'Fenster von', '1', 'Uhr')}"
                      f"{_num(f'p_{sg}_window_to_hour', ld.window_to_hour, 'Fenster bis', '1', 'Uhr')}"
                      f"{_num(f'p_{sg}_deadline_hours', ld.deadline_hours, 'Spätestens nach', '1', 'h')}"
                      f"</div><div class='profile-edit'>"
                      f"<label for='p_{sg}_power_profile_w'><b>15-Minuten-Leistungskurve</b>"
                      f"<small>Wattwerte wie in power_profile_w, mit Komma oder Leerzeichen getrennt</small></label>"
                      f"<textarea id='p_{sg}_power_profile_w' rows='2' "
                      f"oninput=\"emsCurve('{sg}')\" placeholder='z. B. 2100, 300, 150, 2000'>{profile}</textarea>"
                      f"<div class='curve-box'><canvas id='curve_{sg}' height='92'></canvas>"
                      f"<span id='curve_info_{sg}'></span></div></div>")
            type_label = "Verschiebbare Last"
        meta[sg] = {"name": ld.name, "keys": keys, "profile": ld.type != "thermal"}
        chk = "checked" if ld.enabled else ""
        rows.append(
            f"<article class='load-card'><div class='load-head'><div>"
            f"<b>{_esc(ld.name)}</b><small>{type_label}</small></div>"
            f"<label class='switch'><input type='checkbox' id='en_{sg}' {chk}>"
            f"<span></span><em>aktiv</em></label></div>{fields}"
            f"<div class='load-actions'><button class='primary' "
            f"onclick=\"emsLoad('{sg}')\">Änderungen speichern</button></div></article>")

    strat = getattr(config.optimization, "charge_strategy", "auto")
    mode_text = {
        "auto": ("Automatisch", "situativ früh laden oder Spitzen glätten"),
        "asap": ("Frühestmöglich laden", "verfügbare PV-Energie sofort nutzen"),
        "peak": ("PV-Spitzen glätten", "Akkuladung über den Tag verteilen"),
        "late": ("Spät laden", "maximalen Ziel-SoC möglichst spät erreichen"),
    }
    mode_btns = "".join(
        f"<button class='mode{' on' if strat == m else ''}' "
        f"onclick=\"emsMode('{m}')\"><b>{mode_text[m][0]}</b>"
        f"<small>{mode_text[m][1]}</small></button>"
        for m in ("auto", "asap", "peak", "late"))
    e3dc_on = bool(getattr(getattr(config, "e3dc_rscp", None),
                           "control_enabled", False))
    mqtt_on = bool(getattr(getattr(config, "mqtt", None), "enabled", False))
    e3dc_control = (
        "<section class='ctl-section e3dc-control'><div><b>Direkte E3/DC-Steuerung (RSCP)</b>"
        "<small>Steuert den Akku direkt und prüft die übernommenen Limits. Beim Ausschalten "
        "werden RSCP-Limits freigegeben; MQTT-Sollwerte bleiben davon unabhängig.</small>"
        "<div class='control-channels'><span>RSCP · direkte Akkusteuerung</span>"
        f"<span>MQTT-Sollwerte · {'aktiv' if mqtt_on else 'deaktiviert'}</span></div></div>"
        "<label class='switch control-master'><input type='checkbox' id='e3dc-control-enabled' "
        f"{'checked' if e3dc_on else ''} onchange='emsE3dcControl(this)'>"
        "<span></span><em id='e3dc-control-label'>"
        f"{'aktiv' if e3dc_on else 'ausgeschaltet'}</em></label></section>")
    hb = config.house_battery
    battery = (
        "<section class='battery-planner'><div class='planner-head'><div>"
        "<b>Akkuplanung</b><small>Vorgang sofort starten oder auf dem Zeitstrahl planen</small>"
        "</div><span class='planner-badge' id='schedule-running'>kein Handplan aktiv</span></div>"
        "<div class='planner-form'>"
        "<label><span>Aktion</span><select id='schedule-action' onchange='emsScheduleAction()'>"
        "<option value='grid_charge'>Netzladen</option>"
        "<option value='pv_charge'>Laden</option>"
        "<option value='discharge'>Entladen</option>"
        "<option value='idle'>Idle</option>"
        "</select></label>"
        "<label><span>Start</span><input type='datetime-local' id='schedule-start' step='300'></label>"
        f"<label><span>Leistung</span><span class='ctl-input'><input type='number' "
        f"id='schedule-watts' min='1' max='{max(hb.max_dc_charge_w, hb.max_discharge_w):g}' "
        "value='3000'><small>W</small></span></label>"
        "<label><span>Dauer</span><span class='ctl-input'><input type='number' "
        "id='schedule-duration' min='1' max='1440' value='60'><small>min</small></span></label>"
        "<div class='planner-actions'><button class='primary planner-add' onclick='emsScheduleAdd()'>＋ Einplanen</button>"
        "<button class='instant' onclick=\"emsBat('selected')\">▶ Jetzt starten</button>"
        "<button class='stop' onclick=\"emsBat('auto')\">Auto / Stop</button></div>"
        "</div><div id='schedule-msg' class='schedule-msg'></div>"
        "<div class='schedule-chart-wrap'><div class='schedule-axis' id='schedule-axis'></div>"
        "<div class='schedule-chart' id='schedule-chart'></div>"
        "<div class='schedule-legend'><span class='grid_charge'>■ Netzladen</span>"
        "<span class='pv_charge'>■ Laden</span><span class='discharge'>■ Entladen</span>"
        "<span class='idle'>■ Idle</span><span class='now'>│ Jetzt</span></div></div>"
        "<div class='schedule-list' id='schedule-list'></div></section>")
    compare = (
        "<section class='plan-compare'><div><b>Automatischer Modusvergleich</b>"
        "<small>Drei Ladestrategien mit Karten und Kurven; die aktuelle Auto-Auswahl ist markiert</small></div>"
        "<div id='shadow-result' class='compare-result'>Vergleich wird nach dem nächsten Lauf erstellt.</div>"
        "<div id='shadow-chart-status' class='compare-chart-status'></div>"
        "<div id='shadow-chart' class='compare-chart'></div></section>")

    js = """
const EMS_LOADS=%s;
async function emsPost(action,payload){
  const m=document.getElementById('ctl-msg'); m.textContent='… '+action;
  try{
    const r=await fetch('api/control/'+action,{method:'POST',
      headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)});
    if(!r.ok){ throw new Error((await r.text()).slice(0,200)); }
    m.textContent='✓ übernommen – Neuberechnung wurde vorgemerkt';
    if(window.emsRuntimePoll)window.emsRuntimePoll();
  }catch(e){ m.textContent='✗ '+e.message; }
}
function emsLoad(sg){
  const L=EMS_LOADS[sg], p={};
  L.keys.forEach(k=>{const el=document.getElementById('p_'+sg+'_'+k);
    if(el&&el.value!=='') p[k]=parseFloat(el.value);});
  if(L.profile){
    try{p.power_profile_w=emsProfile(sg);}catch(e){
      const m=document.getElementById('ctl-msg');m.textContent='✗ '+e.message;return;
    }
  }
  emsPost('load',{name:L.name, enabled:document.getElementById('en_'+sg).checked, params:p});
}
function emsProfile(sg){
  const raw=document.getElementById('p_'+sg+'_power_profile_w').value.trim();
  if(!raw)return null;
  const a=raw.split(/[;,\\s]+/).filter(Boolean).map(Number);
  if(!a.length||a.some(v=>!Number.isFinite(v)||v<0||v>100000))
    throw new Error('Leistungskurve: nur positive Wattwerte eingeben.');
  if(a.length>192)throw new Error('Leistungskurve: maximal 192 Werte.');
  return a;
}
function emsCurve(sg){
  const c=document.getElementById('curve_'+sg), info=document.getElementById('curve_info_'+sg);
  if(!c)return;
  let a=[];try{a=emsProfile(sg)||[];}catch(e){info.textContent=e.message;info.className='bad';return;}
  const ratio=window.devicePixelRatio||1,w=Math.max(280,c.clientWidth||500),h=92;
  c.width=w*ratio;c.height=h*ratio;const x=c.getContext('2d');x.scale(ratio,ratio);x.clearRect(0,0,w,h);
  const dark=document.documentElement.classList.contains('dark');
  x.fillStyle=dark?'#18212b':'#f7f9fc';x.fillRect(0,0,w,h);
  if(!a.length){x.fillStyle=dark?'#aeb9c5':'#8a9099';x.font='12px sans-serif';x.fillText('Keine Kurve – konstante Leistung wird verwendet',12,50);info.textContent='';return;}
  const max=Math.max(...a,1),gap=Math.min(3,w/a.length*.18),bw=w/a.length;
  a.forEach((v,i)=>{const bh=(h-20)*v/max;x.fillStyle=dark?'#5da7f0':'#4f83cc';x.fillRect(i*bw+gap/2,h-bh-16,Math.max(1,bw-gap),bh);});
  x.strokeStyle=dark?'#6f8193':'#9ca9ba';x.beginPath();x.moveTo(0,h-15.5);x.lineTo(w,h-15.5);x.stroke();
  info.className='';info.textContent=a.length+' × 15 min = '+(a.length*15)+' min · Spitze '+Math.round(max).toLocaleString('de-DE')+' W';
}
function emsMode(s){ emsPost('mode',{strategy:s}); }
async function emsE3dcControl(el){
  const enabled=el.checked,m=document.getElementById('ctl-msg'),label=document.getElementById('e3dc-control-label');
  if(!enabled&&!confirm('E3/DC-Steuerung wirklich ausschalten? Laufende EMS-Limits werden sofort freigegeben.')){el.checked=true;return;}
  el.disabled=true;m.textContent=enabled?'… E3/DC-Steuerung wird aktiviert':'… E3/DC-Limits werden freigegeben';
  try{const r=await fetch('api/control/e3dc_control',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled})});if(!r.ok)throw Error((await r.text()).replace(/<[^>]+>/g,' ').slice(0,180));const d=await r.json(),verified=d.result.verified!==false;label.textContent=enabled?'aktiv':(verified?'ausgeschaltet':'aus · unbestätigt');m.textContent=(verified?'✓ ':'⚠ ')+(d.result.message||'E3/DC-Steuerung geändert');if(window.emsRuntimePoll)window.emsRuntimePoll();}
  catch(e){el.checked=!enabled;label.textContent=el.checked?'aktiv':'ausgeschaltet';m.textContent='✗ '+e.message;}finally{el.disabled=false;}
}
function emsModeName(k){return({auto:'Automatisch',asap:'Früh laden',peak:'Spitzen glätten',late:'Spät laden'})[k]||k;}
function emsLateBadge(c,f){if(!c)return '';const tip='Ziel '+f(c.target_soc_percent,1)+' %% · Erwartung '+f(c.expected_soc_percent,1)+' %% · P10 '+f(c.p10_soc_percent,1)+' %%';return '<div class="late-confidence '+emsEsc(c.code)+'" title="'+emsEsc(tip)+'"><span>Late-Ziel</span><b>'+emsEsc(c.label)+'</b><small>'+emsEsc(tip)+'</small></div>';}
function emsModeCards(r,buttons){
  const f=(v,d=2)=>v==null?'–':Number(v).toLocaleString('de-DE',{maximumFractionDigits:d});
  return '<div class="mode-compare-grid">'+Object.entries(r.modes).filter(([k])=>k!==\'auto\').map(([k,m])=>'<article class="mode-compare-card '+(k===r.recommended?'recommended ':'')+(m.active?'active ':'')+'"><header><div><b>'+emsModeName(k)+'</b><small>'+(m.active?(r.configured_mode===\'auto\'?\'aktuell automatisch gewählt\':\'aktiver Modus\'):k===r.recommended?'Empfehlung':'Vorschau')+'</small></div>'+(k===r.recommended?'<span class="recommend-badge">empfohlen</span>':'')+'</header><div class="mode-metrics"><div><span>Kosten</span><b>'+f(m.cost_eur)+' €</b></div><div><span>Netzbezug</span><b>'+f(m.grid_import_kwh)+' kWh</b></div><div><span>Einspeisung</span><b>'+f(m.grid_export_kwh)+' kWh</b></div><div><span>Spitze</span><b>'+f(m.peak_export_w,0)+' W</b></div><div><span>Abregelung</span><b>'+f(m.curtailment_kwh)+' kWh</b></div><div><span>Eingriffe</span><b>'+f(m.intervention_slots,0)+' Slots</b></div><div><span>End-SoC</span><b>'+f(m.end_soc_percent,1)+' %%</b></div><div class="wide"><span>Max-SoC</span><b>'+f(m.max_soc_percent,1)+' %% · '+(m.max_soc_at?new Date(m.max_soc_at).toLocaleString('de-DE',{weekday:'short',hour:'2-digit',minute:'2-digit'}):'–')+'</b></div></div><small class="mode-validation">'+f(m.errors,0)+' Fehler · '+f(m.warnings,0)+' Warnungen</small>'+(k==='late'?emsLateBadge(r.late_confidence,f):'')+(buttons&&!m.active?'<button onclick="emsMode(\\\''+k+'\\\')">Diesen Modus übernehmen</button>':'')+'</article>').join('')+'</div>';
}
function emsShadowRender(c){
  const box=document.getElementById('shadow-result');if(!box||!c)return;
  if(c.state==='queued'||c.state==='running'){box.textContent='… '+(c.message||'Schattenvergleich läuft');const chart=document.getElementById('shadow-chart');if(chart)chart.style.display='none';return;}
  if(c.state==='error'){box.textContent='✗ '+c.message;return;}
  if(c.state!=='done'||!c.result)return;
  const r=c.result;
  box.innerHTML='<div class="compare-note">✓ '+emsEsc(c.message)+'</div>'+emsModeCards(r,false)+(r.recommended!==r.active?'<button class="primary shadow-apply" onclick="emsMode(\\''+r.recommended+'\\')">Empfehlung übernehmen</button>':'');
  window.EMS_SHADOW_LAST=r;
  if(window.EMS_SHADOW_CURVE_GEN!==c.generated)emsShadowCurves(c.generated);
}
async function emsShadowCurves(generated){
  const status=document.getElementById('shadow-chart-status');
  if(window.EMS_SHADOW_CURVE_LOADING===generated)return;
  window.EMS_SHADOW_CURVE_LOADING=generated;if(status)status.textContent='… Vergleichskurven werden geladen';
  try{const q=await fetch('api/mode-comparison.json?_='+Date.now(),{cache:'no-store'});if(!q.ok)throw Error(q.status);const d=await q.json();if(d.generated!==generated)throw Error('Kurven gehören noch zum vorherigen Plan');window.EMS_SHADOW_CURVES=d.series;window.EMS_SHADOW_CURVE_GEN=generated;emsShadowChart(d.series);if(status)status.textContent='';}
  catch(e){if(status)status.textContent='Kurven noch nicht verfügbar – werden beim nächsten Statusabruf erneut geladen';}
  finally{window.EMS_SHADOW_CURVE_LOADING=null;}
}
function emsShadowChart(s){
  const chart=document.getElementById('shadow-chart');if(!chart||!s||!window.Plotly)return;
  const controls=document.getElementById('ems-controls');if(controls&&!controls.open)return;
  const dark=document.documentElement.classList.contains('dark'),grid=dark?'#354352':'#e3e8ed',font=dark?'#e7edf4':'#27313a',colors={asap:'#28a261',peak:'#e29a2d',late:'#9b6bd3'},tr=[];Object.entries(s.modes).filter(([k])=>k!=='auto').forEach(([k,v])=>{tr.push({x:s.timestamp,y:v.battery_w,name:emsModeName(k)+' · Akku',line:{color:colors[k]}});tr.push({x:s.timestamp,y:v.soc_percent,name:emsModeName(k)+' · SoC',yaxis:'y2',line:{color:colors[k],dash:'dot'}});tr.push({x:s.timestamp,y:v.grid_w,name:emsModeName(k)+' · Netz',visible:'legendonly',line:{color:colors[k],dash:'dash'}});});chart.style.display='block';Plotly.react(chart,tr,
  {height:380,autosize:true,hovermode:'x unified',paper_bgcolor:'rgba(0,0,0,0)',plot_bgcolor:'rgba(0,0,0,0)',font:{color:font},hoverlabel:{bgcolor:dark?'#202b36':'#ffffff',bordercolor:dark?'#536273':'#cfd7df',font:{color:font}},margin:{l:52,r:48,t:18,b:76},legend:{orientation:'h',x:0,y:-.2,font:{size:10}},xaxis:{gridcolor:grid},yaxis:{title:'Akkuleistung W',gridcolor:grid,zerolinecolor:grid},yaxis2:{title:'SoC %%',overlaying:'y',side:'right',range:[0,100],gridcolor:grid}},{responsive:true,displaylogo:false,displayModeBar:false});}
window.addEventListener('ems-status',e=>emsShadowRender(e.detail.shadow_comparison));
if(window.emsRuntimePoll)window.emsRuntimePoll();
window.addEventListener('ems-theme-change',()=>{if(window.EMS_SHADOW_CURVES)emsShadowChart(window.EMS_SHADOW_CURVES);});
document.getElementById('ems-controls')?.addEventListener('toggle',e=>{if(e.target.open&&window.EMS_SHADOW_CURVES)emsShadowChart(window.EMS_SHADOW_CURVES);});
async function emsBat(a){
  const action=a==='selected'?document.getElementById('schedule-action').value:a,
    watts=parseFloat(document.getElementById('schedule-watts').value),
    minutes=parseFloat(document.getElementById('schedule-duration').value),
    m=document.getElementById('schedule-msg');
  m.textContent='… Handbetrieb wird gesetzt';
  try{
    const r=await fetch('api/control/battery',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({action,watts,minutes})});
    if(!r.ok)throw new Error((await r.text()).replace(/<[^>]+>/g,' ').slice(0,220));
    m.textContent=action==='auto'?'✓ Handbetrieb beendet':'✓ Vorgang sofort gestartet';
    await emsScheduleRefresh();
  }catch(e){m.textContent='✗ '+e.message;}
}
function emsLocalInput(d){
  const p=n=>String(n).padStart(2,'0');
  return d.getFullYear()+'-'+p(d.getMonth()+1)+'-'+p(d.getDate())+'T'+p(d.getHours())+':'+p(d.getMinutes());
}
function emsScheduleDefaults(){
  const e=document.getElementById('schedule-start');if(!e||e.value)return;
  const d=new Date();d.setSeconds(0,0);d.setMinutes(Math.ceil((d.getMinutes()+1)/15)*15);e.value=emsLocalInput(d);
}
function emsScheduleAction(){
  const idle=document.getElementById('schedule-action').value==='idle',
    w=document.getElementById('schedule-watts');
  if(idle){if(Number(w.value)>0)w.dataset.previous=w.value;w.value='0';w.disabled=true;}
  else{w.disabled=false;if(Number(w.value)<=0)w.value=w.dataset.previous||'3000';}
}
async function emsSchedulePost(payload){
  const m=document.getElementById('schedule-msg');m.textContent='… wird gespeichert';
  try{
    const r=await fetch('api/control/battery_schedule',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
    if(!r.ok)throw new Error((await r.text()).replace(/<[^>]+>/g,' ').slice(0,220));
    m.textContent='✓ Plan aktualisiert';await emsScheduleRefresh();
  }catch(e){m.textContent='✗ '+e.message;}
}
function emsScheduleAdd(){
  const start=document.getElementById('schedule-start').value,
    action=document.getElementById('schedule-action').value,
    watts=parseFloat(document.getElementById('schedule-watts').value),
    duration=parseFloat(document.getElementById('schedule-duration').value);
  if(!start||!Number.isFinite(watts)||!Number.isFinite(duration)){
    document.getElementById('schedule-msg').textContent='✗ Bitte alle Planwerte ausfüllen.';return;
  }
  emsSchedulePost({op:'add',start,action,watts,duration_minutes:duration});
}
function emsScheduleCancel(id){
  if(confirm('Geplanten Vorgang #'+id+' wirklich abbrechen?'))emsSchedulePost({op:'cancel',id});
}
function emsScheduleDelete(id){
  if(confirm('Abgelaufenen Planeintrag #'+id+' endgültig löschen?'))emsSchedulePost({op:'delete',id});
}
function emsEsc(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
function emsActionName(a){return ({charge:'Netzladen',grid_charge:'Netzladen',pv_charge:'Laden',discharge:'Entladen',idle:'Idle'})[a]||a;}
function emsScheduleRender(data){
  const entries=data.entries||[], now=new Date(), from=new Date(now);from.setMinutes(0,0,0);
  const until=new Date(from.getTime()+48*3600000), span=until-from;
  const axis=document.getElementById('schedule-axis'), chart=document.getElementById('schedule-chart');
  axis.innerHTML='';chart.innerHTML='';
  [0,12,24,36,48].forEach(h=>{const d=new Date(from.getTime()+h*3600000),x=h/48*100;
    axis.insertAdjacentHTML('beforeend','<span style="left:'+x+'%%">'+d.toLocaleString('de-DE',{weekday:'short',hour:'2-digit',minute:'2-digit'})+'</span>');
    chart.insertAdjacentHTML('beforeend','<i class="schedule-grid" style="left:'+x+'%%"></i>');});
  const nx=Math.max(0,Math.min(100,(now-from)/span*100));chart.insertAdjacentHTML('beforeend','<i class="schedule-now" style="left:'+nx+'%%"></i>');
  entries.filter(e=>['planned','running'].includes(e.status)).forEach(e=>{
    const s=new Date(e.start_ts),en=new Date(e.end_ts);if(en<from||s>until)return;
    const left=Math.max(0,(s-from)/span*100),right=Math.min(100,(en-from)/span*100),w=Math.max(.7,right-left);
    const label=emsActionName(e.action)+(e.action==='idle'?'':' '+Math.round(e.watts).toLocaleString('de-DE')+' W');
    chart.insertAdjacentHTML('beforeend','<div class="schedule-bar '+e.action+' '+e.status+'" style="left:'+left+'%%;width:'+w+'%%" title="'+emsEsc(label)+'">'+emsEsc(label)+'</div>');
  });
  const names={planned:'geplant',running:'läuft',completed:'beendet',cancelled:'abgebrochen',failed:'Fehler',skipped:'übersprungen'};
  document.getElementById('schedule-list').innerHTML=entries.length?entries.slice().reverse().map(e=>{
    const s=new Date(e.start_ts), action=emsActionName(e.action);
    const active=['planned','running'].includes(e.status), actionButton=active
      ?'<button class="stop mini" onclick="emsScheduleCancel('+e.id+')">Abbrechen</button>'
      :'<button class="mini schedule-delete" onclick="emsScheduleDelete('+e.id+')">Löschen</button>';
    return '<div class="schedule-item '+e.status+'"><span class="schedule-color '+e.action+'"></span><div><b>'+action+' · '+Math.round(e.watts).toLocaleString('de-DE')+' W</b><small>'+s.toLocaleString('de-DE',{weekday:'short',day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'})+' · '+Number(e.duration_minutes).toLocaleString('de-DE')+' min</small></div><span class="schedule-state">'+emsEsc(names[e.status]||e.status)+(e.note?' · '+emsEsc(e.note):'')+'</span>'+actionButton+'</div>';
  }).join(''):'<div class="schedule-empty">Noch keine manuellen Vorgänge geplant.</div>';
  const man=data.manual||{}, badge=document.getElementById('schedule-running');
  badge.textContent=man.active?(emsActionName(man.action)+(man.action==='idle'?' aktiv':' '+Math.round(man.watts).toLocaleString('de-DE')+' W aktiv')):'kein Handplan aktiv';
  badge.classList.toggle('active',!!man.active);
}
async function emsScheduleRefresh(){
  try{const r=await fetch('api/battery-schedule.json?_='+Date.now(),{cache:'no-store'});if(!r.ok)throw Error(r.status);emsScheduleRender(await r.json());}
  catch(e){const m=document.getElementById('schedule-msg');if(m)m.textContent='Zeitplanung nicht erreichbar ('+e.message+')';}
}
document.getElementById('ems-controls')?.addEventListener('toggle',function(){
  if(this.open){Object.keys(EMS_LOADS).forEach(s=>{if(EMS_LOADS[s].profile)emsCurve(s);});emsScheduleDefaults();emsScheduleRefresh();}
});
emsScheduleDefaults();emsScheduleAction();emsScheduleRefresh();setInterval(emsScheduleRefresh,5000);
window.addEventListener('ems-theme-change',()=>Object.keys(EMS_LOADS).forEach(s=>{if(EMS_LOADS[s].profile)emsCurve(s);}));
""" % _j.dumps(meta)

    return (
        "<details class='controls' id='ems-controls'><summary>"
        "<span class='ctl-title'>⚙ Steuerung</span>"
        "<span class='ctl-summary'>Lasten, Optimierungsmodus und Akku-Handbetrieb</span>"
        "<span class='ctl-chevron'>⌄</span></summary><div class='ctl-body'>"
        "<div class='ctl-section-head'><b>Steuerbare Lasten</b>"
        "<small>Parameter und zeitlicher Leistungsverlauf</small></div>"
        f"<div class='load-cards'>{''.join(rows)}</div>"
        f"{e3dc_control}"
        f"<div class='ctl-section mode-select'><div><b>Optimierungsmodus</b>"
        f"<small>Ladestrategie des Hausakkus</small></div><div class='button-group'>{mode_btns}</div></div>"
        f"{compare}{battery}<div id='ctl-msg' class='ctl-msg'></div>"
        f"</div></details><script>{js}</script>")


def _sources_block(source_status) -> str:
    """Frische-Chips der externen Datenquellen (Spotpreis/Wetter/Solcast):
    grün = frisch, gelb = älter als erwartet, rot = veraltet/fehlend -
    macht sichtbar, wenn still auf Cache/Schätzung optimiert wird."""
    if not source_status:
        return ""
    col = {"ok": "#2ca02c", "warn": "#e6a700", "err": "#d62728"}
    chips = "".join(
        f'<span class="chip"><span class="dot" style="background:'
        f'{col.get(s.get("level"), "#999")}"></span>{s.get("name")}: '
        f'{s.get("detail", "")}</span>'
        for s in source_status)
    return f'<div class="chips">Datenquellen: {chips}</div>'


_LEVEL_RANK = {"current": 0, "partial": 1, "replaced": 2}


def _panel_level(levels) -> str:
    """Schlechtester Level einer Panel-Kartengruppe (für die Farbe am Summary,
    auch im eingeklappten Zustand). Reihenfolge current < partial < replaced."""
    worst, rank = "current", -1
    for lv in levels:
        r = _LEVEL_RANK.get(lv, 1)
        if r > rank:
            rank, worst = r, (lv if lv in _LEVEL_RANK else "partial")
    return worst


def _forecast_quality_block(quality, timezone="Europe/Berlin") -> str:
    """Operative Prognosequalität je Quelle und aktuellem Horizont."""
    if not quality:
        return ""
    items = []
    levels = []
    for source in quality:
        level = source.get("level", "replaced")
        levels.append(level)
        issue = source.get("issued_at")
        issue_text = ""
        if issue:
            try:
                stamp = pd.Timestamp(issue)
                if stamp.tzinfo is not None:
                    stamp = stamp.tz_convert(timezone)
                issue_text = f" · erstellt {stamp.strftime('%d.%m. %H:%M')}"
            except Exception:
                issue_text = f" · erstellt {_esc(issue)}"
        items.append(
            f"<article class='quality-item {level}'>"
            f"<div class='quality-source'>{_esc(source.get('name', 'Quelle'))}</div>"
            f"<div class='quality-state'>{_esc(source.get('state', 'unbekannt'))}</div>"
            f"<div class='quality-detail'>{_esc(source.get('detail', ''))}"
            f"{issue_text}</div>"
            "</article>")
    return (f"<details class='forecast-quality lvl-{_panel_level(levels)}'><summary>"
            "<span>Prognosequalität</span><small>verwendete Daten im aktuellen "
            "Optimierungshorizont</small></summary><div class='quality-grid'>"
            f"{''.join(items)}</div></details>")


def _operations_block(solver, execution) -> str:
    """Kompakte, standardmaessig eingeklappte Betriebsdiagnose."""
    if not solver and not execution:
        return ""
    cards = []
    levels = []
    if solver:
        levels.append("replaced" if solver.get("slow") else "current")
        typical = solver.get("median_seconds")
        detail = (f"typisch {typical:.1f} s" if typical is not None
                  else "Historie wird aufgebaut")
        gap = solver.get("mip_gap")
        cards.append(
            "<article class='quality-item %s'><div class='quality-source'>Solver</div>"
            "<div class='quality-state'>%.1f s + %.1f s Politur</div>"
            "<div class='quality-detail'>%s · %s · %s Variablen, %s binär, %s Regeln%s"
            "</div></article>" % (
                "replaced" if solver.get("slow") else "current",
                solver.get("seconds", 0.0), solver.get("polish_seconds", 0.0),
                _esc(detail), "Warmstart" if solver.get("warm_start") else "Kaltstart",
                f"{solver.get('variables', 0):,}".replace(",", "."),
                f"{solver.get('binaries', 0):,}".replace(",", "."),
                f"{solver.get('constraints', 0):,}".replace(",", "."),
                " · Gap %.3g" % gap if gap is not None else ""))
    if execution:
        levels.append("current" if execution.get("ok") else "partial")
        planned, actual = execution.get("planned", {}), execution.get("actual", {})
        def _w(value):
            return "–" if value is None else f"{value:,.0f} W".replace(",", ".")
        detail = (f"Netz {_w(planned.get('grid_w'))} → {_w(actual.get('grid_w'))} · "
                  f"Akku {_w(planned.get('battery_w'))} → {_w(actual.get('battery_w'))} · "
                  f"SoC {planned.get('soc', '–')} → {actual.get('soc', '–')} %")
        deviations = execution.get("deviations", {})
        action = execution.get("battery_action") or {}
        if action:
            detail += (f" · Aktion {action.get('planned', '–')} → "
                       f"{action.get('actual', '–')}")
        if deviations.get("battery_energy_kwh") is not None:
            detail += (f" · Energieabweichung Akku "
                       f"{float(deviations['battery_energy_kwh']):+.3f} kWh")
        if execution.get("export_limit_ok") is not None:
            detail += (" · Einspeisegrenze eingehalten" if execution.get(
                "export_limit_ok") else " · Einspeisegrenze überschritten")
        cause = {"device": "Gerät", "forecast": "Prognose", "model": "Modell",
                 "data": "Daten", "none": "keine"}.get(
                     execution.get("cause"), execution.get("cause", "–"))
        detail += f" · Ursache: {cause}"
        cards.append(
            f"<article class='quality-item {'current' if execution.get('ok') else 'partial'}'>"
            "<div class='quality-source'>Plan-Ausführung</div>"
            f"<div class='quality-state'>{_esc(execution.get('message', ''))}</div>"
            f"<div class='quality-detail'>{_esc(detail)}</div></article>")
    return (f"<details class='forecast-quality lvl-{_panel_level(levels)}'><summary>"
            "<span>Betriebsdiagnose</span>"
            "<small>Solver und Ergebnisprüfung abgeschlossener Slots</small></summary>"
            f"<div class='quality-grid'>{''.join(cards)}</div></details>")


def _thermal_feedback_block(feedback, calibrations) -> str:
    if not feedback and not calibrations:
        return ""
    cards = []
    levels = []
    for item in feedback or []:
        if not item.get("configured"):
            state, level, detail = "nicht konfiguriert", "partial", (
                "feedback_topic oder power_topic ergänzen")
        elif item.get("fresh"):
            state, level = ("läuft" if item.get("on") else "aus"), "current"
            power = item.get("power_w")
            age = item.get("age_seconds")
            detail = ((f"{power:,.0f} W".replace(",", ".")
                       if power is not None else "Statusrückmeldung")
                      + (f" · vor {age:.0f} s" if age is not None else ""))
        else:
            state, level, detail = "Rückmeldung veraltet", "replaced", "keine frischen Istwerte"
        levels.append(level)
        cards.append(
            f"<article class='quality-item {level}'><div class='quality-source'>"
            f"{_esc(item.get('label', 'Wärmepumpe'))}</div>"
            f"<div class='quality-state'>{state}</div>"
            f"<div class='quality-detail'>{detail}</div></article>")
    for cal in calibrations or []:
        applied = cal.get("applied") or {}
        state = ("automatisch übernommen" if applied else
                 ("Qualität reicht noch nicht" if cal.get("status") != "applied"
                  else "geprüft"))
        level = "current" if applied else "partial"
        levels.append(level)
        r2 = cal.get("r2")
        detail = (f"{cal.get('n_windows') or 0} Fenster"
                  + (f" · R² {r2:.2f}" if r2 is not None else "")
                  + (f" · {cal.get('message')}" if cal.get("message") else ""))
        cards.append(
            f"<article class='quality-item {level}'><div class='quality-source'>"
            f"Thermomodell {_esc(cal.get('name', ''))}</div>"
            f"<div class='quality-state'>{state}</div>"
            f"<div class='quality-detail'>{_esc(detail)}</div></article>")
    return (f"<details class='forecast-quality lvl-{_panel_level(levels)}'><summary>"
            "<span>Pool-Rückkopplung"
            "</span><small>reale Wärmepumpen und Thermomodell</small></summary>"
            f"<div class='quality-grid'>{''.join(cards)}</div></details>")


def build_dashboard(config: Config, table: pd.DataFrame, total_cost_ct: float,
                    export_line_w=None, savings_eur=None, violations=None,
                    load_temp_actual=None, ambient_temp_c=None,
                    source_status=None, pv_compare=None,
                    control_status=None, forecast_quality=None,
                    solver_status=None, execution_status=None,
                    load_feedback_status=None,
                    thermal_calibration=None) -> str:
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
        titles.append("<b>Temperaturen</b>")
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
                hovertemplate=hover, legendrank=_GROUP_RANK.get(group, 1000),
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
                                 name="PV (80%)", legendgroup="prog",
                                 hoverinfo="skip"), row=1, col=1)
    line("actual_pv_w", "PV (Ist)", "#ff7f0e", 1, "ist")
    line("pv_w", "PV (Prog.)", "#ff7f0e", 1, "prog", dash="dash")
    # Vergleichs-Overlay: pvlib-Modell (shadow) neben der aktiven Prognose,
    # zum Bewerten Solcast vs. pv_model. Nur wenn Vergleichsdaten vorliegen.
    if pv_compare is not None and len(pv_compare) > 0:
        pvc = pv_compare.reindex(x)
        if pvc.notna().any():
            fig.add_trace(go.Scatter(
                x=x, y=pvc, name="PV (Model)", mode="lines",
                line=dict(color="#8c564b", width=2, dash="dot"),
                hovertemplate=HOVER_W, legendgroup="prog",
                legendgrouptitle_text=_GROUPS["prog"]), row=1, col=1)
    line("actual_load_w", "Verbrauch (Ist)", "#d62728", 1, "ist")
    if ({"house_load_p10_w", "house_load_p90_w"} <= set(t.columns)
            and t["house_load_p10_w"].notna().any()):
        fig.add_trace(go.Scatter(
            x=x, y=t["house_load_p90_w"], mode="lines",
            line=dict(width=0), legendgroup="prog", showlegend=False,
            hoverinfo="skip"), row=1, col=1)
        fig.add_trace(go.Scatter(
            x=x, y=t["house_load_p10_w"], mode="lines",
            line=dict(width=0), fill="tonexty",
            fillcolor="rgba(214,39,40,0.10)", name="Verbrauch (80%)",
            legendgroup="prog", hoverinfo="skip"), row=1, col=1)
    line("house_load_w", "Verbrauch (Prog.)", "#d62728", 1, "prog", dash="dash")
    # Steuerbare Lasten (Pool etc.): geplante Gesamt-Leistung als eigener Verlauf.
    if has_loads:
        _cl_cols = [c for c in t.columns if c.startswith("load_") and c.endswith("_w")]
        if _cl_cols:
            cl_sum = t[_cl_cols].sum(axis=1)
            if float(cl_sum.abs().sum()) > 0:
                fig.add_trace(go.Scatter(
                    x=x, y=cl_sum, name="Steuerb. Lasten", mode="lines",
                    line=dict(color="#9467bd", width=1.6, dash="dot"),
                    hovertemplate=HOVER_W, legendgroup="prog",
                    legendgrouptitle_text=_GROUPS["prog"]), row=1, col=1)
    line("actual_grid_w", "Netz (Ist)", "#1f77b4", 1, "ist", width=1.8)
    if "grid_import_w" in t.columns and "grid_export_w" in t.columns:
        net = t["grid_import_w"].fillna(0) - t["grid_export_w"].fillna(0)
        net = net.where(t["grid_import_w"].notna() | t["grid_export_w"].notna())
        fig.add_trace(go.Scatter(x=x, y=net, name="Netz (Prog.)", mode="lines",
                                 line=dict(color="#1f77b4", width=1.5, dash="dot"),
                                 hovertemplate=HOVER_W, legendrank=_GROUP_RANK["progb"],
                                 legendgroup="progb",
                                 legendgrouptitle_text=_GROUPS["progb"]), row=1, col=1)
    if "export_line_w" in t.columns and t["export_line_w"].notna().any():
        line("export_line_w", "Einspeise-Linie", "#2ca02c", 1, "progb",
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
    line("house_soc_percent", "Haus-SoC (Prog.)", "#111111", 2, "soc",
         dash="dash", width=2.5, hover=HOVER_PCT)
    line("car_soc_percent", "Auto-SoC", "#9467bd", 2, "soc", dash="dot",
         hover=HOVER_PCT)

    # ---------- Panel 3: Preis + Vergütung ----------
    if "price_ct_kwh" in t.columns:
        price = t["price_ct_kwh"]
        est = (t["price_estimated"].fillna(0) > 0.5) \
            if "price_estimated" in t.columns else pd.Series(False, index=x)
        fig.add_trace(go.Scatter(x=x, y=price.mask(est), name="Börsenpreis",
                                 mode="lines", legendgroup="progb",
                                 legendrank=_GROUP_RANK["progb"],
                                 legendgrouptitle_text=_GROUPS["progb"],
                                 hovertemplate=HOVER_CT,
                                 line=dict(color="#8c564b", width=2, shape="hv")),
                      row=3, col=1)
        if est.any():
            fig.add_trace(go.Scatter(
                x=x, y=price.where(est | est.shift(-1, fill_value=False)),
                name="Preis (Schätzung)", mode="lines", legendgroup="progb",
                legendrank=_GROUP_RANK["progb"],
                legendgrouptitle_text=_GROUPS["progb"], hovertemplate=HOVER_CT,
                line=dict(color="#8c564b", width=2, shape="hv", dash="dash")),
                row=3, col=1)
    line("feedin_ct_kwh", "Einspeisevergütung", "#2ca02c", 3, "progb",
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
        # "peak" und "late" sind geformtes Normalverhalten, keine Störungen
        n_eingriffe = int((~modes.isin(["auto", "peak", "late"])).sum())
        z = [[_MODES.index(m) if m in _MODES else 0 for m in modes]]
        colorscale = []
        for i, m in enumerate(_MODES):
            colorscale += [[i / len(_MODES), _MODE_COLOR[m]],
                           [(i + 1) / len(_MODES), _MODE_COLOR[m]]]
        mode_hover = []
        for i, m in enumerate(modes):
            text = _MODE_LABEL.get(m, m)
            reason = (str(t.iloc[i].get("decision_reason", "") or "")
                      if "decision_reason" in t.columns else "")
            e = t.iloc[i].get("decision_energy_kwh", float("nan"))
            v = t.iloc[i].get("decision_value_ct", float("nan"))
            if reason:
                text += f"<br>{reason}"
            if pd.notna(e) and float(e) >= 0.001:
                text += f"<br>Energie: {float(e):.3f} kWh"
            if pd.notna(v):
                text += f"<br>Modellschätzung: {float(v):+.2f} ct"
            mode_hover.append(text)
        fig.add_trace(go.Heatmap(
            x=x, y=[""], z=z, zmin=-0.5, zmax=len(_MODES) - 0.5,
            colorscale=colorscale, showscale=False, meta="mode_timeline",
            customdata=[mode_hover],
            hovertemplate="%{x|%H:%M}<br>%{customdata}<extra>Entscheidung</extra>"),
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
            meta="load_timeline",
            colorscale=[[0.0, "#e9ecef"], [0.33, "#e9ecef"],      # 0 = aus
                        [0.34, "#2ca02c"], [0.66, "#2ca02c"],     # 1 = AN
                        [0.67, "#adb5bd"], [1.0, "#adb5bd"]],     # 2 = deaktiviert
            customdata=[[_lab[v] for v in row] for row in z],
            hovertemplate="%{y}  %{x|%H:%M} – %{customdata}<extra></extra>"),
            row=6, col=1)

    # ---------- Panel 7: Temperaturen (Pool erwartet/echt, Außentemperatur) ---
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
        # Außentemperatur (Open-Meteo, Ist+Prognose) als Referenz - erklärt
        # Wärmeverlust/-eintrag der Pooltemperatur mit.
        if ambient_temp_c is not None and len(ambient_temp_c) > 0:
            amb = ambient_temp_c.dropna()
            if len(amb) > 0:
                fig.add_trace(go.Scatter(
                    x=amb.index, y=amb.values, name="Außentemperatur", mode="lines",
                    line=dict(color="#7f7f7f", width=1.5, dash="dot"),
                    hovertemplate="%{y:.1f} °C", legendgroup="temp",
                    legendgrouptitle_text="Temperatur"), row=temp_row, col=1)

    # ---------- Orientierung: Vergangenheit, Jetzt, Tagesgrenzen ----------
    if x[0] < now:
        fig.add_vrect(x0=x[0], x1=min(now, x[-1]), fillcolor="rgba(0,0,0,0.05)",
                      line_width=0, layer="below", row="all", col=1)
    fig.add_vline(x=now, line=dict(color="#0d6efd", width=2), row="all", col=1)
    # "Jetzt"-Label in den freien oberen Rand setzen. Innerhalb von Panel 1
    # verdeckte die Box genau am aktuellen Zeitpunkt Leistungs-/PV-Kurven.
    # Die senkrechte Linie bleibt im Plot die eindeutige Zeitmarkierung.
    fig.add_annotation(x=now, y=1.01, xref="x", yref="paper",
                       yanchor="bottom",
                       text=f"▼ Jetzt {now.strftime('%H:%M')}", showarrow=False,
                       font=dict(color="#0d6efd", size=11))
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
    fig.add_annotation(xref="paper", yref="paper", x=0, y=-0.085,
                       xanchor="left", yanchor="top", showarrow=False,
                       text=mode_leg, font=dict(size=11, color="#555"))
    fig.update_layout(
        height=(1120 if has_loads else 980) + (180 if temp_row else 0),
        autosize=True, template="plotly_white",
        hovermode="x unified", barmode="relative", bargap=0,
        # Deutsche Zahlenformate in Hover/Achsen: Dezimal-Komma, Tausender-Punkt
        separators=",.",
        # Großer Fußbereich: die (im Quermodus mehrzeilig umbrechende) Legende und
        # die Modus-Zeile müssen UNTER dem letzten Panel Platz haben, sonst
        # überlappen sie auf dem Handy das unterste Panel (steuerbare Lasten).
        margin=dict(l=60, r=30, t=80, b=210),
        legend=dict(orientation="h", yanchor="top", y=-0.135, xanchor="left",
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
    # Gegen die echten E3DC-Zähler validierte, kumulierte Ersparnis (nächtlich
    # via savings_check.py --persist). Bestätigt das Live-Tracking unabhängig.
    validated_note = "vs. ohne EMS"
    try:
        from .local_history import read_savings_validated
        _vdf = read_savings_validated(config.e3dc_rscp.history_db_path)
        if not _vdf.empty:
            _vtot = float(_vdf["saved_eur"].sum())
            validated_note = (f"vs. ohne EMS · {_vtot:+.2f} € an Zählern "
                              f"bestätigt ({len(_vdf)} T)")
    except Exception:
        pass
    # Der Akku-SoC steht bereits in den E3/DC-Live-Kacheln (Echtzeit). Die
    # KPI-Kachel dafür nur zeigen, wenn die Live-Kacheln AUS sind - sonst wäre
    # sie redundant.
    live_active = float(getattr(
        config.dashboard, "live_refresh_seconds", 5.0) or 0.0) > 0.0
    tiles = [
        _tile("Netto-Kosten Horizont", f"{total_cost_ct / 100:.2f} €",
              f"bis {_WD[x[-1].weekday()]} {x[-1].strftime('%d.%m.')}"),
        _tile("Ersparnis gesamt",
              "–" if savings_eur is None else f"{savings_eur:.2f} €",
              validated_note),
    ]
    if not live_active:
        tiles.append(_tile(
            "Akku-SoC", "–" if pd.isna(soc_now) else f"{soc_now:.0f} %",
            f"{config.house_battery.capacity_wh / 1000:.0f} kWh Speicher"))
    tiles += [
        _tile("Modus jetzt", _MODE_LABEL.get(mode_now, mode_now),
              "" if pd.isna(dis_lim) else
              f"Limit Laden {ch_lim:,.0f} W · Entladen {dis_lim:,.0f} W"
              .replace(",", ".")),
        _tile("Eingriffe im Plan", f"{n_eingriffe}", "Slots ≠ auto"),
    ]
    # (Planprüfung wird als eigenes Banner gezeigt - _alert_banner -, daher hier
    # KEINE zusätzliche KPI-Kachel, um die Doppelanzeige zu vermeiden.)
    if control_status:
        ok = control_status.get("ok")
        state = control_status.get("state", "unknown")
        if ok is True:
            value, color = "✓ bestätigt", "#2ca02c"
        elif ok is False:
            value, color = "✗ ausgefallen", "#d62728"
        elif state == "manual":
            value, color = "Handbetrieb", "#e6a700"
        else:
            value, color = "nicht geprüft", "#777"
        tiles.append(_tile("E3DC-Steuerung", value,
                           _esc(control_status.get("message", "")), color=color))

    plot_html = fig.to_html(full_html=False, include_plotlyjs=False,
                            default_width="100%",
                            config={"responsive": True, "displaylogo": False,
                                    "modeBarButtonsToRemove": [
                                        "select2d", "lasso2d", "autoScale2d",
                                        "toggleSpikelines",
                                        "hoverClosestCartesian",
                                        "hoverCompareCartesian"]})
    report_html = _report_block(config, now, violations)
    controls_html = _controls_block(config)
    runtime_html = _runtime_block(bool(getattr(config.dashboard, "controls_enabled", False)))
    live_html = _live_block(config)
    decision_html = _decision_block(t, now)
    mobile_plot_html = _mobile_plot_block(now, has_loads, temp_row)
    html = f"""<!DOCTYPE html>
<html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="#1769c2">
<meta name="apple-mobile-web-app-capable" content="yes">
<link rel="manifest" href="/manifest.webmanifest">
<link rel="icon" href="/app-icon.svg" type="image/svg+xml">
<title>E3DC EMS Steuerung</title>
<script>(function(){{var saved=localStorage.getItem('ems-theme');var dark=saved==='dark'||(!saved&&matchMedia('(prefers-color-scheme:dark)').matches);document.documentElement.classList.toggle('dark',dark);}})();</script>
<script src="plotly.min.js"></script>
<style>
 html {{ background: #eef3f8; }}
 body {{ font-family: -apple-system, 'Segoe UI', Roboto, sans-serif;
        max-width: 1800px; margin: 0 auto; padding: 16px; color: #20252b;
        background: linear-gradient(145deg,#f7f9fc 0,#eef3f8 100%); }}
 .app-header {{ display: flex; align-items: center; gap: 12px; margin: 0 0 14px;
        padding: 13px 16px; background: #fff; border: 1px solid #e0e5eb;
        border-radius: 12px; box-shadow: 0 3px 14px rgba(28,45,68,.07); }}
 h1 {{ flex: 1; font-size: 22px; margin: 0;
        min-width: 0; }}
 h1 .ts {{ color: #888; font-weight: normal; font-size: 14px; }}
 .header-actions {{ display: flex; gap: 7px; }}
 .header-actions button {{ min-width: 42px; min-height: 38px; padding: 7px 10px;
        border: 1px solid #ccd4dc; border-radius: 8px; background: #f4f6f8;
        color: #26313c; cursor: pointer; font: inherit; font-size: 13px; }}
 #install-app {{ display: none; }}
 .runtime-strip {{ display: grid; grid-template-columns: minmax(220px,1fr) minmax(120px,2fr) auto auto;
        align-items: center; gap: 12px; margin: -4px 0 12px; padding: 9px 12px;
        border: 1px solid #dbe3eb; border-radius: 10px; background: #fff; font-size: 12px; }}
 .runtime-main {{ display: flex; align-items: center; gap: 9px; min-width: 0; }}
 .runtime-main small {{ display: block; color: #6e7781; margin-top: 2px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
 .runtime-dot {{ width: 10px; height: 10px; border-radius: 50%; background: #7b8794; flex: 0 0 auto; }}
 .runtime-strip[data-state=ready] .runtime-dot {{ background: #2a9d55; }}
 .runtime-strip[data-state=running] .runtime-dot, .runtime-strip[data-state=queued] .runtime-dot {{ background: #2678c8; animation: runtimePulse 1.2s infinite; }}
 .runtime-strip[data-state=error] .runtime-dot {{ background: #d13a32; }}
 @keyframes runtimePulse {{ 50% {{ opacity: .35; }} }}
 .runtime-progress {{ height: 7px; border-radius: 7px; background: #e5eaf0; overflow: hidden; }}
 .runtime-progress i {{ display: block; width: 0; height: 100%; background: #2678c8; transition: width .25s; }}
 #runtime-meta {{ color: #68727c; white-space: nowrap; }}
 #recalc-plan {{ padding: 7px 11px; border: 1px solid #a9bdd1; border-radius: 8px; background: #edf5fd; color: #155c9f; cursor: pointer; font: inherit; }}
 #recalc-plan:disabled {{ opacity: .55; cursor: wait; }}
 .tiles {{ display: flex; gap: 10px; flex-wrap: wrap; margin-bottom: 10px; }}
 .tile {{ flex: 1 1 150px; background: #fff; border: 1px solid #e0e5eb;
         border-radius: 11px; padding: 11px 14px; box-shadow: 0 2px 8px rgba(25,42,65,.05); }}
 .tile .v {{ font-size: 22px; font-weight: 700; }}
 .tile .l {{ font-size: 12px; color: #555; margin-top: 2px; }}
 .tile .s {{ font-size: 11px; color: #999; }}
 .live-panel {{ margin-bottom: 12px; }}
 .live-head {{ display: flex; justify-content: space-between; align-items: center;
        margin: 2px 2px 7px; font-size: 14px; }}
 .live-head #live-status {{ color: #666; font-size: 12px; font-weight: normal; }}
 .live-daily-panel {{ margin-top: 10px; }}
 .live-daily-panel > summary {{ display: flex; align-items: baseline; gap: 8px;
        margin: 0 2px 7px; cursor: pointer; list-style: none; }}
 .live-daily-panel > summary::-webkit-details-marker {{ display: none; }}
 .live-daily-panel > summary::after {{ content: '⌄'; margin-left: auto; color: #687582;
        transition: transform .2s; }}
 .live-daily-panel:not([open]) > summary::after {{ transform: rotate(-90deg); }}
 .live-daily-panel > summary span {{ color: #707983; font-size: 11px; }}
 #live-day-status[data-quality=current] {{ color: #247b45; }}
 #live-day-status[data-quality=warning] {{ color: #9a6b00; }}
 #live-day-status[data-quality=stale] {{ color: #b3261e; font-weight: 700; }}
 .live-tiles {{ margin-bottom: 0; }}
 .live-daily {{ display: grid; grid-template-columns: repeat(5,minmax(0,1fr)); gap: 10px; }}
 .live-daily .tile {{ max-width: none; }}
 /* Live-Kacheln nach oben begrenzen: volle Zeilen füllen weiter gleichmäßig,
    aber eine einzelne Kachel in der letzten Zeile wird nicht mehr über die ganze
    Breite gezogen (flex-grow). */
 .live-tiles .tile {{ max-width: 320px; }}
 .live-tiles .tile {{ transition: background-color .25s, border-color .25s,
        color .25s; }}
 .live-tiles .live-solar {{ background: #fff8d8; border-color: #efd86e; }}
 .live-tiles .live-solar .v {{ color: #9a6b00; }}
 .live-tiles .live-house {{ background: #f5f0ff; border-color: #d9c9f3; }}
 .live-tiles .live-house .v {{ color: #65439b; }}
 .live-tiles .live-soc {{ background: #eaf8f7; border-color: #b8dedb; }}
 .live-tiles .live-soc .v {{ color: #147a74; }}
 .live-tiles .live-wallbox {{ background: #eef5ff; border-color: #c5d9f5; }}
 .live-tiles .live-wallbox .v {{ color: #285f9e; }}
 .live-tiles .live-temp {{ background: #fff4ec; border-color: #f0cdb0; }}
 .live-tiles .live-temp .v {{ color: #b5642a; }}
 .live-tiles .daily-deviation.positive, .live-tiles .daily-balance.ok {{ background: #eaf8ee; border-color: #b8dfc3; }}
 .live-tiles .daily-deviation.positive .v, .live-tiles .daily-balance.ok .v {{ color: #237a3b; }}
 .live-tiles .daily-deviation.negative, .live-tiles .daily-balance.bad {{ background: #fff3dd; border-color: #eccb82; }}
 .live-tiles .daily-deviation.negative .v, .live-tiles .daily-balance.bad .v {{ color: #9a6100; }}
 .live-tiles .flow-import {{ background: #fdecec; border-color: #efb6b6; }}
 .live-tiles .flow-import .v {{ color: #b3261e; }}
 .live-tiles .flow-export {{ background: #eaf8ee; border-color: #b8dfc3; }}
 .live-tiles .flow-export .v {{ color: #237a3b; }}
 .live-tiles .flow-charge {{ background: #eaf3ff; border-color: #b8d2f2; }}
 .live-tiles .flow-charge .v {{ color: #2468a9; }}
 .live-tiles .flow-discharge {{ background: #fff1e5; border-color: #edc59f; }}
 .live-tiles .flow-discharge .v {{ color: #b45f16; }}
 .live-tiles .flow-idle {{ background: #f4f5f6; border-color: #d7dadd; }}
 .live-tiles .flow-idle .v {{ color: #62676d; }}
 .live-tiles .daily-import {{ background: #fdecec; border-color: #efb6b6; }}
 .live-tiles .daily-import .v {{ color: #b3261e; }}
 .live-tiles .daily-export {{ background: #eaf8ee; border-color: #b8dfc3; }}
 .live-tiles .daily-export .v {{ color: #237a3b; }}
 .live-tiles .daily-charge {{ background: #eaf3ff; border-color: #b8d2f2; }}
 .live-tiles .daily-charge .v {{ color: #2468a9; }}
 .live-tiles .daily-discharge {{ background: #edf9ef; border-color: #bddfc5; }}
 .live-tiles .daily-discharge .v {{ color: #287942; }}
 .live-tiles .daily-price {{ background: #eaf8f7; border-color: #b8dedb; }}
 .live-tiles .daily-price .v {{ color: #147a74; }}
 .live-panel.stale .live-tiles {{ opacity: .62; }}
 .forecast-quality {{ margin: 10px 0 0; padding: 0; background: #fff;
        border: 1px solid #dfe6ed; border-radius: 11px;
        box-shadow: 0 2px 8px rgba(25,42,65,.05); }}
 .forecast-quality > summary {{ display: flex; align-items: baseline; gap: 9px;
        padding: 11px 38px 11px 12px; cursor: pointer; position: relative;
        font-weight: 700; list-style: none; }}
 .forecast-quality > summary::-webkit-details-marker {{ display: none; }}
 .forecast-quality > summary::after {{ content: '⌄'; position: absolute;
        right: 13px; top: 8px; font-size: 18px; transition: transform .16s; }}
 .forecast-quality[open] > summary::after {{ transform: rotate(180deg); }}
 /* Statusfarbe des Panels – auch eingeklappt sichtbar (Akzentrand + Punkt). */
 .forecast-quality > summary::before {{ content: ''; width: 9px; height: 9px;
        border-radius: 50%; background: #b6bdc5; align-self: center; flex: none; }}
 .forecast-quality.lvl-current {{ border-left: 4px solid #2ca02c; }}
 .forecast-quality.lvl-partial {{ border-left: 4px solid #e6a700; }}
 .forecast-quality.lvl-replaced {{ border-left: 4px solid #d62728; }}
 .forecast-quality.lvl-current > summary::before {{ background: #2ca02c; }}
 .forecast-quality.lvl-partial > summary::before {{ background: #e6a700; }}
 .forecast-quality.lvl-replaced > summary::before {{ background: #d62728; }}
 .forecast-quality > summary small {{ color: #737d87; font-size: 11px;
        font-weight: 400; }}
 .quality-grid {{ display: grid; grid-template-columns: repeat(auto-fit,minmax(175px,1fr));
        gap: 7px; padding: 0 12px 12px; }}
 .quality-item {{ position: relative; min-width: 0; padding: 8px 9px 8px 12px;
        border: 1px solid #dfe5eb; border-left: 4px solid #999;
        border-radius: 8px; background: #f8fafb; }}
 .quality-item.current {{ border-left-color: #2ca02c; background: #f1faf4; }}
 .quality-item.partial {{ border-left-color: #e6a700; background: #fff9e8; }}
 .quality-item.replaced {{ border-left-color: #d62728; background: #fdf0ef; }}
 .quality-source {{ font-size: 12px; font-weight: 700; }}
 .quality-state {{ margin-top: 2px; font-size: 12px; font-weight: 650; }}
 .quality-item.current .quality-state {{ color: #237a3b; }}
 .quality-item.partial .quality-state {{ color: #8a6d00; }}
 .quality-item.replaced .quality-state {{ color: #b3261e; }}
 .quality-detail {{ margin-top: 2px; color: #727c86; font-size: 10px;
        line-height: 1.3; }}
 .live-dot {{ display: inline-block; width: 8px; height: 8px; border-radius: 50%;
        margin-right: 4px; background: #999; }}
 .live-dot.ok {{ background: #2ca02c; }}
 .live-dot.err {{ background: #d62728; }}
 .mobile-plot-shell {{ display: none; }}
 .banner {{ border-radius: 8px; padding: 8px 14px; margin-bottom: 10px;
           font-size: 13px; border: 1px solid; }}
 .banner ul {{ margin: 6px 0 0; padding-left: 20px; }}
 .banner li {{ margin: 2px 0; }}
 .banner.ok {{ background: #eafaf0; border-color: #b6e2c6; color: #1e7e46; }}
 .banner.warn {{ background: #fff8e1; border-color: #f0d98a; color: #8a6d00; }}
 .banner.err {{ background: #fdecea; border-color: #f5b5ae; color: #b3261e; }}
 .decisions {{ margin: 8px 0 12px; background: #fff;
        border: 1px solid #dfe5eb; border-radius: 12px;
        box-shadow: 0 2px 9px rgba(20,35,55,.06); }}
 .decisions > summary {{ display: flex; align-items: center; gap: 10px;
        padding: 13px 14px; cursor: pointer; list-style: none; user-select: none; }}
 .decisions > summary::-webkit-details-marker {{ display: none; }}
 .decisions > summary:after {{ content: '⌄'; margin-left: auto; color: #69717b;
        font-size: 19px; transition: transform .2s; }}
 .decisions[open] > summary:after {{ transform: rotate(180deg); }}
 .decisions[open] > summary {{ border-bottom: 1px solid #e2e7ec; }}
 .decision-head {{ display: flex; justify-content: space-between; align-items: baseline;
        gap: 10px; flex: 1; }}
 .decision-head b {{ font-size: 15px; }}
 .decision-head small {{ color: #737c86; }}
 .decision-body {{ padding: 11px 14px 14px; }}
 .decision-list {{ display: grid; grid-template-columns: repeat(auto-fit,minmax(260px,1fr));
        gap: 8px; }}
 .decision-item {{ position: relative; min-width: 0; padding: 10px 11px 10px 15px;
        border: 1px solid #e2e7ec; border-radius: 9px; background: #fafcfe; }}
 .decision-item:before {{ content: ''; position: absolute; left: 0; top: 0; bottom: 0;
        width: 5px; border-radius: 9px 0 0 9px; background: var(--decision-color); }}
 .decision-time {{ color: #66717c; font-size: 11px; }}
 .decision-name {{ font-weight: 750; margin: 2px 0 4px; }}
 .decision-reason {{ color: #4d5863; font-size: 12px; line-height: 1.35; }}
 .decision-facts {{ display: flex; flex-wrap: wrap; gap: 6px; margin-top: 7px; }}
 .decision-facts span {{ padding: 3px 6px; border-radius: 10px; background: #eef3f7;
        color: #52606d; font-size: 10px; }}
 .decision-empty {{ color: #68737d; font-size: 12px; }}
 .report {{ margin: 4px 0 12px; }}
 .report button {{ font-size: 13px; padding: 7px 14px; border-radius: 7px;
        border: 1px solid #c9ccd1; background: #f0f1f3; cursor: pointer; }}
 .report button.hot {{ background: #fdecea; border-color: #f5b5ae; color: #b3261e;
        font-weight: 600; }}
 .report .msg {{ margin-left: 10px; font-size: 12px; color: #555; }}
 .controls {{ margin: 8px 0 14px; background: #fff; border: 1px solid #dfe3e8;
        border-radius: 12px; font-size: 13px; box-shadow: 0 2px 9px rgba(20,35,55,.06); }}
 .controls > summary {{ display: flex; align-items: center; gap: 12px; padding: 14px 16px;
        cursor: pointer; list-style: none; user-select: none; background: #f7f9fc;
        border-radius: 12px; }}
 .controls > summary::-webkit-details-marker {{ display: none; }}
 .controls[open] > summary {{ border-radius: 12px 12px 0 0; border-bottom: 1px solid #e4e7eb; }}
 .controls .ctl-title {{ font-size: 15px; font-weight: 750; }}
 .controls .ctl-summary {{ color: #737a84; font-size: 12px; flex: 1; }}
 .controls .ctl-chevron {{ font-size: 20px; color: #69717b; transition: transform .2s; }}
 .controls[open] .ctl-chevron {{ transform: rotate(180deg); }}
 .controls .ctl-body {{ padding: 15px; }}
 .ctl-section-head {{ display: flex; flex-direction: column; margin: 0 0 9px 2px; }}
 .ctl-section-head small, .ctl-section small, .load-head small {{ display: block; color: #7b828c;
        font-weight: normal; margin-top: 2px; }}
 .load-cards {{ display: grid; grid-template-columns: repeat(auto-fit,minmax(330px,1fr)); gap: 12px; }}
 .load-card {{ border: 1px solid #e1e5ea; border-radius: 10px; padding: 13px;
        background: #fbfcfe; min-width: 0; }}
 .load-head {{ display: flex; justify-content: space-between; align-items: flex-start;
        padding-bottom: 10px; margin-bottom: 10px; border-bottom: 1px solid #e9ecf0; }}
 .load-head b {{ font-size: 14px; }}
 .ctl-grid {{ display: grid; grid-template-columns: repeat(auto-fit,minmax(130px,1fr)); gap: 9px; }}
 .ctl-field {{ color: #555e68; font-size: 11px; }}
 .ctl-field > span:first-child {{ display: block; margin-bottom: 4px; }}
 .ctl-input {{ display: flex; align-items: center; gap: 4px; }}
 .controls input[type=number], .controls textarea {{ box-sizing: border-box; border: 1px solid #cfd5dc;
        border-radius: 7px; background: #fff; color: #222; padding: 7px 8px; font: inherit; }}
 .controls input[type=number] {{ width: 100%; min-width: 65px; }}
 .ctl-input small {{ white-space: nowrap; color: #777f88; }}
 .profile-edit {{ margin-top: 12px; }}
 .profile-edit > label {{ display: flex; flex-direction: column; gap: 2px; margin-bottom: 5px; }}
 .profile-edit > label small {{ color: #7b828c; font-weight: normal; }}
 .profile-edit textarea {{ width: 100%; resize: vertical; line-height: 1.4; }}
 .curve-box {{ margin-top: 7px; border: 1px solid #e0e5eb; border-radius: 8px;
        overflow: hidden; background: #f7f9fc; }}
 .curve-box canvas {{ display: block; width: 100%; height: 92px; }}
 .curve-box span {{ display: block; padding: 5px 9px; color: #5e6874; background: #fff;
        border-top: 1px solid #e5e8ec; font-size: 11px; }}
 .curve-box span.bad {{ color: #b3261e; }}
 .load-actions {{ display: flex; justify-content: flex-end; margin-top: 11px; }}
 .controls button {{ font-size: 13px; padding: 7px 12px; border-radius: 7px;
        border: 1px solid #c9d0d8; background: #f2f4f7; color: #30363d; cursor: pointer; }}
 .controls button:hover {{ background: #e8edf3; }}
 .controls button.primary {{ background: #1769c2; color: #fff; border-color: #1769c2; }}
 .controls button.mode.on {{ background: #0d6efd; color: #fff; border-color: #0d6efd; }}
 .controls button.mode {{ display: flex; flex-direction: column; align-items: flex-start;
        gap: 2px; min-width: 155px; text-align: left; }}
 .controls button.mode small {{ color: #737c86; margin: 0; font-size: 10px; }}
 .controls button.mode.on small {{ color: rgba(255,255,255,.82); }}
 .controls button.stop {{ color: #a22b25; border-color: #e2b6b3; background: #fff5f4; }}
 .ctl-section {{ display: flex; align-items: center; flex-wrap: wrap; gap: 10px;
        margin-top: 13px; padding: 12px 13px; border: 1px solid #e2e6eb;
        border-radius: 9px; background: #fafbfc; }}
 .ctl-section > div:first-child {{ min-width: 175px; margin-right: auto; }}
 .ctl-section label {{ color: #555e68; }}
 .ctl-section input {{ width: 75px !important; margin: 0 3px; }}
 .button-group {{ display: flex; flex-wrap: wrap; gap: 6px; }}
 .controls .ctl-msg {{ margin-top: 9px; font-size: 12px; color: #555; min-height: 1em; }}
 .plan-compare {{ display: block; margin-top: 12px; padding: 13px; border: 1px solid #dbe3eb;
        border-radius: 9px; background: #f5f9fd; }}
 .plan-compare > div:first-child small {{ display: block; color: #6f7983; margin-top: 3px; }}
 .compare-result {{ color: #5e6872; }}
 .compare-chart-status {{ min-height: 16px; margin-top: 6px; color: #74808b; font-size: 10px; }}
 .compare-chart {{ display: none; width: 100%; min-height: 340px; }}
 .compare-note {{ margin-bottom: 7px; }}
 .mode-compare-grid {{ display: grid; grid-template-columns: repeat(auto-fit,minmax(205px,1fr)); gap: 8px; margin: 8px 0; }}
 .mode-compare-card {{ min-width: 0; padding: 10px; border: 1px solid #dfe6ed; border-radius: 9px; background: #fff; }}
 .mode-compare-card.recommended {{ border-color: #62ad78; box-shadow: inset 0 3px #45a164; }}
 .mode-compare-card.active {{ border-color: #6ca6dc; }}
 .mode-compare-card header {{ display: flex; justify-content: space-between; gap: 5px; align-items: flex-start; margin-bottom: 8px; }}
 .mode-compare-card header b, .mode-compare-card header small {{ display: block; }}
 .mode-compare-card header small, .mode-metrics span {{ color: #74808b; font-size: 9px; }}
 .recommend-badge {{ padding: 2px 5px; border-radius: 999px; background: #e1f2e6; color: #27703e; font-size: 9px; }}
 .mode-metrics {{ display: grid; grid-template-columns: 1fr 1fr; gap: 6px; }}
 .mode-metrics > div {{ min-width: 0; }}
 .mode-metrics > div.wide {{ grid-column: 1/-1; }}
 .mode-metrics span, .mode-metrics b {{ display: block; }}
 .mode-metrics b {{ font-size: 11px; white-space: nowrap; }}
 .mode-validation {{ display: block; margin-top: 7px; color: #74808b; font-size: 9px; }}
 .late-confidence {{ margin-top: 9px; padding: 7px; border-radius: 7px; background: #f1f4f7; }}
 .late-confidence span, .late-confidence b, .late-confidence small {{ display: block; }}
 .late-confidence span, .late-confidence small {{ color: #6f7983; font-size: 9px; }}
 .late-confidence b {{ font-size: 11px; margin: 2px 0; }}
 .late-confidence.very_likely {{ background: #e6f4ea; color: #246b39; }}
 .late-confidence.expected_only {{ background: #fff3d8; color: #805a12; }}
 .late-confidence.p10_unreachable {{ background: #fbe7e5; color: #96342d; }}
 .mode-compare-card > button {{ width: 100%; margin-top: 8px; font-size: 10px; }}
 .shadow-apply {{ margin-top: 7px; }}
 .control-channels {{ display: flex; flex-wrap: wrap; gap: 5px; margin-top: 7px; }}
 .control-channels span {{ padding: 3px 7px; border-radius: 999px; background: #e7f0f8;
        color: #315f83; font-size: 10px; }}
 .info-panel {{ margin: 10px 0; border: 1px solid #dde4eb; border-radius: 10px; background: #fff; overflow: hidden; }}
 .info-panel > summary {{ padding: 11px 13px; cursor: pointer; font-weight: 700; background: #f7f9fb; }}
 .info-panel > summary small {{ margin-left: 8px; color: #75808a; font-weight: 400; }}
 .detail-grid {{ display: grid; grid-template-columns: repeat(auto-fit,minmax(145px,1fr)); gap: 7px; padding: 12px; }}
 .detail-grid h3, .detail-grid p {{ grid-column: 1/-1; margin: 0 0 4px; }}
 .detail-grid > div {{ padding: 8px 9px; border-radius: 7px; background: #f4f7fa; }}
 .detail-grid span, .detail-grid b {{ display: block; }}
 .detail-grid span {{ color: #74808b; font-size: 10px; }}
 .events-list {{ max-height: 360px; overflow: auto; padding: 6px 12px 11px; }}
 .event {{ display: grid; grid-template-columns: 115px 1fr; gap: 10px; padding: 8px 3px; border-bottom: 1px solid #edf0f3; }}
 .event time {{ color: #74808b; font-size: 11px; }}
 .event.error span {{ color: #bd302a; }}
 .switch {{ display: inline-flex; align-items: center; gap: 6px; cursor: pointer; }}
 .switch input {{ position: absolute; opacity: 0; pointer-events: none; }}
 .switch span {{ width: 34px; height: 19px; border-radius: 12px; background: #b9bec5;
        position: relative; transition: background .2s; }}
 .switch span:after {{ content: ''; position: absolute; width: 15px; height: 15px;
        left: 2px; top: 2px; border-radius: 50%; background: #fff; transition: transform .2s;
        box-shadow: 0 1px 3px rgba(0,0,0,.25); }}
 .switch input:checked + span {{ background: #278445; }}
 .switch input:checked + span:after {{ transform: translateX(15px); }}
 .switch em {{ font-style: normal; font-size: 11px; color: #68707a; }}
 .battery-planner {{ margin-top: 13px; padding: 14px; border: 1px solid #d9e1ea;
        border-radius: 10px; background: linear-gradient(150deg,#fbfdff,#f3f7fc); }}
 .planner-head {{ display: flex; align-items: flex-start; justify-content: space-between;
        gap: 10px; margin-bottom: 12px; }}
 .planner-head small {{ display: block; color: #737c87; margin-top: 2px; }}
 .planner-badge {{ padding: 5px 9px; border-radius: 12px; background: #edf0f3;
        color: #68717a; font-size: 11px; white-space: nowrap; }}
 .planner-badge.active {{ background: #dcecff; color: #155aa4; font-weight: 700; }}
 .planner-form {{ display: grid; grid-template-columns: 1fr 1.6fr 1fr 1fr auto;
        align-items: end; gap: 9px; }}
 .planner-form > label {{ display: flex; flex-direction: column; gap: 4px;
        color: #59636e; font-size: 11px; }}
 .controls select, .controls input[type=datetime-local] {{ box-sizing: border-box;
        width: 100%; border: 1px solid #cfd5dc; border-radius: 7px; background: #fff;
        color: #222; padding: 7px 8px; font: inherit; min-height: 34px; }}
 .planner-actions {{ display: flex; flex-wrap: wrap; gap: 6px; justify-content: flex-end; }}
 .planner-actions button {{ min-height: 35px; white-space: nowrap; }}
 .controls button.instant {{ background: #e8f2ff; border-color: #a9c8ea; color: #165d9f; }}
 .schedule-msg {{ min-height: 18px; margin: 7px 1px 0; font-size: 12px; color: #59636e; }}
 .schedule-chart-wrap {{ margin-top: 4px; padding: 12px 12px 8px; background: #fff;
        border: 1px solid #dfe5eb; border-radius: 9px; overflow: hidden; }}
 .schedule-axis {{ position: relative; height: 20px; margin: 0 5px; color: #77818c;
        font-size: 10px; }}
 .schedule-axis span {{ position: absolute; transform: translateX(-50%); white-space: nowrap; }}
 .schedule-axis span:first-child {{ transform: none; }}
 .schedule-axis span:last-child {{ transform: translateX(-100%); }}
 .schedule-chart {{ position: relative; height: 94px; margin: 0 5px; border-radius: 6px;
        background: linear-gradient(to bottom,#edf5ff 0,#edf5ff 48%,#fff4e9 52%,#fff4e9 100%); overflow: hidden; }}
 .schedule-grid, .schedule-now {{ position: absolute; top: 0; bottom: 0; width: 1px;
        background: rgba(80,95,115,.18); }}
 .schedule-now {{ width: 2px; background: #20252b; z-index: 4; }}
 .schedule-bar {{ position: absolute; height: 29px; border-radius: 5px; color: #fff;
        padding: 6px 7px; box-sizing: border-box; overflow: hidden; white-space: nowrap;
        text-overflow: ellipsis; font-size: 10px; font-weight: 700; z-index: 3;
        min-width: 4px; box-shadow: 0 1px 4px rgba(0,0,0,.18); }}
 .schedule-bar.charge, .schedule-bar.grid_charge {{ top: 8px; background: #397fd0; }}
 .schedule-bar.pv_charge {{ top: 8px; background: #269b79; }}
 .schedule-bar.discharge {{ bottom: 10px; background: #dc7a27; }}
 .schedule-bar.idle {{ top: 33px; background: #77818c; }}
 .schedule-bar.running {{ outline: 2px solid #1c2733; animation: schedulePulse 1.5s infinite; }}
 @keyframes schedulePulse {{ 50% {{ filter: brightness(1.18); }} }}
 .schedule-legend {{ display: flex; flex-wrap: wrap; gap: 14px; margin-top: 6px;
        color: #737c86; font-size: 10px; }}
 .schedule-legend .charge, .schedule-legend .grid_charge {{ color: #397fd0; }}
 .schedule-legend .pv_charge {{ color: #269b79; }}
 .schedule-legend .discharge {{ color: #dc7a27; }}
 .schedule-legend .idle {{ color: #77818c; }}
 .schedule-legend .now {{ color: #20252b; }}
 .schedule-list {{ display: grid; gap: 6px; margin-top: 9px; }}
 .schedule-item {{ display: flex; align-items: center; gap: 9px; padding: 8px 9px;
        border: 1px solid #e0e5ea; border-radius: 8px; background: #fff; }}
 .schedule-item > div {{ flex: 1; min-width: 120px; }}
 .schedule-item small {{ display: block; color: #747d87; margin-top: 2px; }}
 .schedule-color {{ width: 9px; height: 30px; border-radius: 5px; flex: 0 0 auto; }}
 .schedule-color.charge, .schedule-color.grid_charge {{ background: #397fd0; }}
 .schedule-color.pv_charge {{ background: #269b79; }}
 .schedule-color.discharge {{ background: #dc7a27; }}
 .schedule-color.idle {{ background: #77818c; }}
 .schedule-state {{ color: #68717a; font-size: 11px; text-align: right; }}
 .schedule-item.running {{ border-color: #85addb; background: #f1f7ff; }}
 .schedule-item.cancelled, .schedule-item.completed, .schedule-item.skipped {{ opacity: .68; }}
 .controls button.mini {{ padding: 4px 8px; font-size: 11px; }}
 .controls button.schedule-delete {{ color: #6b4b4b; background: #f7eeee; border-color: #e8cece; }}
 .schedule-empty {{ color: #7c858e; text-align: center; padding: 12px; font-size: 12px; }}
 .plotly-graph-div {{ border-radius: 12px; box-shadow: 0 3px 14px rgba(28,45,68,.07); }}
 html.dark {{ background: #111820; color-scheme: dark; }}
 html.dark body {{ color: #e7edf4; background: linear-gradient(145deg,#111820,#17212b); }}
 html.dark .app-header, html.dark .tile, html.dark .controls,
 html.dark .schedule-chart-wrap, html.dark .schedule-item, html.dark .decisions,
 html.dark .curve-box span, html.dark .runtime-strip, html.dark .info-panel {{ background: #18212b; border-color: #354352; color: #e7edf4; }}
 html.dark .runtime-main small, html.dark #runtime-meta {{ color: #b6c2ce; }}
 html.dark .runtime-progress {{ background: #354352; }}
 html.dark #recalc-plan {{ background: #1d3c59; border-color: #3c6f9d; color: #9fd0ff; }}
 html.dark .info-panel > summary {{ background: #202b36; color: #e7edf4; }}
 html.dark .detail-grid > div {{ background: #202b36; border-color: #354352; }}
 html.dark .detail-grid span, html.dark .event time {{ color: #aebbc8; }}
 html.dark .compare-chart-status {{ color: #aebbc8; }}
 html.dark .event {{ border-color: #303e4b; }}
 html.dark .plan-compare {{ background: #1b2834; border-color: #354352; }}
 html.dark .mode-compare-card {{ background: #202b36; border-color: #425364; }}
 html.dark .mode-compare-card.recommended {{ border-color: #4d9b67; box-shadow: inset 0 3px #4d9b67; }}
 html.dark .mode-compare-card.active {{ border-color: #4e86b9; }}
 html.dark .mode-compare-card header small, html.dark .mode-metrics span {{ color: #aebbc8; }}
 html.dark .mode-validation {{ color: #aebbc8; }}
 html.dark .recommend-badge {{ background: #254a32; color: #9bd5ad; }}
 html.dark .late-confidence {{ background: #2b3743; }}
 html.dark .late-confidence.very_likely {{ background: #244332; color: #a7d9b7; }}
 html.dark .late-confidence.expected_only {{ background: #4b4026; color: #f1d18a; }}
 html.dark .late-confidence.p10_unreachable {{ background: #4b2d2c; color: #efaaa5; }}
 html.dark .decision-item {{ background: #202b36; border-color: #354352; }}
 html.dark .decision-time, html.dark .decision-head small {{ color: #aebbc8; }}
 html.dark .decision-reason, html.dark .decision-empty {{ color: #d1dae4; }}
 html.dark .decision-facts span {{ background: #2a3947; color: #d9e3ed; }}
 html.dark .controls > summary, html.dark .load-card, html.dark .ctl-section,
 html.dark .curve-box, html.dark .planner-badge {{ background: #202b36; border-color: #354352; }}
 html.dark .battery-planner {{ background: linear-gradient(150deg,#18232d,#202b36); border-color: #354352; }}
 html.dark .controls input, html.dark .controls textarea, html.dark .controls select {{ background: #111820; color: #eef3f8; border-color: #4b5b6b; }}
 html.dark .controls button, html.dark .header-actions button {{ background: #263442; color: #eef3f8; border-color: #4b5b6b; }}
 html.dark .controls button.primary, html.dark .controls button.mode.on {{ background: #287fd8; color: #fff; border-color: #4d9bea; }}
 html.dark .controls button.instant {{ background: #1c3c5c; color: #8fc8ff; border-color: #3c6f9d; }}
 html.dark .controls button.stop {{ background: #432529; color: #ffaaa4; border-color: #75454a; }}
 html.dark .report button {{ background: #263442; color: #e7edf4; border-color: #4b5b6b; }}
 html.dark .report button:hover {{ background: #304253; }}
 html.dark .report button.hot {{ background: #432529; color: #ffaaa4; border-color: #75454a; }}
 html.dark .tile .l, html.dark .chips, html.dark .report .msg {{ color: #c2ccd6; }}
 html.dark .tile .s, html.dark .controls .ctl-summary,
 html.dark .ctl-section-head small, html.dark .ctl-section small,
 html.dark .load-head small {{ color: #97a5b4; }}
 html.dark .ctl-field, html.dark .ctl-section label, html.dark .planner-form > label,
 html.dark .schedule-msg, html.dark .schedule-state, html.dark .schedule-axis,
 html.dark .schedule-legend, html.dark .schedule-item small {{ color: #b7c2ce; }}
 html.dark .schedule-chart {{ background: linear-gradient(to bottom,#172d42 0,#172d42 48%,#3a291c 52%,#3a291c 100%); }}
 html.dark .schedule-grid {{ background: rgba(205,220,235,.2); }}
 html.dark .schedule-now {{ background: #f4f7fa; }}
 html.dark .schedule-legend .now {{ color: #f4f7fa; }}
 html.dark .controls button.schedule-delete {{ color: #ffc7c7; background: #40282b; border-color: #6e4045; }}
 html.dark .live-tiles .live-solar {{ background: #3a3319; border-color: #6d5e26; }}
 html.dark .live-tiles .live-house {{ background: #292238; border-color: #50436b; }}
 html.dark .live-tiles .live-soc {{ background: #173634; border-color: #2d615d; }}
 html.dark .live-tiles .live-wallbox, html.dark .live-tiles .flow-charge {{ background: #192d43; border-color: #31577e; }}
 html.dark .live-tiles .live-temp {{ background: #3a2a1c; border-color: #6e4d2f; }}
 html.dark .live-tiles .flow-import {{ background: #421f22; border-color: #74373a; }}
 html.dark .live-tiles .flow-export {{ background: #183522; border-color: #326541; }}
 html.dark .live-tiles .flow-discharge {{ background: #422b18; border-color: #75502e; }}
 html.dark .live-tiles .flow-idle {{ background: #252d35; border-color: #46515c; }}
 html.dark .live-tiles .daily-import {{ background: #421f22; border-color: #74373a; }}
 html.dark .live-tiles .daily-export, html.dark .live-tiles .daily-discharge {{ background: #183522; border-color: #326541; }}
 html.dark .live-tiles .daily-charge {{ background: #192d43; border-color: #31577e; }}
 html.dark .live-tiles .daily-price {{ background: #173634; border-color: #2d615d; }}
 html.dark .live-daily-panel > summary span {{ color: #aebbc8; }}
 html.dark #live-day-status[data-quality=current] {{ color: #73d595; }}
 html.dark #live-day-status[data-quality=warning] {{ color: #efd06d; }}
 html.dark #live-day-status[data-quality=stale] {{ color: #ff9691; }}
 html.dark .live-tiles .daily-deviation.positive, html.dark .live-tiles .daily-balance.ok {{ background: #183522; border-color: #326541; }}
 html.dark .live-tiles .daily-deviation.negative, html.dark .live-tiles .daily-balance.bad {{ background: #3a3219; border-color: #6d5e26; }}
 html.dark .control-channels span {{ background: #263b4d; color: #b9d9f3; }}
 html.dark .live-tiles .tile .v {{ filter: brightness(1.55) saturate(1.18); }}
 html.dark .live-head, html.dark .live-head #live-status {{ color: #dbe5ef; }}
 html.dark .live-tiles .tile .l {{ color: #e0e7ef; }}
 html.dark .live-tiles .tile .s {{ color: #aebbc8; }}
 html.dark .forecast-quality {{ background: #18212b; border-color: #354352; }}
 html.dark .quality-head small, html.dark .quality-detail {{ color: #aebbc8; }}
 html.dark .quality-item {{ background: #202b36; border-color: #43515f; }}
 html.dark .quality-item.current {{ background: #173326; border-left-color: #58b879; }}
 html.dark .quality-item.partial {{ background: #3a3219; border-left-color: #d9b83f; }}
 html.dark .quality-item.replaced {{ background: #402124; border-left-color: #df6c68; }}
 html.dark .quality-item.current .quality-state {{ color: #8fd7a9; }}
 html.dark .quality-item.partial .quality-state {{ color: #e1c96b; }}
 html.dark .quality-item.replaced .quality-state {{ color: #f1a29c; }}
 html.dark .forecast-quality.lvl-current {{ border-left-color: #58b879; }}
 html.dark .forecast-quality.lvl-partial {{ border-left-color: #d9b83f; }}
 html.dark .forecast-quality.lvl-replaced {{ border-left-color: #df6c68; }}
 html.dark .forecast-quality.lvl-current > summary::before {{ background: #58b879; }}
 html.dark .forecast-quality.lvl-partial > summary::before {{ background: #d9b83f; }}
 html.dark .forecast-quality.lvl-replaced > summary::before {{ background: #df6c68; }}
 html.dark .banner.ok {{ background: #173326; border-color: #285b40; color: #8fd7a9; }}
 html.dark .banner.warn {{ background: #3a3219; border-color: #6a5925; color: #e1c96b; }}
 html.dark .banner.err {{ background: #402124; border-color: #73383d; color: #f1a29c; }}
 @media (max-width: 620px) {{
   body {{ padding: 8px; }}
   .app-header {{ padding: 10px 11px; margin-bottom: 8px; position: relative; }}
   h1 {{ font-size: 17px; line-height: 1.25; }}
   h1 .ts {{ display: block; font-size: 11px; margin-top: 2px; }}
   .header-actions button {{ min-width: 44px; min-height: 44px; font-size: 0; padding: 0; }}
   #theme-toggle:after {{ content: '◐'; font-size: 21px; }}
   #install-app:after {{ content: '↓'; font-size: 22px; }}
   .tiles {{ display: grid; grid-template-columns: repeat(2,minmax(0,1fr)); gap: 7px; }}
   .live-daily {{ grid-template-columns: repeat(2,minmax(0,1fr)); }}
   .tile {{ min-width: 0; padding: 9px 10px; overflow: hidden; }}
   .tile .v {{ font-size: 17px; line-height: 1.2; overflow-wrap: anywhere; }}
   .tile .l {{ font-size: 11px; }}
   .tile .s {{ font-size: 10px; line-height: 1.25; }}
   .live-head {{ margin-top: 0; }}
   .live-daily-panel > summary {{ min-height: 42px; align-items: center; margin-bottom: 2px; }}
   .desktop-plot {{ display: none; }}
   .mobile-plot-shell {{ display: block; background: #fff; border: 1px solid #e0e5eb;
        border-radius: 12px; margin: 10px 0 13px; overflow: hidden;
        box-shadow: 0 3px 14px rgba(28,45,68,.07); }}
   html.dark .mobile-plot-shell {{ background: #18212b; border-color: #354352; }}
   .mobile-plot-toolbar {{ display: flex; flex-direction: column; gap: 7px; padding: 9px;
        border-bottom: 1px solid #e2e7ec; }}
   html.dark .mobile-plot-toolbar {{ border-color: #354352; }}
   .mobile-plot-tabs {{ display: flex; gap: 5px; overflow-x: auto; scrollbar-width: none; }}
   .mobile-plot-tabs button, .horizon-switch button {{ flex: 0 0 auto; min-height: 40px;
        padding: 7px 11px; border: 1px solid #cbd3db; border-radius: 8px;
        background: #f4f6f8; color: #34404c; font: inherit; font-size: 12px; }}
   .mobile-plot-tabs button.on, .horizon-switch button.on {{ color: #fff; background: #1769c2; border-color: #1769c2; }}
   html.dark .mobile-plot-tabs button, html.dark .horizon-switch button {{ color: #e7edf4; background: #263442; border-color: #4b5b6b; }}
   html.dark .mobile-plot-tabs button.on, html.dark .horizon-switch button.on {{ background: #287fd8; }}
   .horizon-switch {{ display: grid; grid-template-columns: 1fr 1fr; gap: 5px; }}
   #mobile-plot {{ min-height: 420px; }}
   .controls .ctl-summary {{ display: none; }}
   .controls > summary {{ min-height: 44px; box-sizing: border-box; }}
   .controls button, .controls select, .controls input[type=datetime-local] {{ min-height: 44px; }}
   .load-cards {{ grid-template-columns: 1fr; }}
   .ctl-section {{ align-items: flex-start; }}
   .planner-form {{ grid-template-columns: 1fr 1fr; }}
   .planner-actions {{ grid-column: 1 / -1; justify-content: stretch; }}
   .planner-actions button {{ flex: 1 1 auto; }}
   .planner-head {{ flex-direction: column; }}
   .schedule-state {{ width: 100%; text-align: left; }}
   .schedule-item {{ flex-wrap: wrap; }}
   .runtime-strip {{ grid-template-columns: 1fr auto; gap: 8px; }}
   .runtime-main {{ grid-column: 1/-1; }}
   .runtime-progress {{ grid-column: 1/-1; grid-row: 2; }}
   #runtime-meta {{ display: none; }}
   #recalc-plan {{ min-height: 42px; grid-column: 1/-1; }}
   .mode-compare-grid {{ grid-template-columns: 1fr; }}
   .compare-chart {{ min-height: 380px; margin: 0 -5px; width: calc(100% + 10px); }}
   .event {{ grid-template-columns: 90px 1fr; }}
 }}
 .chips {{ font-size: 12px; color: #555; margin: -2px 0 10px; }}
 .chips .chip {{ margin-right: 14px; white-space: nowrap; }}
 .chips .dot {{ display: inline-block; width: 8px; height: 8px;
        border-radius: 50%; margin-right: 4px; }}
</style></head><body>
<header class="app-header"><h1>E3DC EMS Steuerung
 <span class="ts">{now.strftime('%Y-%m-%d %H:%M')}</span></h1>
 <div class="header-actions"><button type="button" id="install-app" title="Als App installieren">Installieren</button>
 <button type="button" id="theme-toggle" title="Darstellung wechseln">Darstellung</button></div></header>
{runtime_html}
{live_html}
<div class="tiles">{''.join(tiles)}</div>
{_sources_block(source_status)}
{_control_banner(control_status)}
{_alert_banner(violations)}
{controls_html}
<div class="desktop-plot">{plot_html}</div>
{mobile_plot_html}
{_slot_detail_block()}
{decision_html}
{_operations_block(solver_status, execution_status)}
{_thermal_feedback_block(load_feedback_status, thermal_calibration)}
{_forecast_quality_block(forecast_quality, config.general.timezone)}
{_forecast_accuracy_block()}
{_savings_history_block()}
{_events_block()}
{report_html}
<script>(function(){{
 var theme=document.getElementById('theme-toggle'),install=document.getElementById('install-app'),prompt=null;
 function label(){{var dark=document.documentElement.classList.contains('dark');theme.title=dark?'Helle Darstellung':'Dunkle Darstellung';theme.setAttribute('aria-label',theme.title);}}
 function paint(){{var dark=document.documentElement.classList.contains('dark');var c=dark?{{paper_bgcolor:'#18212b',plot_bgcolor:'#18212b','font.color':'#e7edf4'}}:{{paper_bgcolor:'#fff',plot_bgcolor:'#fff','font.color':'#20252b'}};var lines={{'Haus-SoC (Ist)':['#111111','#f7fafc'],'Haus-SoC (Prog.)':['#111111','#d5e0ea'],'Akku-Leistung (Ist)':['#111111','#58d68d'],'Außentemperatur':['#7f7f7f','#a9d5ff']}};document.querySelectorAll('.desktop-plot .plotly-graph-div').forEach(function(p){{Plotly.relayout(p,c);(p.layout.annotations||[]).forEach(function(a,i){{if(String(a.text||'').includes('Modus:')){{var u={{}};u['annotations['+i+'].font.color']=dark?'#e7edf4':'#555';Plotly.relayout(p,u);}}}});p.data.forEach(function(t,i){{if(lines[t.name])Plotly.restyle(p,{{'line.color':lines[t.name][dark?1:0]}},[i]);if(t.meta==='mode_timeline'){{if(!t._emsLightColorscale)t._emsLightColorscale=t.colorscale;Plotly.restyle(p,{{colorscale:[dark?[[0,'#344250'],[.125,'#344250'],[.126,'#3f8f55'],[.25,'#3f8f55'],[.251,'#a98e2e'],[.375,'#a98e2e'],[.376,'#914e82'],[.5,'#914e82'],[.501,'#b96d23'],[.625,'#b96d23'],[.626,'#9f3434'],[.75,'#9f3434'],[.751,'#3475ad'],[.875,'#3475ad'],[.876,'#71318f'],[1,'#71318f']]:t._emsLightColorscale]}},[i]);}}if(t.meta==='load_timeline')Plotly.restyle(p,{{colorscale:[dark?[[0,'#263442'],[.33,'#263442'],[.34,'#329b4c'],[.66,'#329b4c'],[.67,'#596979'],[1,'#596979']]:[[0,'#e9ecef'],[.33,'#e9ecef'],[.34,'#2ca02c'],[.66,'#2ca02c'],[.67,'#adb5bd'],[1,'#adb5bd']]]}},[i]);}});}});}}
 theme.addEventListener('click',function(){{var dark=!document.documentElement.classList.contains('dark');document.documentElement.classList.toggle('dark',dark);localStorage.setItem('ems-theme',dark?'dark':'light');label();paint();window.dispatchEvent(new Event('ems-theme-change'));}});label();paint();
 window.addEventListener('beforeinstallprompt',function(e){{e.preventDefault();prompt=e;install.style.display='block';}});
 install.addEventListener('click',function(){{if(prompt){{prompt.prompt();prompt.userChoice.finally(function(){{prompt=null;install.style.display='none';}});}}}});
 if('serviceWorker' in navigator)navigator.serviceWorker.register('/sw.js').catch(function(){{}});
}})();</script>
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
