-- Independent source mirror; source tokens and microseconds remain unchanged.
CREATE SCHEMA IF NOT EXISTS ods;
CREATE TABLE IF NOT EXISTS ods.snapshot (
 snapshot_id TEXT PRIMARY KEY, manifest_sha256 TEXT NOT NULL,
 profile_id TEXT NOT NULL, profile_version TEXT NOT NULL,
 asset_policy TEXT NOT NULL CHECK(asset_policy IN ('strict','registered')),
 status TEXT NOT NULL CHECK(status IN ('LOADING','READY')),
 counts JSONB NOT NULL, assets JSONB NOT NULL, capabilities JSONB NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE OR REPLACE FUNCTION ods.guard_snapshot() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF TG_OP='INSERT' AND NEW.status <> 'LOADING' THEN RAISE EXCEPTION 'snapshot must start LOADING'; END IF;
 IF TG_OP<>'INSERT' AND OLD.status='READY' THEN RAISE EXCEPTION 'ready snapshot is immutable'; END IF;
 IF TG_OP='DELETE' THEN RETURN OLD; END IF;
 RETURN NEW;
END $$;
CREATE OR REPLACE FUNCTION ods.guard_record() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE sid text;
BEGIN
 IF TG_OP='DELETE' THEN sid=OLD.snapshot_id; ELSE sid=NEW.snapshot_id; END IF;
 PERFORM 1 FROM ods.snapshot WHERE snapshot_id=sid AND status='LOADING' FOR UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION 'snapshot is not loading'; END IF;
 IF TG_OP='UPDATE' AND OLD.snapshot_id IS DISTINCT FROM NEW.snapshot_id THEN RAISE EXCEPTION 'snapshot move forbidden'; END IF;
 IF TG_OP='DELETE' THEN RETURN OLD; END IF;
 RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS immutable_snapshot ON ods.snapshot;
CREATE TRIGGER immutable_snapshot BEFORE INSERT OR UPDATE OR DELETE ON ods.snapshot FOR EACH ROW EXECUTE FUNCTION ods.guard_snapshot();
CREATE TABLE IF NOT EXISTS ods."attribute" (
 snapshot_id TEXT REFERENCES ods.snapshot,
 "token" TEXT,
 "name" TEXT,
 "description" TEXT,
 source_record JSONB NOT NULL, PRIMARY KEY(snapshot_id,token)
);
DROP TRIGGER IF EXISTS immutable_record ON ods."attribute";
CREATE TRIGGER immutable_record BEFORE INSERT OR UPDATE OR DELETE ON ods."attribute" FOR EACH ROW EXECUTE FUNCTION ods.guard_record();
CREATE TABLE IF NOT EXISTS ods."calibrated_sensor" (
 snapshot_id TEXT REFERENCES ods.snapshot,
 "token" TEXT,
 "sensor_token" TEXT,
 "translation" JSONB,
 "rotation" JSONB,
 "camera_intrinsic" JSONB,
 source_record JSONB NOT NULL, PRIMARY KEY(snapshot_id,token)
);
DROP TRIGGER IF EXISTS immutable_record ON ods."calibrated_sensor";
CREATE TRIGGER immutable_record BEFORE INSERT OR UPDATE OR DELETE ON ods."calibrated_sensor" FOR EACH ROW EXECUTE FUNCTION ods.guard_record();
CREATE TABLE IF NOT EXISTS ods."category" (
 snapshot_id TEXT REFERENCES ods.snapshot,
 "token" TEXT,
 "name" TEXT,
 "description" TEXT,
 source_record JSONB NOT NULL, PRIMARY KEY(snapshot_id,token)
);
DROP TRIGGER IF EXISTS immutable_record ON ods."category";
CREATE TRIGGER immutable_record BEFORE INSERT OR UPDATE OR DELETE ON ods."category" FOR EACH ROW EXECUTE FUNCTION ods.guard_record();
CREATE TABLE IF NOT EXISTS ods."ego_pose" (
 snapshot_id TEXT REFERENCES ods.snapshot,
 "token" TEXT,
 "timestamp" BIGINT,
 "rotation" JSONB,
 "translation" JSONB,
 source_record JSONB NOT NULL, PRIMARY KEY(snapshot_id,token)
);
DROP TRIGGER IF EXISTS immutable_record ON ods."ego_pose";
CREATE TRIGGER immutable_record BEFORE INSERT OR UPDATE OR DELETE ON ods."ego_pose" FOR EACH ROW EXECUTE FUNCTION ods.guard_record();
CREATE TABLE IF NOT EXISTS ods."instance" (
 snapshot_id TEXT REFERENCES ods.snapshot,
 "token" TEXT,
 "category_token" TEXT,
 "nbr_annotations" BIGINT,
 "first_annotation_token" TEXT,
 "last_annotation_token" TEXT,
 source_record JSONB NOT NULL, PRIMARY KEY(snapshot_id,token)
);
DROP TRIGGER IF EXISTS immutable_record ON ods."instance";
CREATE TRIGGER immutable_record BEFORE INSERT OR UPDATE OR DELETE ON ods."instance" FOR EACH ROW EXECUTE FUNCTION ods.guard_record();
CREATE TABLE IF NOT EXISTS ods."log" (
 snapshot_id TEXT REFERENCES ods.snapshot,
 "token" TEXT,
 "logfile" TEXT,
 "vehicle" TEXT,
 "date_captured" TEXT,
 "location" TEXT,
 source_record JSONB NOT NULL, PRIMARY KEY(snapshot_id,token)
);
DROP TRIGGER IF EXISTS immutable_record ON ods."log";
CREATE TRIGGER immutable_record BEFORE INSERT OR UPDATE OR DELETE ON ods."log" FOR EACH ROW EXECUTE FUNCTION ods.guard_record();
CREATE TABLE IF NOT EXISTS ods."map" (
 snapshot_id TEXT REFERENCES ods.snapshot,
 "category" TEXT,
 "token" TEXT,
 "filename" TEXT,
 "log_tokens" JSONB,
 source_record JSONB NOT NULL, PRIMARY KEY(snapshot_id,token)
);
DROP TRIGGER IF EXISTS immutable_record ON ods."map";
CREATE TRIGGER immutable_record BEFORE INSERT OR UPDATE OR DELETE ON ods."map" FOR EACH ROW EXECUTE FUNCTION ods.guard_record();
CREATE TABLE IF NOT EXISTS ods."sample" (
 snapshot_id TEXT REFERENCES ods.snapshot,
 "token" TEXT,
 "timestamp" BIGINT,
 "prev" TEXT,
 "next" TEXT,
 "scene_token" TEXT,
 source_record JSONB NOT NULL, PRIMARY KEY(snapshot_id,token)
);
DROP TRIGGER IF EXISTS immutable_record ON ods."sample";
CREATE TRIGGER immutable_record BEFORE INSERT OR UPDATE OR DELETE ON ods."sample" FOR EACH ROW EXECUTE FUNCTION ods.guard_record();
CREATE TABLE IF NOT EXISTS ods."sample_annotation" (
 snapshot_id TEXT REFERENCES ods.snapshot,
 "token" TEXT,
 "sample_token" TEXT,
 "instance_token" TEXT,
 "visibility_token" TEXT,
 "attribute_tokens" JSONB,
 "translation" JSONB,
 "size" JSONB,
 "rotation" JSONB,
 "prev" TEXT,
 "next" TEXT,
 "num_lidar_pts" BIGINT,
 "num_radar_pts" BIGINT,
 source_record JSONB NOT NULL, PRIMARY KEY(snapshot_id,token)
);
DROP TRIGGER IF EXISTS immutable_record ON ods."sample_annotation";
CREATE TRIGGER immutable_record BEFORE INSERT OR UPDATE OR DELETE ON ods."sample_annotation" FOR EACH ROW EXECUTE FUNCTION ods.guard_record();
CREATE TABLE IF NOT EXISTS ods."sample_data" (
 snapshot_id TEXT REFERENCES ods.snapshot,
 "token" TEXT,
 "sample_token" TEXT,
 "ego_pose_token" TEXT,
 "calibrated_sensor_token" TEXT,
 "timestamp" BIGINT,
 "fileformat" TEXT,
 "is_key_frame" BOOLEAN,
 "height" BIGINT,
 "width" BIGINT,
 "filename" TEXT,
 "prev" TEXT,
 "next" TEXT,
 source_record JSONB NOT NULL, PRIMARY KEY(snapshot_id,token)
);
DROP TRIGGER IF EXISTS immutable_record ON ods."sample_data";
CREATE TRIGGER immutable_record BEFORE INSERT OR UPDATE OR DELETE ON ods."sample_data" FOR EACH ROW EXECUTE FUNCTION ods.guard_record();
CREATE TABLE IF NOT EXISTS ods."scene" (
 snapshot_id TEXT REFERENCES ods.snapshot,
 "token" TEXT,
 "log_token" TEXT,
 "nbr_samples" BIGINT,
 "first_sample_token" TEXT,
 "last_sample_token" TEXT,
 "name" TEXT,
 "description" TEXT,
 source_record JSONB NOT NULL, PRIMARY KEY(snapshot_id,token)
);
DROP TRIGGER IF EXISTS immutable_record ON ods."scene";
CREATE TRIGGER immutable_record BEFORE INSERT OR UPDATE OR DELETE ON ods."scene" FOR EACH ROW EXECUTE FUNCTION ods.guard_record();
CREATE TABLE IF NOT EXISTS ods."sensor" (
 snapshot_id TEXT REFERENCES ods.snapshot,
 "token" TEXT,
 "channel" TEXT,
 "modality" TEXT,
 source_record JSONB NOT NULL, PRIMARY KEY(snapshot_id,token)
);
DROP TRIGGER IF EXISTS immutable_record ON ods."sensor";
CREATE TRIGGER immutable_record BEFORE INSERT OR UPDATE OR DELETE ON ods."sensor" FOR EACH ROW EXECUTE FUNCTION ods.guard_record();
CREATE TABLE IF NOT EXISTS ods."visibility" (
 snapshot_id TEXT REFERENCES ods.snapshot,
 "description" TEXT,
 "token" TEXT,
 "level" TEXT,
 source_record JSONB NOT NULL, PRIMARY KEY(snapshot_id,token)
);
DROP TRIGGER IF EXISTS immutable_record ON ods."visibility";
CREATE TRIGGER immutable_record BEFORE INSERT OR UPDATE OR DELETE ON ods."visibility" FOR EACH ROW EXECUTE FUNCTION ods.guard_record();

-- TRUNCATE is a statement operation and bypasses row triggers; guard it explicitly.
CREATE OR REPLACE FUNCTION ods.guard_truncate() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF EXISTS(SELECT 1 FROM ods.snapshot WHERE status='READY') THEN
  RAISE EXCEPTION 'ready ODS snapshots cannot be truncated';
 END IF;
 RETURN NULL;
END $$;
DO $$ DECLARE t text; BEGIN
 FOREACH t IN ARRAY ARRAY['snapshot','scene','sample','sample_data','sensor','calibrated_sensor','ego_pose','sample_annotation','instance','category','log','map','attribute','visibility'] LOOP
  IF NOT EXISTS(SELECT 1 FROM pg_trigger WHERE tgrelid=format('ods.%I',t)::regclass AND tgname='immutable_truncate') THEN
   EXECUTE format('CREATE TRIGGER immutable_truncate BEFORE TRUNCATE ON ods.%I FOR EACH STATEMENT EXECUTE FUNCTION ods.guard_truncate()',t);
  END IF;
 END LOOP;
END $$;
