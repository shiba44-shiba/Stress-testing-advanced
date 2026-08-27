#!/usr/bin/env python3
"""
stress_test.py - Advanced staged HTTP load / stress tester.

A dependency-free (Python standard library only) async load generator for
capacity testing sites you own or are explicitly authorized to test. It ramps
traffic through configurable STAGES and reports latency percentiles, throughput,
and error rates so you can find where your site starts to degrade.

Advanced features:
  * HTTP keep-alive with full response parsing (Content-Length + chunked),
    so connections are reused for far higher real throughput.
  * Multiprocess mode (--processes) to drive load from every CPU core.
  * Open-loop rate mode (--rate) with coordinated-omission correction: latency
    is measured from each request's *intended* send time, not just when a free
    worker happened to fire it -- the honest way to measure tail latency.
  * Weighted multi-endpoint mix (--path PATH:WEIGHT, repeatable).
  * Self-contained HTML report (--html) with charts, plus text + JSON output.

This is a LOAD TESTER, not an attack tool: it identifies itself honestly via
User-Agent, does not spoof source addresses, does not rotate proxies or evade
defenses, and refuses to run against any host not on your allowlist.

Runs on Python 3.8+ (including a Chromebook Linux container). No pip install.

Examples:
  python3 stress_test.py https://script.ceo/ --i-own-this
  python3 stress_test.py https://script.ceo/ --i-own-this --profile heavy --processes 4
  python3 stress_test.py https://script.ceo/ --i-own-this --rate 2000 --duration 30
  python3 stress_test.py https://script.ceo/ --i-own-this \
        --path /:5 --path /about:2 --path /api/health:3 --html report.html
"""

import argparse
import asyncio
import json
import math
import multiprocessing as mp
import os
import random
import signal
import ssl
import sys
import time
from urllib.parse import urlparse

ALLOWLIST_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "allowlist.txt")
DEFAULT_UA = "stress_test.py/2.0 (load-tester; authorized capacity testing)"
MAX_SAMPLES = 60000  # per-process reservoir cap for latency percentiles

PROFILES = {
    "light": [
        {"name": "warmup",    "workers": 5,   "duration": 10},
        {"name": "ramp",      "workers": 20,  "duration": 15},
        {"name": "sustained", "workers": 40,  "duration": 20},
        {"name": "cooldown",  "workers": 5,   "duration": 5},
    ],
    "standard": [
        {"name": "warmup",    "workers": 10,  "duration": 15},
        {"name": "ramp",      "workers": 50,  "duration": 20},
        {"name": "sustained", "workers": 150, "duration": 30},
        {"name": "spike",     "workers": 400, "duration": 20},
        {"name": "cooldown",  "workers": 20,  "duration": 10},
    ],
    "heavy": [
        {"name": "warmup",     "workers": 25,   "duration": 15},
        {"name": "ramp",       "workers": 150,  "duration": 25},
        {"name": "sustained",  "workers": 500,  "duration": 40},
        {"name": "spike",      "workers": 1200, "duration": 25},
        {"name": "hold-spike", "workers": 1200, "duration": 20},
        {"name": "cooldown",   "workers": 50,   "duration": 10},
    ],
    "max": [
        {"name": "warmup",     "workers": 50,   "duration": 15},
        {"name": "ramp",       "workers": 400,  "duration": 25},
        {"name": "sustained",  "workers": 1500, "duration": 40},
        {"name": "spike",      "workers": 4000, "duration": 30},
        {"name": "hold-spike", "workers": 4000, "duration": 30},
        {"name": "cooldown",   "workers": 100,  "duration": 10},
    ],
}


# --------------------------------------------------------------------------- #
# Percentiles
# --------------------------------------------------------------------------- #
def percentile(sorted_vals, pct):
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    k = (len(sorted_vals) - 1) * (pct / 100.0)
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return sorted_vals[int(k)]
    return sorted_vals[lo] * (hi - k) + sorted_vals[hi] * (k - lo)


# --------------------------------------------------------------------------- #
# Safety: allowlist
# --------------------------------------------------------------------------- #
def load_allowlist():
    hosts = set()
    if os.path.exists(ALLOWLIST_FILE):
        with open(ALLOWLIST_FILE, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#"):
                    hosts.add(line.lower())
    return hosts


def is_authorized(host, allowlist):
    host = (host or "").lower()
    if host in ("localhost", "127.0.0.1", "::1"):
        return True
    if host.startswith(("10.", "192.168.", "172.16.", "172.17.", "172.18.",
                        "172.19.", "172.2", "172.30.", "172.31.")):
        return True
    for h in allowlist:
        if host == h or host.endswith("." + h):
            return True
    return False


# --------------------------------------------------------------------------- #
# HTTP/1.1 response reader (supports Content-Length, chunked, keep-alive)
# --------------------------------------------------------------------------- #
async def read_http_response(reader, method, timeout):
    line = await asyncio.wait_for(reader.readline(), timeout)
    if not line:
        raise EOFError("no status line")
    parts = line.split(b" ", 2)
    version = parts[0].upper() if parts else b"HTTP/1.0"
    status = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0

    headers = {}
    while True:
        h = await asyncio.wait_for(reader.readline(), timeout)
        if h in (b"\r\n", b"\n", b""):
            break
        if b":" in h:
            k, v = h.split(b":", 1)
            headers[k.strip().lower()] = v.strip()

    # Persistent-connection default depends on the HTTP version: HTTP/1.1 keeps
    # the connection open unless "Connection: close"; HTTP/1.0 closes it unless
    # "Connection: keep-alive" is explicitly present.
    conn_hdr = headers.get(b"connection", b"").lower()
    if version >= b"HTTP/1.1":
        keep_alive = conn_hdr != b"close"
    else:
        keep_alive = conn_hdr == b"keep-alive"
    nbytes = 0

    bodyless = method == "HEAD" or status in (204, 304) or 100 <= status < 200
    te = headers.get(b"transfer-encoding", b"").lower()

    if bodyless:
        pass
    elif b"chunked" in te:
        while True:
            size_line = await asyncio.wait_for(reader.readline(), timeout)
            try:
                size = int(size_line.strip().split(b";")[0] or b"0", 16)
            except ValueError:
                keep_alive = False
                break
            if size == 0:
                while True:
                    t = await asyncio.wait_for(reader.readline(), timeout)
                    if t in (b"\r\n", b"\n", b""):
                        break
                break
            chunk = await asyncio.wait_for(reader.readexactly(size), timeout)
            nbytes += len(chunk)
            await asyncio.wait_for(reader.readexactly(2), timeout)  # trailing CRLF
    elif b"content-length" in headers:
        try:
            body_len = int(headers[b"content-length"])
        except ValueError:
            body_len = 0
        if body_len > 0:
            data = await asyncio.wait_for(reader.readexactly(body_len), timeout)
            nbytes = len(data)
    else:
        # no length signalled -> read to EOF; connection must close
        while True:
            chunk = await asyncio.wait_for(reader.read(65536), timeout)
            if not chunk:
                break
            nbytes += len(chunk)
        keep_alive = False

    return status, nbytes, keep_alive


def build_request(method, path, host, ua, extra_headers, body_bytes, keepalive):
    lines = [
        f"{method} {path} HTTP/1.1",
        f"Host: {host}",
        f"User-Agent: {ua}",
        "Accept: */*",
        "Connection: " + ("keep-alive" if keepalive else "close"),
    ]
    for k, v in extra_headers:
        lines.append(f"{k}: {v}")
    if body_bytes:
        lines.append(f"Content-Length: {len(body_bytes)}")
    req = ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")
    if body_bytes:
        req += body_bytes
    return req


async def open_conn(target, timeout):
    conn = asyncio.open_connection(
        target["host"], target["port"], ssl=target["ssl_ctx"],
        server_hostname=target["host"] if target["ssl_ctx"] else None,
    )
    return await asyncio.wait_for(conn, timeout=timeout)


# --------------------------------------------------------------------------- #
# Local (per-process) accumulator
# --------------------------------------------------------------------------- #
class Acc:
    __slots__ = ("count", "ok", "http_err", "conn_fail", "bytes", "lat_sum",
                 "samples", "status", "errors", "buckets", "t0")

    def __init__(self, t0):
        self.count = 0
        self.ok = 0
        self.http_err = 0
        self.conn_fail = 0
        self.bytes = 0
        self.lat_sum = 0.0
        self.samples = []          # reservoir of latencies (ms)
        self.status = {}
        self.errors = {}
        self.buckets = {}          # sec_offset -> [count, lat_sum]
        self.t0 = t0

    def _sample(self, ms):
        if len(self.samples) < MAX_SAMPLES:
            self.samples.append(ms)
        else:
            j = random.randint(0, self.count)
            if j < MAX_SAMPLES:
                self.samples[j] = ms

    def add(self, status, ms, nbytes, when):
        self.count += 1
        self.lat_sum += ms
        self._sample(ms)
        self.bytes += nbytes
        self.status[status] = self.status.get(status, 0) + 1
        if 200 <= status < 400:
            self.ok += 1
        else:
            self.http_err += 1
        b = int(when - self.t0)
        slot = self.buckets.get(b)
        if slot is None:
            self.buckets[b] = [1, ms]
        else:
            slot[0] += 1
            slot[1] += ms

    def add_error(self, kind):
        self.count += 1
        self.conn_fail += 1
        self.errors[kind] = self.errors.get(kind, 0) + 1

    def to_dict(self):
        return {
            "count": self.count, "ok": self.ok, "http_err": self.http_err,
            "conn_fail": self.conn_fail, "bytes": self.bytes,
            "lat_sum": self.lat_sum, "samples": self.samples,
            "status": self.status, "errors": self.errors, "buckets": self.buckets,
        }


def classify_error(exc):
    if isinstance(exc, asyncio.TimeoutError):
        return "timeout"
    if isinstance(exc, (ConnectionResetError, ConnectionRefusedError, BrokenPipeError)):
        return "conn_reset"
    if isinstance(exc, ssl.SSLError):
        return "ssl_error"
    if isinstance(exc, EOFError):
        return "empty_response"
    if isinstance(exc, OSError):
        return f"os_{getattr(exc, 'errno', '?')}"
    return "unknown"


# --------------------------------------------------------------------------- #
# Closed-loop worker (keep-alive, reuses one connection)
# --------------------------------------------------------------------------- #
async def closed_worker(target, acc, deadline, stop, cfg, rng):
    reader = writer = None
    paths = cfg["paths"]
    weights = cfg["weights"]
    depth = max(1, cfg["pipeline"])          # HTTP pipelining depth
    multi = len(paths) > 1
    while time.perf_counter() < deadline and not stop.value:
        try:
            if writer is None:
                reader, writer = await open_conn(target, cfg["timeout"])
            # Build and send `depth` requests back-to-back on one connection,
            # then read their responses in order (HTTP/1.1 pipelining). With
            # depth=1 this is a plain one-at-a-time keep-alive loop.
            sent, buf = [], b""
            for _ in range(depth):
                path = rng.choices(paths, weights)[0] if multi else paths[0]
                buf += build_request(cfg["method"], path, target["host"], cfg["ua"],
                                     cfg["headers"], cfg["body"], cfg["keepalive"])
                sent.append(time.perf_counter())
            writer.write(buf)
            await asyncio.wait_for(writer.drain(), cfg["timeout"])
            reuse = cfg["keepalive"]
            for i in range(depth):
                status, nbytes, ka = await read_http_response(
                    reader, cfg["method"], cfg["timeout"])
                done = time.perf_counter()
                acc.add(status, (done - sent[i]) * 1000.0, nbytes, done)
                reuse = reuse and ka
            if not reuse:
                _close(writer); reader = writer = None
        except Exception as exc:
            acc.add_error(classify_error(exc))
            _close(writer); reader = writer = None
            await asyncio.sleep(0)  # yield


# --------------------------------------------------------------------------- #
# Open-loop dispatcher (coordinated-omission-corrected).
# Requests are scheduled at a fixed rate; latency is measured from the intended
# send time, so a slow server shows up as rising latency instead of vanishing.
# --------------------------------------------------------------------------- #
async def open_loop(target, acc, deadline, stop, cfg, rate, rng):
    interval = 1.0 / rate if rate > 0 else 0.0
    paths = cfg["paths"]
    weights = cfg["weights"]
    inflight = set()
    max_inflight = cfg["max_inflight"]

    async def one(scheduled, path):
        writer = None
        try:
            reader, writer = await open_conn(target, cfg["timeout"])
            req = build_request(cfg["method"], path, target["host"], cfg["ua"],
                                cfg["headers"], cfg["body"], False)
            writer.write(req)
            await asyncio.wait_for(writer.drain(), cfg["timeout"])
            status, nbytes, _ = await read_http_response(reader, cfg["method"], cfg["timeout"])
            done = time.perf_counter()
            # coordinated-omission correction: measure from *scheduled* time
            acc.add(status, (done - scheduled) * 1000.0, nbytes, done)
        except Exception as exc:
            acc.add_error(classify_error(exc))
        finally:
            _close(writer)

    next_t = time.perf_counter()
    while True:
        now = time.perf_counter()
        if now >= deadline or stop.value:
            break
        if now >= next_t and len(inflight) < max_inflight:
            path = rng.choices(paths, weights)[0] if len(paths) > 1 else paths[0]
            t = asyncio.ensure_future(one(next_t, path))
            inflight.add(t)
            t.add_done_callback(inflight.discard)
            next_t += interval
        else:
            await asyncio.sleep(min(max(next_t - now, 0), 0.005))
    if inflight:
        await asyncio.gather(*inflight, return_exceptions=True)


def _close(writer):
    if writer is not None:
        try:
            writer.close()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# Per-process stage runner (one event loop)
# --------------------------------------------------------------------------- #
def _try_install_uvloop():
    """Use uvloop's faster event loop if it's installed. Returns True if active.
    Pure speedup — a drop-in replacement for asyncio's loop, no behavior change."""
    try:
        import uvloop
        uvloop.install()
        return True
    except Exception:
        return False


def run_stage_in_process(target, stage, cfg, workers, rate, seed, conn):
    if cfg.get("uvloop"):
        _try_install_uvloop()
    rng = random.Random(seed)

    class _Stop:
        __slots__ = ("value",)
        def __init__(self): self.value = False
    stop = _Stop()

    async def go():
        t0 = time.perf_counter()
        acc = Acc(t0)
        deadline = t0 + stage["duration"]
        if rate and rate > 0:
            await open_loop(target, acc, deadline, stop, cfg, rate, rng)
        else:
            tasks = [asyncio.ensure_future(closed_worker(target, acc, deadline, stop, cfg, rng))
                     for _ in range(workers)]
            await asyncio.gather(*tasks, return_exceptions=True)
        return acc.to_dict()

    try:
        result = asyncio.run(go())
    except Exception as exc:  # never hang the parent
        result = {"count": 0, "ok": 0, "http_err": 0, "conn_fail": 0, "bytes": 0,
                  "lat_sum": 0.0, "samples": [], "status": {},
                  "errors": {f"proc_{classify_error(exc)}": 1}, "buckets": {}}
    conn.send(result)
    conn.close()


def merge(parts):
    out = {"count": 0, "ok": 0, "http_err": 0, "conn_fail": 0, "bytes": 0,
           "lat_sum": 0.0, "samples": [], "status": {}, "errors": {}, "buckets": {}}
    for p in parts:
        out["count"] += p["count"]; out["ok"] += p["ok"]
        out["http_err"] += p["http_err"]; out["conn_fail"] += p["conn_fail"]
        out["bytes"] += p["bytes"]; out["lat_sum"] += p["lat_sum"]
        out["samples"].extend(p["samples"])
        for k, v in p["status"].items():
            out["status"][k] = out["status"].get(k, 0) + v
        for k, v in p["errors"].items():
            out["errors"][k] = out["errors"].get(k, 0) + v
        for b, cs in p["buckets"].items():
            slot = out["buckets"].get(b)
            if slot is None:
                out["buckets"][b] = [cs[0], cs[1]]
            else:
                slot[0] += cs[0]; slot[1] += cs[1]
    return out


def run_stage(target, stage, cfg, processes, rate):
    workers_total = stage["workers"]
    per = [workers_total // processes] * processes
    for i in range(workers_total % processes):
        per[i] += 1
    rate_per = (rate / processes) if rate else 0

    procs, conns = [], []
    started = time.perf_counter()
    for i in range(processes):
        parent, child = mp.Pipe()
        p = mp.Process(target=run_stage_in_process,
                       args=(target, stage, cfg, max(per[i], 1 if not rate else 0),
                             rate_per, random.randint(0, 2**31), child))
        p.start()
        procs.append(p); conns.append(parent)

    # live progress from the parent
    _live_progress(stage, started, conns, procs)

    parts = []
    for c, p in zip(conns, procs):
        try:
            parts.append(c.recv())
        except EOFError:
            parts.append({"count": 0, "ok": 0, "http_err": 0, "conn_fail": 0,
                          "bytes": 0, "lat_sum": 0.0, "samples": [], "status": {},
                          "errors": {"proc_crash": 1}, "buckets": {}})
        p.join(timeout=5)
        if p.is_alive():
            p.terminate()
    elapsed = time.perf_counter() - started
    return summarize(stage, merge(parts), elapsed)


def _live_progress(stage, started, conns, procs):
    if os.environ.get("STRESS_QUIET"):
        # still need to wait for processes; do it silently
        while any(p.is_alive() for p in procs) and \
                time.perf_counter() - started < stage["duration"] + 30:
            time.sleep(0.5)
        return
    while any(p.is_alive() for p in procs):
        elapsed = time.perf_counter() - started
        remaining = max(0.0, stage["duration"] - elapsed)
        sys.stdout.write(
            f"\r  [{stage['name']:<10}] {remaining:5.1f}s left | "
            f"{stage['workers']} workers x {len(procs)} proc | running...   ")
        sys.stdout.flush()
        time.sleep(0.5)
        if elapsed > stage["duration"] + 30:  # safety
            break
    sys.stdout.write("\r" + " " * 72 + "\r")
    sys.stdout.flush()


# --------------------------------------------------------------------------- #
# Summaries + reports
# --------------------------------------------------------------------------- #
def summarize(stage, m, elapsed):
    lat = sorted(m["samples"])
    total = m["count"]
    rps = total / elapsed if elapsed > 0 else 0.0
    series = []
    for sec in sorted(m["buckets"]):
        c, s = m["buckets"][sec]
        series.append({"t": sec, "rps": c, "avg_ms": round(s / c, 1) if c else 0})
    return {
        "stage": stage["name"], "workers": stage["workers"],
        "duration_s": round(stage["duration"], 1), "elapsed_s": round(elapsed, 2),
        "requests": total, "throughput_rps": round(rps, 1),
        "ok_2xx_3xx": m["ok"], "http_errors_4xx_5xx": m["http_err"],
        "connection_failures": m["conn_fail"],
        "success_rate_pct": round(100.0 * m["ok"] / total, 2) if total else 0.0,
        "mb_read": round(m["bytes"] / 1_048_576, 2),
        "avg_ms": round(m["lat_sum"] / max(m["ok"] + m["http_err"], 1), 1),
        "latency_ms": {
            "min": round(lat[0], 1) if lat else 0,
            "p50": round(percentile(lat, 50), 1),
            "p90": round(percentile(lat, 90), 1),
            "p95": round(percentile(lat, 95), 1),
            "p99": round(percentile(lat, 99), 1),
            "max": round(lat[-1], 1) if lat else 0,
        },
        "status_codes": {str(k): v for k, v in sorted(m["status"].items())},
        "errors": dict(sorted(m["errors"].items())),
        "series": series,
    }


def print_report(url, profile_name, summaries, meta):
    print("\n" + "=" * 82)
    print(f"  LOAD TEST REPORT  ->  {url}")
    print(f"  profile: {profile_name} | processes: {meta['processes']} | "
          f"mode: {meta['mode']}")
    print("=" * 82)
    print(f"{'stage':<12}{'reqs':>9}{'rps':>9}{'ok%':>8}{'p50':>7}{'p95':>8}"
          f"{'p99':>8}{'max':>8}{'errs':>8}")
    print("-" * 82)
    tr = tok = te = 0
    for s in summaries:
        errs = s["http_errors_4xx_5xx"] + s["connection_failures"]
        tr += s["requests"]; tok += s["ok_2xx_3xx"]; te += errs
        L = s["latency_ms"]
        print(f"{s['stage']:<12}{s['requests']:>9}{s['throughput_rps']:>9.0f}"
              f"{s['success_rate_pct']:>8.1f}{L['p50']:>7.0f}{L['p95']:>8.0f}"
              f"{L['p99']:>8.0f}{L['max']:>8.0f}{errs:>8}")
    print("-" * 82)
    ok = (100.0 * tok / tr) if tr else 0.0
    print(f"{'TOTAL':<12}{tr:>9}{'':>9}{ok:>8.1f}{'':>7}{'':>8}{'':>8}{'':>8}{te:>8}")
    print("=" * 82)
    print("\nInterpretation:")
    worst = max(summaries, key=lambda s: s["latency_ms"]["p95"])
    print(f"  - Peak p95 latency {worst['latency_ms']['p95']:.0f}ms at "
          f"'{worst['stage']}' ({worst['workers']} concurrent).")
    degraded = [s for s in summaries if s["success_rate_pct"] < 99.0 and s["requests"]]
    if degraded:
        f = degraded[0]
        print(f"  - Success dropped below 99% starting at '{f['stage']}' "
              f"({f['workers']} concurrent -> {f['success_rate_pct']:.1f}% ok).")
        print("    That's roughly your breaking point on this path.")
    else:
        print("  - Stayed above 99% success at every stage. Push harder with "
              "--profile heavy, more --processes, or --rate.")
    print()


def write_html(path, url, profile_name, summaries, meta):
    import html as _h
    stages_json = json.dumps(summaries)
    # Flatten a global timeline for the rps chart
    timeline = []
    offset = 0
    for s in summaries:
        for pt in s["series"]:
            timeline.append({"t": offset + pt["t"], "rps": pt["rps"],
                             "ms": pt["avg_ms"], "stage": s["stage"]})
        offset += int(round(s["elapsed_s"]))
    tl_json = json.dumps(timeline)
    doc = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Load test report</title>
<style>
 body{font:14px/1.5 system-ui,sans-serif;margin:0;background:#0f1115;color:#e7e9ee}
 .wrap{max-width:1000px;margin:0 auto;padding:24px}
 h1{font-size:20px;margin:0 0 4px} .sub{color:#9aa3b2;margin-bottom:20px}
 table{width:100%;border-collapse:collapse;margin:16px 0;font-variant-numeric:tabular-nums}
 th,td{padding:7px 10px;text-align:right;border-bottom:1px solid #232733}
 th:first-child,td:first-child{text-align:left}
 thead th{color:#9aa3b2;font-weight:600;border-bottom:2px solid #2c3240}
 tr:hover td{background:#161a22}
 .bad{color:#ff6b6b} .ok{color:#4ade80}
 .card{background:#161a22;border:1px solid #232733;border-radius:10px;padding:16px;margin:16px 0}
 svg{width:100%;height:auto;display:block} .note{color:#9aa3b2;font-size:13px}
 code{background:#232733;padding:1px 5px;border-radius:4px}
</style></head><body><div class="wrap">
<h1>Load test report</h1>
<div class="sub">__URL__ &middot; profile <b>__PROFILE__</b> &middot; __MODE__ &middot; __PROC__ process(es)</div>
<div class="card"><div id="rps"></div><div class="note">Requests/sec and average latency over the whole run.</div></div>
<div class="card"><div id="p95"></div><div class="note">p95 latency by stage &mdash; the slow tail as load climbs.</div></div>
<table id="tbl"><thead><tr><th>stage</th><th>workers</th><th>reqs</th><th>rps</th>
<th>ok%</th><th>p50</th><th>p95</th><th>p99</th><th>max</th><th>errs</th></tr></thead><tbody></tbody></table>
<div class="note">Generated by stress_test.py &mdash; a load tester for authorized capacity testing.</div>
</div>
<script>
const STAGES=__STAGES__, TL=__TIMELINE__;
function el(t,a){const e=document.createElementNS("http://www.w3.org/2000/svg",t);for(const k in a)e.setAttribute(k,a[k]);return e;}
function lineChart(id,data,xk,yk,color,ylab){
 const W=920,H=240,P=44;const svg=el("svg",{viewBox:`0 0 ${W} ${H}`});
 const xs=data.map(d=>d[xk]),ys=data.map(d=>d[yk]);
 const xmax=Math.max(1,...xs),ymax=Math.max(1,...ys);
 const X=v=>P+(W-P-10)*v/xmax, Y=v=>H-P-(H-2*P)*v/ymax;
 for(let i=0;i<=4;i++){const yy=P+(H-2*P)*i/4,val=Math.round(ymax*(1-i/4));
  svg.appendChild(el("line",{x1:P,y1:yy,x2:W-10,y2:yy,stroke:"#232733"}));
  const tx=el("text",{x:P-8,y:yy+4,fill:"#9aa3b2","font-size":11,"text-anchor":"end"});tx.textContent=val;svg.appendChild(tx);}
 let d="";data.forEach((pt,i)=>{d+=(i?"L":"M")+X(pt[xk])+" "+Y(pt[yk]);});
 svg.appendChild(el("path",{d,fill:"none",stroke:color,"stroke-width":2}));
 const lab=el("text",{x:P,y:16,fill:"#9aa3b2","font-size":12});lab.textContent=ylab;svg.appendChild(lab);
 document.getElementById(id).appendChild(svg);
}
function barChart(id,data,color){
 const W=920,H=240,P=44,n=data.length;const svg=el("svg",{viewBox:`0 0 ${W} ${H}`});
 const ymax=Math.max(1,...data.map(d=>d.latency_ms.p95));
 const bw=(W-P-10)/n*0.6,gap=(W-P-10)/n;
 for(let i=0;i<=4;i++){const yy=P+(H-2*P)*i/4,val=Math.round(ymax*(1-i/4));
  svg.appendChild(el("line",{x1:P,y1:yy,x2:W-10,y2:yy,stroke:"#232733"}));
  const tx=el("text",{x:P-8,y:yy+4,fill:"#9aa3b2","font-size":11,"text-anchor":"end"});tx.textContent=val+"ms";svg.appendChild(tx);}
 data.forEach((s,i)=>{const h=(H-2*P)*s.latency_ms.p95/ymax;const x=P+gap*i+ (gap-bw)/2;
  svg.appendChild(el("rect",{x,y:H-P-h,width:bw,height:h,fill:color,rx:3}));
  const tx=el("text",{x:x+bw/2,y:H-P+16,fill:"#9aa3b2","font-size":11,"text-anchor":"middle"});tx.textContent=s.stage;svg.appendChild(tx);});
 document.getElementById(id).appendChild(svg);
}
lineChart("rps",TL,"t","rps","#4ade80","requests / sec");
barChart("p95",STAGES,"#60a5fa");
const tb=document.querySelector("#tbl tbody");
STAGES.forEach(s=>{const errs=s.http_errors_4xx_5xx+s.connection_failures;
 const tr=document.createElement("tr");
 tr.innerHTML=`<td>${s.stage}</td><td>${s.workers}</td><td>${s.requests}</td>
 <td>${s.throughput_rps.toFixed(0)}</td>
 <td class="${s.success_rate_pct<99?'bad':'ok'}">${s.success_rate_pct.toFixed(1)}</td>
 <td>${s.latency_ms.p50}</td><td>${s.latency_ms.p95}</td><td>${s.latency_ms.p99}</td>
 <td>${s.latency_ms.max}</td><td class="${errs?'bad':''}">${errs}</td>`;
 tb.appendChild(tr);});
</script></body></html>"""
    doc = (doc.replace("__URL__", _h.escape(url))
              .replace("__PROFILE__", _h.escape(profile_name))
              .replace("__MODE__", _h.escape(meta["mode"]))
              .replace("__PROC__", str(meta["processes"]))
              .replace("__STAGES__", stages_json)
              .replace("__TIMELINE__", tl_json))
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(doc)


def write_csv(path, summaries):
    import csv
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["stage", "second", "requests_in_second", "avg_latency_ms"])
        for s in summaries:
            for pt in s["series"]:
                w.writerow([s["stage"], pt["t"], pt["rps"], pt["avg_ms"]])


def evaluate_slo(summaries, slo_p95, slo_p99, slo_err):
    """Return (passed, list_of_breach_strings) against the given thresholds."""
    breaches = []
    for s in summaries:
        if not s["requests"]:
            continue
        err_pct = 100.0 - s["success_rate_pct"]
        if slo_p95 and s["latency_ms"]["p95"] > slo_p95:
            breaches.append(f"{s['stage']}: p95 {s['latency_ms']['p95']:.0f}ms > {slo_p95:.0f}ms")
        if slo_p99 and s["latency_ms"]["p99"] > slo_p99:
            breaches.append(f"{s['stage']}: p99 {s['latency_ms']['p99']:.0f}ms > {slo_p99:.0f}ms")
        if slo_err and err_pct > slo_err:
            breaches.append(f"{s['stage']}: errors {err_pct:.1f}% > {slo_err:.1f}%")
    return (len(breaches) == 0), breaches


def report_capacity(summaries):
    """Print the highest stage that still stayed healthy — the practical capacity."""
    healthy = [s for s in summaries
               if s["requests"] and s["success_rate_pct"] >= 99.0]
    if healthy:
        best = max(healthy, key=lambda s: s["throughput_rps"])
        print(f"  - Highest sustained throughput at >=99% success: "
              f"~{best['throughput_rps']:.0f} req/s at '{best['stage']}' "
              f"({best['workers']} concurrent, p95 {best['latency_ms']['p95']:.0f}ms).")


def run_find_capacity(target, cfg, processes, start, step, step_seconds,
                      max_rate, slo_p95, slo_p99, slo_err):
    """Auto-ramp the open-loop request rate step by step until the target breaches
    an SLO, then report the highest rate it served healthily. This is a capacity
    *search*, not a sustained flood: each step runs briefly and it stops the
    moment the site starts to degrade."""
    print("Capacity search: ramping request rate until an SLO breaks.")
    print(f"  start {start:.0f}/s, +{step:.0f}/s each {step_seconds:.0f}s, "
          f"cap {max_rate:.0f}/s")
    print(f"  SLO: p95<{slo_p95:.0f}ms p99<{slo_p99:.0f}ms errors<{slo_err:.1f}%\n")

    summaries = []
    last_healthy = None
    rate = start
    while rate <= max_rate:
        stage = {"name": f"{int(rate)}rps", "workers": 0, "duration": step_seconds}
        s = run_stage(target, stage, cfg, processes, rate)
        summaries.append(s)
        err_pct = 100.0 - s["success_rate_pct"]
        L = s["latency_ms"]
        breach = []
        if L["p95"] > slo_p95:
            breach.append(f"p95 {L['p95']:.0f}>{slo_p95:.0f}ms")
        if L["p99"] > slo_p99:
            breach.append(f"p99 {L['p99']:.0f}>{slo_p99:.0f}ms")
        if err_pct > slo_err:
            breach.append(f"errors {err_pct:.1f}>{slo_err:.1f}%")
        status = "BREACH " + ", ".join(breach) if breach else "ok"
        print(f"  {int(rate):>7}/s target | served {s['throughput_rps']:>7.0f}/s | "
              f"p95 {L['p95']:>6.0f}ms | p99 {L['p99']:>6.0f}ms | "
              f"err {err_pct:>5.1f}% | {status}")
        if breach:
            break
        last_healthy = s
        rate += step

    print()
    if last_healthy is None:
        print("Capacity: the very first step already breached the SLO — lower "
              "--start-rate, or the site is degrading immediately.")
    else:
        L = last_healthy["latency_ms"]
        print(f"CAPACITY: ~{last_healthy['throughput_rps']:.0f} req/s sustained "
              f"within SLO (target {last_healthy['stage']}, "
              f"p95 {L['p95']:.0f}ms, p99 {L['p99']:.0f}ms).")
    return summaries


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def build_target(url):
    p = urlparse(url)
    if p.scheme not in ("http", "https"):
        raise ValueError("URL must start with http:// or https://")
    if not p.hostname:
        raise ValueError("Could not parse a hostname from the URL")
    return {
        "host": p.hostname,
        "port": p.port or (443 if p.scheme == "https" else 80),
        "base_path": p.path or "/",
        "ssl_ctx": ssl.create_default_context() if p.scheme == "https" else None,
    }


def load_stages(args):
    if args.config:
        with open(args.config, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        stages = data["stages"] if isinstance(data, dict) else data
        for s in stages:
            s.setdefault("name", "stage"); s.setdefault("duration", 10)
            s.setdefault("workers", 10)
        return stages, os.path.basename(args.config)
    if args.rate and args.duration:
        return ([{"name": "open-loop", "workers": 0, "duration": args.duration}],
                f"rate={args.rate}/s")
    return PROFILES[args.profile], args.profile


def main():
    ap = argparse.ArgumentParser(description="Advanced staged HTTP load tester (authorized targets only).")
    ap.add_argument("url")
    ap.add_argument("--i-own-this", action="store_true")
    ap.add_argument("--profile", choices=list(PROFILES), default="standard")
    ap.add_argument("--config")
    ap.add_argument("--processes", type=int, default=1,
                    help="Worker processes (spread load across CPU cores).")
    ap.add_argument("--rate", type=float, default=0,
                    help="Open-loop target requests/sec (coordinated-omission corrected).")
    ap.add_argument("--duration", type=float, default=0,
                    help="Duration for --rate open-loop mode (seconds).")
    ap.add_argument("--max-inflight", type=int, default=5000,
                    help="Cap on concurrent in-flight requests in open-loop mode.")
    ap.add_argument("--path", action="append", default=[],
                    help="Weighted endpoint 'PATH:WEIGHT' (repeatable), e.g. /api:3")
    ap.add_argument("--method", default="GET")
    ap.add_argument("--header", action="append", default=[])
    ap.add_argument("--body")
    ap.add_argument("--timeout", type=float, default=15.0)
    ap.add_argument("--user-agent", default=DEFAULT_UA)
    ap.add_argument("--no-keepalive", action="store_true",
                    help="Open a fresh connection per request (default: reuse).")
    ap.add_argument("--pipeline", type=int, default=1,
                    help="HTTP/1.1 pipeline depth: requests sent per connection "
                         "before reading responses (default 1). Higher = more "
                         "throughput per socket on keep-alive servers.")
    ap.add_argument("--report", help="Write JSON report to this path.")
    ap.add_argument("--html", help="Write a self-contained HTML report.")
    ap.add_argument("--csv", help="Write the per-second time-series to a CSV file.")
    ap.add_argument("--slo-p95", type=float, default=0,
                    help="Fail the run (exit 1) if any stage's p95 latency (ms) exceeds this.")
    ap.add_argument("--slo-p99", type=float, default=0,
                    help="Fail the run (exit 1) if any stage's p99 latency (ms) exceeds this.")
    ap.add_argument("--slo-error-pct", type=float, default=0,
                    help="Fail the run (exit 1) if any stage's error rate (percent) exceeds this.")
    ap.add_argument("--find-capacity", action="store_true",
                    help="Auto-ramp the request rate until an SLO breaks and report "
                         "the highest healthy req/s (capacity search).")
    ap.add_argument("--start-rate", type=float, default=200,
                    help="find-capacity: starting request rate (req/s).")
    ap.add_argument("--step-rate", type=float, default=200,
                    help="find-capacity: rate increase per step (req/s).")
    ap.add_argument("--step-seconds", type=float, default=10,
                    help="find-capacity: duration of each step (seconds).")
    ap.add_argument("--max-rate", type=float, default=20000,
                    help="find-capacity: stop ramping at this rate (req/s).")
    ap.add_argument("--uvloop", action="store_true",
                    help="Use the uvloop event loop if installed (pip install uvloop) "
                         "for 2-4x higher throughput. Falls back to asyncio if missing.")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    try:
        target = build_target(args.url)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr); return 2

    if not is_authorized(target["host"], load_allowlist()):
        print(f"REFUSED: '{target['host']}' is not authorized.\n", file=sys.stderr)
        print("This tool only runs against hosts you have confirmed you control.", file=sys.stderr)
        print(f"Add the host to {ALLOWLIST_FILE} (one per line), then re-run.", file=sys.stderr)
        return 3
    if not args.i_own_this:
        print("REFUSED: pass --i-own-this to confirm you're authorized to test this target.",
              file=sys.stderr)
        return 3

    # endpoints
    paths, weights = [], []
    for spec in args.path:
        if ":" in spec and spec.rsplit(":", 1)[1].isdigit():
            pth, w = spec.rsplit(":", 1); paths.append(pth); weights.append(int(w))
        else:
            paths.append(spec); weights.append(1)
    if not paths:
        paths = [target["base_path"]]; weights = [1]

    extra_headers = []
    for h in args.header:
        if ":" in h:
            k, v = h.split(":", 1); extra_headers.append((k.strip(), v.strip()))

    cfg = {
        "timeout": args.timeout, "ua": args.user_agent, "method": args.method.upper(),
        "headers": extra_headers, "body": args.body.encode("utf-8") if args.body else b"",
        "keepalive": not args.no_keepalive, "paths": paths, "weights": weights,
        "max_inflight": args.max_inflight, "pipeline": max(1, args.pipeline),
        "uvloop": args.uvloop,
    }
    if args.uvloop:
        try:
            import uvloop  # noqa: F401
            print("uvloop: enabled (faster event loop)")
        except Exception:
            print("uvloop: requested but not installed — run 'pip3 install uvloop'; "
                  "using default asyncio for now")
    if args.quiet:
        os.environ["STRESS_QUIET"] = "1"

    # Capacity-search mode short-circuits the normal staged run.
    if args.find_capacity:
        processes = max(1, args.processes)
        print(f"Target : {args.url}  (host {target['host']}, authorized)")
        print(f"Mode   : find-capacity | {processes} process(es) | "
              f"keepalive {'on' if cfg['keepalive'] else 'off'}"
              + (f" | pipeline {cfg['pipeline']}" if cfg['pipeline'] > 1 else "") + "\n")
        slo_p95 = args.slo_p95 or 1000.0
        slo_p99 = args.slo_p99 or 2000.0
        slo_err = args.slo_error_pct or 2.0
        summaries = run_find_capacity(target, cfg, processes, args.start_rate,
                                      args.step_rate, args.step_seconds,
                                      args.max_rate, slo_p95, slo_p99, slo_err)
        meta = {"processes": processes, "mode": "find-capacity"}
        if summaries and args.csv:
            write_csv(args.csv, summaries); print(f"CSV time-series -> {args.csv}")
        if summaries and args.html:
            write_html(args.html, args.url, "find-capacity", summaries, meta)
            print(f"HTML report -> {args.html}")
        return 0

    stages, profile_name = load_stages(args)
    processes = max(1, args.processes)
    mode = "open-loop (CO-corrected)" if args.rate else "closed-loop"
    total_s = sum(s["duration"] for s in stages)

    print(f"Target : {args.url}  (host {target['host']}, authorized)")
    print(f"Profile: {profile_name} | {len(stages)} stage(s) | ~{total_s:.0f}s | "
          f"{processes} process(es) | {mode}")
    print(f"Paths  : " + ", ".join(f"{p}(w{w})" for p, w in zip(paths, weights)))
    print(f"Keepalive: {'on' if cfg['keepalive'] else 'off'}"
          + (f" | pipeline {cfg['pipeline']}" if cfg['pipeline'] > 1 else "")
          + (f" | rate {args.rate}/s" if args.rate else "") + "\n")

    stop_all = {"flag": False}

    def on_sigint(*_):
        stop_all["flag"] = True
        print("\n[stopping...]\n")
    try:
        signal.signal(signal.SIGINT, on_sigint)
    except Exception:
        pass

    summaries = []
    for stage in stages:
        if stop_all["flag"]:
            break
        summaries.append(run_stage(target, stage, cfg, processes, args.rate))

    meta = {"processes": processes, "mode": mode}
    if summaries:
        print_report(args.url, profile_name, summaries, meta)
        if args.report:
            with open(args.report, "w", encoding="utf-8") as fh:
                json.dump({"target": args.url, "host": target["host"],
                           "profile": profile_name, "meta": meta,
                           "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
                           "stages": summaries}, fh, indent=2)
            print(f"JSON report -> {args.report}")
        if args.html:
            write_html(args.html, args.url, profile_name, summaries, meta)
            print(f"HTML report -> {args.html}")
        if args.csv:
            write_csv(args.csv, summaries)
            print(f"CSV time-series -> {args.csv}")

        report_capacity(summaries)

        if args.slo_p95 or args.slo_p99 or args.slo_error_pct:
            passed, breaches = evaluate_slo(summaries, args.slo_p95,
                                            args.slo_p99, args.slo_error_pct)
            print()
            if passed:
                print("SLO: PASS - every stage stayed within the thresholds.")
            else:
                print("SLO: FAIL")
                for b in breaches:
                    print(f"  - {b}")
                return 1
    return 0


if __name__ == "__main__":
    try:
        mp.set_start_method("fork")
    except (RuntimeError, ValueError):
        pass
    sys.exit(main())
