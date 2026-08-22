-- =====================================================================
--  VIX75 Platform - TimescaleDB schema
--  Applied automatically on first container init via
--  docker-entrypoint-initdb.d. Idempotent-ish: guarded by IF NOT EXISTS.
--
--  Hypertables : ohlcv, features   (time-partitioned)
--  Tables      : signals, trades, audit_log
-- =====================================================================

CREATE EXTENSION IF NOT EXISTS timescaledb;

-- ---------------------------------------------------------------------
-- OHLCV market data (hypertable)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ohlcv (
    symbol        TEXT           NOT NULL,
    timeframe     TEXT           NOT NULL,          -- M5 | M15 | H1 | H4 | D1
    ts            TIMESTAMPTZ    NOT NULL,          -- bar OPEN time, UTC
    open          DOUBLE PRECISION NOT NULL,
    high          DOUBLE PRECISION NOT NULL,
    low           DOUBLE PRECISION NOT NULL,
    close         DOUBLE PRECISION NOT NULL,
    tick_volume   BIGINT,
    real_volume   BIGINT,
    spread        INTEGER,
    source        TEXT           NOT NULL DEFAULT 'mt5',
    created_at    TIMESTAMPTZ    NOT NULL DEFAULT now(),
    PRIMARY KEY (symbol, timeframe, ts),
    CONSTRAINT ohlcv_prices_valid CHECK (
        high >= low
        AND high >= open AND high >= close
        AND low  <= open AND low  <= close
        AND open > 0 AND high > 0 AND low > 0 AND close > 0
    )
);

SELECT create_hypertable(
    'ohlcv', 'ts',
    chunk_time_interval => INTERVAL '7 days',
    if_not_exists       => TRUE
);

ALTER TABLE ohlcv SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'symbol, timeframe'
);
SELECT add_compression_policy('ohlcv', INTERVAL '30 days', if_not_exists => TRUE);

-- ---------------------------------------------------------------------
-- Feature store output of feature-service + HMM regime labels
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS features (
    symbol        TEXT             NOT NULL,
    timeframe     TEXT             NOT NULL,
    ts            TIMESTAMPTZ      NOT NULL,
    close         DOUBLE PRECISION,
    atr           DOUBLE PRECISION,
    atr_norm      DOUBLE PRECISION,               -- ATR / close
    rsi           DOUBLE PRECISION,
    ema50         DOUBLE PRECISION,
    ema200        DOUBLE PRECISION,
    bb_upper      DOUBLE PRECISION,
    bb_mid        DOUBLE PRECISION,
    bb_lower      DOUBLE PRECISION,
    stoch_k       DOUBLE PRECISION,
    stoch_d       DOUBLE PRECISION,
    realized_vol  DOUBLE PRECISION,
    log_return    DOUBLE PRECISION,
    swing_high    DOUBLE PRECISION,               -- latest confirmed pivot
    swing_low     DOUBLE PRECISION,
    regime_id     SMALLINT,
    regime_probs  DOUBLE PRECISION[],
    zones         JSONB,                            -- zone snapshot at ts
    created_at    TIMESTAMPTZ      NOT NULL DEFAULT now(),
    PRIMARY KEY (symbol, timeframe, ts)
);

SELECT create_hypertable(
    'features', 'ts',
    chunk_time_interval => INTERVAL '7 days',
    if_not_exists       => TRUE
);

-- Upgrade path for databases initialised with the Sprint-1 schema.
ALTER TABLE features ADD COLUMN IF NOT EXISTS close       DOUBLE PRECISION;
ALTER TABLE features ADD COLUMN IF NOT EXISTS atr_norm     DOUBLE PRECISION;
ALTER TABLE features ADD COLUMN IF NOT EXISTS bb_upper     DOUBLE PRECISION;
ALTER TABLE features ADD COLUMN IF NOT EXISTS bb_mid       DOUBLE PRECISION;
ALTER TABLE features ADD COLUMN IF NOT EXISTS bb_lower     DOUBLE PRECISION;
ALTER TABLE features ADD COLUMN IF NOT EXISTS stoch_k      DOUBLE PRECISION;
ALTER TABLE features ADD COLUMN IF NOT EXISTS stoch_d      DOUBLE PRECISION;
ALTER TABLE features ADD COLUMN IF NOT EXISTS swing_high   DOUBLE PRECISION;
ALTER TABLE features ADD COLUMN IF NOT EXISTS swing_low    DOUBLE PRECISION;

ALTER TABLE features SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'symbol, timeframe'
);
SELECT add_compression_policy('features', INTERVAL '30 days', if_not_exists => TRUE);

-- ---------------------------------------------------------------------
-- Signals produced by signal-service
-- ---------------------------------------------------------------------
DO $$
BEGIN
    CREATE TYPE signal_status AS ENUM (
        'proposed', 'approved', 'rejected', 'executed', 'shadow_filled', 'expired'
    );
EXCEPTION
    WHEN duplicate_object THEN NULL;
END $$;

CREATE TABLE IF NOT EXISTS signals (
    id              UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    created_ts      TIMESTAMPTZ     NOT NULL DEFAULT now(),
    symbol          TEXT            NOT NULL,
    ltf_timeframe   TEXT            NOT NULL,
    direction       TEXT            NOT NULL CHECK (direction IN ('BUY', 'SELL')),
    entry           DOUBLE PRECISION NOT NULL,
    sl              DOUBLE PRECISION NOT NULL,
    tp1             DOUBLE PRECISION NOT NULL,
    tp2             DOUBLE PRECISION NOT NULL,
    score           INTEGER         NOT NULL CHECK (score >= 0),
    max_score       INTEGER         NOT NULL DEFAULT 7,
    components      JSONB           NOT NULL,      -- confluence flags
    p_win           DOUBLE PRECISION,              -- LightGBM meta-label
    regime_id       SMALLINT,
    status          signal_status   NOT NULL DEFAULT 'proposed',
    rejection_reasons JSONB
);

CREATE INDEX IF NOT EXISTS idx_signals_symbol_ts   ON signals (symbol, created_ts DESC);
CREATE INDEX IF NOT EXISTS idx_signals_status_ts   ON signals (status, created_ts DESC);

-- ---------------------------------------------------------------------
-- Trades written by execution-service (one row per order intent;
-- idempotency_key makes retries safe across restarts)
-- ---------------------------------------------------------------------
DO $$
BEGIN
    CREATE TYPE trade_side AS ENUM ('BUY', 'SELL');
    CREATE TYPE trade_status AS ENUM (
        'pending', 'submitted', 'filled', 'rejected', 'cancelled'
    );
EXCEPTION
    WHEN duplicate_object THEN NULL;
END $$;

CREATE TABLE IF NOT EXISTS trades (
    id               BIGSERIAL     PRIMARY KEY,
    idempotency_key  TEXT          NOT NULL UNIQUE,
    signal_id        UUID          REFERENCES signals(id),
    symbol           TEXT          NOT NULL,
    side             trade_side    NOT NULL,
    lots             DOUBLE PRECISION NOT NULL CHECK (lots > 0),
    entry_price      DOUBLE PRECISION,
    sl_price         DOUBLE PRECISION,
    tp_price         DOUBLE PRECISION,
    status           trade_status  NOT NULL DEFAULT 'pending',
    retcode          INTEGER,                      -- MT5 ResultRetcode
    retcode_desc     TEXT,
    broker_ticket    BIGINT,
    requested_at     TIMESTAMPTZ   NOT NULL DEFAULT now(),
    executed_at      TIMESTAMPTZ,
    closed_at        TIMESTAMPTZ,
    profit           DOUBLE PRECISION,
    raw_response     JSONB
);

CREATE INDEX IF NOT EXISTS idx_trades_signal ON trades (signal_id);
CREATE INDEX IF NOT EXISTS idx_trades_status ON trades (status, requested_at DESC);

-- ---------------------------------------------------------------------
-- Audit log - append-only trail for every state-changing action
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS audit_log (
    id          BIGSERIAL      PRIMARY KEY,
    ts          TIMESTAMPTZ    NOT NULL DEFAULT now(),
    actor       TEXT           NOT NULL,           -- service name
    action      TEXT           NOT NULL,           -- e.g. 'order.submit'
    subject     TEXT,                              -- e.g. signal/order key
    outcome     TEXT           NOT NULL,           -- ok | error
    payload     JSONB,                             -- redacted details
    error       TEXT
);

CREATE INDEX IF NOT EXISTS idx_audit_action_ts ON audit_log (action, ts DESC);
CREATE INDEX IF NOT EXISTS idx_audit_actor_ts  ON audit_log (actor, ts DESC);

REVOKE UPDATE, DELETE ON audit_log FROM PUBLIC;
