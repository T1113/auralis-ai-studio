-- Postgres 版本的初始 schema，从 cloudflare-worker/migrations/0001-0004.sql 翻译过来。
-- 注意：本项目 app.py 当前仍直接走 sqlite3。要切到 Postgres 还需要重写 db 访问层。
-- 这份 SQL 先准备好，便于后续切换。

BEGIN;

CREATE TABLE IF NOT EXISTS users (
  id              TEXT PRIMARY KEY,
  email           TEXT NOT NULL UNIQUE,
  name            TEXT NOT NULL,
  password_hash   TEXT NOT NULL,
  password_salt   TEXT NOT NULL,
  role            TEXT NOT NULL DEFAULT 'user',
  credits         INTEGER NOT NULL DEFAULT 0,
  created_at      BIGINT NOT NULL,
  last_login_at   BIGINT
);

CREATE TABLE IF NOT EXISTS sessions (
  id           TEXT PRIMARY KEY,
  user_id      TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  token_hash   TEXT NOT NULL UNIQUE,
  created_at   BIGINT NOT NULL,
  expires_at   BIGINT NOT NULL,
  user_agent   TEXT,
  ip_address   TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_token_hash ON sessions(token_hash);
CREATE INDEX IF NOT EXISTS idx_sessions_user_id    ON sessions(user_id);

CREATE TABLE IF NOT EXISTS sales_leads (
  id          TEXT PRIMARY KEY,
  source      TEXT NOT NULL,
  name        TEXT,
  company     TEXT,
  contact     TEXT,
  note        TEXT,
  created_at  BIGINT NOT NULL
);

CREATE TABLE IF NOT EXISTS uploads (
  id          TEXT PRIMARY KEY,
  user_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  object_key  TEXT NOT NULL UNIQUE,
  file_name   TEXT NOT NULL,
  mime_type   TEXT NOT NULL,
  size_bytes  BIGINT NOT NULL,
  created_at  BIGINT NOT NULL,
  status      TEXT NOT NULL DEFAULT 'uploaded'
);
CREATE INDEX IF NOT EXISTS idx_uploads_user_id ON uploads(user_id);

CREATE TABLE IF NOT EXISTS jobs (
  id                            TEXT PRIMARY KEY,
  user_id                       TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  style_summary                 TEXT NOT NULL,
  package_tier                  TEXT NOT NULL,
  amount_cents                  INTEGER NOT NULL,
  status                        TEXT NOT NULL DEFAULT 'processing',
  result_count                  INTEGER NOT NULL DEFAULT 6,
  generation_cost_credits       INTEGER NOT NULL DEFAULT 100,
  free_regenerations_remaining  INTEGER NOT NULL DEFAULT 2,
  created_at                    BIGINT NOT NULL,
  updated_at                    BIGINT NOT NULL,
  preview_ready_at              BIGINT NOT NULL,
  charged_at                    BIGINT,
  unlocked_at                   BIGINT
);
CREATE INDEX IF NOT EXISTS idx_jobs_user_id ON jobs(user_id);

CREATE TABLE IF NOT EXISTS job_uploads (
  job_id      TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  upload_id   TEXT NOT NULL REFERENCES uploads(id) ON DELETE CASCADE,
  sort_order  INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (job_id, upload_id)
);
CREATE INDEX IF NOT EXISTS idx_job_uploads_sort_order ON job_uploads(job_id, sort_order);

CREATE TABLE IF NOT EXISTS job_results (
  id                  TEXT PRIMARY KEY,
  job_id              TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  label               TEXT NOT NULL,
  provider            TEXT NOT NULL,
  status              TEXT NOT NULL,
  object_key          TEXT,
  source_upload_id    TEXT REFERENCES uploads(id) ON DELETE SET NULL,
  image_url           TEXT,
  mime_type           TEXT,
  file_name           TEXT,
  quality_score       INTEGER,
  diagnostic_message  TEXT,
  created_at          BIGINT NOT NULL,
  updated_at          BIGINT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_job_results_job_id ON job_results(job_id, created_at);

CREATE TABLE IF NOT EXISTS chat_messages (
  id          TEXT PRIMARY KEY,
  user_id     TEXT NOT NULL,
  is_admin    SMALLINT NOT NULL DEFAULT 0,
  content     TEXT NOT NULL,
  created_at  BIGINT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chat_messages_user_id ON chat_messages(user_id, created_at);

CREATE TABLE IF NOT EXISTS payment_orders (
  id              TEXT PRIMARY KEY,
  user_id         TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  provider        TEXT NOT NULL,
  status          TEXT NOT NULL,
  amount_cents    INTEGER NOT NULL,
  credits         INTEGER NOT NULL,
  out_trade_no    TEXT NOT NULL UNIQUE,
  code_url        TEXT,
  transaction_id  TEXT,
  raw_response    TEXT,
  created_at      BIGINT NOT NULL,
  updated_at      BIGINT NOT NULL,
  paid_at         BIGINT
);
CREATE INDEX IF NOT EXISTS idx_payment_orders_user_id     ON payment_orders(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_payment_orders_out_trade_no ON payment_orders(out_trade_no);

COMMIT;
