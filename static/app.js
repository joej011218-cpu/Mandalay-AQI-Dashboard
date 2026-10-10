"use strict";
const $ = (id) => document.getElementById(id);
const names = {
  pm2_5: "PM₂.₅",
  pm10: "PM₁₀",
  nitrogen_dioxide: "NO₂",
  sulphur_dioxide: "SO₂",
  ozone: "O₃",
  carbon_monoxide: "CO",
  ammonia: "NH₃",
};
const units = {carbon_monoxide:"mg/m³",ammonia:"µg/m³"};
const source = p => ["carbon_monoxide","ammonia"].includes(p)?"ESP32":"Open-Meteo";
const unit = p => units[p] || "µg/m³";
let theme;
try {
  theme = localStorage.getItem("theme");
} catch {}
document.documentElement.dataset.theme = theme || "dark";
$("theme").onclick = () => {
  const t =
    document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = t;
  try {
    localStorage.setItem("theme", t);
  } catch {}
};
const page = location.pathname;
for (const a of document.querySelectorAll("nav a"))
  if (a.getAttribute("href") === page) a.setAttribute("aria-current", "page");
$("page-title").textContent =
  {
    "/": "Mandalay Air Quality — Live",
    "/analytics": "Mandalay Air Quality — Analytics",
    "/alerts": "Mandalay Air Quality — Alerts",
    "/about": "Mandalay Air Quality — About",
  }[page] || "Mandalay air quality";
$("live").hidden = page !== "/";
$("history").hidden = !["/", "/analytics"].includes(page);
$("analytics").hidden = page !== "/analytics";
$("alerts").hidden = page !== "/alerts";
$("about").hidden = page !== "/about";
$("overview").hidden = !["/analytics", "/alerts"].includes(page);
const clock = () => {
  $("clock").textContent =
    new Date().toLocaleString("en-GB", { timeZone: "Asia/Yangon" }) + " · MMT";
};
clock();
setInterval(clock, 1000);
const fmt = (v) =>
  v == null
    ? "—"
    : Number(v).toLocaleString(undefined, { maximumFractionDigits: 1 });
const time = (v) =>
  new Date(v).toLocaleString("en-GB", {
    timeZone: "Asia/Yangon",
    dateStyle: "medium",
    timeStyle: "short",
  });
function card(label, value, detail) {
  const div = document.createElement("div");
  div.className = "card";
  const h = document.createElement("h3");
  h.textContent = label;
  const strong = document.createElement("strong");
  strong.textContent = value;
  const p = document.createElement("p");
  p.textContent = detail;
  div.append(h, strong, p);
  return div;
}
async function api(path) {
  const r = await fetch(path, { cache: "no-store" });
  const data = await r.json();
  if (!r.ok) throw new Error(data.error || "Data service unavailable");
  if (!data.aqi_standard?.startsWith("Indian CPCB AQI"))
    throw new Error("Unexpected AQI standard; CPCB data is required.");
  return data;
}
function chart(
  rows,
  svg = $("chart"),
  field = "cpcb_aqi",
  caption = $("chart-caption"),
) {
  svg.replaceChildren();
  let axis = svg.nextElementSibling;
  if (!axis || !axis.classList.contains("chart-time-axis")) {
    axis = document.createElement("div");
    axis.className = "chart-time-axis";
    axis.setAttribute("aria-label", "Observation times in Asia/Yangon");
    svg.after(axis);
  }
  axis.replaceChildren();
  if (!rows.length) {caption.textContent="No observations in this period.";return;}
  const points = rows.filter((r) => r[field] != null);

  const start = new Date(rows[0].timestamp).getTime(),
    end = new Date(rows.at(-1).timestamp).getTime();
  const tickFormat = new Intl.DateTimeFormat("en-US", {
    timeZone: "Asia/Yangon", month:"short",day:"numeric",hour: "numeric", minute: "2-digit", hour12: true,
  });
  const ticks=Math.min(5,rows.length);
  for (let i = 0; i < ticks; i++) {
    const row = rows[Math.round((i * (rows.length - 1)) / Math.max(1,ticks-1))];
    const label = document.createElement("span");
    label.textContent = tickFormat.format(new Date(row.timestamp)).toLowerCase();
    label.title = time(row.timestamp);
    axis.append(label);
  }
  if (!points.length) {caption.textContent="No covered observations; gaps are not filled.";return;}
  const max = Math.max(50, ...points.map((r) => r[field]));
  const x = (r) =>
    40 +
    ((new Date(r.timestamp).getTime() - start) /
      Math.max(3600000, end - start)) *
      820;
  const y = (r) => 225 - (r[field] / max) * 200;
  const add = (tag, attrs) => {
    const el = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
    svg.append(el);
    return el;
  };
  for (let i = 0; i <= 4; i++) {
    const yy = 225 - i * 50;
    add("line", { x1: 40, x2: 860, y1: yy, y2: yy, stroke: "var(--border)" });
    add("text", {
      x: 2,
      y: yy,
      fill: "var(--muted)",
      "font-size": 12,
    }).textContent = Math.round((max * i) / 4);
  }
  let previous;
  for (const r of rows) {
    if (r[field] == null) {
      previous = null;
      continue;
    }
    if (
      previous &&
      new Date(r.timestamp) - new Date(previous.timestamp) <= ($("view").value === "daily" ? 129600000 : 5400000)
    )
      add("line", {
        x1: x(previous),
        y1: y(previous),
        x2: x(r),
        y2: y(r),
        stroke: "var(--accent)",
        "stroke-width": 2,
      });
    const dot = add("circle", {
      cx: x(r),
      cy: y(r),
      r: 2,
      fill: "var(--accent)",
    });
    const title = document.createElementNS(
      "http://www.w3.org/2000/svg",
      "title",
    );
    title.textContent = `${time(r.timestamp)} · ${field === "cpcb_aqi" ? "AQI" : names[field]} ${fmt(r[field])}`;
    dot.append(title);
    previous = r;
  }
  caption.textContent = `${time(rows[0].timestamp)} — ${time(rows.at(-1).timestamp)} · ${field === "cpcb_aqi" ? "CPCB AQI" : names[field] + " ("+unit(field)+") · "+source(field)} · ${points.length} ${$("view").value} observations. Gaps are not interpolated.`;
}
let loading = false;
async function load() {
  if (loading || page === "/about") {
    if (page === "/about")
      $("status").textContent = "Mandalay only · transparent data provenance";
    return;
  }
  loading = true;
  $("status").classList.remove("error");
  $("status").textContent = "Updating Mandalay observations…";
  try {
    const days = $("days").value;
    if (page === "/") {
      const data = await api("/api/monitoring/latest");
      const r = data.data;
      $("aqi").textContent = fmt(r.cpcb_aqi);
      $("category").textContent = r.category + " · " + r.aqi_label;
      document.querySelector(".hero").style.setProperty("--aqi-color", r.color);
      $("guidance").textContent = r.precautions;
      $("dominant").textContent = r.dominant_pollutants.length
        ? r.dominant_pollutants.map((p) => names[p] || p).join(", ")
        : "Unavailable";
      $("eligibility").textContent =
        `${r.eligible_pollutants.length}/7 eligible pollutants · ${r.status} · ${r.coverage}`;
      $("aqi-reasons").textContent = [...r.reasons,r.missing_pollutants.length?"Missing/insufficient: "+r.missing_pollutants.map(p=>names[p]).join(", "):""].filter(Boolean).join("; ");
      $("hardware-status").textContent=`ESP32: ${r.hardware.status} · ${r.hardware.transport==="webhook"?"EMQX HTTPS delivery":`MQTT subscriber ${r.hardware.mqtt_connected?"connected":"disconnected/unverified"}`}`;
      $("calculation-details").replaceChildren();
      for (const [field, detail] of Object.entries(r.pollutant_details)) {
        const tr = document.createElement("tr");
        for (const value of [
          names[field],
          `${detail.averaging_hours}h`,
          `${detail.valid_hours_24}/24 · ${detail.valid_hours_window}/${detail.averaging_hours}`,
          `${fmt(detail.average)} ${detail.unit}`,
          `${fmt(detail.reported_concentration)} ${detail.unit}`,
          fmt(detail.subindex),
          detail.reason || "Eligible",
        ]) {
          const td = document.createElement("td");
          td.textContent = value;
          tr.append(td);
        }
        $("calculation-details").append(tr);
      }
      $("updated").textContent =
        `${time(r.timestamp)} · hourly rolling calculation · provisional mixed-source estimates`;
      $("pollutants").replaceChildren(
        ...Object.entries(names).map(([k, n]) => {const m=r.measurements[k];return card(n,fmt(m.value),`${m.unit} · ${m.source} · ${m.status}${m.timestamp?" · "+time(m.timestamp):" · no measurement"}`);}),
      );
      $("status").textContent = `Mandalay monitoring · ${r.aqi_label} · ${r.status}${r.model_stale?" · Open-Meteo stale/missing":""}`;
    }
    if (["/", "/analytics"].includes(page)) {
      const h = await api(`/api/monitoring/history?days=${days}&view=${$("view").value}`);
      chart(h.data);
      if (page === "/analytics") {
        const a = await api(`/api/monitoring/analytics?days=${days}`);
        const v = a.summary.cpcb_aqi;
        $("overview-cards").replaceChildren(
          card("Average AQI", fmt(v.mean), "Selected period"),
          card("Peak AQI", fmt(v.max), "Selected period"),
          card("Best AQI", fmt(v.min), "Selected period"),
          card("AQI readings", String(v.count), "Eligible calculated hours"),
        );
        $("pollutant-charts").replaceChildren();
        for (const [field, label] of Object.entries(names)) {
          const box = document.createElement("div");
          box.className = "chart-box";
          const title = document.createElement("h3");
          title.textContent = label + " ("+unit(field)+") · "+source(field);
          const svg = document.createElementNS(
            "http://www.w3.org/2000/svg",
            "svg",
          );
          svg.setAttribute("viewBox", "0 0 900 260");
          svg.setAttribute("role", "img");
          svg.setAttribute(
            "aria-label",
            label + " Mandalay concentration history",
          );
          const caption = document.createElement("p");
          box.append(title, svg, caption);
          $("pollutant-charts").append(box);
          chart(h.data, svg, field, caption);
        }
        $("stats").replaceChildren(
          ...Object.entries(a.summary).map(([k, v]) =>
            card(
              names[k] ? `${names[k]} (${unit(k)}) · ${source(k)}` : "CPCB AQI",
              fmt(v.mean),
              `Min ${fmt(v.min)} · Max ${fmt(v.max)} · ${v.count} readings`,
            ),
          ),
        );
        $("status").textContent =
          `${a.observations} Mandalay observations · ${a.unavailable_aqi_count} CPCB AQI unavailable in the selected period`;
      }
    }
    if (page === "/alerts") {
      const a = await api("/api/monitoring/alerts?days=7");
      const current = await api("/api/monitoring/latest").catch(() => null);
      const numeric=a.data.filter(r=>r.alert_type==='cpcb-aqi' && r.value!=null);
      $('overview-cards').replaceChildren(card('Current CPCB',fmt(current?.data?.cpcb_aqi),current?.data?.aqi_label||'Unavailable'),card('Monitoring alerts',String(a.data.length),'Rolling, raw and sensor status'),card('Peak CPCB alert',numeric.length?fmt(Math.max(...numeric.map(r=>r.value))):'—','Provisional'),card('ESP32',current?.data?.hardware.status||'unavailable','Hardware CO/NH₃ only'));
      $('alert-list').replaceChildren();
      for (const r of a.data) {
        const tr=document.createElement('tr');tr.style.borderLeft=`4px solid ${r.color}`;
        for (const value of [time(r.timestamp),names[r.pollutant]||r.pollutant||'AQI',r.alert_type+' · '+r.severity,fmt(r.value)+' '+r.unit,fmt(r.threshold)+' '+r.unit+(r.averaging_hours?' · '+r.averaging_hours+'h':''),r.source,r.message]) {
          const td=document.createElement('td');td.textContent=value;tr.append(td);
        }
        $('alert-list').append(tr);
      }
      $("alert-empty").hidden = !!a.data.length;
      $("status").textContent =
        `${a.data.length} Mandalay monitoring alerts in the last seven days`;
    }
  } catch (e) {
    $("status").textContent = e.message;
    $("status").classList.add("error");
  } finally {
    loading = false;
  }
}
$("days").onchange = load;
$("view").onchange = load;
load();
setInterval(load, 300000);
