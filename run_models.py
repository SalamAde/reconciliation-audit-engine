"""
run_models.py -- executes all SQL models in dependency order against DuckDB.

Creates views for staging, intermediate, and marts. Writes the mart output
to data/extracted/mart_reconciliation.json for use by the audit agent.

Usage:
    python run_models.py
    python run_models.py --save-db warehouse.duckdb   # persist to file
"""

import argparse
import json
import sys
from pathlib import Path

import duckdb

ROOT = Path(__file__).parent
MODELS = ROOT / "models"
DATA_OUT = ROOT / "data" / "extracted"

# Execution order matters -- each model depends on the previous layer.
MODEL_ORDER = [
    MODELS / "staging" / "stg_client_events.sql",
    MODELS / "staging" / "stg_server_logs.sql",
    MODELS / "intermediate" / "int_user_journey.sql",
    MODELS / "marts" / "mart_reconciliation.sql",
    MODELS / "marts" / "mart_revenue_summary.sql",
]

VIEW_NAMES = [
    "stg_client_events",
    "stg_server_logs",
    "int_user_journey",
    "mart_reconciliation",
    "mart_revenue_summary",
]


def run(db_path: str | None = None) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(db_path or ":memory:")

    for sql_path, view_name in zip(MODEL_ORDER, VIEW_NAMES):
        sql = sql_path.read_text(encoding="utf-8")

        # Strip the header comment block and any trailing semicolons, then
        # wrap in CREATE OR REPLACE VIEW so we can reference it by name.
        # Models are written as bare SELECT statements (dbt style).
        body = sql.strip().rstrip(";")

        con.execute(f"CREATE OR REPLACE VIEW {view_name} AS\n{body}")
        count = con.execute(f"SELECT count(*) FROM {view_name}").fetchone()[0]
        print(f"  {view_name:<30s} {count:>6,} rows")

    # Persist the reconciliation mart as JSON for the audit agent.
    DATA_OUT.mkdir(parents=True, exist_ok=True)
    mart_df = con.execute("SELECT * FROM mart_reconciliation").df()
    mart_path = DATA_OUT / "mart_reconciliation.json"
    mart_df.to_json(mart_path, orient="records", indent=2, date_format="iso")
    print(f"\n  mart_reconciliation.json written ({len(mart_df):,} rows)")

    return con


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--save-db", metavar="PATH", help="Persist DuckDB to file")
    args = parser.parse_args()

    print("Running models...\n")
    con = run(db_path=args.save_db)

    print("\nRevenue reconciliation snapshot:")
    result = con.execute("""
        SELECT
            attribution_category,
            count(*)                                     as transactions,
            round(sum(server_amount), 2)                 as server_revenue,
            round(sum(coalesce(client_reported_amount, 0)), 2) as client_revenue
        FROM mart_reconciliation
        GROUP BY 1
        ORDER BY 2 DESC
    """).fetchall()

    for row in result:
        print(f"  {row[0]:<20s} tx={row[1]:>4}  server=${row[2]:>12,.2f}  client=${row[3]:>12,.2f}")

    print("\nDone.")
