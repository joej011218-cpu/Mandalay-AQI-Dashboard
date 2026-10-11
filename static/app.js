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
document.documentElement.dataset.page=page.slice(1)||"live";
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
if($("history"))$("history").hidden = page!=="/analytics";
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
const estimate = v => typeof v === "number" && Number.isFinite(v)
  ? v.toLocaleString(undefined, { maximumSignificantDigits: 4 }) : "—";
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
const exact = v => typeof v === "number" && Number.isFinite(v) ? String(v) : "—";
const measurementTime = v => new Date(v).toLocaleString("en-GB", {
  timeZone:"Asia/Yangon", dateStyle:"medium", timeStyle:"medium",
}) + " · Asia/Yangon";
function hardwareValidity(row, field) {
  if (field === "carbon_monoxide" && row.validity?.co_range_valid === false)
    return "Outside calibrated range — not AQI eligible";
  const key=field === "carbon_monoxide"?"co_mg_m3":"nh3_ug_m3";
  if (exact(row[key]) === "—") return "Unavailable — not AQI eligible";
  return row[field === "carbon_monoxide"?"co_valid":"nh3_valid"] === true
    ? "Valid estimate" : "Invalid — not AQI eligible";
}
let activeSensorTooltip;
document.addEventListener("pointerdown",event=>{
  if(activeSensorTooltip && !activeSensorTooltip.box.contains(event.target))activeSensorTooltip.dismiss();
});
// Yangon has a fixed UTC+06:30 offset; tick times never alter observations.
const analyticsAxes=new Map();
function hourlyTicks(start,end,width,labelWidth=70,minHours=1) {
  const hour=3600000,offset=6.5*hour;
  const slots=Math.max(1,Math.floor(width/labelWidth));
  const minimum=Math.max(minHours,(end-start)/hour/slots);
  const steps=[1,2,3,6,12,24,48,72,168,336,720,1440,2880,4320,8760];
  const step=steps.find(v=>v>=minimum)||Math.ceil(minimum/24)*24;
  const interval=step*hour;
  // Multiples >=24 hours align to Yangon midnight; shorter ones align to
  // local clock hours (e.g. 00:00, 03:00, 06:00 for a three-hour interval).
  const first=Math.ceil((start+offset)/interval)*interval-offset;
  const timestamps=[];
  for(let at=first;at<=end;at+=interval)timestamps.push(at);
  return {timestamps,step};
}
function dailyTicks(start,end) {
  const day=86400000,offset=6.5*3600000;
  const timestamps=[];
  for(let at=Math.ceil((start+offset)/day)*day-offset;at<=end;at+=day)timestamps.push(at);
  return {timestamps,step:24};
}
function analyticsTimeAxis(svg,axis,start,end) {
  axis.replaceChildren();axis.classList.add("hourly-time-axis");
  const daily=Number($("days").value)!==1;
  const box=svg.parentElement;
  box.classList.toggle("daily-chart-scroll",daily);
  if(daily){
    const width=Math.max(box.clientWidth-40,Math.ceil((end-start)/86400000)*72*900/820);
    svg.style.width=width+"px";svg.style.height="260px";
    svg.setAttribute("preserveAspectRatio","none");axis.style.width=width+"px";
    box.setAttribute("tabindex","0");box.setAttribute("aria-label","Chart with daily Yangon dates; scroll horizontally to inspect all days");
  } else {
    svg.style.removeProperty("width");svg.style.removeProperty("height");axis.style.removeProperty("width");
    svg.removeAttribute("preserveAspectRatio");box.removeAttribute("tabindex");box.removeAttribute("aria-label");
  }
  const rect=svg.getBoundingClientRect();
  const scale=Math.min(rect.width/900,rect.height/260);
  const inset=daily?0:(rect.width-900*scale)/2;
  const timeScale=daily?rect.width/900:scale;
  // Horizontal expansion must not stretch the labels or sensor markers.
  for(const label of svg.querySelectorAll("text")){
    if(daily)label.setAttribute("transform",`scale(${1/timeScale},1)`);
    else label.removeAttribute("transform");
  }
  for(const dot of svg.querySelectorAll("circle")){
    if(daily){
      const cx=dot.getAttribute("cx"),cy=dot.getAttribute("cy");
      dot.setAttribute("transform",`translate(${cx},${cy}) scale(${1/timeScale},1) translate(${-cx},${-cy})`);
    } else dot.removeAttribute("transform");
  }

  // SVG viewBox scaling must not shrink the Y-axis labels on phones.
  for(const label of svg.querySelectorAll("text"))label.setAttribute("font-size",Math.min(40,12/Math.max(.1,scale)));
  const mode=daily?"date":"time";
  const ticks=daily?dailyTicks(start,end):hourlyTicks(start,end,820*scale);
  const formatter=new Intl.DateTimeFormat("en-US",{
    timeZone:"Asia/Yangon",
    ...(mode!=="time"?{month:"short",day:"numeric"}:{}),
    ...(mode!=="date"?{hour:"numeric",hour12:true}:{}),
  });
  axis.dataset.intervalHours=String(ticks.step);
  for(const at of ticks.timestamps) {
    const tick=document.createElement("span"),label=document.createElement("span");
    tick.dataset.timestamp=new Date(at).toISOString();
    const left=inset+(40+(at-start)/Math.max(3600000,end-start)*820)*timeScale;
    tick.style.left=left+"px";tick.title=measurementTime(at);
    label.textContent=formatter.format(at);tick.append(label);axis.append(tick);
    // Keep text inside the chart while its small tick marker stays at the
    // exact SVG time coordinate, including SVG letterboxing on wide screens.
    const labelWidth=label.getBoundingClientRect().width;
    const labelLeft=Math.max(0,Math.min(rect.width-labelWidth,left-labelWidth/2));
    label.style.left=(labelLeft-left)+"px";
  }
}
let axisResizePending=false;
window.addEventListener("resize",()=>{
  if(page!=="/analytics" || axisResizePending)return;
  axisResizePending=true;
  requestAnimationFrame(()=>{
    axisResizePending=false;
    for(const [svg,state] of analyticsAxes){
      if(!svg.isConnected)analyticsAxes.delete(svg);
      else analyticsTimeAxis(svg,...state);
    }
  });
});
function chart(
  rows,
  svg = $("chart"),
  field = "cpcb_aqi",
  caption = $("chart-caption"),
  diagnostic = false,
  maxGapMs = 5400000,
) {
  svg.replaceChildren();
  svg.parentElement.querySelector(".sensor-tooltip")?.remove();
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
  if(page==="/analytics") {
    for(const svg of analyticsAxes.keys())if(!svg.isConnected)analyticsAxes.delete(svg);
    analyticsAxes.set(svg,[axis,start,end]);
    analyticsTimeAxis(svg,axis,start,end);
  } else {
    const tickFormat = new Intl.DateTimeFormat("en-US", {
      timeZone: "Asia/Yangon", month:"short",day:"numeric",hour: "numeric", minute: "2-digit", hour12: true,
    });
    const ticks=Math.min(5,rows.length);
    for (let i = 0; i < ticks; i++) {
      const row = rows[Math.round((i * (rows.length - 1)) / Math.max(1,ticks-1))];
      const label = document.createElement("span");
      const tick=diagnostic?new Date(start+(end-start)*i/Math.max(1,ticks-1)):new Date(row.timestamp);
      label.textContent = tickFormat.format(tick).toLowerCase();
      label.title = time(tick);
      axis.append(label);
    }
  }
  if (!points.length) {caption.textContent="No covered observations; gaps are not filled.";return;}
  const max = Math.max(field === "cpcb_aqi" ? 50 : 1, ...points.map((r) => r[field]));
  const x = (r) =>
    40 +
    ((new Date(r.timestamp).getTime() - start) /
      Math.max(3600000, end - start)) *
      820;
  const y = (r) => 225 - (r[field] / max) * 200;
  const analytics=page==="/analytics";
  const lineColor=diagnostic?"var(--accent)":"var(--chart-line)";
  const add = (tag, attrs) => {
    const el = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
    svg.append(el);
    return el;
  };
  for (let i = 0; i <= 4; i++) {
    const yy = 225 - i * 50;
    add("line", { x1: 40, x2: 860, y1: yy, y2: yy, stroke: "var(--border)", "stroke-opacity": analytics ? .45 : 1 });
    add("text", {
      x: 2,
      y: yy,
      fill: "var(--muted)",
      "font-size": 12,
    }).textContent = fmt((max * i) / 4);
  }
  let tooltip,pinned=false;
  const dismiss=()=>{pinned=false;if(tooltip)tooltip.hidden=true;};
  const sensorTargets=[];
  if (diagnostic || analytics) {
    tooltip=document.createElement("div");tooltip.className="sensor-tooltip";
    tooltip.setAttribute("role","tooltip");tooltip.hidden=true;
    svg.parentElement.append(tooltip);
    // Keep tooltips in the visible card when its daily timeline is scrolled.
    const box=svg.parentElement;
    if(analytics)box.onscroll=()=>{
      if(box.classList.contains("daily-chart-scroll")){
        tooltip.style.left=(box.scrollLeft+12)+"px";tooltip.style.right="auto";
        tooltip.style.width=Math.max(0,box.clientWidth-24)+"px";tooltip.style.boxSizing="border-box";
      }
    };
    if(analytics)box.onscroll();

  }
  let previous,segment=[];
  const drawSegment=()=>{
    if(segment.length>1)add("polyline",{
      points:segment.map(r=>`${x(r)},${y(r)}`).join(" "),fill:"none",
      stroke:diagnostic && !segment[0].sensor_valid?"#E8C547":lineColor,
      "stroke-dasharray":diagnostic && !segment[0].sensor_valid?"4 3":"none",
      "stroke-width":2,"stroke-linecap":"round","stroke-linejoin":"round",
      "vector-effect":"non-scaling-stroke","pointer-events":"none",
    });
    segment=[];
  };
  for (const r of rows) {
    if (r[field] == null) {
      if(analytics){drawSegment();analyticsTimeAxis(svg,axis,start,end);}
      previous = null;
      continue;
    }
    const connected=(
      previous &&
      new Date(r.timestamp) - new Date(previous.timestamp) <= (diagnostic ? maxGapMs : $("view").value === "daily" ? 129600000 : 5400000) &&
      (!diagnostic || r.sensor_valid === previous.sensor_valid)
    );
    if(analytics){
      if(!connected)drawSegment();
      segment.push(r);
    } else if(connected)
      add("line", {
        x1: x(previous),
        y1: y(previous),
        x2: x(r),
        y2: y(r),
        stroke: diagnostic && !r.sensor_valid ? "#E8C547" : "var(--accent)",
        "stroke-dasharray": diagnostic && !r.sensor_valid ? "4 3" : "none",
        "stroke-width": 2,
      });
    const dot = add("circle", {
      cx: x(r),
      cy: y(r),
      r: diagnostic ? 5 : analytics ? 1.5 : 2,
      stroke: diagnostic ? "var(--bg)" : "none",
      "stroke-width": diagnostic ? 1.5 : 0,
      fill: diagnostic && !r.sensor_valid ? "#E8C547" : analytics ? lineColor : "var(--accent)",
      "data-valid": diagnostic ? String(r.sensor_valid) : "covered",
    });
    const title = document.createElementNS(
      "http://www.w3.org/2000/svg",
      "title",
    );
    title.textContent = `${time(r.timestamp)} · ${field === "cpcb_aqi" ? "AQI" : names[field]} ${diagnostic ? estimate(r[field]) : fmt(r[field])}${diagnostic ? " " + unit(field) + " · ESP32 · " + (r.sensor_valid ? "valid estimate" : "invalid estimate — not AQI eligible") : ""}`;
    dot.append(title);
    if (diagnostic || analytics) {
      const detail=diagnostic?`${names[field]} ${exact(r[field])} ${unit(field)}\n${measurementTime(r.timestamp)}\n${r.validity_label} · ESP32\nDevice: ${r.device_id}`
        : `${field==="cpcb_aqi"?"CPCB AQI":names[field]} ${exact(r[field])} ${field==="cpcb_aqi"?"AQI":unit(field)}\n${measurementTime(r.timestamp)}\n${field==="cpcb_aqi"?"Provisional calculated estimate · Combined sources":"Modeled observation · Open-Meteo"}`;
      title.textContent=detail;
      dot.classList.add("sensor-point");dot.setAttribute("tabindex","0");
      dot.setAttribute("role","button");dot.setAttribute("aria-label",detail);
      const show=()=>{
        if(activeSensorTooltip?.tooltip!==tooltip)activeSensorTooltip?.dismiss();
        activeSensorTooltip={tooltip,box:svg.parentElement,dismiss};
        tooltip.textContent=detail;tooltip.hidden=false;
      };
      const hide=()=>{if(!pinned)tooltip.hidden=true;};
      sensorTargets.push({dot,show,x:x(r),y:y(r)});
      dot.addEventListener("pointerenter",show);
      dot.addEventListener("pointermove",show);dot.addEventListener("pointerleave",event=>{if(event.pointerType!=="touch")hide();});
      dot.addEventListener("focus",show);dot.addEventListener("blur",hide);
      dot.addEventListener("click",()=>{pinned=true;show();});
      dot.addEventListener("keydown",event=>{
        if(event.key==="Escape")dismiss();
        if(event.key==="Enter" || event.key===" "){event.preventDefault();pinned=true;show();}
      });
    }
    previous = r;
  }
  if(analytics){drawSegment();analyticsTimeAxis(svg,axis,start,end);}
  if (diagnostic || analytics) {
    // A small screen scales SVG dots down. Permit nearby pointer/touch inspection
    // in screen pixels without adding fabricated points or changing their times.
    const inspectPoint=event=>{
      const matrix=svg.getScreenCTM();
      if(!matrix)return;
      let nearest,distance=Infinity;
      for(const target of sensorTargets){
        const dx=event.clientX-(matrix.a*target.x+matrix.c*target.y+matrix.e);
        const dy=event.clientY-(matrix.b*target.x+matrix.d*target.y+matrix.f);
        const d=dx*dx+dy*dy;
        if(d<distance){distance=d;nearest=target;}
      }
      if(nearest && distance<=(event.pointerType==="touch"?24*24:12*12)){
        if(event.pointerType==="touch")pinned=true;
        nearest.show();
      } else if(!pinned)tooltip.hidden=true;
    };
    svg.onpointermove=inspectPoint;svg.onpointerdown=inspectPoint;
    svg.onpointerleave=event=>{if(event.pointerType!=="touch" && !pinned)tooltip.hidden=true;};
  }
  caption.textContent = `${analytics?"":time(rows[0].timestamp)+" — "+time(rows.at(-1).timestamp)+" · "}${field === "cpcb_aqi" ? "CPCB AQI" : names[field] + " ("+unit(field)+") · "+source(field)} · ${points.length} ${diagnostic ? "raw sensor" : $("view").value} observations. Gaps are not interpolated.${diagnostic ? " Valid estimates: accent color. Invalid estimates: amber/dashed, diagnostic only — not AQI eligible. Hover, focus or tap a point for its exact measurement. The ESP32 view applies to raw samples; daily view applies to covered-hour charts." : ""}`;
}
let loading = false;
async function load() {
  if (loading || page === "/about") {
    if (page === "/about")
      $("status").textContent = "Mandalay only · transparent data provenance";
    return;
  }
  loading = true;
  if(page==="/analytics")$("hardware-window").disabled=true;
  $("status").classList.remove("error");
  $("status").textContent = "Updating Mandalay observations…";
  try {
    const days = $("days")?.value;
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
      $("updated").textContent =
        `${time(r.timestamp)} · hourly rolling calculation · provisional mixed-source estimates`;
      $("pollutants").replaceChildren(
        ...Object.entries(names).map(([k, n]) => {
          const m=r.measurements[k];
          let value=m.value,status=m.status;
          const raw=r.hardware.last_observation;
          // Display the audited estimate only; never feed it into AQI/history.
          if(["carbon_monoxide","ammonia"].includes(k) && m.source==="ESP32" && raw && raw.timestamp===m.timestamp){
            const key=k==="carbon_monoxide"?"co_mg_m3":"nh3_ug_m3";
            if(typeof raw[key]==="number" && Number.isFinite(raw[key])) value=raw[key];
            const valid=raw[k==="carbon_monoxide"?"co_valid":"nh3_valid"];
            status=(valid?"valid estimate":"Invalid — not AQI eligible")+(m.status==="stale"?" · stale":"");
            if(k==="carbon_monoxide" && raw.validity?.co_range_valid===false){
              status="Outside calibrated range — not AQI eligible"+(m.status==="stale"?" · stale":"");
            }
          }
          return card(n,source(k)==="ESP32"?estimate(value):fmt(value),`${m.unit} · ${m.source} · ${status}${m.timestamp?" · "+time(m.timestamp):" · no measurement"}`);
        }),
      );
      $("status").textContent = `Mandalay monitoring · ${r.aqi_label} · ${r.status}${r.model_stale?" · Open-Meteo stale/missing":""}`;
    }
    if (page === "/analytics") {
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
        const windowHours=$("hardware-window").value;
        const hardwareDays=windowHours==="period"?days:Math.ceil(Number(windowHours)/24);
        const hardware = await api(`/api/hardware/history?days=${hardwareDays}`);
        const end=new Date(),start=new Date(end.getTime()-(windowHours==="period"?days*24:Number(windowHours))*3600000);
        const hardwareRows=hardware.data.filter(r=>new Date(r.timestamp)>=start && new Date(r.timestamp)<=end);
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
          if (["carbon_monoxide","ammonia"].includes(field)) {
            const key=field==="carbon_monoxide"?"co_mg_m3":"nh3_ug_m3";
            const flag=field==="carbon_monoxide"?"co_valid":"nh3_valid";
            const raw=hardwareRows.map(r=>({timestamp:r.timestamp,[field]:r[key],sensor_valid:r[flag],
              validity_label:hardwareValidity(r,field),device_id:r.device_id}));
            // Empty boundaries preserve the chosen time window and real gaps.
            raw.unshift({timestamp:start.toISOString(),[field]:null});
            raw.push({timestamp:end.toISOString(),[field]:null});
            chart(raw,svg,field,caption,true,hardware.sample_period_seconds*1500);
          } else chart(h.data, svg, field, caption);
        }
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
        for (const value of [time(r.measurement_timestamp||r.timestamp)+(r.status?' · started '+time(r.timestamp):''),names[r.pollutant]||r.pollutant||'AQI',r.alert_type+' · '+r.severity+(r.status?' · '+r.status:''),fmt(r.value)+' '+r.unit,fmt(r.threshold)+' '+r.unit+(r.averaging_hours?' · '+r.averaging_hours+'h':''),r.source+(r.device_id?' · '+r.device_id:''),r.message+(r.resolved_at?' Resolved: '+time(r.resolved_at):'')]) {
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
    if(page==="/analytics")$("hardware-window").disabled=false;
  }
}
$("hardware-window").onchange = load;
if($("days"))$("days").onchange = () => {
  // Selecting a historical range must not retain a narrower recent zoom.
  if(page==="/analytics")$("hardware-window").value="period";
  load();
};
if($("view"))$("view").onchange = load;
load();
setInterval(load, 300000);
