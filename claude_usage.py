#!/usr/bin/env python3
"""
Claude Code usage analytics — by project, over time.

Reads your local Claude Code transcripts (~/.claude/projects/**/*.jsonl) and
reports, per project:
  - active engaged time (capped-gap method)
  - sessions, message counts, token volume
  - estimated API-equivalent cost
  - estimated labor hours saved (two methods, shown side by side)

Outputs an interactive HTML dashboard (daily / weekly / monthly) and a
terminal summary for the current week.

Everything runs locally. It only reads files under your Claude projects
directory; nothing is uploaded anywhere.

Usage:
    python claude_usage.py                 # build dashboard + print weekly summary
    python claude_usage.py --open          # also open the dashboard in a browser
    python claude_usage.py --out report.html
    python claude_usage.py --config my.json # use a custom config file

Configuration is optional. See claude_usage.config.example.json for the schema;
copy it to claude_usage.config.json (next to this script) to customize project
grouping, the labor-saved factors, idle cap, or model pricing.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import webbrowser
from collections import defaultdict
from datetime import datetime, timedelta, timezone

# ─────────────────────────────────────────────────────────────────────────────
# Defaults. Override any of these via a config file (see --config / load_config).
# ─────────────────────────────────────────────────────────────────────────────

DEFAULTS = {
    # Any gap between two consecutive events longer than this (seconds) counts as
    # "stepped away" and is NOT added to active time. 5 minutes is a sane default.
    "idle_cap_seconds": 5 * 60,

    # Labor saved, method A (time multiplier):
    #   labor_saved_hours = active_hours * time_multiplier
    # "One focused hour driving Claude ~= time_multiplier hours done by hand."
    "time_multiplier": 4.0,

    # Labor saved, method B (output volume):
    #   labor_saved_hours = output_tokens / human_throughput_tokens_per_hour
    # i.e. tokens of finished, reviewed deliverable a person produces per hour.
    # NOTE: Claude emits far more output than the final deliverable (explanations,
    # tool calls, retries), so method B reads high. Treat it as a relative signal,
    # or raise this number (8000-15000) to bring it near method A. See README.
    "human_throughput_tokens_per_hour": 1500.0,

    # When two consecutive events land on different projects, attribute the gap to
    # the earlier event's project. (No knob — documented for transparency.)

    # Path segments that typically *contain* projects. When a session's working
    # directory sits under one of these, the segment immediately after it becomes
    # the project name. Matched case-insensitively. e.g. /home/me/dev/api/src
    # under "dev" -> project "api".
    "container_dirs": [
        "dev", "projects", "project", "repos", "repo", "src", "code", "git",
        "work", "workspace", "sites", "sandbox", "documents", "desktop",
    ],

    # Optional explicit grouping. Each rule is {"match": <substring>, "label": <name>}.
    # First matching rule (case-insensitive substring of the full path) wins, before
    # auto-detection. Use this to merge subfolders or rename projects.
    "project_rules": [],

    # Label used when a project can't be determined.
    "unknown_project": "(unknown)",

    # Model pricing, USD per 1,000,000 tokens. Anthropic list prices (2026-06).
    # Cache-write (5-min TTL) billed at cache_write_mult x input; cache-read at
    # cache_read_mult x input.
    "pricing": {
        "fable":  {"input": 10.0, "output": 50.0},
        "opus":   {"input": 5.0,  "output": 25.0},
        "sonnet": {"input": 3.0,  "output": 15.0},
        "haiku":  {"input": 1.0,  "output": 5.0},
    },
    "default_model_tier": "sonnet",
    "cache_write_mult": 1.25,   # use 2.0 if you run 1-hour-TTL caching
    "cache_read_mult": 0.10,
}

# Populated by load_config() before aggregation runs.
CONFIG = dict(DEFAULTS)


def load_config(path: str | None) -> dict:
    """Merge an optional JSON config file over DEFAULTS. Looks (in order) at the
    --config path, then claude_usage.config.json next to this script, then
    ~/.claude/claude_usage.config.json."""
    cfg = dict(DEFAULTS)
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        path,
        os.path.join(here, "claude_usage.config.json"),
        os.path.expanduser("~/.claude/claude_usage.config.json"),
    ]
    for cand in candidates:
        if cand and os.path.isfile(cand):
            try:
                with open(cand, "r", encoding="utf-8") as fh:
                    user = json.load(fh)
                cfg.update(user)
                cfg["_loaded_from"] = cand
            except (OSError, json.JSONDecodeError) as e:
                print(f"Warning: could not read config {cand}: {e}", file=sys.stderr)
            break
    return cfg


# ─────────────────────────────────────────────────────────────────────────────
# Parsing
# ─────────────────────────────────────────────────────────────────────────────


def model_tier(model: str | None) -> str:
    m = (model or "").lower()
    for tier in CONFIG["pricing"]:
        if tier in m:
            return tier
    return CONFIG["default_model_tier"]


def _path_parts(cwd: str) -> list[str]:
    """Split a Windows or POSIX path into clean segments (drive/empties stripped)."""
    norm = cwd.replace("\\", "/")
    parts = [p for p in norm.split("/") if p]
    # drop a leading drive letter like "C:"
    if parts and len(parts[0]) == 2 and parts[0][1] == ":":
        parts = parts[1:]
    return parts


def project_for(cwd: str | None) -> str:
    if not cwd:
        return CONFIG["unknown_project"]
    low = cwd.lower()
    for rule in CONFIG["project_rules"]:
        needle = str(rule.get("match", "")).lower()
        if needle and needle in low:
            return str(rule.get("label", needle))

    parts = _path_parts(cwd)
    if not parts:
        return CONFIG["unknown_project"]
    containers = {c.lower() for c in CONFIG["container_dirs"]}
    # the segment after the LAST container dir, if any
    chosen = None
    for i, seg in enumerate(parts[:-1]):
        if seg.lower() in containers:
            chosen = parts[i + 1]
    if chosen is None:
        # no recognized container — fall back to the last segment
        chosen = parts[-1]
    return chosen


def parse_ts(ts: str) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def cost_for(tier: str, usage: dict) -> float:
    price = CONFIG["pricing"].get(tier, CONFIG["pricing"][CONFIG["default_model_tier"]])
    inp = price["input"] / 1_000_000.0
    out = price["output"] / 1_000_000.0
    c = 0.0
    c += usage.get("input_tokens", 0) * inp
    c += usage.get("cache_creation_input_tokens", 0) * inp * CONFIG["cache_write_mult"]
    c += usage.get("cache_read_input_tokens", 0) * inp * CONFIG["cache_read_mult"]
    c += usage.get("output_tokens", 0) * out
    return c


def iter_events(base: str):
    """Yield (session_id, project, datetime, usage_tuple_or_None) per event."""
    files = glob.glob(os.path.join(base, "**", "*.jsonl"), recursive=True)
    for path in files:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        o = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    ts = parse_ts(o.get("timestamp"))
                    if ts is None:
                        continue
                    sid = o.get("sessionId") or os.path.basename(path)
                    proj = project_for(o.get("cwd"))
                    usage = None
                    msg = o.get("message")
                    if isinstance(msg, dict):
                        u = msg.get("usage")
                        if isinstance(u, dict):
                            usage = (model_tier(msg.get("model")), u)
                    yield sid, proj, ts, usage
        except (OSError, UnicodeDecodeError):
            continue


def aggregate(base: str):
    """Build per-(date, project) daily records."""
    sessions: dict[str, list] = defaultdict(list)
    daily = defaultdict(lambda: {
        "seconds": 0.0, "out_tokens": 0, "total_tokens": 0,
        "cost": 0.0, "msgs": 0, "sessions": set(),
    })

    for sid, proj, ts, usage in iter_events(base):
        sessions[sid].append((ts, proj, usage))

    if not sessions:
        return [], None, None

    all_dates = []
    cap = CONFIG["idle_cap_seconds"]

    for sid, events in sessions.items():
        events.sort(key=lambda e: e[0])
        for ts, proj, usage in events:
            rec = daily[(ts.date().isoformat(), proj)]
            rec["sessions"].add(sid)
            all_dates.append(ts.date())
            if usage is not None:
                tier, u = usage
                rec["msgs"] += 1
                rec["out_tokens"] += u.get("output_tokens", 0)
                rec["total_tokens"] += (
                    u.get("input_tokens", 0)
                    + u.get("cache_creation_input_tokens", 0)
                    + u.get("cache_read_input_tokens", 0)
                    + u.get("output_tokens", 0)
                )
                rec["cost"] += cost_for(tier, u)
        for (ts0, proj0, _), (ts1, _p, _u) in zip(events, events[1:]):
            gap = (ts1 - ts0).total_seconds()
            if gap <= 0:
                continue
            daily[(ts0.date().isoformat(), proj0)]["seconds"] += min(gap, cap)

    records = []
    for (date_iso, proj), rec in daily.items():
        records.append({
            "date": date_iso,
            "project": proj,
            "minutes": round(rec["seconds"] / 60.0, 2),
            "out_tokens": rec["out_tokens"],
            "total_tokens": rec["total_tokens"],
            "cost": round(rec["cost"], 4),
            "msgs": rec["msgs"],
            "sessions": len(rec["sessions"]),
        })
    records.sort(key=lambda r: (r["date"], r["project"]))
    return records, min(all_dates), max(all_dates)


# ─────────────────────────────────────────────────────────────────────────────
# Terminal summary (current week)
# ─────────────────────────────────────────────────────────────────────────────


def monday_of(d) -> str:
    dt = datetime.fromisoformat(d) if isinstance(d, str) else datetime.combine(d, datetime.min.time())
    return (dt - timedelta(days=dt.weekday())).date().isoformat()


def print_weekly_summary(records, dmin, dmax):
    if not records:
        print("No usage data found.")
        return
    mult = CONFIG["time_multiplier"]
    thru = CONFIG["human_throughput_tokens_per_hour"]
    wk = monday_of(dmax)
    rows = [r for r in records if monday_of(r["date"]) == wk]
    by_proj = defaultdict(lambda: {"minutes": 0.0, "out_tokens": 0, "cost": 0.0, "sessions": 0})
    for r in rows:
        p = by_proj[r["project"]]
        p["minutes"] += r["minutes"]
        p["out_tokens"] += r["out_tokens"]
        p["cost"] += r["cost"]
        p["sessions"] += r["sessions"]

    week_end = (datetime.fromisoformat(wk) + timedelta(days=6)).date().isoformat()
    print()
    print(f"  Claude Code -- week of {wk} -> {week_end}")
    print("  " + "-" * 78)
    print(f"  {'Project':<24}{'Active h':>9}{'Saved A':>9}{'Saved B':>9}{'Sessions':>10}{'Est $':>10}")
    print("  " + "-" * 78)
    tot = {"minutes": 0.0, "out_tokens": 0, "cost": 0.0, "sessions": 0}
    for proj, p in sorted(by_proj.items(), key=lambda kv: -kv[1]["minutes"]):
        h = p["minutes"] / 60.0
        print(f"  {proj[:24]:<24}{h:>9.1f}{h*mult:>9.1f}{p['out_tokens']/thru:>9.1f}"
              f"{p['sessions']:>10}{p['cost']:>10.2f}")
        for k in tot:
            tot[k] += p[k]
    print("  " + "-" * 78)
    H = tot["minutes"] / 60.0
    print(f"  {'TOTAL':<24}{H:>9.1f}{H*mult:>9.1f}{tot['out_tokens']/thru:>9.1f}"
          f"{tot['sessions']:>10}{tot['cost']:>10.2f}")
    print()
    print(f"  Saved A = active time x {mult:g}.   Saved B = output tokens / {thru:g}/h.")
    print(f"  Full breakdown (daily / weekly / monthly) in the HTML dashboard.")
    print()


# ─────────────────────────────────────────────────────────────────────────────
# HTML dashboard
# ─────────────────────────────────────────────────────────────────────────────

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Claude Code Usage</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,400;9..144,500&family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@300;400;500;600&display=swap" rel="stylesheet">
<style>
:root{
  --paper:#f7f3ec; --paper-dark:#efe9dd; --ink:#1a1815; --ink-soft:#46423b;
  --ink-muted:#6b665d; --rule:#d4cdbe; --accent:#c8421f;
}
*{box-sizing:border-box;}
body{margin:0;background:var(--paper);color:var(--ink-soft);
  font-family:"IBM Plex Sans",system-ui,sans-serif;line-height:1.6;}
.wrap{max-width:1152px;margin:0 auto;padding:48px 24px 96px;}
.eyebrow{font-family:"IBM Plex Mono",monospace;font-size:14px;text-transform:uppercase;
  letter-spacing:0.1em;color:var(--accent);margin:0 0 12px;}
h1{font-family:"Fraunces",Georgia,serif;font-weight:500;color:var(--ink);
  font-size:48px;line-height:1.05;margin:0 0 8px;}
.sub{color:var(--ink-muted);font-size:16px;margin:0 0 40px;}
.rule{height:1px;background:var(--rule);position:relative;margin:40px 0;}
.rule::before{content:"";position:absolute;left:0;top:-3px;width:48px;height:7px;background:var(--accent);}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:1px;
  background:var(--rule);border:1px solid var(--rule);}
.kpi{background:var(--paper);padding:20px 22px;}
.kpi .label{font-family:"IBM Plex Mono",monospace;font-size:14px;text-transform:uppercase;
  letter-spacing:0.06em;color:var(--ink-muted);margin:0 0 8px;}
.kpi .val{font-family:"Fraunces",serif;font-size:34px;font-weight:500;color:var(--ink);line-height:1;}
.kpi .unit{font-size:16px;color:var(--ink-muted);margin-left:4px;}
.toolbar{display:flex;align-items:center;gap:16px;flex-wrap:wrap;margin:8px 0 24px;}
.seg{display:inline-flex;border:1px solid var(--ink);}
.seg button{font-family:"IBM Plex Mono",monospace;font-size:14px;text-transform:uppercase;
  letter-spacing:0.08em;background:var(--paper);color:var(--ink);border:0;padding:10px 18px;
  cursor:pointer;min-height:44px;}
.seg button+button{border-left:1px solid var(--ink);}
.seg button.on{background:var(--ink);color:var(--paper);}
.sectitle{font-family:"Fraunces",serif;font-size:24px;font-weight:500;color:var(--ink);margin:40px 0 4px;}
.note{font-size:14px;color:var(--ink-muted);margin:0 0 20px;}
.chartcard{border:1px solid var(--rule);background:var(--paper);padding:24px;}
svg{display:block;width:100%;height:auto;font-family:"IBM Plex Mono",monospace;}
.bar{fill:var(--ink);}
.bar:hover{fill:var(--accent);}
.axis{stroke:var(--rule);stroke-width:1;}
.tick{fill:var(--ink-muted);font-size:11px;}
.barlbl{fill:var(--ink-muted);font-size:11px;}
table{width:100%;border-collapse:collapse;margin-top:8px;font-size:16px;}
th{font-family:"IBM Plex Mono",monospace;font-size:14px;text-transform:uppercase;
  letter-spacing:0.06em;color:var(--ink-muted);text-align:right;padding:12px 14px;
  border-bottom:1px solid var(--ink);white-space:nowrap;}
th:first-child,td:first-child{text-align:left;}
td{padding:12px 14px;border-bottom:1px solid var(--rule);text-align:right;
  font-variant-numeric:tabular-nums;}
tr:last-child td{border-bottom:1px solid var(--ink);}
tbody tr:hover{background:var(--paper-dark);}
td.proj{color:var(--ink);font-weight:500;}
tfoot td{font-weight:600;color:var(--ink);border-bottom:0;}
.method{background:var(--paper-dark);border:1px solid var(--rule);padding:20px 24px;margin-top:40px;}
.method h3{font-family:"IBM Plex Mono",monospace;font-size:14px;text-transform:uppercase;
  letter-spacing:0.08em;color:var(--accent);margin:0 0 12px;}
.method p{font-size:14px;margin:0 0 8px;}
.method code{background:var(--paper);padding:2px 6px;border:1px solid var(--rule);font-size:13px;}
.foot{color:var(--ink-muted);font-size:14px;margin-top:48px;}
.foot a{color:var(--accent);}
</style>
</head>
<body>
<div class="wrap">
  <p class="eyebrow">Claude Code &middot; Usage Report</p>
  <h1>Where the hours went</h1>
  <p class="sub" id="sub"></p>

  <div class="kpis" id="kpis"></div>

  <div class="rule"></div>

  <h2 class="sectitle">Active time over time</h2>
  <p class="note">Engaged minutes per period, idle gaps excluded. Switch granularity below.</p>
  <div class="toolbar">
    <div class="seg" id="seg">
      <button data-g="day">Daily</button>
      <button data-g="week" class="on">Weekly</button>
      <button data-g="month">Monthly</button>
    </div>
  </div>
  <div class="chartcard"><div id="chart"></div></div>

  <h2 class="sectitle">By project</h2>
  <p class="note">Totals across the full date range.</p>
  <table id="ptable">
    <thead><tr>
      <th>Project</th><th>Active h</th><th>Sessions</th><th>Out tokens</th>
      <th>Est $</th><th>Saved A</th><th>Saved B</th>
    </tr></thead>
    <tbody></tbody>
    <tfoot></tfoot>
  </table>

  <div class="method">
    <h3>How these numbers are estimated</h3>
    <p><strong>Active time</strong> — within each session, the time between consecutive
      events is summed, but any gap longer than the idle cap is treated as "away" and
      dropped. This measures engaged time, not wall-clock.</p>
    <p><strong>Saved A (time multiplier)</strong> — <code>active hours &times; __MULT__</code>.
      One focused hour driving Claude &asymp; __MULT__ hours done by hand.</p>
    <p><strong>Saved B (output volume)</strong> — <code>output tokens &divide; __THRU__ /hr</code>.
      Claude emits far more output than the final deliverable, so this reads high — treat it
      as a relative signal, or raise the throughput in config to calibrate it.</p>
    <p><strong>Est $</strong> — API list-price equivalent of the tokens used
      (cache reads at 0.1&times;, cache writes at 1.25&times;). Not your subscription cost —
      a proxy for the raw compute value consumed.</p>
  </div>

  <p class="foot" id="foot"></p>
</div>

<script>
const DATA = __DATA__;
const MULT = __MULT__, THRU = __THRU__;

function mondayOf(iso){
  const d = new Date(iso+"T00:00:00");
  const day = (d.getDay()+6)%7;
  d.setDate(d.getDate()-day);
  return d.toISOString().slice(0,10);
}
function bucketKey(iso, g){
  if(g==="day") return iso;
  if(g==="month") return iso.slice(0,7);
  return mondayOf(iso);
}
function fmt(n, d=0){ return n.toLocaleString(undefined,{minimumFractionDigits:d,maximumFractionDigits:d}); }
function fmtTokens(n){
  if(n>=1e6) return (n/1e6).toFixed(1)+"M";
  if(n>=1e3) return (n/1e3).toFixed(1)+"k";
  return ""+n;
}

function renderTotals(){
  const byProj = {};
  let T={min:0,out:0,cost:0,sess:0,tot:0};
  for(const r of DATA.records){
    const p = byProj[r.project] || (byProj[r.project]={min:0,out:0,cost:0,sess:0});
    p.min+=r.minutes; p.out+=r.out_tokens; p.cost+=r.cost; p.sess+=r.sessions;
    T.min+=r.minutes; T.out+=r.out_tokens; T.cost+=r.cost; T.sess+=r.sessions; T.tot+=r.total_tokens;
  }
  const H=T.min/60;
  const kpis=[
    ["Active time", fmt(H,1), "h"],
    ["Saved &middot; A", fmt(H*MULT,0), "h"],
    ["Saved &middot; B", fmt(T.out/THRU,0), "h"],
    ["Sessions", fmt(T.sess,0), ""],
    ["Tokens", fmtTokens(T.tot), ""],
    ["Est. value", "$"+fmt(T.cost,0), ""],
  ];
  document.getElementById("kpis").innerHTML = kpis.map(k=>
    `<div class="kpi"><p class="label">${k[0]}</p><div class="val">${k[1]}<span class="unit">${k[2]}</span></div></div>`).join("");

  const rows = Object.entries(byProj).sort((a,b)=>b[1].min-a[1].min);
  document.querySelector("#ptable tbody").innerHTML = rows.map(([proj,p])=>{
    const h=p.min/60;
    return `<tr><td class="proj">${proj}</td><td>${fmt(h,1)}</td><td>${fmt(p.sess,0)}</td>`+
      `<td>${fmtTokens(p.out)}</td><td>$${fmt(p.cost,2)}</td>`+
      `<td>${fmt(h*MULT,1)}</td><td>${fmt(p.out/THRU,1)}</td></tr>`;
  }).join("");
  document.querySelector("#ptable tfoot").innerHTML =
    `<tr><td>TOTAL</td><td>${fmt(H,1)}</td><td>${fmt(T.sess,0)}</td>`+
    `<td>${fmtTokens(T.out)}</td><td>$${fmt(T.cost,2)}</td>`+
    `<td>${fmt(H*MULT,1)}</td><td>${fmt(T.out/THRU,1)}</td></tr>`;
}

function renderChart(g){
  const buckets = {};
  for(const r of DATA.records){
    const k = bucketKey(r.date,g);
    buckets[k] = (buckets[k]||0) + r.minutes/60;
  }
  const keys = Object.keys(buckets).sort();
  const vals = keys.map(k=>buckets[k]);
  const W=1100, H=320, padL=44, padB=46, padT=12, padR=8;
  const maxV = Math.max(1, ...vals);
  const n = keys.length || 1;
  const bw = (W-padL-padR)/n;
  const x = i => padL + i*bw;
  const y = v => padT + (1-v/maxV)*(H-padT-padB);
  const barw = Math.max(1, bw*0.7);
  const off = (bw-barw)/2;
  const step = Math.ceil(n/14);
  const lbl = k => (g==="month") ? k : k.slice(5);
  const gridN=4;
  let grid="";
  for(let i=0;i<=gridN;i++){
    const v=maxV*i/gridN, yy=y(v);
    grid += `<line class="axis" x1="${padL}" y1="${yy}" x2="${W-padR}" y2="${yy}"/>`+
            `<text class="tick" x="${padL-6}" y="${yy+3}" text-anchor="end">${v.toFixed(0)}</text>`;
  }
  let bars="";
  keys.forEach((k,i)=>{
    const v=vals[i], h=(H-padT-padB)-(y(v)-padT);
    bars += `<rect class="bar" x="${x(i)+off}" y="${y(v)}" width="${barw}" height="${Math.max(0,h)}">`+
            `<title>${k}: ${v.toFixed(1)} h active</title></rect>`;
    if(i%step===0){
      bars += `<text class="barlbl" x="${x(i)+bw/2}" y="${H-padB+16}" text-anchor="middle">${lbl(k)}</text>`;
    }
  });
  document.getElementById("chart").innerHTML =
    `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet">`+
    `<text class="barlbl" x="${padL-6}" y="${padT-2}" text-anchor="end">h</text>`+
    grid+bars+`</svg>`;
}

document.getElementById("sub").textContent =
  `${DATA.range[0]} -> ${DATA.range[1]} · ${DATA.records.length} project-days · generated ${DATA.generated}`;
document.getElementById("foot").innerHTML =
  `Read from ${DATA.files} local transcript files. Nothing was uploaded. `+
  `Re-run <code>claude_usage.py</code> to refresh.`;
renderTotals();
renderChart("week");
document.querySelectorAll("#seg button").forEach(b=>{
  b.addEventListener("click",()=>{
    document.querySelectorAll("#seg button").forEach(x=>x.classList.remove("on"));
    b.classList.add("on");
    renderChart(b.dataset.g);
  });
});
</script>
</body>
</html>
"""


def build_html(records, dmin, dmax, file_count):
    generated = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")
    data = {
        "records": records,
        "range": [dmin.isoformat() if dmin else "—", dmax.isoformat() if dmax else "—"],
        "generated": generated,
        "files": file_count,
    }
    html = HTML_TEMPLATE
    html = html.replace("__DATA__", json.dumps(data, separators=(",", ":")))
    html = html.replace("__MULT__", f"{CONFIG['time_multiplier']:g}")
    html = html.replace("__THRU__", f"{CONFIG['human_throughput_tokens_per_hour']:g}")
    return html


# ─────────────────────────────────────────────────────────────────────────────
# main
# ─────────────────────────────────────────────────────────────────────────────


def main():
    ap = argparse.ArgumentParser(description="Claude Code usage analytics by project.")
    ap.add_argument("--base", default=os.path.expanduser("~/.claude/projects"),
                    help="Transcripts root (default: ~/.claude/projects)")
    ap.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "usage_dashboard.html"),
                    help="Output HTML path")
    ap.add_argument("--config", default=None, help="Path to a JSON config file")
    ap.add_argument("--open", action="store_true", help="Open the dashboard after building")
    args = ap.parse_args()

    global CONFIG
    CONFIG = load_config(args.config)

    if not os.path.isdir(args.base):
        print(f"Transcripts directory not found: {args.base}", file=sys.stderr)
        print("Is Claude Code installed? Expected ~/.claude/projects.", file=sys.stderr)
        sys.exit(1)

    file_count = len(glob.glob(os.path.join(args.base, "**", "*.jsonl"), recursive=True))
    records, dmin, dmax = aggregate(args.base)
    if not records:
        print("No usage events found under " + args.base, file=sys.stderr)
        sys.exit(1)

    html = build_html(records, dmin, dmax, file_count)
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(html)

    print_weekly_summary(records, dmin, dmax)
    if CONFIG.get("_loaded_from"):
        print(f"  Config: {CONFIG['_loaded_from']}")
    print(f"  Dashboard written: {args.out}")
    print()

    if args.open:
        webbrowser.open("file:///" + os.path.abspath(args.out).replace("\\", "/"))


if __name__ == "__main__":
    main()
