# Reconciliation & Audit Engine

## Section 1: Stakeholder Briefing Memo

**To:** CEO
**From:** Salam Adedokun (Principal Analytics Engineering)
**Re:** The $200 Revenue inconsistency
**Date:** 2026-05-14

Hi Branden,

The revenue gap in the Appendix comes from the fact that our two systems are counting sales at different points in the customer journey.

The marketing system records a sale when a customer clicks Complete Purchase. At that point, the customer has shown intent to buy, but the payment has not yet been confirmed. So if a card is declined, a payment fails, or the customer does not complete the payment successfully, the marketing system may still count it as revenue.

And the transaction log only records the sale after the payment has actually cleared. That means it reflects money we received, not attempted or failed purchases.

My recommendation is this, for financial reporting, we should use the transaction log as the source of truth. It is the only data source that reflects completed payments. For marketing attribution, we should continue using the marketing data because it helps us understand which campaigns are driving purchase intent.

To prevent this issue as the company scales, we should connect the payment outcome and track back to the marketing system so that failed payments are marked properly. We should also show both figures clearly in reporting: attempted revenue from marketing and confirmed revenue from transactions. Finally, we should set a company-wide rule that finance reports only use confirmed transaction data.

## Section 2: Architecture Sketch

<img width="1448" height="1086" alt="image" src="https://github.com/user-attachments/assets/c922e821-2627-4143-b8d6-c982dc1ed456" />

**Implemntation:**

`run_models.py` creates DuckDB views in order, then exports the reconciliation mart as JSON. The mart is the handoff point between SQL modeling and the agent, and the agent doesn't query the database directly; it reads the snapshot file. This is a deliberate tradeoff explained in Section 3.

The audit agent makes three independent LLM calls per anomaly one to detect, one to verify against raw data, one to generate SQL

## Section 3: Trade-offs

**1. The join uses a 5-minute time window, not the direct foreign key**

The data has `ext_id` is a direct link from server transactions back to client events. I didn't use it as the primary join in `int_user_journey.sql`. In production I'd flip this `ext_id` as primary, time-window as fallback for transactions with no client link

**2. SQL files instead of a dbt project**

Five standalone `.sql` files, each with a grain and limitation block in the header, verified by running them as DuckDB views. No `dbt_project.yml`, no schema tests, no CI.

The files are production-quality SQL. The scaffolding is not. Adding dbt wiring takes maybe 90 minutes and would have taken out time better spent on modeling decisions and the agent logic

Five standalone .sql files were delivered, and each was verified by running it as a DuckDB view.

The SQL is production-quality but there is no dbt_project.yml, no schema tests, and no CI setup.

Adding the dbt wiring would likely take about 90 minutes. For this task, that time was better spent on the modeling decisions and the agent logic, which were the higher-value parts of the work.

## Section 4: Time Estimate vs Actual

**Estimated:** **6h**
**Actual:** **~5h**

Came in under estimate. The data analysis went faster than expected because the two sources share a direct foreign key (`ext_id`), which made the reconciliation logic clear. The agent loop took less time because I committed to a simple three-pass structure rather than trying to build something complex.

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

```

**Run the audit agent**
```bash
# Live mode requires ANTHROPIC_API_KEY set in environment
python agents/audit_agent.py

# Demo mode full loop with pre-seeded responses, no API key needed
python agents/audit_agent.py --mock
```

**Outputs**
- `docs/audit_report.json` structured findings with severity and evidence
- `docs/suggested_fixes.sql` corrective SQL patches, one per verified finding
- `docs/agent_log.txt` timestamped log of every LLM prompt and response
- `docs/schema_drift.log` schema violations caught during extraction

