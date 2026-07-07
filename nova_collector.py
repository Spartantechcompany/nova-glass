#!/usr/bin/env python3
"""NOVA Glass — node collector agent.

Runs on every node of the AI factory. Gathers system, GPU, network,
storage, container, log and update telemetry and pushes it to the hub.

Fast loop  (default 5s):  cpu / mem / net / disk-io / gpu
Slow loop  (default 300s): mounts, per-user storage, journal errors,
                           pending apt updates, docker containers, uptime

Only stdlib + psutil. No sudo required.
"""
import json
import os
import re
import socket
import subprocess
import threading
import time
import urllib.request

import psutil

CFG_PATHS = [
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.local.json"),
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json"),
]


def load_cfg():
    cfg = {}
    for p in reversed(CFG_PATHS):          # config.json first, local overrides
        if os.path.exists(p):
            with open(p) as f:
                cfg.update(json.load(f))
    return cfg


CFG = load_cfg()
HUB = os.environ.get("NOVA_HUB", CFG.get("hub_url", "http://127.0.0.1:8082"))
NODE = os.environ.get("NOVA_NODE", CFG.get("node_name") or socket.gethostname())
TOKEN = os.environ.get("NOVA_TOKEN", CFG.get("ingest_token", ""))
FAST = int(CFG.get("fast_interval", 5))
SLOW = int(CFG.get("slow_interval", 300))
HOME_ROOT = CFG.get("user_storage_root", "/home")


def sh(cmd, timeout=20):
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True,
                              timeout=timeout).stdout.strip()
    except Exception:
        return ""


def post(path, payload):
    try:
        req = urllib.request.Request(
            HUB + path,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json",
                     "X-Nova-Token": TOKEN},
        )
        urllib.request.urlopen(req, timeout=10).read()
    except Exception as e:
        print(f"[collector] post {path} failed: {e}", flush=True)


# ---------------------------------------------------------------- fast loop
_prev_net = {}
_prev_dio = None
_prev_t = None


def gpu_sample():
    """nvidia-smi util/temp/power. GB10 reports memory N/A (unified RAM)."""
    out = sh("nvidia-smi --query-gpu=utilization.gpu,temperature.gpu,power.draw "
             "--format=csv,noheader,nounits", timeout=8)
    if not out:
        return {}
    try:
        util, temp, power = [x.strip() for x in out.splitlines()[0].split(",")]
        d = {}
        if util not in ("", "[N/A]"):
            d["gpu.util"] = float(util)
        if temp not in ("", "[N/A]"):
            d["gpu.temp"] = float(temp)
        if power not in ("", "[N/A]"):
            d["gpu.power"] = float(power)
        return d
    except Exception:
        return {}


def fast_sample():
    global _prev_net, _prev_dio, _prev_t
    now = time.time()
    dt = (now - _prev_t) if _prev_t else FAST
    _prev_t = now

    m = {}
    m["cpu.total"] = psutil.cpu_percent(interval=None)
    load1, load5, _ = os.getloadavg()
    m["cpu.load1"] = round(load1, 2)
    m["cpu.load5"] = round(load5, 2)

    vm = psutil.virtual_memory()
    m["mem.used_pct"] = vm.percent
    m["mem.used_gb"] = round(vm.used / 2**30, 2)
    m["mem.total_gb"] = round(vm.total / 2**30, 2)
    sw = psutil.swap_memory()
    m["swap.used_pct"] = sw.percent

    # per-interface throughput (skip loopback/veth/bridges)
    ifaces = {}
    for name, c in psutil.net_io_counters(pernic=True).items():
        if name == "lo" or name.startswith(("veth", "br-", "docker")):
            continue
        prev = _prev_net.get(name)
        _prev_net[name] = (c.bytes_recv, c.bytes_sent)
        if prev:
            rx = max(0, c.bytes_recv - prev[0]) / dt
            tx = max(0, c.bytes_sent - prev[1]) / dt
            ifaces[name] = {"rx": round(rx / 2**20, 3), "tx": round(tx / 2**20, 3)}  # MB/s
            m[f"net.{name}.rx"] = ifaces[name]["rx"]
            m[f"net.{name}.tx"] = ifaces[name]["tx"]
    if ifaces:
        m["net.total.rx"] = round(sum(v["rx"] for v in ifaces.values()), 3)
        m["net.total.tx"] = round(sum(v["tx"] for v in ifaces.values()), 3)

    dio = psutil.disk_io_counters()
    if dio and _prev_dio:
        m["disk.read_mbs"] = round(max(0, dio.read_bytes - _prev_dio.read_bytes) / dt / 2**20, 2)
        m["disk.write_mbs"] = round(max(0, dio.write_bytes - _prev_dio.write_bytes) / dt / 2**20, 2)
    _prev_dio = dio

    m.update(gpu_sample())
    return m


# ---------------------------------------------------------------- slow loop
def slow_sample():
    meta = {}

    mounts = []
    seen = set()
    for p in psutil.disk_partitions(all=False):
        if p.mountpoint in seen or p.fstype in ("squashfs", "overlay"):
            continue
        seen.add(p.mountpoint)
        try:
            u = psutil.disk_usage(p.mountpoint)
            mounts.append({"mount": p.mountpoint, "fs": p.fstype,
                           "total_gb": round(u.total / 2**30, 1),
                           "used_gb": round(u.used / 2**30, 1),
                           "pct": u.percent})
        except Exception:
            pass
    meta["mounts"] = mounts

    users = []
    out = sh(f"du -s --block-size=1G {HOME_ROOT}/* 2>/dev/null", timeout=120)
    for line in out.splitlines():
        try:
            size, path = line.split(None, 1)
            users.append({"user": os.path.basename(path), "gb": int(size)})
        except ValueError:
            pass
    meta["users_storage"] = sorted(users, key=lambda x: -x["gb"])[:20]

    errs = sh("journalctl -p err --since '-1 hour' --no-pager -n 40 -o short 2>/dev/null",
              timeout=25)
    lines = [l for l in errs.splitlines() if l and not l.startswith("--")]
    meta["journal_errors"] = {"count_1h": len(lines), "lines": lines[-25:]}

    upg = sh("apt list --upgradable 2>/dev/null | tail -n +2", timeout=60)
    pkgs = [l.split("/")[0] for l in upg.splitlines() if "/" in l]
    meta["updates"] = {"count": len(pkgs), "packages": pkgs[:40]}

    docker = sh("docker ps --format '{{.Names}}|{{.Image}}|{{.Status}}' 2>/dev/null",
                timeout=20)
    conts = []
    for line in docker.splitlines():
        parts = line.split("|")
        if len(parts) == 3:
            conts.append({"name": parts[0], "image": parts[1][:60], "status": parts[2]})
    meta["containers"] = conts

    # Firmware updates disponibles (fwupd) — inventario propio, NO pentest
    fw = sh("fwupdmgr get-updates 2>/dev/null | grep -iE 'upgrade|update' | head -20",
            timeout=30)
    meta["firmware_updates"] = [l.strip() for l in fw.splitlines() if l.strip()][:15]

    # CVE del inventario propio: parches de seguridad pendientes en apt
    # (usa el canal -security; NO escanea red ni hace pentest)
    sec = sh("apt list --upgradable 2>/dev/null | grep -i security", timeout=30)
    sec_pkgs = [l.split("/")[0] for l in sec.splitlines() if "/" in l]
    meta["security_updates"] = {"count": len(sec_pkgs), "packages": sec_pkgs[:30]}

    meta["uptime_s"] = int(time.time() - psutil.boot_time())
    meta["kernel"] = sh("uname -r", timeout=5)
    return meta


def slow_loop():
    while True:
        try:
            post("/api/ingest_meta", {"node": NODE, "meta": slow_sample()})
        except Exception as e:
            print(f"[collector] slow loop error: {e}", flush=True)
        time.sleep(SLOW)


def main():
    print(f"[collector] node={NODE} hub={HUB} fast={FAST}s slow={SLOW}s", flush=True)
    psutil.cpu_percent(interval=None)                     # prime
    threading.Thread(target=slow_loop, daemon=True).start()
    while True:
        t0 = time.time()
        try:
            post("/api/ingest", {"node": NODE, "ts": time.time(),
                                 "metrics": fast_sample()})
        except Exception as e:
            print(f"[collector] fast loop error: {e}", flush=True)
        time.sleep(max(0.5, FAST - (time.time() - t0)))


if __name__ == "__main__":
    main()
