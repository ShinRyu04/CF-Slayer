# cf-slayer

Cloudflare clearance solver and replay service. It drives a stealth Firefox
(Camoufox) through a challenge, harvests the `cf_clearance` cookie together with
the exact User-Agent that earned it, caches both per domain for 30 minutes, and
replays the credential with `curl_cffi` (Firefox TLS fingerprint).

```
POST /solve  {url}                 ──►  {clearance, user_agent, domain, expires_in, cached}
      │                                   cache hit: no browser starts
      └─ miss ─► Camoufox: goto → poll cf_clearance (2s, max 90s) → UA
                        └─► SQLite cache (per domain, TTL 30 min)

GET /replay?url=...                ──►  {status, length, challenge_page, ...}
      └─ cached cookie + solve-time UA ─► target
            └─ 403/challenge page ─► invalidate, solve once more, retry once
```

## Requirements

- Python 3.12+ (developed on 3.13, Windows)
- ~700 MB disk for the patched Firefox once fetched
- CMake/Git Bash/PowerShell for the commands below

Camoufox patches fingerprinting in Firefox's C++ layer (WebGL renderer, canvas,
AudioContext, WebRTC, screen geometry, …), which is why it beats stock Playwright
on Cloudflare-managed challenges.

## Install

Git Bash:

```bash
cd cf-slayer
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt
.venv/Scripts/python -m camoufox fetch      # one-time, ~700 MB
```

PowerShell:

```powershell
cd cf-slayer
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m camoufox fetch
```

`uv` works too: `uv venv --python 3.13 .venv && uv pip install -r requirements.txt --python .venv/Scripts/python.exe`.

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
| GET | `/stats` | Bearer | cache hits, solves, errors, average solve time |

### curl examples

```bash
# Solve
curl -s -X POST http://127.0.0.1:8878/solve \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://target.example/","want":"clearance"}'

# -> {"clearance":"...","user_agent":"Mozilla/5.0 ... Firefox/156.0",
#     "domain":"target.example","expires_in":1789,"cached":false,"solve_seconds":14.2}

# Replay (uses the cached cookie + the UA that was used to solve)
curl -s "http://127.0.0.1:8878/replay?url=https://target.example/" \
  -H "Authorization: Bearer $TOKEN"

# -> {"url":"https://target.example/","domain":"target.example","status":200,
#     "length":202117,"challenge_page":false,"replay_seconds":1.34,"resolved":false}

# Health and stats
curl -s http://127.0.0.1:8878/health
curl -s http://127.0.0.1:8878/stats -H "Authorization: Bearer $TOKEN"
```

`/solve` accepts only `want: "clearance"` in v1. Any other value is `BAD_REQUEST`.

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
`CF_SLAYER_CHALLENGE_TIMEOUT` (90), `CF_SLAYER_HEADLESS` (true).

## Tests

```bash
.venv/Scripts/python -m pytest     # 36 offline tests, no browser, no network
.venv/Scripts/ruff check .
```

The engine tests inject a fake browser factory; API tests inject a fake backend
and patch `solver.main.replay`. Nothing contacts a Cloudflare-protected site.

## Project layout

```
solver/
  main.py      FastAPI app + service: auth, cache/solve/replay orchestration, stats
  engine.py    Backend protocol + CamoufoxBackend (one browser per solve)
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
```

## Limits (v1)

- Single backend: Camoufox. Turnstile / reCAPTCHA / AWS WAF tokens are not
  produced.
- No proxy pool and no egress pinning; IP-bound clearances only work from the
  solving host.
- One browser process per solve (no pooling); concurrent solves fan out to
  multiple browsers and are not queued.
- `/health` reports `ready` once the app is up; it does not probe the browser
  binary (`python -m camoufox fetch` must have been run).
- Camoufox queries GitHub release metadata at launch. If that call is rate
  limited, solves fail with `SOLVE_FAILED`; export `GITHUB_TOKEN` to lift the
  limit (Camoufox forwards it to the GitHub API).

## Roadmap (v2)

- **Backend fallback chain.** `engine.py` already exposes a `Backend` protocol;
  add a pure-protocol (Rust cfsolve) and/or pydoll backend and try them in order
  before Camoufox, recording the winning backend.
- **Proxy pool.** Sticky per-domain egress with health scoring, so solves and
  replays share the same IP; makes the service usable off the solving host.
- **Prewarm.** Renew credentials shortly before expiry so the expensive solve
  never lands on a request path.
- **Go client.** Native HTTP replay with the cached UA + cookie, early renewal,
  and 403 self-healing, for high-throughput consumers.

## License

MIT.
