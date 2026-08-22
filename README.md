# VIX75 Platform

Algorithmic trading system for the **Deriv Volatility 75 Index (VIX75)**, engineered as a set of
small, independently deployable microservices. Market data flows through a vectorized feature
pipeline into a **3-state Gaussian HMM regime model** and a **LightGBM meta-label classifier**
(triple-barrier labeled); a multi-timeframe confluence engine generates signals that are gated by a
strict risk layer before reaching an idempotent MT5 execution path. Everything ships behind a
JWT-authenticated API gateway with Prometheus/Grafana observability, structured redacted logging,
and a **dry-run mode that simulates fills end-to-end so the system can soak for weeks before a
single real lot is traded**.

---

## Architecture Overview

```
                        ┌───────────────┐
   MT5 Terminal ───────▶│ data-service  │──▶ TimescaleDB (ohlcv hypertable)
   (Windows bridge)     └──────┬────────┘
                                │ Redis Stream: ohlcv.update
                        ┌───────▼────────┐
                        │feature-service │──▶ features hypertable
                        └───────┬────────┘        │
              Redis Streams:    │                 ▼
        ┌───────────────────────┼──── feature.computed
        │                       │
 ┌──────▼──────┐         ┌──────▼──────┐
 │ ml-service  │         │signal-service│──▶ signals table
 │ HMM + LGBM  │         └──────┬──────┘
 └──────┬──────┘                │ Redis Stream: signal.generated
        │ regime:current        ▼
        │ meta_label:current ┌─────────────┐
        └───────────────────▶│risk-service │──▶ order.request | signal.rejected
                             └──────┬──────┘
                                    ▼
                             ┌──────────────┐    order.filled / rejected / closed
                             │execution-svc │────────────────────────────┐
                             └──────┬───────┘                            │
                                    ▼                                    ▼
                              MT5 order_send                      ┌───────────────┐
                              (dry-run aware)                     │notify-service │──▶ Telegram
                                                                  │               │──▶ audit_log
      api-gateway ◀── Caddy (TLS) ◀── Internet                    └───────────────┘
```

### Services

| Service | Role |
|---|---|
| **data-service** | Polls MT5 for closed bars (M1/M5/M15/H1), bulk-upserts into TimescaleDB, publishes bar events; chunked historical backfills |
| **feature-service** | Vectorized indicator computation (ATR, RSI, EMA50/200, Bollinger, Stochastic, realized vol), swing pivots, supply/demand zone state machine |
| **ml-service** | HMM regime classification (S0 range / S1 up / S2 down) + LightGBM meta-label P(win); artifacts verified by mandatory SHA256 sidecars |
| **signal-service** | Multi-timeframe confluence scoring with hard gates (zone touch, HTF EMA alignment, HMM regime, meta-label ≥ 0.55) |
| **risk-service** | Position sizing (clamp-DOWN semantics, never exceeds configured risk), stops-level validation, margin headroom (≤ 50% free margin), exposure caps |
| **execution-service** | Idempotent MT5 order placement with full retcode handling and bounded retries; broker/local reconciliation sweep every 30 s; dry-run simulation |
| **notify-service** | Signal-lifecycle tracking (audit log + structlog) and formatted Telegram alerts (🟡 generated · 🟢 filled · 🔴 rejected · 🔵 closed) |
| **api-gateway** | Single public edge: JWT auth (`POST /token`), per-subject sliding-window rate limiting, async reverse proxy with correlation-id propagation |

### Infrastructure

- **Redis 7** — event bus (consumer-group streams: `ohlcv.update → feature.computed → signal.generated → order.request → order.filled/rejected/closed`) plus hot caches (`regime:current`, `meta_label:current`, broker snapshots).
- **TimescaleDB** — time-series store: `ohlcv` and `features` hypertables, plus `signals`, `trades` (idempotency-keyed) and an append-only `audit_log`.
- Every message and HTTP request carries an **X-Correlation-Id**, propagated across streams, downstream calls, database rows and every log line.

---

## Prerequisites

| Requirement | Notes |
|---|---|
| Python **3.12+** | [uv](https://docs.astral.sh/uv/) recommended for workspace management |
| Docker & Docker Compose v2 | Local stack + production deployment |
| MetaTrader 5 Terminal | Runs natively on Windows; acts as the local data/order bridge |
| Oracle Cloud account | For the Always-Free ARM production deployment (Ubuntu 22.04 A1.Flex) |

---

## Initial Setup

All work happens inside this repository:

```bash
cd trading_bot/vix75-platform/
```

Create your local environment file from the template and fill in real values:

```bash
cp .env.example .env
```

Configure at minimum:

```ini
MT5_LOGIN=<your_mt5_login>
MT5_PASSWORD=<your_mt5_password>
MT5_SERVER=DerivSVG-Server-03
TELEGRAM_TOKEN=<bot_token>
TELEGRAM_CHAT_ID=<chat_id>
JWT_SECRET=<32+ byte random secret>
GATEWAY_USERNAME=vix-admin
GATEWAY_PASSWORD=<operator password>
DRY_RUN_MODE=true          # safety default - see "Shadow Trading" below
```

> ⚠️ **`.env` must NEVER be committed.** It is git-ignored, scanned-for by gitleaks in CI, and
> excluded from Docker image layers. In production these values live at `/opt/vix75/.env`
> (chmod 600) or in a secret manager. If any credential was ever exposed elsewhere, rotate it
> before connecting to a live account.

Install dependencies (creates `.venv` from the uv workspace):

```bash
uv sync --all-packages --dev
```

---

## Running the System (Local Dev)

The master CLI orchestrates bootstrap, training and lifecycle:

```bash
# 1. Start infrastructure + all services, waiting until DB/Redis/gateway are healthy
python scripts/start_system.py up

# 2. Backfill ~5 years of history (run on the WINDOWS BRIDGE host - needs MT5)
python scripts/start_system.py backfill --symbol "Volatility 75 Index" --days 1825

# 3. Train the regime model and the meta-label model
python scripts/start_system.py train --model hmm
python scripts/start_system.py train --model meta_label
```

Useful variants:

```bash
python scripts/start_system.py backfill --days 365 --timeframes M15 H1   # smaller bootstrap
python scripts/start_system.py backfill --via-gateway                    # queue remotely (no local MT5 needed)
python scripts/start_system.py down                                      # stop everything
python scripts/start_system.py status                                    # compose state
```

You can also bring the stack up directly:

```bash
docker compose up -d            # add --profile services if you want every skeleton running
docker compose logs -f data-service
```

Service endpoints are reachable through the gateway (`http://localhost:8000`) after obtaining a
token:

```bash
curl -s -X POST http://localhost:8000/token \
     -H "Content-Type: application/json" \
     -d '{"username":"vix-admin","password":"<GATEWAY_PASSWORD>"}'

TOKEN=... # from response.access_token
curl -s http://localhost:8000/signals/history?limit=20 -H "Authorization: Bearer $TOKEN"
```

---

## Shadow Trading (Dry-Run Mode)

**`DRY_RUN_MODE=true` is the shipped default** — the system cannot place a live order out of the box.

In this mode the **execution-service intercepts every `order.request`** and, instead of calling
`mt5.order_send()`, prices the intent off the live tick feed and emits a fully-formed mock
`order.filled` (epoch-based ticket, `DONE (dry_run)` retcode). Downstream, *nothing* changes:
trades rows land in TimescaleDB as `filled`, reconciliation sweeps track them, metrics increment,
and the notify-service fires the same 🟢 Telegram alerts you would receive with real money at
stake. Run the full pipeline like this for at least two weeks and inspect:

- `/signals/history` quality vs. what you expect,
- rejection reasons on `signal.rejected`,
- reconciliation counters and PnL sanity,
- Grafana latency/error panels under realistic alert load.

### Going live

When validation is complete, make the deliberate flip in your environment
(`.env` locally, `/opt/vix75/.env` on the server):

```ini
DRY_RUN_MODE=false
```

then restart the execution-service (`docker compose restart execution-service`). The current mode
is always visible via `GET /health` on the execution service and in its startup logs. There is no
other switch: sizing still clamps lots **down** (rejecting below broker minimum rather than
oversizing), stops-level and 50%-free-margin checks remain hard gates, and duplicate order intents
stay idempotent.

---

## Oracle Cloud Deployment (Production)

Targets an **Oracle Always Free ARM VM** (A1.Flex, 4 OCPU / 24 GB RAM, Ubuntu 22.04).

1. Clone the repository onto the VM at `/opt/vix75`:

   ```bash
   sudo mkdir -p /opt/vix75 && sudo chown "$USER" /opt/vix75
   git clone https://github.com/BhunganeCodes/vix_75_platform.git /opt/vix75
   cd /opt/vix75
   ```

2. Provision the host — installs Docker + Compose plugin and configures the firewall
   (**UFW deny-by-default; only SSH, 80/tcp and 443/tcp open** — Postgres, Redis and all internal
   services stay network-private):

   ```bash
   sudo ./deploy/oracle/setup.sh
   ```

3. Place real secrets at `/opt/vix75/.env` (the setup script creates a chmod-600 placeholder).
   Set `ACME_EMAIL`, `POSTGRES_PASSWORD`, `JWT_SECRET`, `GRAFANA_PASSWORD` in addition to the
   broker/Telegram values.

4. Point DNS: create an `A` record for `api.yourdomain.com` → VM public IP, then edit
   `deploy/oracle/Caddyfile` (copied to `/opt/vix75/Caddyfile`) replacing `api.yourdomain.com`
   with your domain. Caddy obtains and renews certificates automatically on first start.

5. Launch the production stack:

   ```bash
   cd /opt/vix75
   docker compose -f docker-compose.prod.yml up -d
   docker compose -f docker-compose.prod.yml logs -f caddy   # watch certificate issuance
   curl https://api.yourdomain.com/health
   ```

Production hardening baked into `docker-compose.prod.yml`: **only Caddy publishes host ports**
(80/443); every service carries explicit `mem_limit`/`cpus` sized for the ARM tier;
`restart: unless-stopped` everywhere; secrets load via `env_file: /opt/vix75/.env` — never baked
into images.

---

## Observability

| Surface | Location |
|---|---|
| Grafana dashboards | `http://localhost:3000` (dev) — admin credentials in `.env` |
| Prometheus | `http://localhost:9090` (dev) |
| Dashboard model | `infra/grafana/dashboards/vix75_overview.json` (auto-provisioned) |

The dashboard tracks HTTP request rate by service/status code, p50/p99 latency histograms, signal
generation/rejection rates (`vix75_signals_generated_total`) and order execution rates
(`vix75_orders_filled_total` / `vix75_orders_rejected_total{reason}`). Every service exposes
`GET /metrics` on its internal port.

Logs are **structured JSON** emitted through `structlog` with a redaction filter that scrubs
account numbers, passwords, tokens and balances from every line — including third-party library
output. Each lifecycle event is also appended to the TimescaleDB `audit_log` table, and every hop
of every request shares an `X-Correlation-Id`.

---

## Testing & CI/CD

Run the test suite locally (134 tests: unit, integration against testcontainers Postgres/Redis,
lookahead-bias labeling guards, artifact-tamper checks, full-bus end-to-end):

```bash
uv sync --all-packages --dev
uv run pytest -v                                   # shared library + compose/config tests
cd services/<service> && uv run --no-sync python -m pytest tests -v   # per-service suites
```

Static analysis:

```bash
uvx ruff check . && uvx black --check .            # lint + format
uv run mypy packages/vix_core/vix_core packages/vix_core/tests scripts tests
for svc in services/*/; do (cd "$svc" && uv run --no-sync python -m mypy --config-file ../../pyproject.toml app); done
```

GitHub Actions (`.github/workflows/ci.yml`) runs on every push/PR:

- **lint** — `ruff check .` + `black --check .`
- **typecheck** — strict mypy over the shared library, scripts and each service
- **test** — Redis + TimescaleDB provisioned as job **services**, schema auto-applied, full
  pytest run with coverage reporting (artifact upload)
- **security** — gitleaks secret scan over full history + `pip-audit --strict` dependency CVE scan

---

## Repository Layout

```
vix75-platform/
├── packages/vix_core/        # shared library: config, schemas, indicators, zones,
│                             # swings, scoring, risk, artifacts, correlation, logging
├── services/                 # the eight FastAPI microservices (see Architecture)
├── infra/
│   ├── timescale/schema.sql  # hypertables + tables (auto-applied on first boot)
│   ├── redis/redis.conf
│   ├── prometheus/prometheus.yml
│   └── grafana/              # datasource provisioning + vix75_overview.json
├── deploy/oracle/            # setup.sh (UFW/Docker provisioning) + Caddyfile
├── scripts/                  # fetch_history.py, start_system.py
├── docker-compose.yml        # local development stack
├── docker-compose.prod.yml   # hardened production overlay
└── .github/workflows/ci.yml  # lint / typecheck / test / security
```

---

## Risk Disclaimer

Trading synthetic indices carries substantial financial risk. This software is provided for
research and educational purposes; past performance of any configuration does not guarantee future
results. Always validate extensively in dry-run mode and seek licensed financial advice before
enabling live execution.
