#!/usr/bin/env python3
"""Operations Hub. Four folders only: 01-app, 02-database, 03-videos, 04-feeds.

    python3 01-app/app.py
    then open http://127.0.0.1:8080
"""
from __future__ import annotations

import json
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

APP_DIR = Path(__file__).resolve().parent
ROOT = APP_DIR.parent
DB_PATH = ROOT / "02-database" / "itops.db"
VIDEOS = ROOT / "03-videos"
FEEDS = ROOT / "04-feeds"
HOST, PORT = "127.0.0.1", 8080


def ensure_db():
    if DB_PATH.exists():
        return
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    parts = sorted(DB_PATH.parent.glob("itops*.sql"))
    if not parts:
        raise FileNotFoundError(f"Missing {DB_PATH} and no itops.sql dump")
    script = "\n".join(p.read_text() for p in parts)
    con = sqlite3.connect(DB_PATH)
    try:
        con.executescript(script)
        con.commit()
    finally:
        con.close()


def connect():
    ensure_db()
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def rows(sql, params=()):
    con = connect()
    try:
        return [dict(r) for r in con.execute(sql, params)]
    finally:
        con.close()


def one(sql, params=()):
    r = rows(sql, params)
    return r[0] if r else None


def api_overview():
    open_n = one("select count(*) as c from incidents where status in ('open','investigating')")["c"]
    p1 = one("select count(*) as c from incidents where status in ('open','investigating') and severity='P1'")["c"]
    mtta = one("select round(avg(mtta_minutes),1) as a from incidents")["a"]
    snap = one("select risk_score, health_score, next_high_risk_days from sla_snapshots order by snapshot_at desc limit 1") or {}
    recs = one("select count(*) as c from incidents where status in ('open','investigating') and severity in ('P1','P2')")["c"]
    deg = one("select count(*) as c from services where status='degraded'")["c"]
    return {
        "openIncidents": open_n,
        "p1Open": p1,
        "avgMtta": mtta or 0,
        "riskScore": snap.get("risk_score", 32),
        "healthScore": snap.get("health_score", 87),
        "nextHighRiskDays": snap.get("next_high_risk_days", 14),
        "aiRecommendations": recs,
        "degraded": deg,
        "recent": rows("select incident_number as id, title, severity, opened_at as ts, service_name from incidents order by opened_at desc limit 8"),
        "services": rows("select name, status, type, criticality, owner_team from services order by criticality desc, name"),
        "insight": "High likelihood of SLA breach on the current payment P1 if webhook retries stay exhausted. Open Video RCA and the payment runbook.",
    }


def api_sla():
    hours = rows("""select hour_of_day, round(avg(incident_count),2) as avg_volume,
                    round(avg(avg_response_min),2) as avg_response
                    from hourly_incident_patterns group by hour_of_day order by hour_of_day""")
    snap = one("""select risk_score, health_score, next_high_risk_days, notes, open_p1, open_p2, avg_mtta_today
                  from sla_snapshots order by snapshot_at desc limit 1""")
    series = rows("select snapshot_at, risk_score, health_score, open_p1, open_p2 from sla_snapshots order by snapshot_at desc limit 30")
    breached = one("select count(*) as c from incidents where sla_breached=1")["c"]
    return {"hours": hours, "snapshot": snap, "series": list(reversed(series)), "breached": breached}


def api_roi():
    t = one("""select round(sum(cost_usd),2) as total,
                      round(sum(case when avoidable then cost_usd else 0 end),2) as avoidable from incidents""") or {}
    drivers = rows("""select root_cause_class as class, count(*) as count,
                      round(sum(cost_usd),2) as total_cost,
                      round(100.0*sum(case when avoidable then 1 else 0 end)/count(*),1) as pct_avoidable,
                      round(avg(cost_usd),2) as avg_cost
                      from incidents group by root_cause_class order by total_cost desc limit 8""")
    by_sev = rows("""select severity, count(*) as count, round(sum(cost_usd),2) as total_cost,
                     round(avg(mtta_minutes),1) as avg_mtta, round(avg(mttr_minutes),1) as avg_mttr
                     from incidents group by severity order by severity""")
    total, avoidable = t.get("total") or 0, t.get("avoidable") or 0
    recs = []
    for d in drivers[:3]:
        impl = round((d["total_cost"] or 0) * 0.08)
        savings = round((d["total_cost"] or 0) * ((d["pct_avoidable"] or 0) / 100) * 0.7)
        recs.append({"action": f"Address {d['class']}", "class": d["class"], "implCost": impl, "savings": savings,
                     "roi": round(savings / impl, 1) if impl else 0})
    impl_sum = sum(r["implCost"] for r in recs) or 1
    projected = round(avoidable * 0.65, 2)
    return {"total": total, "avoidable": avoidable, "avoidablePct": round(100 * avoidable / total, 1) if total else 0,
            "projected": projected, "blendedRoi": round(projected / impl_sum, 1), "drivers": drivers, "bySev": by_sev, "recs": recs}


def _inc_where(qs):
    where, params = [], []
    if qs.get("sev"):
        where.append("severity=?"); params.append(qs["sev"][0])
    if qs.get("status"):
        where.append("status=?"); params.append(qs["status"][0])
    if qs.get("service"):
        where.append("service_name=?"); params.append(qs["service"][0])
    if qs.get("cause"):
        where.append("root_cause_class=?"); params.append(qs["cause"][0])
    if qs.get("open", [""])[0] == "1":
        where.append("status in ('open','investigating')")
    if qs.get("hour"):
        where.append("cast(strftime('%H', replace(opened_at,'T',' ')) as integer)=?"); params.append(int(qs["hour"][0]))
    if qs.get("q"):
        q = f"%{qs['q'][0]}%"
        where.append("(incident_number like ? or title like ? or service_name like ? or root_cause_class like ?)")
        params.extend([q, q, q, q])
    clause = (" where " + " and ".join(where)) if where else ""
    return clause, params


def api_incidents(qs):
    clause, params = _inc_where(qs)
    items = rows(f"""select incident_number, title, severity, status, service_name, opened_at,
                     mtta_minutes, mttr_minutes, cost_usd, root_cause_class, sla_breached, avoidable, impact_summary
                     from incidents{clause} order by opened_at desc limit 200""", params)
    return {"items": items}


def api_incident(iid):
    incident = one("""select incident_number, title, description, severity, status, service_name,
                      opened_at, acknowledged_at, resolved_at, mtta_minutes, mttr_minutes, cost_usd,
                      root_cause_class, impact_summary, sla_breached, avoidable
                      from incidents where incident_number=?""", (iid,))
    svc = (incident or {}).get("service_name") or ""
    tickets = rows("""select ticket_number, source, type, subject, status, priority, assignee
                      from tickets where related_incident_number=? or subject like ?
                      order by created_at desc limit 12""", (iid, f"%{(svc.split() or ['x'])[0]}%"))
    logs = rows("""select log_id, level, message, timestamp, host, upgrade_related
                   from pca_logs where service_name=? order by timestamp desc limit 20""", (svc,))
    videos = rows("""select capture_id, title, video_url, rca_summary, confidence, duration_sec
                     from video_captures where incident_number=? or service_name=? order by captured_at desc limit 6""", (iid, svc))
    anns = []
    if videos:
        ph = ",".join("?" * len(videos))
        anns = rows(f"select capture_id, timestamp_sec, kind, label, detail from rca_annotations where capture_id in ({ph}) order by timestamp_sec",
                    [v["capture_id"] for v in videos])
    books = rows("select id, name, service_name, version, steps_json, success_rate from runbooks")
    runbook = next((b for b in books if b["service_name"] == svc), books[0] if books else None)
    if runbook:
        try:
            runbook = {**runbook, "steps": json.loads(runbook.get("steps_json") or "[]")}
        except json.JSONDecodeError:
            runbook = {**runbook, "steps": []}
    execs = rows("""select runbook_id, started_at, status, steps_completed, total_steps, executed_by
                    from runbook_executions where incident_number=? order by started_at desc limit 8""", (iid,))
    return {"incident": incident, "tickets": tickets, "logs": logs, "videos": videos, "anns": anns, "runbook": runbook, "execs": execs}


def api_tickets(qs):
    where, params = [], []
    if qs.get("source"):
        where.append("source=?"); params.append(qs["source"][0])
    if qs.get("incident"):
        where.append("related_incident_number=?"); params.append(qs["incident"][0])
    clause = (" where " + " and ".join(where)) if where else ""
    return {"items": rows(f"""select ticket_number, source, type, subject, status, priority, assignee, created_at, related_incident_number
                              from tickets{clause} order by created_at desc limit 120""", params)}


def api_logs(qs):
    where, params = [], []
    if qs.get("service"):
        where.append("service_name=?"); params.append(qs["service"][0])
    if qs.get("level"):
        where.append("level=?"); params.append(qs["level"][0])
    if qs.get("pcai", [""])[0] == "1":
        where.append("upgrade_related=1")
    clause = (" where " + " and ".join(where)) if where else ""
    return {"items": rows(f"""select log_id, service_name, level, message, timestamp, upgrade_related, host
                              from pca_logs{clause} order by timestamp desc limit 120""", params)}


def api_runbooks():
    books = rows("select id, name, service_name, version, steps_json, success_rate, avg_duration_min from runbooks order by id")
    for b in books:
        try:
            b["steps"] = json.loads(b.get("steps_json") or "[]")
        except json.JSONDecodeError:
            b["steps"] = []
    execs = rows("""select runbook_id, incident_number, started_at, status, steps_completed, total_steps, executed_by
                    from runbook_executions order by started_at desc limit 80""")
    open_p1 = rows("""select incident_number, title, service_name, opened_at, mtta_minutes, status, severity
                      from incidents where severity='P1' and status in ('open','investigating') order by opened_at desc limit 5""")
    return {"books": books, "execs": execs, "featured": open_p1[0] if open_p1 else None, "openP1": open_p1}


def api_rca():
    videos = rows("""select capture_id, incident_number, title, service_name, video_url, duration_sec,
                     captured_at, rca_summary, confidence from video_captures order by captured_at desc limit 80""")
    anns = rows("select capture_id, timestamp_sec, kind, label, detail from rca_annotations order by timestamp_sec")
    return {"videos": videos, "anns": anns}


def api_telemetry():
    items = rows("""select service_name, metric_name, timestamp, value, unit, anomaly
                    from telemetry_metrics order by timestamp desc limit 200""")
    by_service = rows("""select service_name, sum(case when anomaly then 1 else 0 end) as anomalies, count(*) as points
                         from telemetry_metrics group by service_name order by anomalies desc""")
    return {"items": items, "byService": by_service}


def api_counts():
    tables = ["services","incidents","alerts","tickets","runbooks","runbook_executions","hourly_incident_patterns",
              "telemetry_metrics","pca_logs","cost_events","sla_snapshots","video_captures","rca_annotations"]
    con = connect()
    try:
        return {t: con.execute(f"select count(*) as c from {t}").fetchone()["c"] for t in tables}
    finally:
        con.close()


MIME = {".mp4": "video/mp4", ".json": "application/json", ".jsonl": "application/json",
        ".log": "text/plain; charset=utf-8", ".txt": "text/plain; charset=utf-8", ".sql": "text/plain; charset=utf-8",
        ".svg": "image/svg+xml"}

PAGE = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Operations Hub</title>
<style>
:root{--bg:#020617;--panel:#0f172a;--raised:#1e293b;--line:#334155;--txt:#e2e8f0;--mut:#94a3b8;--ok:#01A982;--warn:#fbbf24;--bad:#fb7185;--pri:#01A982;--hpe:#01A982}
*{box-sizing:border-box}body{margin:0;font:14px/1.45 DM Sans,ui-sans-serif,system-ui,sans-serif;background:var(--bg);color:var(--txt)}
a{color:inherit;text-decoration:none}.layout{display:grid;grid-template-columns:220px 1fr;min-height:100vh}
nav{border-right:1px solid #d1d5db;padding:16px 10px;background:#fff;display:flex;flex-direction:column}
nav h1{font-size:14px;margin:8px 0 14px;color:#007a5e}
nav .logo{display:block;height:40px;width:auto;max-width:180px;object-fit:contain;object-position:left}
nav a{display:block;padding:9px 10px;border-radius:8px;color:#01A982;min-height:40px;background:#fff;transition:background .16s,color .16s}
nav a:hover{background:#01A982;color:#fff}
nav a.active{background:#e6f7f2;color:#007a5e;font-weight:600}
nav a.active:hover{background:#01A982;color:#fff}
nav .copy{margin-top:auto;padding:12px 8px 4px;color:#6b7280;font-size:10px}
main{padding:22px 26px 48px;overflow:auto}h2{margin:0 0 4px;font-size:20px}.sub{color:var(--mut);margin-bottom:16px;font-size:12px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin-bottom:16px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px}
.kpi .l{color:var(--mut);font-size:12px}.kpi .v{font-size:24px;font-weight:650;margin-top:4px;font-variant-numeric:tabular-nums}
.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}.pri{color:var(--pri)}
table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:8px 6px;border-bottom:1px solid var(--line);font-size:13px;vertical-align:top}
th{color:var(--mut);font-weight:600}.grid2{display:grid;grid-template-columns:2fr 1fr;gap:12px}
.svc{display:flex;justify-content:space-between;padding:8px 10px;border:1px solid var(--line);border-radius:8px;margin-bottom:8px}
.barwrap{display:flex;align-items:flex-end;gap:3px;height:140px}.bar{flex:1;background:#312e81;border-radius:4px 4px 0 0;min-height:4px;cursor:pointer}
video{width:100%;max-height:280px;background:#000;border-radius:8px}.muted{color:var(--mut)}.err{color:var(--bad)}
.pill{display:inline-flex;border-radius:999px;padding:2px 8px;font-size:10px;font-family:ui-monospace,monospace}
.p1{background:rgba(251,113,133,.15);color:var(--bad)}.p2{background:rgba(251,191,36,.15);color:var(--warn)}.px{background:rgba(129,140,248,.15);color:var(--pri)}
.chip{display:inline-block;background:rgba(129,140,248,.15);color:var(--pri);border-radius:999px;padding:4px 8px;font-size:11px;margin:0 6px 8px 0}
button,a.btn{cursor:pointer;border:1px solid var(--line);background:var(--raised);color:var(--txt);border-radius:8px;padding:8px 12px;min-height:40px}
a.btn.pri{background:#01A982;border-color:#01A982}
@media(max-width:800px){.layout{grid-template-columns:1fr}nav{display:flex;gap:6px;flex-wrap:wrap}.grid2{grid-template-columns:1fr}}
</style></head><body>
<div class="layout">
<nav>
  <img class="logo" src="/brand/hpe-logo.svg" alt="Hewlett Packard Enterprise"/>
  <h1>Operations Hub<br><span class="muted" style="font-weight:400;font-size:11px;color:#6b7280">24×7 command center</span></h1>
  <a href="#/" data-r="/">Overview</a>
  <a href="#/sla" data-r="/sla">SLA dashboard</a>
  <a href="#/metrics" data-r="/metrics">Metrics deep dive</a>
  <a href="#/risk" data-r="/risk">Risk mitigation</a>
  <a href="#/roi" data-r="/roi">ROI analysis</a>
  <a href="#/runbook" data-r="/runbook">Runbook investigation</a>
  <a href="#/rca" data-r="/rca">Video RCA</a>
  <a href="#/tickets" data-r="/tickets">Salesforce tickets</a>
  <a href="#/logs" data-r="/logs">PCAI logs</a>
  <a href="#/incidents?open=1" data-r="/incidents">Incident explorer</a>
  <div class="copy">
    <div style="display:flex;gap:8px;align-items:center;margin-bottom:10px">
      <div style="width:28px;height:28px;border-radius:50%;background:#e6f7f2;color:#01A982;display:flex;align-items:center;justify-content:center;font-size:11px">DS</div>
      <div><div style="font-size:12px;color:#007a5e">DS & SA</div><div style="font-size:10px;color:#6b7280">Keycloak ready</div></div>
    </div>
    All copyright reserved 2026
  </div>
</nav>
<main id="app"><p class="muted">Loading…</p></main>
</div>
<div style="position:fixed;bottom:8px;right:16px;color:#94a3b8;font-size:10px">Hewlett Packard Enterprise · All copyright reserved 2026</div>
</div>
<script>
const app=document.getElementById("app");
const money=n=>"$"+Number(n||0).toLocaleString(undefined,{maximumFractionDigits:0});
const when=s=>(s||"").replace("T"," ").slice(0,16);
async function get(path){const r=await fetch(path); if(!r.ok) throw new Error(path+" "+r.status); return r.json();}
function parseHash(){
  const raw=(location.hash.replace(/^#/,"")||"/");
  const [path,qs]=raw.split("?");
  const p=new URLSearchParams(qs||"");
  const o={}; for(const [k,v] of p) o[k]=v;
  return {path: path||"/", q:o, qs: p.toString()};
}
function href(path, q={}){const p=new URLSearchParams(q); const s=p.toString(); return "#"+path+(s?"?"+s:"");}
function kpi(label,value,cls,to){return `<a class="card kpi" href="${to}"><div class="l">${label}</div><div class="v ${cls}">${value}</div><div class="muted" style="font-size:10px;margin-top:6px">DRILL THROUGH</div></a>`;}
function sev(v){const c=v==="P1"||v==="CRITICAL"||v==="ERROR"||v==="Critical"?"p1":(v==="P2"||v==="WARN"||v==="High"?"p2":"px"); return `<span class="pill ${c}">${v}</span>`;}
function table(headers,rows){return `<div class="card" style="overflow:auto"><table><thead><tr>${headers.map(h=>`<th>${h}</th>`).join("")}</tr></thead><tbody>${rows.map(r=>`<tr>${r.map(c=>`<td>${c??""}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`;}
function incLink(id){return `<a class="pri" href="${href("/incident/"+id)}">${id}</a>`;}
function svcLink(n){return `<a href="${href("/incidents",{service:n})}">${n}</a>`;}

const routes={
  "/": async()=>{
    const d=await get("/api/overview");
    return `<h2>Operations overview</h2><div class="sub">Live command center · click any KPI, node, or incident</div>
      <div class="kpis">
        ${kpi("Open incidents",d.openIncidents,"bad",href("/incidents",{open:"1"}))}
        ${kpi("Avg MTTA",d.avgMtta+"m","ok",href("/sla"))}
        ${kpi("SLA risk score",d.riskScore+"/100","warn",href("/risk"))}
        ${kpi("AI recommendations",d.aiRecommendations,"pri",href("/roi"))}
      </div>
      <div class="grid2">
        <div class="card"><div style="margin-bottom:8px">Services</div>
          ${d.services.map(s=>`<a class="svc" href="${href("/incidents",{service:s.name})}"><div>${s.name}<div class="muted">${s.type} · ${s.criticality} · ${s.owner_team}</div></div><span class="${s.status==="degraded"?"bad":"ok"}">${s.status}</span></a>`).join("")}
        </div>
        <div class="card"><div style="margin-bottom:8px">Live activity</div>
          ${d.recent.map(r=>`<div style="margin-bottom:10px">${incLink(r.id)} ${sev(r.severity)}<div class="muted">${r.title}<br>${r.service_name}</div></div>`).join("")}
        </div>
      </div>
      <div class="card" style="margin-top:12px;border-color:rgba(129,140,248,.35)">
        <div class="pri" style="margin-bottom:6px">AI insights (Grok)</div>
        <p>${d.insight}</p>
        <a class="btn pri" href="${href("/rca")}">View full RCA</a>
        <a class="btn" href="${href("/runbook")}">Open runbook</a>
      </div>`;
  },
  "/incidents": async({q})=>{
    const p=new URLSearchParams(q).toString();
    const d=await get("/api/incidents"+(p?"?"+p:""));
    const chips=Object.entries(q).map(([k,v])=>`<span class="chip">${k}: ${v}</span>`).join("") + (Object.keys(q).length?`<a href="${href("/incidents")}">Clear</a>`:"");
    return `<h2>Incident explorer</h2><div class="sub">${d.items.length} matching rows · click a number</div>${chips}`+
      table(["Incident","Sev","Status","Service","Cause","Cost","Opened"],
        d.items.map(r=>[incLink(r.incident_number)+`<div class="muted">${r.title}</div>`,sev(r.severity),r.status,svcLink(r.service_name),
          `<a href="${href("/incidents",{cause:r.root_cause_class})}">${r.root_cause_class}</a>`,money(r.cost_usd),when(r.opened_at)]));
  },
  "/incident": async({path})=>{
    const id=decodeURIComponent(path.replace("/incident/",""));
    const d=await get("/api/incident/"+encodeURIComponent(id));
    const i=d.incident; if(!i) return `<p class="err">No row for ${id}</p>`;
    const v=d.videos[0];
    return `<h2>${i.incident_number}</h2><div class="sub">${sev(i.severity)} ${i.status} · ${i.service_name}</div>
      <div class="grid2">
        <div class="card"><h3 style="margin:0 0 8px">${i.title}</h3><p class="muted">${i.description||""}</p><p>${i.impact_summary||""}</p>
          <div>Cause: <a href="${href("/incidents",{cause:i.root_cause_class})}">${i.root_cause_class}</a> · Cost ${money(i.cost_usd)} · MTTA ${i.mtta_minutes}m</div>
          <div style="margin-top:10px">
            <a class="btn pri" href="${href("/rca")}">Video RCA</a>
            <a class="btn" href="${href("/runbook")}">Runbook</a>
            <a class="btn" href="${href("/tickets",{incident:i.incident_number})}">Tickets</a>
            <a class="btn" href="${href("/logs",{service:i.service_name})}">Logs</a>
          </div>
        </div>
        <div class="card"><div>Runbook · ${d.runbook?.name||"—"}</div>
          <ol>${(d.runbook?.steps||[]).map(s=>`<li>${s}</li>`).join("")}</ol>
        </div>
      </div>
      ${v?`<div class="card" style="margin-top:12px"><video controls src="${v.video_url.replace("/rca/","/rca/")}"></video><p class="muted">${v.rca_summary}</p></div>`:""}
      <h3>Tickets</h3>${table(["Ticket","Pri","Status"], d.tickets.map(t=>[t.ticket_number,sev(t.priority),t.status]))}
      <h3>Logs</h3>${table(["Time","Level","Message"], d.logs.map(l=>[when(l.timestamp),sev(l.level),l.message]))}`;
  },
  "/sla": async()=>{
    const d=await get("/api/sla"); const max=Math.max(...d.hours.map(h=>h.avg_volume),1);
    return `<h2>SLA dashboard</h2><div class="sub">Click a bar → incidents opened in that hour</div>
      <div class="kpis">${kpi("Risk", (d.snapshot?.risk_score??"—")+"/100","warn",href("/risk"))}
        ${kpi("Health",(d.snapshot?.health_score??"—")+"/100","ok",href("/metrics"))}
        ${kpi("Breached", d.breached,"bad",href("/incidents",{open:"1"}))}</div>
      <div class="card"><div class="muted">Avg volume by hour</div>
        <div class="barwrap">${d.hours.map(h=>`<a class="bar" title="${h.hour_of_day}:00" href="${href("/incidents",{hour:h.hour_of_day})}" style="height:${Math.round(8+120*h.avg_volume/max)}px"></a>`).join("")}</div>
      </div>`;
  },
  "/roi": async()=>{
    const d=await get("/api/roi");
    return `<h2>ROI analysis</h2><div class="sub">Click a driver to open matching incidents</div>
      <div class="kpis">${kpi("Total",money(d.total),"bad",href("/incidents"))}
        ${kpi("Avoidable",money(d.avoidable)+" ("+d.avoidablePct+"%)","warn",href("/incidents"))}
        ${kpi("90d savings",money(d.projected),"ok",href("/risk"))}
        ${kpi("Blended ROI",d.blendedRoi+"x","pri",href("/runbook"))}</div>
      <h3>Recommendations</h3>${table(["Action","Impl","Savings","ROI"], d.recs.map(r=>[`<a href="${href("/incidents",{cause:r.class})}">${r.action}</a>`,money(r.implCost),money(r.savings),r.roi+"x"]))}
      <h3>Drivers</h3>${table(["Class","Count","Cost","Avoidable %"], d.drivers.map(x=>[`<a href="${href("/incidents",{cause:x.class})}">${x.class}</a>`,x.count,money(x.total_cost),x.pct_avoidable+"%"]))}`;
  },
  "/risk": async()=>{
    const d=await get("/api/sla");
    return `<h2>Risk mitigation</h2><div class="sub">SLA risk vs health</div>
      <div class="kpis">${kpi("Risk now",(d.snapshot?.risk_score??"—")+"/100","warn",href("/sla"))}
        ${kpi("Health",(d.snapshot?.health_score??"—")+"/100","ok",href("/metrics"))}</div>
      <div class="card">${d.series.map(s=>`<span title="${s.snapshot_at} risk ${s.risk_score}" style="display:inline-block;width:8px;height:${Math.max(4,s.risk_score)}px;background:#fbbf24;margin-right:2px;vertical-align:bottom"></span>`).join("")}</div>
      <div style="margin-top:12px"><a class="btn" href="${href("/incidents",{sev:"P1",open:"1"})}">Clear open P1s</a>
      <a class="btn" href="${href("/roi")}">Fund avoidable cost</a>
      <a class="btn" href="${href("/runbook")}">Rehearse payment playbook</a></div>`;
  },
  "/metrics": async()=>{
    const d=await get("/api/telemetry");
    return `<h2>Metrics deep dive</h2><div class="sub">Click a service</div>
      <div class="kpis">${d.byService.map(s=>`<a class="card" href="${href("/incidents",{service:s.service_name})}"><div>${s.service_name}</div><div class="${s.anomalies?"bad":"ok"}">${s.anomalies} anomalies</div></a>`).join("")}</div>`+
      table(["Service","Metric","Value","When","Flag"], d.items.slice(0,80).map(i=>[svcLink(i.service_name),i.metric_name,i.value+" "+i.unit,when(i.timestamp),i.anomaly?"anomaly":"ok"]));
  },
  "/runbook": async()=>{
    const d=await get("/api/runbooks");
    return `<h2>Runbook investigation</h2>
      <div class="kpis">${(d.openP1||[]).map(p=>`<div class="card">${sev(p.severity)} ${incLink(p.incident_number)}<div>${p.title}</div><div class="muted">${p.service_name}</div></div>`).join("")}</div>
      ${(d.books||[]).map(b=>`<div class="card" style="margin-bottom:10px"><b>${b.name}</b> <span class="muted">${b.service_name} ${b.version} · success ${((b.success_rate<=1?b.success_rate*100:b.success_rate).toFixed(0))}%</span>
        <ol>${(b.steps||[]).map(s=>`<li>${s}</li>`).join("")}</ol>
        <a class="btn" href="${href("/incidents",{service:b.service_name})}">Incidents</a>
        <a class="btn" href="${href("/rca")}">RCA</a></div>`).join("")}
      <h3>Recent executions</h3>${table(["Incident","Status","Steps","By"], (d.execs||[]).slice(0,25).map(e=>[e.incident_number?incLink(e.incident_number):"adhoc",e.status,e.steps_completed+"/"+e.total_steps,e.executed_by]))}`;
  },
  "/rca": async()=>{
    const d=await get("/api/rca");
    const files=["payment-outage.mp4","dynamodb-latency.mp4","cache-stampede.mp4","checkout-cascade.mp4"];
    const v=d.videos.find(x=>x.video_url.includes("payment"))||d.videos[0];
    const anns=d.anns.filter(a=>a.capture_id===v?.capture_id);
    window.__rcaAnns=anns;
    return `<h2>Video RCA</h2><div class="sub">Click a timestamp to scrub · then jump to the incident</div>
      <div class="grid2">
        <div class="card"><video id="rcaPlayer" controls src="/rca/${(v?.video_url||"").split("/").pop()}"></video>
          <div>${v?.title||""} · ${v?Math.round(v.confidence*100):0}% confidence</div>
          <p class="muted">${v?.rca_summary||""}</p>
          ${v?.incident_number?incLink(v.incident_number):""}
        </div>
        <div class="card"><div>Timeline</div>${anns.map(a=>`<button style="display:block;width:100%;text-align:left;margin:6px 0" onclick="document.getElementById('rcaPlayer').currentTime=${a.timestamp_sec};document.getElementById('rcaPlayer').play()">${a.timestamp_sec}s ${sev(a.kind)} ${a.label}</button>`).join("")}</div>
      </div>
      <div class="kpis" style="margin-top:12px">${files.map(f=>`<div class="card"><video muted src="/rca/${f}"></video><div>${f}</div></div>`).join("")}</div>`;
  },
  "/tickets": async({q})=>{
    const p=new URLSearchParams(q).toString();
    const d=await get("/api/tickets"+(p?"?"+p:""));
    return `<h2>Salesforce tickets</h2><div class="sub"><a href="${href("/tickets",{source:"Salesforce"})}">Salesforce</a> · <a href="${href("/tickets",{source:"Jira"})}">Jira</a></div>`+
      table(["Ticket","Src","Type","Pri","Status","Incident","Subject"], d.items.map(t=>[t.ticket_number,t.source,t.type,sev(t.priority),t.status,t.related_incident_number?incLink(t.related_incident_number):"—",t.subject]));
  },
  "/logs": async({q})=>{
    const p=new URLSearchParams(q).toString();
    const d=await get("/api/logs"+(p?"?"+p:""));
    return `<h2>PCAI logs</h2><div class="sub"><a href="${href("/logs",{pcai:"1"})}">PCAI only</a> · <a href="${href("/logs",{level:"ERROR"})}">ERROR</a></div>`+
      table(["Time","Level","Service","Message"], d.items.map(l=>[when(l.timestamp),sev(l.level),svcLink(l.service_name),l.message]));
  },
};
async function render(){
  const {path,q}=parseHash();
  document.querySelectorAll("nav a").forEach(a=>{
    const r=a.getAttribute("data-r");
    a.classList.toggle("active", r==="/" ? path==="/" : path.startsWith(r));
  });
  const key=path.startsWith("/incident/")?"/incident":path;
  try{ app.innerHTML=await (routes[key]||routes["/"])({path,q}); }
  catch(e){ app.innerHTML=`<p class="err">${e.message}<br>Keep python3 app.py running.</p>`; }
}
window.addEventListener("hashchange", render); render();
</script></body></html>
"""


def safe_file(base: Path, rel: str):
    if not rel or ".." in rel.split("/"):
        return None
    path = (base / rel).resolve()
    try:
        path.relative_to(base.resolve())
    except ValueError:
        return None
    return path if path.is_file() else None


class Handler(BaseHTTPRequestHandler):
    server_version = "OperationsHub/2.0"

    def log_message(self, fmt, *args):
        print(f"{self.address_string()} {fmt % args}")

    def _send(self, code, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        path, qs = parsed.path, parse_qs(parsed.query)
        if path in ("/", "/index.html"):
            self._send(200, PAGE.encode(), "text/html; charset=utf-8")
            return
        try:
            if path == "/api/overview":
                payload = api_overview()
            elif path == "/api/sla":
                payload = api_sla()
            elif path == "/api/roi":
                payload = api_roi()
            elif path == "/api/incidents":
                payload = api_incidents(qs)
            elif path.startswith("/api/incident/"):
                payload = api_incident(unquote(path.split("/api/incident/", 1)[1]))
            elif path == "/api/tickets":
                payload = api_tickets(qs)
            elif path == "/api/logs":
                payload = api_logs(qs)
            elif path == "/api/runbooks":
                payload = api_runbooks()
            elif path == "/api/rca":
                payload = api_rca()
            elif path == "/api/telemetry":
                payload = api_telemetry()
            elif path == "/api/counts":
                payload = api_counts()
            elif path == "/api/health":
                payload = {"ok": True, "db": DB_PATH.name}
            else:
                payload = None
            if payload is not None:
                self._send(200, json.dumps(payload, default=str).encode(), "application/json; charset=utf-8")
                return
        except Exception as exc:
            self._send(500, json.dumps({"error": str(exc)}).encode(), "application/json; charset=utf-8")
            return
        file_path = None
        if path.startswith("/rca/"):
            file_path = safe_file(VIDEOS, path[len("/rca/"):])
        elif path.startswith("/feeds/"):
            file_path = safe_file(FEEDS, path[len("/feeds/"):])
        elif path.startswith("/brand/"):
            file_path = safe_file(APP_DIR, path[len("/brand/"):])
        if file_path:
            self._send(200, file_path.read_bytes(), MIME.get(file_path.suffix.lower(), "application/octet-stream"))
            return
        self._send(404, b'{"error":"not found"}', "application/json")


def main():
    ensure_db()
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    print("Operations Hub")
    print("  DB :", DB_PATH)
    print("  URL: http://127.0.0.1:8080")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped")
        httpd.server_close()


if __name__ == "__main__":
    main()
