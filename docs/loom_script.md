# Loom Walkthrough Script
## Reconciliation & Audit Engine -- 5-Minute Technical Walkthrough

*Delivery notes: speak at a measured pace, pause at section breaks. Aim for ~750 words spoken across 5 minutes. Screen share the repo with the file tree visible on the left.*

---

### Opening (20 seconds)

"Quick walkthrough of the Reconciliation and Audit Engine I built for this assessment. I'll cover three things: how the extraction layer handles the messy parts of pulling data from unreliable sources, why I structured the SQL models the way I did, and then I want to spend a minute on the CEO memo specifically -- because that one had a deliberate communication strategy behind it."

---

### Part 1 -- Extraction Strategy (1 min 45 sec)

"Starting with the extractor. The file is in `extractors/extract.py`.

The core problem with client-side event data is that it's unreliable by nature. Users have flaky connections, SDKs retry, events get double-fired. So before any SQL model runs, the extractor does three things: it simulates pagination, it deduplicates, and it validates the schema.

The pagination simulation reads the source JSON in pages of five records -- mimicking what you'd get from a cursor-based API endpoint. The reason to do this rather than just loading the file at once is that it forces the extraction logic to be stateless per page. When you eventually swap the JSON file for a real API call, you change one line and the rest of the pipeline is unaffected.

Deduplication uses a composite key. For client events that's `user_id + event_name + timestamp` -- because there's no reliable single unique ID on browser events. We dropped 618 duplicate records from the source data. Those were real duplicates -- same user, same event, same timestamp. Without that dedup step, any revenue total computed downstream would be inflated.

The schema drift piece is worth noting. Every record gets validated against a declared expected schema -- field presence, types, enum values, timestamp format. Violations get logged to `docs/schema_drift.log` but records still pass through. I made that call deliberately. Dropping records on a schema violation is dangerous -- you lose data silently and the downstream gap looks like a data quality issue when it was actually your pipeline being overly strict. Log it, flag it, let a human decide."

---

### Part 2 -- Warehouse Modeling Structure (1 min 30 sec)

"The SQL is in the `models/` directory, structured in three layers.

Staging is just cleaning. Type casting, null handling, renaming to snake_case, adding a `_source` lineage tag so every downstream row knows where it came from. Nothing else. The reason to keep staging boring is that it's the layer that breaks most often when source schemas change -- and you want that layer to be simple enough to fix in five minutes.

The intermediate model is where the two streams join. It's a left join on `user_id` with a five-minute timestamp window. Left join because unmatched client events are not noise -- they're the signal. A purchase intent that has no corresponding server transaction is exactly the kind of anomaly the audit agent should flag. If I inner-joined, I'd silently hide that.

The mart layer is where the business logic lives, and there's a design decision there worth explaining. Marketing and Finance define a 'conversion' differently. Marketing counts the moment a customer signals intent -- the purchase_intent event firing in the browser. Finance counts the moment money settles -- a completed transaction on the server. Both definitions are correct for their purpose, and the wrong move is to pick one and filter out the other.

So `mart_reconciliation.sql` has both on every row. `conversion_definition_marketing` and `conversion_definition_finance` sit next to each other with an `attribution_category` column that tells you whether a transaction is claimed by both, by neither, or by only one side. The revenue summary model then surfaces the gap as its own column. You can't hide from a column."

---

### Part 3 -- Communication Strategy Behind the CEO Memo (1 min 15 sec)

"The memo is the part I want to be deliberate about.

The scenario is that the CEO is questioning the validity of the entire data infrastructure because there's a $200 discrepancy between two dashboards. That's a loaded question. The wrong response is to be defensive, or to technically correct them, or to over-explain.

My instinct was: lead with the conclusion, not the explanation. Get to 'here is what happened' in sentence two. The CEO does not need to understand how browser SDKs work. They need to trust that you understand it and that it's handled.

I was also careful not to frame this as 'both systems are right' in a way that dodges the question. They are both right -- but the CEO asked which one to use for financial reporting, and the answer to that is unambiguous. Transaction logs. Stated clearly, not buried in qualifications.

The forward-looking section isn't three bullet points. It's a short description of what changes and who owns it. The engineering work is presented as straightforward -- because it is -- and the cultural work around data definitions is acknowledged as the harder part. That's honest, and it's the kind of observation that builds more trust than a polished slide deck.

The memo is under 300 words. I think brevity is the most respectful thing you can offer a CEO who is already worried about their data."

---

### Closing (10 seconds)

"Repo is linked below. Everything runs with `python extractors/extract.py` then `python run_models.py` then `python agents/audit_agent.py --mock`. Happy to go deeper on any of it."
