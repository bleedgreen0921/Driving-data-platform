-- Run in the project ddp database; no writes or credentials.
SELECT table_name, record_count FROM (
SELECT 'scene' AS table_name, count(*) AS record_count FROM ods."scene"
UNION ALL
SELECT 'sample' AS table_name, count(*) AS record_count FROM ods."sample"
UNION ALL
SELECT 'sample_data' AS table_name, count(*) AS record_count FROM ods."sample_data"
UNION ALL
SELECT 'sensor' AS table_name, count(*) AS record_count FROM ods."sensor"
UNION ALL
SELECT 'calibrated_sensor' AS table_name, count(*) AS record_count FROM ods."calibrated_sensor"
UNION ALL
SELECT 'ego_pose' AS table_name, count(*) AS record_count FROM ods."ego_pose"
UNION ALL
SELECT 'sample_annotation' AS table_name, count(*) AS record_count FROM ods."sample_annotation"
UNION ALL
SELECT 'instance' AS table_name, count(*) AS record_count FROM ods."instance"
UNION ALL
SELECT 'category' AS table_name, count(*) AS record_count FROM ods."category"
UNION ALL
SELECT 'log' AS table_name, count(*) AS record_count FROM ods."log"
UNION ALL
SELECT 'map' AS table_name, count(*) AS record_count FROM ods."map"
UNION ALL
SELECT 'attribute' AS table_name, count(*) AS record_count FROM ods."attribute"
UNION ALL
SELECT 'visibility' AS table_name, count(*) AS record_count FROM ods."visibility"
) counts ORDER BY table_name;

-- Source token joins preserve snapshot boundaries.
SELECT count(*) AS joined_annotations
FROM ods.sample_annotation a
JOIN ods.sample s ON s.snapshot_id=a.snapshot_id AND s.token=a.sample_token
JOIN ods.instance i ON i.snapshot_id=a.snapshot_id AND i.token=a.instance_token
JOIN ods.category c ON c.snapshot_id=i.snapshot_id AND c.token=i.category_token;

-- API and pgAdmin must observe this same active generation and snapshot.
SELECT g.generation,g.snapshot_id,g.processing_version,
       g.manifest->>'profile_id' AS profile_id,
       g.manifest->>'asset_policy' AS asset_policy,
       o.status AS source_snapshot_status,
       (SELECT count(*) FROM serving.frame f WHERE f.generation=g.generation) AS frame_count,
       (SELECT count(*) FROM serving.annotation a WHERE a.generation=g.generation) AS annotation_count
FROM control.active p JOIN control.generation g USING(generation)
JOIN ods.snapshot o USING(snapshot_id);
