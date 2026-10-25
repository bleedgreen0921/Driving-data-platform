SELECT
(SELECT count(*) FROM fact_frame) AS frame_count,
(SELECT count(*) FROM fact_annotation) AS annotation_count,
(SELECT count(DISTINCT instance_id) FROM fact_annotation) AS object_count,
(SELECT count(*) FROM fact_media WHERE is_key_frame = false) AS sweep_count,
(SELECT count(*) FROM fact_media WHERE is_key_frame = true) AS keyframe_media_count
