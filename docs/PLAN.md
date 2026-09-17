# Service Status Aggregator — v1 Implementation Plan

Status: **plan only, nothing implemented yet.** This document is the handoff for
whichever model or person builds v1. Read it fully before writing code.

How to use this plan:

- Implement the phases in §14 in order. Each phase ends with a commit and a green
  `uv run pytest` + `uv run ruff check`.
- Every decision below is either **settled** (the spec says so) or a **default**
  tagged `Q#`. A `Q#` default applies unless the owner answered that question
  in §17. Do not re-open settled items.
- Do not widen scope. §18 lists what is deliberately deferred.

---

## 1. Purpose and scope

A standalone, read-only dashboard that discovers and monitors other home
services on a Tailscale network. It runs on the same machine as the owner's
OpenClaw installation and is reached over Tailscale.

**In scope (v1):**

1. `POST /register` upsert endpoint, SQLite-backed, keyed on `(name, host)`.
2. Background health-poll loop hitting each service's `health_url`.
3. One server-rendered status page plus a read-only view of effective config.
4. `/health` on the aggregator itself (readiness, not just liveness).
5. Three deployment paths with install scripts: systemd, plain venv, Docker.
6. README documenting the payload, secrets setup and rotation, and the
   integration paragraph for other services.

**Out of scope (v1):** capability-query protocol, ntfy alerting, editing any
other service's config, any write UI beyond `/register`, TLS termination
(Tailscale already encrypts on the wire), multi-user auth on the page.

---

## 2. Settled decisions and defaults

| Topic | Decision | Source |
|---|---|---|
| Language / packaging | Python ≥ 3.11 (`tomllib`), `pyproject.toml`, `uv`, `uv.lock` committed | spec |
| Web stack | FastAPI + uvicorn (single process, no workers) + httpx (async polling) + Jinja2. Chosen over Bottle for built-in request validation and async polling; owner is fine with either. | owner (Q8) |
| Storage | stdlib `sqlite3`, WAL mode, `busy_timeout=5000`, single writer lock | default |
| Config | One TOML file; missing/invalid ⇒ exit 2 with a clear message | spec |
| Bind address | `auto`: Tailscale IPv4 if found, else `127.0.0.1` + SSH-tunnel note. **Never `0.0.0.0`.** | spec |
| Default port | `8720` (does not collide with OpenClaw's default gateway port 18789). Installers and `check-config` verify the port is free before starting. | owner (Q4) |
| Poll interval | 30 s default, configurable | spec |
| Poll timeout | 5 s default, configurable | default |
| Healthy response | HTTP 200 only; everything else is `down` | spec |
| Staleness window | 900 s default (3 missed 5-minute re-registrations) | default |
| Stale handling | Computed at read time, never stored, never deleted | spec |
| Registration auth | Shared bearer token, **required by default** | owner (Q1) |
| Poll-target restriction | `health_url` host must equal registered `host`; resolved IP must be inside `allowed_target_cidrs` | Q9 default |
| History | `services` row holds latest values; `status_events` table stores transitions only, pruned after 90 days | owner (Q6) |
| Removal of dead entries | CLI subcommand only, no HTTP delete | owner (Q7) |
| Self-listing | Aggregator registers its own row (`register_self = true`) | owner (Q5) |
| Non-tailnet services | Optional `[[static_services]]` entries in the TOML; the aggregator re-registers them itself each cycle so they are polled and never stale (see §8) | Q11 default |
| Target host | Latest Ubuntu LTS (systemd ≥ 255, Python 3.12 available); uv still pins the interpreter | owner (Q4) |
| CI | GitHub Actions: ruff + pytest only, on push and PR. Build/test only, not deployment. ~1 min per run; free on public repos, well inside the 2000 min/month free tier on private ones. | owner (Q10) |
| Logging | Plain text, `RotatingFileHandler`, 5 MiB × 5 by default; also stderr when not a daemon | spec |
| Secrets | systemd: `LoadCredentialEncrypted` via `systemd-creds`; venv/Docker: `.env` | spec |
| Docker network | `--network host` (needed to bind the Tailscale IP and reach tailnet peers) | owner (Q3) |
| systemd | `Type=notify`, READY + WATCHDOG via `$NOTIFY_SOCKET` (no dependency), `Restart=on-failure`, `RestartSec=10`, `StartLimitIntervalSec=300`, `StartLimitBurst=5` | spec |
| Service user | `svcstatus` system user, no login shell, on all three paths | default |
| Paths | `/opt/service_status_aggregator` (code), `/etc/service_status_aggregator/config.toml`, `/var/lib/service_status_aggregator` (db), `/var/log/service_status_aggregator` (log) | owner (Q4) |
| License | AGPL-3.0 (already in repo); mirror in `pyproject.toml` | repo |

---

## 3. Security and privacy posture

The spec is silent on several points that matter for a service that accepts
network writes and then makes outbound requests on their behalf. These are the
guardrails v1 must implement; the owner's preferences favour secure defaults.

1. **Authenticated registration.** `POST /register` requires
   `Authorization: Bearer <token>` when a token is configured. If
   `registration.require_token = true` (default) and no token can be loaded at
   startup, exit with a clear error. If set to `false`, log a WARNING at startup
   and show a banner on the status page. Compare with `hmac.compare_digest`.
2. **No SSRF via `health_url`.** The aggregator will fetch whatever URL a
   registrant supplies, so it must be constrained:
   - scheme must be `http` or `https`;
   - URL hostname must equal the registered `host` (case-insensitive);
   - if `host` is an IP literal it must fall inside `allowed_target_cidrs`;
     if it is a hostname, resolve it at registration time **and** at each poll
     and require every resolved address to be inside the allowlist;
   - default allowlist: `100.64.0.0/10` (Tailscale CGNAT), `127.0.0.0/8`,
     `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, `fd00::/8`;
   - redirects are **not** followed; `GET` only; no request headers beyond
     `User-Agent`; response body capped at 64 KiB and discarded.
3. **`log_path` is display-only.** The aggregator stores and shows the string;
   it never opens, tails or serves another service's log file.
4. **Links are sanitised.** `config_page_url` must be empty or `http(s)://…`.
   Render with `rel="noopener noreferrer"`. Jinja2 autoescape on everywhere.
5. **No third-party assets.** The page is self-contained HTML/CSS; no CDN
   fonts, scripts or trackers. Send `Content-Security-Policy: default-src 'self'`,
   `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`.
6. **Input limits.** Request body ≤ 16 KiB, JSON only. Field limits in §6.
7. **Never log the token.** Redact it in the config view and in any error
   message that echoes headers or environment.
8. **Least privilege at deploy time.** Dedicated non-root user; systemd
   hardening (§11); Docker `--read-only`, `--cap-drop ALL`,
   `--security-opt no-new-privileges`, explicit `--user`.
9. **Loopback fallback is loud.** If bind resolves to `127.0.0.1`, log a
   WARNING with the SSH tunnel command every startup, not just once.
10. **Pinned dependencies.** Commit `uv.lock`. Keep the dependency list short.

**Non-tailnet services.** The aggregator binds only to its Tailscale IP, so a
service that is not on the tailnet cannot reach `/register`. Polling in the
other direction still works (outbound from the host). Such services are
described statically in the TOML (§8) instead of self-registering. Their
health URLs must still pass the allowlist; a service on a public IP needs its
address added to `allowed_target_cidrs` explicitly, never a wildcard.

**Tailscale identity as auth (later).** The owner suggested using Tailscale as
the trust boundary. `tailscale whois <src-ip>` can identify the registering
node and would remove the shared token for tailnet peers. Deferred to a later
phase (§18); the bearer token stays for v1 because it also covers same-host
registrants and is one line for integrators.

Residual risks to state in the README: DNS rebinding between the two resolve
checks is possible but low value on a tailnet; anyone on the tailnet can read
the page (acceptable by design, page contains hostnames and file paths).

---

## 4. Architecture

Single asyncio process:

```
uvicorn (programmatic, uvicorn.Server, one worker)
 └─ FastAPI app
     ├─ POST /register          → storage.upsert()
     ├─ GET  /                  → Jinja2 status page
     ├─ GET  /api/services      → JSON of the same data
     ├─ GET  /config            → read-only effective config (secrets redacted)
     └─ GET  /health            → readiness JSON, 200 or 503
 ├─ poller task   (every interval_seconds: gather all services, semaphore N)
 ├─ janitor task  (hourly: prune status_events > retention, VACUUM never)
 ├─ self-register task (optional, every 300 s, writes own row)
 └─ sd_notify heartbeat (WATCHDOG=1 every WatchdogSec/3 while poller is healthy)
```

Package layout:

```
src/service_status_aggregator/
  __init__.py        __version__
  __main__.py        python -m entry
  cli.py             argparse: run | check-config | remove | version
  config.py          dataclasses + TOML load + validation + secret loading
  netbind.py         Tailscale IP detection, bind resolution
  storage.py         sqlite schema, migrations, upsert/list/prune
  models.py          pydantic request/response models, validators
  poller.py          poll loop, classification, transition logging
  sdnotify.py        tiny NOTIFY_SOCKET client
  logging_setup.py   rotating file + stderr handlers
  web/
    app.py           FastAPI factory, routes, security headers middleware
    templates/       index.html, config.html, base.html
    static/style.css
```

---

## 5. Data model (SQLite)

```sql
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);

CREATE TABLE IF NOT EXISTS services (
  id                    INTEGER PRIMARY KEY,
  name                  TEXT    NOT NULL,          -- normalised lowercase
  host                  TEXT    NOT NULL,          -- as registered, lowercased
  port                  INTEGER NOT NULL,
  health_url            TEXT    NOT NULL,
  log_path              TEXT    NOT NULL DEFAULT '',
  config_page_url       TEXT    NOT NULL DEFAULT '',
  first_registered_at   TEXT    NOT NULL,          -- ISO-8601 UTC
  last_registered_at    TEXT    NOT NULL,
  registration_count    INTEGER NOT NULL DEFAULT 1,
  source                TEXT    NOT NULL DEFAULT 'register',  -- register|static|self
  last_status           TEXT    NOT NULL DEFAULT 'unknown',  -- up|down|unknown
  last_checked_at       TEXT,
  last_response_ms      REAL,
  last_failure_reason   TEXT,
  last_status_change_at TEXT,
  UNIQUE (name, host)
);

CREATE TABLE IF NOT EXISTS status_events (
  id          INTEGER PRIMARY KEY,
  service_id  INTEGER NOT NULL REFERENCES services(id) ON DELETE CASCADE,
  at          TEXT    NOT NULL,
  from_status TEXT    NOT NULL,
  to_status   TEXT    NOT NULL,
  reason      TEXT
);
CREATE INDEX IF NOT EXISTS ix_status_events_service_at ON status_events(service_id, at);
```

`stale` is derived: `now - last_registered_at > staleness_seconds`. It is a
registration-liveness flag independent of health; a stale service is still
polled. The UI shows two indicators per row: **Health** (up / down / unknown)
and **Registration** (fresh / stale).

Migrations: `schema_version` starts at 1; `storage.migrate()` applies numbered
steps forward only. v1 has a single step.

---

## 6. HTTP API contract

### `POST /register`

Headers: `Content-Type: application/json`,
`Authorization: Bearer <token>` (required when a token is configured).

Body (all fields required; unknown fields rejected):

| Field | Type | Rules |
|---|---|---|
| `name` | string | 1–64 chars, `^[a-z0-9][a-z0-9_-]*$` after lowercasing |
| `host` | string | 1–253 chars; valid hostname or IP literal; lowercased |
| `port` | int | 1–65535 |
| `health_url` | string | ≤ 2048; `http`/`https`; hostname == `host`; target IP inside allowlist |
| `log_path` | string | ≤ 1024; must be absolute (`/…`) or empty |
| `config_page_url` | string | ≤ 2048; empty or `http(s)://…` |

Responses:

- `201 Created` on first registration, `200 OK` on re-registration. Body: the
  normalised stored record plus `registration_count`.
- `400` malformed JSON / oversize body; `422` validation failure (field-level
  detail); `401` missing or bad token; `415` wrong content type.

Semantics: upsert on `(name, host)`. Every field in the payload overwrites the
stored value. `first_registered_at` and status columns are preserved.

### `GET /health` (aggregator readiness)

```json
{"status":"ok","version":"0.1.0","uptime_seconds":123,
 "checks":{"db":"ok","poller":"ok","bind":"tailscale"}}
```

- `db`: `SELECT 1` succeeds and the db directory is writable.
- `poller`: last cycle completed within `2 × interval_seconds` (or the process
  is younger than one interval).
- Any failed check ⇒ HTTP 503 with the failing check named. No secrets, no
  hostnames of other services.

### `GET /` — status page

Server-rendered table, `<meta http-equiv="refresh" content="30">`. Columns:
name, host:port, health badge, response ms, last checked (relative + UTC),
registration badge + last registered, failure reason, config link, log path
(plain text). Sort: down → stale → unknown → up, then by name. Header shows
aggregator bind address, poll interval, staleness window, and a warning banner
if the token is disabled or bind fell back to loopback.

### `GET /api/services` — same rows as JSON, for future consumers.

### `GET /config` — effective configuration rendered read-only, token redacted
as `***`, plus the resolved bind address and the config file path. This is the
v1 "config form": view only, no editing.

---

## 7. Health poller

Every `interval_seconds`:

1. Load all services (including stale ones).
2. For each, under an `asyncio.Semaphore(max_concurrent)`:
   - re-validate target (§3.2); a validation failure is classified `down`
     with reason `target_rejected: …`;
   - `GET health_url`, `timeout_seconds`, `follow_redirects=False`;
   - classify: `200` ⇒ `up`; other status ⇒ `down` (`http_<code>`);
     `httpx.ConnectError` ⇒ `down` (`connection_error: <detail>`);
     `httpx.TimeoutException` ⇒ `down` (`timeout_after_<n>s`);
     other exception ⇒ `down` (`error: <ExceptionType>: <msg>`), logged at ERROR.
   - record `last_status`, `last_checked_at`, `last_response_ms`,
     `last_failure_reason`.
3. On a status change, insert a `status_events` row, set
   `last_status_change_at`, and log at INFO (`→ up`) or WARNING (`→ down`).
4. Every poll result is logged at DEBUG; every failed poll at WARNING with the
   reason (spec: fail loudly). Note in README that a long-down service produces
   ~2 lines/minute and that size-based rotation bounds this.
5. One service's exception never aborts the cycle; the cycle itself is wrapped
   so the loop survives and logs at ERROR.
6. After each completed cycle set `poller_last_cycle_at` (used by `/health`)
   and send `WATCHDOG=1`.

---

## 8. Configuration

Location precedence: `--config PATH` > `$SSA_CONFIG` >
`/etc/service_status_aggregator/config.toml`. If none exists ⇒ exit 2.

`config.example.toml` (ship in repo; installers copy it):

```toml
[server]
bind = "auto"        # "auto" | "tailscale" (strict: exit if no Tailscale IP) | explicit IP. Never 0.0.0.0.
port = 8720

[storage]
db_path = "/var/lib/service_status_aggregator/aggregator.db"

[polling]
interval_seconds = 30
timeout_seconds = 5
max_concurrent = 10

[registration]
staleness_seconds = 900
require_token = true
allowed_target_cidrs = ["100.64.0.0/10", "127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fd00::/8"]
register_self = true

[history]
retention_days = 90

# Services that cannot self-register (not on the tailnet). The aggregator
# re-registers these itself every poll cycle, so they are never stale.
# Same field rules as POST /register (§6); host must match health_url's host.
[[static_services]]
name = "nas"
host = "192.168.1.20"
port = 5000
health_url = "http://192.168.1.20:5000/health"
log_path = ""
config_page_url = "http://192.168.1.20:5000/"

[logging]
path = "/var/log/service_status_aggregator/aggregator.log"
level = "INFO"
max_bytes = 5242880
backup_count = 5
```

Validation at load: positive intervals, `timeout < interval`, port range,
each `[[static_services]]` entry passes the §6 validators (including the
allowlist),
parseable CIDRs, bind is `auto`/`tailscale`/IP and **not** `0.0.0.0`/`::`,
db and log directories exist and are writable (create the file, not the dir).
Any failure ⇒ exit 2 listing every problem, not just the first.

**Secret (registration token) precedence:**
`$CREDENTIALS_DIRECTORY/registration_token` (systemd) >
`$SSA_REGISTRATION_TOKEN` (from `.env` for venv/Docker, loaded by the
installer/compose, not by the app) > absent. The token is never read from the
TOML file. Trim trailing newline. Minimum 16 characters, else refuse.

---

## 9. Bind address resolution (`netbind.py`)

For `bind = "auto"` or `"tailscale"`, try in order, retrying the whole chain up
to 10 times with 3 s sleeps (Tailscale may come up after us at boot):

1. `tailscale ip -4` (subprocess, 3 s timeout) if the binary exists.
2. `ioctl(SIOCGIFADDR)` on interface `tailscale0` (pure stdlib).
3. `ip -j -4 addr show` and pick the first address inside `100.64.0.0/10`.

If found ⇒ bind it, log `bind=tailscale <ip>`. If not:
`auto` ⇒ bind `127.0.0.1`, log WARNING with
`ssh -L 8720:127.0.0.1:8720 <host>` note; `tailscale` ⇒ exit 3.
Explicit IP ⇒ must be assigned to a local interface, else exit 3.

Also register the aggregator's own row (if `register_self`) using the resolved
IP so the dashboard shows the aggregator itself.

---

## 10. Logging

- `RotatingFileHandler(path, maxBytes, backupCount)`, format
  `%(asctime)s %(levelname)s %(name)s: %(message)s` in UTC.
- Add a stderr handler when stdout is a TTY or `$SSA_LOG_STDERR=1`
  (Docker path sets this so `docker logs` works; file logging still on).
- uvicorn access log off by default (page auto-refresh would flood it);
  uvicorn error log routed into our handlers.
- Startup logs: version, config path, resolved bind, db path, token
  enabled/disabled, allowlist.

---

## 11. systemd integration

`deploy/systemd/service-status-aggregator.service`:

```ini
[Unit]
Description=Service Status Aggregator
After=network-online.target tailscaled.service
Wants=network-online.target
StartLimitIntervalSec=300
StartLimitBurst=5

[Service]
Type=notify
NotifyAccess=main
User=svcstatus
Group=svcstatus
WorkingDirectory=/opt/service_status_aggregator
ExecStart=/opt/service_status_aggregator/.venv/bin/service-status-aggregator run --config /etc/service_status_aggregator/config.toml
Restart=on-failure
RestartSec=10
WatchdogSec=90
LoadCredentialEncrypted=registration_token:/etc/credstore.encrypted/ssa-registration_token
# hardening
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
PrivateDevices=true
ProtectKernelTunables=true
ProtectControlGroups=true
RestrictSUIDSGID=true
LockPersonality=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
ReadWritePaths=/var/lib/service_status_aggregator /var/log/service_status_aggregator
UMask=0077

[Install]
WantedBy=multi-user.target
```

`sdnotify.py`: if `$NOTIFY_SOCKET` is set, send datagrams `READY=1` once the
db is migrated and the socket is bound, `WATCHDOG=1` from the heartbeat task
(only while the poller is healthy, so a wedged poller gets us restarted),
`STOPPING=1` on shutdown. Silently no-op when the variable is absent.

Token creation and rotation (documented in README, scripted in installer):

```
sudo systemd-creds encrypt --name=registration_token - /etc/credstore.encrypted/ssa-registration_token
sudo systemctl restart service-status-aggregator
```

Reminder in README: keep a plaintext copy of the token in a password manager;
rotation = generate new token, re-encrypt, restart aggregator, update every
registering service.

---

## 12. Deployment paths and install scripts

All scripts: `set -euo pipefail`, run as root via sudo, idempotent, print each
step, never echo the token, check that the configured port is not already
listening (`ss -ltn`) and abort with the offending process if it is, finish by curling `/health` on the resolved bind
and printing the status page URL. Shared helper `deploy/common.sh` for user
creation, directory creation, and Tailscale IP display.

**`deploy/systemd/install.sh`**: check `uv` (offer the official installer
command, do not auto-run curl|sh without confirmation); create `svcstatus`;
copy repo to `/opt/...`; `uv sync --frozen --no-dev`; copy
`config.example.toml` to `/etc/...` if absent; generate a token
(`openssl rand -hex 32`), print it once with the password-manager reminder,
encrypt with `systemd-creds`; install the unit; `daemon-reload`, `enable --now`;
wait for `/health`; print `journalctl -u` hint.

**`deploy/venv/install.sh`**: same layout under a user-chosen prefix (default
`~/service_status_aggregator`), `uv sync --frozen --no-dev`, writes `.env`
with a generated token (mode 600), prints `uv run service-status-aggregator run
--config …` and the SSH tunnel note if not on Tailscale. No root required.

**`deploy/docker/`**: `Dockerfile` (`python:3.12-slim`, uv, non-root `svcstatus`
with build-arg UID/GID matching the host user, `iproute2` for bind detection,
`HEALTHCHECK` hitting `/health`), `docker-compose.yml` (`network_mode: host`,
`read_only: true`, `cap_drop: [ALL]`, `security_opt: [no-new-privileges]`,
`env_file: .env`, bind mount `<storage>:/data`, `tmpfs: /tmp`),
`install.sh`: verify docker present; create host user `svcstatus` if missing;
prompt for storage dir (default `/srv/service_status_aggregator`), create,
chown; write `config.toml` into it with `/data/...` paths; write `.env` with
generated token; build; `compose up -d`; poll `/health` until 200 or 60 s;
verify the port is listening on the Tailscale IP (`ss -ltn`), fail loudly
otherwise.

---

## 13. Repository layout (target)

```
pyproject.toml  uv.lock  README.md  LICENSE  config.example.toml
src/service_status_aggregator/...           (see §4)
tests/
deploy/common.sh
deploy/systemd/{install.sh,service-status-aggregator.service}
deploy/venv/install.sh
deploy/docker/{Dockerfile,docker-compose.yml,install.sh,.dockerignore}
docs/PLAN.md   (this file)
.github/workflows/ci.yml                     (ruff + pytest on push/PR)
```

`pyproject.toml` essentials: `requires-python = ">=3.11"`, deps
`fastapi`, `uvicorn`, `httpx`, `jinja2`; dev deps `pytest`, `pytest-asyncio`,
`ruff`; `[project.scripts] service-status-aggregator = "service_status_aggregator.cli:main"`;
`license = "AGPL-3.0-or-later"`; ruff with `select = ["E","F","I","B","S"]`
(S = bandit rules, keep them on).

---

## 14. Implementation phases (each = one commit, tests green)

**Phase 0 — scaffold.** pyproject, uv.lock, package skeleton, `config.py`
with full validation and exit-2 behaviour, `logging_setup.py`, `cli.py` with
`check-config` and `version`, `config.example.toml`, CI workflow.
*Accept:* `uv run service-status-aggregator check-config --config config.example.toml`
reports each problem; missing file exits 2.

**Phase 1 — storage + register.** `storage.py` (schema, migrate, upsert,
list, prune, remove), `models.py` validators incl. SSRF rules, `POST /register`,
bearer-token check, security-header middleware.
*Accept:* tests for 201-then-200 upsert, field-level 422s, 401 without token,
rejection of `health_url` whose host ≠ `host`, rejection of out-of-allowlist
IP, oversize body 400, `first_registered_at` preserved on re-register.

**Phase 2 — poller.** `poller.py`, transition events, `/health` readiness,
`sdnotify.py`. Use a mocked httpx transport in tests.
*Accept:* 200⇒up, 500⇒down `http_500`, timeout⇒down with reason,
connection refused⇒down, transition rows only on change, `/health` 503 when
poller cycle is overdue, one failing target does not stop the cycle.

**Phase 3 — web.** Templates, `/`, `/api/services`, `/config` (redacted),
stale computation, sort order, banners, `register_self`, `[[static_services]]`
seeding with `source` label shown on the page.
*Accept:* rendered page contains each service exactly once; stale badge appears
when `last_registered_at` is old; token never appears in `/config` output;
CSP header present on every response.

**Phase 4 — bind + runtime.** `netbind.py` with the three detection methods
and retry, `run` subcommand wiring uvicorn programmatically, graceful shutdown
(cancel tasks, `STOPPING=1`), loopback warning.
*Accept:* unit tests with mocked subprocess/ioctl for each detection path;
`bind = "0.0.0.0"` rejected at config load.

**Phase 5 — deployment.** Unit file, three installers, Dockerfile, compose.
*Accept:* `shellcheck` clean; Docker image builds in CI; a container started
with `--network host` in CI answers `/health` (CI has no Tailscale, so it must
log the loopback warning and still come up).

**Phase 6 — README + polish.** README per §16, `remove` CLI subcommand,
`prune` janitor, final ruff/mypy pass.

---

## 15. Testing plan

- `pytest` + `httpx.ASGITransport` against the FastAPI app with a temp SQLite
  file; poller tests use `httpx.MockTransport`.
- No network access in tests; bind detection is mocked.
- Security-focused cases are mandatory, not optional: SSRF rejections,
  token constant-time compare path, redaction in `/config`, header presence,
  `javascript:` URL rejection, body-size limit.
- `ruff` with bandit (`S`) rules; `shellcheck` on installers.

---

## 16. README contents checklist

1. What it is, what it deliberately does not do (§1).
2. Quick start for each deployment path.
3. **Registration payload** — the table from §6 plus a full curl example and a
   stdlib-only Python snippet (`urllib.request`) other services can copy,
   including best-effort error handling and the 5-minute re-register loop.
4. **Integration paragraph** (revised Prompt 2) — see below.
5. Secrets: where the token lives on each path, how to create it, how to
   rotate it, the password-manager reminder.
6. Bind behaviour and the SSH tunnel fallback command.
7. Logging location, rotation, and what a down service looks like in the log.
8. `/health` semantics and how to use it from systemd or Docker.
9. Removing a decommissioned service (CLI).
10. Security notes and residual risks (§3).
11. Roadmap: capability query, ntfy.

**Revised integration paragraph for future project prompts** (the only change
from the owner's Prompt 2 is the auth header and the exact endpoint/port):

> This service should self-register with the Service Status Aggregator running
> on the same Tailscale network. On startup, and then every 5 minutes, send a
> `POST` to `http://<aggregator-tailscale-ip>:8720/register` with header
> `Authorization: Bearer <SSA_REGISTRATION_TOKEN>` (read the token from this
> service's own secret store, never hard-code it) and a JSON body with exactly
> these fields: `name` (short unique identifier, lowercase letters, digits,
> `-`, `_`), `host` (this service's Tailscale hostname or IP; the same host
> that appears in `health_url`), `port` (the port it listens on),
> `health_url` (full URL to this service's own `/health` endpoint),
> `log_path` (absolute path to its log file), and `config_page_url` (deep link
> to its own config or settings page, or an empty string). Treat registration
> as best-effort: on any failure log a warning and keep running; this service
> must not depend on the aggregator. This service must already expose a
> `/health` endpoint per the standard project template, since the aggregator
> polls it directly (HTTP 200 = healthy, anything else = down).

---

## 17. Decisions log and remaining questions

Answered by the owner on 2026-09-17 (applied throughout this document):

| # | Answer |
|---|---|
| Q1 | Token required by default. |
| Q2 | No template to mirror; layout in this plan stands. |
| Q3 | Docker host networking accepted. |
| Q4 | Latest Ubuntu; port 8720 status unknown, so installers verify it is free; standard paths exist. |
| Q5 | Aggregator lists itself. |
| Q6 | Transitions table, 90-day retention. |
| Q7 | CLI-only removal. |
| Q8 | Owner usually uses Bottle but accepted the FastAPI stack. |
| Q9 | Not all monitored services are on the tailnet today. Handled by `[[static_services]]` (Q11) and the allowlist. Tailscale-identity auth noted as a later phase. |
| Q10 | CI wanted as long as it costs nothing: ruff + pytest only. |

Remaining questions (defaults apply until answered):

| # | Question | Default if unanswered |
|---|---|---|
| Q11 | For services not on the tailnet, describe them as `[[static_services]]` in the TOML so they are polled without self-registering? | **Yes.** |
| Q12 | Where do those non-tailnet services live: LAN (RFC1918), same host (loopback), or public internet? Public IPs must be added to `allowed_target_cidrs` one by one. | LAN and loopback only; no public targets. |
| Q13 | Is this GitHub repo public or private? Only affects whether CI minutes are metered at all. | Assume private, keep CI to ~1 min per run. |

## 18. Deferred (do not build in v1)

- Two-way capability query protocol.
- Tailscale-identity authentication for `/register` (`tailscale whois`) as an
  alternative to the shared token.
- ntfy (or any) alerting.
- Config editing UI; HTTP delete; per-service auth; TLS (`tailscale serve` can
  front it later); IPv6 bind; uptime percentages; reading other services' logs.
