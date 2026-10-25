SELECT m.entity_id, m.source_token, f.entity_id AS frame_id, m.timestamp_us,
m.raw_record.filename AS path, m.raw_record.is_key_frame AS is_key_frame,
s.raw_record.channel AS channel, s.raw_record.modality AS modality,
m.raw_record.width AS width, m.raw_record.height AS height,
m.raw_record.calibrated_sensor_token AS calibrated_sensor_token,
m.raw_record.ego_pose_token AS ego_pose_token
FROM sample_data m JOIN sample f ON m.raw_record.sample_token = f.source_token
JOIN calibrated_sensor c ON m.raw_record.calibrated_sensor_token = c.source_token
JOIN sensor s ON c.raw_record.sensor_token = s.source_token
