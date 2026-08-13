#!/usr/bin/env python3
"""Migrate data from SQLite to PostgreSQL.

Usage (inside Docker container):
    python migrate_sqlite_to_postgres.py

Prerequisites:
    - PostgreSQL container running and accessible
    - SQLite database at /app/instance/cleaning_service.db
    - DATABASE_URL environment variable set to PostgreSQL URL
"""
import os
import sys
import sqlite3

# Ensure we can import the app
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

SQLITE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'instance', 'cleaning_service.db')


def get_sqlite_tables(sqlite_conn):
    """Get all table names from SQLite."""
    cursor = sqlite_conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name != 'alembic_version'"
    )
    return [row[0] for row in cursor.fetchall()]


def get_table_columns(sqlite_conn, table_name):
    """Get column names for a table."""
    cursor = sqlite_conn.execute(f"PRAGMA table_info({table_name})")
    return [row[1] for row in cursor.fetchall()]


def migrate():
    """Main migration function."""
    if not os.path.exists(SQLITE_PATH):
        print(f"ERROR: SQLite database not found at {SQLITE_PATH}")
        sys.exit(1)

    pg_url = os.environ.get('DATABASE_URL')
    if not pg_url or 'postgresql' not in pg_url:
        print("ERROR: DATABASE_URL must be set to a PostgreSQL URL")
        print("Example: DATABASE_URL=postgresql://nfc_app:password@postgres:5432/cleaning_service")
        sys.exit(1)

    print(f"Source: {SQLITE_PATH}")
    print(f"Target: {pg_url}")
    print()

    # Connect to SQLite
    sqlite_conn = sqlite3.connect(SQLITE_PATH)
    sqlite_conn.row_factory = sqlite3.Row

    # Connect to PostgreSQL via SQLAlchemy
    from sqlalchemy import create_engine, text, inspect
    pg_engine = create_engine(pg_url)

    # First, create all tables in PostgreSQL using Flask-Migrate
    print("=== Step 1: Creating tables in PostgreSQL ===")
    os.environ['DATABASE_URL'] = pg_url
    from app import app, db
    with app.app_context():
        db.create_all()
        print("Tables created successfully.")

    # Get SQLite tables
    tables = get_sqlite_tables(sqlite_conn)
    print(f"\n=== Step 2: Migrating {len(tables)} tables ===")

    # Order tables to respect foreign keys (parents first)
    # Simple approach: try all, retry failures
    migrated = set()
    remaining = list(tables)
    max_retries = 5

    for attempt in range(max_retries):
        if not remaining:
            break
        still_remaining = []
        for table_name in remaining:
            try:
                columns = get_table_columns(sqlite_conn, table_name)
                rows = sqlite_conn.execute(f"SELECT * FROM {table_name}").fetchall()

                if not rows:
                    print(f"  {table_name}: 0 rows (skip)")
                    migrated.add(table_name)
                    continue

                with pg_engine.connect() as pg_conn:
                    # Check if table exists in PostgreSQL
                    inspector = inspect(pg_engine)
                    if table_name not in inspector.get_table_names():
                        print(f"  {table_name}: table not in PostgreSQL (skip)")
                        migrated.add(table_name)
                        continue

                    # Clear existing data
                    pg_conn.execute(text(f"DELETE FROM {table_name}"))

                    # Insert rows
                    placeholders = ', '.join([f':{c}' for c in columns])
                    col_names = ', '.join(columns)
                    insert_sql = text(f"INSERT INTO {table_name} ({col_names}) VALUES ({placeholders})")

                    batch = []
                    for row in rows:
                        row_dict = {}
                        for i, col in enumerate(columns):
                            val = row[i]
                            # Handle SQLite booleans (0/1) for PostgreSQL
                            row_dict[col] = val
                        batch.append(row_dict)

                    if batch:
                        pg_conn.execute(insert_sql, batch)
                    pg_conn.commit()

                    # Reset sequence for auto-increment columns
                    pg_cols = inspector.get_columns(table_name)
                    for col_info in pg_cols:
                        if col_info.get('autoincrement', False) or col_info['name'] == 'id':
                            try:
                                max_id = max(r['id'] for r in batch) if batch and 'id' in batch[0] else 0
                                if max_id:
                                    pg_conn.execute(text(
                                        f"SELECT setval(pg_get_serial_sequence('{table_name}', 'id'), {max_id})"
                                    ))
                                    pg_conn.commit()
                            except Exception:
                                pass
                            break

                print(f"  {table_name}: {len(rows)} rows migrated")
                migrated.add(table_name)

            except Exception as e:
                error_msg = str(e)
                if 'foreign key' in error_msg.lower() or 'violates' in error_msg.lower():
                    still_remaining.append(table_name)
                else:
                    print(f"  {table_name}: ERROR - {error_msg}")
                    still_remaining.append(table_name)

        remaining = still_remaining
        if remaining and attempt < max_retries - 1:
            print(f"\n  Retrying {len(remaining)} tables (attempt {attempt + 2})...")

    if remaining:
        print(f"\n  WARNING: Could not migrate: {remaining}")

    # Stamp alembic version
    print("\n=== Step 3: Stamping Alembic version ===")
    with pg_engine.connect() as pg_conn:
        try:
            # Get latest revision from SQLite
            cursor = sqlite_conn.execute("SELECT version_num FROM alembic_version")
            row = cursor.fetchone()
            if row:
                version = row[0]
                pg_conn.execute(text("DELETE FROM alembic_version"))
                pg_conn.execute(text(f"INSERT INTO alembic_version (version_num) VALUES ('{version}')"))
                pg_conn.commit()
                print(f"  Alembic version: {version}")
            else:
                print("  No alembic version in SQLite")
        except Exception as e:
            print(f"  Alembic stamp error: {e}")

    sqlite_conn.close()

    print(f"\n=== Migration complete: {len(migrated)}/{len(tables)} tables ===")
    print("\nVerification:")
    with pg_engine.connect() as pg_conn:
        for table_name in sorted(migrated):
            try:
                result = pg_conn.execute(text(f"SELECT COUNT(*) FROM {table_name}"))
                count = result.scalar()
                print(f"  {table_name}: {count} rows")
            except Exception:
                print(f"  {table_name}: error counting")


if __name__ == '__main__':
    migrate()
