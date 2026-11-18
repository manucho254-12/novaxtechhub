"""Copy your existing SQLite data (instance/novax.db) into a PostgreSQL database.

    export DATABASE_URL="postgresql://user:password@host:5432/dbname"
    python migrate_to_postgres.py --yes                  # copies instance/novax.db
    python migrate_to_postgres.py path/to/other.db --yes  # copies a different SQLite file

What it does: creates the tables in PostgreSQL, then REPLACES their contents with your SQLite data
(admin login, tabs, projects, stack, documents, Password Manager entries and so on), keeping every id
so links such as /docs/3 keep working. Your SQLite file is only read, never changed.

Uploaded files and images are not in the database. They stay in instance/uploads and instance/media,
so keep using the same DATA_DIR. Keep instance/vault.key (or VAULT_KEY) too: it unlocks the saved passwords.
"""
import os
import sqlite3
import sys

if not os.environ.get("DATABASE_URL", "").strip():
    sys.exit("Set DATABASE_URL first, for example: postgresql://user:password@localhost:5432/novax")

import app as site  # connects to PostgreSQL and creates the tables

if not site.USE_PG:
    sys.exit("DATABASE_URL must start with postgresql:// (or postgres://).")

args = [a for a in sys.argv[1:] if not a.startswith("--")]
src_path = args[0] if args else site.DB_PATH
if not os.path.exists(src_path):
    sys.exit(f"SQLite file not found: {src_path}")
if "--yes" not in sys.argv:
    sys.exit(f"This replaces the data in your PostgreSQL database with the contents of {src_path}.\n"
             "Run it again with --yes to continue.")

TABLES = ["admin", "media", "meta", "tabs", "projects", "documents",
          "stack_groups", "stack_items", "tab_items", "vault_entries"]
WITH_ID_SEQUENCE = [t for t in TABLES if t in site.ID_TABLES]

src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True)
src.row_factory = sqlite3.Row
dst = site.connect()
try:
    for table in TABLES:
        try:
            src_cols = [r[1] for r in src.execute(f"PRAGMA table_info({table})")]
        except sqlite3.Error:
            src_cols = []
        if not src_cols:
            print(f"  {table}: not in the SQLite file, skipped")
            continue
        pg_cols = {r[0] for r in dst.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_schema = current_schema() "
            "AND table_name = ?", (table,))}
        cols = [c for c in src_cols if c in pg_cols]
        rows = src.execute(f"SELECT {', '.join(cols)} FROM {table}").fetchall()
        dst.execute(f"DELETE FROM {table}")
        marks = ", ".join("?" for _ in cols)
        for r in rows:
            dst.execute(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({marks})", tuple(r))
        print(f"  {table}: {len(rows)} row(s) copied")
    for table in WITH_ID_SEQUENCE:  # new rows must continue after the highest copied id
        dst.execute(f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
                    f"COALESCE((SELECT MAX(id) FROM {table}), 0) + 1, false)").fetchall()
    dst.commit()
except Exception:
    dst.rollback()
    raise
finally:
    dst.close()
    src.close()
print("Done. Keep DATABASE_URL set (for example in the PythonAnywhere web app settings) and reload the site.")
