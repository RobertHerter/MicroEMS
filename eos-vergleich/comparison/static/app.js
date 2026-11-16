"use strict";

const COLORS = {
  microems: "#f2f6f7",
  eos_import: "#62dbeb",
  eos_native: "#ffc64a",
};

const METRICS = {
  pvKw: { label: "PV-Prognose", unit: "kW", decimals: 1, floor: 0 },
  loadKw: { label: "Lastprognose", unit: "kW", decimals: 1, floor: 0 },
  priceCt: { label: "Strompreis", unit: "ct/kWh", decimals: 1 },
  socPct: { label: "Batterie-SOC", unit: "%", decimals: 0, floor: 0, ceiling: 100 },
  gridKw: { label: "Netzleistung", unit: "kW", decimals: 1, symmetric: true },
};

const state = { data: null, metric: "pvKw" };
const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const svgNS = "http://www.w3.org/2000/svg";
const SLOT_MS = 15 * 60 * 1000;

function element(name, attributes = {}, text = null) {
  const node = document.createElement(name);
  Object.entries(attributes).forEach(([key, value]) => node.setAttribute(key, value));
  if (text !== null) node.textContent = text;
  return node;
}

function svgElement(name, attributes = {}, text = null) {
  const node = document.createElementNS(svgNS, name);
  Object.entries(attributes).forEach(([key, value]) => node.setAttribute(key, value));
  if (text !== null) node.textContent = text;
  return node;
}

function systemColor(id) { return COLORS[id] || "#e9f1f5"; }

function formatDate(raw, withDate = true) {
  return new Intl.DateTimeFormat("de-DE", {
    weekday: withDate ? "short" : undefined,
    day: withDate ? "2-digit" : undefined,
    month: withDate ? "2-digit" : undefined,
    hour: "2-digit",
    minute: "2-digit",
    timeZone: "Europe/Berlin",
  }).format(new Date(raw));
}

function formatNumber(value, decimals = 1) {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return "–";
  return Number(value).toLocaleString("de-DE", { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
}

function hasNumber(value) {
  return value !== null && value !== undefined && Number.isFinite(Number(value));
}

function formatDay(raw) {
  return new Intl.DateTimeFormat("de-DE", {
    weekday: "short", day: "2-digit", month: "2-digit", timeZone: "Europe/Berlin",
  }).format(new Date(`${raw}T12:00:00Z`));
}

function renderSystemRail() {
  const rail = $("#system-rail");
  rail.replaceChildren();
  state.data.systems.forEach((system) => {
    const card = element("article", { class: "system-card", style: `--system-color:${systemColor(system.id)}` });
    const top = element("div", { class: "system-top" });
    top.append(element("h3", {}, system.name));
    top.append(element("span", { class: `status-pill ${system.cached ? "cached" : ""}` }, system.cached ? "Snapshot" : "bereit"));
    card.append(top);
    card.append(element("p", {}, system.source));
    const meta = element("div", { class: "system-meta" });
    meta.append(element("span", {}, system.optimizer));
    meta.append(element("span", {}, `Stand ${formatDate(system.generatedAt)}`));
    card.append(meta);
    rail.append(card);
  });
}

function renderScoreboard() {
  const body = $("#score-table tbody");
  body.replaceChildren();
  state.data.systems.forEach((system) => {
    const total = system.totals;
    const row = element("tr", { style: `--system-color:${systemColor(system.id)}` });
    const name = element("td");
    name.append(document.createTextNode(system.shortName));
    name.append(element("span", { class: "sub" }, `${system.sourceRasterMin} Min Quellraster`));
    row.append(name);
    row.append(element("td", { class: "net" }, `${formatNumber(total.netEur, 2)} €`));
    row.append(element("td", {}, `${formatNumber(total.importKwh, 1)} kWh`));
    row.append(element("td", {}, `${formatNumber(total.exportKwh, 1)} kWh`));
    row.append(element("td", {}, `${formatNumber(total.pvKwh, 1)} kWh`));
    row.append(element("td", {}, `${formatNumber(total.loadKwh, 1)} kWh`));
    body.append(row);
  });
}

function valueExtent(systems, metric) {
  const values = systems.flatMap((system) => system.points.map((point) => Number(point[metric])).filter(Number.isFinite));
  if (!values.length) return [0, 1];
  const config = METRICS[metric];
  let min = Math.min(...values);
  let max = Math.max(...values);
  if (config.symmetric) {
    const extent = Math.max(Math.abs(min), Math.abs(max), 1);
    return [-extent * 1.08, extent * 1.08];
  }
  if (config.floor !== undefined) min = config.floor;
  if (config.ceiling !== undefined) max = config.ceiling;
  if (max === min) max = min + 1;
  const pad = (max - min) * .08;
  return [config.floor !== undefined ? min : min - pad, config.ceiling !== undefined ? max : max + pad];
}

function timelineDomain(systems) {
  const times = systems.flatMap((system) => system.points.map((point) => Date.parse(point.t))).filter(Number.isFinite);
  if (!times.length) return [0, SLOT_MS];
  return [Math.min(...times), Math.max(...times) + SLOT_MS];
}

function chartMargins(host, width) {
  const margin = { left: 62, right: 22, top: 24, bottom: 42 };
  if (host.id !== "grid-chart" || host.clientWidth <= 0) return margin;
  const rootStyle = getComputedStyle(document.documentElement);
  const leftPx = parseFloat(rootStyle.getPropertyValue("--intent-label-width")) || 160;
  const rightPx = parseFloat(rootStyle.getPropertyValue("--intent-right-pad")) || 22;
  return {
    ...margin,
    left: leftPx / host.clientWidth * width,
    right: rightPx / host.clientWidth * width,
  };
}

function drawChart(host, metric, readout) {
  host.replaceChildren();
  const systems = state.data.systems.filter((system) => system.points.length);
  if (!systems.length) return;
  const width = 1000;
  const height = host.classList.contains("compact-host") ? 320 : 400;
  const margin = chartMargins(host, width);
  const plotW = width - margin.left - margin.right;
  const plotH = height - margin.top - margin.bottom;
  const [timeMin, timeMax] = timelineDomain(systems);
  const [yMin, yMax] = valueExtent(systems, metric);
  const x = (time) => margin.left + (time - timeMin) / (timeMax - timeMin) * plotW;
  const y = (number) => margin.top + (yMax - number) / (yMax - yMin) * plotH;
  const svg = svgElement("svg", { viewBox: `0 0 ${width} ${height}`, preserveAspectRatio: "none", tabindex: "0" });

  const axis = svgElement("g");
  for (let i = 0; i <= 4; i += 1) {
    const current = yMin + (yMax - yMin) * i / 4;
    const py = y(current);
    axis.append(svgElement("line", { x1: margin.left, x2: width - margin.right, y1: py, y2: py, class: "chart-grid" }));
    axis.append(svgElement("text", { x: margin.left - 10, y: py + 4, class: "chart-axis", "text-anchor": "end" }, formatNumber(current, metric === "socPct" ? 0 : 1)));
  }
  const tickCount = host.clientWidth < 650 ? 4 : 8;
  for (let i = 0; i <= tickCount; i += 1) {
    const current = timeMin + (timeMax - timeMin) * i / tickCount;
    const px = x(current);
    axis.append(svgElement("line", { x1: px, x2: px, y1: margin.top, y2: height - margin.bottom, class: "chart-grid" }));
    axis.append(svgElement("text", { x: px, y: height - 15, class: "chart-axis", "text-anchor": i === 0 ? "start" : i === tickCount ? "end" : "middle" }, formatDate(new Date(current).toISOString(), true)));
  }
  if (yMin < 0 && yMax > 0) axis.append(svgElement("line", { x1: margin.left, x2: width - margin.right, y1: y(0), y2: y(0), class: "chart-zero" }));
  svg.append(axis);

  systems.forEach((system) => {
    const valid = system.points.filter((point) => Number.isFinite(Number(point[metric])));
    const path = valid.map((point, index) => `${index ? "L" : "M"}${x(Date.parse(point.t)).toFixed(1)},${y(Number(point[metric])).toFixed(1)}`).join(" ");
    svg.append(svgElement("path", { d: path, class: `chart-path ${system.id}` }));
  });

  const cursor = svgElement("g", { class: "cursor-layer" });
  const cursorLine = svgElement("line", { class: "chart-cursor", y1: margin.top, y2: height - margin.bottom });
  cursor.append(cursorLine);
  const dots = systems.map((system) => {
    const dot = svgElement("circle", { r: 5, class: "chart-dot", fill: systemColor(system.id) });
    cursor.append(dot);
    return dot;
  });
  svg.append(cursor);

  function updateCursor(clientX) {
    const bounds = svg.getBoundingClientRect();
    const px = Math.max(margin.left, Math.min(width - margin.right, (clientX - bounds.left) / bounds.width * width));
    const timestamp = timeMin + (px - margin.left) / plotW * (timeMax - timeMin);
    cursorLine.setAttribute("x1", px);
    cursorLine.setAttribute("x2", px);
    const labels = systems.map((system, index) => {
      const point = system.points.reduce((nearest, candidate) => Math.abs(Date.parse(candidate.t) - timestamp) < Math.abs(Date.parse(nearest.t) - timestamp) ? candidate : nearest);
      dots[index].setAttribute("cx", x(Date.parse(point.t)));
      dots[index].setAttribute("cy", y(Number(point[metric])));
      return `${system.shortName}: ${formatNumber(point[metric], METRICS[metric].decimals)} ${METRICS[metric].unit}`;
    });
    readout.textContent = `${formatDate(new Date(timestamp).toISOString())} · ${labels.join(" · ")}`;
  }

  svg.addEventListener("pointermove", (event) => updateCursor(event.clientX));
  svg.addEventListener("focus", () => updateCursor(svg.getBoundingClientRect().left + svg.getBoundingClientRect().width / 2));
  host.append(svg);
}

function renderIntent() {
  const grid = $("#intent-grid");
  grid.replaceChildren();
  const systems = state.data.systems.filter((system) => system.points.length);
  const [timeMin, timeMax] = timelineDomain(systems);
  const duration = timeMax - timeMin;
  systems.forEach((system) => {
    const row = element("div", { class: "intent-row" });
    row.append(element("div", { class: "intent-name" }, system.shortName));
    const track = element("div", { class: "intent-track", title: "Batterieabsicht je 15 Minuten" });
    system.points.slice(0, 192).forEach((point) => {
      const left = (Date.parse(point.t) - timeMin) / duration * 100;
      const width = SLOT_MS / duration * 100;
      track.append(element("span", {
        class: "intent-slot",
        "data-intent": point.intent,
        style: `left:${left}%;width:${width}%`,
        title: `${formatDate(point.t)} · ${point.intent}`,
      }));
    });
    row.append(track);
    grid.append(row);
  });
}

function renderForecast() {
  const config = METRICS[state.metric];
  $("#forecast-label").textContent = config.label;
  $("#forecast-unit").textContent = config.unit;
  drawChart($("#forecast-chart"), state.metric, $("#forecast-readout"));
}

function renderAgreement() {
  const comparison = state.data.comparison;
  const value = comparison.intentAgreementPct;
  $("#agreement-value").textContent = value === null || value === undefined ? "–" : `${formatNumber(value, 0)}%`;
  $("#agreement-bar").style.width = `${value || 0}%`;
  const pairs = comparison.pairwise || [];
  const mean = pairs.length ? pairs.reduce((sum, pair) => sum + (pair.maeGridKw || 0), 0) / pairs.length : null;
  $("#agreement-copy").textContent = mean === null ? "Zu wenige gemeinsame Zeitpunkte" : `Mittlere paarweise Netz-Abweichung: ${formatNumber(mean, 2)} kW`;
}

function renderQuality() {
  const quality = state.data.quality;
  const ranking = $("#quality-ranking");
  const daysBody = $("#quality-days");
  ranking.replaceChildren();
  daysBody.replaceChildren();
  if (!quality || !quality.systems) {
    $("#quality-verdict").textContent = "Keine Auswertung verfügbar";
    $("#quality-copy").textContent = "Mitternachtsstände werden gesammelt";
    $("#quality-window").textContent = "";
    $("#quality-method").textContent = "";
    return;
  }

  const systemNames = Object.fromEntries(quality.systems.map((system) => [system.id, system.name]));
  const available = quality.systems.filter((system) => hasNumber(system.distanceScorePct));
  const maxError = Math.max(1, ...available.map((system) => Number(system.distanceScorePct)));
  const winners = quality.winnerIds || [];

  if (quality.comparisonReady && winners.length) {
    const names = winners.map((id) => systemNames[id]).join(" + ");
    $("#quality-verdict").textContent = winners.length > 1 ? `Gleichauf: ${names}` : `Am nächsten: ${names}`;
    $("#quality-copy").textContent = "Gemeinsamer Abstand aus SOC und Netzbezug";
  } else {
    $("#quality-verdict").textContent = "Vergleich noch offen";
    const pending = quality.systems.filter((system) => !system.capturedDays).map((system) => system.name);
    $("#quality-copy").textContent = pending.length
      ? `${pending.join(", ")}: Mitternachtsstände werden gesammelt`
      : "Für eine belastbare Rangfolge fehlen gemeinsame Tage";
  }

  [...quality.systems]
    .sort((left, right) => {
      const leftMeasured = hasNumber(left.distanceScorePct);
      const rightMeasured = hasNumber(right.distanceScorePct);
      if (leftMeasured !== rightMeasured) return leftMeasured ? -1 : 1;
      if (!leftMeasured) return 0;
      const a = Number(left.distanceScorePct);
      const b = Number(right.distanceScorePct);
      return a - b;
    })
    .forEach((system) => {
      const measured = hasNumber(system.distanceScorePct);
      const row = element("article", {
        class: `quality-rank-row${winners.includes(system.id) ? " winner" : ""}${measured ? "" : " pending"}`,
        style: `--system-color:${systemColor(system.id)}`,
      });
      const identity = element("div", { class: "quality-identity" });
      identity.append(element("strong", {}, system.name));
      identity.append(element("span", {}, system.days
        ? `${system.days} gemeinsame Tagesproben`
        : system.capturedDays
          ? `${system.capturedDays} Mitternachtspläne · noch ohne EOS-Vergleich`
          : "Datenerhebung läuft"));
      row.append(identity);
      const distance = element("div", {
        class: "quality-distance",
        "aria-hidden": "true",
        "data-pending": system.capturedDays ? "wartet auf gemeinsame EOS-Tage" : "Aufzeichnung ab jetzt",
      });
      if (measured) distance.append(element("span", { style: `width:${Number(system.distanceScorePct) / maxError * 100}%` }));
      row.append(distance);
      const score = element("div", { class: "quality-score" });
      score.append(element("strong", {}, measured ? `${formatNumber(system.distanceScorePct, 1)}%` : "–"));
      score.append(element("span", {}, measured
        ? `Bezug ${formatNumber(system.gridImportMaeKw, 2)} kW · SOC ${formatNumber(system.socMaePp, 1)} pp`
        : system.capturedDays ? "noch keine gemeinsamen Tage" : "Mitternachtsstände werden gesammelt"));
      row.append(score);
      ranking.append(row);
    });

  const ids = ["microems", "eos_import", "eos_native"];
  (quality.days || []).slice(0, 7).forEach((day) => {
    const row = element("tr");
    const actual = element("td");
    actual.append(element("strong", {}, formatDay(day.day)));
    actual.append(element("span", {}, `Ist · Bezug ${formatNumber(day.actualImportKwh, 1)} kWh · SOC ${formatNumber(day.actualSocStartPct, 0)}→${formatNumber(day.actualSocEndPct, 0)}%`));
    row.append(actual);
    ids.forEach((id) => {
      const result = day.systems[id];
      const cell = element("td", { title: result?.issuedAt ? `Prognosestand ${formatDate(result.issuedAt)}` : "" });
      if (result?.available) {
        cell.append(element("strong", {}, `${formatNumber(result.distanceScorePct, 1)}%`));
        cell.append(element("span", {}, `Bezug ${formatNumber(result.gridImportMaeKw, 2)} kW · SOC ${formatNumber(result.socMaePp, 1)} pp`));
      } else {
        cell.append(element("span", { class: "quality-pending" }, "noch nicht erfasst"));
      }
      row.append(cell);
    });
    daysBody.append(row);
  });
  if (!(quality.days || []).length) {
    const row = element("tr");
    row.append(element("td", { colspan: "4", class: "quality-empty" }, "Noch kein abgeschlossener Tag mit Mitternachtsfahrplan und vollständigen Bezugs-/SOC-Istwerten."));
    daysBody.append(row);
  }
  $("#quality-window").textContent = `${quality.commonDays} gemeinsame Tage · ${quality.lookbackDays} Tage Fenster`;
  $("#quality-method").textContent = quality.note || "";
}

function renderDivergence() {
  const list = $("#divergence-list");
  list.replaceChildren();
  (state.data.comparison.divergence || []).forEach((item) => {
    const card = element("article", { class: "divergence-item" });
    card.append(element("time", { datetime: item.t }, formatDate(item.t)));
    card.append(element("strong", {}, `${formatNumber(item.spreadKw, 1)} kW`));
    const values = element("div", { class: "divergence-values" });
    state.data.systems.forEach((system) => {
      const line = element("span", { style: `--system-color:${systemColor(system.id)}` });
      line.append(element("i"));
      line.append(document.createTextNode(system.shortName));
      line.append(element("b", {}, `${formatNumber(item.values[system.id], 1)} kW · ${item.intents[system.id]}`));
      values.append(line);
    });
    card.append(values);
    list.append(card);
  });
}

function renderMethod() {
  const grid = $("#method-grid");
  grid.replaceChildren();
  state.data.systems.forEach((system) => {
    const article = element("article", { style: `--system-color:${systemColor(system.id)}` });
    article.append(element("h3", {}, system.shortName));
    article.append(element("p", {}, system.forecast));
    article.append(element("p", {}, system.optimizer));
    grid.append(article);
  });
  $("#raster-note").textContent = state.data.note;
}

function render() {
  $("#updated-at").textContent = `Leitstand ${formatDate(state.data.generatedAt)}`;
  renderSystemRail();
  renderScoreboard();
  drawChart($("#grid-chart"), "gridKw", $("#grid-readout"));
  renderIntent();
  renderForecast();
  renderAgreement();
  renderQuality();
  renderDivergence();
  renderMethod();
}

function toast(message) {
  const node = $("#toast");
  node.textContent = message;
  node.classList.add("visible");
  window.setTimeout(() => node.classList.remove("visible"), 6000);
}

async function load() {
  $("#loading").classList.remove("hidden");
  $("#refresh").disabled = true;
  try {
    const response = await fetch("/api/comparison", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    state.data = await response.json();
    render();
    if (state.data.errors.length) toast(state.data.errors.join(" · "));
  } catch (error) {
    toast(`Vergleichsdaten konnten nicht geladen werden: ${error.message}`);
  } finally {
    $("#loading").classList.add("hidden");
    $("#refresh").disabled = false;
  }
}

$("#refresh").addEventListener("click", load);
$$('[role="tab"]').forEach((button) => button.addEventListener("click", () => {
  $$('[role="tab"]').forEach((item) => item.setAttribute("aria-selected", item === button ? "true" : "false"));
  state.metric = button.dataset.metric;
  renderForecast();
}));

let resizeTimer;
window.addEventListener("resize", () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => state.data && (drawChart($("#grid-chart"), "gridKw", $("#grid-readout")), renderForecast()), 120);
});

load();
setInterval(load, 5 * 60 * 1000);
