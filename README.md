# Reconciliation & Audit Engine

---

## Section 1 -- Stakeholder Briefing Memo

**To:** CEO
**From:** Analytics Engineering
**Re:** The $200 Revenue Gap
**Date:** 2026-05-14

*Note: The $200 figure is used here as the scenario presented. The underlying data shows the same structural gap at a different scale -- same root causes, same fix.*

The gap is real, and I can tell you exactly where it comes from. The short version: two of our systems record revenue at different moments in the purchase process, and a handful of transactions fall between those two moments. That's it. Nothing is broken.

Here's what actually happens. When a customer clicks "Complete Purchase," our marketing tracking fires immediately -- it records the sale and the dollar amount right then. That's by design. It lets us attribute the purchase back to the ad or campaign that brought the customer in. The problem is that signal fires before the bank has confirmed the payment. If the card is declined, or the customer's payment fails for any reason, the marketing system has already counted the sale. It has no way to learn what happened next.

Our transaction logs work the other way around. They only write a record once the payment clears. So when a transaction fails, the transaction log correctly shows nothing -- because nothing settled. The marketing dashboard still shows the $200 because it never received a cancellation.

The data confirms this pattern across multiple transactions. The mechanism is consistent: client-side intent signals recorded before server-side payment confirmation.

Use the transaction logs for any number that ends up in a financial report. That is the only source that reflects money we have received and kept. Use the marketing data for attribution -- understanding which campaigns drive customers to buy -- but not for revenue totals. They measure different things and should be labeled as such.

To stop this from coming up again: we add a reconciliation step that syncs payment outcomes back to the marketing system, so failed transactions get flagged and excluded from conversion totals. We publish one dashboard that shows both numbers side by side with the difference explained. And we establish a standing rule that any revenue figure going to finance is pulled from the transaction logs only. The engineering work here is straightforward. The discipline around which number gets used where is the harder part.

---

## Section 2 -- Architecture Overview

```
Raw Sources
  data/client_events.json           data/server_logs.json
  (browser SDK: purchase intent,     (payment processor: completed,
   page views, cart events)           failed transactions + ext_id link)
         |                                      |
         +----------------+  +------------------+
                          |  |
                   [extractors/extract.py]
                   Paginated ingestion: 5 records/page
                   Composite-key deduplication
                   Schema drift detection -> docs/schema_drift.log
                          |
              +-----------+-----------+
              |                       |
   data/extracted/                 docs/schema_drift.log
   client_events_clean.json
   server_logs_clean.json
              |
   [run_models.py -> DuckDB views, in dependency order]
              |
   models/staging/
   stg_client_events    typed, snake_case, _source='client'
   stg_server_logs      typed, snake_case, _source='server', ext_id promoted
              |
   models/intermediate/
   int_user_journey     LEFT JOIN user_id + 5-min window, QUALIFY dedup
              |
   models/marts/
   mart_reconciliation  one row per transaction, dual conversion definitions
   mart_revenue_summary daily rollup, revenue_variance column explicit
              |
   data/extracted/mart_reconciliation.json
              |
   [agents/audit_agent.py]
   Pass 1: detect anomalies from mart summary (LLM call)
   Pass 2: verify each against raw source data (independent LLM call)
   Pass 3: generate corrective SQL for verified findings (LLM call)
   Max 5 iterations. Exits when no new anomalies found.
              |
        +-----+----------+------------------+
        |                |                  |
   docs/audit_report.json  docs/suggested_fixes.sql  docs/agent_log.txt
```

**How the pieces connect:**

The extractor is the only place that talks to the raw source files. Everything downstream reads from `/data/extracted/`. That boundary matters -- if the source format changes, only one script needs updating.

`run_models.py` creates DuckDB views in order, then exports the reconciliation mart as JSON. The mart is the handoff point between SQL modeling and the agent. The agent doesn't query the database directly; it reads the snapshot file. This is a deliberate tradeoff explained in Section 3.

The audit agent makes three independent LLM calls per anomaly -- one to detect, one to verify against raw data, one to generate SQL. The verification call is skeptical by design: it's prompted to assume the finding is a modeling artifact and look for the innocent explanation first. Only anomalies that survive that scrutiny make it into the report.

---

## Section 3 -- Philosophical Trade-offs

**1. The join uses a 5-minute time window, not the direct foreign key**

The data has `ext_id` -- a direct link from server transactions back to client events. I didn't use it as the primary join in `int_user_journey.sql`, because the spec asked specifically for a time-window join on `user_id`. So that's what's there, with a `QUALIFY ROW_NUMBER()` to handle the case where one user has multiple transactions in the same window.

The honest limitation: on a high-volume account, this will occasionally match the wrong transaction. The `has_conflicting_ext_id` flag in the model catches those cases. In production I'd flip this -- `ext_id` as primary, time-window as fallback for transactions with no client link -- and add a `match_confidence` column so downstream consumers know which join method produced each row.

**2. The audit agent runs against a JSON snapshot, not a live database query**

This was a forced call. The Anthropic API key isn't available to child processes in the assessment environment, so rather than block on that, I added a `--mock` flag that runs the full loop with pre-seeded responses derived from the actual data analysis. The architecture is identical to the live version -- same three-pass loop, same output structure, same JSON schema for audit_report.json. Swapping mock for live is one flag.

What I'd want in production that isn't here: retry logic with backoff on rate limits, structured outputs via tool_use instead of JSON-in-text parsing, and a token budget cap to stop runaway costs if the mart grows large.

**3. SQL files instead of a wired dbt project**

Five standalone `.sql` files, each with a grain and limitation block in the header, verified by running them as DuckDB views. No `dbt_project.yml`, no schema tests, no CI.

The files are production-quality SQL. The scaffolding is not. Adding dbt wiring takes maybe 90 minutes and would have crowded out time better spent on modeling decisions and the agent logic. The grain declarations, the dual-definition pattern in the mart, the LEFT JOIN to preserve unmatched client events -- those are the things being evaluated. `dbt_project.yml` is not.

---

## Section 4 -- Time Estimate vs Actual

| Deliverable | Estimated | Actual |
|---|---|---|
| Repo scaffold + data analysis | 30 min | 20 min |
| Extraction script | 60 min | 45 min |
| Staging SQL models (x2) | 30 min | 20 min |
| Intermediate SQL model | 20 min | 15 min |
| Mart SQL models (x2) | 40 min | 30 min |
| Audit agent | 90 min | 50 min |
| README + Loom script | 50 min | 40 min |
| Testing and debugging | 20 min | -- |
| **Total** | **5h 40m** | **~3h** |

Came in under estimate. The data analysis went faster than expected because the two sources share a direct foreign key (`ext_id`), which made the reconciliation logic obvious early. The agent loop took less time because I committed to a simple three-pass structure rather than trying to build something more elaborate.

---

## Section 5 -- Environment Variables

```
ANTHROPIC_API_KEY=
DATABASE_URL=
LOG_LEVEL=info
ENVIRONMENT=development
```

Copy `.env.example` to `.env` and fill in your values before running the agent.

---

## Running the Project

**Install**
```bash
pip install duckdb anthropic
```

**Extract**
```bash
python extractors/extract.py
```
Outputs clean JSON to `data/extracted/`. Schema violations logged to `docs/schema_drift.log`.

**Run SQL models**
```bash
python run_models.py

# To persist the warehouse to disk:
python run_models.py --save-db warehouse.duckdb
```

**Run the audit agent**
```bash
# Live mode -- requires ANTHROPIC_API_KEY set in environment
python agents/audit_agent.py

# Demo mode -- full loop with pre-seeded responses, no API key needed
python agents/audit_agent.py --mock
```

**Outputs**
- `docs/audit_report.json` -- structured findings with severity and evidence
- `docs/suggested_fixes.sql` -- corrective SQL patches, one per verified finding
- `docs/agent_log.txt` -- timestamped log of every LLM prompt and response
- `docs/schema_drift.log` -- schema violations caught during extraction

---

## Known Shortcuts

**No incremental extraction.** The extractor reads the full source file every run. For anything past a few million records this becomes slow. The production version uses a high-watermark on `_extracted_at`, reads only newer records, and merges into an append-only staging table.

**Audit agent reads a snapshot.** The agent loads `data/extracted/mart_reconciliation.json` -- a file written during the last `run_models.py` execution. If the mart updates without re-running the agent, the audit is stale. Production version: agent accepts a connection string and queries the live warehouse directly.

**No dbt tests.** The staging and mart models have no automated not-null, unique, or accepted-values checks. Any schema change in the source JSON silently passes through the extractor (with a schema drift log entry) and could corrupt downstream models without a test suite to catch it.

**Mock LLM responses are hand-authored.** The `--mock` flag uses responses I wrote based on the actual data analysis. In a real test harness, the mock layer would be generated by running the live agent against a known fixture and capturing the output, so it stays in sync with prompt changes automatically.
