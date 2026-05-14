-- =============================================================================
-- Model  : mart_revenue_summary
-- Grain  : one row per calendar day per revenue source definition
--          (marketing vs finance), with a cross-source variance column.
-- Purpose: the stakeholder-facing model that makes the gap between client-side
--          and server-side revenue totals explicit and queryable. The
--          revenue_variance column is the number executives ask about.
--          Designed so a BI tool can pivot on revenue_source to show both
--          lines on the same chart.
-- Limit  : day is derived from the server transaction timestamp. Client-only
--          events (purchase_intents with no server match) are not represented
--          in this model -- the server transaction timestamp is the anchor.
--          A separate client-only summary would be needed to surface those.
-- =============================================================================

with recon as (
    select * from mart_reconciliation
),

-- Finance view: completed server transactions, one row per transaction
finance_daily as (
    select
        date_trunc('day', transaction_at)::date  as report_date,
        'finance'                                as revenue_source,
        count(*)                                 as transaction_count,
        sum(server_amount)                       as gross_revenue,
        sum(case when is_reconciled then server_amount else 0 end)
                                                 as reconciled_revenue,
        sum(case when not is_reconciled then server_amount else 0 end)
                                                 as unreconciled_revenue,
        count(case when conversion_definition_finance then 1 end)
                                                 as conversion_count
    from recon
    where conversion_definition_finance
    group by 1, 2
),

-- Marketing view: transactions where the client fired a purchase_intent,
-- using the client-reported value as the revenue figure.
-- This includes transactions that later failed server-side.
marketing_daily as (
    select
        date_trunc('day', transaction_at)::date  as report_date,
        'marketing'                              as revenue_source,
        count(*)                                 as transaction_count,
        sum(coalesce(client_reported_amount, 0)) as gross_revenue,
        sum(case when is_reconciled
                 then coalesce(client_reported_amount, 0)
                 else 0 end)                     as reconciled_revenue,
        sum(case when not is_reconciled
                 then coalesce(client_reported_amount, 0)
                 else 0 end)                     as unreconciled_revenue,
        count(case when conversion_definition_marketing then 1 end)
                                                 as conversion_count
    from recon
    where conversion_definition_marketing
    group by 1, 2
),

combined as (
    select * from finance_daily
    union all
    select * from marketing_daily
),

-- Pivot to put both numbers on the same row for direct comparison.
-- This is the row a CEO or analyst reads when asking "where is the gap?"
pivoted as (
    select
        f.report_date,
        f.transaction_count                              as finance_transaction_count,
        m.transaction_count                              as marketing_transaction_count,
        f.gross_revenue                                  as finance_revenue,
        m.gross_revenue                                  as marketing_revenue,

        -- revenue_variance: positive = finance sees more than marketing.
        -- The expected direction: finance counts dark transactions that
        -- marketing never tracked. Marketing counts failed intents that
        -- finance excludes.
        f.gross_revenue - m.gross_revenue                as revenue_variance,

        f.reconciled_revenue                             as finance_reconciled_revenue,
        m.reconciled_revenue                             as marketing_reconciled_revenue,
        f.conversion_count                               as finance_conversions,
        m.conversion_count                               as marketing_conversions,
        m.conversion_count - f.conversion_count          as conversion_count_variance

    from finance_daily f
    full outer join marketing_daily m using (report_date)
)

select
    *,
    -- percentage gap for executive dashboards
    case
        when finance_revenue > 0
        then round(100.0 * revenue_variance / finance_revenue, 2)
        else null
    end                                                  as revenue_variance_pct

from pivoted
order by report_date
