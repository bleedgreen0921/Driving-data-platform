CREATE EXTENSION IF NOT EXISTS vector VERSION '0.8.7';
CREATE TABLE control.embedding_space (
 space_id text PRIMARY KEY CHECK(space_id ~ '^[a-f0-9]{64}$'),
 contract jsonb NOT NULL,
 dimension integer NOT NULL CHECK(dimension BETWEEN 1 AND 16000),
 test_only boolean NOT NULL
);
CREATE TABLE control.embedding_generation (
 embedding_generation text PRIMARY KEY CHECK(embedding_generation ~ '^e_[a-f0-9]{64}$'),
 generation text NOT NULL REFERENCES control.generation,
 space_id text NOT NULL REFERENCES control.embedding_space,
 artifact_id text NOT NULL UNIQUE,
 manifest jsonb NOT NULL,
 input_manifest jsonb NOT NULL,
 status text NOT NULL CHECK(status IN ('LOADING','READY')),
 count integer NOT NULL CHECK(count>0),
 UNIQUE(embedding_generation,generation,space_id)
);
CREATE TABLE serving.image_embedding (
 embedding_generation text NOT NULL,
 generation text NOT NULL,
 space_id text NOT NULL,
 media_id text NOT NULL,
 image_sha256 text NOT NULL CHECK(image_sha256 ~ '^[a-f0-9]{64}$'),
 embedding vector NOT NULL,
 PRIMARY KEY(embedding_generation,media_id),
 FOREIGN KEY(embedding_generation,generation,space_id)
  REFERENCES control.embedding_generation(embedding_generation,generation,space_id),
 FOREIGN KEY(generation,media_id) REFERENCES serving.media(generation,entity_id)
);
CREATE TABLE control.embedding_active (
 generation text PRIMARY KEY REFERENCES control.generation,
 embedding_generation text,
 revision bigint NOT NULL DEFAULT 0 CHECK(revision>=0),
 FOREIGN KEY(embedding_generation) REFERENCES control.embedding_generation
);
CREATE TABLE control.embedding_publication (
 event_id text PRIMARY KEY,
 generation text NOT NULL REFERENCES control.generation,
 previous_embedding_generation text REFERENCES control.embedding_generation,
 embedding_generation text NOT NULL REFERENCES control.embedding_generation,
 revision bigint NOT NULL,
 action text NOT NULL CHECK(action IN ('publish','rollback')),
 UNIQUE(generation,revision)
);
CREATE FUNCTION control.guard_embedding_space() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF TG_OP<>'INSERT' THEN RAISE EXCEPTION 'embedding space is immutable'; END IF;
 IF (NEW.contract->>'dimension')::integer IS DISTINCT FROM NEW.dimension
   OR (NEW.contract->>'test_only')::boolean IS DISTINCT FROM NEW.test_only THEN
   RAISE EXCEPTION 'space contract mismatch';
 END IF;
 IF NEW.test_only AND NOT EXISTS(SELECT 1 FROM control.embedding_environment
   WHERE singleton AND database_name=current_database() AND test_only_allowed) THEN
   RAISE EXCEPTION 'test space requires trial database';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER immutable_embedding_space BEFORE INSERT OR UPDATE OR DELETE ON control.embedding_space
 FOR EACH ROW EXECUTE FUNCTION control.guard_embedding_space();
CREATE FUNCTION control.guard_embedding_generation() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE actual_count bigint;
BEGIN
 IF TG_OP='INSERT' THEN
   IF NEW.status<>'LOADING' THEN RAISE EXCEPTION 'generation must start loading'; END IF;
   RETURN NEW;
 END IF;
 IF TG_OP='DELETE' OR OLD.status='READY' THEN
   RAISE EXCEPTION 'embedding generation is immutable';
 END IF;
 IF TG_OP='UPDATE' AND (to_jsonb(NEW)-'status') IS DISTINCT FROM (to_jsonb(OLD)-'status') THEN
   RAISE EXCEPTION 'embedding generation identity is immutable';
 END IF;
 IF NEW.status='READY' THEN
   SELECT count(*) INTO actual_count FROM serving.image_embedding WHERE embedding_generation=NEW.embedding_generation;
   IF actual_count<>NEW.count OR NEW.count<>(NEW.input_manifest->>'count')::integer THEN
     RAISE EXCEPTION 'embedding coverage incomplete';
   END IF;
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER immutable_embedding_generation BEFORE INSERT OR UPDATE OR DELETE ON control.embedding_generation
 FOR EACH ROW EXECUTE FUNCTION control.guard_embedding_generation();
CREATE FUNCTION control.guard_image_embedding() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE expected_dimension integer; state text; payload jsonb; camera boolean; trial boolean;
BEGIN
 IF TG_OP<>'INSERT' THEN RAISE EXCEPTION 'image embeddings are immutable'; END IF;
 SELECT g.status,s.dimension,s.test_only INTO state,expected_dimension,trial
  FROM control.embedding_generation g JOIN control.embedding_space s USING(space_id)
  WHERE g.embedding_generation=NEW.embedding_generation;
 IF state IS DISTINCT FROM 'LOADING' THEN RAISE EXCEPTION 'embedding generation is not loading'; END IF;
 IF trial AND NOT EXISTS(SELECT 1 FROM control.embedding_environment
   WHERE singleton AND database_name=current_database() AND test_only_allowed) THEN
   RAISE EXCEPTION 'test vector requires registered trial database';
 END IF;
 SELECT m.payload,(m.modality='camera' AND m.is_key_frame) INTO payload,camera
  FROM serving.media m WHERE m.generation=NEW.generation AND m.entity_id=NEW.media_id;
 IF camera IS DISTINCT FROM true OR payload->>'sha256' IS DISTINCT FROM NEW.image_sha256 THEN
   RAISE EXCEPTION 'image identity mismatch';
 END IF;
 IF vector_dims(NEW.embedding)<>expected_dimension OR abs(vector_norm(NEW.embedding)-1)>0.0001 THEN
   RAISE EXCEPTION 'embedding dimension or normalization mismatch';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER validate_image_embedding BEFORE INSERT OR UPDATE OR DELETE ON serving.image_embedding
 FOR EACH ROW EXECUTE FUNCTION control.guard_image_embedding();
CREATE FUNCTION control.guard_embedding_pointer() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE target control.embedding_generation; trial boolean;
BEGIN
 IF NEW.embedding_generation IS NULL THEN
   IF TG_OP='INSERT' AND NEW.revision=0 THEN RETURN NEW; END IF;
   RAISE EXCEPTION 'cannot clear embedding pointer';
 END IF;
 SELECT * INTO target FROM control.embedding_generation WHERE embedding_generation=NEW.embedding_generation;
 IF target.status IS DISTINCT FROM 'READY' OR target.generation IS DISTINCT FROM NEW.generation THEN
   RAISE EXCEPTION 'embedding pointer version mismatch';
 END IF;
 SELECT test_only INTO trial FROM control.embedding_space WHERE space_id=target.space_id;
 IF trial AND NOT EXISTS(SELECT 1 FROM control.embedding_environment
   WHERE singleton AND database_name=current_database() AND test_only_allowed) THEN
   RAISE EXCEPTION 'test publication requires trial database';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER validate_embedding_pointer BEFORE INSERT OR UPDATE ON control.embedding_active
 FOR EACH ROW EXECUTE FUNCTION control.guard_embedding_pointer();
