# Changelog

Notable changes, backfilled from git history. Dates use DD-Mon-YYYY.

## v5 — 12-Nov-2026

### Added

- `POST /solve` with `want=turnstile` returns a one-shot Turnstile token plus
  the User-Agent that solved it. Tokens never touch the SQLite cache.
- Camoufox token backend: blank page, navigate to the target, explicit widget
  render with the supplied `sitekey`, poll the hidden input or callback for up
  to 60 seconds.
- Capability-aware backend chain: backends without token support are skipped
  and recorded in `attempts`.
- Go `SolveTurnstile(ctx, url, sitekey)`: no client-side cache, no renewal.
- `tokens_solved` and `token_errors` statistics, separate from clearance
  counters.
- 14 offline Python tests and 5 Go tests for the token path.

### Notes

- `sitekey` must match `^0x[0-9a-zA-Z]{20,}$`; Cloudflare's published
  always-pass test key uses the `1x` prefix and is rejected by that contract.
  Live verification against a real site was therefore not performed.

## v4 — 12-Nov-2026

### Added

- `client-go/`: standard-library client with per-domain credential cache,
  renewal at 80% of TTL, 403 self-healing (one re-solve, one retry), and no
  credential logging.
- Worker pool over `CF_SLAYER_SERVICES` with consistent hashing, removal after
  three consecutive failures, health probes every 5 seconds, and recovery
  after two successful probes.
- `scripts/run-workers.ps1` for running multiple workers against one shared
  SQLite cache.
- 15 Go tests and a demo CLI.

### Verified

- Cross-process cache: one real Camoufox solve on a local target, second worker
  served the same domain from cache.
- Replay latency on a local fake target: Python `/replay` median ≈ 11.1 ms,
  Go client median ≈ 1.1 ms.

## v3 — deferred (12-Nov-2026)

Proxy pool support was planned and explicitly deferred. The deployment had a
clean egress IP and no proxy resources, so no proxy code was written. The
roadmap in the README still lists it as future work.

## v2 — 12-Nov-2026

### Added

- Ordered backend chain (Camoufox then pydoll) with per-attempt tracing and
  availability probes.
- Background prewarm worker that renews cached credentials before expiry.

### Changed

- Internal identifiers, comments, and messages switched to Simplified Chinese;
  the public ASCII contract was left unchanged.

## v1.x — 12-Nov-2026

### Fixed

- Real-site end-to-end corrections; the clearance path was verified against
  steamdb.info and recorded in `docs/e2e-v1.md`.

## v1 — 12-Nov-2026

### Added

- FastAPI service with `/solve`, `/replay`, `/health`, and `/stats`.
- Camoufox backend harvesting `cf_clearance` and the exact User-Agent.
- SQLite cache (WAL, per-domain, 30-minute TTL).
- `curl_cffi` replay with the cached cookie and User-Agent, including one
  re-solve and retry when the target returns a challenge page.
- Bearer authentication and runtime statistics.
