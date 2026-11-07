CREATE TABLE control.embedding_deployment (
 singleton boolean PRIMARY KEY CHECK(singleton),
 deployment_id text NOT NULL CHECK(deployment_id ~ '^[a-f0-9]{32}$'),
 kind text NOT NULL CHECK(kind IN ('production','rehearsal')),
 database_name text NOT NULL,
 database_oid text NOT NULL,
 system_identifier text NOT NULL
);
CREATE FUNCTION control.guard_embedding_deployment() RETURNS trigger LANGUAGE plpgsql AS $$
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
 IF TG_TABLE_NAME='embedding_space' AND NOT allow_test AND NEW.test_only THEN
  RAISE EXCEPTION 'test space forbidden in deployment';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER check_embedding_space_environment BEFORE INSERT ON control.embedding_space
 FOR EACH ROW EXECUTE FUNCTION control.guard_embedding_deployment();
CREATE TRIGGER check_embedding_generation_environment BEFORE INSERT OR UPDATE ON control.embedding_generation
 FOR EACH ROW EXECUTE FUNCTION control.guard_embedding_deployment();
CREATE TRIGGER check_image_embedding_environment BEFORE INSERT ON serving.image_embedding
 FOR EACH ROW EXECUTE FUNCTION control.guard_embedding_deployment();
CREATE TRIGGER check_embedding_active_environment BEFORE INSERT OR UPDATE ON control.embedding_active
 FOR EACH ROW EXECUTE FUNCTION control.guard_embedding_deployment();
