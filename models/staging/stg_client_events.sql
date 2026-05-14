-- =============================================================================
-- Model  : stg_client_events
-- Grain  : one row per unique client-side event (user_id + event_name + timestamp)
-- Purpose: cast raw client event JSON to typed, snake_case columns and add a
--          _source tag so downstream models can always trace lineage.
-- Limit  : properties is a heterogeneous bag -- only fields used in downstream
--          models are promoted to columns; the rest stays in properties_raw.
-- =============================================================================

with raw as (
    select *
    from read_json_auto('data/extracted/client_events_clean.json')
),

typed as (
    select
        -- identity
        cast(event_id  as varchar)                              as event_id,
        cast(user_id   as varchar)                              as user_id,

        -- timestamps
        cast(timestamp as timestamptz)                          as event_at,

        -- event classification
        lower(cast(event_name as varchar))                      as event_name,

        -- promoted property columns (null-safe coalesce on each)
        coalesce(cast(properties.value       as double), 0.0)  as event_value,
        coalesce(cast(properties.product_id  as varchar), '')   as product_id,
        coalesce(cast(properties.url         as varchar), '')   as page_url,
        coalesce(cast(properties.source      as varchar), '')   as traffic_source,

        -- full properties bag preserved for schema-drift visibility
        cast(properties as json)                                as properties_raw,

        -- lineage
        'client'                                                as _source,
        cast(_extracted_at as timestamptz)                      as _extracted_at

    from raw
)

select *,
    -- derived convenience flag used in journey and mart models
    case when event_name = 'purchase_intent' then true else false end as is_conversion_client

from typed
