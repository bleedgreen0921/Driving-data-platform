SELECT a.entity_id, a.source_token, f.entity_id AS frame_id, i.entity_id AS instance_id,
c.raw_record.name AS category, a.raw_record.translation AS translation,
a.raw_record.size AS size, a.raw_record.rotation AS rotation,
a.raw_record.num_lidar_pts AS num_lidar_pts, a.raw_record.num_radar_pts AS num_radar_pts,
a.raw_record.visibility_token AS visibility_token, a.raw_record.attribute_tokens AS attribute_tokens
FROM sample_annotation a JOIN sample f ON a.raw_record.sample_token = f.source_token
JOIN instance i ON a.raw_record.instance_token = i.source_token
JOIN category c ON i.raw_record.category_token = c.source_token
