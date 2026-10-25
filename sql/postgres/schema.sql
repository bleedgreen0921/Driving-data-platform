CREATE SCHEMA IF NOT EXISTS control;
CREATE SCHEMA IF NOT EXISTS serving;
CREATE TABLE IF NOT EXISTS control.task (
 task_id text PRIMARY KEY, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS control.attempt (
 attempt_id text PRIMARY KEY, task_id text NOT NULL REFERENCES control.task,
 status text NOT NULL CHECK (status IN ('RUNNING','SUCCEEDED','FAILED','UNKNOWN')),
 error_code text, created_at timestamptz NOT NULL DEFAULT now(), finished_at timestamptz
);
CREATE TABLE IF NOT EXISTS control.generation (
 generation text PRIMARY KEY, snapshot_id text NOT NULL, processing_version text NOT NULL,
 status text NOT NULL CHECK (status IN ('LOADING','READY')),
 manifest_sha256 text NOT NULL UNIQUE, manifest jsonb NOT NULL, quality jsonb NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS control.active (
 singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton), generation text REFERENCES control.generation,
 revision bigint NOT NULL DEFAULT 0
);
INSERT INTO control.active(singleton) VALUES(true) ON CONFLICT DO NOTHING;
CREATE TABLE IF NOT EXISTS control.publication (
 event_id text PRIMARY KEY, previous_generation text REFERENCES control.generation,
 generation text NOT NULL REFERENCES control.generation, revision bigint NOT NULL UNIQUE,
 action text NOT NULL CHECK(action IN ('publish','rollback')), created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS serving.scene (
 generation text REFERENCES control.generation, entity_id text, payload jsonb NOT NULL,
 PRIMARY KEY(generation, entity_id)
);
CREATE TABLE IF NOT EXISTS serving.frame (
 generation text REFERENCES control.generation, entity_id text, timestamp_us bigint NOT NULL,
 scene_id text NOT NULL, location text NOT NULL, payload jsonb NOT NULL,
 PRIMARY KEY(generation, entity_id), FOREIGN KEY(generation, scene_id) REFERENCES serving.scene
);
CREATE INDEX IF NOT EXISTS frame_order ON serving.frame(generation,timestamp_us,entity_id);
CREATE TABLE IF NOT EXISTS serving.media (
 generation text REFERENCES control.generation, entity_id text, frame_id text NOT NULL,
 is_key_frame boolean NOT NULL, channel text NOT NULL, modality text NOT NULL,
 payload jsonb NOT NULL, PRIMARY KEY(generation,entity_id),
 FOREIGN KEY(generation,frame_id) REFERENCES serving.frame
);
CREATE INDEX IF NOT EXISTS media_frame ON serving.media(generation,frame_id,is_key_frame);
CREATE TABLE IF NOT EXISTS serving.annotation (
 generation text REFERENCES control.generation, entity_id text, frame_id text NOT NULL,
 category text NOT NULL, payload jsonb NOT NULL, PRIMARY KEY(generation,entity_id),
 FOREIGN KEY(generation,frame_id) REFERENCES serving.frame
);
CREATE INDEX IF NOT EXISTS annotation_frame ON serving.annotation(generation,frame_id);
CREATE TABLE IF NOT EXISTS serving.frame_category (
 generation text, frame_id text, category text,
 PRIMARY KEY(generation,frame_id,category), FOREIGN KEY(generation,frame_id) REFERENCES serving.frame
);
CREATE TABLE IF NOT EXISTS serving.frame_channel (
 generation text, frame_id text, channel text,
 PRIMARY KEY(generation,frame_id,channel), FOREIGN KEY(generation,frame_id) REFERENCES serving.frame
);
CREATE TABLE IF NOT EXISTS serving.statistics (
 generation text REFERENCES control.generation, dimension text, value text, payload jsonb NOT NULL,
 PRIMARY KEY(generation,dimension,value)
);
CREATE TABLE IF NOT EXISTS serving.dimension (
 generation text REFERENCES control.generation, kind text, identity text, payload jsonb NOT NULL,
 PRIMARY KEY(generation,kind,identity)
);
CREATE OR REPLACE FUNCTION control.guard_service() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE g text;
BEGIN
 IF TG_OP = 'DELETE' THEN g := OLD.generation; ELSE g := NEW.generation; END IF;
 IF (SELECT status FROM control.generation WHERE generation=g) IS DISTINCT FROM 'LOADING' THEN
   RAISE EXCEPTION 'generation is immutable';
 END IF;
 IF TG_OP='UPDATE' AND OLD.generation IS DISTINCT FROM NEW.generation THEN
   RAISE EXCEPTION 'cannot move generation';
 END IF;
 IF TG_OP='DELETE' THEN RETURN OLD; END IF;
 RETURN NEW;
END $$;
DO $$ DECLARE t text; BEGIN
 FOREACH t IN ARRAY ARRAY['scene','frame','media','annotation','frame_category','frame_channel','statistics','dimension'] LOOP
  IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='immutable_'||t) THEN
   EXECUTE format('CREATE TRIGGER %I BEFORE INSERT OR UPDATE OR DELETE ON serving.%I FOR EACH ROW EXECUTE FUNCTION control.guard_service()', 'immutable_'||t, t);
  END IF;
 END LOOP;
END $$;
CREATE OR REPLACE FUNCTION control.guard_generation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF OLD.status='READY' THEN RAISE EXCEPTION 'ready generation is immutable'; END IF;
 IF TG_OP='DELETE' THEN RETURN OLD; END IF;
 RETURN NEW;
END $$;
DO $$ BEGIN
 IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='immutable_generation') THEN
  CREATE TRIGGER immutable_generation BEFORE UPDATE OR DELETE ON control.generation FOR EACH ROW EXECUTE FUNCTION control.guard_generation();
 END IF;
END $$;
