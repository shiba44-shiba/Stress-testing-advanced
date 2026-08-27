# stress_test.py — Advanced HTTP Load & Stress Tester

A single-file, **dependency-free** (Python standard library only) HTTP load
generator for measuring how much traffic your own website, API, or server can
handle before it slows down or starts failing. It ramps traffic through
configurable **stages**, corrects for coordinated omission, and reports latency
percentiles, throughput, and error rates — with text, JSON, CSV, and a
self-contained HTML report.

Runs anywhere Python 3.8+ runs: **macOS, Linux, and Chromebook** (Crostini
Linux container). No `pip install` required (optional `uvloop` for extra speed).

---

## Table of contents

1. [What this is (and isn't)](#what-this-is-and-isnt)
2. [Legal & safety](#legal--safety)
3. [Install](#install)
4. [The allowlist (required)](#the-allowlist-required)
5. [Quick start](#quick-start)
6. [Core concepts](#core-concepts)
7. [Modes of operation](#modes-of-operation)
8. [Full option reference](#full-option-reference)
9. [Built-in profiles](#built-in-profiles)
10. [Custom stage files](#custom-stage-files)
11. [Testing APIs](#testing-apis)
12. [Testing by IP (IPv4 & IPv6)](#testing-by-ip-ipv4--ipv6)
13. [Finding capacity automatically](#finding-capacity-automatically)
14. [SLO thresholds & CI](#slo-thresholds--ci)
15. [Helper scripts](#helper-scripts)
16. [Reports & output files](#reports--output-files)
17. [Reading the results](#reading-the-results)
18. [Getting maximum throughput](#getting-maximum-throughput)
19. [How it works internally](#how-it-works-internally)
20. [Troubleshooting](#troubleshooting)
21. [FAQ](#faq)

---

## What this is (and isn't)

**It is** a capacity/stress-testing tool. You point it at a server you own,
turn up the load, and it tells you where performance degrades — so you can size
your infrastructure, catch regressions, and find bottlenecks.

**It is not** an attack tool. It identifies itself honestly with a real
`User-Agent`, does not spoof source addresses, does not rotate proxies, and does
nothing to evade rate limits, WAFs, or DDoS protection. It refuses to run against
any host that isn't on your allowlist. If a target (e.g. behind Cloudflare)
rate-limits or blocks it, that is the target's protection working correctly —
not a limitation to be "fixed."

---

## Legal & safety

> **Only test systems you own or have explicit written permission to test.**

Sending load at a server you don't control — even briefly, even "just to see" —
is illegal in most jurisdictions (in the US, the Computer Fraud and Abuse Act;
elsewhere, equivalents). "I was only testing" is not a defense. The allowlist in
this tool exists to make you consciously confirm each target.

The tool is deliberately a **single, honest source**. It cannot and will not
distribute load across many machines or IPs — that would be the mechanism of a
denial-of-service attack, which this is not.

---

## Install

No dependencies beyond Python 3.8+.

### macOS

```bash
# Python 3 ships via the Xcode command-line tools:
xcode-select --install        # if `python3` is missing
cd ~/Downloads/stress_test_tool   # wherever you unzipped it
python3 stress_test.py --help
```

### Linux / Chromebook (Crostini)

```bash
sudo apt-get update && sudo apt-get install -y python3   # usually already present
python3 stress_test.py --help
```

### Optional: uvloop (2–4× faster)

```bash
pip3 install uvloop
# then add --uvloop to any command
```

---

## The allowlist (required)

The tool reads `allowlist.txt` **from the same directory as `stress_test.py`**.
It runs only against hosts listed there. One host per line; lines starting with
`#` are comments.

```
# allowlist.txt
script.ceo
api.script.ceo
203.0.113.45
2001:db8::1234
```

Rules:

- **Host only** — no `https://`, no path, no port. Just `script.ceo` or the IP.
- A listed apex covers its subdomains: `script.ceo` also allows `www.script.ceo`.
- **Always allowed without an entry:** `localhost`, `127.0.0.1`, `::1`, and the
  private IPv4 ranges `10.x`, `192.168.x`, `172.16–31.x`.
- A **public** IP or hostname must be added explicitly — that's your ownership
  confirmation.

Add a host:

```bash
echo "script.ceo" >> allowlist.txt      # append (won't erase existing lines)
cat allowlist.txt                        # check what's in it
```

Every run also requires the explicit `--i-own-this` flag.

---

## Quick start

```bash
# 1. Add your host (once)
echo "script.ceo" >> allowlist.txt

# 2. Simple staged run
python3 stress_test.py https://script.ceo/ --i-own-this

# 3. Harder, using all cores + pipelining, with an HTML report
python3 stress_test.py https://script.ceo/ --i-own-this \
    --profile heavy --processes 8 --pipeline 16 --html report.html

# 4. Auto-find the breaking point
python3 stress_test.py https://script.ceo/ --i-own-this \
    --find-capacity --slo-p99 800 --slo-error-pct 2
```

Press **Ctrl+C** at any time to stop early — you still get a report for whatever
ran.

---

## Core concepts

### Stages
A test is a sequence of **stages**. Each stage holds a fixed number of
concurrent **workers** (in-flight requests) for a **duration**. Ramping stages
let you watch latency and errors climb as load increases, so you can find the
exact point your server starts to strain.

### Workers vs. rate (closed vs. open loop)
- **Closed loop** (`--profile` / `workers`): a fixed number of workers each fire
  a request, wait for the response, then fire again. Load *adapts* to the
  server's speed — if it slows down, you send fewer requests.
- **Open loop** (`--rate`): requests are scheduled at a fixed rate regardless of
  how fast the server responds. This models real traffic (users arrive whether
  or not your server is ready) and is the honest way to measure latency under
  load.

### Coordinated omission
Naïve load testers under-report latency: when a server stalls, a closed-loop
tester simply sends fewer requests and never records how bad the stall was.
Open-loop mode here measures each request's latency from its **intended** send
time, so a stall shows up as rising latency instead of vanishing. This is called
correcting for *coordinated omission*, and most homemade tools get it wrong.

### Percentiles (p50/p95/p99)
Latency is summarized by percentiles, not averages (averages hide slow
requests). **p99 = 800ms** means 99% of requests completed within 800ms and the
slowest 1% took longer. p95/p99 are the "tail" — the slow requests real users
actually notice — and they usually climb *before* errors appear.

### Keep-alive & pipelining
- **Keep-alive** (default) reuses one TCP connection for many requests instead of
  reconnecting each time — the biggest single throughput win.
- **Pipelining** (`--pipeline N`) sends N requests back-to-back on one connection
  before reading the responses, multiplying per-connection throughput.

---

## Modes of operation

| Mode | How to invoke | What it does |
|------|---------------|--------------|
| Staged (closed loop) | `--profile NAME` or `--config FILE` | Runs a sequence of fixed-concurrency stages. Default. |
| Open-loop rate | `--rate R --duration S` | Holds a steady request rate for a fixed time, CO-corrected. |
| Capacity search | `--find-capacity` | Auto-ramps the rate until an SLO breaks; reports max healthy req/s. |

---

## Full option reference

```
positional:
  url                   Target URL. http(s)://host[:port][/path].
                        IPv6 must be bracketed and quoted: "http://[::1]:3000/"

required:
  --i-own-this          Confirms you're authorized to test the target.

target selection:
  --profile NAME        Built-in stage profile: light | standard | heavy | max
                        (default: standard).
  --config FILE         JSON file defining custom stages (see below).
  --path PATH:WEIGHT    Weighted endpoint, repeatable. e.g. --path /api:3 --path /:1
                        Overrides the URL's path; hits several routes in the
                        given proportions.

load shape:
  --processes N         Worker processes across CPU cores (default 1).
  --rate R              Open-loop target requests/sec (needs --duration).
  --duration S          Duration for --rate mode (seconds).
  --max-inflight N      Max concurrent in-flight requests in open-loop/capacity
                        modes (default 5000).
  --pipeline N          HTTP/1.1 pipeline depth: requests per connection before
                        reading responses (default 1).
  --no-keepalive        Open a fresh connection per request (default: reuse).

request shape:
  --method METHOD       GET (default), POST, PUT, DELETE, HEAD, ...
  --header 'K: V'       Add a request header (repeatable).
  --body TEXT           Request body for POST/PUT.
  --timeout SECONDS     Per-request timeout (default 15).
  --user-agent STR      Override the User-Agent.

capacity search:
  --find-capacity       Auto-ramp the rate until an SLO breaks.
  --start-rate R        Starting rate for the ramp (default 200).
  --step-rate R         Rate increase per step (default 200).
  --step-seconds S      Seconds per step (default 10).
  --max-rate R          Stop ramping at this rate (default 20000).

pass/fail (SLO):
  --slo-p95 MS          Fail the run (exit 1) if any stage's p95 exceeds MS.
  --slo-p99 MS          Fail the run (exit 1) if any stage's p99 exceeds MS.
  --slo-error-pct PCT   Fail the run (exit 1) if any stage's error rate exceeds PCT.

performance:
  --uvloop              Use the uvloop event loop if installed (2–4x faster).

output:
  --report FILE         Write a JSON report.
  --html FILE           Write a self-contained HTML report with charts.
  --csv FILE            Write the per-second time-series to CSV.
  --quiet               Hide the live progress line.
```

---

## Built-in profiles

| Profile    | Shape                                          | Peak concurrency |
|------------|------------------------------------------------|------------------|
| `light`    | warmup → ramp → sustained → cooldown           | 40               |
| `standard` | + a spike stage (default)                      | 400              |
| `heavy`    | long sustained + a big held spike              | 1,200            |
| `max`      | huge sustained + a held 4,000-worker spike     | 4,000            |

```bash
python3 stress_test.py https://script.ceo/ --i-own-this --profile max --processes 8
```

---

## Custom stage files

Define your own stages in JSON (see `stages.example.json`):

```json
{
  "stages": [
    { "name": "warmup",    "workers": 10,   "duration": 15 },
    { "name": "ramp",      "workers": 200,  "duration": 30 },
    { "name": "sustained", "workers": 600,  "duration": 60, "rps": 4000 },
    { "name": "spike",     "workers": 1500, "duration": 30 },
    { "name": "cooldown",  "workers": 25,   "duration": 10 }
  ]
}
```

- `workers` — max concurrent in-flight requests during the stage
- `duration` — seconds
- `rps` — optional per-stage rate cap (omit for uncapped)

```bash
python3 stress_test.py https://script.ceo/ --i-own-this --config stages.example.json
```

---

## Testing APIs

APIs are the most useful thing to load-test: an API route runs real code
(database queries, auth, business logic), so it strains under load and reveals
your true bottleneck. A cached static page just gets served from memory.

Manual:

```bash
# POST JSON to an API with an auth token, ramp to find capacity
python3 stress_test.py https://script.ceo/api/items --i-own-this \
    --method POST \
    --header 'Content-Type: application/json' \
    --header 'Authorization: Bearer YOUR_TOKEN' \
    --body '{"name":"load-test"}' \
    --find-capacity --slo-p99 800 --slo-error-pct 1
```

Or use the included **`api-load-test.sh`** — edit the variables at the top
(URL/method/body/auth/SLO) and run it.

Mix several endpoints in realistic proportions:

```bash
python3 stress_test.py https://script.ceo/ --i-own-this \
    --path /:5 --path /api/search?q=x:3 --path /api/health:2
```

---

## Testing by IP (IPv4 & IPv6)

The tool accepts IPs directly. Add the IP to `allowlist.txt` first (public IPs
only; private ranges and loopback are automatic).

**IPv4:**
```bash
echo "203.0.113.45" >> allowlist.txt
python3 stress_test.py http://203.0.113.45:3000/ --i-own-this --find-capacity
```

**IPv6** — wrap the address in `[ ]` and quote the whole URL; put the bare
address (no brackets) in the allowlist:
```bash
echo "2001:db8::1234" >> allowlist.txt
python3 stress_test.py "http://[2001:db8::1234]:3000/" --i-own-this --find-capacity
```

**Loopback (no allowlist entry needed):**
```bash
python3 stress_test.py http://127.0.0.1:3000/ --i-own-this      # IPv4
python3 stress_test.py "http://[::1]:3000/" --i-own-this        # IPv6
```

Get the port right (`:3000`, `:8080`, `:80`, …) — a wrong port gives "connection
refused."

---

## Finding capacity automatically

`--find-capacity` ramps the request rate step by step, checks your SLOs after
each step, and **stops at the first breach** — reporting the highest rate your
server served healthily.

```bash
python3 stress_test.py https://script.ceo/api/route --i-own-this \
    --find-capacity \
    --start-rate 200 --step-rate 200 --step-seconds 15 --max-rate 50000 \
    --slo-p99 800 --slo-error-pct 1 \
    --processes 8 --pipeline 16 --csv ramp.csv
```

Example output:

```
    200/s target | served  195/s | p95   40ms | p99   70ms | err 0.0% | ok
    400/s target | served  392/s | p95   55ms | p99  120ms | err 0.0% | ok
    600/s target | served  540/s | p95  610ms | p99  980ms | err 1.8% | BREACH p99 980>800ms
CAPACITY: ~392 req/s sustained within SLO (p95 55ms, p99 120ms).
```

It's a capacity *search* — short steps that halt on degradation, not a sustained
flood.

---

## SLO thresholds & CI

Any run (not just capacity search) can enforce Service Level Objectives. If any
stage breaches a threshold, the process exits **1** — perfect for CI to catch
performance regressions.

```bash
python3 stress_test.py https://script.ceo/ --i-own-this \
    --profile heavy --processes 8 \
    --slo-p99 800 --slo-error-pct 1
echo "exit code: $?"     # 0 = passed, 1 = an SLO was breached
```

```yaml
# Example GitHub Actions step
- name: Load-test regression gate
  run: |
    echo "myserver.internal" >> allowlist.txt
    python3 stress_test.py http://myserver.internal/ --i-own-this \
      --profile standard --slo-p99 500 --slo-error-pct 1
```

---

## Helper scripts

| Script | Purpose |
|--------|---------|
| `run.sh` | Quick launcher: `./run.sh <url> <profile> <processes>` |
| `api-load-test.sh` | API-focused test — edit URL/method/body/auth at the top |
| `timed-test.sh` | Run at a fixed rate for a set time, with a live timer and total elapsed |

**Timed test** (live countdown + total wall-clock time, saves a CSV):

```bash
./timed-test.sh <url> <rate-per-sec> <seconds> [processes] [pipeline]

# 20,000 req/s for 60 seconds on your local server:
./timed-test.sh http://localhost:3000/ 20000 60

# 5,000 req/s for 2 minutes on an API, 8 procs, pipeline 32:
./timed-test.sh http://localhost:3000/api/health 5000 120 8 32
```

---

## Reports & output files

- **Terminal** — live per-stage progress, then a summary table and a plain-
  English interpretation (peak p95, where success dropped, your capacity).
- **`--report file.json`** — full structured data: every stage's percentiles,
  status-code histogram, error breakdown, and per-second series.
- **`--csv file.csv`** — per-second time-series (`stage,second,requests,avg_ms`)
  for graphing in a spreadsheet.
- **`--html file.html`** — a self-contained page (no internet needed) with a
  throughput/latency timeline chart and a per-stage p95 bar chart.

---

## Reading the results

```
stage            reqs      rps     ok%    p50     p95     p99     max    errs
------------------------------------------------------------------------------
warmup           2157     1077   100.0      4       7        8      14       0
ramp             3219      813   100.0      8      12       31    1031       0
spike            2247      940    97.5      7      49       88    2688      56
```

- **ok%** — share of 2xx/3xx responses. When this drops, you've found where the
  server starts failing.
- **p95 / p99** — the slow tail. Rising p95 *before* errors appear is your early
  warning the server is straining.
- **errs** — HTTP 4xx/5xx plus connection failures (timeouts/resets). Sudden
  connection failures usually mean you hit a connection or rate limit.
- **max** — the single slowest request; a high max with a low p99 is an
  occasional stall (often GC, cold cache, or a lock).

Rule of thumb: your practical capacity is the **highest rate where ok% stays
≥ 99% and p99 stays acceptable** — which is exactly what `--find-capacity`
reports.

---

## Getting maximum throughput

The client can usually outrun a small server, but if *the tool* is your limit,
in priority order:

1. **Raise the open-file limit.** This is the #1 fix, especially on macOS
   (default is only 256):
   ```bash
   ulimit -n 200000
   # macOS, if that's refused:
   sudo launchctl limit maxfiles 200000 200000 && ulimit -n 200000
   ```
2. **Use all cores:** `--processes N` (set N to your `nproc` / CPU count).
3. **Pipeline:** `--pipeline 32` (or higher) for a big per-connection multiplier.
4. **uvloop:** `pip3 install uvloop` then add `--uvloop` (2–4×).
5. **Raise `--max-inflight`** if you have the RAM (each connection is cheap;
   60000 is fine on 16GB+).

Realistic single-machine ceilings (keep-alive + pipelining + uvloop):

| Machine | Raw throughput to localhost/LAN |
|---------|---------------------------------|
| Chromebook (2–4 cores) | ~2,000–30,000 req/s |
| MacBook Air (M-series) | ~30,000–80,000 req/s |
| MacBook Pro / M3 Pro    | ~50,000–150,000+ req/s |

**The real cap over the internet is your upload bandwidth**, not the CPU — on
home Wi-Fi you'll be limited to a few hundred to a couple thousand full
request/response cycles per second no matter the machine. The machine numbers
above only apply to **localhost or same-LAN** targets.

---

## How it works internally

- **Async I/O:** each worker process runs an `asyncio` event loop driving many
  concurrent connections via `asyncio.open_connection` (TLS via `ssl`).
- **Multiprocess:** the parent splits the worker/rate budget across N processes
  (`fork`), each returning a compact result (counters, a reservoir sample of
  latencies for percentiles, per-second buckets) over a pipe; the parent merges
  them.
- **HTTP:** minimal HTTP/1.1 client that correctly handles `Content-Length` and
  chunked transfer encoding, and honors HTTP/1.0 vs 1.1 keep-alive semantics so
  connections are reused safely.
- **Percentiles** come from a bounded reservoir sample per process (default
  60,000) so memory stays flat even at millions of requests.
- **Coordinated-omission correction** in open-loop mode times each request from
  its scheduled send time.

---

## Troubleshooting

**`REFUSED: 'host' is not authorized`** — the host isn't in `allowlist.txt`, or
the file isn't next to `stress_test.py`. Add it (`echo host >> allowlist.txt`)
and make sure you run from the folder containing both files.

**It's slow / low req/s** — usually the open-file limit (`ulimit -n 200000`),
missing `--uvloop`, single `--processes`, or you're testing a remote site over
the internet (bandwidth/CDN-limited). See
[Getting maximum throughput](#getting-maximum-throughput).

**Lots of `conn_reset` / `429` / `403`** — the target is rate-limiting or
resetting you. If it's behind a CDN/WAF (e.g. Cloudflare), that's its protection
working; a single source can't and shouldn't get past it.

**`connection refused`** — wrong port, or the server isn't running. Check the
`:PORT` in your URL.

**`python3: command not found` (macOS)** — run `xcode-select --install`.

**`--help` errors** — you're on an old copy; re-download the latest.

**Dev server looks slow** — a Next.js/Vite dev server recompiles on the fly and
is far slower than production. Test a production build
(`npm run build && npm start`) for real numbers.

---

## FAQ

**Can it take my site down / get past Cloudflare?**
No. It's a single honest source with no evasion; it will be rate-limited by any
real DDoS protection, and it won't out-muscle a CDN. That's by design. Use it to
find where your *own* server strains, and fix that.

**Does it need any pip packages?**
No — standard library only. `uvloop` is optional and just makes it faster.

**Why does it stop at the breaking point instead of pushing further?**
Because the useful information is *where* it breaks. Pushing past that just keeps
a server down without teaching you anything.

**Can I test a friend's / a company's site?**
Only with explicit written permission, and only after adding it to your
allowlist. Testing systems you don't control is illegal.

**What's the difference between workers and rate?**
Workers = fixed concurrency that adapts to the server's speed (closed loop).
Rate = fixed requests/sec regardless of server speed (open loop, CO-corrected).
Use rate mode for realistic latency numbers.

---

*A load tester for authorized capacity testing. Use it on your own systems.*
