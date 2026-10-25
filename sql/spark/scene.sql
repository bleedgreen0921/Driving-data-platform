SELECT s.entity_id, s.source_token, s.raw_record.name AS name,
s.raw_record.description AS description, l.raw_record.location AS location
FROM scene s JOIN log l ON s.raw_record.log_token = l.source_token
