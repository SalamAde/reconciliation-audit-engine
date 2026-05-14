"""
Reconciliation & Audit Engine -- Extraction Layer
Simulates paginated API ingestion from two sources, deduplicates records,
validates schemas, and writes clean output to /data/extracted/.
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Generator

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

ROOT = Path(__file__).parent.parent
DATA_RAW = ROOT / "data"
DATA_OUT = ROOT / "data" / "extracted"
DOCS = ROOT / "docs"

CLIENT_SRC = DATA_RAW / "client_events.json"
SERVER_SRC = DATA_RAW / "server_logs.json"
CLIENT_OUT = DATA_OUT / "client_events_clean.json"
SERVER_OUT = DATA_OUT / "server_logs_clean.json"
DRIFT_LOG = DOCS / "schema_drift.log"

DATA_OUT.mkdir(parents=True, exist_ok=True)
DOCS.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("extract")

drift_logger = logging.getLogger("schema_drift")
drift_handler = logging.FileHandler(DRIFT_LOG, mode="w", encoding="utf-8")
drift_handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
drift_logger.addHandler(drift_handler)
drift_logger.setLevel(logging.WARNING)

# ---------------------------------------------------------------------------
# Expected schemas
# Required fields and their expected Python types. Enum values constrain
# allowed strings. Optional fields are validated for type only when present.
# ---------------------------------------------------------------------------

CLIENT_SCHEMA: dict[str, Any] = {
    "required": {
        "event_id": str,
        "timestamp": str,
        "user_id": str,
        "event_name": str,
        "properties": dict,
    },
    "optional": {},
    "enums": {
        "event_name": {
            "page_view",
            "add_to_cart",
            "purchase_intent",
            "purchase",
            "session_start",
            "checkout",
            "search",
        }
    },
}

SERVER_SCHEMA: dict[str, Any] = {
    "required": {
        "tx_id": str,
        "timestamp": str,
        "user_id": str,
        "status": str,
        "amount": (int, float),
        "meta": dict,
    },
    "optional": {},
    "enums": {
        "status": {"completed", "failed", "refunded", "pending"},
    },
}

# ---------------------------------------------------------------------------
# Pagination simulator
# Reads a JSON array from disk and yields pages of `page_size` records,
# mirroring how a cursor-based API would return batches.
# Page size resolves in this order: CLI arg > PAGE_SIZE env var > default (5).
# ---------------------------------------------------------------------------

DEFAULT_PAGE_SIZE = 5


def resolve_page_size(cli_value: int | None) -> int:
    if cli_value is not None:
        return max(1, cli_value)
    env_value = os.environ.get("PAGE_SIZE", "").strip()
    if env_value.isdigit() and int(env_value) > 0:
        return int(env_value)
    return DEFAULT_PAGE_SIZE


def paginated_source(path: Path, page_size: int) -> Generator[list[dict], None, None]:
    with open(path, encoding="utf-8") as fh:
        records: list[dict] = json.load(fh)

    total = len(records)
    for offset in range(0, total, page_size):
        page = records[offset : offset + page_size]
        logger.debug("Page %d-%d / %d from %s", offset, offset + len(page), total, path.name)
        yield page


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------

def validate_record(record: dict, schema: dict, source_label: str, index: int) -> list[str]:
    """Return a list of violation strings. Empty list means clean."""
    violations: list[str] = []
    rec_id = record.get("event_id") or record.get("tx_id") or f"idx:{index}"

    for field, expected_type in schema["required"].items():
        if field not in record:
            violations.append(f"MISSING_FIELD field={field} record={rec_id}")
            continue
        if record[field] is not None and not isinstance(record[field], expected_type):
            actual = type(record[field]).__name__
            exp = (
                expected_type.__name__
                if isinstance(expected_type, type)
                else "/".join(t.__name__ for t in expected_type)
            )
            violations.append(
                f"TYPE_MISMATCH field={field} expected={exp} actual={actual} record={rec_id}"
            )

    for field, expected_type in schema.get("optional", {}).items():
        if field in record and record[field] is not None:
            if not isinstance(record[field], expected_type):
                actual = type(record[field]).__name__
                violations.append(
                    f"TYPE_MISMATCH field={field} expected={expected_type.__name__} actual={actual} record={rec_id}"
                )

    for field, allowed in schema.get("enums", {}).items():
        val = record.get(field)
        if val is not None and val not in allowed:
            violations.append(
                f"UNKNOWN_ENUM field={field} value={val!r} allowed={sorted(allowed)} record={rec_id}"
            )

    ts_val = record.get("timestamp", "")
    if ts_val:
        try:
            datetime.fromisoformat(str(ts_val).replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            violations.append(f"BAD_TIMESTAMP value={ts_val!r} record={rec_id}")

    return violations


# ---------------------------------------------------------------------------
# Composite key builders for deduplication
# ---------------------------------------------------------------------------

def client_key(record: dict) -> str:
    # Deterministic composite key: user_id + event_type + timestamp
    return "|".join([
        str(record.get("user_id", "")),
        str(record.get("event_name", "")),
        str(record.get("timestamp", "")),
    ])


def server_key(record: dict) -> str:
    # transaction_id is the natural primary key for server logs
    return str(record.get("tx_id", ""))


# ---------------------------------------------------------------------------
# Normalization
# Promotes ext_id from nested meta to a top-level field for join clarity.
# Does not flatten properties -- the staging SQL layer handles that.
# ---------------------------------------------------------------------------

def normalize_client(record: dict) -> dict:
    return {
        "event_id": record.get("event_id"),
        "timestamp": record.get("timestamp"),
        "user_id": record.get("user_id"),
        "event_name": record.get("event_name"),
        "properties": record.get("properties") or {},
        "_source": "client",
        "_extracted_at": datetime.now(timezone.utc).isoformat(),
    }


def normalize_server(record: dict) -> dict:
    return {
        "tx_id": record.get("tx_id"),
        "timestamp": record.get("timestamp"),
        "user_id": record.get("user_id"),
        "status": record.get("status"),
        "amount": record.get("amount"),
        "ext_id": (record.get("meta") or {}).get("ext_id"),
        "_source": "server",
        "_extracted_at": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# Core extraction function
# ---------------------------------------------------------------------------

def extract(
    source_path: Path,
    output_path: Path,
    schema: dict,
    key_fn,
    normalize_fn,
    source_label: str,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> dict:
    seen_keys: set[str] = set()
    clean_records: list[dict] = []

    total_ingested = 0
    total_duplicates = 0
    total_violations = 0

    for page in paginated_source(source_path, page_size=page_size):
        for record in page:
            total_ingested += 1

            composite_key = key_fn(record)
            if composite_key in seen_keys:
                total_duplicates += 1
                logger.debug("Duplicate dropped: key=%s", composite_key)
                continue
            seen_keys.add(composite_key)

            violations = validate_record(record, schema, source_label, total_ingested)
            if violations:
                total_violations += len(violations)
                for v in violations:
                    drift_logger.warning("[%s] %s", source_label, v)

            # Keep records even with violations so downstream models can decide
            # their own tolerance. The drift log is the audit trail.
            clean_records.append(normalize_fn(record))

    with open(output_path, "w", encoding="utf-8") as fh:
        json.dump(clean_records, fh, indent=2)

    return {
        "source": source_label,
        "total_ingested": total_ingested,
        "duplicates_removed": total_duplicates,
        "schema_violations": total_violations,
        "clean_records_written": len(clean_records),
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Extract and clean source data files.")
    parser.add_argument(
        "--page-size",
        type=int,
        default=None,
        metavar="N",
        help="Records per page (overrides PAGE_SIZE env var, default 5).",
    )
    args = parser.parse_args()
    page_size = resolve_page_size(args.page_size)

    logger.info("Starting extraction run (page_size=%d)", page_size)

    client_stats = extract(
        source_path=CLIENT_SRC,
        output_path=CLIENT_OUT,
        schema=CLIENT_SCHEMA,
        key_fn=client_key,
        normalize_fn=normalize_client,
        source_label="client_events",
        page_size=page_size,
    )

    server_stats = extract(
        source_path=SERVER_SRC,
        output_path=SERVER_OUT,
        schema=SERVER_SCHEMA,
        key_fn=server_key,
        normalize_fn=normalize_server,
        source_label="server_logs",
        page_size=page_size,
    )

    print()
    print("=" * 60)
    print("EXTRACTION SUMMARY")
    print(f"Page size: {page_size}")
    print("=" * 60)
    for stats in [client_stats, server_stats]:
        print(f"\nSource: {stats['source']}")
        print(f"  Records ingested   : {stats['total_ingested']:,}")
        print(f"  Duplicates removed : {stats['duplicates_removed']:,}")
        print(f"  Schema violations  : {stats['schema_violations']:,}")
        print(f"  Clean records out  : {stats['clean_records_written']:,}")

    total_violations = client_stats["schema_violations"] + server_stats["schema_violations"]
    if total_violations:
        print(f"\nSchema drift log written to: {DRIFT_LOG}")
    print()
    print(f"Output: {CLIENT_OUT}")
    print(f"Output: {SERVER_OUT}")
    print("=" * 60)
    logger.info("Extraction complete.")


if __name__ == "__main__":
    main()
