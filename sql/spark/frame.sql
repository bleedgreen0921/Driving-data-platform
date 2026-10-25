SELECT f.entity_id, f.source_token, f.timestamp_us,
s.entity_id AS scene_id, s.raw_record.name AS scene_name, l.raw_record.location AS location
FROM sample f JOIN scene s ON f.raw_record.scene_token = s.source_token
JOIN log l ON s.raw_record.log_token = l.source_token
