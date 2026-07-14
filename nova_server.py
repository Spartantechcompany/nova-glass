#!/usr/bin/env python3
"""NOVA Glass — hub server.

Single pane of glass for an AI factory:
  * persistent metric history (SQLite WAL — survives browser reloads & reboots)
  * anomaly detection (rolling z-score) + threshold alerting (runtime-tunable)
  * webhook notifications (generic JSON / Telegram)
  * NOC device monitor (ping / tcp / http / snmp) with runtime device registry
  * AI Insights powered by the factory's own LLM (OpenAI-compatible endpoint)
  * NOVA Agents: user-defined autonomous analysts that reason over the fleet
    telemetry with a tool loop, streamed live to the UI via SSE

Run:  uvicorn nova_server:app --host 0.0.0.0 --port 8080
"""
import asyncio
import json
import math
import os
import sqlite3
import subprocess
import time
from collections import defaultdict
from contextlib import asynccontextmanager

import urllib.request

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

BASE = os.path.dirname(os.path.abspath(__file__))


def load_cfg():
    cfg = {}
    for name in ("config.json", "config.local.json"):     # local overrides
        p = os.path.join(BASE, name)
        if os.path.exists(p):
            with open(p) as f:
                cfg.update(json.load(f))
    return cfg


CFG = load_cfg()
DB_PATH = os.path.join(BASE, CFG.get("db_file", "nova.db"))
TOKEN = CFG.get("ingest_token", "")
LLM = CFG.get("llm", {})
if os.environ.get("LLM_BASE_URL"):
    LLM["base_url"] = os.environ["LLM_BASE_URL"]
if os.environ.get("LLM_API_KEY"):
    LLM["api_key"] = os.environ["LLM_API_KEY"]
if os.environ.get("LLM_MODEL"):
    LLM["model"] = os.environ["LLM_MODEL"]
THRESHOLDS = CFG.get("thresholds", {})
WEBHOOKS = CFG.get("webhooks", [])
DEVICES = CFG.get("devices", [])
RETAIN_RAW_H = int(CFG.get("retention_raw_hours", 48))
RETAIN_ROLL_D = int(CFG.get("retention_rollup_days", 90))

# ------------------------------------------------------------------ storage
def db():
    c = sqlite3.connect(DB_PATH)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    return c


def init_db():
    c = db()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS samples(
        ts REAL, node TEXT, metric TEXT, value REAL);
    CREATE INDEX IF NOT EXISTS ix_samples ON samples(node, metric, ts);
    CREATE TABLE IF NOT EXISTS rollup(
        ts INTEGER, node TEXT, metric TEXT, avg REAL, mx REAL,
        PRIMARY KEY(ts, node, metric));
    CREATE TABLE IF NOT EXISTS meta(
        node TEXT, key TEXT, ts REAL, json TEXT, PRIMARY KEY(node, key));
    CREATE TABLE IF NOT EXISTS alerts(
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, node TEXT,
        severity TEXT, title TEXT, msg TEXT, state TEXT DEFAULT 'open');
    CREATE TABLE IF NOT EXISTS insights(
        kind TEXT PRIMARY KEY, ts REAL, report TEXT);
    CREATE TABLE IF NOT EXISTS insight_history(
        id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, ts REAL, report TEXT);
    CREATE TABLE IF NOT EXISTS device_status(
        name TEXT PRIMARY KEY, host TEXT, kind TEXT, ok INTEGER,
        latency_ms REAL, detail TEXT, ts REAL);
    CREATE TABLE IF NOT EXISTS devices_dyn(
        name TEXT PRIMARY KEY, host TEXT, kind TEXT, port INTEGER,
        url TEXT, community TEXT);
    CREATE TABLE IF NOT EXISTS threshold_overrides(
        key TEXT PRIMARY KEY, value REAL);
    CREATE TABLE IF NOT EXISTS agents(
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, goal TEXT,
        created REAL, enabled INTEGER DEFAULT 1);
    CREATE TABLE IF NOT EXISTS agent_runs(
        id INTEGER PRIMARY KEY AUTOINCREMENT, agent_id INTEGER, ts REAL,
        status TEXT, steps TEXT, report TEXT);
    """)
    c.commit()
    c.close()


# ----------------------------------------------------- runtime thresholds
OVERRIDES = {}


def load_overrides():
    c = db()
    OVERRIDES.clear()
    OVERRIDES.update({k: v for k, v in
                      c.execute("SELECT key,value FROM threshold_overrides")})
    c.close()


def th(key, default):
    """Effective threshold: runtime override > config.json > code default."""
    if key in OVERRIDES:
        return OVERRIDES[key]
    return THRESHOLDS.get(key, default)


# --------------------------------------------------------- anomaly + alerts
class Rolling:
    """EWMA + variance tracker for one metric stream."""
    __slots__ = ("mean", "var", "n", "hits")

    def __init__(self):
        self.mean, self.var, self.n, self.hits = 0.0, 0.0, 0, 0

    def push(self, x, alpha=0.05):
        if self.n == 0:
            self.mean, self.var = x, 1.0
        else:
            d = x - self.mean
            self.mean += alpha * d
            self.var = (1 - alpha) * (self.var + alpha * d * d)
        self.n += 1
        z = abs(x - self.mean) / max(math.sqrt(self.var), 1e-3)
        return z


ROLL = defaultdict(Rolling)
ALERT_STATE = {}          # key -> open alert id (dedup)
LAST_SEEN = {}            # node -> ts

ANOMALY_METRICS = ("cpu.total", "mem.used_pct", "net.total.rx", "net.total.tx",
                   "gpu.util", "disk.read_mbs", "disk.write_mbs")


def fire_webhooks(payload):
    for wh in WEBHOOKS:
        try:
            if wh.get("type") == "telegram":
                text = f"🛰 NOVA Glass [{payload['severity'].upper()}] {payload['node']}\n" \
                       f"{payload['title']}\n{payload['msg']}"
                url = (f"https://api.telegram.org/bot{wh['bot_token']}/sendMessage")
                data = json.dumps({"chat_id": wh["chat_id"], "text": text}).encode()
            else:
                url = wh["url"]
                data = json.dumps(payload).encode()
            req = urllib.request.Request(url, data=data,
                                         headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=10).read()
        except Exception as e:
            print(f"[webhook] {wh.get('type','generic')} failed: {e}", flush=True)


def raise_alert(node, severity, title, msg, key=None):
    key = key or f"{node}:{title}"
    if key in ALERT_STATE:
        return
    c = db()
    cur = c.execute("INSERT INTO alerts(ts,node,severity,title,msg) VALUES(?,?,?,?,?)",
                    (time.time(), node, severity, title, msg))
    c.commit(); c.close()
    ALERT_STATE[key] = cur.lastrowid
    fire_webhooks({"event": "alert.open", "node": node, "severity": severity,
                   "title": title, "msg": msg, "ts": time.time()})


def clear_alert(node, title, key=None):
    key = key or f"{node}:{title}"
    aid = ALERT_STATE.pop(key, None)
    if aid is None:
        return
    c = db()
    c.execute("UPDATE alerts SET state='closed' WHERE id=? AND state='open'", (aid,))
    c.commit(); c.close()
    fire_webhooks({"event": "alert.close", "node": node, "title": title,
                   "ts": time.time()})


THRESH_HITS = defaultdict(int)


def evaluate(node, metrics):
    checks = [
        ("cpu.total", th("cpu_pct", 92), "CPU saturada"),
        ("mem.used_pct", th("mem_pct", 92), "Memoria al límite"),
        ("swap.used_pct", th("swap_pct", 60), "Swap en uso intensivo"),
        ("gpu.temp", th("gpu_temp", 88), "GPU temperatura alta"),
    ]
    need = int(th("sustained_samples", 4))
    for metric, limit, title in checks:
        v = metrics.get(metric)
        if v is None:
            continue
        k = f"{node}:{metric}"
        if v >= limit:
            THRESH_HITS[k] += 1
            if THRESH_HITS[k] >= need:
                raise_alert(node, "critical", title,
                            f"{metric}={v:.1f} ≥ {limit} (sostenido)", key=k)
        else:
            THRESH_HITS[k] = 0
            clear_alert(node, title, key=k)

    for metric in ANOMALY_METRICS:
        v = metrics.get(metric)
        if v is None:
            continue
        r = ROLL[f"{node}:{metric}"]
        z = r.push(v)
        if r.n > 60 and z > float(th("anomaly_z", 4.0)):
            r.hits += 1
            if r.hits >= 3:
                raise_alert(node, "warning", f"Anomalía en {metric}",
                            f"valor {v:.2f}, z-score {z:.1f} vs media {r.mean:.2f}",
                            key=f"anom:{node}:{metric}")
                r.hits = 0
        else:
            r.hits = 0
            clear_alert(node, f"Anomalía en {metric}", key=f"anom:{node}:{metric}")


# --------------------------------------------------------------- device NOC
def dyn_devices():
    c = db()
    rows = c.execute("SELECT name,host,kind,port,url,community FROM devices_dyn").fetchall()
    c.close()
    out = []
    for name, host, kind, port, url, community in rows:
        d = {"name": name, "host": host, "kind": kind or "ping"}
        if port: d["port"] = port
        if url: d["url"] = url
        if community: d["community"] = community
        out.append(d)
    return out


def all_devices():
    dyn = dyn_devices()
    dyn_names = {d["name"] for d in dyn}
    return [d for d in DEVICES if d["name"] not in dyn_names] + dyn


async def probe_device(d):
    host, kind = d["host"], d.get("kind", "ping")
    t0 = time.time()
    ok, detail = False, ""
    try:
        if kind == "tcp":
            fut = asyncio.open_connection(host, int(d.get("port", 80)))
            _, w = await asyncio.wait_for(fut, timeout=3)
            w.close()
            ok, detail = True, f"tcp:{d.get('port')}"
        elif kind == "http":
            def _get():
                return urllib.request.urlopen(d.get("url", f"http://{host}"),
                                              timeout=4).status
            code = await asyncio.get_event_loop().run_in_executor(None, _get)
            ok, detail = code < 500, f"http {code}"
        elif kind == "snmp":
            # optional dependency — graceful fallback to ping
            try:
                from pysnmp.hlapi import (getCmd, SnmpEngine, CommunityData,
                                          UdpTransportTarget, ContextData,
                                          ObjectType, ObjectIdentity)
                def _snmp():
                    it = getCmd(SnmpEngine(),
                                CommunityData(d.get("community", "public")),
                                UdpTransportTarget((host, 161), timeout=2, retries=0),
                                ContextData(),
                                ObjectType(ObjectIdentity("1.3.6.1.2.1.1.5.0")))
                    err, _, _, binds = next(it)
                    return None if err else str(binds[0][1])
                name = await asyncio.get_event_loop().run_in_executor(None, _snmp)
                ok, detail = name is not None, (f"sysName={name}" if name else "snmp timeout")
            except ImportError:
                kind = "ping"
        if kind == "ping":
            proc = await asyncio.create_subprocess_exec(
                "ping", "-c1", "-W2", str(host), stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL)
            ok = (await proc.wait()) == 0
            detail = "icmp"
    except Exception as e:
        detail = str(e)[:60]
    ms = round((time.time() - t0) * 1000, 1)

    c = db()
    prev = c.execute("SELECT ok FROM device_status WHERE name=?",
                     (d["name"],)).fetchone()
    c.execute("REPLACE INTO device_status VALUES(?,?,?,?,?,?,?)",
              (d["name"], host, d.get("kind", "ping"), int(ok), ms, detail, time.time()))
    c.commit(); c.close()

    if prev is not None and bool(prev[0]) != ok:
        if ok:
            clear_alert(d["name"], "Dispositivo caído", key=f"dev:{d['name']}")
        else:
            raise_alert(d["name"], "critical", "Dispositivo caído",
                        f"{host} ({d.get('kind','ping')}) sin respuesta",
                        key=f"dev:{d['name']}")


async def device_loop():
    while True:
        try:
            await asyncio.gather(*(probe_device(d) for d in all_devices()))
        except Exception as e:
            print(f"[devices] {e}", flush=True)
        # stale-node watchdog
        now = time.time()
        for node, ts in list(LAST_SEEN.items()):
            if now - ts > 90:
                raise_alert(node, "critical", "Nodo sin telemetría",
                            f"último dato hace {int(now-ts)}s", key=f"stale:{node}")
            else:
                clear_alert(node, "Nodo sin telemetría", key=f"stale:{node}")
        await asyncio.sleep(int(CFG.get("device_interval", 30)))


# ------------------------------------------------------------- maintenance
async def maintenance_loop():
    while True:
        try:
            c = db()
            cutoff = time.time() - RETAIN_RAW_H * 3600
            # roll up raw → 1-minute buckets before purge
            c.execute("""
                INSERT OR REPLACE INTO rollup(ts,node,metric,avg,mx)
                SELECT CAST(ts/60 AS INT)*60, node, metric, AVG(value), MAX(value)
                FROM samples WHERE ts < ? GROUP BY 1,2,3""", (cutoff,))
            c.execute("DELETE FROM samples WHERE ts < ?", (cutoff,))
            c.execute("DELETE FROM rollup WHERE ts < ?",
                      (time.time() - RETAIN_ROLL_D * 86400,))
            c.commit(); c.close()
        except Exception as e:
            print(f"[maintenance] {e}", flush=True)
        await asyncio.sleep(1800)


# ----------------------------------------------------------------- LLM glue
def llm_chat(prompt, system="Eres el analista NOC de una AI factory.",
             max_tokens=1200, messages=None):
    url = LLM.get("base_url", "http://127.0.0.1:8003/v1").rstrip("/") + "/chat/completions"
    body = {
        "model": LLM.get("model", "deepseek-v4-flash"),
        "messages": messages or [{"role": "system", "content": system},
                                 {"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": float(LLM.get("temperature", 0.3)),
        "chat_template_kwargs": {"thinking": False},
    }
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json",
                                          "Authorization": f"Bearer {LLM.get('api_key','none')}"})
    with urllib.request.urlopen(req, timeout=int(LLM.get("timeout", 180))) as r:
        out = json.load(r)
    return out["choices"][0]["message"]["content"] or ""


INSIGHT_PROMPTS = {
    "anomaly": "Analiza anomalías: identifica métricas fuera de patrón, correlaciónalas "
               "entre nodos y explica causas probables. Sé específico con horas y valores.",
    "capacity": "Haz capacity planning: proyecta crecimiento de CPU/RAM/almacenamiento/red, "
                "señala cuellos de botella próximos y recomienda cuándo ampliar.",
    "summary": "Resumen ejecutivo de la infraestructura: salud general, disponibilidad, "
               "uso de recursos por nodo, contenedores activos y pendientes de atención.",
    "performance": "Optimización de rendimiento: detecta desperdicio o saturación de "
                   "recursos, procesos/contenedores problemáticos y acciones concretas.",
}


def build_context():
    """Compact JSON snapshot of the fleet for the LLM."""
    c = db()
    ctx = {"generated": time.strftime("%Y-%m-%d %H:%M:%S"), "nodes": {}}
    nodes = [r[0] for r in c.execute("SELECT DISTINCT node FROM samples")]
    hour_ago = time.time() - 3600
    for n in nodes:
        rows = c.execute("""
            SELECT metric, ROUND(AVG(value),2), ROUND(MAX(value),2), ROUND(MIN(value),2)
            FROM samples WHERE node=? AND ts>? GROUP BY metric""", (n, hour_ago)).fetchall()
        ctx["nodes"][n] = {m: {"avg": a, "max": mx, "min": mn} for m, a, mx, mn in rows}
        for key in ("mounts", "users_storage", "updates", "security_updates",
                    "firmware_updates", "containers", "journal_errors"):
            r = c.execute("SELECT json FROM meta WHERE node=? AND key=?", (n, key)).fetchone()
            if r:
                ctx["nodes"][n][key] = json.loads(r[0])
    ctx["alerts_open"] = [dict(zip(("ts", "node", "sev", "title", "msg"), r)) for r in
                          c.execute("SELECT ts,node,severity,title,msg FROM alerts "
                                    "WHERE state='open' ORDER BY ts DESC LIMIT 20")]
    ctx["alerts_recent"] = [dict(zip(("ts", "node", "sev", "title"), r)) for r in
                            c.execute("SELECT ts,node,severity,title FROM alerts "
                                      "ORDER BY ts DESC LIMIT 30")]
    c.close()
    return ctx


# ---------------------------------------------------------- NOVA Agents
# User-defined autonomous analysts. Each run is a ReAct-style tool loop
# against the fleet telemetry, executed by the factory's own LLM and
# streamed live to the UI (SSE) as a "reasoning stream".
AGENT_TOOLS_DOC = """Herramientas disponibles (responde SOLO con un JSON por turno):
  {"action":"tool","tool":"get_nodes","args":{}}                          → estado actual de todos los nodos
  {"action":"tool","tool":"get_series","args":{"node":"…","metric":"…","mins":60}} → serie histórica (metrics: cpu.total, mem.used_pct, gpu.util, gpu.temp, net.total.rx, net.total.tx, disk.read_mbs, disk.write_mbs, swap.used_pct)
  {"action":"tool","tool":"get_meta","args":{"node":"…","key":"…"}}      → snapshot lento (keys: mounts, users_storage, updates, security_updates, firmware_updates, containers, journal_errors)
  {"action":"tool","tool":"get_alerts","args":{"limit":30}}              → alertas recientes
  {"action":"tool","tool":"get_devices","args":{}}                       → dispositivos NOC (up/down, latencia)
  {"action":"final","report":"…markdown…"}                               → entrega tu reporte final
Incluye siempre un campo "thought" (1-2 frases) explicando tu razonamiento antes de la acción."""


def agent_tool_call(tool, args):
    args = args or {}
    if tool == "get_nodes":
        return nodes()
    if tool == "get_series":
        return series(str(args.get("node", "")), str(args.get("metric", "cpu.total")),
                      min(int(args.get("mins", 60)), 2880), 120)
    if tool == "get_meta":
        return meta(str(args.get("node", "")), str(args.get("key", "mounts")))
    if tool == "get_alerts":
        return alerts(min(int(args.get("limit", 30)), 100))
    if tool == "get_devices":
        return devices()
    return {"error": f"tool desconocida: {tool}"}


def parse_agent_json(txt):
    """Extract the first JSON object from the model reply (tolerates fences)."""
    txt = txt.strip()
    if txt.startswith("```"):
        txt = txt.strip("`")
        if txt.startswith("json"):
            txt = txt[4:]
    start = txt.find("{")
    if start < 0:
        return None
    depth = 0
    for i, ch in enumerate(txt[start:], start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(txt[start:i + 1])
                except json.JSONDecodeError:
                    return None
    return None


async def run_agent_stream(agent_id, name, goal):
    """Async generator yielding SSE events for one agent run."""
    def ev(type_, **kw):
        return f"data: {json.dumps({'type': type_, **kw}, ensure_ascii=False)}\n\n"

    steps, max_steps = [], 6
    sysmsg = (f"Eres «{name}», un agente autónomo del NOC de una AI factory. "
              f"Tu misión permanente: {goal}\n\n"
              f"Trabajas en un bucle razonamiento→herramienta→observación. "
              f"{AGENT_TOOLS_DOC}\n"
              f"Máximo {max_steps} turnos; sé eficiente. El reporte final es Markdown "
              f"en español, accionable, con valores y horas concretas.")
    msgs = [{"role": "system", "content": sysmsg},
            {"role": "user", "content": "Inicia tu misión. Snapshot base:\n```json\n"
             + json.dumps(build_context())[:16000] + "\n```"}]
    yield ev("start", agent=name, goal=goal)
    report, status = None, "error"
    loop = asyncio.get_event_loop()
    try:
        for step in range(max_steps):
            raw = await loop.run_in_executor(None, lambda: llm_chat("", messages=msgs, max_tokens=1600))
            act = parse_agent_json(raw)
            if act is None:
                # model answered free-form: treat as final report
                report, status = raw, "done"
                yield ev("final", report=report)
                break
            thought = act.get("thought", "")
            if thought:
                steps.append({"thought": thought})
                yield ev("thought", text=thought, step=step + 1)
            if act.get("action") == "final":
                report, status = act.get("report", raw), "done"
                yield ev("final", report=report)
                break
            tool, targs = act.get("tool", ""), act.get("args", {})
            yield ev("tool", tool=tool, args=targs, step=step + 1)
            obs = agent_tool_call(tool, targs)
            obs_txt = json.dumps(obs, ensure_ascii=False)[:8000]
            steps.append({"tool": tool, "args": targs})
            yield ev("observation", size=len(obs_txt), step=step + 1)
            msgs.append({"role": "assistant", "content": raw})
            msgs.append({"role": "user", "content": f"OBSERVACIÓN:\n{obs_txt}"})
        else:
            # step budget exhausted — ask for a wrap-up
            msgs.append({"role": "user", "content":
                         "Límite de pasos alcanzado. Entrega ahora tu reporte final en Markdown."})
            report = await loop.run_in_executor(None, lambda: llm_chat("", messages=msgs, max_tokens=1600))
            status = "done"
            yield ev("final", report=report)
    except Exception as e:
        yield ev("error", msg=str(e)[:200])
        report = f"error: {e}"
    c = db()
    c.execute("INSERT INTO agent_runs(agent_id,ts,status,steps,report) VALUES(?,?,?,?,?)",
              (agent_id, time.time(), status, json.dumps(steps, ensure_ascii=False), report or ""))
    c.commit(); c.close()
    yield ev("end", status=status)


# -------------------------------------------------------------------- app
@asynccontextmanager
async def lifespan(app):
    init_db()
    load_overrides()
    t1 = asyncio.create_task(device_loop())
    t2 = asyncio.create_task(maintenance_loop())
    yield
    t1.cancel(); t2.cancel()


app = FastAPI(title="NOVA Glass", lifespan=lifespan)


def check_token(req: Request):
    if TOKEN and req.headers.get("X-Nova-Token", "") != TOKEN:
        raise HTTPException(401, "bad token")


@app.post("/api/ingest")
async def ingest(req: Request):
    check_token(req)
    p = await req.json()
    node, ts, metrics = p["node"], float(p.get("ts", time.time())), p["metrics"]
    LAST_SEEN[node] = time.time()
    c = db()
    c.executemany("INSERT INTO samples VALUES(?,?,?,?)",
                  [(ts, node, k, float(v)) for k, v in metrics.items()
                   if isinstance(v, (int, float))])
    c.commit(); c.close()
    evaluate(node, metrics)
    return {"ok": True}


@app.post("/api/ingest_meta")
async def ingest_meta(req: Request):
    check_token(req)
    p = await req.json()
    node, meta = p["node"], p["meta"]
    LAST_SEEN.setdefault(node, time.time())
    c = db()
    for k, v in meta.items():
        c.execute("REPLACE INTO meta VALUES(?,?,?,?)",
                  (node, k, time.time(), json.dumps(v)))
    c.commit(); c.close()
    # disk threshold alerts from mounts snapshot
    for mnt in meta.get("mounts", []):
        key = f"{node}:disk:{mnt['mount']}"
        if mnt["pct"] >= th("disk_pct", 85):
            raise_alert(node, "warning", f"Disco lleno {mnt['mount']}",
                        f"{mnt['pct']}% de {mnt['total_gb']}GB", key=key)
        else:
            clear_alert(node, f"Disco lleno {mnt['mount']}", key=key)
    return {"ok": True}


@app.get("/api/nodes")
def nodes():
    c = db()
    out = {}
    cutoff = time.time() - 30
    for node, in c.execute("SELECT DISTINCT node FROM samples"):
        rows = c.execute("""
            SELECT metric, value, ts FROM samples s WHERE node=? AND ts=(
              SELECT MAX(ts) FROM samples WHERE node=? AND metric=s.metric)""",
            (node, node)).fetchall()
        latest = {r[0]: r[1] for r in rows}
        last_ts = max((r[2] for r in rows), default=0)
        out[node] = {"metrics": latest,
                     "online": LAST_SEEN.get(node, 0) > cutoff,
                     "last_seen": LAST_SEEN.get(node, 0),
                     "last_sample_ts": last_ts}
    c.close()
    return out


@app.get("/api/series")
def series(node: str, metric: str, mins: int = 60, points: int = 240):
    c = db()
    t0 = time.time() - mins * 60
    if mins <= RETAIN_RAW_H * 60:
        bucket = max(5, int(mins * 60 / points))
        rows = c.execute("""
            SELECT CAST(ts/? AS INT)*?, ROUND(AVG(value),3), ROUND(MAX(value),3)
            FROM samples WHERE node=? AND metric=? AND ts>? GROUP BY 1 ORDER BY 1""",
            (bucket, bucket, node, metric, t0)).fetchall()
    else:
        rows = c.execute("""
            SELECT ts, avg, mx FROM rollup
            WHERE node=? AND metric=? AND ts>? ORDER BY ts""",
            (node, metric, t0)).fetchall()
    c.close()
    return {"node": node, "metric": metric,
            "points": [{"t": r[0], "v": r[1], "mx": r[2]} for r in rows]}


@app.get("/api/meta")
def meta(node: str, key: str):
    c = db()
    r = c.execute("SELECT ts, json FROM meta WHERE node=? AND key=?",
                  (node, key)).fetchone()
    c.close()
    if not r:
        return {"ts": 0, "data": None}
    return {"ts": r[0], "data": json.loads(r[1])}


@app.get("/api/logs")
def logs():
    """Aggregated journal errors reported by each node's collector."""
    c = db()
    out = {}
    for node, ts, js in c.execute(
            "SELECT node, ts, json FROM meta WHERE key='journal_errors'"):
        out[node] = {"ts": ts, **json.loads(js)}
    c.close()
    return out


@app.get("/api/alerts")
def alerts(limit: int = 100):
    c = db()
    rows = c.execute("""SELECT id,ts,node,severity,title,msg,state
                        FROM alerts ORDER BY ts DESC LIMIT ?""", (limit,)).fetchall()
    c.close()
    return [dict(zip(("id", "ts", "node", "severity", "title", "msg", "state"), r))
            for r in rows]


@app.post("/api/alerts/{alert_id}/ack")
def ack_alert(alert_id: int):
    """Dismiss an alert. It can re-fire if the condition triggers again."""
    c = db()
    cur = c.execute("UPDATE alerts SET state='acked' WHERE id=?", (alert_id,))
    c.commit(); c.close()
    if cur.rowcount == 0:
        raise HTTPException(404, "alerta no encontrada")
    for k, v in list(ALERT_STATE.items()):
        if v == alert_id:
            ALERT_STATE.pop(k, None)
    return {"ok": True, "id": alert_id}


@app.get("/api/thresholds")
def get_thresholds():
    defaults = {"cpu_pct": 92, "mem_pct": 92, "swap_pct": 60, "gpu_temp": 88,
                "disk_pct": 85, "anomaly_z": 4.0, "sustained_samples": 4}
    return {k: {"effective": th(k, d), "config": THRESHOLDS.get(k, d),
                "override": OVERRIDES.get(k)} for k, d in defaults.items()}


@app.post("/api/thresholds")
async def set_thresholds(req: Request):
    p = await req.json()
    allowed = {"cpu_pct", "mem_pct", "swap_pct", "gpu_temp", "disk_pct",
               "anomaly_z", "sustained_samples"}
    c = db()
    for k, v in p.items():
        if k not in allowed:
            continue
        if v is None or v == "":
            c.execute("DELETE FROM threshold_overrides WHERE key=?", (k,))
        else:
            c.execute("REPLACE INTO threshold_overrides VALUES(?,?)", (k, float(v)))
    c.commit(); c.close()
    load_overrides()
    return get_thresholds()


@app.get("/api/devices")
def devices():
    c = db()
    rows = c.execute("SELECT name,host,kind,ok,latency_ms,detail,ts "
                     "FROM device_status ORDER BY name").fetchall()
    c.close()
    dyn_names = {d["name"] for d in dyn_devices()}
    return [dict(zip(("name", "host", "kind", "ok", "latency_ms", "detail", "ts"), r),
                 dynamic=r[0] in dyn_names)
            for r in rows]


@app.post("/api/devices")
async def add_device(req: Request):
    p = await req.json()
    name, host = (p.get("name") or "").strip(), (p.get("host") or "").strip()
    kind = p.get("kind", "ping")
    if not name or not host:
        raise HTTPException(400, "name y host son obligatorios")
    if kind not in ("ping", "tcp", "http", "snmp"):
        raise HTTPException(400, "kind inválido")
    c = db()
    c.execute("REPLACE INTO devices_dyn VALUES(?,?,?,?,?,?)",
              (name, host, kind, int(p["port"]) if p.get("port") else None,
               p.get("url") or None, p.get("community") or None))
    c.commit(); c.close()
    d = next(x for x in all_devices() if x["name"] == name)
    asyncio.ensure_future(probe_device(d))       # probe immediately
    return {"ok": True, "device": d}


@app.delete("/api/devices/{name}")
def del_device(name: str):
    c = db()
    c.execute("DELETE FROM devices_dyn WHERE name=?", (name,))
    c.execute("DELETE FROM device_status WHERE name=?", (name,))
    c.commit(); c.close()
    return {"ok": True}


@app.get("/api/insight/{kind}")
def get_insight(kind: str):
    c = db()
    r = c.execute("SELECT ts, report FROM insights WHERE kind=?", (kind,)).fetchone()
    c.close()
    return {"kind": kind, "ts": r[0] if r else 0, "report": r[1] if r else None}


@app.get("/api/insights/history")
def insight_history(limit: int = 12):
    c = db()
    rows = c.execute("""SELECT id,kind,ts,report FROM insight_history
                        ORDER BY ts DESC LIMIT ?""", (limit,)).fetchall()
    c.close()
    return [{"id": r[0], "kind": r[1], "ts": r[2], "report": r[3]} for r in rows]


@app.post("/api/insight/{kind}")
async def gen_insight(kind: str):
    if kind not in INSIGHT_PROMPTS:
        raise HTTPException(404, "unknown insight kind")
    ctx = build_context()
    prompt = (f"{INSIGHT_PROMPTS[kind]}\n\nResponde en español, en Markdown, máximo "
              f"~400 palabras, con secciones y bullets accionables.\n\n"
              f"TELEMETRÍA (última hora):\n```json\n{json.dumps(ctx)[:24000]}\n```")
    try:
        report = await asyncio.get_event_loop().run_in_executor(None, llm_chat, prompt)
    except Exception as e:
        raise HTTPException(502, f"LLM no disponible: {e}")
    now = time.time()
    c = db()
    c.execute("REPLACE INTO insights VALUES(?,?,?)", (kind, now, report))
    c.execute("INSERT INTO insight_history(kind,ts,report) VALUES(?,?,?)",
              (kind, now, report))
    c.commit(); c.close()
    return {"kind": kind, "ts": now, "report": report}


@app.get("/api/llm/ping")
async def llm_ping():
    """Payload test: verify the factory LLM endpoint end-to-end."""
    t0 = time.time()
    try:
        reply = await asyncio.get_event_loop().run_in_executor(
            None, lambda: llm_chat("Responde exactamente: NOVA-OK", max_tokens=16))
        ok = "NOVA-OK" in reply
        return {"ok": ok, "latency_ms": round((time.time() - t0) * 1000, 1),
                "model": LLM.get("model", "?"),
                "endpoint": LLM.get("base_url", "?"),
                "reply": reply.strip()[:120]}
    except Exception as e:
        return {"ok": False, "latency_ms": round((time.time() - t0) * 1000, 1),
                "model": LLM.get("model", "?"),
                "endpoint": LLM.get("base_url", "?"), "error": str(e)[:200]}


# ------------------------------------------------------------ agents API
@app.get("/api/agents")
def list_agents():
    c = db()
    ags = [dict(zip(("id", "name", "goal", "created", "enabled"), r)) for r in
           c.execute("SELECT id,name,goal,created,enabled FROM agents ORDER BY id")]
    for a in ags:
        r = c.execute("""SELECT ts,status FROM agent_runs WHERE agent_id=?
                         ORDER BY ts DESC LIMIT 1""", (a["id"],)).fetchone()
        a["last_run"] = {"ts": r[0], "status": r[1]} if r else None
    c.close()
    return ags


@app.post("/api/agents")
async def create_agent(req: Request):
    p = await req.json()
    name, goal = (p.get("name") or "").strip(), (p.get("goal") or "").strip()
    if not name or not goal:
        raise HTTPException(400, "name y goal son obligatorios")
    c = db()
    cur = c.execute("INSERT INTO agents(name,goal,created) VALUES(?,?,?)",
                    (name, goal, time.time()))
    c.commit(); c.close()
    return {"ok": True, "id": cur.lastrowid}


@app.delete("/api/agents/{agent_id}")
def delete_agent(agent_id: int):
    c = db()
    c.execute("DELETE FROM agents WHERE id=?", (agent_id,))
    c.execute("DELETE FROM agent_runs WHERE agent_id=?", (agent_id,))
    c.commit(); c.close()
    return {"ok": True}


@app.get("/api/agents/{agent_id}/run")
async def run_agent(agent_id: int):
    c = db()
    r = c.execute("SELECT name,goal FROM agents WHERE id=?", (agent_id,)).fetchone()
    c.close()
    if not r:
        raise HTTPException(404, "agente no encontrado")
    return StreamingResponse(run_agent_stream(agent_id, r[0], r[1]),
                             media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@app.get("/api/agents/{agent_id}/runs")
def agent_runs(agent_id: int, limit: int = 10):
    c = db()
    rows = c.execute("""SELECT id,ts,status,report FROM agent_runs
                        WHERE agent_id=? ORDER BY ts DESC LIMIT ?""",
                     (agent_id, limit)).fetchall()
    c.close()
    return [{"id": r[0], "ts": r[1], "status": r[2], "report": r[3]} for r in rows]


@app.get("/api/health")
def health():
    return {"ok": True, "nodes": len(LAST_SEEN), "ts": time.time()}


@app.get("/")
def index():
    return FileResponse(os.path.join(BASE, "static", "index.html"))


app.mount("/static", StaticFiles(directory=os.path.join(BASE, "static")), name="static")
