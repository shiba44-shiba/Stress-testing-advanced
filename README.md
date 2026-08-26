# stress_test.py — Staged HTTP Load / Stress Tester

A single-file, **dependency-free** (Python standard library only) load generator
for finding out how much traffic your website can take before it starts to
degrade. It ramps traffic through configurable **stages** and reports latency
percentiles, throughput, and error rates.

Runs anywhere Python 3.8+ runs — including a **Chromebook** Linux container — with
no `pip install`.

> **This is a load tester, not an attack tool.** It identifies itself honestly,
> does not spoof source addresses, does not rotate proxies or evade defenses, and
> **refuses to run against any host that isn't on your allowlist.** Only test
> sites you own or are explicitly authorized to test. Load testing someone else's
> service without permission is illegal in most places.

---

## Quick start

```bash
# 1. Your site is already in allowlist.txt (script.ceo). To test another host,
#    add it there first.

# 2. Run the default staged test:
python3 stress_test.py https://script.ceo/ --i-own-this

# or use the launcher:
./run.sh https://script.ceo/ standard
```

You'll see a live per-second readout, then a report like:

```
stage            reqs      rps     ok%     p50     p95      p99    errs
------------------------------------------------------------------------------
warmup           2157     1077   100.0       4       7        8       0
ramp             3219      813   100.0       8      12       31       0
spike            2247      940   100.0       7      49       88       0
```

---

## Stages

The tool runs a sequence of stages. Each stage holds a fixed number of
concurrent **workers** (in-flight requests) for a **duration**, so you can watch
latency and errors climb as load increases and find your breaking point.

Built-in profiles (`--profile`):

| profile    | shape                                              | peak concurrency |
|------------|----------------------------------------------------|------------------|
| `light`    | warmup → ramp → sustained → cooldown               | 40               |
| `standard` | + a spike stage (default)                          | 400              |
| `heavy`    | long sustained + a big held spike                  | 1200             |

### Custom stages

Define your own in a JSON file (see `stages.example.json`):

```json
{
  "stages": [
    { "name": "warmup",    "workers": 10,  "duration": 15 },
    { "name": "sustained", "workers": 600, "duration": 45, "rps": 4000 },
    { "name": "spike",     "workers": 1500,"duration": 30 }
  ]
}
```

- `workers` — max concurrent in-flight requests
- `duration` — seconds
- `rps` — optional per-stage request-rate cap (omit for uncapped)

```bash
python3 stress_test.py https://script.ceo/ --i-own-this --config stages.example.json
```

---

## Options

```
--i-own-this          Required. Confirms you're authorized to test the target.
--profile NAME        light | standard | heavy   (default: standard)
--config FILE         Custom stage definitions (JSON).
--method METHOD       GET (default), POST, HEAD, ...
--header 'K: V'       Add a request header (repeatable).
--body TEXT           Request body for POST/PUT.
--timeout SECONDS     Per-request timeout (default 15).
--user-agent STR      Override the User-Agent.
--report FILE         Write a full JSON report.
--quiet               Hide the live progress line.
```

Press **Ctrl+C** any time to stop early — you still get a report for the stages
that ran.

---

## How to read the results

- **ok%** — share of requests returning 2xx/3xx. When this drops, you've found
  where the site starts failing under load.
- **p95 / p99 latency** — the slow tail. Rising p95 well before errors appear is
  your early warning that the site is straining.
- **connection_failures** — timeouts / resets. These usually mean you've hit a
  connection or rate limit (your host's, or your own network's).

### A note on Netlify / CDN-hosted sites

`script.ceo` is on Netlify, which serves static assets from a global CDN with its
own rate limiting and DDoS protection. From a single Chromebook you're mostly
measuring **the CDN edge and your local network**, not an origin server — so
expect it to absorb a lot, and expect Netlify to start returning `429`/`403` or
resetting connections rather than falling over. That's the CDN doing its job. To
stress *application* behavior, point the tester at a dynamic endpoint (a function
route) rather than a static page.

---

## Safety & scope

- Refuses any host not in `allowlist.txt` (localhost and private IPs always OK).
- Requires the explicit `--i-own-this` flag every run.
- Single source, honest User-Agent, no evasion. It measures capacity; it is not
  built to defeat defenses.

Use it on your own stuff. That's the whole point.
