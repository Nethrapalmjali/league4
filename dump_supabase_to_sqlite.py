"""Copy every table from Supabase PostgreSQL into the local SQLite database.

The SQLite schema is built from the app's own models (db.create_all), so column
types, autoincrement ids and datetimes match exactly what the app expects.

Usage (Windows):
    set SOURCE_DATABASE_URL=postgresql://postgres.<ref>:<password>@aws-0-<region>.pooler.supabase.com:5432/postgres
    python dump_supabase_to_sqlite.py

An existing instance/league.db is renamed to league.backup-<timestamp>.db first.
The source database is only read, never modified.
"""

import os
import sys
from datetime import datetime

from sqlalchemy import MetaData, create_engine, func, select

HERE = os.path.dirname(os.path.abspath(__file__))
TARGET = os.path.join(HERE, "instance", "league.db")


def source_url():
    url = os.environ.get("SOURCE_DATABASE_URL") or (sys.argv[1] if len(sys.argv) > 1 else "")
    if not url:
        sys.exit(__doc__)
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg2://" + url[len("postgresql://"):]
    if "sslmode=" not in url:
        url += ("&" if "?" in url else "?") + "sslmode=require"
    return url


def main():
    src_url = source_url()

    os.makedirs(os.path.dirname(TARGET), exist_ok=True)
    if os.path.exists(TARGET):
        backup = TARGET.replace(".db", f".backup-{datetime.now():%Y%m%d-%H%M%S}.db")
        os.replace(TARGET, backup)
        print(f"Existing SQLite DB moved to {backup}")

    # Point the app at the new SQLite file BEFORE importing it. load_dotenv()
    # never overrides variables that are already set, so .env can't redirect it.
    os.environ["DATABASE_URL"] = "sqlite:///" + TARGET.replace(os.sep, "/")
    sys.path.insert(0, HERE)
    from app import app, db  # noqa: E402  (registers app + scorecard models)

    print("Connecting to Supabase...")
    src_engine = create_engine(src_url)
    src_meta = MetaData()
    src_meta.reflect(bind=src_engine)

    problems = []
    with app.app_context():
        db.create_all()
        with src_engine.connect() as src, db.engine.begin() as dst:
            for table in db.metadata.sorted_tables:  # parents before children
                src_table = src_meta.tables.get(table.name)
                if src_table is None:
                    print(f"  {table.name:<22} not in Supabase, left empty")
                    continue
                cols = [c.name for c in table.columns if c.name in src_table.c]
                rows = src.execute(select(*[src_table.c[n] for n in cols])).mappings().all()
                dst.execute(table.delete())
                if rows:
                    dst.execute(table.insert(), [dict(r) for r in rows])
                copied = dst.execute(select(func.count()).select_from(table)).scalar()
                status = "OK" if copied == len(rows) else "MISMATCH"
                if status != "OK":
                    problems.append(table.name)
                print(f"  {table.name:<22} {len(rows):>6} rows  {status}")

    extra = sorted(set(src_meta.tables) - set(db.metadata.tables))
    if extra:
        print(f"\nTables in Supabase that the app doesn't use (not copied): {', '.join(extra)}")
    if problems:
        sys.exit(f"\nRow count mismatch in: {', '.join(problems)}")
    print(f"\nDone. SQLite database: {TARGET}")


if __name__ == "__main__":
    main()
