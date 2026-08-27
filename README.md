# stress_test.py — Advanced Staged HTTP Load / Stress Tester

A single-file, **dependency-free** (Python standard library only) load generator
for finding out how much traffic your website can take before it degrades. It
ramps traffic through configurable **stages** and reports latency percentiles,
throughput, and error rates — with a self-contained HTML report.

Runs anywhere Python 3.8+ runs — including a **Chromebook** Linux container — with
no `pip install`.

> **This is a load tester, not an attack tool.** It identifies itself honestly,
> does not spoof source addresses, does not rotate proxies, and does not try to
> evade rate limits or WAFs. It **refuses to run against any host not on your
> allowlist.** Only test sites you own or are explicitly authorized to test.
> Load testing someone else's service without permission is illegal in most
> places. (See "Why you get blocked" below — that's the CDN working, not the
> tool being weak.)

---

## Quick start

```bash
python3 stress_test.py https://script.ceo/ --i-own-this            # default staged run
python3 stress_test.py https://script.ceo/ --i-own-this --profile heavy --processes 4
./run.sh https://script.ceo/ heavy
```

You get a live readout, then a report, then (optionally) HTML/JSON files.

---

## What makes it "advanced"

- **HTTP keep-alive with real response parsing** (Content-Length *and* chunked).
  Connections are reused, which is where most of the throughput comes from —
  the same trick `wrk` uses. Disable with `--no-keepalive`.
- **Multiprocess mode** (`--processes N`) drives load from every CPU core on your
  machine, still as one honest source.
- **HTTP pipelining** (`--pipeline N`) fires N requests per connection before
  reading responses — a large per-socket throughput multiplier on keep-alive
  servers (roughly 5-7x in local testing).
- **Open-loop rate mode** (`--rate R --duration S`) with **coordinated-omission
  correction**: requests are scheduled at a fixed rate and latency is measured
  from each request's *intended* send time, not just when a free worker fired it.
  This is the honest way to measure tail latency — most homemade testers get it
  wrong and flatter the results.
- **Weighted multi-endpoint mix** (`--path PATH:WEIGHT`, repeatable) so you test
  realistic traffic across several routes, not just `/`.
- **Self-contained HTML report** (`--html report.html`) with a throughput/latency
  timeline chart and a per-stage p95 bar chart — no internet or libraries needed
  to open it. Plus `--report results.json` for the raw data.

---

## Stages & profiles

Each stage holds a fixed number of concurrent **workers** for a **duration**, so
you watch latency and errors climb as load increases and find the breaking point.

| profile    | shape                                   | peak concurrency |
|------------|-----------------------------------------|------------------|
| `light`    | warmup → ramp → sustained → cooldown     | 40               |
| `standard` | + a spike stage (default)                | 400              |
| `heavy`    | long sustained + a big held spike        | 1200             |
| `max`      | huge sustained + held 4000-worker spike  | 4000             |

Custom stages via JSON (`stages.example.json`):

```json
{ "stages": [
  { "name": "sustained", "workers": 600, "duration": 45, "rps": 4000 },
  { "name": "spike",     "workers": 1500,"duration": 30 }
]}
```

```bash
python3 stress_test.py https://script.ceo/ --i-own-this --config stages.example.json
```

---

## Options

```
--i-own-this          Required. Confirms you're authorized to test the target.
--profile NAME        light | standard | heavy   (default: standard)
--config FILE         Custom stage definitions (JSON).
--processes N         Worker processes across CPU cores (default 1).
--rate R              Open-loop target requests/sec (needs --duration).
--duration S          Duration for --rate mode (seconds).
--max-inflight N      Cap on concurrent in-flight requests in --rate mode.
--path PATH:WEIGHT    Weighted endpoint, repeatable (e.g. --path /api:3 --path /:1).
--method METHOD       GET (default), POST, HEAD, ...
--header 'K: V'       Add a request header (repeatable).
--body TEXT           Request body for POST/PUT.
--no-keepalive        Fresh connection per request (default: reuse).
--pipeline N          HTTP/1.1 pipeline depth (requests/connection, default 1).
--timeout SECONDS     Per-request timeout (default 15).
--user-agent STR      Override the User-Agent.
--report FILE         Write a JSON report.
--html FILE           Write a self-contained HTML report with charts.
--csv FILE            Write the per-second time-series to CSV (graph it anywhere).
--slo-p95 MS          Fail the run (exit 1) if any stage's p95 latency exceeds MS.
--slo-p99 MS          Fail the run (exit 1) if any stage's p99 latency exceeds MS.
--slo-error-pct PCT   Fail the run (exit 1) if any stage's error rate exceeds PCT.
--find-capacity       Auto-ramp the rate until an SLO breaks; report max healthy req/s.
--start-rate R        find-capacity: starting rate (default 200).
--step-rate R         find-capacity: rate increase per step (default 200).
--step-seconds S      find-capacity: seconds per step (default 10).
--max-rate R          find-capacity: stop ramping at this rate (default 20000).
--quiet               Hide the live progress line.
```

Press **Ctrl+C** to stop early — you still get a report for the stages that ran.

---

## Auto capacity finder (`--find-capacity`)

The most advanced mode: it ramps the request rate step by step, watches latency
and errors, and **stops automatically at the rate where your site breaks**,
reporting the highest healthy throughput. Hands-off — you get one number.

```bash
python3 stress_test.py https://mybox.example/api/route --i-own-this \
    --find-capacity --start-rate 200 --step-rate 200 --step-seconds 10 \
    --slo-p99 800 --slo-error-pct 1 --processes 8 --pipeline 16 --csv ramp.csv
```

Output looks like:

```
    200/s target | served  195/s | p95   40ms | p99   70ms | err 0.0% | ok
    400/s target | served  392/s | p95   55ms | p99  120ms | err 0.0% | ok
    600/s target | served  540/s | p95  610ms | p99  980ms | err 1.8% | BREACH p99 980>800ms
CAPACITY: ~392 req/s sustained within SLO (p95 55ms, p99 120ms).
```

Tunables: `--start-rate`, `--step-rate`, `--step-seconds`, `--max-rate`, and the
same `--slo-*` thresholds define "broken." It's a capacity *search* — each step
is short and it halts the moment things degrade, so it never sits there flooding.

---

## Capacity stress test (the "how much can it take" number)

The point of a stress test is a number: the most traffic your box serves
*healthily*. Set SLO thresholds and the run fails the moment a stage breaches
them, and it prints the highest throughput that stayed at >=99% success:

```bash
python3 stress_test.py https://mybox.example/ --i-own-this \
    --profile heavy --processes 4 --pipeline 16 \
    --slo-p99 800 --slo-error-pct 1 \
    --csv timeseries.csv --html report.html
```

- **SLO: PASS/FAIL** and exit code 0/1 — drop it straight into CI to catch
  regressions ("fail the build if p99 > 800ms under load").
- **"Highest sustained throughput at >=99% success: ~N req/s"** — your capacity.
- **`--csv`** gives you the per-second series to graph anywhere.

Point `--path` at your heaviest route (a DB query, a big dynamic page) to find
the real limit — a static file will always look faster than your app actually is.

---

## Reading the results

- **ok%** — share of 2xx/3xx responses. When it drops, you've found where the
  site starts failing under load.
- **p95 / p99 latency** — the slow tail. Rising p95 before errors appear is your
  early warning that the site is straining.
- **errs** — HTTP 4xx/5xx plus connection failures (timeouts/resets), which
  usually mean you've hit a connection or rate limit.

---

## Why you get blocked (Netlify / CDN targets)

`script.ceo` is on Netlify, whose global CDN is *built* to absorb floods and rate-
limit abusive sources. From a single machine you're mostly measuring **the CDN
edge and your own network**, and you should expect `429`/`403`/resets under a
spike — that's the protection doing its job, not a weakness in this tool. There is
no honest single-source tool that "gets past" that; the things that do are
botnets, which this deliberately is not.

To measure your **application** (the part you can actually tune), point it at a
dynamic route — e.g. a Netlify Function endpoint — with `--path`, rather than a
static, cached page.

---

## Safety & scope

- Refuses any host not in `allowlist.txt` (localhost and private IPs always OK).
- Requires the explicit `--i-own-this` flag every run.
- Single source, honest User-Agent, no evasion. It measures capacity; it is not
  built to defeat defenses.

Use it on your own stuff. That's the whole point.
