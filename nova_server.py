#!/usr/bin/env python3
"""NOVA Glass — hub server.

Single pane of glass for an AI factory:
  * persistent metric history (SQLite WAL — survives browser reloads & reboots)
  * anomaly detection (rolling z-score) + threshold alerting
  * webhook notifications (generic JSON / Telegram)
  * NOC device monitor (ping / tcp, SNMP-ready)
  * AI Insights powered by the factory's own LLM (OpenAI-compatible endpoint)

Run:  uvicorn nova_server:app --host 0.0.0.0 --port 8082
"""
import asyncio
import json
import math
import os
import sqlite3
import subprocess
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager

import urllib.request

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, FileResponse
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
    CREATE TABLE IF NOT EXISTS device_status(
        name TEXT PRIMARY KEY, host TEXT, kind TEXT, ok INTEGER,
        latency_ms REAL, detail TEXT, ts REAL);
    """)
    c.commit()
    c.close()


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
    c.execute("UPDATE alerts SET state='closed' WHERE id=?", (aid,))
    c.commit(); c.close()
    fire_webhooks({"event": "alert.close", "node": node, "title": title,
                   "ts": time.time()})


THRESH_HITS = defaultdict(int)


def evaluate(node, metrics):
    th = THRESHOLDS
    checks = [
        ("cpu.total", th.get("cpu_pct", 92), "CPU saturada"),
        ("mem.used_pct", th.get("mem_pct", 92), "Memoria al límite"),
        ("swap.used_pct", th.get("swap_pct", 60), "Swap en uso intensivo"),
        ("gpu.temp", th.get("gpu_temp", 88), "GPU temperatura alta"),
    ]
    need = int(th.get("sustained_samples", 4))
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
        if r.n > 60 and z > float(th.get("anomaly_z", 4.0)):
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
            proc = await asyncio.create_subprocess_shell(
                f"ping -c1 -W2 {host}", stdout=subprocess.DEVNULL,
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
            await asyncio.gather(*(probe_device(d) for d in DEVICES))
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
def llm_chat(prompt, system="Eres el analista NOC de una AI factory.", max_tokens=1200):
    url = LLM.get("base_url", "http://127.0.0.1:8003/v1").rstrip("/") + "/chat/completions"
    body = {
        "model": LLM.get("model", "deepseek-v4-flash"),
        "messages": [{"role": "system", "content": system},
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


# -------------------------------------------------------------------- app
@asynccontextmanager
async def lifespan(app):
    init_db()
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
        if mnt["pct"] >= THRESHOLDS.get("disk_pct", 85):
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
            SELECT metric, value FROM samples s WHERE node=? AND ts=(
              SELECT MAX(ts) FROM samples WHERE node=? AND metric=s.metric)""",
            (node, node)).fetchall()
        latest = dict(rows)
        out[node] = {"metrics": latest,
                     "online": LAST_SEEN.get(node, 0) > cutoff,
                     "last_seen": LAST_SEEN.get(node, 0)}
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


@app.get("/api/alerts")
def alerts(limit: int = 100):
    c = db()
    rows = c.execute("""SELECT id,ts,node,severity,title,msg,state
                        FROM alerts ORDER BY ts DESC LIMIT ?""", (limit,)).fetchall()
    c.close()
    return [dict(zip(("id", "ts", "node", "severity", "title", "msg", "state"), r))
            for r in rows]


@app.get("/api/devices")
def devices():
    c = db()
    rows = c.execute("SELECT name,host,kind,ok,latency_ms,detail,ts "
                     "FROM device_status ORDER BY name").fetchall()
    c.close()
    return [dict(zip(("name", "host", "kind", "ok", "latency_ms", "detail", "ts"), r))
            for r in rows]


@app.get("/api/insight/{kind}")
def get_insight(kind: str):
    c = db()
    r = c.execute("SELECT ts, report FROM insights WHERE kind=?", (kind,)).fetchone()
    c.close()
    return {"kind": kind, "ts": r[0] if r else 0, "report": r[1] if r else None}


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
    c = db()
    c.execute("REPLACE INTO insights VALUES(?,?,?)", (kind, time.time(), report))
    c.commit(); c.close()
    return {"kind": kind, "ts": time.time(), "report": report}


@app.get("/api/health")
def health():
    return {"ok": True, "nodes": len(LAST_SEEN), "ts": time.time()}


@app.get("/")
def index():
    return FileResponse(os.path.join(BASE, "static", "index.html"))


app.mount("/static", StaticFiles(directory=os.path.join(BASE, "static")), name="static")
