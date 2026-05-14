-- =============================================================================
-- Model  : int_user_journey
-- Grain  : one row per client event, enriched with its best-matching server
--          transaction where one exists within a 5-minute window on user_id.
--          Client events with no server match are preserved (LEFT JOIN).
-- Purpose: unifies the two event streams into a single user journey timeline
--          so analysts can reason about the full funnel without touching raw
--          sources. Unmatched rows surface client-side signals that never
--          materialized on the server (dropped conversions, failed payments).
-- Limit  : the 5-minute window is a heuristic. Users on slow connections or
--          retried checkouts may have legitimate gaps larger than 5 minutes.
--          Cross-check with ext_id direct link in stg_server_logs for cases
--          where the window join produces multiple candidates.
-- =============================================================================

-- Note: written to run as a DuckDB view. In a dbt project, swap the view
-- references below with {{ ref('stg_client_events') }} and {{ ref('stg_server_logs') }}.

with client as (
    select * from stg_client_events
),

server as (
    select * from stg_server_logs
),

-- For each client event, find the closest server transaction for the same user
-- within +/- 5 minutes. When multiple candidates exist, prefer the one with
-- the smallest absolute time delta (QUALIFY + ROW_NUMBER pattern).
joined as (
    select
        -- client event columns
        c.event_id,
        c.user_id,
        c.event_at,
        c.event_name,
        c.event_value,
        c.product_id,
        c.page_url,
        c.traffic_source,
        c.is_conversion_client,

        -- matched server transaction columns (null when no match)
        s.transaction_id,
        s.transaction_at,
        s.transaction_amount,
        s.transaction_status,
        s.client_event_id       as server_reported_event_id,
        s.is_completed,
        s.is_failed,
        s.is_refunded,
        s.is_revenue_recognized,

        -- join metadata
        abs(epoch(c.event_at) - epoch(s.transaction_at)) as time_delta_seconds,

        -- flag rows where no server match was found
        s.transaction_id is null                         as is_unmatched_client_event,

        -- flag rows where the time-window join matched but the direct ext_id
        -- link disagrees -- signals a potential data quality issue
        case
            when s.client_event_id is not null
             and s.client_event_id <> c.event_id
            then true
            else false
        end                                              as has_conflicting_ext_id

    from client c
    left join server s
        on  c.user_id = s.user_id
        and abs(epoch(c.event_at) - epoch(s.transaction_at)) <= 300  -- 5 minutes
    qualify
        row_number() over (
            partition by c.event_id
            order by abs(epoch(c.event_at) - epoch(s.transaction_at)) asc nulls last
        ) = 1
)

select * from joined
