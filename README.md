# CF-Slayer

[![CI](https://img.shields.io/github/actions/workflow/status/ShinRyu04/CF-Slayer/ci.yml?branch=main&label=CI)](https://github.com/ShinRyu04/CF-Slayer/actions/workflows/ci.yml)
[![Python 3.13](https://img.shields.io/badge/python-3.13-3776AB)](https://www.python.org/)
[![Go 1.23](https://img.shields.io/badge/go-1.23-00ADD8)](https://go.dev/)
[![License: GPL v3](https://img.shields.io/badge/license-GPL%20v3-blue.svg)](LICENSE)
[![Code style: ruff](https://img.shields.io/badge/code%20style-ruff-261230)](https://github.com/astral-sh/ruff)

A Cloudflare clearance solver and replay service. It drives a browser through
the challenge, harvests the `cf_clearance` cookie and the exact User-Agent that
won it, caches both per domain for 30 minutes, then replays the credential with
`curl_cffi` (Firefox TLS fingerprint).

Backends are tried in order (`CF_SLAYER_BACKENDS`, default `camoufox,pydoll`).
Camoufox (stealth Firefox) is first; pydoll drives a locally installed Chrome
over CDP and is skipped automatically when Chrome or the package is missing.
Every attempt is recorded in the response's `attempts` array. An optional
background worker renews cached credentials before they expire.

```
POST /solve  {url}    ──►  {clearance, user_agent, domain, expires_in, cached, attempts[]}
      │                        cache hit: no browser, attempts=[]
      └─ miss ─► BackendChain
                   ├─ camoufox: stealth Firefox → goto → poll cf_clearance → UA
                   └─ pydoll: local Chrome → same cookie polling (skipped if absent)
                        └─► SQLite cache (per domain, 30-minute TTL)

PrewarmWorker (CF_SLAYER_PREWARM_DOMAINS) ──► renews records inside the margin

GET /replay?url=...    ──►  {status, length, challenge_page, ...}
      └─ cached cookie + solving UA ─► target site
            └─ 403/challenge page ─► invalidate, re-solve once, retry once
```

## Requirements

- Python 3.12+ (developed on 3.13, Windows)
- About 2.5 GB of disk for the patched Firefox and add-ons on first install
  (1.3 GB download; 198 seconds on the verification machine)
- The commands below assume CMake/Git Bash/PowerShell
- Optional: an installed Chrome/Chromium for the pydoll fallback backend.
  It is discovered from common install paths and `PATH`, or set
  `CF_SLAYER_CHROME_PATH`.

Camoufox patches fingerprints at Firefox's C++ layer (WebGL renderer, canvas,
AudioContext, WebRTC, screen geometry, and more), so it handles Cloudflare
managed challenges better than stock Playwright.

## Install

Git Bash:

```bash
cd cf-slayer
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt
.venv/Scripts/python -m camoufox fetch      # once, ~1.3 GB download
```

PowerShell:

```powershell
cd cf-slayer
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m camoufox fetch
```

`uv` also works: `uv venv --python 3.13 .venv && uv pip install -r requirements.txt --python .venv/Scripts/python.exe`.

`camoufox fetch` resolves releases through the GitHub API. If that call is
rate-limited (60 requests per hour per anonymous IP), export a token first:

```bash
export GITHUB_TOKEN=$(gh auth token)   # or a personal access token
.venv/Scripts/python -m camoufox fetch
```

Verify the installed version with
`.venv/Scripts/python -m camoufox version`.

## Run

```bash
.venv/Scripts/python -m uvicorn solver.main:app --host 127.0.0.1 --port 8878
```

On first start the service generates a bearer token, appends it to `.env`, and
logs the file path (the token itself is never logged). Read it with:

```bash
# Git Bash
TOKEN=$(grep '^CF_SLAYER_AUTH_TOKEN=' .env | cut -d= -f2)

# PowerShell
$TOKEN = (Select-String -Path .env -Pattern '^CF_SLAYER_AUTH_TOKEN=(.+)$').Matches[0].Groups[1].Value
```

Windows does not enforce POSIX `0600`; tighten permissions explicitly on a
shared machine:

```powershell
icacls .env /inheritance:r /grant:r "$env:USERNAME:F"
```

### Endpoints

| Method | Path | Auth | Purpose |
|---|---|---|---|
| POST | `/solve` | Bearer | Solve and cache a clearance credential, or mint a Turnstile token |
| GET | `/replay?url=...` | Bearer | Fetch the target with a cached credential |
| GET | `/health` | none | `{"state":"ready","browser":"camoufox"}` |
| GET | `/stats` | Bearer | Cache hits, solves, errors, prewarm errors, average solve time |

### curl examples

```bash
# Solve
curl -s -X POST http://127.0.0.1:8878/solve \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://target.example/","want":"clearance"}'

# -> {"clearance":"...","user_agent":"Mozilla/5.0 ... Firefox/156.0",
#     "domain":"target.example","expires_in":1789,"cached":false,"solve_seconds":14.2,
#     "attempts":[{"backend":"camoufox","ok":true,"error":null,"seconds":14.2}]}

# Replay (cached cookie + solving UA)
curl -s "http://127.0.0.1:8878/replay?url=https://target.example/" \
  -H "Authorization: Bearer $TOKEN"

# -> {"url":"https://target.example/","domain":"target.example","status":200,
#     "length":202117,"challenge_page":false,"replay_seconds":1.34,"resolved":false}

# Health and stats
curl -s http://127.0.0.1:8878/health
curl -s http://127.0.0.1:8878/stats -H "Authorization: Bearer $TOKEN"
```

`/solve` accepts `want: "clearance"` (default) and `want: "turnstile"`; other
values return `BAD_REQUEST`. On a cache hit, `attempts: []`. On failure the same
array appears under `error.attempts`, with one entry per backend that ran or was
skipped.

## Errors

Every failure is JSON:

```json
{"error": {"code": "SOLVE_TIMEOUT", "message": "cf_clearance did not appear within 90s"}}
```

| Code | HTTP | Meaning |
|---|---|---|
| `AUTH_MISSING` | 401 | Missing or wrong `Authorization: Bearer <token>` |
| `BAD_REQUEST` | 400 | Bad URL, bad JSON, or unsupported `want` |
| `SOLVE_TIMEOUT` | 504 | No `cf_clearance` within the solve/challenge budget (120 s / 90 s) |
| `SOLVE_FAILED` | 502 | Browser started but the challenge was not passed, or the engine crashed |
| `UPSTREAM_403` | 502 | Replay was still challenged after one automatic re-solve |

## Hard rules (why it works)

A `cf_clearance` cookie is only meaningful in the context that produced it. The
service enforces what it can and records the rest explicitly:

1. **Same egress IP.** The cookie is bound to the solving IP. v1 has no proxy
   support, so solving and replay must run on the same host/connection.
2. **Same User-Agent.** Replay sends the exact UA harvested from the browser
   session; the cache stores cookie and UA as one unit. Changing either during
   replay silently triggers a 403.
3. **Roughly 30-minute TTL.** The cache TTL is capped at 1800 seconds, or the
   cookie's own `expires` when that is earlier. Expired rows are deleted on read.
4. **Per-domain isolation.** The cache key is the lowercase hostname. A
   credential for `a.example.com` is never used for `b.example.com`; the replay
   client rejects domain mismatches.

If replay sees a 403/503 or a Cloudflare interstitial in the body, the service
invalidates the entry, solves once, and retries once. A second failure returns
`UPSTREAM_403`. Calling `/replay` for a domain without a cached credential
solves first, then replays (still retrying only once).

## Configuration

`pydantic-settings` reads `CF_SLAYER_*` from the environment or `.env`; see
[`.env.example`](.env.example). Key settings: `CF_SLAYER_PORT` (8878),
`CF_SLAYER_TTL_SECONDS` (1800), `CF_SLAYER_SOLVE_TIMEOUT` (120),
`CF_SLAYER_CHALLENGE_TIMEOUT` (90), `CF_SLAYER_HEADLESS` (true),
`CF_SLAYER_BACKENDS` (`camoufox,pydoll`), `CF_SLAYER_CHROME_PATH` (auto),
`CF_SLAYER_PREWARM_DOMAINS` (off), `CF_SLAYER_PREWARM_MARGIN` (300),
`CF_SLAYER_PREWARM_INTERVAL` (60).

## Backends

`CF_SLAYER_BACKENDS` is an ordered list. Each backend gets the same timeout
budget per request; the first success wins, and the attempt log keeps every
record.

- `camoufox` — patched Firefox, the primary solver.
- `pydoll` — drives local Chrome over the DevTools protocol (no WebDriver).
  When the `pydoll` package or Chrome is missing, the backend reports itself
  unavailable and the chain skips it instead of failing. `--user-agent` is
  derived from the installed Chrome version; otherwise Chrome reports
  `HeadlessChrome`, which Cloudflare treats as a bot signal.

Unknown names are logged and ignored. If no backend is available, startup fails
with a clear `RuntimeError`. A chain failure returns `SOLVE_FAILED` (or
`SOLVE_TIMEOUT` when every attempt timed out), with per-backend errors in
`error.attempts`.

## Prewarm

Set `CF_SLAYER_PREWARM_DOMAINS` to a comma-separated list of domains or full
URLs (`steamdb.info,https://example.com/`). While the service runs, a background
worker checks each cache record and re-solves when it is missing or has less
than `CF_SLAYER_PREWARM_MARGIN` seconds left. It sleeps
`CF_SLAYER_PREWARM_INTERVAL` between rounds, tracks in-progress domains to avoid
concurrent duplicate solves, and counts failures in `prewarm_errors` on
`/stats` without breaking the loop. Prewarm bypasses the cache read
(`force=True`), so a still-valid but nearly expired credential is genuinely
renewed.

## Go client (v4)

`client-go/` is the native hot path: Go standard library only, no CGO, no third-party
dependencies, Go 1.23+.

```bash
cd client-go
go test ./...
go build -o demo.exe ./examples/demo

# Single instance
CF_SLAYER_AUTH_TOKEN=... ./demo.exe -url https://target.example/ -n 5

# Multiple workers: per-domain stickiness + failure removal
CF_SLAYER_SERVICES=http://127.0.0.1:8878,http://127.0.0.1:8879 \
CF_SLAYER_AUTH_TOKEN=... ./demo.exe -url https://target.example/ -n 20
```

- `Client`: `Solve` calls `/solve`, caches per domain in memory (`sync.Map`),
  uses `expires_in` as the TTL, and renews in the background at 80%. A cache
  hit performs no HTTP. `Do` injects `cf_clearance` and the exact solving
  User-Agent; on 403 it invalidates the entry, re-solves once, and retries once.
  A second 403 is returned unchanged. Bodies that cannot be replayed (no
  `GetBody`) are not retried.
- `Pool`: `CF_SLAYER_SERVICES` is a comma-separated list. Consistent hashing
  keeps a domain on one worker (so the server-side per-domain cache can hit).
  Three consecutive failures (connection refused/5xx/timeout) remove a worker
  from the ring; a background `GET /health` every 5 seconds restores it after
  two healthy probes. `Status()` exposes `{URL, State, ConsecutiveFails}`. When
  every worker is removed it returns `ErrNoWorkers` instead of retrying forever.
- Tests use `httptest` fakes and cover cache hits, early renewal, exactly one
  re-solve on 403, removal after three 500s, probe recovery, hash stability,
  and credential-free logs.
- The token is read only from `CF_SLAYER_AUTH_TOKEN`; credentials and UAs are
  never logged.

v4 local measurements (`127.0.0.1` fake target plus two real Python workers,
**not** a real Cloudflare site): Python `/replay` median ≈ 11.1 ms over 10 runs
(7.7–12.6 ms); Go client replay median ≈ 1.1 ms (0.6–1.8 ms). The difference
comes from bypassing FastAPI and the Python request stack and reflects local
hot-path overhead only.

## Multi-worker runtime (v4)

```powershell
powershell -ExecutionPolicy Bypass -File scripts/run-workers.ps1
```

Starts two uvicorn workers by default (8878, 8879) sharing one `cache.sqlite3`
(WAL). `-Count`, `-BasePort`, `-CachePath`, `-Token`, and `-NoWait` are
configurable; `-NoWait` only prints PIDs. The token is never printed.

Cross-process cache was verified: after worker A completed a real Camoufox solve
against a `127.0.0.1` fake target (34.8 s), worker B returned `cached=true` and
`expires_in=1796` for the same domain in 153 ms. Sharing relies on SQLite WAL,
not in-process state; v4 did not change the storage layer and showed no write
lock contention.

## Turnstile tokens (v5)

`POST /solve` now has two paths: `want=clearance` (default) and
`want=turnstile`.

```bash
curl -s http://127.0.0.1:8878/solve \
  -H "Authorization: Bearer $CF_SLAYER_AUTH_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"want":"turnstile","url":"https://target.example/login","sitekey":"0x4AAAAAAA..."}'
```

```json
{
  "want": "turnstile",
  "token": "0.xxxxx…",
  "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:156.0) …",
  "domain": "target.example",
  "solve_seconds": 8.4,
  "cached": false
}
```

- `sitekey` is required and must match `^0x[0-9a-zA-Z]{20,}$`; missing or
  invalid values return `BAD_REQUEST` before the browser starts. The URL is
  validated as absolute http(s) up front. Registration cannot be verified
  offline, so no registration check is performed; a wrong domain simply makes
  the Cloudflare widget fail.
- **One-shot**: the token is consumed on first use. The service never reads or
  writes SQLite; the client never caches or renews it. Every `/solve` is a fresh
  browser solve, and `cached` is always `false`.
- The token is bound to the solving session's User-Agent; replay must use the
  returned `user_agent`.
- The chain only tries backends that implement token support; unsupported
  backends are recorded in `attempts` and skipped. When none support it, the
  response is `SOLVE_FAILED` with `attempts` in the error body.
- Go client: `SolveTurnstile(ctx, url, sitekey) (Token, error)`, with no cache
  and no renewal; `Do` remains clearance-only.
- Statistics add `tokens_solved` and `token_errors`, separate from clearance
  counters.

Honest note: **this was not verified successfully against a real Cloudflare
site.** Cloudflare's official always-pass test sitekey starts with `1x`
(`1x00000000000000000000AA`), while this service's contract accepts only `0x`
production sitekeys; that request is rejected early with `BAD_REQUEST`. The
behavior above is covered by 84 offline Python tests and 20 Go tests; the
browser path was exercised only with fake pages for component loading, polling,
close order, and timeout.

## Verified

Real-site E2E on 2026-11-12 against `https://steamdb.info/` (Cloudflare
managed), Camoufox v156.0.1-beta.36, Python 3.13.16. Full log:
[`docs/e2e-v1.md`](docs/e2e-v1.md).

| Step | Result | Measured |
|---|---|---|
| `POST /solve` (cold) | 200, `cached=false` | 12.94 s solve, 12.98 s HTTP |
| `POST /solve` (warm) | 200, `cached=true` | 0.0038 s HTTP |
| `GET /replay` | 200, `challenge_page=false` | 194,161 bytes, 0.265 s |

v2 acceptance (2026-11-12, same machine, offline/local only):

| Check | Result |
|---|---|
| Full offline suite | 70 passed, ruff clean (v1's 36 unchanged) |
| pydoll only, `CF_SLAYER_CHROME_PATH` pointing at a missing path | 502 `SOLVE_FAILED`, `attempts[0].backend="pydoll"`, error includes the path; startup unaffected |
| Prewarm against a local cookie server, `TTL=30 s`, interval 5 s | 2 renewals in 18.5 s, `cached_domains=1`, `prewarm_errors=0`; a consumer `/solve` returned `cached=true` with 28 s left, `cache_hits=1` |
| v4: `go test ./...` | 15 offline tests passed (no browser, no network) |
| v4: cross-process cache | After worker A solved in 34.8 s, worker B returned `cached=true` in 153 ms |
| v4: kill a worker mid-demo | Replays stayed 200; pool state moved `healthy -> down` at request 68 and `down -> healthy` at request 150 after restart |
| v5: `pytest` | 84 offline tests passed (14 new Turnstile cases) |
| v5: `go test ./...` | 20 offline tests passed (5 new token cases) |

The pydoll backend itself has been run against `https://example.com` (Chrome 157
startup, cookie read, `navigator.userAgent` read); it has not passed a real
Cloudflare challenge, so it is treated as a fallback rather than a replacement.

## Tests

```bash
.venv/Scripts/python -m pytest     # 84 offline tests, no browser, no network
.venv/Scripts/ruff check .
cd client-go && go test ./...      # 20 Go offline tests, standard library only
```

Engine tests inject fake browser factories; API tests inject fake backends and
monkeypatch `solver.main.回放`. Chain and prewarm tests use stub backends and a
temporary SQLite cache. No test touches a Cloudflare-protected site; pydoll
probe tests are skipped when Chrome is absent.

## Project layout

```
solver/
  main.py      FastAPI app + service: auth, cache/solve/replay orchestration, stats
  engine.py    backend protocol + attempt records + Camoufox backend
  chain.py     backend chain: ordered fallback, skip unavailable, aggregate attempts
  pydoll_backend.py  pydoll backend: CDP-driven local Chrome, Chrome/UA discovery
  prewarm.py   prewarm worker: margin-based renewal loop
  cache.py     SQLite cache: WAL, TTL, thread safety, per domain
  config.py    pydantic-settings + first-run token written to .env
  errors.py    error codes and HTTP status mapping
client/
  replay.py    curl_cffi replay (impersonate="firefox") + challenge-page detection
client-go/
  client.go    Go client: solve, per-domain cache, 80% early renewal, 403 self-heal
  pool.go      consistent hashing, failure removal, health probes, pool status
  examples/demo/main.go  demo CLI (solve → N replays → pool status)
scripts/
  run-workers.ps1  multi-worker launcher (shared SQLite cache)
tests/
  test_cache.py
  test_engine_offline.py
  test_api.py
  test_replay.py
  test_chain.py
  test_pydoll.py
  test_prewarm.py
  test_turnstile.py
```

## Limitations

- pydoll is best-effort: it rewrites the UA, but stock Chrome lacks Camoufox's
  fingerprint patches and may be challenged harder. Camoufox is always first in
  the default chain.
- Prewarm is an in-process interval loop, not a persistent scheduler: it starts
  with the app, runs each round, and restarts with the process. Each renewal is
  a full solve and consumes the same backend budget as a request.
- There is no proxy pool or fixed egress; IP-bound credentials work only on the
  solving host.
- One browser process per solve (no pooling); concurrent solves fan out multiple
  browsers instead of queuing.
- `/health` reports `ready` as soon as the app is up; it does not probe browser
  binaries (run `python -m camoufox fetch` first, and install Chrome for pydoll).
  The chain checks availability on each solve.
- Without a browser installed, Camoufox queries GitHub release metadata during
  `fetch`. If that call is rate-limited, `fetch` reports "Synced 0 versions" and
  solves fail with `SOLVE_FAILED`; exporting `GITHUB_TOKEN` lifts the limit
  (Camoufox forwards it to the GitHub API).

## Roadmap (v3 deferred, Go side shipped in v4)

The v3 proxy pool was deliberately deferred: the deployment had a clean egress
IP and no proxy resources.

- **FlareSolverr backend.** Optional HTTP backend for a FlareSolverr instance;
  same `Backend` protocol, ordered after Camoufox.
- **Rust cfsolve backend.** Pure-protocol solver as a final fallback.
- **Proxy pool.** Per-domain sticky egress with health scoring, so solving and
  replay share one IP and the service can run away from the solving host.

## A note on identifiers

Internal implementation (module variables, functions, classes, test names, logs,
exception messages) uses Simplified Chinese identifiers. This is deliberate. The
public contract stays ASCII: JSON keys, error codes, environment variable names,
SQL table/column names, endpoint paths, HTTP headers, `配置` field names
(pydantic environment mapping depends on them), module paths, and the ASGI
attribute `app`. Existing clients and first-run behavior are unaffected.

## Languages

Comments and Python identifiers in this repository are written in Simplified
Chinese. The public interface is English: JSON field names, error codes,
environment variables, endpoint paths, SQL schema, and Go identifiers. This is
a naming convention, not a localization layer; clients never need to parse
Chinese text.

## Contributors

- **Muhammad Khalid ([@ShinRyu04](https://github.com/ShinRyu04))** — maintainer and repository owner.
- **Zyrex** — original author of the commit history.

Contributions are welcome; see [CONTRIBUTING.md](CONTRIBUTING.md) and the
[contributors graph](https://github.com/ShinRyu04/CF-Slayer/graphs/contributors).

## License

GPL-3.0. Full text in [LICENSE](LICENSE).
