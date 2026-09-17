# Service Status Aggregator

A small, read-only dashboard for a home network. Other services announce
themselves to it, it polls their `/health` endpoints, and one page shows what
is up, what is down, and what has gone quiet. It never edits another service's
configuration; it only links to each one's own settings page.

It is built to run on the same machine as an OpenClaw installation and to be
reached over Tailscale. It binds to the machine's Tailscale IP, never to
`0.0.0.0`.

**v1 scope:** registration endpoint, health polling, status page, readiness
endpoint, three deployment paths. Deliberately not yet: the two-way capability
query protocol and ntfy alerting.

## How it works

```
other service ──POST /register (every 5 min, bearer token)──▶ aggregator ──SQLite
                                                                  │
      ◀────────────── GET health_url (every 30 s) ────────────────┘
                                                                  │
you (browser over Tailscale) ──────────── GET / ──────────────────┘
```

- **Registration** is an upsert keyed on `name` + `host`. A service that stops
  re-registering is shown as **stale** after a configurable window; it is never
  deleted, so a late restart shows up as a gap rather than erased history.
- **Polling** treats only HTTP 200 as `up`. Any other status, a timeout, or a
  connection error is `down`, with the specific reason recorded and logged.
- **The page** is laid out like a public status page: a banner at the top
  (green "All systems operational", yellow for degraded, red when anything is
  down), then one card per service with its current state and a strip of
  nodes, one per day, for the last 90 days. A day is green with no incidents,
  yellow with downtime under a threshold, red at or above it, and grey where
  there is no data. Hovering a node shows the date, total downtime, incident
  count and the failure reasons seen. The card also shows response time,
  last-checked time, registration freshness, a deep link to the service's
  config page, and its log path (shown as text; the aggregator never reads
  other logs).
- **Live states:** Operational (green), Degraded (yellow: a 200 that took
  longer than `degraded_response_ms`, or a registration that has gone stale),
  Down (red), Unchecked (grey, no poll yet). Hover the state for the reason.
- **Honest gaps:** if the aggregator itself was not running, that period is
  recorded as unknown and drawn grey rather than stretching the last known
  colour across time nobody was watching.

## Install

On the machine that will run it (Ubuntu with Tailscale, the OpenClaw host):

```sh
git clone https://github.com/clams2121/service_status_aggregator.git
cd service_status_aggregator
sudo ./install.sh
```

That installs it as a hardened systemd service, creates the `svcstatus` user,
generates the registration token and prints it **once** (save it in your
password manager), starts the service, and waits until `/health` answers.
When it finishes it prints the dashboard address, normally
`http://<tailscale-ip>:8720/`.

If `uv` is not installed the script offers to install it and asks first.

Other ways to run it:

| Command | What you get |
|---|---|
| `./install.sh --venv` | No root. Runs from this checkout; start it with `deploy/venv/run.sh`. |
| `sudo ./install.sh --docker` | A read-only container with host networking. Prompts for a storage directory. |

Re-running the same command after `git pull` upgrades in place and keeps your
config, database and token. Then edit
`/etc/service_status_aggregator/config.toml` if you want to change the check
interval, timezone or thresholds, and `sudo systemctl restart
service-status-aggregator`.

### Defaults and configuration

Port `8720`, check every `60 s` (allowed 10–600 s; 1–5 minutes is the
intended range), stale after `900 s`, 90 days of history, day boundaries in
`UTC` (set `[display].timezone` to your zone, e.g. `America/New_York`). All of
it lives in one TOML file; see `config.example.toml` for every option with
comments. `service-status-aggregator check-config --config <file>` reports
every problem at once, and the service refuses to start on any of them.

## Registering a service

`POST /register` with `Content-Type: application/json` and
`Authorization: Bearer <token>`. All six fields are required; unknown fields
are rejected.

| Field | Type | Rules |
|---|---|---|
| `name` | string | 1–64 chars, lowercase letters, digits, `-`, `_`; starts with a letter or digit. Uppercase is lowercased. |
| `host` | string | This service's Tailscale hostname or IP. Must equal the host in `health_url`. |
| `port` | integer | 1–65535 |
| `health_url` | string | `http://` or `https://` URL of the service's own `/health`. No credentials in the URL. |
| `log_path` | string | Absolute path to its log file, or `""`. Display only. |
| `config_page_url` | string | `http(s)://` deep link to its settings page, or `""`. |

Responses: `201` on first registration, `200` on re-registration (body is the
stored record), `401` bad or missing token, `415` wrong content type, `413`
body over 16 KiB, `400` malformed JSON, `422` validation failure with a
field-level `detail` list.

```sh
curl -sS -X POST http://100.100.100.100:8720/register \
  -H "Authorization: Bearer $SSA_REGISTRATION_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "media",
    "host": "100.100.100.101",
    "port": 8080,
    "health_url": "http://100.100.100.101:8080/health",
    "log_path": "/var/log/media/app.log",
    "config_page_url": "http://100.100.100.101:8080/settings"
  }'
```

**Python services:** copy `examples/register_client.py` (standard library
only) and call `start_registration(...)` at startup. It registers immediately,
then every 5 minutes in a daemon thread, and only ever logs a warning when the
aggregator is unreachable.

**Where can a registrant point the aggregator?** Only at hosts inside
`[registration].allowed_target_cidrs`. The default is Tailscale's ranges plus
loopback. The aggregator resolves hostnames at registration time and again
before every poll; anything outside the allowlist is rejected with
`target_rejected`. This is what stops a bad registration from turning the
aggregator into a proxy for probing arbitrary addresses.

### Paragraph for other projects' build prompts

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

## Secrets

The only secret is the registration token (≥ 16 characters; installers
generate 64 hex characters). It is **never** read from the config file, never
logged, and shown as `***` on the config page. This repository is public: no
token, `.env`, database or log is ever committed.

**Keep a plaintext copy of the token in your password manager.** The
installers print it exactly once. Every registering service needs it.

Where it lives, and how to rotate it:

| Path | Location | Rotate |
|---|---|---|
| systemd | Encrypted with `systemd-creds` at `/etc/credstore.encrypted/ssa-registration_token`, decrypted into the unit's `$CREDENTIALS_DIRECTORY` at start. | `sudo deploy/systemd/rotate-token.sh [new-token]` (generates one if omitted, restarts the unit). |
| venv | `.env` in the checkout, mode 600, as `SSA_REGISTRATION_TOKEN`. | Edit `.env`, restart `run.sh`. |
| Docker | `<storage>/secrets/registration_token`, mode 600, mounted read-only at `/run/credentials`. Not in the container environment. | Overwrite the file, `docker compose restart`. |

After rotating, update the token in every service that registers. Old
registrations stay visible and will go stale if a service is not updated,
which is the signal you want.

To run without a token, set `[registration].require_token = false`. The
service will start, log a warning, and show a banner on every page. Only do
this on a network where every peer is trusted.

## Network and bind behaviour

`[server].bind` accepts:

- `"auto"` (default): the machine's Tailscale IPv4, found via `tailscale ip -4`,
  the `tailscale0` interface, or `ip -j`. The lookup retries for up to 30 s
  because tailscaled may still be starting at boot. If nothing is found it
  binds `127.0.0.1` and logs a warning with the tunnel command every start.
- `"tailscale"`: same lookup, but exit with status 3 instead of falling back.
- an explicit IP that is assigned to a local interface.

`0.0.0.0` and `::` are rejected at config load. If you end up on loopback,
reach the page with `ssh -L 8720:127.0.0.1:8720 <host>` and browse to
`http://127.0.0.1:8720/`.

Traffic is plain HTTP. On a tailnet that is already encrypted end to end by
WireGuard; if you want TLS at the browser, `tailscale serve` can front it.

## Endpoints

| Path | What |
|---|---|
| `GET /` | The status page. Auto-refreshes at the check interval. |
| `GET /api/services` | The same data as JSON: live state, detail, uptime percentage and the per-day history for each service. |
| `GET /config` | Effective configuration, read-only, token redacted. Editing is by file + restart in v1. |
| `GET /health` | Readiness: `200` only when the database is reachable and writable **and** the poller completed a cycle within 2× the interval. Otherwise `503` naming the failing check. |
| `POST /register` | Described above. The only write endpoint. |

`/health` is what systemd's watchdog, Docker's `HEALTHCHECK`, and the
installers use. The aggregator also registers itself (`register_self = true`)
so it appears as a row on its own dashboard.

## Logging

Plain text, UTC timestamps, size-rotated (`5 MiB × 5` by default) at
`[logging].path`. Also on stderr when run in a terminal or with
`SSA_LOG_STDERR=1` (the Docker image sets this so `docker logs` works).

- State changes: `INFO` for `→ up`, `WARNING` for `→ down`.
- Every failed poll: `WARNING` with the reason (`http_503`,
  `timeout_after_5s`, `connection_error: …`, `target_rejected: …`). A service
  that stays down produces about two lines a minute; rotation bounds it.
- Rejected registrations: `WARNING` with the client address and the reason.
- Startup: version, config path, resolved bind, whether the token is enabled,
  and the allowlist.

## Command line

```
service-status-aggregator run          --config PATH   # the service
service-status-aggregator check-config --config PATH   # validate, exit 2 on problems
service-status-aggregator healthcheck  --config PATH   # GET /health on the resolved bind; exit 0 if ok
service-status-aggregator remove NAME HOST --config PATH   # delete a decommissioned entry
service-status-aggregator version
```

Config path precedence: `--config`, then `$SSA_CONFIG`, then
`/etc/service_status_aggregator/config.toml`.

Exit codes: `2` no usable configuration, `3` could not bind (address not
local, port in use, or `bind = "tailscale"` with no Tailscale IP), `0` on a
clean stop via SIGTERM. systemd's `Restart=on-failure` therefore restarts
crashes but not deliberate stops.

## Operating notes

- **Removing a service:** there is no delete endpoint by design. Use
  `service-status-aggregator remove <name> <host>`.
- **History:** the `services` row holds the latest result; `status_events`
  keeps transitions only (up, down, unknown), pruned after
  `[history].retention_days`. The day strip is reconstructed from those
  transitions, so it costs no extra storage per check.
  `[display].history_days` cannot exceed the retention.
- **Thresholds:** `[display].major_outage_minutes` decides yellow versus red
  for a day; `[display].degraded_response_ms` decides when a healthy but slow
  response shows as Degraded.
- **Upgrading:** pull, then re-run the same installer. Config, database and
  token are kept.
- **Database:** SQLite in WAL mode at `[storage].db_path`. Back it up by
  copying the file while the service is stopped, or with `sqlite3 .backup`.

## Security notes

- Registration requires a bearer token compared in constant time.
- Poll targets are restricted to an allowlist of networks (SSRF guard);
  redirects are never followed; response bodies are capped at 64 KiB.
- The page is self-contained: no CDN scripts or fonts, a strict
  `Content-Security-Policy`, autoescaped templates, `rel="noopener"` links.
- The service runs as a dedicated user. The systemd unit uses `ProtectSystem=
  strict`, no capabilities, a system-call filter and a private `/tmp`; the
  Docker container is read-only with all capabilities dropped.
- Anyone on the tailnet can read the page. It shows hostnames, ports and log
  paths, which is the point; keep the tailnet private.
- Residual: a hostname could resolve differently between the registration
  check and the poll (DNS rebinding). Low value on a tailnet, so accepted.

## Development

```sh
uv sync
uv run ruff check . && uv run ruff format --check .
uv run pytest -q
```

Tests run without network access and include an end-to-end run of the real
process on a loopback port. CI runs the same checks on every push.

`docs/PLAN.md` is the design document this implementation follows.

## Roadmap

- Capability query protocol (ask a service what data it offers, request fields).
- ntfy alerting on transitions.
- Tailscale-identity authentication for `/register` as an alternative to the token.

## License

AGPL-3.0-or-later. See `LICENSE`.
