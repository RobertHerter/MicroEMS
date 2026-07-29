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
    day_start = pd.Timestamp(now).normalize()
    horizon_ranges = _j.dumps({
        "24": [day_start.isoformat(),
               (day_start + pd.Timedelta(hours=24)).isoformat()],
        "48": [day_start.isoformat(),
               (day_start + pd.Timedelta(hours=48)).isoformat()],
    })
    return f"""
<div class="desktop-horizon-toolbar" aria-label="Zeitraum der Plangrafik">
 <span>Zeitraum</span>
 <div class="horizon-switch desktop-horizon-switch">
  <button type="button" data-hours="24">24 h</button><button type="button" data-hours="48">48 h</button><button type="button" data-hours="all">Alles</button>
 </div>
</div>
<section class="mobile-plot-shell" aria-label="Prognose und Steuerung">
 <div class="mobile-plot-toolbar">
  <div class="mobile-plot-tabs" role="tablist">{buttons}</div>
  <div class="horizon-switch" aria-label="Zeitraum">
   <button type="button" data-hours="24">24 h</button><button type="button" data-hours="48">48 h</button><button type="button" data-hours="all">Alles</button>
  </div>
 </div>
<div id="mobile-plot"></div>
</section>
<script>(function(){{
 var axes={axes}, ranges={horizon_ranges}, current='power';
 function mobileView(){{return window.matchMedia('(max-width:620px)').matches;}}
 function storedHours(){{
  var fallback=mobileView()?'24':'all';
  var value=localStorage.getItem(mobileView()?'ems-mobile-hours':'ems-desktop-hours')||fallback;
  return value==='24'||value==='48'||value==='all'?value:fallback;
 }}
 var hours=storedHours();
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
  // Die Grenzen kommen mit der EMS-Zeitzone vom Server. Eine UTC-Umwandlung
  // würde lokale Mitternacht im Sommer auf 22:00 des Vortags verschieben.
  var xaxis={{gridcolor:c.grid,tickformat:'%a %H:%M'}};
  if(hours==='all'){{xaxis.autorange=true;}}
  else{{xaxis.range=ranges[hours];}}
  var srcAxis=src.layout[axis==='y'?'yaxis':'yaxis'+axis.slice(1)]||{{}};
  var layout={{height:420,autosize:true,hovermode:'x unified',separators:',.',showlegend:true,
   paper_bgcolor:c.paper,plot_bgcolor:c.plot,font:{{color:c.font}},margin:{{l:48,r:12,t:18,b:85}},
   hoverlabel:{{bgcolor:document.documentElement.classList.contains('dark')?'#202b36':'#ffffff',
    bordercolor:document.documentElement.classList.contains('dark')?'#536273':'#cfd7df',
    font:{{color:c.font}}}},
   legend:{{orientation:'h',x:0,y:-.2,font:{{size:10}}}},
   xaxis:xaxis,
   yaxis:{{title:srcAxis.title||'',gridcolor:c.grid,zerolinecolor:c.grid}}}};
  if(current==='soc')layout.yaxis.range=[0,101];
  if(current==='loads')layout.yaxis.autorange='reversed';
  Plotly.react('mobile-plot',traces,layout,{{responsive:true,displaylogo:false,displayModeBar:false,scrollZoom:false}})
   .then(function(){{window.dispatchEvent(new Event('ems-plot-ready'));}});
  document.querySelectorAll('.mobile-plot-tabs button').forEach(function(b){{b.classList.toggle('on',b.dataset.panel===current);}});
  document.querySelectorAll('.horizon-switch button').forEach(function(b){{b.classList.toggle('on',b.dataset.hours===hours);}});
 }}
 function desktopRange(){{
  var src=source();if(mobileView()||!src||!window.Plotly)return;
  var update={{}};
  Object.keys(src.layout).filter(function(k){{return /^xaxis\\d*$/.test(k);}}).forEach(function(k){{
   if(hours==='all'){{update[k+'.autorange']=true;update[k+'.range']=null;}}
   else{{update[k+'.autorange']=false;update[k+'.range']=ranges[hours];}}
  }});
  Plotly.relayout(src,update);
 }}
 function setHours(value){{
  hours=value;localStorage.setItem(mobileView()?'ems-mobile-hours':'ems-desktop-hours',hours);
  document.querySelectorAll('.horizon-switch button').forEach(function(b){{b.classList.toggle('on',b.dataset.hours===hours);}});
  desktopRange();render();
 }}
 document.querySelectorAll('.mobile-plot-tabs button').forEach(function(b){{b.addEventListener('click',function(){{current=b.dataset.panel;render();}});}});
 document.querySelectorAll('.horizon-switch button').forEach(function(b){{b.addEventListener('click',function(){{setHours(b.dataset.hours);}});}});
 function refresh(){{hours=storedHours();desktopRange();render();document.querySelectorAll('.horizon-switch button').forEach(function(b){{b.classList.toggle('on',b.dataset.hours===hours);}});}}
 window.addEventListener('resize',refresh);window.addEventListener('ems-theme-change',render);
 setTimeout(refresh,0);
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
                "❖ <b>Planentscheidungen erklärt</b><small>keine besonderen "
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
            "❖ <b>Planentscheidungen erklärt</b>"
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
    """Debug-Button (nur wenn report.enabled): lädt den gewählten Schnappschuss
    und öffnet das Mailprogramm vorausgefüllt. Über die Auswahl lässt sich auch
    ein ÄLTERER Lauf (z.B. der infeasible/falsche) senden - so debuggt man auch
    Probleme anderer Anlagen. mailto kann keine Datei anhängen -> Nutzer hängt
    die geladene .json manuell an."""
    import urllib.parse
    if not getattr(config, "report", None) or not config.report.enabled:
        return ""
    errs = sum(1 for v in (violations or []) if getattr(v, "severity", "") == "error")
    warns = sum(1 for v in (violations or []) if getattr(v, "severity", "") == "warning")
    hot = "hot" if errs else ""
    subj = f"EMS Debug-Report {now.strftime('%Y-%m-%d %H:%M')} ({errs} Fehler, {warns} Warn.)"
    body = ("Auffälligkeit im EMS.\n\n"
            "Bitte die zuvor heruntergeladene .json an diese Mail anhängen (sie "
            "enthält Eingaben + Plan des gewählten Laufs zum Reproduzieren, keine "
            "Zugangsdaten).\n\nNotiz:\n")
    mailto = "mailto:" + urllib.parse.quote(config.report.mail_to) + "?" + \
        urllib.parse.urlencode({"subject": subj, "body": body})
    return (
        '<div class="report">'
        '<select id="ems-report-pick" title="Lauf für den Debug-Report wählen">'
        '<option value="">letzter Lauf</option></select> '
        f'<button class="{hot}" onclick="emsReport()">'
        '✉ Debug-Daten herunterladen &amp; Mail öffnen</button>'
        '</div>'
        '<script>(function(){'
        " const esc=s=>String(s??'').replace(/[&<>\"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;'}[c]));"
        " const sel=document.getElementById('ems-report-pick');"
        " fetch('api/debug-snapshots.json?_='+Date.now(),{cache:'no-store'})"
        "  .then(r=>r.json()).then(d=>{const a=(d.snapshots||[]);"
        "   a.forEach(s=>{const o=document.createElement('option');o.value=s.generated;"
        "    const t=new Date(s.ts_local).toLocaleString('de-DE',{day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'});"
        "    const mark=s.infeasible?'\\u26d4 ':(s.n_violations>0?'\\u26a0 ':'');"
        "    o.textContent=mark+t+(s.status?' \\u00b7 '+s.status:'');o.title=esc(s.reason);sel.appendChild(o);});})"
        "  .catch(()=>{});"
        " window.emsReport=function(){const ts=sel.value;"
        "  const href='report.json'+(ts?('?ts='+encodeURIComponent(ts)):'');"
        "  const fn=ts?('ems_debug_'+ts.replace(/[^0-9T]/g,'-').slice(0,19)+'.json'):'last_run_debug.json';"
        "  const a=document.createElement('a');a.href=href;a.download=fn;"
        "  document.body.appendChild(a);a.click();a.remove();"
        f"  setTimeout(function(){{window.location.href={_js_str(mailto)};}},600);}};"
        '})();</script>')


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
  const watchdog=d.cycle_watchdog||{{}},overdue=watchdog.overdue===true;
  strip.dataset.state=overdue?'error':(d.state||'unknown');
  document.getElementById('runtime-phase').textContent=overdue?'EMS-Zyklus überfällig':(d.phase||'EMS');
  document.getElementById('runtime-message').textContent=overdue?(watchdog.message||'Kein erfolgreicher Optimierungszyklus'):(d.message||'');
  document.getElementById('runtime-progress').style.width=Math.max(0,Math.min(100,Number(d.progress)||0))+'%';
  document.getElementById('runtime-meta').textContent=overdue?'letzter Plan '+fmt(d.plan_generated):(d.state==='ready'?'Plan '+fmt(d.plan_generated)+(d.duration_seconds!=null?' · '+d.duration_seconds.toLocaleString('de-DE')+' s':''):(d.state==='running'?'läuft …':''));
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
<details class="info-panel slot-detail" id="slot-detail"><summary><span class="an-dot neutral" id="slot-detail-dot"></span>⌖ Slot-Details <small id="slot-detail-summary">Kurve anklicken · noch kein Slot gewählt</small></summary>
 <div id="slot-detail-body" class="detail-grid"><p>Wähle einen Zeitpunkt in einem Diagramm aus.</p></div>
</details>
<script>(function(){
 let rows=null;
 const esc=s=>String(s??'–').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
 const num=(v,d=0)=>typeof v==='number'&&isFinite(v)?v.toLocaleString('de-DE',{maximumFractionDigits:d}):'–';
 async function data(){if(rows)return rows;let r=await fetch('api/data.json?_='+Date.now(),{cache:'no-store'});if(!r.ok)throw Error(r.status);rows=await r.json();return rows;}
 function render(x){data().then(a=>{let target=new Date(x).getTime(),best=null,dist=Infinity;if(!isFinite(target))return;a.forEach(r=>{let t=new Date(r.timestamp).getTime(),d=Math.abs(t-target);if(d<dist){dist=d;best=r;}});if(!best)return;let when=new Date(best.timestamp),cmp=(actual,planned,delta,unit='W',digits=0)=>num(actual,digits)+' / '+num(planned,digits)+' / '+num(delta,digits)+' '+unit;let items=[['Hauslast Ist / Soll / Δ',cmp(best.actual_load_w,best.house_load_w,best.load_deviation_w)],['PV Ist / Soll / Δ',cmp(best.actual_pv_w,best.pv_w,best.pv_deviation_w)],['Netz Ist / Soll / Δ',cmp(best.actual_grid_w,best.planned_grid_w,best.grid_deviation_w)],['Akku Ist / Soll / Δ',cmp(best.actual_battery_w,best.planned_battery_w,best.battery_deviation_w)],['SoC Ist / Soll / Δ',cmp(best.actual_soc_percent,best.house_soc_percent,best.soc_deviation_percent,'%',0)],['Preis',num(best.price_ct_kwh,2)+' ct/kWh'],['Akku laden',num((best.batt_dc_charge_w||0)+(best.batt_ac_charge_w||0))+' W'],['Akku entladen',num(best.batt_discharge_w)+' W'],['Netzbezug',num(best.grid_import_w)+' W'],['Einspeisung',num(best.grid_export_w)+' W'],['Modus',best.mode],['Entscheidung',best.decision_reason],['Ausführung',best.execution_label||'–'],['Ausführungsdetail',best.execution_detail||'–'],['verschobene Energie',num(best.decision_energy_kwh,2)+' kWh'],['Wert',num(best.decision_value_ct,1)+' ct'],['Referenz',best.decision_reference_time?new Date(best.decision_reference_time).toLocaleString('de-DE'):'–']];document.getElementById('slot-detail-body').innerHTML='<h3>'+when.toLocaleString('de-DE',{weekday:'short',day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'})+'</h3>'+items.map(i=>'<div><span>'+esc(i[0])+'</span><b>'+esc(i[1])+'</b></div>').join('');document.getElementById('slot-detail-dot').className='an-dot ok';document.getElementById('slot-detail-summary').textContent=when.toLocaleString('de-DE',{weekday:'short',day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'})+' · '+(best.mode||'–');}).catch(()=>{document.getElementById('slot-detail-body').innerHTML='<p>Detaildaten sind nicht verfügbar.</p>';document.getElementById('slot-detail-dot').className='an-dot bad';document.getElementById('slot-detail-summary').textContent='Detaildaten nicht verfügbar';});}
 // WICHTIG: '.plotly-graph-div' vergibt nur Pythons Plotly-HTML (Desktop-Plot).
 // Der Mobil-Plot ist ein eigenes <div id="mobile-plot">, dem Plotly.react nur
 // '.js-plotly-plot' anhängt - ohne diesen Selektor blieb er ungebunden und die
 // Slot-Details funktionierten auf dem Handy gar nicht (Desktop-Plot ist dort
 // display:none).
 function bind(){document.querySelectorAll('.plotly-graph-div,.js-plotly-plot').forEach(p=>{if(p.dataset.emsSlotBound||!p.on)return;p.dataset.emsSlotBound='1';p.on('plotly_click',e=>{let pt=e&&e.points&&e.points[0];if(pt&&pt.x)render(pt.x);});
  // Plotly erzeugt auf Touchscreens nicht zuverlässig plotly_click. Ein kurzes
  // Antippen wird deshalb zusätzlich anhand der x-Achse in eine Zeit übersetzt;
  // Wischen/Zoomen bleibt durch die Bewegungsgrenze unberührt.
  let start=null;p.addEventListener('pointerdown',e=>{if(e.pointerType!=='mouse')start={x:e.clientX,y:e.clientY};},true);
  p.addEventListener('pointerup',e=>{if(!start||e.pointerType==='mouse')return;let s=start;start=null;if(Math.hypot(e.clientX-s.x,e.clientY-s.y)>12)return;let full=p._fullLayout,size=full&&full._size,axis=full&&full.xaxis;if(!size||!axis||!axis.p2d)return;let rect=p.getBoundingClientRect(),px=e.clientX-rect.left-size.l;if(px<0||px>size.w)return;render(axis.p2d(px));autoUnhover();},true);
 });}
 // Touch-Geraete kennen kein mouseleave: das Hover-Panel ('x unified') blieb
 // nach einem Tap dauerhaft stehen und liess sich nicht mehr wegklicken.
 // Daher nach dem Tap zeitversetzt und bei jeder Beruehrung ausserhalb eines
 // Diagramms aktiv abraeumen. Die Werte stehen ohnehin in den Slot-Details.
 let hoverTimer=null;
 function unhoverAll(){document.querySelectorAll('.plotly-graph-div,.js-plotly-plot').forEach(g=>{try{if(window.Plotly&&Plotly.Fx&&Plotly.Fx.unhover)Plotly.Fx.unhover(g);}catch(err){}});}
 function autoUnhover(){clearTimeout(hoverTimer);hoverTimer=setTimeout(unhoverAll,2500);}
 document.addEventListener('pointerdown',e=>{if(e.pointerType==='mouse')return;if(e.target&&e.target.closest&&e.target.closest('.plotly-graph-div,.js-plotly-plot'))return;clearTimeout(hoverTimer);unhoverAll();},true);
 window.addEventListener('ems-plot-ready',bind);bind();setInterval(bind,2000);
})();</script>"""


def _events_block() -> str:
    return """
<details class="info-panel events-panel" id="events-panel"><summary><span class="an-dot neutral" id="events-dot"></span>☷ Ereignisse &amp; Bedienverlauf <small id="events-summary">wird geprüft …</small></summary>
 <div class="events-filter" id="events-filter">
  <button type="button" data-lvl="info" class="info">Info</button>
  <button type="button" data-lvl="warning" class="warn">Warnung</button>
  <button type="button" data-lvl="error" class="err">Fehler</button>
 </div>
 <div id="events-list" class="events-list">wird geladen …</div>
</details>
<script>(function(){
 const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
 const listEl=document.getElementById('events-list');
 let LAST=[];
 const lvlClass=l=>{l=String(l||'info');return l.indexOf('err')===0?'error':l.indexOf('warn')===0?'warning':'info';};
 const icon=l=>l==='error'?'⛔':l==='warning'?'⚠':'ℹ';
 const active=()=>{try{return new Set(JSON.parse(localStorage.getItem('ems-event-filter'))||['info','warning','error']);}catch(e){return new Set(['info','warning','error']);}};
 function render(){
  const act=active();
  const rows=LAST.filter(e=>act.has(lvlClass(e.level)));
  listEl.innerHTML=rows.length?rows.map(e=>'<div class="event '+esc(e.level)+' k-'+esc(e.kind)+'"><time>'+new Date(e.ts).toLocaleString('de-DE',{day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'})+'</time><span><i class="ev-ic">'+icon(lvlClass(e.level))+'</i>'+esc(e.message)+'</span></div>').join(''):'<p>Keine Einträge für diese Filterauswahl.</p>';
  listEl.scrollTop=0;   // neueste (oben) zeigen, NICHT ans Ende springen
 }
 function headline(){const dot=document.getElementById('events-dot'),summary=document.getElementById('events-summary'),err=LAST.filter(e=>lvlClass(e.level)==='error').length,warn=LAST.filter(e=>lvlClass(e.level)==='warning').length;dot.className='an-dot '+(err?'bad':warn?'warn':'ok');summary.textContent=err+' Fehler · '+warn+' Warnungen · '+LAST.length+' Einträge';}
 async function load(){try{let r=await fetch('api/events.json?_='+Date.now(),{cache:'no-store'});if(!r.ok)throw Error(r.status);LAST=(await r.json()).events||[];headline();render();}catch(e){listEl.textContent='Ereignisverlauf nicht erreichbar.';document.getElementById('events-dot').className='an-dot bad';document.getElementById('events-summary').textContent='nicht erreichbar';}}
 (function(){const act=active();document.querySelectorAll('#events-filter button').forEach(b=>{b.classList.toggle('on',act.has(b.dataset.lvl));b.addEventListener('click',()=>{const a=active();if(a.has(b.dataset.lvl))a.delete(b.dataset.lvl);else a.add(b.dataset.lvl);localStorage.setItem('ems-event-filter',JSON.stringify([...a]));b.classList.toggle('on',a.has(b.dataset.lvl));render();});});})();
 document.getElementById('events-panel').addEventListener('toggle',function(){if(this.open){load();listEl.scrollTop=0;}});load();setInterval(()=>{if(document.getElementById('events-panel').open)load();},10000);
})();</script>"""


def _analysis_block(headline=None) -> str:
    """Analyse-Panel für Entscheidungsgüte, Ersparnis inkl. Verlauf/Treiber
    und Akku-Gesundheit. Lädt lazy beim
    Aufklappen. `headline` (server-seitig, günstig) zeigt schon im eingeklappten
    Titel die Gesamt-Ersparnis + eine Status-Ampel."""
    if headline:
        summary = ('<summary><span class="an-dot ' + _esc(headline.get("status", "ok"))
                   + '"></span>◴ Analyse &amp; Gesundheit <small>'
                   + _esc(headline.get("text", "")) + '</small></summary>')
    else:
        summary = ('<summary><span class="an-dot neutral"></span>◴ Analyse &amp; Gesundheit '
                   '<small>Entscheidungsgüte · Ersparnis · Akku</small></summary>')
    body = """
 <h4>Entscheidungsgüte <small>was die Unsicherheit kostet · Ø je Tag</small></h4>
 <div class="tiles" id="an-pvalue"><span class="an-hint">wird geladen …</span></div>
 <h4>Ersparnis-Verlauf <small>validiert gegen die Zähler</small></h4>
 <div class="tiles" id="an-savings"><span class="an-hint">wird geladen …</span></div>
 <div class="sparkline" id="an-spark"></div>
 <h4>Treiber <small>Eigenverbrauch · Autarkie · Negativpreis · 30 Tage</small></h4>
 <div class="tiles" id="an-drivers"></div>
 <h4>Akku-Gesundheit <small>letzte 30 Tage</small></h4>
 <div class="tiles" id="an-bhealth"><span class="an-hint">wird geladen …</span></div>
</details>
<script>(function(){
 const g=id=>document.getElementById(id);
 const num=(v,d)=>(typeof v==='number'&&isFinite(v)?v.toLocaleString('de-DE',{maximumFractionDigits:d==null?1:d}):'–');
 const eur=v=>(typeof v==='number'?v.toLocaleString('de-DE',{minimumFractionDigits:2,maximumFractionDigits:2}):'–');
 function tile(v,l,s){return '<div class="tile"><div class="v">'+v+'</div><div class="l">'+l+'</div>'+(s?'<div class="s">'+s+'</div>':'')+'</div>';}
 function fail(id,msg){var e=g(id);if(e)e.innerHTML='<span class="an-hint">'+msg+'</span>';}
 function spark(weekly){var wk=(weekly||[]).slice(-12);if(!wk.length){g('an-spark').innerHTML='<span class="an-hint">noch keine Wochendaten</span>';return;}
  var mx=Math.max(1,...wk.map(x=>Math.abs(x.saved_eur)||0));
  g('an-spark').innerHTML=wk.map(x=>'<span class="bar'+((x.saved_eur||0)<0?' neg':'')+'" style="height:'+Math.max(4,Math.round(100*Math.abs(x.saved_eur||0)/mx))+'%" title="'+x.period+': '+eur(x.saved_eur)+' €"></span>').join('');}
 async function savings(){try{let r=await fetch('api/savings-history.json?_='+Date.now(),{cache:'no-store'});if(!r.ok)throw 0;let d=await r.json();let days=d.days||0,avg=days?d.total_saved_eur/days:null,wk=(d.weekly||[]).slice(-1)[0],dr=d.drivers||{};
  g('an-savings').innerHTML=tile(eur(d.total_saved_eur)+' €','Gesamt',days+' validierte Tage')
   +tile(eur(avg)+' €','Ø je Tag','')
   +tile(wk?eur(wk.saved_eur)+' €':'–','Letzte Woche',wk?wk.period:'noch keine');
  spark(d.weekly);
  g('an-drivers').innerHTML=tile(num(dr.self_consumption_pct)+' %','Eigenverbrauch',num(dr.self_consumed_kwh)+' / '+num(dr.pv_kwh)+' kWh PV')
   +tile(num(dr.autarky_pct)+' %','Autarkie','Netzbezug '+num(dr.import_kwh)+' kWh')
   +tile(num(dr.negative_price_export_kwh,2)+' kWh','Einspeisung @Neg.-Preis','möglichst 0');
 }catch(e){fail('an-savings','Ersparnis-Verlauf nicht erreichbar.');g('an-drivers').innerHTML='';}}
 async function bhealth(){try{let r=await fetch('api/battery-health.json?_='+Date.now(),{cache:'no-store'});if(!r.ok)throw 0;let d=await r.json();
  g('an-bhealth').innerHTML=tile(num(d.cycles_equiv),'Vollzyklen','äquiv. · '+num(d.throughput_kwh)+' kWh Durchsatz')
   +tile(num(d.time_full_pct)+' %','Zeit ~100 %',num(d.full_hours)+' h')
   +tile(num(d.time_empty_pct)+' %','Zeit ~min-SoC',num(d.empty_hours)+' h')
   +tile(num(d.soc_avg_pct)+' %','SoC Ø','min '+num(d.soc_min_pct)+' / max '+num(d.soc_max_pct)+' %');
 }catch(e){fail('an-bhealth','Akku-Gesundheit nicht erreichbar.');}}
 async function pvalue(){try{g('an-pvalue').innerHTML='<span class="an-hint">wird gerechnet (zwei Solverläufe je Tag) …</span>';let r=await fetch('api/plan-value.json?_='+Date.now(),{cache:'no-store'});if(!r.ok)throw 0;let d=await r.json();
  var rd=d.regret_days||0,td=d.timing_days||0,sd=d.discharge_scored_days||0;
  g('an-pvalue').innerHTML=tile(rd?eur(d.avg_forecast_regret_eur)+' €':'–','Prognose kostet',rd?('gegen Hellsicht · Ø '+rd+' Tag(e)'):'braucht abgeschlossene Tage')
   +tile(rd?eur(d.avg_replanning_gain_eur)+' €':'–','Nachplanen holt zurück','durch die 15-min-Neuberechnung')
   +tile(rd?eur(d.avg_total_gap_eur)+' €':'–','Restlücke','abgerechnet gegen Hellsicht')
   +tile(sd?num(d.discharge_score_percent)+' %':'–','Entlade-Timing',sd?('bewertbar an '+sd+' von '+td+' Tagen'):'kein Spielraum an '+td+' Tagen');
 }catch(e){fail('an-pvalue','Entscheidungsgüte nicht erreichbar.');}}
 let done=false;
 g('analysis-panel').addEventListener('toggle',function(){if(this.open&&!done){done=true;pvalue();savings();bhealth();}});
})();</script>"""
    return ('<details class="info-panel analysis-panel" id="analysis-panel">'
            + summary + body)


def _load_bias_quality_card(load_bias) -> tuple[str, str]:
    """Rollenden Last-Bias als fachlichen Prognosequalitätsstatus darstellen."""
    if not load_bias:
        return "", "neutral"
    alert = bool(load_bias.get("alert"))
    level = "partial" if alert else "current"
    median = load_bias.get("median_w")
    night = load_bias.get("night_median_w")
    # alert_scope ist None, wenn gar kein Alarm vorliegt - ein Default im get()
    # greift dann NICHT (der Schluessel existiert), und die Karte zeigte "None".
    scope = load_bias.get("alert_scope") or "Gesamt"
    threshold = load_bias.get("threshold_w", 100)
    days = load_bias.get("window_days", 7)
    samples = int(load_bias.get("n", 0) or 0)

    parts = []
    if night is not None:
        parts.append(f"Nacht {float(night):+.0f} W")
    if median is not None:
        parts.append(f"Gesamt {float(median):+.0f} W")
    # Die Richtung im Klartext dazuschreiben statt das Vorzeichen deuten zu
    # lassen (Konvention: Prognose minus Ist, siehe ems/quality.bias_w).
    direction = load_bias.get("direction")
    if direction and (night is not None or median is not None):
        parts.append(str(direction))
    state = " · ".join(parts) or "noch nicht auswertbar"
    if not alert:
        state = "kein systematischer Versatz · " + state
    detail = (
        f"{scope} {'über' if alert else 'unter'} der Schwelle "
        f"±{float(threshold):.0f} W · {samples} Paare · {days}-Tage-Fenster"
        " · historische Tagesstart-Prognosen; Modelländerungen laufen verzögert ein")
    return (
        f"<article class='quality-item {level}'>"
        "<div class='quality-source'>Lastprognose-Bias</div>"
        f"<div class='quality-state'>{_esc(state)}</div>"
        f"<div class='quality-detail'>{_esc(detail)}</div>"
        "</article>",
        level)


def _forecast_analysis_block(forecast_quality=None,
                             timezone="Europe/Berlin",
                             load_bias=None) -> str:
    """Gemeinsames Panel für Datenstatus, Güte, Reife und Archivanalysen."""
    quality_items, levels = _forecast_quality_cards(
        forecast_quality, timezone)
    bias_item, bias_level = _load_bias_quality_card(load_bias)
    all_items = quality_items + bias_item
    quality_html = (
        "<h4>Aktueller Datenstatus <small>im verwendeten Optimierungshorizont</small></h4>"
        f"<div class='quality-grid'>{all_items}</div>"
        if all_items else
        "<h4>Aktueller Datenstatus</h4>"
        "<span class='an-hint'>Noch kein Quellenstatus verfügbar.</span>")
    status_levels = levels + ([bias_level] if bias_item else [])
    dot = _panel_dot(status_levels) if status_levels else "neutral"
    current_count = sum(1 for level in levels if level == "current")
    summary_text = (
        f"{current_count}/{len(levels)} Quellen aktuell"
        if levels else "noch kein Quellenstatus")
    if forecast_quality:
        first_issue = next((
            source for source in forecast_quality
            if source.get("level") != "current"), None)
        if first_issue:
            summary_text += (
                f" · {first_issue.get('name', 'Quelle')}: "
                f"{first_issue.get('state', 'prüfen')}")
    if load_bias and load_bias.get("alert"):
        bias_value = (load_bias.get("night_median_w")
                      if load_bias.get("alert_scope") in ("Nacht", "Tag und Nacht")
                      else load_bias.get("median_w"))
        if bias_value is not None:
            summary_text += f" · Last-Bias {float(bias_value):+.0f} W"
    html = """
<details class="info-panel forecast-analysis-panel" id="forecast-analysis-panel">
 <summary><span class="an-dot __DOT__"></span>▦ Prognosen &amp; Qualität <small>__SUMMARY__</small></summary>
 <!--QUALITY-->
 <h4>Prognosegüte <small>WAPE gegen die Ist-Werte · 7 Tage (30 Tage)</small></h4>
 <div class="tiles forecast-accuracy-tiles" id="fa-accuracy"><span class="an-hint">wird beim Aufklappen gemessen …</span></div>
 <div class="facc-trend" id="fa-accuracy-trend"></div>
 <div class="forecast-analysis-toolbar">
  <label>Zieltag <input type="date" id="fa-day"></label>
  <button type="button" id="fa-today">Heute</button>
 <button type="button" id="fa-refresh">Neu laden</button>
  <span id="fa-status">wird beim Aufklappen geladen</span>
 </div>
 <h4>Tagesverlauf <small>PV-Ist · Solcast · pvlib · produktives Last-Soll</small></h4>
 <div class="forecast-day-stage">
  <div id="fa-day-loading" class="forecast-day-loading" role="status" aria-live="polite">
   <i></i><span>Neue Vergleichsdaten werden geladen …</span>
  </div>
  <div id="fa-day-comparison" class="forecast-analysis-chart day-comparison-chart"></div>
 </div>
 <h4>Kalibrierungsreife <small>Datenmenge · zeitliche Abdeckung · aktive und empfohlene Werte</small></h4>
 <div id="fa-calibration" class="calibration-grid"><span class="an-hint">wird geladen …</span></div>
 <h4>Kalibrierungsverlauf <small>Faktoren und Bandparameter nach Erstellungszeit</small></h4>
 <div id="fa-calibration-history" class="forecast-analysis-chart calibration-history-chart"></div>
 <div id="fa-calibration-changes" class="calibration-change-list"></div>
 <h4>Fehler-Heatmap <small>WAPE · Hover zeigt Bias und Stichprobe · letzte 30 Tage</small></h4>
 <div class="forecast-heat-grid">
  <div><b>PV-Prognose</b><div id="fa-heat-pv" class="forecast-analysis-chart"></div></div>
  <div><b>Lastprognose</b><div id="fa-heat-load" class="forecast-analysis-chart"></div></div>
 </div>
 <div class="forecast-vintage-head">
  <h4>Prognose-Vintages <small>produktiver Optimierereingang gegen Ist</small></h4>
  <div class="forecast-signal-switch">
   <button type="button" data-signal="pv" class="on">PV</button>
   <button type="button" data-signal="load">Last</button>
  </div>
 </div>
 <div id="fa-vintages" class="forecast-analysis-chart vintage-chart"></div>
</details>
<script>(function(){
 const panel=document.getElementById('forecast-analysis-panel'),day=document.getElementById('fa-day'),status=document.getElementById('fa-status'),todayBtn=document.getElementById('fa-today'),refreshBtn=document.getElementById('fa-refresh');
 let payload=null,signal='pv',loaded=false,pending=0,requestId=0,readyStatus='wird beim Aufklappen geladen';
 const dark=()=>document.documentElement.classList.contains('dark');
 const colors=()=>dark()?{text:'#e7edf4',grid:'#3b4a59',paper:'rgba(0,0,0,0)',actual:'#ffffff'}:{text:'#28323c',grid:'#e2e7ec',paper:'rgba(0,0,0,0)',actual:'#111827'};
 const config={responsive:true,displaylogo:false,modeBarButtonsToRemove:['lasso2d','select2d']};
 const esc=s=>String(s??'–').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
 const num=(v,d)=>(typeof v==='number'&&isFinite(v)?v.toLocaleString('de-DE',{maximumFractionDigits:d==null?1:d}):'–');
 const tile=(v,l,s)=>'<div class="tile"><div class="v">'+esc(v)+'</div><div class="l">'+esc(l)+'</div>'+(s?'<div class="s">'+esc(s)+'</div>':'')+'</div>';
 function busy(delta){pending=Math.max(0,pending+delta);const on=pending>0;panel.classList.toggle('loading',on);todayBtn.disabled=on;refreshBtn.disabled=on;day.disabled=on;refreshBtn.textContent=on?'Lädt …':'Neu laden';status.textContent=on?'Daten werden geladen …':readyStatus;}
 function trendSvg(trend){var t=(trend||[]).filter(x=>typeof x.pv_wape==='number'||typeof x.load_wape==='number');
  if(t.length<2)return '<span class="an-hint">Trend erscheint ab dem 2. Tag mit Daten</span>';
  var W=280,H=42,n=t.length,all=[];t.forEach(x=>{if(typeof x.pv_wape==='number')all.push(x.pv_wape);if(typeof x.load_wape==='number')all.push(x.load_wape);});var mx=Math.max(10,Math.max(...all));
  function line(key,color){var pts=t.map((x,i)=>typeof x[key]==='number'?((i/(n-1))*W).toFixed(1)+','+(H-(x[key]/mx)*H).toFixed(1):null).filter(Boolean).join(' ');return pts?'<polyline points="'+pts+'" fill="none" stroke="'+color+'" stroke-width="1.5" vector-effect="non-scaling-stroke"/>':'';}
  return '<svg viewBox="0 0 '+W+' '+H+'" preserveAspectRatio="none" class="facc-svg">'+line('pv_wape','#3a86c8')+line('load_wape','#e29a2d')+'</svg><div class="facc-legend"><span style="color:#3a86c8">■ PV</span> <span style="color:#e29a2d">■ Last</span> · WAPE % über '+n+' Tage</div>';
 }
 async function accuracy(){const el=document.getElementById('fa-accuracy');busy(1);try{let r=await fetch('api/forecast-accuracy.json?_='+Date.now(),{cache:'no-store'});if(!r.ok)throw Error(r.status);let d=await r.json(),a=d['7d']||{},b=d['30d']||{},pv=a.pv||{},lo=a.load||{},pv30=b.pv||{},lo30=b.load||{},nw=a.pv_nowcast||{},delta=nw.improvement_wape_pp;
  let nowcastValue=(nw.n&&typeof delta==='number'?(delta>0?'+':'')+num(delta)+' pp':'–'),nowcastDetail=nw.n?('n='+(nw.n||0)+' · '+(nw.winner==='nowcast'?'Nowcast besser':nw.winner==='ohne_nowcast'?'ohne Nowcast besser':'gleichauf')+' · '+(nw.operational_slots||0)+' Slots'):'Vergleich startet mit neuen Prognose-Vintages';
  el.innerHTML=tile(num(pv.wape_pct)+' %','PV WAPE','30 T: '+num(pv30.wape_pct)+' % · Bias '+num(pv.bias_w,0)+' W')+tile(num(lo.wape_pct)+' %','Last WAPE','30 T: '+num(lo30.wape_pct)+' % · Bias '+num(lo.bias_w,0)+' W')+tile(pv.source||'–','PV-Quelle','n='+(pv.n||0)+' Slots')+tile(nowcastValue,'PV-Nowcast Nutzen','WAPE-Verbesserung · '+nowcastDetail);document.getElementById('fa-accuracy-trend').innerHTML=trendSvg(d.trend);
  }catch(e){el.innerHTML='<span class="an-hint">Prognosegüte nicht erreichbar.</span>';}finally{busy(-1);}}
 function value(v){
  if(Array.isArray(v))return v.map(x=>typeof x==='number'?x.toLocaleString('de-DE',{maximumFractionDigits:3}):'–').join(' → ');
  if(v&&typeof v==='object')return Object.entries(v).map(([k,x])=>k+' '+(typeof x==='number'?x.toLocaleString('de-DE',{maximumFractionDigits:1})+' %':'–')).join(' · ');
  if(typeof v==='number')return v.toLocaleString('de-DE',{maximumFractionDigits:3});
  return String(v??'–').replaceAll('_',' ');
 }
 function calibration(c){
  const el=document.getElementById('fa-calibration'),cards=(c&&c.cards)||[];
  if(!cards.length){el.innerHTML='<span class="an-hint">Noch kein Kalibrierungsbericht vorhanden.</span>';return;}
  el.innerHTML=cards.map(x=>'<article class="calibration-card '+esc(x.level||'partial')+'"><header><div><b>'+esc(x.title)+'</b><small>'+esc(x.state)+'</small></div><strong>'+Math.round(x.confidence_pct||0)+' %</strong></header><div class="calibration-bar"><i style="width:'+Math.max(0,Math.min(100,x.confidence_pct||0))+'%"></i></div><p>'+esc(x.detail)+'</p><dl>'+((x.values||[]).map(v=>'<div><dt>'+esc(v.label)+'</dt><dd>'+esc(value(v.value))+'</dd></div>').join(''))+'</dl>'+(x.reason?'<footer>'+esc(x.reason)+'</footer>':'')+'</article>').join('')+'<small class="calibration-note">'+esc(c.note||'')+(c.generated?' · Stand '+new Date(c.generated).toLocaleString('de-DE',{day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'}):'')+'</small>';
 }
 function layout(title,height){
  const c=colors();return {title:{text:title,font:{size:13}},height:height,margin:{l:58,r:18,t:38,b:45},paper_bgcolor:c.paper,plot_bgcolor:c.paper,font:{color:c.text,size:11},xaxis:{gridcolor:c.grid},yaxis:{gridcolor:c.grid},legend:{orientation:'h',y:-.24},hovermode:'x unified',hoverlabel:{bgcolor:dark()?'#202b36':'#ffffff',bordercolor:dark()?'#526273':'#c8d0d8',font:{color:c.text}}};
 }
 function heat(id,h,title){
  const el=document.getElementById(id);
  if(!h||!h.samples){el.innerHTML='<span class="an-hint">Noch keine auswertbaren Prognose-/Ist-Paare.</span>';return;}
  const custom=h.bias_w.map((row,i)=>row.map((v,j)=>[v,(h.n[i]||[])[j]||0]));
  Plotly.react(el,[{type:'heatmap',x:h.hours,y:h.lead_buckets,z:h.wape,customdata:custom,zmin:0,zmax:60,
   colorscale:[[0,'#2f9e63'],[.35,'#b5c94a'],[.65,'#efb447'],[1,'#d14b57']],colorbar:{title:'WAPE %',thickness:12},
   hovertemplate:'Zielstunde %{x}:00<br>Vorlauf %{y}<br>WAPE %{z:.1f} %<br>Bias %{customdata[0]:.0f} W<br>n=%{customdata[1]}<extra></extra>'}],
   {...layout(title+' · '+h.samples+' Paare',270),hovermode:'closest',yaxis:{gridcolor:colors().grid,autorange:'reversed'}},config);
 }
 function dayComparison(d){
  const el=document.getElementById('fa-day-comparison');
  if(!d){el.innerHTML='<span class="an-hint">Für diesen Tag sind noch keine Vergleichsdaten vorhanden.</span>';return;}
  const specs=[
   ['pv_actual_w','PV Ist',dark()?'#ffd166':'#e87917','solid',2.8],
   ['solcast_w','Solcast','#4c9be8','solid',1.8],
   ['pvlib_w','pvlib','#43a66b','dash',1.8],
   ['load_forecast_w','Last-Soll','#d95f59','dot',1.8],
   ['load_actual_w','Last Ist','#d95f59','solid',2.2]
  ];
  const traces=specs.filter(s=>(d[s[0]]||[]).some(v=>typeof v==='number')).map(s=>({
   x:d.timestamps,y:d[s[0]],type:'scatter',mode:'lines',name:s[1],
   line:{color:s[2],dash:s[3],width:s[4]},
   hovertemplate:'%{x|%d.%m. %H:%M}<br>%{y:.0f} W<extra>'+s[1]+'</extra>'
  }));
  if(!traces.length){el.innerHTML='<span class="an-hint">Für diesen Tag sind noch keine Prognose-/Ist-Werte archiviert.</span>';return;}
  const lo=layout('Tagesvergleich · Rolling-Origin',330);
  lo.yaxis={title:'W',gridcolor:colors().grid,rangemode:'tozero'};
  lo.xaxis={gridcolor:colors().grid,tickformat:'%H:%M'};
  Plotly.react(el,traces,lo,config);
 }
 function calibrationHistory(c){
  const el=document.getElementById('fa-calibration-history'),list=document.getElementById('fa-calibration-changes'),h=(c&&c.history)||[];
  if(!h.length){el.innerHTML='<span class="an-hint">Die Änderungshistorie beginnt mit dem nächsten Kalibrierungslauf.</span>';list.innerHTML='';return;}
  const specs=[
   ['pv_factor','PV-Faktor','#e29a2d','pv_samples','pv_confidence_pct'],
   ['load_factor','Last-Faktor','#d95f59','load_samples','load_confidence_pct'],
   ['p10','P10-Band','#4c9be8','band_samples','band_confidence_pct'],
   ['p90','P90-Band','#43a66b','band_samples','band_confidence_pct']
  ];
  const traces=specs.filter(s=>h.some(x=>typeof x[s[0]]==='number')).map(s=>({
   x:h.map(x=>x.generated),y:h.map(x=>x[s[0]]),type:'scatter',mode:'lines+markers',name:s[1],
   customdata:h.map(x=>[x[s[3]]||0,x[s[4]]||0]),line:{color:s[2],width:1.8},
   hovertemplate:'%{x|%d.%m.%Y %H:%M}<br>Wert %{y:.3f}<br>n=%{customdata[0]} · Reife %{customdata[1]:.0f} %<extra>'+s[1]+'</extra>'
  }));
  const lo=layout('Archivierte Kalibrierungsstände',300);
  lo.yaxis={title:'Faktor / Anteil',gridcolor:colors().grid};
  lo.xaxis={gridcolor:colors().grid,tickformat:'%d.%m.'};
  Plotly.react(el,traces,lo,config);
  const changed=h.filter(x=>(x.changes||[]).length).slice(-6).reverse();
  list.innerHTML=changed.length?changed.map(x=>'<article><time>'+new Date(x.generated).toLocaleString('de-DE',{day:'2-digit',month:'2-digit',year:'numeric',hour:'2-digit',minute:'2-digit'})+'</time><span>'+x.changes.map(v=>esc(v.label)+' '+num(v.from,3)+' → '+num(v.to,3)).join(' · ')+'</span></article>').join(''):'<small class="an-hint">Bisher ist noch keine Wertänderung zwischen zwei Läufen archiviert.</small>';
 }
 function vintages(v){
  const el=document.getElementById('fa-vintages');
  if(!v||!(v.series||[]).length){el.innerHTML='<span class="an-hint">Für diesen Zieltag sind noch keine archivierten Prognosestände vorhanden.</span>';return;}
  const key=signal==='pv'?'pv_w':'load_w',palette=['#8e7cc3','#4c78a8','#72a0c1','#54a56b','#e2a03b','#d95f59'];
  let traces=(v.series||[]).map((s,i)=>({x:v.timestamps,y:s[key],type:'scatter',mode:'lines',
   name:new Date(s.issue).toLocaleString('de-DE',{day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'}),
   line:{color:palette[i%palette.length],width:1.6},opacity:.88,
   hovertemplate:'%{x|%d.%m. %H:%M}<br>%{y:.0f} W<extra>'+new Date(s.issue).toLocaleString('de-DE')+'</extra>'}));
  const actual=(v.actual||{})[key]||[];
  if(actual.some(x=>typeof x==='number'))traces.push({x:v.timestamps,y:actual,type:'scatter',mode:'lines',name:'Ist',
   line:{color:colors().actual,width:3},hovertemplate:'%{x|%d.%m. %H:%M}<br>Ist %{y:.0f} W<extra></extra>'});
  const lo=layout((signal==='pv'?'PV':'Hauslast')+' am '+new Date(v.day+'T12:00:00').toLocaleDateString('de-DE'),360);
  lo.yaxis={title:'W',gridcolor:colors().grid,rangemode:'tozero'};
  lo.xaxis={gridcolor:colors().grid,tickformat:'%H:%M'};
  Plotly.react(el,traces,lo,config);
 }
 function render(){if(!payload)return;dayComparison(payload.day_comparison);calibration(payload.calibration);calibrationHistory(payload.calibration);heat('fa-heat-pv',payload.heatmaps&&payload.heatmaps.pv,'PV');heat('fa-heat-load',payload.heatmaps&&payload.heatmaps.load,'Last');vintages(payload.vintages);}
 async function load(){
  const own=++requestId;busy(1);
  try{let q=day.value?'?day='+encodeURIComponent(day.value):'';let r=await fetch('api/forecast-analysis.json'+q+(q?'&':'?')+'_='+Date.now(),{cache:'no-store'});if(!r.ok)throw Error(r.status);let next=await r.json();if(own!==requestId)return;
   payload=next;day.value=(payload.vintages||{}).day||day.value;
   if(payload.available_from)day.min=payload.available_from;if(payload.available_to)day.max=payload.available_to;
   readyStatus=(payload.lookback_days||30)+' Tage · erstellt '+new Date(payload.generated).toLocaleTimeString('de-DE',{hour:'2-digit',minute:'2-digit'});render();
  }catch(e){if(own===requestId){readyStatus='nicht erreichbar';document.getElementById('fa-vintages').innerHTML='<span class="an-hint">Prognoseanalyse konnte nicht geladen werden.</span>';}}
  finally{busy(-1);}
 }
 panel.addEventListener('toggle',function(){if(this.open&&!loaded){loaded=true;load();accuracy();}});
 day.addEventListener('change',load);
 todayBtn.addEventListener('click',function(){day.value=new Date().toLocaleDateString('sv-SE');load();});
 refreshBtn.addEventListener('click',function(){load();accuracy();});
 document.querySelectorAll('.forecast-signal-switch button').forEach(b=>b.addEventListener('click',function(){signal=this.dataset.signal;document.querySelectorAll('.forecast-signal-switch button').forEach(x=>x.classList.toggle('on',x===this));vintages(payload&&payload.vintages);}));
 window.addEventListener('ems-theme-change',render);
})();</script>"""
    return (html.replace("<!--QUALITY-->", quality_html)
            .replace("__DOT__", dot)
            .replace("__SUMMARY__", _esc(summary_text)))


def _whatif_block(config) -> str:
    """What-if-Simulator (nur bei controls_enabled): Plan mit geändertem Modus/
    Preis-Faktor durchrechnen lassen (POST /api/whatif) und die Kennzahlen
    anzeigen – ohne zu steuern/persistieren."""
    if not getattr(config.dashboard, "controls_enabled", False):
        return ""
    return """
<details class="info-panel whatif-panel" id="whatif-panel"><summary><span class="an-dot neutral" id="whatif-dot"></span>⚗ What-if-Simulation <small id="whatif-summary">noch keine Simulation</small></summary>
 <div class="whatif-form">
  <label class="wi-field"><span>Modus</span><select id="wi-mode"><option value="">unverändert</option><option value="auto">auto</option><option value="asap">asap</option><option value="peak">peak</option><option value="late">late</option></select></label>
  <label class="wi-field"><span>Preis-Faktor</span><input type="number" id="wi-factor" value="1.0" step="0.1" min="0.1" max="5"></label>
  <button type="button" id="wi-run" class="primary">Simulieren</button>
 </div>
 <div id="whatif-result" class="whatif-result"><span class="an-hint">noch keine Simulation</span></div>
</details>
<script>(function(){
 const eur=v=>(typeof v==='number'?v.toLocaleString('de-DE',{minimumFractionDigits:2,maximumFractionDigits:2}):'–');
 const n1=v=>(typeof v==='number'?v.toLocaleString('de-DE',{maximumFractionDigits:1}):'–');
 const tile=(v,l,s)=>'<div class="tile"><div class="v">'+v+'</div><div class="l">'+l+'</div>'+(s?'<div class="s">'+s+'</div>':'')+'</div>';
 async function run(){var btn=document.getElementById('wi-run'),box=document.getElementById('whatif-result');btn.disabled=true;box.innerHTML='<span class="an-hint">rechnet …</span>';
  try{let body={mode:document.getElementById('wi-mode').value,price_factor:parseFloat(document.getElementById('wi-factor').value)||1};
   let r=await fetch('api/whatif',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
   if(!r.ok)throw Error((await r.text()).slice(0,160));let d=(await r.json()).result;
   box.innerHTML='<div class="whatif-head"><span class="mode-badge '+(d.mode||'')+'">'+(d.mode||'–')+'</span><small>Preis-Faktor '+n1(d.price_factor)+(d.infeasible?' · <b class="wi-bad">⚠ unzulässig</b>':'')+'</small></div>'
    +'<div class="tiles">'
    +tile(eur(d.total_cost_eur)+' €','erwartete Kosten','über den Horizont')
    +tile(n1(d.grid_import_kwh)+' kWh','Netzbezug','Einspeisung '+n1(d.grid_export_kwh)+' kWh')
    +tile(n1(d.end_soc_percent)+' %','End-SoC','')
    +'</div>';document.getElementById('whatif-dot').className='an-dot '+(d.infeasible?'bad':'ok');document.getElementById('whatif-summary').textContent=(d.infeasible?'unzulässig · ':'')+(d.mode||'–')+' · '+eur(d.total_cost_eur)+' €';
  }catch(e){box.innerHTML='<span class="an-hint">Simulation fehlgeschlagen: '+e.message+'</span>';document.getElementById('whatif-dot').className='an-dot bad';document.getElementById('whatif-summary').textContent='Simulation fehlgeschlagen';}finally{btn.disabled=false;}}
 document.getElementById('wi-run').addEventListener('click',run);
})();</script>"""


def _pv_confidence_block(auto_peak_basis) -> str:
    """Robuste PV-Planung sichtbar machen: je Horizont-Tag, ob Auto peak oder
    asap gewählt hat und auf welcher Basis (pessimistischer p10-Überschuss vs.
    Erwartung vs. Schwelle). Server-seitig gerendert aus der Optimierer-Basis;
    leer außer bei charge_strategy=auto."""
    if not auto_peak_basis:
        return ""
    labels = {"p10": "p10 ≥ Schwelle (robust)",
              "expected+p10-floor": "Erwartung ≥ Schwelle, p10 über Boden",
              "insufficient": "zu wenig p10-Überschuss"}

    def _day_label(day: str) -> str:
        try:
            return pd.Timestamp(day).strftime("%a %d.%m.")
        except Exception:
            return str(day)

    def _n(v):
        return f"{float(v):.1f}" if isinstance(v, (int, float)) else "–"

    values = list(auto_peak_basis.values())
    robust = sum(1 for value in values if value.get("basis") == "p10")
    insufficient = sum(
        1 for value in values if value.get("basis") == "insufficient")
    dot = ("bad" if insufficient == len(values)
           else "warn" if robust < len(values) else "ok")
    mode_counts = {}
    for value in values:
        mode = str(value.get("mode", "–"))
        mode_counts[mode] = mode_counts.get(mode, 0) + 1
    mode_summary = ", ".join(
        f"{mode} {count} T" for mode, count in mode_counts.items())
    summary = f"{robust}/{len(values)} Tage robust · {mode_summary}"

    cards = "".join(
        "<div class='pvconf-card'>"
        "<div class='pvconf-head'><span class='pvconf-day'>{d}</span>"
        "<span class='mode-badge {mode}'>{mode}</span></div>"
        "<div class='pvconf-metrics'><span>p10<b>{p10}</b></span>"
        "<span>Erwartung<b>{exp}</b></span><span>Schwelle<b>{thr}</b></span></div>"
        "<div class='pvconf-basis'>{basis}</div></div>".format(
            d=_esc(_day_label(day)), mode=_esc(str(b.get("mode", ""))),
            p10=_n(b.get("p10_kwh")), exp=_n(b.get("expected_kwh")),
            thr=_n(b.get("threshold_kwh")),
            basis=_esc(labels.get(b.get("basis"), str(b.get("basis", "")))))
        for day, b in auto_peak_basis.items())
    return ("<details class=\"info-panel pv-confidence-panel\" id=\"pvconf-panel\">"
            f"<summary><span class=\"an-dot {dot}\"></span>☀ PV-Konfidenz &amp; "
            f"Auto-Modus <small>{_esc(summary)}</small></summary>"
            "<div class=\"pvconf-grid\">" + cards + "</div>"
            "<p class=\"pvconf-note\">Peak nur, wenn der pessimistische (p10-)"
            "Überschuss die Schwelle trägt – sonst asap.</p></details>")


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
  const dark=document.documentElement.classList.contains('dark'),grid=dark?'#354352':'#e3e8ed',font=dark?'#e7edf4':'#27313a',colors={asap:'#28a261',peak:'#e29a2d',late:'#9b6bd3'},tr=[];
  if(s.pv_w)tr.push({x:s.timestamp,y:s.pv_w,name:'PV-Prognose',line:{color:dark?'#6f7d8a':'#c3ccd5',width:1},fill:'tozeroy',fillcolor:dark?'rgba(120,140,160,0.14)':'rgba(150,167,181,0.16)',hoverinfo:'skip'});
  Object.entries(s.modes).filter(([k])=>k!=='auto').forEach(([k,v])=>{tr.push({x:s.timestamp,y:v.battery_w,name:emsModeName(k)+' · Akku',line:{color:colors[k]}});tr.push({x:s.timestamp,y:v.soc_percent,name:emsModeName(k)+' · SoC',yaxis:'y2',line:{color:colors[k],dash:'dot'}});tr.push({x:s.timestamp,y:v.grid_w,name:emsModeName(k)+' · Netz',visible:'legendonly',line:{color:colors[k],dash:'dash'}});});chart.style.display='block';Plotly.react(chart,tr,
  {height:380,autosize:true,hovermode:'x unified',paper_bgcolor:'rgba(0,0,0,0)',plot_bgcolor:'rgba(0,0,0,0)',font:{color:font},hoverlabel:{bgcolor:dark?'#202b36':'#ffffff',bordercolor:dark?'#536273':'#cfd7df',font:{color:font}},margin:{l:52,r:48,t:18,b:76},legend:{orientation:'h',x:0,y:-.2,font:{size:10}},xaxis:{gridcolor:grid},yaxis:{title:'Leistung W (Akku · PV)',gridcolor:grid,zerolinecolor:grid},yaxis2:{title:'SoC %%',overlaying:'y',side:'right',range:[0,100],gridcolor:grid}},{responsive:true,displaylogo:false,displayModeBar:false});}
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
        "<details class='controls info-panel' id='ems-controls'><summary>"
        f"<span class='an-dot {'ok' if e3dc_on else 'warn'}'></span>⚙ Steuerung"
        f"<small>E3/DC {'aktiv' if e3dc_on else 'aus'} · "
        f"{_esc(mode_text.get(strat, (strat, ''))[0])}</small>"
        "</summary><div class='ctl-body'>"
        "<div class='ctl-section-head'><b>Steuerbare Lasten</b>"
        "<small>Parameter und zeitlicher Leistungsverlauf</small></div>"
        f"<div class='load-cards'>{''.join(rows)}</div>"
        f"{e3dc_control}"
        f"<div class='ctl-section mode-select'><div><b>Optimierungsmodus</b>"
        f"<small>Ladestrategie des Hausakkus</small></div><div class='button-group'>{mode_btns}</div></div>"
        f"{compare}{battery}<div id='ctl-msg' class='ctl-msg'></div>"
        f"</div></details><script>{js}</script>")



def _load_profile_block(config) -> str:
    """Gelernte Lastprofile verschiebbarer Lasten (unter Betriebsdiagnose).

    Rein informativ: das Anlernen laeuft passiv aus der Verbrauchsrueckmeldung,
    es gibt nichts zu starten. Gezeigt wird, was gelernt wurde und ob es schon
    in der Planung steckt.
    """
    loads = [ld for ld in (getattr(config, "controllable_loads", []) or [])
             if ld.type == "deferrable"]
    if not loads:
        return ""
    # "In der Planung" heisst: die Last ist AKTIV und hat ein Profil. Nur
    # power_profile_w zu zaehlen log dreifach: Platzhalter galten als gelernt,
    # deaktivierte Lasten als eingeplant, und 4/4 stand da, waehrend nichts
    # gelernt war. Anlern-Lasten (enabled: false) werden getrennt genannt.
    planned = sum(1 for load in loads if load.power_profile_w and load.enabled)
    learning = sum(1 for load in loads
                   if not load.enabled and getattr(load, "power_topic", None))
    # Gruen nur, wenn wirklich alle aktiven Lasten ein Profil fahren;
    # Anlern-Lasten sind kein Fehler, aber auch kein "fertig".
    active_loads = sum(1 for load in loads if load.enabled)
    initial_dot = ("ok" if active_loads and planned == active_loads
                   else "warn")
    html = """<details class="info-panel" id="profile-panel">
<summary><span class="an-dot __PROFILE_DOT__" id="lp-dot"></span>▥ Gelernte Lastprofile
 <small id="lp-summary">__PROFILE_SUMMARY__</small></summary>
<div class="tiles" id="lp-tiles"><span class="an-hint">wird geladen …</span></div>
<div class="hint" id="lp-hint"></div>
<script>(function(){
 const g=id=>document.getElementById(id);
 const num=(v,d)=>(typeof v==='number'&&isFinite(v)?v.toLocaleString('de-DE',{maximumFractionDigits:d==null?0:d}):'–');
 function bars(p){
  if(!p||!p.length)return '';
  const mx=Math.max.apply(null,p)||1;
  return '<div class="lp-bars">'+p.map(v=>'<span style="height:'+Math.max(3,Math.round(100*v/mx))+'%" title="'+num(v)+' W"></span>').join('')+'</div>';
 }
 async function load(){
  try{
   const r=await fetch('api/load-profiles.json?_='+Date.now(),{cache:'no-store'});
   if(!r.ok)throw Error(r.status);
   const d=await r.json();
   const rows=d.loads||[];
   if(!rows.length){g('lp-tiles').innerHTML='<span class="an-hint">Keine verschiebbare Last konfiguriert.</span>';g('lp-dot').className='an-dot neutral';g('lp-summary').textContent='keine Last konfiguriert';return;}
   const learned=rows.filter(x=>x.learned&&x.learned.usable).length,inUse=rows.filter(x=>x.in_use).length;
   g('lp-dot').className='an-dot '+(learned===rows.length?'ok':inUse?'warn':'bad');
   g('lp-summary').textContent=learned+'/'+rows.length+' gelernt · '+inUse+' in der Planung';
   g('lp-tiles').innerHTML=rows.map(function(x){
    const learned=x.learned;
    if(!learned)return '<div class="tile"><div class="v">–</div><div class="l">'+x.name+'</div><div class="s">'+(x.reason||'noch nichts gelernt')+'</div></div>';
    return '<div class="tile'+(x.in_use?'':' warn')+'"><div class="v">'+num(learned.runtime_minutes)+' min</div>'
      +'<div class="l">'+x.name+'</div>'
      +'<div class="s">'+learned.n_runs+' Läufe · '+num(learned.energy_kwh,2)+' kWh · Spitze '+num(learned.peak_w)+' W'
      +(x.in_use?' · in der Planung':' · noch nicht übernommen')+'</div>'
      +bars(learned.power_w)+'</div>';
   }).join('');
   g('lp-hint').textContent='Übernommen wird ab '+(d.min_runs||3)+' Läufen; die wöchentliche Kalibrierung trägt das Profil ein. Ohne Rückmeldung (power_topic) bleibt die Last bei konstanter Leistung.';
  }catch(e){g('lp-tiles').innerHTML='<span class="an-hint">Lastprofile nicht erreichbar.</span>';g('lp-dot').className='an-dot bad';g('lp-summary').textContent='nicht erreichbar';}
 }
 let done=false;
 g('profile-panel').addEventListener('toggle',function(){if(this.open&&!done){done=true;load();}});
})();</script></details>"""
    return (html.replace("__PROFILE_DOT__", initial_dot)
            .replace("__PROFILE_SUMMARY__",
                     f"{planned}/{len(loads)} in der Planung"
                     + (f" · {learning} im Anlernen" if learning else "")))

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


def _panel_dot(levels) -> str:
    """Ampel-Punktklasse (.an-dot) für die Panel-Kopfzeile – wie beim
    Analyse-Panel, damit die Diagnose-Panels denselben Look tragen."""
    return {"current": "ok", "partial": "warn", "replaced": "bad"}.get(
        _panel_level(levels), "ok")


def _forecast_quality_cards(quality, timezone="Europe/Berlin"):
    """Karten + Statusstufen der operativen Prognosequellen."""
    if not quality:
        return "", []
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
    return "".join(items), levels


def _forecast_quality_block(quality, timezone="Europe/Berlin") -> str:
    """Operative Prognosequalität je Quelle und aktuellem Horizont."""
    items, levels = _forecast_quality_cards(quality, timezone)
    if not items:
        return ""
    return (f'<details class="info-panel"><summary>'
            f'<span class="an-dot {_panel_dot(levels)}"></span>⌁ Prognosequalität '
            "<small>verwendete Daten im aktuellen Optimierungshorizont</small>"
            f"</summary><div class='quality-grid'>{items}</div></details>")


def _operations_block(solver, execution, timezone="Europe/Berlin",
                      diagnostics=None, plan_status=None) -> str:
    """Kompakte, standardmaessig eingeklappte Betriebsdiagnose."""
    diagnostics = diagnostics or {}
    plan_status = plan_status or {}
    if not solver and not execution and not diagnostics and not plan_status:
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
        # Welcher Slot wurde geprüft? Die bestätigte Prüfung braucht die
        # E3DC-Zählerenergie und hängt darum rund eine Stunde nach - ohne den
        # Zeitbezug wirkt eine gelbe Diagnose wie ein AKTUELLES Problem, obwohl
        # sie einen länger vergangenen Slot bewertet.
        slot_hint = ""
        _slot = planned.get("issued_at") or execution.get("slot")
        if _slot:
            try:
                _ts = pd.Timestamp(_slot)
                if _ts.tzinfo is not None:
                    _ts = _ts.tz_convert(timezone)
                slot_hint = f"Slot {_ts.strftime('%d.%m. %H:%M')} · "
            except Exception:
                slot_hint = ""
        detail = (slot_hint
                  + f"Netz {_w(planned.get('grid_w'))} → {_w(actual.get('grid_w'))} · "
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

    soc_drift = diagnostics.get("soc_drift")
    if soc_drift and soc_drift.get("mae_pp") is not None:
        mae = float(soc_drift["mae_pp"])
        threshold = float(soc_drift.get("threshold_pp", 0.0))
        level = "partial" if soc_drift.get("alert") else "current"
        levels.append(level)
        cards.append(
            f"<article class='quality-item {level}'>"
            "<div class='quality-source'>SoC-Prognose ↔ Ist</div>"
            f"<div class='quality-state'>MAE {mae:.1f} Prozentpunkte</div>"
            f"<div class='quality-detail'>{float(soc_drift.get('window_hours', 0)):.0f}-h-Fenster"
            f" · Warnschwelle {threshold:.1f} pp"
            " · kurzfristige Abweichung des SoC-Modells</div></article>")

    efficiency = diagnostics.get("efficiency")
    if efficiency:
        level = "partial" if efficiency.get("alert") else "current"
        levels.append(level)
        cards.append(
            f"<article class='quality-item {level}'>"
            "<div class='quality-source'>Entladewirkungsgrad</div>"
            f"<div class='quality-state'>gemessen {float(efficiency.get('measured', 0)):.3f}"
            f" · Modell {float(efficiency.get('model', 0)):.3f}</div>"
            f"<div class='quality-detail'>Abweichung "
            f"{float(efficiency.get('deviation_percent', 0)):+.1f} %"
            f" · {int(efficiency.get('windows', 0))} Entladephasen / "
            f"{float(efficiency.get('hours', 0)):.0f} h"
            f" · Schwelle ±{float(efficiency.get('threshold_percent', 0)):.1f} %"
            "</div></article>")

    execution_bias = diagnostics.get("execution_bias")
    if execution_bias:
        level = "partial" if execution_bias.get("alert") else "current"
        levels.append(level)
        cards.append(
            f"<article class='quality-item {level}'>"
            "<div class='quality-source'>Ausführungs-Bias</div>"
            f"<div class='quality-state'>Median "
            f"{float(execution_bias.get('median_w', 0)):+.0f} W"
            f" · {float(execution_bias.get('kwh_per_day', 0)):+.2f} kWh/Tag</div>"
            f"<div class='quality-detail'>{int(execution_bias.get('n', 0))} Zählerprüfungen"
            f" · {float(execution_bias.get('window_days', 0)):.0f}-Tage-Fenster"
            f" · Schwelle ±{float(execution_bias.get('threshold_w', 0)):.0f} W"
            " · systematische Abweichung vom Sollfahrplan</div></article>")

    infeasible = bool(plan_status.get("infeasible"))
    shortfall_wh = float(plan_status.get("car_target_shortfall_wh", 0.0) or 0.0)
    overload_wh = float(plan_status.get("grid_overload_wh", 0.0) or 0.0)
    if infeasible:
        levels.append("replaced")
        reason = plan_status.get("infeasible_reason") or "keine Ursache ermittelt"
        cards.append(
            "<article class='quality-item replaced'>"
            "<div class='quality-source'>Planlösbarkeit</div>"
            f"<div class='quality-state'>{_esc(plan_status.get('status', 'unlösbar'))}</div>"
            f"<div class='quality-detail'>{_esc(reason)}</div></article>")
    if shortfall_wh > 100.0:
        levels.append("partial")
        cards.append(
            "<article class='quality-item partial'>"
            "<div class='quality-source'>Fahrzeug-Ziel</div>"
            f"<div class='quality-state'>{shortfall_wh / 1000.0:.1f} kWh fehlen</div>"
            "<div class='quality-detail'>Ziel-SoC ist bis zur Abfahrt mit den "
            "aktuellen Grenzen und Prognosen nicht vollständig erreichbar.</div></article>")
    if overload_wh > 100.0:
        levels.append("partial")
        cards.append(
            "<article class='quality-item partial'>"
            "<div class='quality-source'>Hausanschluss-Grenze</div>"
            f"<div class='quality-state'>{overload_wh / 1000.0:.1f} kWh Überschreitung</div>"
            "<div class='quality-detail'>Eine geplante Lastspitze war innerhalb "
            "der verfügbaren Freiheitsgrade nicht anders deckbar.</div></article>")
    headline = []
    if solver:
        headline.append(
            f"Solver {float(solver.get('seconds', 0.0)):.1f} s"
            + (" langsam" if solver.get("slow") else ""))
    if execution:
        headline.append(
            "Soll erfüllt" if execution.get("ok")
            else _esc(execution.get("message", "Planabweichung")))
    diagnostic_alerts = sum(
        1 for item in diagnostics.values()
        if isinstance(item, dict) and item.get("alert"))
    if diagnostic_alerts:
        headline.append(f"{diagnostic_alerts} Modellwarnung"
                        + ("en" if diagnostic_alerts != 1 else ""))
    plan_alerts = int(infeasible) + int(shortfall_wh > 100.0) + int(overload_wh > 100.0)
    if plan_alerts:
        headline.append(f"{plan_alerts} Planziel"
                        + ("e auffällig" if plan_alerts != 1 else " auffällig"))
    return (f'<details class="info-panel"><summary>'
            f'<span class="an-dot {_panel_dot(levels)}"></span>⚙ Betriebsdiagnose '
            f"<small>{' · '.join(headline)}</small>"
            f"</summary><div class='quality-grid'>{''.join(cards)}</div></details>")


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
    configured_feedback = [
        item for item in (feedback or []) if item.get("configured")]
    fresh = sum(1 for item in configured_feedback if item.get("fresh"))
    running = sum(
        1 for item in configured_feedback
        if item.get("fresh") and item.get("on"))
    applied_models = sum(
        1 for item in (calibrations or []) if item.get("applied"))
    headline = (
        f"{fresh}/{len(configured_feedback)} Rückmeldungen frisch"
        f" · {running} läuft"
        + (f" · {applied_models} Thermomodell aktiv"
           if calibrations else ""))
    return (f'<details class="info-panel"><summary>'
            f'<span class="an-dot {_panel_dot(levels)}"></span>♨ Last-Rückkopplung '
            f"<small>{headline}</small>"
            f"</summary><div class='quality-grid'>{''.join(cards)}</div></details>")


def build_dashboard(config: Config, table: pd.DataFrame, total_cost_ct: float,
                    export_line_w=None, savings_eur=None, violations=None,
                    load_temp_actual=None, ambient_temp_c=None,
                    source_status=None, pv_compare=None,
                    control_status=None, forecast_quality=None,
                    solver_status=None, execution_status=None,
                    load_feedback_status=None,
                    thermal_calibration=None, auto_peak_basis=None,
                    load_bias=None, monitoring_status=None,
                    plan_status=None) -> str:
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots

    t = table
    x = t.index
    now = pd.Timestamp.now(tz=x.tz)
    load_temp_actual = load_temp_actual or {}

    # Günstige Kennzahl für den (eingeklappten) Analyse-Panel-Titel + Ampel:
    # Gesamt-Ersparnis (kleine Tabelle) und Akku-Vollstand-Anteil (Ist-Werte).
    analysis_headline = None
    try:
        from .observability import battery_health, savings_over_time
        _sv = savings_over_time(config.e3dc_rscp.history_db_path)
        _bh = battery_health(config, days=30)
        _full = _bh.get("time_full_pct")
        _txt = ("Ersparnis %s € · %d Tage" % (
            f"{_sv.get('total_saved_eur', 0.0):.2f}".replace(".", ","),
            int(_sv.get("days", 0))))
        if isinstance(_full, (int, float)):
            _txt += " · Akku %.0f %% Zeit voll" % _full
        analysis_headline = {
            "status": ("warn" if isinstance(_full, (int, float)) and _full >= 25.0
                       else "ok"),
            "text": _txt}
    except Exception as exc:  # Analyse-Kennzahl darf den Build nie stören
        log.debug("Analyse-Headline nicht berechenbar: %s", exc)

    loads_cfg = list(getattr(config, "controllable_loads", []) or [])
    has_loads = len(loads_cfg) > 0
    thermal_cfg = [ld for ld in loads_cfg if ld.type == "thermal"]
    has_thermal = len(thermal_cfg) > 0
    if has_loads:
        titles = ["<b>Leistung</b>", "<b>Ladezustand</b>", "<b>Strompreis</b>",
                  "<b>Steuerung</b>", "", "<b>Steuerbare Lasten</b>"]
        # Die Lastenleiste muss mit der Spurenzahl wachsen. Bei fester Hoehe
        # blieben von 12 Spuren nur ~8 px je Zeile - Plotly liess dann jedes
        # zweite Tick-Label weg und es sah aus, als fehlten Lasten.
        # Eine Zeile je Stufe (Soll und Ist sind zusammengefasst).
        lane_count = sum(len(_ld.stages) if _ld.type == "thermal" else 1
                         for _ld in loads_cfg)
        lane_share = min(0.30, max(0.105, 0.032 * lane_count))
        rest = 1.0 - lane_share - 0.045
        row_heights = [0.33 * rest / 0.85, 0.14 * rest / 0.85,
                       0.14 * rest / 0.85, 0.24 * rest / 0.85,
                       0.045, lane_share]
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
             hover=HOVER_W, compare_col=None, compare_unit="W",
             compare_decimals=0):
        if col in t.columns and t[col].notna().any():
            values = pd.to_numeric(t[col], errors="coerce")
            # Istwerte dürfen nie rechts vom Jetzt-Marker erscheinen. Diese
            # zusätzliche UI-Grenze schützt auch gegen vorgefüllte/stale API-
            # Frames und vermeidet scheinbare Messwerte in Zukunftsslots.
            if col.startswith("actual_"):
                values = values.where(x <= now)
            customdata = None
            hovertemplate = f"{name}: {hover}"
            if compare_col in t.columns:
                actual = values
                planned = pd.to_numeric(t[compare_col], errors="coerce")
                customdata = (actual - planned).to_list()
                fmt = f",.{int(compare_decimals)}f"
                base_name = name.replace(" (Ist)", "")
                hovertemplate += (
                    f"<br>{base_name} (Δ): "
                    f"%{{customdata:{fmt}}} {compare_unit}")
            hovertemplate += "<extra></extra>"
            fig.add_trace(go.Scatter(
                x=x, y=values, name=name, mode="lines",
                line=dict(color=color, width=width, dash=dash,
                          shape=shape or "linear"),
                customdata=customdata, hovertemplate=hovertemplate,
                legendrank=_GROUP_RANK.get(group, 1000),
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
    line("actual_pv_w", "PV (Ist)", "#ff7f0e", 1, "ist",
         compare_col="pv_w")
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
    line("actual_load_w", "Verbrauch (Ist)", "#d62728", 1, "ist",
         compare_col="house_load_w")
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
        from .loads import _slug as _lslug
        _cl_cols = [c for c in t.columns if c.startswith("load_") and c.endswith("_w")]
        if _cl_cols:
            cl_sum = t[_cl_cols].sum(axis=1)
            if float(cl_sum.abs().sum()) > 0:
                fig.add_trace(go.Scatter(
                    x=x, y=cl_sum, name="Steuerb. Lasten", mode="lines",
                    line=dict(color="#9467bd", width=1.6, dash="dot"),
                    hovertemplate=HOVER_W, legendgroup="prog",
                    legendgrouptitle_text=_GROUPS["prog"]), row=1, col=1)
        # Verschiebbare Lasten mit Leistungsmessung einzeln als Soll/Ist
        # darstellen. Das Summensignal oben bleibt für die Gesamtwirkung
        # erhalten, die Einzelkurven erlauben die Ausführungskontrolle.
        for ld in loads_cfg:
            if ld.type != "deferrable":
                continue
            sg = _lslug(ld.name)
            planned_col = f"load_{sg}_w"
            actual_col = f"actual_load_{sg}_power_w"
            if actual_col in t.columns and t[actual_col].notna().any():
                line(planned_col, f"{ld.name} (Soll)", "#9467bd", 1, "prog",
                     dash="dash", width=1.4)
                line(actual_col, f"{ld.name} (Ist)", "#6f42c1", 1, "ist",
                     width=1.8, compare_col=planned_col)
    line("actual_grid_w", "Netz (Ist)", "#1f77b4", 1, "ist", width=1.8,
         compare_col="planned_grid_w")
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
         hover=HOVER_PCT, compare_col="house_soc_percent", compare_unit="%",
         compare_decimals=0)
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
        # Was der 00:00-Plan geschätzt hatte, wo der Börsenpreis inzwischen
        # veröffentlicht ist: die Lücke zur durchgezogenen Linie IST der
        # Schätzfehler. Im laufenden Plan selbst ist das nicht sichtbar - dort
        # sind geschätzte Slots genau die ohne Börsenpreis.
        if "plan0_price_ct_kwh" in t.columns:
            guess = pd.to_numeric(t["plan0_price_ct_kwh"], errors="coerce")
            known = guess.notna() & price.notna() & ~est
            if bool(((guess - price).abs() > 0.05)[known].any()):
                fig.add_trace(go.Scatter(
                    x=x, y=guess.where(known),
                    name="Preis (Schätzung 00:00)", mode="lines",
                    legendgroup="progb", legendrank=_GROUP_RANK["progb"],
                    legendgrouptitle_text=_GROUPS["progb"],
                    hovertemplate=HOVER_CT,
                    line=dict(color="#c49a94", width=1.4, shape="hv",
                              dash="dot")),
                    row=3, col=1)
    line("feedin_ct_kwh", "Einspeisevergütung", "#2ca02c", 3, "progb",
         width=1.2, shape="hv", hover=HOVER_CT)

    # ---------- Panel 4: Steuerung ----------
    def bar(col, name, color, sign=1):
        if col in t.columns and t[col].abs().max() > 1:
            fig.add_trace(go.Bar(x=x, y=sign * t[col], name=name,
                                 marker_color=color,
                                 hovertemplate=(
                                     f"{name}: {HOVER_W}<extra></extra>"),
                                 legendgroup="ctrl",
                                 legendgrouptitle_text=_GROUPS["ctrl"]),
                          row=4, col=1)

    bar("batt_dc_charge_w", "Akku Laden (PV)", "#2ca02c")
    bar("batt_ac_charge_w", "Akku Netzladen", "#1f77b4")
    bar("batt_discharge_w", "Akku Entladen", "#d62728", sign=-1)
    bar("batt_grid_discharge_w", "Akku Netz-Entladen", "#9400d3", sign=-1)
    bar("car_charge_w", "Auto-Laden", "#9467bd")
    bar("pv_curtail_w", "PV-Abregelung", "#7f7f7f", sign=-1)
    line("planned_battery_w", "Akku-Leistung (Soll)", "#2ca02c", 4, "ctrl",
         dash="dash", width=2)
    if "actual_battery_w" in t.columns:
        line("actual_battery_w", "Akku-Leistung (Ist)", "#111111", 4, "ctrl",
             width=1.8, compare_col="planned_battery_w")

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
        # EINE Zeile je Stufe: der Zustand kodiert das PAAR aus Soll und Ist.
        # Vorher belegte jede Stufe zwei Zeilen, und die eigentlich
        # interessante Frage - stimmt Ist mit Soll ueberein? - musste man mit
        # dem Auge aus zwei Zeilen zusammenreimen. Jetzt sind genau die beiden
        # Konfliktfaelle eigene Farben: "geplant, laeuft nicht" ist ein
        # Ausfuehrungsproblem, "laeuft ungeplant" Fremdbedienung oder ein
        # haengender Schalter. Nebeneffekt: halbe Zeilenzahl.
        lanes = []   # (label, plan_col, actual_col|None, enabled, owner)
        for ld in loads_cfg:
            if ld.type == "thermal":
                sg = _lslug(ld.name)
                for st in ld.stages:
                    stage_slug = _lslug(st.name)
                    lanes.append((
                        f"{ld.name} / {st.name}",
                        f"load_{sg}_{stage_slug}_w",
                        (f"actual_load_{sg}_{stage_slug}_on"
                         if (st.feedback_topic or st.power_topic) else None),
                        ld.enabled, ld))
            else:
                sg = _lslug(ld.name)
                lanes.append((
                    ld.name, f"load_{sg}_w",
                    (f"actual_load_{sg}_on"
                     if (ld.feedback_topic or ld.power_topic) else None),
                    ld.enabled, ld))

        # Zustandscodes. Reihenfolge = Reihenfolge der Farbstufen unten UND in
        # paint() fuer den Dunkelmodus - beide muessen dieselbe Zahl an Stufen
        # haben, sonst verschieben sich die Farben gegen die Codes.
        OFF, RUN, DISABLED, UNKNOWN, ARMED, MISSING, UNPLANNED = range(7)
        _lab = {OFF: "aus", RUN: "läuft wie geplant",
                DISABLED: "deaktiviert", UNKNOWN: "Ist unbekannt",
                ARMED: "freigegeben, heizt nicht",
                MISSING: "geplant, läuft nicht",
                UNPLANNED: "läuft, nicht geplant"}

        ylabels, z, hover = [], [], []
        for label, plan_col, actual_col, enabled, owner in lanes:
            ylabels.append(label)
            if not enabled or plan_col not in t.columns:
                # Deaktiviert heisst NICHT unsichtbar: eine Last kann laufen,
                # ohne dass wir sie steuern (Anlern-Phase, Handbetrieb). Sie
                # pauschal grau zu malen hat genau diese Information vernichtet
                # - der laufende Trockner war nicht mehr zu sehen.
                real = (pd.to_numeric(t[actual_col], errors="coerce")
                        if actual_col and actual_col in t.columns
                        else pd.Series(float("nan"), index=x))
                row, texts = [], []
                for stamp, actual_v in zip(x, real):
                    if stamp > now or not actual_col:
                        row.append(DISABLED)
                        texts.append(_lab[DISABLED])
                    elif pd.isna(actual_v):
                        row.append(UNKNOWN)
                        texts.append("nicht gesteuert · Ist unbekannt")
                    elif float(actual_v) > 0.5:
                        row.append(UNPLANNED)
                        texts.append("nicht gesteuert · Ist läuft")
                    else:
                        row.append(DISABLED)
                        texts.append("nicht gesteuert · Ist aus")
                z.append(row)
                hover.append(texts)
                continue
            power = pd.to_numeric(t[plan_col], errors="coerce").fillna(0.0)
            planned = power.gt(5.0)
            # "Freigabe steht, aber Plan 0 W": bei T >= Geraete-Abschaltpunkt
            # bleibt die Freigabe an (weniger Schaltspiele). Als "aus" gezeigt
            # war das getarnt - genau dieser Zustand hat einmal 21 kWh ueber
            # Nacht gekostet, weil das Geraet trotz Freigabe lief.
            armed = pd.Series(False, index=x)
            if owner is not None and getattr(owner, "thermostat", False):
                tcol = f"load_{_lslug(owner.name)}_temp_c"
                cutoff = getattr(owner, "thermostat_cutoff_c", None)
                if cutoff is None:
                    cutoff = getattr(owner, "target_c", None)
                limit = getattr(owner, "max_c", None)
                if tcol in t.columns and cutoff is not None:
                    temps = pd.to_numeric(t[tcol], errors="coerce")
                    armed = temps.ge(float(cutoff)) & temps.notna()
                    if limit is not None:
                        armed &= temps.lt(float(limit))
            if actual_col and actual_col in t.columns:
                real = pd.to_numeric(t[actual_col], errors="coerce")
            else:
                real = pd.Series(float("nan"), index=x)

            row, texts = [], []
            for stamp, plan_on, is_armed, actual_v in zip(
                    x, planned, armed, real):
                soll = "AN" if plan_on else ("freigegeben" if is_armed
                                             else "aus")
                # Zukunft: es KANN kein Ist geben -> nur den Sollzustand zeigen.
                if stamp > now or pd.isna(actual_v):
                    code = (RUN if plan_on
                            else ARMED if is_armed else OFF)
                    ist = "–" if stamp > now else "unbekannt"
                    if stamp <= now and actual_col:
                        code = UNKNOWN
                else:
                    running = float(actual_v) > 0.5
                    ist = "läuft" if running else "aus"
                    if plan_on and running:
                        code = RUN
                    elif plan_on and not running:
                        code = MISSING
                    elif running:
                        code = UNPLANNED
                    else:
                        code = ARMED if is_armed else OFF
                row.append(code)
                texts.append(f"Soll {soll} · Ist {ist} · {_lab[code]}")
            z.append(row)
            hover.append(texts)

        n_states = 7
        palette_light = ["#e9ecef", "#2ca02c", "#adb5bd", "#d8a52a",
                         "#8fa8c8", "#c2185b", "#e8710a"]
        step = 1.0 / n_states
        colorscale = []
        for i, colour in enumerate(palette_light):
            colorscale.append([round(i * step, 4), colour])
            colorscale.append([round((i + 1) * step - 1e-4, 4), colour])
        fig.add_trace(go.Heatmap(
            x=x, y=ylabels, z=z, zmin=-0.5, zmax=n_states - 0.5,
            showscale=False, meta="load_timeline", colorscale=colorscale,
            customdata=hover,
            hovertemplate="%{y}: %{x|%H:%M} – %{customdata}<extra></extra>"),
            row=6, col=1)
        # Jede Spur MUSS ihr Label bekommen: ohne tickmode="array" duennt Plotly
        # bei vielen Zeilen automatisch aus, und es sieht aus, als fehlten Lasten.
        fig.update_yaxes(tickmode="array", tickvals=ylabels, ticktext=ylabels,
                         tickfont=dict(size=9), row=6, col=1)

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
            act = load_temp_actual.get(ld.name)
            actual_aligned = (
                pd.to_numeric(pd.Series(act).reindex(x), errors="coerce")
                if act is not None and len(act) > 0 else
                pd.Series(float("nan"), index=x))
            planned = (pd.to_numeric(t[col], errors="coerce")
                       if col in t.columns else
                       pd.Series(float("nan"), index=x))
            if col in t.columns and t[col].notna().any():
                fig.add_trace(go.Scatter(
                    x=x, y=planned, name=f"{ld.name} (Soll)", mode="lines",
                    line=dict(color=c, width=2, dash="dash"),
                    hovertemplate=(
                        f"{ld.name} Soll: %{{y:.1f}} °C"
                        "<extra></extra>"),
                    legendgroup="temp",
                    legendgrouptitle_text="Temperatur"), row=temp_row, col=1)
            if act is not None and len(act) > 0:
                fig.add_trace(go.Scatter(
                    x=x, y=actual_aligned, name=f"{ld.name} (Ist)", mode="lines",
                    line=dict(color=c, width=2),
                    customdata=(actual_aligned - planned).to_list(),
                    hovertemplate=(
                        f"{ld.name} Ist: %{{y:.1f}} °C"
                        "<br>Abweichung Δ: "
                        "%{customdata:+.1f} °C<extra></extra>"),
                    legendgroup="temp",
                    legendgrouptitle_text="Temperatur"), row=temp_row, col=1)
        # Außentemperatur (Open-Meteo, Ist+Prognose) als Referenz - erklärt
        # Wärmeverlust/-eintrag der Pooltemperatur mit.
        if ambient_temp_c is not None and len(ambient_temp_c) > 0:
            amb = ambient_temp_c.dropna()
            if len(amb) > 0:
                fig.add_trace(go.Scatter(
                    x=amb.index, y=amb.values, name="Außentemperatur", mode="lines",
                    line=dict(color="#7f7f7f", width=1.5, dash="dot"),
                    hovertemplate="Außentemperatur: %{y:.1f} °C<extra></extra>",
                    legendgroup="temp",
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
        height=((1120 + 26 * max(0, lane_count - 3)) if has_loads else 980)
        + (180 if temp_row else 0),
        autosize=True, template="plotly_white",
        # Kein eigener Hintergrund: sonst rendert Plotly die Grafik WEISS und
        # erst das nachgelagerte paint() faerbt sie dunkel - beim Neuladen ein
        # sichtbares Weiss-Schwarz-Blinken. Transparent laesst die Karte
        # darunter durchscheinen, und die ist per CSS sofort in der richtigen
        # Farbe (das Theme-Skript im <head> laeuft vor dem ersten Paint).
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        hovermode="x unified",
        # Ohne harte Distanzgrenze nimmt Plotly im Unified-Hover für eine
        # Zukunftsprognose noch den letzten Istpunkt links vom Jetzt-Marker
        # hinzu. Dann trägt der Kasten z.B. 11:45, enthält aber Istwerte von
        # 11:15. Ein Pixel bleibt gut bedienbar, ist aber kleiner als der
        # Abstand benachbarter 15-min-Punkte im sichtbaren Zeitbereich.
        hoverdistance=1,
        barmode="relative", bargap=0,
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
    load_profile_html = _load_profile_block(config)
    runtime_html = _runtime_block(bool(getattr(config.dashboard, "controls_enabled", False)))
    live_html = _live_block(config)
    decision_html = _decision_block(t, now)
    mobile_plot_html = _mobile_plot_block(now, has_loads, temp_row)
    from .config_editor import editor_allowed
    config_link = (
        '<a id="config-link" href="/config" title="EMS konfigurieren">'
        '<span class="config-icon">⚙</span><span class="config-label">'
        'Konfiguration</span></a>'
        if editor_allowed(config) else "")
    # Eigene Seite: archivierten Lauf wählen und gegen die Ist-Werte legen.
    archive_link = (
        '<a id="archive-link" href="/archiv" '
        'title="Archivierten Optimierer-Lauf gegen die Ist-Werte legen">'
        '<span class="config-icon">🕘</span><span class="config-label">'
        'Lauf-Archiv</span></a>')
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
 .desktop-plot {{ background: #fff; border-radius: 12px; }}
 html.dark .desktop-plot {{ background: #18212b; }}
 .header-actions {{ display: flex; gap: 7px; }}
 .lp-bars {{ display: flex; align-items: flex-end; gap: 1px; height: 34px;
        margin-top: 6px; }}
 .lp-bars span {{ flex: 1; background: #3a86c8; border-radius: 1px 1px 0 0;
        min-width: 2px; }}
 .tile.warn {{ border-color: #e1b74a; }}
 .header-actions button, .header-actions a {{ min-width: 42px; min-height: 38px; padding: 7px 10px;
        border: 1px solid #ccd4dc; border-radius: 8px; background: #f4f6f8;
        color: #26313c; cursor: pointer; font: inherit; font-size: 13px;
        text-decoration: none; display: inline-flex; align-items: center;
        justify-content: center; gap: 5px; }}
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
 /* Rahmen/Kopf wie .info-panel – die Statusfarbe bleibt als Punkt im Kopf. */
 /* Betriebsdiagnose/Pool-Rückkopplung/Prognosequalität nutzen jetzt den
    .info-panel-Look; die Statusfarbe steckt im .an-dot der Kopfzeile
    (wie beim Analyse-Panel). */
 .quality-grid {{ display: grid; grid-template-columns: repeat(auto-fit,minmax(175px,1fr));
        gap: 7px; padding: 11px 13px 13px; }}
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
 .desktop-horizon-toolbar {{ display: flex; align-items: center; justify-content: flex-end;
        gap: 9px; margin: 6px 3px 4px; color: #687582; font-size: 12px; }}
 .horizon-switch {{ display: inline-grid; grid-template-columns: repeat(3, minmax(54px,auto)); gap: 5px; }}
 .horizon-switch button {{ min-height: 34px; padding: 5px 11px; border: 1px solid #cbd3db;
        border-radius: 8px; background: #f4f6f8; color: #34404c; cursor: pointer;
        font: inherit; font-size: 12px; }}
 .horizon-switch button.on {{ color: #fff; background: #1769c2; border-color: #1769c2; }}
 html.dark .desktop-horizon-toolbar {{ color: #aebbc8; }}
 html.dark .horizon-switch button {{ color: #e7edf4; background: #263442; border-color: #4b5b6b; }}
 html.dark .horizon-switch button.on {{ background: #287fd8; border-color: #287fd8; }}
 .banner {{ border-radius: 8px; padding: 8px 14px; margin-bottom: 10px;
           font-size: 13px; border: 1px solid; }}
 .banner ul {{ margin: 6px 0 0; padding-left: 20px; }}
 .banner li {{ margin: 2px 0; }}
 .banner.ok {{ background: #eafaf0; border-color: #b6e2c6; color: #1e7e46; }}
 .banner.warn {{ background: #fff8e1; border-color: #f0d98a; color: #8a6d00; }}
 .banner.err {{ background: #fdecea; border-color: #f5b5ae; color: #b3261e; }}
 .decisions {{ margin: 10px 0; background: #fff; border: 1px solid #dde4eb;
        border-radius: 10px; overflow: hidden; }}
 .decisions > summary {{ padding: 11px 13px; cursor: pointer;
        font-weight: 700; user-select: none; background: #f7f9fb; }}
 .decisions[open] > summary {{ border-bottom: 1px solid #e2e7ec; }}
 .decision-head b {{ font-weight: 700; }}
 .decision-head small {{ margin-left: 8px; color: #737c86; font-weight: 400; }}
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
 .controls {{ margin: 10px 0; background: #fff; border: 1px solid #dde4eb;
        border-radius: 10px; overflow: hidden; }}
 .controls > summary {{ padding: 11px 13px; cursor: pointer; user-select: none;
        font-weight: 700; background: #f7f9fb; }}
 .controls[open] > summary {{ border-bottom: 1px solid #e4e7eb; }}
 .controls > summary small {{ margin-left: 8px; color: #75808a; font-size: 12px;
        font-weight: 400; }}
 .controls .ctl-body {{ padding: 15px; font-size: 13px; }}
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
 .analysis-panel h4 {{ margin: 13px 12px 6px; font-size: 13px; font-weight: 700; color: #55606a; }}
 .analysis-panel h4 small {{ margin-left: 6px; font-weight: 400; color: #8a949d; }}
 .analysis-panel .tiles {{ padding: 0 12px; margin-bottom: 4px; }}
 .analysis-panel .tiles:last-of-type {{ padding-bottom: 14px; }}
 .analysis-panel .an-hint {{ display: block; padding: 2px 12px 10px; color: #8a949d; font-size: 12px; }}
 html.dark .analysis-panel h4 {{ color: #d3dbe3; }}
 html.dark .analysis-panel h4 small, html.dark .analysis-panel .an-hint {{ color: #97a3ad; }}
 .an-dot {{ display: inline-block; width: 9px; height: 9px; border-radius: 50%; margin-right: 7px; vertical-align: middle; background: #28a261; }}
 .an-dot.warn {{ background: #e29a2d; }} .an-dot.bad {{ background: #d1495b; }}
 .an-dot.neutral {{ background: #8b98a5; }}
 .sparkline {{ display: flex; align-items: flex-end; gap: 3px; height: 46px; padding: 0 12px 12px; }}
 .sparkline .bar {{ flex: 1; min-height: 4px; background: #28a261; border-radius: 2px 2px 0 0; }}
 .sparkline .bar.neg {{ background: #d1495b; }}
 .facc-trend {{ padding: 2px 12px 10px; }}
 .facc-svg {{ width: 100%; height: 42px; display: block; }}
 .facc-legend {{ font-size: 11px; color: #8a949d; margin-top: 3px; }}
 .forecast-analysis-panel h4 {{ margin: 13px 12px 7px; font-size: 13px; color: #55606a; }}
 .forecast-analysis-panel h4 small {{ margin-left: 6px; color: #8a949d; font-weight: 400; }}
 .forecast-analysis-panel > .quality-grid {{ padding: 0 12px 3px; }}
 .forecast-accuracy-tiles {{ padding: 0 12px; }}
 .forecast-analysis-toolbar {{ display: flex; align-items: center; flex-wrap: wrap; gap: 7px;
      padding: 11px 12px 2px; }}
 .forecast-analysis-toolbar label {{ display: flex; align-items: center; gap: 6px; font-size: 12px; }}
 .forecast-analysis-toolbar input, .forecast-analysis-toolbar button,
 .forecast-signal-switch button {{ min-height: 34px; border: 1px solid #cbd3db; border-radius: 7px;
      background: #f7f9fb; color: #34404c; padding: 5px 9px; font: inherit; }}
 .forecast-analysis-toolbar #fa-status {{ margin-left: auto; color: #7b8792; font-size: 11px; }}
 .forecast-analysis-toolbar button:disabled, .forecast-analysis-toolbar input:disabled {{
      cursor: wait; opacity: .62; }}
 .forecast-day-stage {{ position: relative; min-height: 332px; margin: 0 12px 10px; }}
 .forecast-day-stage .day-comparison-chart {{ margin: 0; }}
 .forecast-day-loading {{ position: absolute; inset: 0; z-index: 5; display: none;
      align-items: center; justify-content: center; gap: 10px; border: 1px solid #d7e0e8;
      border-radius: 9px; background: #f6f9fb; color: #596775; font-size: 12px; }}
 .forecast-day-loading i {{ width: 20px; height: 20px; border: 3px solid #cbd7e2;
      border-top-color: #287fd8; border-radius: 50%; animation: forecastSpin .75s linear infinite; }}
 .forecast-analysis-panel.loading .forecast-day-loading {{ display: flex; }}
 .forecast-analysis-panel.loading #fa-day-comparison {{ visibility: hidden; }}
 @keyframes forecastSpin {{ to {{ transform: rotate(360deg); }} }}
 .calibration-grid {{ display: grid; grid-template-columns: repeat(4,minmax(0,1fr)); gap: 8px;
      padding: 0 12px 8px; }}
 .calibration-card {{ min-width: 0; padding: 10px; border: 1px solid #dce3e9;
      border-left: 4px solid #dfa82d; border-radius: 9px; background: #fafbfc; }}
 .calibration-card.current {{ border-left-color: #35a466; }}
 .calibration-card > header {{ display: flex; justify-content: space-between; gap: 7px; align-items: flex-start; }}
 .calibration-card header b, .calibration-card header small {{ display: block; }}
 .calibration-card header b {{ font-size: 12px; }}
 .calibration-card header small {{ margin-top: 2px; color: #77838e; font-size: 10px; }}
 .calibration-card header strong {{ color: #4d5964; font-size: 15px; white-space: nowrap; }}
 .calibration-bar {{ height: 5px; margin: 8px 0; border-radius: 5px; overflow: hidden; background: #e3e8ed; }}
 .calibration-bar i {{ display: block; height: 100%; background: #dda72e; border-radius: inherit; }}
 .calibration-card.current .calibration-bar i {{ background: #35a466; }}
 .calibration-card p {{ min-height: 31px; margin: 0 0 7px; color: #68747f; font-size: 10px; line-height: 1.45; }}
 .calibration-card dl {{ margin: 0; }}
 .calibration-card dl > div {{ display: flex; justify-content: space-between; gap: 5px;
      border-top: 1px solid #e7ebef; padding: 5px 0; font-size: 10px; }}
 .calibration-card dt {{ color: #7a8690; }}
 .calibration-card dd {{ margin: 0; font-weight: 700; text-align: right; overflow-wrap: anywhere; }}
 .calibration-card footer {{ margin-top: 6px; color: #68747f; font-size: 9px; line-height: 1.35; }}
 .calibration-note {{ grid-column: 1/-1; color: #87929c; font-size: 10px; }}
 .forecast-heat-grid {{ display: grid; grid-template-columns: repeat(2,minmax(0,1fr)); gap: 10px;
      padding: 0 12px 8px; }}
 .forecast-heat-grid > div {{ min-width: 0; }}
 .forecast-heat-grid > div > b {{ display: none; }}
 .forecast-analysis-chart {{ min-height: 250px; border: 1px solid #e0e6ec; border-radius: 9px;
      overflow: hidden; }}
 .calibration-history-chart {{ margin: 0 12px 10px; }}
 .calibration-change-list {{ margin: -2px 12px 12px; display: grid; gap: 5px; }}
 .calibration-change-list article {{ display: grid; grid-template-columns: minmax(125px,auto) 1fr;
      gap: 9px; padding: 7px 9px; border: 1px solid #e2e7ec; border-radius: 7px;
      background: #f8fafc; font-size: 11px; }}
 .calibration-change-list time {{ color: #73808c; white-space: nowrap; }}
 .forecast-vintage-head {{ display: flex; align-items: center; justify-content: space-between;
      padding-right: 12px; }}
 .forecast-signal-switch {{ display: flex; gap: 5px; }}
 .forecast-signal-switch button.on {{ color: #fff; background: #1769c2; border-color: #1769c2; }}
 .vintage-chart {{ margin: 0 12px 13px; min-height: 350px; }}
 html.dark .forecast-analysis-panel h4 {{ color: #d3dbe3; }}
 html.dark .forecast-analysis-panel h4 small, html.dark .forecast-analysis-toolbar #fa-status {{ color: #97a3ad; }}
 html.dark .forecast-analysis-toolbar input, html.dark .forecast-analysis-toolbar button,
 html.dark .forecast-signal-switch button {{ color: #e7edf4; background: #263442; border-color: #4b5b6b; }}
 html.dark .forecast-signal-switch button.on {{ background: #287fd8; border-color: #287fd8; }}
 html.dark .forecast-analysis-chart {{ border-color: #354352; }}
 html.dark .forecast-day-loading {{ color: #c1ccd6; background: #202b36;
      border-color: #43515f; }}
 html.dark .forecast-day-loading i {{ border-color: #465869; border-top-color: #58a6e7; }}
 html.dark .calibration-change-list article {{ background: #202b36; border-color: #354352; }}
 html.dark .calibration-change-list time {{ color: #aebbc8; }}
 html.dark .calibration-card {{ background: #202b36; border-color: #43515f; border-left-color: #d9b83f; }}
 html.dark .calibration-card.current {{ border-left-color: #58b879; }}
 html.dark .calibration-card header small, html.dark .calibration-card p,
 html.dark .calibration-card footer, html.dark .calibration-card dt,
 html.dark .calibration-note {{ color: #aebbc8; }}
 html.dark .calibration-card header strong {{ color: #e7edf4; }}
 html.dark .calibration-bar {{ background: #3b4956; }}
 html.dark .calibration-card dl > div {{ border-color: #3b4956; }}
 .pvconf-grid {{ display: grid; grid-template-columns: repeat(auto-fit,minmax(180px,1fr)); gap: 10px; padding: 12px; }}
 .pvconf-card {{ border: 1px solid #e0e5eb; border-radius: 9px; background: #f7f9fb; padding: 10px 11px; }}
 .pvconf-head {{ display: flex; align-items: center; justify-content: space-between; margin-bottom: 7px; }}
 .pvconf-day {{ font-weight: 700; font-size: 13px; }}
 .mode-badge {{ font-size: 11px; font-weight: 700; padding: 2px 9px; border-radius: 999px; color: #fff; text-transform: uppercase; letter-spacing: .3px; }}
 .mode-badge.asap {{ background: #28a261; }} .mode-badge.peak {{ background: #e29a2d; }}
 .mode-badge.late {{ background: #9b6bd3; }} .mode-badge.auto {{ background: #6b7883; }}
 .pvconf-metrics {{ display: flex; flex-wrap: wrap; gap: 4px 14px; font-size: 12px; color: #66707a; }}
 .pvconf-metrics span {{ display: inline-flex; gap: 5px; }}
 .pvconf-metrics b {{ color: #20252b; font-weight: 700; }}
 .pvconf-basis {{ margin-top: 7px; font-size: 11px; color: #75808a; }}
 .pvconf-note {{ margin: 0 12px 12px; font-size: 12px; color: #8a949d; }}
 html.dark .pvconf-card {{ background: #202b36; border-color: #354352; }}
 html.dark .pvconf-metrics {{ color: #97a3ad; }} html.dark .pvconf-metrics b {{ color: #e7edf4; }}
 html.dark .pvconf-basis, html.dark .pvconf-note {{ color: #97a3ad; }}
 .whatif-form {{ display: flex; flex-wrap: wrap; align-items: flex-end; gap: 12px; padding: 12px; }}
 .wi-field {{ display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: #66707a; }}
 .wi-field select, .wi-field input {{ padding: 6px 9px; border: 1px solid #cfd7df; border-radius: 7px; background: #fff; color: #20252b; font-size: 14px; }}
 .whatif-form button.primary {{ padding: 8px 16px; }}
 .whatif-result {{ padding: 0 12px 12px; }}
 .whatif-head {{ display: flex; align-items: center; gap: 10px; margin-bottom: 9px; }}
 .whatif-head small {{ color: #75808a; }} .wi-bad {{ color: #d1495b; }}
 html.dark .wi-field {{ color: #97a3ad; }}
 html.dark .wi-field select, html.dark .wi-field input {{ background: #202b36; border-color: #354352; color: #e7edf4; }}
 html.dark .whatif-head small {{ color: #97a3ad; }}
 .detail-grid {{ display: grid; grid-template-columns: repeat(auto-fit,minmax(145px,1fr)); gap: 7px; padding: 12px; }}
 .detail-grid h3, .detail-grid p {{ grid-column: 1/-1; margin: 0 0 4px; }}
 .detail-grid > div {{ padding: 8px 9px; border-radius: 7px; background: #f4f7fa; }}
 .detail-grid span, .detail-grid b {{ display: block; }}
 .detail-grid span {{ color: #74808b; font-size: 10px; }}
 .events-list {{ max-height: 360px; overflow: auto; padding: 6px 12px 11px; }}
 .events-filter {{ display: flex; gap: 6px; padding: 9px 12px 2px; }}
 .events-filter button {{ flex: 0 0 auto; font-size: 12px; padding: 4px 11px;
        border-radius: 7px; border: 1px solid #cfd6dd; background: #f2f5f8;
        color: #7a8590; cursor: pointer; }}
 .events-filter button.info.on {{ background: #eef6f0; border-color: #4a9d6a; color: #237a3b; }}
 .events-filter button.warn.on {{ background: #fdf4e2; border-color: #d9a441; color: #8a6d00; }}
 .events-filter button.err.on {{ background: #fdecec; border-color: #d1746e; color: #bd302a; }}
 .event {{ display: grid; grid-template-columns: 118px 1fr; gap: 10px; align-items: center;
        padding: 8px 3px; border-bottom: 1px solid #edf0f3; }}
 .event time {{ color: #55606b; font-size: 12.5px; white-space: nowrap; }}
 .event > span {{ display: flex; align-items: center; gap: 7px; }}
 .ev-ic {{ font-style: normal; flex: 0 0 auto; font-size: 13px; line-height: 1; }}
 .event.error span {{ color: #bd302a; font-weight: 600; }}
 .event.warn span, .event.warning span {{ color: #9a6b00; }}
 .event.k-switch span {{ color: #2f6f9e; }}
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
 html.dark .detail-grid span {{ color: #aebbc8; }}
 html.dark .event time {{ color: #c3cdd8; }}
 html.dark .compare-chart-status {{ color: #aebbc8; }}
 html.dark .event {{ border-color: #303e4b; }}
 html.dark .event.error span {{ color: #f1a29c; }}
 html.dark .event.warn span, html.dark .event.warning span {{ color: #e1c96b; }}
 html.dark .event.k-switch span {{ color: #8fc0e8; }}
 html.dark .events-filter button {{ background: #263442; border-color: #4b5b6b; color: #aebbc8; }}
 html.dark .events-filter button.info.on {{ background: #173326; border-color: #58b879; color: #8fd7a9; }}
 html.dark .events-filter button.warn.on {{ background: #3a3219; border-color: #d9b83f; color: #e1c96b; }}
 html.dark .events-filter button.err.on {{ background: #402124; border-color: #df6c68; color: #f1a29c; }}
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
 html.dark .decisions > summary {{ background: #202b36; color: #e7edf4; }}
 html.dark .decision-item {{ background: #202b36; border-color: #354352; }}
 html.dark .decision-time, html.dark .decision-head small {{ color: #aebbc8; }}
 html.dark .decision-reason, html.dark .decision-empty {{ color: #d1dae4; }}
 html.dark .decision-facts span {{ background: #2a3947; color: #d9e3ed; }}
 html.dark .controls > summary, html.dark .load-card, html.dark .ctl-section,
 html.dark .curve-box, html.dark .planner-badge {{ background: #202b36; border-color: #354352; }}
 html.dark .battery-planner {{ background: linear-gradient(150deg,#18232d,#202b36); border-color: #354352; }}
 html.dark .controls input, html.dark .controls textarea, html.dark .controls select {{ background: #111820; color: #eef3f8; border-color: #4b5b6b; }}
 html.dark .controls button, html.dark .header-actions button,
 html.dark .header-actions a {{ background: #263442; color: #eef3f8; border-color: #4b5b6b; }}
 html.dark .controls button.primary, html.dark .controls button.mode.on {{ background: #287fd8; color: #fff; border-color: #4d9bea; }}
 html.dark .controls button.instant {{ background: #1c3c5c; color: #8fc8ff; border-color: #3c6f9d; }}
 html.dark .controls button.stop {{ background: #432529; color: #ffaaa4; border-color: #75454a; }}
 html.dark .report button {{ background: #263442; color: #e7edf4; border-color: #4b5b6b; }}
 html.dark .report button:hover {{ background: #304253; }}
 html.dark .report button.hot {{ background: #432529; color: #ffaaa4; border-color: #75454a; }}
 html.dark .tile .l, html.dark .chips, html.dark .report .msg {{ color: #c2ccd6; }}
 html.dark .tile .s, html.dark .controls > summary small,
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
 html.dark .quality-head small, html.dark .quality-detail {{ color: #aebbc8; }}
 html.dark .quality-item {{ background: #202b36; border-color: #43515f; }}
 html.dark .quality-item.current {{ background: #173326; border-left-color: #58b879; }}
 html.dark .quality-item.partial {{ background: #3a3219; border-left-color: #d9b83f; }}
 html.dark .quality-item.replaced {{ background: #402124; border-left-color: #df6c68; }}
 html.dark .quality-item.current .quality-state {{ color: #8fd7a9; }}
 html.dark .quality-item.partial .quality-state {{ color: #e1c96b; }}
 html.dark .quality-item.replaced .quality-state {{ color: #f1a29c; }}
 html.dark .banner.ok {{ background: #173326; border-color: #285b40; color: #8fd7a9; }}
 html.dark .banner.warn {{ background: #3a3219; border-color: #6a5925; color: #e1c96b; }}
 html.dark .banner.err {{ background: #402124; border-color: #73383d; color: #f1a29c; }}
 @media (max-width: 620px) {{
   body {{ padding: 8px; }}
   .app-header {{ padding: 10px 11px; margin-bottom: 8px; position: relative; }}
   h1 {{ font-size: 17px; line-height: 1.25; }}
   h1 .ts {{ display: block; font-size: 11px; margin-top: 2px; }}
   .header-actions button, .header-actions a {{ min-width: 44px; min-height: 44px; font-size: 0; padding: 0; }}
   #config-link .config-icon, #archive-link .config-icon {{ font-size: 21px; }}
   #config-link .config-label, #archive-link .config-label {{ display: none; }}
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
   .desktop-horizon-toolbar {{ display: none; }}
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
   .horizon-switch {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 5px; }}
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
   .forecast-heat-grid {{ grid-template-columns: 1fr; padding: 0 6px 8px; }}
   .calibration-grid {{ grid-template-columns: 1fr; padding: 0 7px 8px; }}
   .calibration-card p {{ min-height: 0; }}
   .forecast-analysis-toolbar {{ align-items: stretch; }}
   .forecast-analysis-toolbar label {{ width: 100%; }}
   .forecast-analysis-toolbar input {{ flex: 1; min-height: 42px; }}
   .forecast-analysis-toolbar button {{ min-height: 42px; }}
   .forecast-analysis-toolbar #fa-status {{ width: 100%; margin-left: 0; }}
   .forecast-vintage-head {{ align-items: flex-start; padding-right: 7px; }}
   .forecast-vintage-head h4 {{ margin-right: 4px; }}
   .forecast-signal-switch button {{ min-height: 40px; }}
   .day-comparison-chart, .calibration-history-chart {{ margin: 0 6px 10px; }}
   .calibration-change-list article {{ grid-template-columns: 1fr; gap: 3px; }}
   .vintage-chart {{ margin: 0 6px 10px; }}
   .event {{ grid-template-columns: 90px 1fr; }}
 }}
 .chips {{ font-size: 12px; color: #555; margin: -2px 0 10px; }}
 .chips .chip {{ margin-right: 14px; white-space: nowrap; }}
 .chips .dot {{ display: inline-block; width: 8px; height: 8px;
        border-radius: 50%; margin-right: 4px; }}
</style></head><body>
<header class="app-header"><h1>E3DC EMS Steuerung
 <span class="ts">{now.strftime('%Y-%m-%d %H:%M')}</span></h1>
 <div class="header-actions">{archive_link}{config_link}<button type="button" id="install-app" title="Als App installieren">Installieren</button>
 <button type="button" id="theme-toggle" title="Darstellung wechseln">Darstellung</button></div></header>
{runtime_html}
{live_html}
<div class="tiles">{''.join(tiles)}</div>
{_sources_block(source_status)}
{_control_banner(control_status)}
{_alert_banner(violations)}
<div class="desktop-plot">{plot_html}</div>
{mobile_plot_html}
{_slot_detail_block()}
{decision_html}
{_operations_block(solver_status, execution_status, config.general.timezone,
                   monitoring_status, plan_status)}
{load_profile_html}
{_thermal_feedback_block(load_feedback_status, thermal_calibration)}
{_forecast_analysis_block(forecast_quality, config.general.timezone, load_bias)}
{_analysis_block(analysis_headline)}
{_pv_confidence_block(auto_peak_basis)}
{controls_html}
{_whatif_block(config)}
{_events_block()}
{report_html}
<script>(function(){{
 var theme=document.getElementById('theme-toggle'),install=document.getElementById('install-app'),prompt=null;
 function label(){{var dark=document.documentElement.classList.contains('dark');theme.title=dark?'Helle Darstellung':'Dunkle Darstellung';theme.setAttribute('aria-label',theme.title);}}
 function paint(){{var dark=document.documentElement.classList.contains('dark');var c=dark?{{paper_bgcolor:'rgba(0,0,0,0)',plot_bgcolor:'rgba(0,0,0,0)','font.color':'#e7edf4','hoverlabel.bgcolor':'#202b36','hoverlabel.bordercolor':'#536273','hoverlabel.font.color':'#e7edf4'}}:{{paper_bgcolor:'rgba(0,0,0,0)',plot_bgcolor:'rgba(0,0,0,0)','font.color':'#20252b','hoverlabel.bgcolor':'#ffffff','hoverlabel.bordercolor':'#cfd7df','hoverlabel.font.color':'#20252b'}};var lines={{'Haus-SoC (Ist)':['#111111','#f7fafc'],'Haus-SoC (Prog.)':['#111111','#d5e0ea'],'Akku-Leistung (Ist)':['#111111','#58d68d'],'Außentemperatur':['#7f7f7f','#a9d5ff']}};document.querySelectorAll('.desktop-plot .plotly-graph-div').forEach(function(p){{Plotly.relayout(p,c);(p.layout.annotations||[]).forEach(function(a,i){{if(String(a.text||'').includes('Modus:')){{var u={{}};u['annotations['+i+'].font.color']=dark?'#e7edf4':'#555';Plotly.relayout(p,u);}}}});p.data.forEach(function(t,i){{if(lines[t.name])Plotly.restyle(p,{{'line.color':lines[t.name][dark?1:0]}},[i]);if(t.meta==='mode_timeline'){{if(!t._emsLightColorscale)t._emsLightColorscale=t.colorscale;Plotly.restyle(p,{{colorscale:[dark?[[0,'#344250'],[.125,'#344250'],[.126,'#3f8f55'],[.25,'#3f8f55'],[.251,'#a98e2e'],[.375,'#a98e2e'],[.376,'#914e82'],[.5,'#914e82'],[.501,'#b96d23'],[.625,'#b96d23'],[.626,'#9f3434'],[.75,'#9f3434'],[.751,'#3475ad'],[.875,'#3475ad'],[.876,'#71318f'],[1,'#71318f']]:t._emsLightColorscale]}},[i]);}}if(t.meta==='load_timeline')Plotly.restyle(p,{{colorscale:[dark?[[0.0,'#263442'],[0.1428,'#263442'],[0.1429,'#329b4c'],[0.2856,'#329b4c'],[0.2857,'#596979'],[0.4285,'#596979'],[0.4286,'#987620'],[0.5713,'#987620'],[0.5714,'#3f5a7a'],[0.7142,'#3f5a7a'],[0.7143,'#a3134a'],[0.857,'#a3134a'],[0.8571,'#b85a08'],[0.9999,'#b85a08']]:[[0.0,'#e9ecef'],[0.1428,'#e9ecef'],[0.1429,'#2ca02c'],[0.2856,'#2ca02c'],[0.2857,'#adb5bd'],[0.4285,'#adb5bd'],[0.4286,'#d8a52a'],[0.5713,'#d8a52a'],[0.5714,'#8fa8c8'],[0.7142,'#8fa8c8'],[0.7143,'#c2185b'],[0.857,'#c2185b'],[0.8571,'#e8710a'],[0.9999,'#e8710a']]]}},[i]);}});}});}}
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
