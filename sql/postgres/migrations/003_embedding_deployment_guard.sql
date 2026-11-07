CREATE OR REPLACE FUNCTION control.guard_embedding_deployment() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE allow_test boolean;
BEGIN
 SELECT test_only_allowed INTO allow_test FROM control.embedding_environment
 WHERE singleton AND database_name=current_database();
 IF allow_test IS NULL THEN RAISE EXCEPTION 'embedding environment not registered'; END IF;
 IF NOT allow_test AND NOT EXISTS (
  SELECT 1 FROM control.embedding_deployment d WHERE singleton
   AND d.database_name=current_database()
   AND d.database_oid=(SELECT oid::text FROM pg_database WHERE datname=current_database())
   AND d.system_identifier=(SELECT system_identifier::text FROM pg_control_system())
 ) THEN RAISE EXCEPTION 'embedding deployment identity mismatch'; END IF;
 IF TG_TABLE_NAME='embedding_space' THEN
  IF NOT allow_test AND NEW.test_only THEN
   RAISE EXCEPTION 'test space forbidden in deployment';
  END IF;
 END IF;
 RETURN NEW;
END $$;
