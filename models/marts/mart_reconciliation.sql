-- =============================================================================
-- Model  : mart_reconciliation
-- Grain  : one row per server transaction (transaction_id)
-- Purpose: the canonical reconciliation record. Links every server transaction
--          to its corresponding client-side signal (if one exists), computes
--          the variance between what the server charged and what the client
--          reported, and expresses both the marketing and finance definitions
--          of a conversion side by side in a single row.
--
-- Dual-Definition Strategy:
--   Revenue definitions diverge between teams because each team observes a
--   different slice of reality:
--
--   conversion_definition_marketing (client-side):
--     A conversion is ANY purchase_intent event fired by the browser SDK.
--     This fires before the payment is confirmed, so it includes transactions
--     that later fail. Marketing uses this because it reflects user intent and
--     ad attribution windows -- the click happened, the intent existed.
--     Metric: count and sum of purchase_intent events regardless of server status.
--
--   conversion_definition_finance (server-side):
--     A conversion is a transaction with status = 'completed'. Refunded and
--     failed transactions are excluded because money was not retained.
--     Finance uses this because it reflects settled, recognizable revenue.
--     Metric: count and sum of completed server transactions only.
--
--   Both definitions are valid for their purpose. The danger is when one is
--   substituted for the other -- e.g. marketing reporting "completed sales"
--   using client-side data, or finance excluding dark transactions that had
--   no client event. This model keeps both visible on every row so the gap
--   is always explicit and auditable.
--
-- Limit  : server transactions with no client event (ext_id is null) will show
--          null for all client-side columns. This is correct -- those are
--          transactions the client SDK never observed (direct API, phone orders,
--          backend jobs). They are NOT data quality issues; they are legitimate
--          revenue that marketing attribution cannot claim.
-- =============================================================================

with server as (
    select * from stg_server_logs
),

client as (
    select *
    from stg_client_events
    where event_name = 'purchase_intent'
),

reconciled as (
    select
        s.transaction_id,
        s.user_id,
        s.transaction_at,
        s.transaction_status,

        -- Server-side financials (finance source of truth)
        s.transaction_amount                                         as server_amount,

        -- Client-side reported value (null when no client event linked)
        c.event_value                                                as client_reported_amount,

        -- Variance: positive means server charged more than client reported.
        -- Negative means client reported more than server charged.
        -- Zero means perfect reconciliation.
        coalesce(s.transaction_amount, 0.0)
            - coalesce(c.event_value, 0.0)                          as variance,

        -- Reconciliation status
        coalesce(s.transaction_amount, 0.0)
            = coalesce(c.event_value, 0.0)                          as is_reconciled,

        -- Whether a matching client event exists at all
        c.event_id is not null                                       as has_client_event,
        c.event_id                                                   as client_event_id,

        -- -----------------------------------------------------------------------
        -- CONVERSION DEFINITIONS (dual, side-by-side)
        -- -----------------------------------------------------------------------

        -- Marketing counts the intent signal regardless of server outcome.
        -- A client event fired = a marketing conversion.
        c.event_id is not null                                       as conversion_definition_marketing,

        -- Finance counts only completed transactions with no refund.
        -- This is the revenue-recognized flag.
        s.is_revenue_recognized                                      as conversion_definition_finance,

        -- Conflict flag: marketing claims it; finance does not (or vice versa)
        case
            when c.event_id is not null and not s.is_revenue_recognized then 'marketing_only'
            when c.event_id is null     and s.is_revenue_recognized     then 'finance_only'
            when c.event_id is not null and s.is_revenue_recognized     then 'both'
            else 'neither'
        end                                                          as attribution_category

    from server s
    left join client c
        on s.client_event_id = c.event_id
)

select * from reconciled
