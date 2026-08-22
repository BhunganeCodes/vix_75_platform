# System Audit Report

**Project:** volatility_analysis — Volatility 75 Index (VIX75) Trading Bot
**Date:** August 22, 2026
**Auditor:** OpenCode Automated Audit

---

## Executive Summary

This repository is an algorithmic trading system for the Deriv Volatility 75 Index, containing four parallel generations of the same strategy (v1 phase-files, v2 modular rewrite, a misnamed "cpp" ML pipeline, and MQL5 MetaTrader implementations) plus an Oracle Cloud deployment of a live signal monitor. The codebase is in **critical condition from a security standpoint**: live MT5 account credentials (login + plaintext password), a Telegram bot token, and a Deriv trading API token are committed and pushed to a public GitHub remote, and an RSA private SSH key sits in the working tree. Engineering hygiene is poor: **zero automated tests across ~31,500 lines of code**, pervasive copy-paste duplication (~60% of v1 is duplicated logic with realized config drift between copies), no CI/CD, no lint/format configuration, and committed logs/binaries. There are genuine bright spots — the v2 package shows a clean modular architecture, the cloud monitor has proper rotating logging, and trading risk parameters exist throughout — but the project must be treated as compromised until all rotated secrets are scrubbed from git history.

---

## Critical Findings 🔴

1. **Live broker account credentials committed to a public GitHub repo.** `vix75_v1/.env:1-3` contains `MT5_LOGIN=140658884`, `MT5_PASSWORD='@Kingkefas7'`, `MT5_SERVER='DerivSVG-Server-03'` in plaintext. The file **is tracked by git** (`git ls-files` confirms) and the remote is `https://github.com/BhunganeCodes/volatility_analysis.git`. Anyone can log into this trading account. The `.gitignore` for that directory only excludes `vix75_cloud/`, not `.env`.

2. **Telegram bot token and chat ID committed to git.** `vix75_v1/vix75_cloud/.env:8-9` contains what appears to be a real token (`TELEGRAM_TOKEN=7863491044:AAG4...`) — ironically under a header comment reading "NEVER share or commit the .env file". This token also gets baked into Docker image layers via `COPY .env .` (`vix75_v1/vix75_cloud/Dockerfile:22`).

3. **Deriv trading API token committed to git.** `vix75_v1/vix75_cloud/.env:12` (`DERIV_API_TOKEN=IOUdcXzi4lsl0ih`). Combined with finding 1, an attacker has both broker-terminal access and API access.

4. **RSA private SSH key stored inside the project.** `vix75_v1/vix75_cloud/ssh-key-2026-02-21.key` (PEM RSA private key, verified). It is currently *untracked* only because it lives inside the gitignored `vix75_cloud/` directory — one accidental `git add -f`, a moved file, or a changed `.gitignore` exposes server access. Treat as exposed; rotate.

5. **Zero test coverage on the entire codebase.** No `test_*.py`, no `*_test.py`, no `conftest.py` anywhere (verified by filesystem search); the root `.pytest_cache/` is stale residue. ~31,500 lines of Python/MQL5 including order-execution, lot-sizing, and backtesting math have no regression protection whatsoever. A stale pytest cache implies tests once existed and were deleted.

6. **Unauthenticated, CORS-open control plane bound to all interfaces.** The ML engine Flask app uses wide-open `CORS(app)` (`vix75_v1/ml_engine/vix75_ml_engine.py:1800`) with no auth on any route (`:1799-1865`) and binds `host="0.0.0.0"` (`:1927`). `/api/train` and `/api/optimize` let any LAN peer spawn CPU-heavy retraining threads remotely (denial-of-wallet on a 1GB VPS), and `/api/status`, `/api/history` leak trade levels and strategy parameters.

7. **Runtime auto-installation of unpinned packages at import time.** `vix75_ml_engine.py:67-80` runs `pip install torch torchvision xgboost ... --quiet` unconditionally at module import with no version pins and no user confirmation — a supply-chain hazard on every cold start of a live-trading host.

## High Priority Findings 🟠

1. **Four generations of the same strategy maintained as copy-paste forks, with realized config drift.** Indicator enrichment is re-implemented ~7×, swing detection 6×, zone engines 8×. Drift already bit: `MAX_RR = 5.0` (`vix75_v1/vix75_diagnostic.py:78`) vs `15.0` everywhere else; `GRADE_B` 5.5 (`vix75_phase4_signal_engine.py:76`) vs 5.0 (`vix75_phase4_revised.py:77`); RSI_OS 30 vs 25 between the same two files. `local` vs `strict_sell` are 92.6% line-similar; `phase7` vs `phase8` 64.7%.

2. **EA order failures are completely silent.** In `vix75ea.mq5`, return values of `g_trade.Buy/Sell` are checked as booleans (`:244,259`) but retcodes are never inspected or logged (`ResultRetcode()` absent from all three `.mq5` files). Rejected orders produce no log, no retry, no alert — unacceptable for live money movement.

3. **Lot sizing can exceed configured risk and ignores margin/stops constraints.** `CalcLots()` clamps *up* to `lotMin` (`vix75ea.mq5:351`), so small accounts silently exceed the 1% risk cap; there is no `OrderCalcMargin` check, no `SYMBOL_TRADE_STOPS_LEVEL`/freeze-level validation, and filling mode is hardcoded `ORDER_FILLING_IOC` (`:102`) with no FOK/RETURN fallback.

4. **Cloud setup script disables the host firewall.** `oracle_setup.sh:45-50` inserts blanket `iptables -I INPUT -j ACCEPT` + OUTPUT ACCEPT rules and persists them via `netfilter-persistent` — combined with finding 🔴6, this exposes the Flask control plane to the internet on a publicly reachable VM.

5. **Secrets baked into Docker layers.** `Dockerfile:22` `COPY .env .` puts Telegram/Deriv tokens into image layers even though compose already passes `env_file`; any registry push of the image leaks them.

6. **Sensitive financial data printed to stdout/logs.** Account number, server, and **account balance** are printed in at least six places: `vix75_phase1_data_pipeline.py:61-63`, `vix75_phase6_live_monitor.py:1436`, `vix75_phase7_live_monitor.py:1614`, `vix75_phase8_hmm.py:1951`, `vix75_local.py:268`, `vix75_backtester.py:881`. Logs are committed to git (`vix75_v1/logs/*.log`, `vix75_debug.log`, `vix75_local.log`).

7. **Global warning suppression everywhere.** `warnings.filterwarnings("ignore")` appears in ~13+ files (`monitor.py:34`, `signal_engine.py:47`, `trading_bot.py:80`, `refined_signal_engine.py:64`, `simulator.py:36`, `EA_testing/vix75_backtester.py:43`, etc.), hiding pandas/numpy deprecations and numeric instability warnings in a system that computes money-at-risk.

8. **Unsafe deserialization surface.** `joblib.load` of `vix75_model.joblib` / `vix75_meta_model.joblib` (`vix75_cpp/signal_engine.py:106-108`, `trading_bot.py:177-182`, `refined_signal_engine.py:137-142`), plus `pickle.load` and `torch.load` in `vix75_ml_engine.py:1080-1091,1231-1243,809-815` — all from writable relative directories. Any write access to those paths yields arbitrary code execution on the trading host.

9. **No CI/CD whatsoever.** No `.github/`, no pipelines, no pre-commit hooks. Nothing prevents another credential commit (which is how findings 🔴1-3 happened), nothing runs linters or tests (there are none).

10. **No packaging/project metadata.** No `pyproject.toml`, `setup.py`, or lockfiles anywhere. Five divergent `requirements.txt` files with `>=` floors and zero upper bounds make builds irreproducible; the cloud requirements (`vix75_v1/vix75_cloud/requirements.txt`) have **no version constraints at all**.

## Medium Priority Findings 🟡

1. **God files and monster functions.** Largest files: `vix75_phase8_hmm.py` (1,987 lines), `vix75_ml_engine.py` (1,930), `vix75_phase7_ml.py` (1,678), `vix75_phase7_live_monitor.py` (1,650), `vix75_strict_sell.py` (1,632), `vix75_cpp/trading_bot.py` (1,596), `vix75_cloud/monitor.py` (1,529). Worst functions: `render_chart` at 356 lines with nesting depth 7 (`vix75_phase7_live_monitor.py:1141`), 331 lines (`vix75_phase8_hmm.py:1491`), 297 lines (`vix75_phase6_live_monitor.py:1032`).

2. **Hot-path pandas anti-patterns.** Per-zone `iterrows()` scans over up-to-5,000-bar frames → O(zones×bars) (`vix75_phase3_zone_engine.py:323`, repeated in phase6:626, phase7:721, phase8:1046, strict_sell, local); O(n²) swing rescan recomputing `df["swing_high"].iloc[:i].dropna()` per bar (`vix75_phase2_structure_engine.py:174-175`).

3. **Inconsistent logging discipline.** Proper `logging` exists only in the cloud monitor (`monitor.py:123-150`, 100MB rotating handlers — good), `vix75_local.py:143-152`, and `strict_sell.py:154-179`. The other 12+ v1 modules use bare `print()` (up to 82 calls/file) with no levels, timestamps, or file output — requests cannot be traced through the system.

4. **Bare/silent exception swallowing.** Bare `except:` at `vix75_phase7_live_monitor.py:1069`, `vix75_phase8_hmm.py:1424`, `vix75_phase7_ml.py:1095`, `vix75_strict_sell.py:1629-1630`; broad swallows at `ml_bridge.py:51,99,117`, `ml_engine.py:963-965,1413-1414`; lot-size exceptions silently skip trades (`trading_bot.py:404-406`). Unguarded CSV appends inside tick loops (`phase7:1053-1056`, `phase8:1408-1411`).

5. **Repository hygiene: build outputs and binaries tracked in git.** 115 tracked files include logs, backtest CSVs, chart PNGs, and compiled `.joblib` models (`vix75_cpp/VIX75_Analysis/*.joblib`, `vix75_v1/logs/*.log`, 10 PNGs). There is **no root `.gitignore`**, so each sub-project invented its own partial one.

6. **README is materially inaccurate.** Claims TA-Lib, Plotly, Backtrader, "Deriv API integration" and a MIT LICENSE — none present (grep confirms zero usage of talib/plotly/backtrader; no LICENSE file exists despite `README.md:103` linking one). Documents a "Market Intent Indicator" confluence-score design that doesn't match any shipped module.

7. **Misleading file/module names.** `indicators/supply_and_demand.mq5` contains no supply/demand logic (it's the rule-engine indicator); `vix75_cpp/` contains zero C++; `EA_testing/quality_pro.mq5` is a chart indicator, not an EA; internal headers (`VIX75_RuleEngine_EA/IND.mq5`) don't match filenames.

8. **Dead/duplicate sibling files.** `phase4_revised.py` vs `phase4_signal_engine.py` (~45% similar, `to_dict()` 91% identical), `phase5_revised.py` vs `phase5_backtester.py`; `fix_and_rerun.py` is a one-off patch that forks the Phase-3 zone engine inline instead of parameterizing it.

9. **Hardcoded operational values.** ML bridge URL `http://localhost:5678` in four places (`phase7:71,74,1561,1607`); symbol `"Volatility 75 Index"` duplicated in 7 files; HMM priors/transition matrix inline (`vix75_phase8_hmm.py:242-247`); `DERIV_APP_ID=1089` hardcoded in `oracle_setup.sh:76`; output paths duplicated per-file rather than shared config.

10. **Deployment doc references a nonexistent template.** `DEPLOYMENT.md:14` says `cp .env.example .env` but no `.env.example` exists anywhere in the repo (verified) — new deployments start by copying... nothing.

11. **Stale/misleading indicator inputs.** `quality_pro.mq5:87-88,94-95,104` still exposes OB/OS level inputs documented as dashboard-only, inviting operators to tune parameters that don't affect entries.

## Low Priority Findings 🟢

1. **`#property strict` leftover** in all three MQL5 files (`vix75ea.mq5:13`, `supply_and_demand.mq5:20`, `quality_pro.mq5:35`) — an inert MQL4 directive.

2. **Unused function parameter**: `color col` accepted but never used by `ScoreBar()` in all three MQL5 copies (`vix75ea.mq5:578`, `supply_and_demand.mq5:678`, `quality_pro.mq5:771`).

3. **Type annotation bug**: `run_engine()` declares `Optional[Signal]` but returns a 4-tuple `(structure, zones, quant, signal)` (`vix75_v2/main.py:37-55`) — misleading signature.

4. **No formatter/linter config** (black/ruff/flake8/editorconfig absent); style is nonetheless fairly consistent within each generation.

5. **CSV-as-database persistence** with no schema versioning or locking; append-only signal CSVs will corrupt under concurrent writers (relevant since multiple monitors target overlapping filenames like `vix75_signals.csv`).

6. **Chart redraw cadence** redraws full matplotlib figures every N candles during backtests (`vix75_v2/main.py:150-153`) — fine interactively, wasteful headless.

7. **Magic numbers in EA scoring** (RSI midlines `45/50/55` at `vix75ea.mq5:199,201`; confidence divisor `/5.0` at `:230`) not exposed as inputs unlike everything else in the same file.

---

## Detailed Analysis

### Project Overview

An algorithmic trading research system targeting **Volatility 75 Index (VIX75)**, a synthetic index on Deriv brokers. The repo is a monorepo of four independent strategy generations plus native-platform implementations:

| Component | What it is | Status |
|---|---|---|
| `vix75_v1/` | 10-phase pipeline: data → structure → zones → signals → backtest → live monitors → HMM regime model; plus `ml_engine/` Flask ML service and `vix75_cloud/` Dockerized Oracle Cloud signal monitor | Primary historical codebase; live deployment artifact |
| `vix75_v2/` | Clean modular rewrite (`config` / `data` / `engine` / `signals` / `chart`) with dataclasses | Best-structured code; signal-only (no execution) |
| `vix75_cpp/` | **No C++** — ML meta-labeling pipeline (RF+HGB ensemble, triple-barrier labels), simulator, and an ML-driven trading bot consuming `.joblib` models | Research |
| `EA_testing/` | Python backtester replicating the MQL5 rule-engine EA + multi-source data pipeline (MT5/CSV/yfinance) | Research |
| Root MQL5 (`vix75ea.mq5`) | Expert Advisor that actually places orders via CTrade (BB/RSI/Stoch/MACD/CCI scoring, ADX gate) | Live-capable |
| `indicators/supply_and_demand.mq5`, `EA_testing/quality_pro.mq5` | Chart indicators (misleadingly named) | Visual/diagnostic |

**Tech stack:** Python 3.9–3.13 (venvs observed: 3.13.12 in `EA_testing/venv313`), pandas/numpy/scikit-learn/XGBoost/LightGBM/Optuna/PyTorch, matplotlib, Flask (+flask-cors), MetaTrader5 package (Windows-only), Docker/python:3.11-slim for cloud, MQL5 for MetaTrader. **Entry points:** `vix75_v2/main.py` (live/backtest selector), `vix75_v1/vix75_cloud/monitor.py` (Docker CMD), `vix75ea.mq5` (OnTick), `vix75_cpp/main.py` (ML training), `EA_testing/vix75_backtester.py` (CLI).

### Dependency Health

All dependencies come from five `requirements.txt` files. **No lockfiles exist**; every constraint is a `>=` floor (or entirely unconstrained), so builds are irreproducible.

| Name | Constraint(s) found | Status | Notes |
|---|---|---|---|
| MetaTrader5 | `>=5.0.45` (3 files) | 🟡 | Windows-only; floor from 2022, effectively unpinned |
| pandas | `>=1.5.0` / `>=2.0` | 🟡 | Floor drift between files; `>=1.5` allows EOL versions |
| numpy | `>=1.23.0` / `>=1.24` | 🟡 | Unbounded; numpy≥2.0 will break `pandas_ta` (below) |
| matplotlib | `>=3.6.0` / `>=3.7` | 🟢 | Fine |
| python-dotenv | `>=1.0.0` | 🟢 | Fine |
| psutil | `>=5.9.0` | 🟢 | Used only by cloud monitor/plotter |
| pandas_ta | `>=0.3.14b` | 🔴 dep-risk | Abandoned upstream; known breakage importing `numpy.NaN` on numpy≥2.0 — a fresh install today fails |
| seaborn | `>=0.12` | 🟢 | Used in EA_testing only |
| yfinance | `>=0.2` | 🟡 | Optional data source; frequent breaking changes, needs pinning |
| pyarrow | `>=12.0` | 🟢 | Parquet support |
| scikit-learn | `>=1.3.0` | 🟢 | — |
| xgboost | `>=2.0.0` | 🟢 | — |
| lightgbm | `>=4.0.0` | 🟢 | — |
| optuna | `>=3.4.0` | 🟢 | — |
| torch/torchvision | `>=2.1.0` / `>=0.16.0` | 🟡 | Heavyweight; also auto-installed at runtime (finding 🔴7) |
| Pillow | `>=10.0.0` | 🟢 | — |
| Flask | `>=3.0.0` | 🟢 | Current major |
| flask-cors | `>=4.0.0` | 🟡 | Configured wide-open (finding 🔴6) |
| websockets | *(none)* | ⚪ unused | Listed in cloud requirements; never imported anywhere |
| requests | *(none)* | 🟡 | Used only by `ml_bridge.py`; unconstrained |

**Redundant/unused:** `websockets` (never imported). **Claimed-but-absent:** README's TA-Lib, Plotly, Backtrader, Backtrader-style engines appear nowhere in imports. **Pinned vs unpinned:** nothing is truly pinned; worst offender is `vix75_cloud/requirements.txt` with five bare names.

### Architecture Assessment

**Pattern:** polyrepo-in-a-monorepo. Four self-contained strategy generations with **zero shared code** — no common package for indicators, swings, zones, or config, despite these being reimplemented up to 8 times. Within `vix75_v2/` the layering is genuinely good: `config` ← `data feeds` → `engine (structure/zones/quant)` → `signals.generator` → `chart.plotter`, orchestrated by `main.py`, with dataclasses at boundaries (`QuantSnapshot`, `Signal`, `StructureState`). That pattern is the right target architecture; it just covers only one generation.

**Boundary leakage:** v1 phases violate everything v2 does right — each file owns its own config block (drifted), its own I/O paths, its own indicator math. `vix75_cpp/trading_bot.py`, `signal_engine.py`, `refined_signal_engine.py` all do `from main import ...` (sibling-module coupling rather than a package).

**Circular dependencies:** none detected (imports are acyclic within every generation; the cpp trio's shared dependence on `main.py` is a hub-and-spoke smell, not a cycle).

**God modules:** see Medium finding 1 — seven files exceed 1,500 lines.

**Data flow:** MT5 terminal (`mt5.copy_rates_from_pos`) or CSV/yfinance (research) → pandas DataFrame (UTC index, enriched with ATR/RSI) → structure/swing detection → S/D zones + state machine → BBMA/divergence/confluence scoring → signal dict/dataclass → (a) matplotlib charts, (b) CSV/JSON artifacts (`vix75_signals.json`, `vix75_zones.json`, append-only `*_live_signals.csv`, trade logs), (c) Telegram alerts from the cloud monitor, (d) optionally the local Flask ML service over HTTP (`/api/assess`). Execution happens either in MQL5 (`CTrade.Buy/Sell`) or nowhere — notably, the Python cloud monitor generates *signals only*; it does not place orders.

### Code Quality Assessment

- **Duplication is the defining defect.** Verbatim-class clones (`vix75_local.py:313-335` ≡ `vix75_strict_sell.py:345-363`), identical functions across phase files (`detect_swings` ×6; `ts_to_bar_index` ×3; enrich ×7). Estimated 50–60% of v1 could be deleted by extracting a shared library.
- **Long/deep code:** `render_chart` 356 lines / depth 7 (`vix75_phase7_live_monitor.py:1141`); `classify_structure` depth 7 in four copies; `detect_bbma` ~205 lines ×3.
- **Error handling:** 4 bare `except:` clauses; systemic swallow-and-continue; blanket outer catches around whole refresh cycles hide failure modes (Medium findings 4). Positive counterexamples: `vix75_cpp/main.py:868-870` logs-and-reraises; `phase7_ml.py:1601-1604` stores traceback state.
- **Style consistency:** no linter/formatter configured, but naming is internally consistent (`Inp*` inputs and `g_*` globals in MQL5; snake_case elsewhere). Comment style (banner blocks) is consistent and explanatory; no large commented-out code blocks were found — the dead weight is entire redundant *files*, not comments.
- **Hardcoded values:** see Medium finding 9; the pattern is "config copied into every file," which is why thresholds drift apart.
- **v2 quality note:** `vix75_v2/engine/quant.py` is clean, vectorized, well-factored — evidence the author knows better; v1 predates it.

### Security Assessment

No SQL anywhere (flat-file persistence) → SQL injection: **N/A**. XSS: the Flask app serves a static dashboard and JSON APIs; base64 chart images returned by `/api/assess` are rendered client-side — low risk, but the endpoint itself is unauthenticated. Path traversal: no user-controlled paths reach `open()`/`send_from_directory` in reviewed routes. The dominant risks are credential exposure and network exposure:

| Risk | Location | Severity |
|---|---|---|
| Broker login+password in public repo | `vix75_v1/.env:1-3` | 🔴 Critical |
| Telegram token in public repo + image layers | `vix75_v1/vix75_cloud/.env:8`, `Dockerfile:22` | 🔴 Critical |
| Deriv API token in public repo | `vix75_v1/vix75_cloud/.env:12` | 🔴 Critical |
| RSA private key in working tree | `vix75_v1/vix75_cloud/ssh-key-2026-02-21.key` | 🔴 Critical (untracked, fragile) |
| Unauthenticated LAN/internet control plane | `vix75_ml_engine.py:1799-1927` | 🔴 High |
| Host firewall disabled + persisted | `oracle_setup.sh:45-50` | 🟠 High |
| Balance/login printed to stdout & committed logs | `phase1:61-63`, `phase6:1436`, `phase7:1614`, `phase8:1951`, `local:268`, `backtester:881` | 🟠 High |
| Pickle/joblib/torch deserialization from writable dirs | `signal_engine.py:106`, `ml_engine.py:1080,1231,809` | 🟠 High |
| Runtime unpinned pip installs | `ml_engine.py:67-80` | 🔴 High |

AuthN/AuthZ patterns: **there are none** in the Python services (the MT5 terminal session is the only authenticated boundary, via committed credentials). CORS: explicitly wide open (`CORS(app)`, `ml_engine.py:1800`). File permissions: no `chmod` hardening beyond `oracle_setup.sh`'s `chmod +x`.

### Test Coverage Assessment

**Framework:** none. **Test files:** zero (filesystem-verified; `.pytest_cache/` at root is orphaned). **Coverage map:** every module untested.

| Module | Tested? | Gap description |
|---|---|---|
| `vix75ea.mq5` order path (CalcLots, SL/TP) | ❌ | Money-math untested; the lotMin clamp-up bug (🟠3) would be caught by one unit test |
| `vix75_v1` phase engines 1–8 | ❌ | Signal-generation logic has no golden-file/regression tests; drift bugs prove need |
| `vix75_v1` backtesters (both) | ❌ | Win-rate/PF metrics unverifiable |
| `vix75_cloud/monitor.py` | ❌ | Live signal gating (confluence ≥ 4.5, trend scores) untested |
| `vix75_v2/*` | ❌ | Most testable code in repo (pure functions on DataFrames) — ideal first target |
| `vix75_cpp` ML pipeline | ❌ | Feature engineering/labeling correctness untested |
| `ml_engine` Flask routes | ❌ | No request/response contract tests |
| `EA_testing` pipeline/backtester | ❌ | Multi-source loaders (MT5/CSV/yf) untested |
| MQL5 indicators | ❌ | No strategy-tester record committed |

Flaky-pattern risk when tests are eventually added: current code leans on wall-clock (`time.sleep` retries, `datetime.fromtimestamp` without tz in `mt5_feed.get_tick():138`) and live MT5 connectivity — both must be injected/mocked.

### Data Layer Assessment

- **Database:** none. Persistence is flat files: OHLCV CSVs (`vix75_data/*.csv`), JSON state (`vix75_zones.json`, `vix75_signals.json`, metrics), append-only signal CSVs, parquet (EA_testing). MT5 terminal is the de-facto market DB.
- **Schema management:** none — column sets are implicit in writer code; a renamed column breaks readers silently.
- **N+1 equivalent:** per-zone row iteration over future bars (`phase3_zone_engine.py:323` and 5 siblings) is the loop-query analog of N+1; O(zones×bars).
- **Missing-index analog:** full-frame rescans per bar (`phase2:174-175`) instead of precomputed pivots.
- **Connection management:** MT5 init/shutdown handled correctly in most entry points (`phase1:332-334` good); `vix75_backtester.py:863-903` lacks try/finally around `mt5.shutdown()`. HTTP connections in `ml_bridge` use short timeouts (1.5–2.0s) — reasonable.
- **Boundary validation:** essentially absent — CSVs are trusted on read; signal dicts flow to writers unchecked; the Flask `/api/assess` POST body is consumed without schema validation.

### Performance Assessment

- **Sync I/O in loops:** MT5 polling every 3–30s per process is acceptable, but three successive monitors (phase6/7/8) each pull 300 bars × 3 timeframes per cycle with full recompute — heavy for a 1GB VPS (the cloud monitor was visibly patched down from 500 bars to cope, `monitor.py:43,111`).
- **Algorithmic hot spots:** per-zone `iterrows` scans (O(zones×bars)) and O(n²) swing scan (`phase2:174-175`) dominate backtests.
- **Caching:** none anywhere (indicator series recomputed per candle; models loaded per process start — acceptable; feature matrices recomputed per assess call — not).
- **Memory-leak patterns:** `signal_history` grows unboundedly in `vix75_v2/main.py:30` (display slices it, memory doesn't); matplotlib figure churn in long-running monitors is mitigated only by periodic full redraws.
- **Pagination:** N/A (no list endpoints); `/api/history` caps at last 50 — good instinct applied inconsistently.
- **Unbounded queries:** initial history pulls are bounded (300–500 bars); `copy_rates_from_pos(..., 5000)` in the phase-1 pipeline is a one-shot, fine.

### Infrastructure Assessment

- **Build/run:** no Makefile, no task runner, no pyproject. Run = "python <script>" per generation. Only the cloud monitor is containerized: `python:3.11-slim`, `CMD ["python","-u","monitor.py"]` (`Dockerfile:6,32`), compose with `restart: unless-stopped` and json log rotation (`docker-compose.yml:16,22-26`).
- **Env templates:** referenced by `DEPLOYMENT.md:14` but **do not exist**. Real `.env` files are what's committed instead — backwards.
- **CI/CD:** none. Nothing guards the repo against recurrence of the committed-secrets incident.
- **Environments:** dev (local Windows/MT5) and prod (Oracle/AWS-style VPS via two competing guides — `DEPLOYMENT.md` targets DigitalOcean, `ORACLE_DEPLOYMENT.md`/`oracle_setup.sh` target Oracle). No staging; config drift between guides.
- **Drift risks:** five requirements files; two deployment runbooks; per-file config blocks; `SYMBOL` differs between cloud env (`R_75`) and Python code (`"Volatility 75 Index"`) — works only because Deriv maps both, and is exactly the kind of latent mismatch that produces silent wrong-symbol trading.
- **Resource limits:** none in compose (`mem_limit`/`cpus` absent) — risky given Optuna/torch workloads on a 1GB box.
- **Healthcheck:** image-level `pgrep -f monitor.py` every 60s (`Dockerfile:28-29`) detects exit, not hangs; host-side cron `healthcheck.sh` adds restart + Telegram alert.

### Documentation Assessment

- `README.md`: attractive but inaccurate (claims nonexistent LICENSE link at `:103`, TA-Lib/Plotly/Backtrader/Deriv-API integration that aren't in the code, and describes a Market-Intent indicator that isn't any shipped module). It does not mention the four generations, how to run anything, or which entry point is canonical. A newcomer cannot onboard from it.
- `DEPLOYMENT.md` / `ORACLE_DEPLOYMENT.md`: genuinely useful step-by-step ops docs (rare strength), undermined by the missing `.env.example`.
- Inline docs: banner-comment style is consistent; docstrings sparse in v1, decent on public classes in v2 and ml_engine. Public HTTP API of ml_engine is undocumented anywhere except route definitions.
- No architecture doc explains the relationship between generations — the single biggest onboarding gap.

### Observability Assessment

- **Logging:** best-in-repo is `vix75_cloud/monitor.py:123-150` — stdlib logging, rotating handlers (100MB×5 main, 50MB×3 signals), separate console handler. Elsewhere: `print()` in ~12 modules (no levels/timestamps/files) and two FileHandler setups (local/strict_sell). Committed log files double as the only historical record.
- **Error tracking:** none (no Sentry/Rollbar/equivalent). Failures surface as console prints or silent swallows.
- **Health checks:** container process-liveness only; no application health endpoint in the cloud monitor (the Flask service has `/api/status` but is a different component).
- **Metrics:** none (no Prometheus/statsd/counters). psutil is imported by the cloud monitor presumably for memory guardrails.
- **Traceability:** cannot trace a signal end-to-end from logs today — correlation IDs don't exist, print-output isn't aggregated, and the signal CSV lacks run/session identifiers. Adding a structured "signal lifecycle" log (detected → scored → fired → alerted) to the cloud monitor alone would transform debuggability.

---

## Recommended Action Plan

### Phase 1: Immediate (this week)
1. **Rotate every exposed credential** — MT5 account password (change via Deriv), Telegram bot token (revoke via @BotFather), Deriv API token (revoke in dashboard), and the SSH key pair (`ssh-key-2026-02-21.key`) — then update deployments from a *non-committed* secret store. Findings 🔴1-4.
2. **Purge secrets from git history** (`git filter-repo` or BFG) on `vix75_v1/.env` and `vix75_v1/vix75_cloud/.env`, force-push, and add a root `.gitignore` covering `.env`, `*.key`, `logs/`, `*.joblib`, `__pycache__/`, data/artifact directories. Finding 🔴1-3, 🟠6.
3. **Remove the private key from the working tree** and store it in a password manager/ssh-agent. Finding 🔴4.
4. **Neutralize the runtime pip-installer** in `vix75_ml_engine.py:67-80` (fail fast with a clear message instead of installing). Finding 🔴7.
5. **Bind the ML service to `127.0.0.1`, remove `CORS(app)`, and add a shared-token check** on `/api/train` and `/api/optimize` — or simply don't expose it on the VPS. Findings 🔴6, 🟠4.
6. Remove `COPY .env .` from `Dockerfile` (compose `env_file` already handles it). Finding 🟠5.

### Phase 2: Short-term (next sprint)
7. Fix EA trading safety: log `ResultRetcodeDescription()` after every send; stop clamping lots up to `lotMin` (skip the trade instead); validate `SYMBOL_TRADE_STOPS_LEVEL`; add margin check. Findings 🟠2-3.
8. Create `.env.example` files matching `DEPLOYMENT.md`, and reconcile `SYMBOL` naming across cloud env and code. Findings 🟡10, infra drift.
9. Stand up minimal CI (GitHub Actions): ruff/black check, `pip-audit`, and a gitleaks secret scan on every push. Findings 🟠9-10.
10. Consolidate the five `requirements.txt` files into one per deployable component with pinned versions and a lockfile; drop `websockets`; replace or pin `pandas_ta<0.4` with `numpy<2`. Dependency table.
11. Write the first tests where they're cheapest and highest-value: pure functions in `vix75_v2/engine/*` and `CalcLots` math (port to Python for testing). Finding 🔴5.
12. Delete or archive dead twins (`phase4_revised` vs `phase4_signal_engine`, `phase5_revised`, `fix_and_rerun`), fix the type hint in `vix75_v2/main.py:37`, and rename `supply_and_demand.mq5` / `vix75_cpp/` honestly. Findings 🟡7-8, 🟢3.
13. Stop printing account number/balance; route v1 prints through the existing logging setup. Findings 🟠6, 🟡3.

### Phase 3: Medium-term (next quarter)
14. Extract a shared `vix75_core` package (config, indicators, swings, zones, signal logging) and migrate v1 phase-files onto it — projected 50–60% deletion of duplicated code and permanent elimination of threshold drift. Findings 🟠1, 🟡1.
15. Declare `vix75_v2` the canonical engine; freeze v1 phases read-only. Add golden-file regression tests over recorded candles before touching signal logic further. Findings 🔴5, 🟠1.
16. Replace pickle/joblib artifacts with a versioned, checksummed model-release process (e.g., ONNX export or signed joblib + pinned sklearn), and validate `/api/assess` payloads with pydantic. Findings 🟠8, data-layer validation.
17. Vectorize the mitigation scans and the phase-2 swing computation; add compose resource limits and an application health endpoint with heartbeat metrics to the cloud monitor. Findings 🟡2, perf section.
18. Rewrite README to match reality (stack, generations, entry points, risk controls); add an architecture page documenting the intended v2 layout and the ml_engine HTTP API. Findings 🟡6, docs section.
19. Add correlation IDs to the signal lifecycle log in the cloud monitor so a signal can be traced detection → alert. Observability section.

---

## File Reference Index

- `EA_testing/quality_pro.mq5`
- `EA_testing/vix75_backtester.py`
- `EA_testing/vix75_data_pipeline.py`
- `EA_testing/venv313/pyvenv.cfg`
- `README.md`
- `REPORT.md`
- `vix75_v1/.env`
- `vix75_v1/.gitignore`
- `vix75_v1/ml_engine/vix75_ml_bridge.py`
- `vix75_v1/ml_engine/vix75_ml_engine.py`
- `vix75_v1/ml_engine/vix75_phase7_ml.py`
- `vix75_v1/ml_engine/requirements.txt`
- `vix75_v1/vix75_backtester.py`
- `vix75_v1/vix75_cloud/.env`
- `vix75_v1/vix75_cloud/DEPLOYMENT.md`
- `vix75_v1/vix75_cloud/Dockerfile`
- `vix75_v1/vix75_cloud/ORACLE_DEPLOYMENT.md`
- `vix75_v1/vix75_cloud/docker-compose.yml`
- `vix75_v1/vix75_cloud/healthcheck.sh`
- `vix75_v1/vix75_cloud/monitor.py`
- `vix75_v1/vix75_cloud/oracle_setup.sh`
- `vix75_v1/vix75_cloud/plot_monitor.py`
- `vix75_v1/vix75_cloud/requirements.txt`
- `vix75_v1/vix75_cloud/ssh-key-2026-02-21.key`
- `vix75_v1/vix75_diagnostic.py`
- `vix75_v1/vix75_fix_and_rerun.py`
- `vix75_v1/vix75_local.py`
- `vix75_v1/vix75_phase1_data_pipeline.py`
- `vix75_v1/vix75_phase2_structure_engine.py`
- `vix75_v1/vix75_phase3_zone_engine.py`
- `vix75_v1/vix75_phase4_revised.py`
- `vix75_v1/vix75_phase4_signal_engine.py`
- `vix75_v1/vix75_phase5_backtester.py`
- `vix75_v1/vix75_phase5_revised.py`
- `vix75_v1/vix75_phase6_live_monitor.py`
- `vix75_v1/vix75_phase7_live_monitor.py`
- `vix75_v1/vix75_phase8_hmm.py`
- `vix75_v1/vix75_strict_sell.py`
- `vix75_v1/requirements.txt`
- `vix75_v2/config.py`
- `vix75_v2/data/mt5_feed.py`
- `vix75_v2/engine/quant.py`
- `vix75_v2/main.py`
- `vix75_v2/requirements.txt`
- `vix75_v2/signals/generator.py`
- `vix75_cpp/main.py`
- `vix75_cpp/refined_signal_engine.py`
- `vix75_cpp/refined_simulator.py`
- `vix75_cpp/signal_engine.py`
- `vix75_cpp/simulator.py`
- `vix75_cpp/trading_bot.py`
- `vix75ea.mq5`
