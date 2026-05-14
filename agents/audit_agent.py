"""
Reconciliation & Audit Engine -- Agentic Audit Loop

Three-pass autonomous audit using raw Anthropic API calls only.
No LangChain, no CrewAI, no wrappers.

Pass 1 -- Anomaly Detection:
    LLM reviews the modeled mart output and identifies semantic inconsistencies.

Pass 2 -- Verification:
    For each anomaly, an independent LLM call reviews the raw source data with
    a skeptical prompt. Only anomalies confirmed by raw evidence are flagged.

Pass 3 -- SQL Patch Generation:
    For each verified anomaly, an LLM call generates a corrective SQL snippet.

Outputs:
    /docs/audit_report.json   -- structured anomaly findings
    /docs/suggested_fixes.sql -- corrective SQL patches
    /docs/agent_log.txt       -- full prompt/response log with timestamps
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import anthropic

# ---------------------------------------------------------------------------
# Mock responses for --mock mode (used when ANTHROPIC_API_KEY is unavailable).
# These are pre-seeded based on the actual data analysis so the demo is
# representative, not synthetic. In production, remove this section entirely.
# ---------------------------------------------------------------------------

MOCK_DETECTION_RESPONSE = json.dumps([
  {
    "description": "93 transactions are marked as FAILED by the server but the client fired a purchase_intent event for each one. Marketing is counting $39,162.34 as conversions that Finance never recognized as revenue.",
    "financial_impact": "$39,162.34 in conversion credit claimed by marketing for transactions that did not complete. Overstates marketing ROI and inflates reported conversion rate.",
    "severity": "high",
    "category": "ATTRIBUTION_ERROR",
    "suggested_fix_direction": "Exclude purchase_intent events linked to failed server transactions from marketing conversion totals. Join on ext_id and filter where server status != 'completed'."
  },
  {
    "description": "176 completed server transactions totaling $56,768.99 have no corresponding client event (ext_id is null). Finance recognizes this revenue but marketing cannot attribute it to any channel.",
    "financial_impact": "$56,768.99 in recognized revenue is invisible to marketing attribution. Channel-level ROAS calculations are understated.",
    "severity": "high",
    "category": "DARK_REVENUE",
    "suggested_fix_direction": "Investigate the source of transactions with null ext_id. Likely candidates: direct API orders, phone orders, or backend batch processes. Add a channel tag at the transaction creation layer."
  },
  {
    "description": "The client event stream contains no session_start events. Every purchase_intent fires without a traceable preceding session, making funnel analysis impossible.",
    "financial_impact": "Cannot compute session-to-purchase conversion rate. Top-of-funnel attribution is blind. A/B test results relying on session metrics are invalid.",
    "severity": "medium",
    "category": "TRACKING_GAP",
    "suggested_fix_direction": "Verify that the browser SDK session_start event is implemented. Check if events are being filtered before ingestion. Add session_id as a required field on all client events."
  },
  {
    "description": "Net revenue variance of $17,606.65 exists between Finance ($293,540.47 completed) and Marketing ($275,933.82 purchase_intents). This is not a single data error but the compound effect of dark revenue and failed-but-tracked transactions.",
    "financial_impact": "$17,606.65 unexplained gap between the two reporting systems. Without this model, a stakeholder comparing dashboards would see inconsistent totals with no explanation.",
    "severity": "high",
    "category": "REVENUE_VARIANCE",
    "suggested_fix_direction": "Surface the attribution_category breakdown in executive dashboards. The mart_reconciliation model already decomposes the gap. Route finance to the server-side completed total and marketing to the purchase_intent total, each with documented caveats."
  }
])

MOCK_VERIFICATION_RESPONSES = [
  json.dumps({
    "verified": True,
    "verdict": "confirmed",
    "raw_evidence": "Server summary shows 142 failed transactions. Client summary shows 703 purchase_intents total. Mart shows 93 records in attribution_category='marketing_only' with server status='failed'. The raw server data contains status='failed' on 142 records, 93 of which have a client_event_id link.",
    "explanation": "The raw data directly confirms this. 93 transactions appear in both sources but the server marked them failed while the client counted them as conversions. This is not a modeling artifact -- it is a real behavioral difference between when the client fires (at checkout button press) and when the server resolves (after payment processor response).",
    "confidence": "high"
  }),
  json.dumps({
    "verified": True,
    "verdict": "confirmed",
    "raw_evidence": "Server summary: 225 transactions have no client link (ext_id null). Of these, $56,768.99 are completed. Client summary: 703 purchase_intents with no ext_id gaps. The missing link is definitively on the server side -- these transactions were never associated with a client event at creation time.",
    "explanation": "Confirmed by the raw server data. 225 transactions lack an ext_id entirely, meaning they were created through a path that bypasses the client SDK. This is dark revenue by definition -- real money, no attribution signal.",
    "confidence": "high"
  }),
  json.dumps({
    "verified": True,
    "verdict": "confirmed",
    "raw_evidence": "Client event_type_counts shows: page_view=2989, add_to_cart=1199, purchase_intent=703. session_start count is 0 or absent entirely. No innocent explanation: session_start is a standard SDK event that should fire on every new session.",
    "explanation": "Confirmed. Zero session_start events across 4,891 client records is not a timing artifact or business logic decision. The event is missing from the tracking implementation entirely.",
    "confidence": "high"
  }),
  json.dumps({
    "verified": True,
    "verdict": "confirmed",
    "raw_evidence": "Finance completed total: $293,540.47. Marketing purchase_intent total: $275,933.82. Gap: $17,606.65. Decomposed: $56,768.99 dark revenue minus $39,162.34 failed-but-tracked = $17,606.65 net. The arithmetic checks out against both raw source totals.",
    "explanation": "Confirmed. The gap is real and fully explained by the two root causes (dark revenue and failed conversions). It is not a modeling artifact -- the raw source data produces the same numbers when summed directly.",
    "confidence": "high"
  })
]

MOCK_PATCH_RESPONSES = [
  """-- Patch for A001: Exclude failed transactions from marketing conversion totals.
-- Marketing conversions must require confirmed server completion to prevent
-- counting payment failures as revenue events.
CREATE OR REPLACE VIEW mart_reconciliation_corrected AS
SELECT
    *,
    -- Override marketing conversion flag: only count as conversion when server confirmed
    CASE
        WHEN conversion_definition_marketing AND NOT conversion_definition_finance
        THEN false  -- was a purchase_intent but server failed: not a real conversion
        ELSE conversion_definition_marketing
    END AS conversion_definition_marketing_corrected,
    -- Flag rows that were previously miscounted
    (conversion_definition_marketing AND NOT conversion_definition_finance) AS was_overcounted_by_marketing
FROM mart_reconciliation;
""",
  """-- Patch for A002: Tag dark transactions for attribution investigation.
-- Does not drop them -- they are real revenue. Adds a channel tag so
-- analysts can route them to an 'unattributed' bucket rather than ignoring them.
CREATE OR REPLACE VIEW stg_server_logs_with_channel AS
SELECT
    *,
    CASE
        WHEN client_event_id IS NOT NULL THEN 'client_tracked'
        ELSE 'unattributed'  -- dark transaction: no client SDK event
    END AS attribution_channel,
    client_event_id IS NULL AS is_dark_transaction
FROM stg_server_logs;
""",
  """-- Patch for A003: Add session continuity check to flag orphaned purchase_intents.
-- Until session_start events are implemented, this view identifies purchase_intents
-- that have no same-user page_view or add_to_cart within the prior 60 minutes,
-- which is a proxy for 'no preceding session signal'.
CREATE OR REPLACE VIEW client_events_session_check AS
WITH events AS (SELECT * FROM stg_client_events),
purchase_intents AS (SELECT * FROM events WHERE event_name = 'purchase_intent'),
preceding_activity AS (
    SELECT
        p.event_id,
        p.user_id,
        p.event_at,
        COUNT(e.event_id) AS preceding_event_count
    FROM purchase_intents p
    LEFT JOIN events e
        ON  e.user_id = p.user_id
        AND e.event_at < p.event_at
        AND e.event_at >= p.event_at - INTERVAL '60 minutes'
        AND e.event_name IN ('page_view', 'add_to_cart')
    GROUP BY p.event_id, p.user_id, p.event_at
)
SELECT
    p.*,
    COALESCE(pa.preceding_event_count, 0) > 0 AS has_preceding_session_activity,
    COALESCE(pa.preceding_event_count, 0)      AS preceding_event_count
FROM events p
LEFT JOIN preceding_activity pa ON p.event_id = pa.event_id
WHERE p.event_name = 'purchase_intent';
""",
  """-- Patch for A004: Revenue variance reconciliation view.
-- Surfaces the exact decomposition of the $17,606.65 gap for stakeholder reporting.
-- Use this as the source for executive dashboards instead of each source in isolation.
CREATE OR REPLACE VIEW revenue_reconciliation_summary AS
SELECT
    'finance_completed'          AS revenue_definition,
    COUNT(*)                     AS transaction_count,
    ROUND(SUM(server_amount), 2) AS total_revenue,
    'Server-side completed transactions only. Source of truth for financial reporting.' AS definition_note
FROM mart_reconciliation
WHERE conversion_definition_finance

UNION ALL

SELECT
    'marketing_purchase_intents' AS revenue_definition,
    COUNT(*)                     AS transaction_count,
    ROUND(SUM(COALESCE(client_reported_amount, 0)), 2) AS total_revenue,
    'Client-side purchase_intent events. Used for attribution only, not financial reporting.' AS definition_note
FROM mart_reconciliation
WHERE conversion_definition_marketing

UNION ALL

SELECT
    'variance_dark_revenue'      AS revenue_definition,
    COUNT(*)                     AS transaction_count,
    ROUND(SUM(server_amount), 2) AS total_revenue,
    'Completed server transactions with no client event. Finance sees this; marketing cannot attribute it.' AS definition_note
FROM mart_reconciliation
WHERE attribution_category = 'finance_only' AND conversion_definition_finance

UNION ALL

SELECT
    'variance_failed_but_tracked' AS revenue_definition,
    COUNT(*)                      AS transaction_count,
    ROUND(SUM(COALESCE(client_reported_amount, 0)), 2) AS total_revenue,
    'Marketing counted these as conversions but the payment failed. Finance correctly excludes them.' AS definition_note
FROM mart_reconciliation
WHERE attribution_category = 'marketing_only';
"""
]

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

ROOT = Path(__file__).parent.parent
DOCS = ROOT / "docs"
DATA_EXTRACTED = ROOT / "data" / "extracted"

MART_PATH = DATA_EXTRACTED / "mart_reconciliation.json"
CLIENT_PATH = DATA_EXTRACTED / "client_events_clean.json"
SERVER_PATH = DATA_EXTRACTED / "server_logs_clean.json"

REPORT_PATH = DOCS / "audit_report.json"
FIXES_PATH = DOCS / "suggested_fixes.sql"
LOG_PATH = DOCS / "agent_log.txt"

DOCS.mkdir(parents=True, exist_ok=True)

MODEL = "claude-opus-4-7"
MAX_ITERATIONS = 5

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def log(label: str, content: str) -> None:
    ts = datetime.now(timezone.utc).isoformat()
    entry = f"\n{'='*70}\n[{ts}] {label}\n{'='*70}\n{content}\n"
    with open(LOG_PATH, "a", encoding="utf-8") as fh:
        fh.write(entry)
    print(f"[{ts[:19]}] {label}")


# ---------------------------------------------------------------------------
# Data loaders
# ---------------------------------------------------------------------------

def load_json(path: Path) -> Any:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def summarise_mart(mart: list[dict]) -> dict:
    """Compute aggregate stats the LLM can reason about without seeing all rows."""
    total = len(mart)
    by_category = {}
    for row in mart:
        cat = row.get("attribution_category", "unknown")
        by_category.setdefault(cat, {"count": 0, "server_total": 0.0, "client_total": 0.0})
        by_category[cat]["count"] += 1
        by_category[cat]["server_total"] += row.get("server_amount") or 0.0
        by_category[cat]["client_total"] += row.get("client_reported_amount") or 0.0

    server_completed = sum(r["server_amount"] for r in mart if r.get("conversion_definition_finance"))
    client_marketing = sum((r.get("client_reported_amount") or 0.0) for r in mart if r.get("conversion_definition_marketing"))
    failed_but_tracked = [r for r in mart if r.get("attribution_category") == "marketing_only"]
    dark_revenue = [r for r in mart if r.get("attribution_category") == "finance_only"]

    return {
        "total_transactions": total,
        "by_attribution_category": by_category,
        "finance_revenue_completed": round(server_completed, 2),
        "marketing_revenue_purchase_intents": round(client_marketing, 2),
        "net_revenue_gap_finance_minus_marketing": round(server_completed - client_marketing, 2),
        "failed_transactions_with_client_tracking": len(failed_but_tracked),
        "failed_value_marketing_claims": round(sum(r.get("client_reported_amount") or 0 for r in failed_but_tracked), 2),
        "dark_transactions_no_client_event": len(dark_revenue),
        "dark_revenue_finance_sees": round(sum(r.get("server_amount") or 0 for r in dark_revenue), 2),
        "sample_failed_but_tracked": failed_but_tracked[:3],
        "sample_dark_revenue": dark_revenue[:3],
    }


def summarise_client(client: list[dict]) -> dict:
    event_types = {}
    for e in client:
        et = e.get("event_name", "unknown")
        event_types[et] = event_types.get(et, 0) + 1

    has_session_start = event_types.get("session_start", 0) > 0
    purchase_intents = [e for e in client if e.get("event_name") == "purchase_intent"]

    return {
        "total_events": len(client),
        "event_type_counts": event_types,
        "has_session_start_events": has_session_start,
        "purchase_intent_count": len(purchase_intents),
        "purchase_intent_value_total": round(sum(e.get("event_value") or 0 for e in purchase_intents), 2),
    }


def summarise_server(server: list[dict]) -> dict:
    status_counts = {}
    for t in server:
        s = t.get("status", "unknown")
        status_counts[s] = status_counts.get(s, 0) + 1

    with_ext_id = [t for t in server if t.get("ext_id")]
    without_ext_id = [t for t in server if not t.get("ext_id")]

    return {
        "total_transactions": len(server),
        "by_status": status_counts,
        "transactions_with_client_link": len(with_ext_id),
        "transactions_without_client_link": len(without_ext_id),
        "revenue_without_client_link": round(
            sum(t.get("amount") or 0 for t in without_ext_id if t.get("status") == "completed"), 2
        ),
    }


# ---------------------------------------------------------------------------
# LLM call wrapper
# ---------------------------------------------------------------------------

_mock_step1_calls = 0
_mock_step2_calls = 0
_mock_step3_calls = 0


def call_llm(client: anthropic.Anthropic | None, label: str, prompt: str, mock: bool = False) -> str:
    global _mock_step1_calls, _mock_step2_calls, _mock_step3_calls

    log(f"PROMPT: {label}", prompt)

    if mock:
        if "Step1" in label:
            response = MOCK_DETECTION_RESPONSE
        elif "Step2" in label:
            idx = min(_mock_step2_calls, len(MOCK_VERIFICATION_RESPONSES) - 1)
            response = MOCK_VERIFICATION_RESPONSES[idx]
            _mock_step2_calls += 1
        else:
            idx = min(_mock_step3_calls, len(MOCK_PATCH_RESPONSES) - 1)
            response = MOCK_PATCH_RESPONSES[idx]
            _mock_step3_calls += 1
        log(f"RESPONSE [MOCK]: {label}", response)
        return response

    message = client.messages.create(
        model=MODEL,
        max_tokens=2048,
        messages=[{"role": "user", "content": prompt}],
    )
    response = message.content[0].text
    log(f"RESPONSE: {label}", response)
    return response


# ---------------------------------------------------------------------------
# Step 1 -- Anomaly Detection
# ---------------------------------------------------------------------------

DETECTION_PROMPT = """You are a senior data engineer auditing a revenue reconciliation system.

Below is a statistical summary of the reconciliation mart. This mart joins server-side transaction logs (the finance source of truth) with client-side browser events (the marketing tracking layer).

RECONCILIATION MART SUMMARY:
{mart_summary}

CLIENT-SIDE EVENT SUMMARY:
{client_summary}

SERVER-SIDE LOG SUMMARY:
{server_summary}

Your task: identify every semantic anomaly or data integrity issue you can find. Focus on:
1. Purchases or conversions that appear in one source but not the other
2. Revenue counted by marketing that finance does not recognize (and vice versa)
3. Structural gaps in the event stream (e.g. purchase events with no preceding session start)
4. Any revenue discrepancy between sources and its likely cause
5. Any other inconsistency a CFO or auditor would want explained

For each anomaly, provide:
- A short, specific description of what the problem is
- Why it matters financially or operationally
- Your initial severity assessment (high / medium / low)
- A suggested category label (e.g. ATTRIBUTION_ERROR, DARK_REVENUE, TRACKING_GAP, REVENUE_VARIANCE)

Format your response as a JSON array. Each element must have these exact keys:
  description, financial_impact, severity, category, suggested_fix_direction

Return only the JSON array, no prose before or after it.
"""


def step1_detect_anomalies(
    llm: anthropic.Anthropic | None,
    mart: list[dict],
    client: list[dict],
    server: list[dict],
    mock: bool = False,
) -> list[dict]:
    mart_summary = json.dumps(summarise_mart(mart), indent=2)
    client_summary = json.dumps(summarise_client(client), indent=2)
    server_summary = json.dumps(summarise_server(server), indent=2)

    prompt = DETECTION_PROMPT.format(
        mart_summary=mart_summary,
        client_summary=client_summary,
        server_summary=server_summary,
    )

    response = call_llm(llm, "Step1/AnomalyDetection", prompt, mock=mock)

    try:
        # Strip markdown code fences if present
        clean = response.strip()
        if clean.startswith("```"):
            clean = clean.split("```", 2)[1]
            if clean.startswith("json"):
                clean = clean[4:]
            clean = clean.rsplit("```", 1)[0].strip()
        anomalies = json.loads(clean)
    except json.JSONDecodeError:
        print("WARNING: Step 1 response was not valid JSON. Wrapping as single item.")
        anomalies = [{"description": response, "severity": "medium", "category": "PARSE_ERROR",
                      "financial_impact": "unknown", "suggested_fix_direction": "manual review"}]

    return anomalies


# ---------------------------------------------------------------------------
# Step 2 -- Verification Pass
# ---------------------------------------------------------------------------

VERIFICATION_PROMPT = """You are a skeptical senior data engineer reviewing a flagged anomaly in a reconciliation system.

ANOMALY REPORTED:
{anomaly}

RAW CLIENT-SIDE DATA SUMMARY:
{client_summary}

RAW SERVER-SIDE DATA SUMMARY:
{server_summary}

Your task: assume this anomaly might be a modeling artifact or a misinterpretation. Look for the most innocent explanation first.

Only confirm the anomaly if the raw data unambiguously supports it.

Consider:
- Could this be caused by legitimate business logic (e.g. a backend process that bypasses the browser SDK)?
- Could timing differences explain the mismatch?
- Is there a plausible data definition difference between sources that makes this expected?
- Does the raw data actually show the problem, or only the modeled output?

Respond with a JSON object with these exact keys:
  verified (true/false),
  verdict ("confirmed" | "modeling_artifact" | "expected_behavior" | "insufficient_evidence"),
  raw_evidence (a specific data point or count from the raw summaries that supports your verdict),
  explanation (one or two sentences),
  confidence ("high" | "medium" | "low")

Return only the JSON object.
"""


def step2_verify_anomaly(
    llm: anthropic.Anthropic | None,
    anomaly: dict,
    client: list[dict],
    server: list[dict],
    anomaly_id: str,
    mock: bool = False,
) -> dict:
    client_summary = json.dumps(summarise_client(client), indent=2)
    server_summary = json.dumps(summarise_server(server), indent=2)

    prompt = VERIFICATION_PROMPT.format(
        anomaly=json.dumps(anomaly, indent=2),
        client_summary=client_summary,
        server_summary=server_summary,
    )

    response = call_llm(llm, f"Step2/Verification/{anomaly_id}", prompt, mock=mock)

    try:
        clean = response.strip()
        if clean.startswith("```"):
            clean = clean.split("```", 2)[1]
            if clean.startswith("json"):
                clean = clean[4:]
            clean = clean.rsplit("```", 1)[0].strip()
        result = json.loads(clean)
    except json.JSONDecodeError:
        result = {
            "verified": False,
            "verdict": "insufficient_evidence",
            "raw_evidence": response[:200],
            "explanation": "Could not parse verification response.",
            "confidence": "low",
        }

    return result


# ---------------------------------------------------------------------------
# Step 3 -- SQL Patch Generation
# ---------------------------------------------------------------------------

SQL_PATCH_PROMPT = """You are a principal analytics engineer generating a corrective SQL patch for a confirmed data anomaly.

ANOMALY:
{anomaly}

VERIFICATION RESULT:
{verification}

The data warehouse uses DuckDB SQL. The relevant tables/views are:
  - stg_client_events: event_id, user_id, event_at, event_name, event_value, product_id, is_conversion_client
  - stg_server_logs: transaction_id, user_id, client_event_id, transaction_at, transaction_amount, transaction_status, is_revenue_recognized
  - mart_reconciliation: transaction_id, user_id, server_amount, client_reported_amount, variance, is_reconciled, conversion_definition_marketing, conversion_definition_finance, attribution_category

Generate a corrective SQL snippet. This could be:
  - A WHERE clause filter to exclude bad rows from a model
  - A CASE WHEN expression to correct a field value
  - A model amendment (amended SELECT or CTE) that fixes the root cause

The patch should be production-safe: it must not silently drop valid data.
Add a brief comment explaining what it corrects and why.

Return only the SQL. No explanation outside of SQL comments.
"""


def step3_generate_sql_patch(
    llm: anthropic.Anthropic | None,
    anomaly: dict,
    verification: dict,
    anomaly_id: str,
    mock: bool = False,
) -> str:
    prompt = SQL_PATCH_PROMPT.format(
        anomaly=json.dumps(anomaly, indent=2),
        verification=json.dumps(verification, indent=2),
    )
    return call_llm(llm, f"Step3/SQLPatch/{anomaly_id}", prompt, mock=mock)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run_audit(mock: bool = False) -> None:
    llm: anthropic.Anthropic | None = None
    if not mock:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            print("ERROR: ANTHROPIC_API_KEY environment variable not set.")
            print("Tip: run with --mock to execute with pre-seeded responses for testing.")
            sys.exit(1)
        llm = anthropic.Anthropic(api_key=api_key)
    else:
        print("[MOCK MODE] Using pre-seeded responses. Set ANTHROPIC_API_KEY and run without --mock for live LLM calls.")

    print("Loading data...")
    mart = load_json(MART_PATH)
    client = load_json(CLIENT_PATH)
    server = load_json(SERVER_PATH)

    all_findings: list[dict] = []
    all_patches: list[tuple[str, str]] = []

    # Track anomaly descriptions across iterations to detect no new findings
    seen_descriptions: set[str] = set()

    for iteration in range(1, MAX_ITERATIONS + 1):
        print(f"\n--- Audit Iteration {iteration}/{MAX_ITERATIONS} ---")

        # Step 1: Detect anomalies
        print("Step 1: Detecting anomalies...")
        raw_anomalies = step1_detect_anomalies(llm, mart, client, server, mock=mock)
        print(f"  Detected {len(raw_anomalies)} anomaly candidate(s).")

        new_anomalies = [
            a for a in raw_anomalies
            if a.get("description", "") not in seen_descriptions
        ]

        if not new_anomalies:
            print("  No new anomalies found. Exiting loop early.")
            break

        for anomaly in new_anomalies:
            seen_descriptions.add(anomaly.get("description", ""))

        # Step 2: Verify each anomaly independently
        print("Step 2: Verifying anomalies against raw source data...")
        for anomaly in new_anomalies:
            anomaly_id = f"A{len(all_findings) + 1:03d}"
            print(f"  Verifying {anomaly_id}: {anomaly.get('description', '')[:60]}...")

            verification = step2_verify_anomaly(llm, anomaly, client, server, anomaly_id, mock=mock)
            verified = verification.get("verified", False)
            print(f"  Result: {'CONFIRMED' if verified else 'NOT CONFIRMED'} "
                  f"({verification.get('verdict', 'unknown')})")

            # Step 3: Generate SQL patch for verified findings
            sql_patch = None
            if verified:
                print(f"  Generating SQL patch for {anomaly_id}...")
                sql_patch = step3_generate_sql_patch(llm, anomaly, verification, anomaly_id, mock=mock)

            # Assemble finding
            finding = {
                "anomaly_id": anomaly_id,
                "description": anomaly.get("description", ""),
                "severity": anomaly.get("severity", "medium"),
                "category": anomaly.get("category", "UNKNOWN"),
                "financial_impact": anomaly.get("financial_impact", ""),
                "verified": verified,
                "verdict": verification.get("verdict", ""),
                "raw_evidence": verification.get("raw_evidence", ""),
                "explanation": verification.get("explanation", ""),
                "confidence": verification.get("confidence", "low"),
                "recommended_fix": anomaly.get("suggested_fix_direction", ""),
                "iteration": iteration,
            }
            all_findings.append(finding)

            if verified and sql_patch:
                all_patches.append((anomaly_id, sql_patch))

        # Graceful early exit: if all anomalies this iteration were already seen
        # (caught above), we already broke. Here we also break if nothing was verified.
        newly_verified = [f for f in all_findings if f["verified"] and f["iteration"] == iteration]
        if not newly_verified and iteration > 1:
            print("  No verified findings this iteration. Stopping.")
            break

    # Write audit report
    with open(REPORT_PATH, "w", encoding="utf-8") as fh:
        json.dump(all_findings, fh, indent=2)
    print(f"\nAudit report written: {REPORT_PATH} ({len(all_findings)} findings)")

    # Write SQL patches
    with open(FIXES_PATH, "w", encoding="utf-8") as fh:
        fh.write("-- Suggested SQL Patches\n")
        fh.write("-- Generated by audit_agent.py\n")
        fh.write(f"-- Run date: {datetime.now(timezone.utc).isoformat()}\n\n")

        if not all_patches:
            fh.write("-- No verified anomalies generated SQL patches in this run.\n")
        else:
            for anomaly_id, patch in all_patches:
                fh.write(f"-- Patch for anomaly {anomaly_id}\n")
                fh.write(patch.strip())
                fh.write("\n\n")

    print(f"SQL patches written: {FIXES_PATH} ({len(all_patches)} patch(es))")

    verified_count = sum(1 for f in all_findings if f["verified"])
    print(f"\nSummary: {len(all_findings)} finding(s), {verified_count} verified.")
    print(f"Agent log: {LOG_PATH}")


if __name__ == "__main__":
    use_mock = "--mock" in sys.argv
    run_audit(mock=use_mock)
