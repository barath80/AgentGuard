"""Database grants for the runtime role (production Postgres).

Two roles, so the app can't undo its own restrictions:

- the *owner* role owns the schema and runs migrations (and this module);
- the *app* role is what the gateway, broker and console connect as. It owns
  nothing, so it can't grant itself privileges back, and it gets only:
  SELECT/INSERT/UPDATE on ordinary tables, SELECT/INSERT on the append-only
  tables, SELECT on the migration version table, and DELETE nowhere (no
  runtime code path deletes rows).

Run as the owner after every migration, so new tables are covered:

    alembic upgrade head && python -m agentguard.audit.grants --app-role agentguard_app

Scope (docs/THREAT_MODEL.md #8): this protects against a misused or
compromised app connection. The owner role, a superuser, or anyone who can
reassign ownership can still alter the log — no "unbreakable log" claim.
"""

import argparse
import re

from sqlalchemy import create_engine, inspect, text

from agentguard.config import settings

# Written once, never updated or deleted by the application.
APPEND_ONLY = ("audit_events", "operator_actions")
READ_ONLY = ("alembic_version",)
_ROLE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


def statements(app_role: str, tables: list[str]) -> list[str]:
    if not _ROLE.match(app_role):
        raise ValueError(f"not a plain role name: {app_role!r}")
    stmts = [f"GRANT USAGE ON SCHEMA public TO {app_role}"]
    for table in sorted(tables):
        stmts.append(f"REVOKE ALL ON TABLE {table} FROM {app_role}")
        if table in READ_ONLY:
            privileges = "SELECT"
        elif table in APPEND_ONLY:
            privileges = "SELECT, INSERT"
        else:
            privileges = "SELECT, INSERT, UPDATE"
        stmts.append(f"GRANT {privileges} ON TABLE {table} TO {app_role}")
    return stmts


def apply(database_url: str, app_role: str) -> list[str]:
    engine = create_engine(database_url)
    try:
        if engine.dialect.name != "postgresql":
            raise SystemExit("grants apply to Postgres only; SQLite has no grant model")
        tables = inspect(engine).get_table_names(schema="public")
        missing = [t for t in APPEND_ONLY if t not in tables]
        if missing:
            raise SystemExit(f"run migrations first: missing {', '.join(missing)}")
        stmts = statements(app_role, tables)
        with engine.begin() as conn:
            owner = conn.execute(text("select current_user")).scalar_one()
            if owner == app_role:
                raise SystemExit("connect as the schema owner, not the app role: an owner can re-grant itself")
            for stmt in stmts:
                conn.execute(text(stmt))
        return stmts
    finally:
        engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Apply runtime-role grants (run as the schema owner)")
    parser.add_argument("--app-role", default="agentguard_app")
    parser.add_argument("--print", action="store_true", help="print the SQL for the current tables instead")
    args = parser.parse_args(argv)
    if args.print:
        engine = create_engine(settings.database_url)
        print(";\n".join(statements(args.app_role, inspect(engine).get_table_names())) + ";")
        return 0
    stmts = apply(settings.database_url, args.app_role)
    print(f"applied {len(stmts)} grant statements for {args.app_role}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
