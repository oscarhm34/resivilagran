"""Migrate data from SQLite to PostgreSQL - Direct connection version."""
import sqlite3
from sqlalchemy import create_engine, text, inspect

src = sqlite3.connect('/app/instance/cleaning_service.db')
src.row_factory = sqlite3.Row
pg = create_engine('postgresql://nfc_app:LaVilaGran2024!@postgres:5432/cleaning_service')

tables = [r[0] for r in src.execute(
    "SELECT name FROM sqlite_master WHERE type='table' "
    "AND name NOT LIKE 'sqlite_%' AND name != 'alembic_version'"
).fetchall()]

inspector = inspect(pg)
pg_tables = inspector.get_table_names()

# Disable FK checks during migration
with pg.connect() as conn:
    conn.execute(text("SET session_replication_role = 'replica'"))
    conn.commit()

ok = 0
for t in sorted(tables):
    if t not in pg_tables:
        print(f"SKIP {t} (not in PostgreSQL)")
        continue
    rows = src.execute(f'SELECT * FROM {t}').fetchall()
    if not rows:
        continue
    src_cols = [d[0] for d in src.execute(f'SELECT * FROM {t}').description]
    # Get PostgreSQL column info
    pg_cols = {c['name']: str(c['type']) for c in inspector.get_columns(t)}
    bool_cols = {name for name, typ in pg_cols.items() if 'BOOL' in typ.upper()}
    # Only use columns that exist in both SQLite and PostgreSQL
    cols = [c for c in src_cols if c in pg_cols]
    if not cols:
        print(f"  SKIP {t} (no matching columns)")
        continue
    skipped = [c for c in src_cols if c not in pg_cols]
    if skipped:
        print(f"  NOTE {t}: skipping columns {skipped}")

    with pg.connect() as conn:
        conn.execute(text("SET session_replication_role = 'replica'"))
        conn.execute(text(f'DELETE FROM "{t}"'))
        for row in rows:
            vals = {}
            for c in cols:
                v = row[src_cols.index(c)]
                if c in bool_cols and isinstance(v, int):
                    v = bool(v)
                vals[c] = v
            placeholders = ', '.join([f':{c}' for c in cols])
            col_list = ', '.join([f'"{c}"' for c in cols])
            conn.execute(text(f'INSERT INTO "{t}" ({col_list}) VALUES ({placeholders})'), vals)
        conn.commit()
        # Reset sequence
        try:
            if 'id' in cols:
                conn.execute(text(
                    f"SELECT setval(pg_get_serial_sequence('{t}', 'id'), "
                    f"(SELECT COALESCE(MAX(id),1) FROM \"{t}\"))"
                ))
                conn.commit()
        except Exception:
            pass
    print(f"  {t}: {len(rows)} rows")
    ok += 1

# Re-enable FK checks
with pg.connect() as conn:
    conn.execute(text("SET session_replication_role = 'origin'"))
    conn.commit()

src.close()
print(f"\nDone: {ok} tables migrated")
