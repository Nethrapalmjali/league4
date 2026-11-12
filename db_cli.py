"""Interactive or one-shot SQL runner for GM League's PostgreSQL database."""

import sys
import os
from sqlalchemy import create_engine, text
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://postgres.bqoszyelbcxrqojvutva:Rachana%401237@aws-0-ap-southeast-2.pooler.supabase.com:5432/postgres?sslmode=require"
)

if DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = "postgresql+psycopg2://" + DATABASE_URL[len("postgresql://"):]

engine = create_engine(DATABASE_URL)

def run_query(sql: str):
    sql = sql.strip().rstrip(";")
    if not sql:
        return
    with engine.begin() as conn:
        res = conn.execute(text(sql))
        if res.returns_rows:
            rows = res.fetchall()
            keys = res.keys()
            if not rows:
                print("(0 rows returned)")
                return
            col_widths = {k: len(str(k)) for k in keys}
            for row in rows:
                for k, v in zip(keys, row):
                    col_widths[k] = max(col_widths[k], len(str(v)))
            
            header = " | ".join(str(k).ljust(col_widths[k]) for k in keys)
            sep = "-+-".join("-" * col_widths[k] for k in keys)
            print(header)
            print(sep)
            for row in rows:
                print(" | ".join(str(v).ljust(col_widths[k]) for k, v in zip(keys, row)))
            print(f"({len(rows)} row{'s' if len(rows) != 1 else ''})")
        else:
            print(f"Query OK. Affected rows: {res.rowcount}")

if __name__ == "__main__":
    if len(sys.argv) > 1:
        query = " ".join(sys.argv[1:])
        try:
            run_query(query)
        except Exception as e:
            print(f"SQL Error: {e}")
    else:
        print("Connected to Supabase PostgreSQL.")
        print("Type your SQL query and press Enter. Type 'exit' or 'quit' to close.")
        while True:
            try:
                line = input("supabase-sql> ").strip()
                if line.lower() in ("exit", "quit", "q"):
                    break
                if line:
                    run_query(line)
            except (KeyboardInterrupt, EOFError):
                break
            except Exception as e:
                print(f"SQL Error: {e}")
