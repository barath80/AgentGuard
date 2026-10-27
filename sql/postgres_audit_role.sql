-- Restricts write access to audit_events for production Postgres deployments.
--
-- Application code (agentguard.audit.service) only ever INSERTs into
-- audit_events -- there is no update/delete code path for this table.
-- This makes that a DB-enforced guarantee instead of just a code
-- convention: a bug, a future change, or an injected query cannot alter
-- or erase an audit record (docs/THREAT_MODEL.md #8).
--
-- Replace `agentguard` below with the role your DATABASE_URL connects as.
-- Apply after migrations: psql $DATABASE_URL -f sql/postgres_audit_role.sql
--
-- Scope: this protects against the application's own connection being
-- misused. It does not stop a privileged Postgres superuser or a direct
-- `ALTER TABLE ... OWNER TO` / role-reassignment -- no "unbreakable log"
-- claim, consistent with docs/THREAT_MODEL.md's non-claims section.

REVOKE UPDATE, DELETE, TRUNCATE ON audit_events FROM agentguard;

-- Confirm it stuck: this should show only 'SELECT' and 'INSERT'.
-- SELECT privilege_type FROM information_schema.table_privileges
--   WHERE table_name = 'audit_events' AND grantee = 'agentguard';
