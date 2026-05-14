-- =============================================================================
-- Model  : stg_server_logs
-- Grain  : one row per server transaction (tx_id is the natural primary key)
-- Purpose: cast raw server log JSON to typed, snake_case columns. ext_id is
--          promoted from nested meta so the join to client events is explicit.
-- Limit  : status is treated as an open enum. If the backend adds a new status
--          (e.g. "disputed") it will pass through without error but will be
--          excluded from is_completed and is_failed flags. Add the new value
--          to the CASE block before relying on those flags for reporting.
-- =============================================================================

with raw as (
    select *
    from read_json_auto('data/extracted/server_logs_clean.json')
),

typed as (
    select
        -- identity
        cast(tx_id   as varchar)                                     as transaction_id,
        cast(user_id as varchar)                                     as user_id,

        -- cross-source link: ext_id points to client event_id when present
        case
            when ext_id is not null and trim(cast(ext_id as varchar)) <> ''
            then cast(ext_id as varchar)
            else null
        end                                                          as client_event_id,

        -- timestamps
        cast(timestamp as timestamptz)                               as transaction_at,

        -- financials
        coalesce(cast(amount as double), 0.0)                        as transaction_amount,

        -- status
        lower(cast(status as varchar))                               as transaction_status,

        -- lineage
        'server'                                                     as _source,
        cast(_extracted_at as timestamptz)                           as _extracted_at

    from raw
),

flagged as (
    select
        *,
        transaction_status = 'completed'                             as is_completed,
        transaction_status = 'failed'                                as is_failed,
        transaction_status = 'refunded'                              as is_refunded,

        -- Finance source-of-truth flag: completed, not refunded
        (transaction_status = 'completed')                           as is_revenue_recognized
    from typed
)

select * from flagged
