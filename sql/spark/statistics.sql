SELECT 'location' AS dimension, location AS value, count(*) AS frame_count,
CAST(0 AS BIGINT) AS annotation_count, CAST(0 AS BIGINT) AS object_count
FROM fact_frame GROUP BY location
UNION ALL
SELECT 'category', category, count(DISTINCT frame_id), count(*), count(DISTINCT instance_id)
FROM fact_annotation GROUP BY category
UNION ALL
SELECT 'channel', channel, count(DISTINCT frame_id), CAST(0 AS BIGINT), CAST(0 AS BIGINT)
FROM fact_media WHERE is_key_frame = true GROUP BY channel
