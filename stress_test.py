#!/usr/bin/env python3
"""
stress_test.py - Staged HTTP load / stress tester.

A dependency-free (Python standard library only) async load generator built for
capacity testing sites you own or are explicitly authorized to test. It ramps
traffic through configurable STAGES and reports latency percentiles, throughput,
and error rates so you can find where your site starts to degrade.

This is a LOAD TESTER, not an attack tool: it identifies itself honestly via
User-Agent, does not spoof source addresses, does not rotate proxies, and refuses
to run against any host you have not placed on the allowlist.

Runs on anything with Python 3.8+ (including a Chromebook Linux container).
No pip install required.

Usage:
    python3 stress_test.py https://script.ceo/ --i-own-this
    python3 stress_test.py https://script.ceo/ --i-own-this --profile heavy
    python3 stress_test.py https://script.ceo/ --i-own-this --config stages.json
    python3 stress_test.py https://script.ceo/ --i-own-this --report results.json
"""

import argparse
import asyncio
import json
import math
import os
import signal
import ssl
import sys
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse

ALLOWLIST_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "allowlist.txt")
DEFAULT_UA = "stress_test.py/1.0 (+https://github.com/ load-tester; authorized capacity testing)"

# --------------------------------------------------------------------------- #
# Built-in stage profiles. Each stage: name, workers (max concurrent in-flight
# requests), duration in seconds, and an optional per-stage rps cap (None = uncapped).
# --------------------------------------------------------------------------- #
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
}


# --------------------------------------------------------------------------- #
# Stats
# --------------------------------------------------------------------------- #
@dataclass
class StageStats:
    name: str
    workers: int
    duration: float
    latencies_ms: list = field(default_factory=list)
    status_counts: dict = field(default_factory=dict)
    errors: dict = field(default_factory=dict)
    bytes_read: int = 0
    started: float = 0.0
    ended: float = 0.0

    @property
    def total(self):
        return len(self.latencies_ms) + sum(self.errors.values())

    @property
    def ok(self):
        return sum(c for s, c in self.status_counts.items() if 200 <= s < 400)

    @property
    def bad(self):
        return sum(c for s, c in self.status_counts.items() if s >= 400)

    @property
    def failed(self):
        return sum(self.errors.values())

    @property
    def elapsed(self):
        return max(self.ended - self.started, 1e-9)

    @property
    def rps(self):
        return self.total / self.elapsed

    def record_success(self, status, latency_ms, nbytes):
        self.latencies_ms.append(latency_ms)
        self.status_counts[status] = self.status_counts.get(status, 0) + 1
        self.bytes_read += nbytes

    def record_error(self, kind):
        self.errors[kind] = self.errors.get(kind, 0) + 1


def percentile(sorted_vals, pct):
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    k = (len(sorted_vals) - 1) * (pct / 100.0)
    lo = math.floor(k)
    hi = math.ceil(k)
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
                if not line or line.startswith("#"):
                    continue
                hosts.add(line.lower())
    return hosts


def is_authorized(host, allowlist):
    host = (host or "").lower()
    if host in ("localhost", "127.0.0.1", "::1"):
        return True
    # private ranges are always allowed (your own lab)
    if host.startswith(("10.", "192.168.", "172.16.", "172.17.", "172.18.",
                        "172.19.", "172.2", "172.30.", "172.31.")):
        return True
    if host in allowlist:
        return True
    # allow subdomains of an allowlisted apex (e.g. www.script.ceo for script.ceo)
    for h in allowlist:
        if host == h or host.endswith("." + h):
            return True
    return False


# --------------------------------------------------------------------------- #
# HTTP request (asyncio streams, Connection: close so EOF marks end of body)
# --------------------------------------------------------------------------- #
async def do_request(target, timeout, ua, method, extra_headers, body_bytes):
    host = target["host"]
    port = target["port"]
    path = target["path"]
    ssl_ctx = target["ssl_ctx"]

    started = time.perf_counter()
    reader = writer = None
    try:
        conn = asyncio.open_connection(
            host, port, ssl=ssl_ctx,
            server_hostname=host if ssl_ctx else None,
        )
        reader, writer = await asyncio.wait_for(conn, timeout=timeout)

        lines = [
            f"{method} {path} HTTP/1.1",
            f"Host: {host}",
            f"User-Agent: {ua}",
            "Accept: */*",
            "Connection: close",
        ]
        for k, v in extra_headers:
            lines.append(f"{k}: {v}")
        if body_bytes:
            lines.append(f"Content-Length: {len(body_bytes)}")
        req = ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")
        if body_bytes:
            req += body_bytes
        writer.write(req)
        await asyncio.wait_for(writer.drain(), timeout=timeout)

        status_line = await asyncio.wait_for(reader.readline(), timeout=timeout)
        if not status_line:
            return ("error", "empty_response", 0, 0)
        parts = status_line.decode("latin-1", "replace").split(" ", 2)
        status = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0

        # Read (and discard) the rest of the response until the server closes it.
        nbytes = 0
        while True:
            chunk = await asyncio.wait_for(reader.read(65536), timeout=timeout)
            if not chunk:
                break
            nbytes += len(chunk)

        latency_ms = (time.perf_counter() - started) * 1000.0
        return ("ok", status, latency_ms, nbytes)

    except asyncio.TimeoutError:
        return ("error", "timeout", 0, 0)
    except (ConnectionResetError, ConnectionRefusedError, BrokenPipeError):
        return ("error", "conn_reset", 0, 0)
    except ssl.SSLError:
        return ("error", "ssl_error", 0, 0)
    except OSError as exc:
        return ("error", f"os_{exc.errno}", 0, 0)
    except Exception:
        return ("error", "unknown", 0, 0)
    finally:
        if writer is not None:
            try:
                writer.close()
            except Exception:
                pass


# --------------------------------------------------------------------------- #
# Stage runner
# --------------------------------------------------------------------------- #
class StopFlag:
    def __init__(self):
        self.stop = False


async def worker(target, stats, deadline, stopflag, req_kwargs, pace_interval):
    while time.perf_counter() < deadline and not stopflag.stop:
        cycle_start = time.perf_counter()
        kind, a, b, c = await do_request(target, **req_kwargs)
        if kind == "ok":
            stats.record_success(a, b, c)
        else:
            stats.record_error(a)
        if pace_interval > 0:
            elapsed = time.perf_counter() - cycle_start
            if elapsed < pace_interval:
                await asyncio.sleep(pace_interval - elapsed)


async def run_stage(stage, target, stopflag, req_kwargs, quiet):
    stats = StageStats(name=stage["name"], workers=stage["workers"],
                       duration=stage["duration"])
    rps_cap = stage.get("rps")
    pace_interval = (stage["workers"] / rps_cap) if rps_cap else 0.0

    stats.started = time.perf_counter()
    deadline = stats.started + stage["duration"]

    tasks = [
        asyncio.create_task(
            worker(target, stats, deadline, stopflag, req_kwargs, pace_interval)
        )
        for _ in range(stage["workers"])
    ]

    # live progress
    progress = asyncio.create_task(
        _progress(stats, deadline, stopflag, stage, quiet)
    )

    await asyncio.gather(*tasks, return_exceptions=True)
    stopflag_local = False
    progress.cancel()
    try:
        await progress
    except asyncio.CancelledError:
        pass
    stats.ended = time.perf_counter()
    return stats


async def _progress(stats, deadline, stopflag, stage, quiet):
    if quiet:
        return
    last_total = 0
    while True:
        await asyncio.sleep(1.0)
        now = time.perf_counter()
        remaining = max(0, deadline - now)
        cur = stats.total
        inst_rps = cur - last_total
        last_total = cur
        avg = (sum(stats.latencies_ms[-500:]) / len(stats.latencies_ms[-500:])
               if stats.latencies_ms else 0.0)
        sys.stdout.write(
            f"\r  [{stage['name']:<10}] {remaining:4.0f}s left | "
            f"reqs {cur:>7} | ~{inst_rps:>5}/s | ok {stats.ok:>7} | "
            f"err {stats.bad + stats.failed:>6} | avg {avg:6.0f}ms   "
        )
        sys.stdout.flush()
        if now >= deadline or stopflag.stop:
            break


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #
def summarize_stage(stats):
    lat = sorted(stats.latencies_ms)
    return {
        "stage": stats.name,
        "workers": stats.workers,
        "duration_s": round(stats.duration, 1),
        "elapsed_s": round(stats.elapsed, 2),
        "requests": stats.total,
        "throughput_rps": round(stats.rps, 1),
        "ok_2xx_3xx": stats.ok,
        "http_errors_4xx_5xx": stats.bad,
        "connection_failures": stats.failed,
        "success_rate_pct": round(100.0 * stats.ok / stats.total, 2) if stats.total else 0.0,
        "mb_read": round(stats.bytes_read / 1_048_576, 2),
        "latency_ms": {
            "min": round(lat[0], 1) if lat else 0,
            "p50": round(percentile(lat, 50), 1),
            "p90": round(percentile(lat, 90), 1),
            "p95": round(percentile(lat, 95), 1),
            "p99": round(percentile(lat, 99), 1),
            "max": round(lat[-1], 1) if lat else 0,
        },
        "status_codes": dict(sorted(stats.status_counts.items())),
        "errors": dict(sorted(stats.errors.items())),
    }


def print_report(target_url, profile_name, stage_summaries):
    print("\n")
    print("=" * 78)
    print(f"  LOAD TEST REPORT  ->  {target_url}")
    print(f"  profile: {profile_name}")
    print("=" * 78)
    header = f"{'stage':<12}{'reqs':>9}{'rps':>9}{'ok%':>8}{'p50':>8}{'p95':>8}{'p99':>9}{'errs':>8}"
    print(header)
    print("-" * 78)
    tot_reqs = tot_ok = tot_err = 0
    for s in stage_summaries:
        errs = s["http_errors_4xx_5xx"] + s["connection_failures"]
        tot_reqs += s["requests"]
        tot_ok += s["ok_2xx_3xx"]
        tot_err += errs
        print(f"{s['stage']:<12}{s['requests']:>9}{s['throughput_rps']:>9.0f}"
              f"{s['success_rate_pct']:>8.1f}{s['latency_ms']['p50']:>8.0f}"
              f"{s['latency_ms']['p95']:>8.0f}{s['latency_ms']['p99']:>9.0f}{errs:>8}")
    print("-" * 78)
    overall_ok = (100.0 * tot_ok / tot_reqs) if tot_reqs else 0.0
    print(f"{'TOTAL':<12}{tot_reqs:>9}{'':>9}{overall_ok:>8.1f}{'':>8}{'':>8}{'':>9}{tot_err:>8}")
    print("=" * 78)

    # A little interpretation
    print("\nInterpretation:")
    worst = max(stage_summaries, key=lambda s: s["latency_ms"]["p95"])
    print(f"  - Highest p95 latency: {worst['latency_ms']['p95']:.0f}ms during "
          f"'{worst['stage']}' ({worst['workers']} concurrent).")
    degraded = [s for s in stage_summaries if s["success_rate_pct"] < 99.0 and s["requests"] > 0]
    if degraded:
        first = degraded[0]
        print(f"  - Errors first became significant at '{first['stage']}' "
              f"({first['workers']} concurrent, {first['success_rate_pct']:.1f}% ok).")
        print("    That concurrency level is roughly where your site starts to strain.")
    else:
        print("  - No stage dropped below 99% success. Site handled every stage cleanly;")
        print("    try --profile heavy (or a custom --config) to push harder.")
    print()


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def build_target(url):
    p = urlparse(url)
    if p.scheme not in ("http", "https"):
        raise ValueError("URL must start with http:// or https://")
    host = p.hostname
    if not host:
        raise ValueError("Could not parse a hostname from the URL")
    port = p.port or (443 if p.scheme == "https" else 80)
    path = p.path or "/"
    if p.query:
        path += "?" + p.query
    ssl_ctx = None
    if p.scheme == "https":
        ssl_ctx = ssl.create_default_context()
    return {"host": host, "port": port, "path": path, "ssl_ctx": ssl_ctx, "scheme": p.scheme}


def load_stages(args):
    if args.config:
        with open(args.config, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        stages = data["stages"] if isinstance(data, dict) else data
        for s in stages:
            s.setdefault("name", "stage")
            s.setdefault("duration", 10)
            s.setdefault("workers", 10)
        return stages, os.path.basename(args.config)
    return PROFILES[args.profile], args.profile


def main():
    ap = argparse.ArgumentParser(
        description="Staged HTTP load / stress tester (authorized targets only).")
    ap.add_argument("url", help="Target URL, e.g. https://script.ceo/")
    ap.add_argument("--i-own-this", action="store_true",
                    help="Confirm you own or are authorized to load test the target.")
    ap.add_argument("--profile", choices=list(PROFILES), default="standard",
                    help="Built-in stage profile (default: standard).")
    ap.add_argument("--config", help="Path to a JSON file defining custom stages.")
    ap.add_argument("--method", default="GET", help="HTTP method (default GET).")
    ap.add_argument("--header", action="append", default=[],
                    help="Extra header 'Name: value' (repeatable).")
    ap.add_argument("--body", help="Request body (string) for POST/PUT.")
    ap.add_argument("--timeout", type=float, default=15.0,
                    help="Per-request timeout in seconds (default 15).")
    ap.add_argument("--user-agent", default=DEFAULT_UA, help="Override User-Agent.")
    ap.add_argument("--report", help="Write full JSON report to this path.")
    ap.add_argument("--quiet", action="store_true", help="Suppress live progress.")
    args = ap.parse_args()

    try:
        target = build_target(args.url)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    allowlist = load_allowlist()
    if not is_authorized(target["host"], allowlist):
        print(f"REFUSED: '{target['host']}' is not authorized.\n", file=sys.stderr)
        print("This tool only runs against hosts you have confirmed you control.", file=sys.stderr)
        print(f"Add the host to {ALLOWLIST_FILE} (one host per line), then re-run.",
              file=sys.stderr)
        return 3
    if not args.i_own_this:
        print("REFUSED: pass --i-own-this to confirm you are authorized to test this target.",
              file=sys.stderr)
        return 3

    stages, profile_name = load_stages(args)

    extra_headers = []
    for h in args.header:
        if ":" in h:
            k, v = h.split(":", 1)
            extra_headers.append((k.strip(), v.strip()))
    body_bytes = args.body.encode("utf-8") if args.body else b""

    req_kwargs = {
        "timeout": args.timeout,
        "ua": args.user_agent,
        "method": args.method.upper(),
        "extra_headers": extra_headers,
        "body_bytes": body_bytes,
    }

    total_seconds = sum(s["duration"] for s in stages)
    print(f"Target : {args.url}  (host {target['host']}, authorized)")
    print(f"Profile: {profile_name}  |  {len(stages)} stages  |  ~{total_seconds}s total")
    print(f"Stages : " + " -> ".join(f"{s['name']}({s['workers']}w/{s['duration']}s)"
                                      for s in stages))
    print("Press Ctrl+C to stop early and still get a report.\n")

    stopflag = StopFlag()

    async def runner():
        summaries = []
        for stage in stages:
            if stopflag.stop:
                break
            stats = await run_stage(stage, target, stopflag, req_kwargs, args.quiet)
            summaries.append(summarize_stage(stats))
        return summaries

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def handle_sigint():
        stopflag.stop = True
        print("\n\n[stopping after current stage...]\n")
    try:
        loop.add_signal_handler(signal.SIGINT, handle_sigint)
    except (NotImplementedError, RuntimeError):
        pass

    try:
        summaries = loop.run_until_complete(runner())
    except KeyboardInterrupt:
        stopflag.stop = True
        summaries = []
    finally:
        loop.close()

    if summaries:
        print_report(args.url, profile_name, summaries)
        if args.report:
            report = {
                "target": args.url,
                "host": target["host"],
                "profile": profile_name,
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "stages": summaries,
            }
            with open(args.report, "w", encoding="utf-8") as fh:
                json.dump(report, fh, indent=2)
            print(f"Full JSON report written to {args.report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
