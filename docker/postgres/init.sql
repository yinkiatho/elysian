-- ── Elysian — postgres/init.sql ──────────────────────────────────────────────
-- Runs once when the PostgreSQL container is first created (only on a fresh
-- volume). Peewee's create_tables(safe=True) handles schema creation, so
-- this file only sets performance-relevant settings and extensions.
-- ─────────────────────────────────────────────────────────────────────────────

-- Enable the pg_stat_statements extension for query performance monitoring
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;

-- Grant all privileges on the elysian database to the elysian user
-- (already done by POSTGRES_USER env, kept here for explicit documentation)
GRANT ALL PRIVILEGES ON DATABASE elysian TO elysian;

-- Set the default timezone to UTC
ALTER DATABASE elysian SET timezone TO 'UTC';

-- Recommended settings for a trading workload:
--   synchronous_commit=off  → faster writes, safe for time-series inserts
--   (Elysian uses DB writes for audit/analytics, not for primary state)
ALTER SYSTEM SET synchronous_commit = 'off';
SELECT pg_reload_conf();
