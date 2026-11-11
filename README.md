# cf-slayer

Cloudflare clearance solver and replay service. It drives a browser through a
challenge, harvests the `cf_clearance` cookie together with the exact User-Agent
that earned it, caches both per domain for 30 minutes, and replays the credential
with `curl_cffi` (Firefox TLS fingerprint).

Backends are tried in order (`CF_SLAYER_BACKENDS`, default `camoufox,pydoll`).
Camoufox (stealth Firefox) comes first; pydoll drives an installed Chrome over CDP
and is skipped when Chrome or the package is missing. Every try is reported in the
response's `attempts` array. An optional background worker renews cached domains
before their credentials expire.

```
POST /solve  {url}    ──►  {clearance, user_agent, domain, expires_in, cached, attempts[]}
      │                        cache hit: no browser starts, attempts=[]
      └─ miss ─► BackendChain
                   ├─ camoufox: stealth Firefox → goto → poll cf_clearance → UA
                   └─ pydoll: installed Chrome → same cookie poll (skipped if absent)
                        └─► SQLite cache (per domain, TTL 30 min)

PrewarmWorker (CF_SLAYER_PREWARM_DOMAINS) ──► renew entries inside the margin

GET /replay?url=...    ──►  {status, length, challenge_page, ...}
      └─ cached cookie + solve-time UA ─► target
            └─ 403/challenge page ─► invalidate, solve once more, retry once
```

## Requirements

- Python 3.12+ (developed on 3.13, Windows)
- ~2.5 GB disk for the patched Firefox and addons once fetched
  (1.3 GB download, 198 s on the verification machine)
- CMake/Git Bash/PowerShell for the commands below
- Optional: an installed Chrome/Chromium for the pydoll fallback backend.
  Detected from common install paths and `PATH`, or set `CF_SLAYER_CHROME_PATH`.

Camoufox patches fingerprinting in Firefox's C++ layer (WebGL renderer, canvas,
AudioContext, WebRTC, screen geometry, …), which is why it beats stock Playwright
on Cloudflare-managed challenges.

## Install

Git Bash:

```bash
cd cf-slayer
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt
.venv/Scripts/python -m camoufox fetch      # one-time, ~1.3 GB download
```

PowerShell:

```powershell
cd cf-slayer
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m camoufox fetch
```

`uv` works too: `uv venv --python 3.13 .venv && uv pip install -r requirements.txt --python .venv/Scripts/python.exe`.

`camoufox fetch` resolves releases through the GitHub API. If that call is rate
limited (anonymous IPs get 60 requests/hour), export a token first:

```bash
export GITHUB_TOKEN=$(gh auth token)   # or a personal access token
.venv/Scripts/python -m camoufox fetch
```

Verify the installed build with `.venv/Scripts/python -m camoufox version`.

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

Windows does not enforce POSIX `0600`; restrict the file explicitly if the
machine is shared:

```powershell
icacls .env /inheritance:r /grant:r "$env:USERNAME:F"
```

### Endpoints

| Method | Path | Auth | Purpose |
|---|---|---|---|
| POST | `/solve` | Bearer | Solve and cache a clearance |
| GET | `/replay?url=...` | Bearer | Fetch the target with the cached credential |
| GET | `/health` | none | `{"state":"ready","browser":"camoufox"}` |
| GET | `/stats` | Bearer | cache hits, solves, errors, prewarm errors, average solve time |

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

# Replay (uses the cached cookie + the UA that was used to solve)
curl -s "http://127.0.0.1:8878/replay?url=https://target.example/" \
  -H "Authorization: Bearer $TOKEN"

# -> {"url":"https://target.example/","domain":"target.example","status":200,
#     "length":202117,"challenge_page":false,"replay_seconds":1.34,"resolved":false}

# Health and stats
curl -s http://127.0.0.1:8878/health
curl -s http://127.0.0.1:8878/stats -H "Authorization: Bearer $TOKEN"
```

`/solve` accepts only `want: "clearance"`. Any other value is `BAD_REQUEST`.
Cache hits return `attempts: []`. Failures carry the same array inside
`error.attempts`, one object per backend that ran or was skipped.

## Errors

All failures are JSON:

```json
{"error": {"code": "SOLVE_TIMEOUT", "message": "cf_clearance did not appear within 90s"}}
```

| Code | HTTP | Meaning |
|---|---|---|
| `AUTH_MISSING` | 401 | Missing or wrong `Authorization: Bearer <token>` |
| `BAD_REQUEST` | 400 | Bad URL, bad JSON, unsupported `want` |
| `SOLVE_TIMEOUT` | 504 | No `cf_clearance` within the solve/challenge budget (120 s / 90 s) |
| `SOLVE_FAILED` | 502 | Browser launched but the challenge was not solved, or the engine broke |
| `UPSTREAM_403` | 502 | Replay still hit a challenge after one automatic re-solve |

## Hard rules (why this works)

A `cf_clearance` cookie is meaningless unless the replay reproduces the solve
context. The service enforces what it can and documents the rest:

1. **Same egress IP.** The cookie is bound to the IP that solved it. v1 has no
   proxy support, so solve and replay must run on the same host/connection.
2. **Same User-Agent.** Replay sends the exact UA harvested from the browser
   session; the cache stores cookie and UA as one pair. Changing either on
   replay yields a silent 403.
3. **~30 minute TTL.** The cache TTL ceiling is 1800 s, and a cookie's own
   `expires` wins when it is sooner. Expired rows are deleted on read.
4. **Per-domain isolation.** Cache key is the lowercase hostname. A clearance
   for `a.example.com` is never replayed against `b.example.com`, and the
   replay client refuses a domain mismatch.

If replay sees 403/503 or a Cloudflare interstitial in the body, the service
invalidates the entry, solves once, and retries once. A second failure returns
`UPSTREAM_403`. A `/replay` call for a domain with no cached credential solves
first, then replays (same one-retry rule).

## Configuration

`pydantic-settings` reads `CF_SLAYER_*` from the environment or `.env`; see
[`.env.example`](.env.example). Notable knobs: `CF_SLAYER_PORT` (8878),
`CF_SLAYER_TTL_SECONDS` (1800), `CF_SLAYER_SOLVE_TIMEOUT` (120),
`CF_SLAYER_CHALLENGE_TIMEOUT` (90), `CF_SLAYER_HEADLESS` (true),
`CF_SLAYER_BACKENDS` (`camoufox,pydoll`), `CF_SLAYER_CHROME_PATH` (auto),
`CF_SLAYER_PREWARM_DOMAINS` (off), `CF_SLAYER_PREWARM_MARGIN` (300),
`CF_SLAYER_PREWARM_INTERVAL` (60).

## Backends

`CF_SLAYER_BACKENDS` is an ordered list. Each backend gets the same timeout budget
per request; the first success wins and the attempt trace records every try.

- `camoufox` — patched Firefox, the primary solver.
- `pydoll` — drives an installed Chrome over the DevTools protocol (no WebDriver).
  The backend reports itself unavailable when the `pydoll` package or Chrome is
  missing, so the chain skips it instead of failing. `--user-agent` is set from the
  installed Chrome build, because Chrome otherwise advertises `HeadlessChrome`,
  which Cloudflare treats as a bot signal.

Unknown names are logged and ignored. If no backend remains, startup fails with a
clear `RuntimeError`. A failed chain returns `SOLVE_FAILED` (or `SOLVE_TIMEOUT`
when every backend timed out) with per-backend errors in `error.attempts`.

## Prewarm

Set `CF_SLAYER_PREWARM_DOMAINS` to a comma-separated list of domains or full URLs
(`steamdb.info,https://example.com/`). While the service runs, a background worker
checks each cached entry and solves again when it is missing or has less than
`CF_SLAYER_PREWARM_MARGIN` seconds left. It sleeps `CF_SLAYER_PREWARM_INTERVAL`
between passes, tracks in-flight domains so passes cannot duplicate a solve, and
counts failures under `prewarm_errors` in `/stats` without stopping the loop.
Prewarm bypasses the cache read (`force=True`) so a still-valid but near-expiry
credential is actually renewed.

## Verified

Real-site E2E on 2026-11-12, target `https://steamdb.info/` (Cloudflare-managed),
Camoufox v156.0.1-beta.36, Python 3.13.16. Full transcript:
[`docs/e2e-v1.md`](docs/e2e-v1.md).

| Step | Result | Measured |
|---|---|---|
| `POST /solve` (cold) | 200, `cached=false` | 12.94 s solve, 12.98 s HTTP |
| `POST /solve` (warm) | 200, `cached=true` | 0.0038 s HTTP |
| `GET /replay` | 200, `challenge_page=false` | 194,161 bytes, 0.265 s |

v2 acceptance checks (2026-11-12, same machine, offline/local only):

| Check | Result |
|---|---|
| Full offline suite | 70 passed, ruff clean (v1's 36 unchanged) |
| pydoll-only with `CF_SLAYER_CHROME_PATH` pointing at nothing | 502 `SOLVE_FAILED`, `attempts[0].backend="pydoll"`, error names the path; startup unaffected |
| Prewarm against a local cookie-issuing server, `TTL=30 s`, interval 5 s | 2 renewals in 18.5 s, `cached_domains=1`, `prewarm_errors=0`; consumer `/solve` returned `cached=true`, 28 s left, `cache_hits=1` |

The pydoll backend itself was exercised against `https://example.com` (Chrome 157
launch, cookie read, `navigator.userAgent`); it has not yet solved a live
Cloudflare challenge, so treat it as a fallback, not a replacement.

## Tests

```bash
.venv/Scripts/python -m pytest     # 70 offline tests, no browser, no network
.venv/Scripts/ruff check .
```

The engine tests inject a fake browser factory; API tests inject a fake backend
and patch `solver.main.replay`. Chain and prewarm tests use stub backends and a
temporary SQLite cache. Nothing contacts a Cloudflare-protected site; the pydoll
detection test skips when no Chrome is installed.

## Project layout

```
solver/
  main.py      FastAPI app + service: auth, cache/solve/replay orchestration, stats
  engine.py    Backend protocol + Attempt trace + CamoufoxBackend
  chain.py     BackendChain: ordered fallback, skip unavailable, aggregate attempts
  pydoll_backend.py  PydollBackend: installed Chrome over CDP, Chrome/UA detection
  prewarm.py   PrewarmWorker: margin-based renewal loop
  cache.py     SQLiteCache: WAL, TTL, thread-safe, per-domain
  config.py    pydantic-settings + first-run token generation into .env
  errors.py    error codes and HTTP status mapping
client/
  replay.py    curl_cffi replay (impersonate="firefox") + challenge detection
tests/
  test_cache.py
  test_engine_offline.py
  test_api.py
  test_replay.py
  test_chain.py
  test_pydoll.py
  test_prewarm.py
```

## Limits (v2)

- Turnstile / reCAPTCHA / AWS WAF tokens are not produced.
- pydoll is best-effort: it overrides the UA, but stock Chrome is not fingerprint-patched the way Camoufox is, so it may be
  challenged harder. Camoufox stays first in the default chain.
- Prewarm is in-process and interval-based, not a durable scheduler: it starts
  with the app, acts per pass, and restarts with the process. Every renewal is a
  full solve and consumes the same backend budget as a request.
- No proxy pool and no egress pinning; IP-bound clearances only work from the
  solving host.
- One browser process per solve (no pooling); concurrent solves fan out to
  multiple browsers and are not queued.
- `/health` reports `ready` once the app is up; it does not probe the browser
  binaries (`python -m camoufox fetch` must have been run, and pydoll needs
  Chrome). Availability is checked per solve by the chain.
- Camoufox queries GitHub release metadata when fetching and when no browser is
  installed. If that call is rate limited, `fetch` reports "Synced 0 versions"
  and solves fail with `SOLVE_FAILED`; export `GITHUB_TOKEN` to lift the limit
  (Camoufox forwards it to the GitHub API).

## Roadmap (v3)

- **FlareSolverr backend.** Optional HTTP backend talking to a FlareSolverr
  instance; same `Backend` protocol, placed after Camoufox.
- **Rust cfsolve backend.** Pure-protocol solver as a final fallback.
- **Proxy pool.** Sticky per-domain egress with health scoring, so solves and
  replays share the same IP; makes the service usable off the solving host.
- **Go client.** Native HTTP replay with the cached UA + cookie, early renewal,
  and 403 self-healing, for high-throughput consumers.

## License

MIT.
