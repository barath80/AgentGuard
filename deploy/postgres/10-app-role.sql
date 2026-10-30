-- Compose only: runs once when the Postgres volume is first created.
-- `agentguard` (POSTGRES_USER) owns the schema and runs migrations; the services
-- connect as `agentguard_app`, which owns nothing and gets its privileges from
-- `python -m agentguard.audit.grants` after each migration. Synthetic dev password.
CREATE ROLE agentguard_app LOGIN PASSWORD 'dev-only-agentguard-app';
