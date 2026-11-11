# E2E verification — v1

Date: 2026-11-12 (local, Windows 11 10.0.26200)
Target: `https://steamdb.info/` — Cloudflare-managed challenge.
Result: **PASS** — solve, cache hit, and replay all succeeded on the first run.

## Environment

| Component | Version |
|---|---|
| Python | 3.13.16 |
| fastapi | 0.162.4 |
| uvicorn | 0.54.0 |
| camoufox (package) | 0.5.8 |
| camoufox (browser) | v156.0.1-beta.36 (`official/stable`, build Oct 6, SHA256 `096fd4832a7a`) |
| playwright | 1.62.0 |
| curl_cffi | 0.17.0 |

Browser install: `python -m camoufox fetch` (2.4 GB on disk, wall 198 s).

First fetch attempt failed without a GitHub token:

```
Syncing repositories...
  Official... Error: 403 Client Error: rate limit exceeded for url:
https://api.github.com/repos/camoufox/camoufox/releases
  ...
Synced 0 versions from 0 repos.
v156.0.1-beta.36, the browser this camoufox release pairs with, is not in
official's releases. Run 'camoufox sync' and try again.
```

Cause: unauthenticated GitHub API calls from this IP were rate limited.
Fix (operational, no code change): export a token before fetching.

```bash
export GITHUB_TOKEN=$(gh auth token)
python -m camoufox fetch
# -> Synced 34 versions from 3 repos.
# -> Camoufox v156.0.1-beta.36 installed.
```

Service start:

```bash
.venv/Scripts/python -m uvicorn solver.main:app --host 127.0.0.1 --port 8878
# INFO: Application startup complete.
# INFO: Uvicorn running on http://127.0.0.1:8878
```

## 1. Solve (cold cache)

```bash
TOKEN=$(grep '^CF_SLAYER_AUTH_TOKEN=' .env | cut -d= -f2)
time curl -s --max-time 130 -w "\nHTTP %{http_code} total=%{time_total}s\n" \
  -X POST http://127.0.0.1:8878/solve \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"url":"https://steamdb.info/"}'
```

Response:

```json
{"clearance":"CsTueaXo…<redacted, 328 chars>…HL0az","user_agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:156.0) Gecko/20100101 Firefox/156.0","domain":"steamdb.info","expires_in":1799,"cached":false,"solve_seconds":12.94}
HTTP 200 total=12.984348s
real    0m13.048s
```

Notes:

- `cf_clearance` appeared ~13 s after launch; the engine polls every 2 s and
  stops as soon as the cookie and UA are both readable, well inside the 90 s
  challenge cap (120 s overall budget unused).
- `expires_in` 1799 comes from the cookie's own expiry, clamped to the 1800 s
  cache TTL.
- The value is redacted in this document; it is IP-bound and expires in 30 min.

## 2. Solve (warm cache)

Same request again:

```json
{"clearance":"CsTueaXo…<redacted, 328 chars>…HL0az","user_agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:156.0) Gecko/20100101 Firefox/156.0","domain":"steamdb.info","expires_in":1796,"cached":true,"solve_seconds":0.0}
HTTP 200 total=0.003777s
real    0m0.074s
```

No browser start, no solve stats increment; remaining TTL counts down (1799 → 1796).

## 3. Replay

```bash
curl -s --max-time 60 -w "\nHTTP %{http_code} total=%{time_total}s\n" \
  "http://127.0.0.1:8878/replay?url=https://steamdb.info/" \
  -H "Authorization: Bearer $TOKEN"
```

Response:

```json
{"url":"https://steamdb.info/","domain":"steamdb.info","status":200,"length":194161,"challenge_page":false,"replay_seconds":0.265,"resolved":false}
HTTP 200 total=0.269056s
real    0m0.345s
```

`status=200`, `challenge_page=false`, 194,161 bytes — the real SteamDB page, not
an interstitial. `resolved=false` because the cached credential was used
without a re-solve.

## Stats after the run

```json
{"cache_hits":1,"solves":1,"errors":0,"avg_solve_seconds":12.94,"uptime_seconds":34.7,"cached_domains":1}
```

Cache row: domain `steamdb.info`, clearance length 328, UA equal to the solve-time
UA, expiry ~30 min out.

## Supporting checks

- **UA forwarding**: a replay with `impersonate="firefox"` sends the explicit
  `User-Agent` header (verified against `https://httpbin.org/headers`) and the
  `cf_clearance` cookie is forwarded as a request cookie.
- **Domain normalization**: `https://STEAMDB.INFO:8443/x` → `steamdb.info`
  (lowercased, port stripped) in both `solver.engine.domain_of` and
  `client.replay.domain_of`.
- **503/403 recovery path** is covered offline (`test_replay_403_triggers_one_resolve`,
  `test_replay_still_403_after_resolve_maps_to_upstream_403`), not re-triggered
  against the live site to avoid pointless solves.

## Verdict

No code changes were required by this run. v1 solves a real Cloudflare-protected
site in ~13 s, serves the credential from cache in ~4 ms, and replays it for a
200 in ~0.27 s. Remaining v1 limits (single backend, no proxy pool, IP-bound
clearance) are unchanged; the GitHub-token requirement for fetching is an
environment/network fact, now documented in the README.
