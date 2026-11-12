"""Export all tournament data from Supabase PostgreSQL into local SQLite (instance/league.db)."""

import os
import sqlite3
from sqlalchemy import create_engine, text

SUPABASE_URL = "postgresql+psycopg2://postgres.bqoszyelbcxrqojvutva:Rachana%401237@aws-0-ap-southeast-2.pooler.supabase.com:5432/postgres?sslmode=require"

os.makedirs("instance", exist_ok=True)
SQLITE_PATH = os.path.abspath("instance/league.db")

print(f"Connecting to Supabase...")
engine = create_engine(SUPABASE_URL)

TABLES = [
    "player",
    "team_owner",
    "team_wallet",
    "wallet_transaction",
    "sc_team",
    "sc_scorer",
    "sc_player",
    "sc_match",
    "sc_event"
]

# Connect to SQLite
sqlite_conn = sqlite3.connect(SQLITE_PATH)
sqlite_cursor = sqlite_conn.cursor()

with engine.connect() as pg_conn:
    for table in TABLES:
        try:
            # Check if table exists in PostgreSQL
            check = pg_conn.execute(text(f"SELECT to_regclass('public.{table}')")).scalar()
            if not check:
                continue

            # Fetch schema columns
            cols_info = pg_conn.execute(text(f"""
                SELECT column_name, data_type 
                FROM information_schema.columns 
                WHERE table_name = '{table}' 
                ORDER BY ordinal_position
            """)).fetchall()

            col_names = [c[0] for c in cols_info]
            
            # Fetch data
            rows = pg_conn.execute(text(f"SELECT {', '.join(col_names)} FROM {table}")).fetchall()
            print(f"Exporting '{table}': {len(rows)} records...")

            # Create table in SQLite if not exists
            col_defs = []
            for col, dtype in cols_info:
                sql_type = "TEXT"
                if "int" in dtype.lower():
                    sql_type = "INTEGER"
                elif "bool" in dtype.lower():
                    sql_type = "BOOLEAN"
                elif "float" in dtype.lower() or "numeric" in dtype.lower():
                    sql_type = "REAL"
                if col == "id":
                    col_defs.append(f"{col} {sql_type} PRIMARY KEY")
                else:
                    col_defs.append(f"{col} {sql_type}")

            sqlite_cursor.execute(f"DROP TABLE IF EXISTS {table}")
            sqlite_cursor.execute(f"CREATE TABLE {table} ({', '.join(col_defs)})")

            # Insert rows
            if rows:
                placeholders = ", ".join(["?"] * len(col_names))
                insert_sql = f"INSERT INTO {table} ({', '.join(col_names)}) VALUES ({placeholders})"
                
                # Convert row values (e.g. booleans, datetimes) to sqlite compatible types
                formatted_rows = []
                for r in rows:
                    row_vals = []
                    for val in r:
                        if isinstance(val, bool):
                            row_vals.append(1 if val else 0)
                        elif hasattr(val, "isoformat"):
                            row_vals.append(val.isoformat())
                        else:
                            row_vals.append(val)
                    formatted_rows.append(tuple(row_vals))

                sqlite_cursor.executemany(insert_sql, formatted_rows)
                sqlite_conn.commit()
            print(f"  -> Successfully imported {len(rows)} rows into SQLite.")
        except Exception as e:
            print(f"Error exporting table {table}: {e}")

sqlite_conn.close()
print(f"\nAll data successfully backed up to: {SQLITE_PATH}")

