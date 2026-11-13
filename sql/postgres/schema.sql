-- 结构化业务基础表：由 ./platform.sh init-db 调用 publishing.store.initialize 执行。
-- control 保存任务、版本和发布状态；serving 保存按业务 generation 隔离的查询数据。
-- 本文件不创建向量表；向量能力由 migrations/001～003 及 Python 迁移入口建立。
-- IF NOT EXISTS 允许重复初始化，但不会把已有表自动升级成这里的新定义。
CREATE SCHEMA IF NOT EXISTS control;
CREATE SCHEMA IF NOT EXISTS serving;
-- 逻辑装载任务：当前调用方以完成清单的 SHA-256 作为 task_id，重跑复用任务。
CREATE TABLE IF NOT EXISTS control.task (
 task_id text PRIMARY KEY, created_at timestamptz NOT NULL DEFAULT now()
);
-- 一次实际装载尝试：同一 task 可有多次 attempt，分别保留结果及失败信息。
-- UNKNOWN 表示结果尚未确认；状态枚举不等于数据库自动实现了状态流转。
-- created_at/finished_at 使用带时区时间，error_code、finished_at 可为空。
CREATE TABLE IF NOT EXISTS control.attempt (
 attempt_id text PRIMARY KEY, task_id text NOT NULL REFERENCES control.task,
 status text NOT NULL CHECK (status IN ('RUNNING','SUCCEEDED','FAILED','UNKNOWN')),
 error_code text, created_at timestamptz NOT NULL DEFAULT now(), finished_at timestamptz
);
-- 业务版本登记：generation 由调用方构造为 g_ + 完成清单摘要。
-- snapshot_id 关联 ODS 输入，processing_version 标识加工逻辑版本。
-- manifest 保存产物契约及清单；quality 保存输入质量报告，供 API 展示。
-- LOADING 允许装载；READY 后由下方触发器冻结版本及其服务行。
-- manifest_sha256 唯一防止同一产物重复登记；SQL 本身不重新计算 JSON 摘要。
CREATE TABLE IF NOT EXISTS control.generation (
 generation text PRIMARY KEY, snapshot_id text NOT NULL, processing_version text NOT NULL,
 status text NOT NULL CHECK (status IN ('LOADING','READY')),
 manifest_sha256 text NOT NULL UNIQUE, manifest jsonb NOT NULL, quality jsonb NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now()
);
-- 全局活动业务版本指针：CHECK(singleton) 只允许 true，主键保证至多一行。
-- 尚未发布时 generation 为 NULL；初始化插入 true，重复执行不重置指针。
-- revision 是发布并发控制的修订号；预期版本比较、递增由 Python 发布事务执行。
CREATE TABLE IF NOT EXISTS control.active (
 singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton), generation text REFERENCES control.generation,
 revision bigint NOT NULL DEFAULT 0
);
INSERT INTO control.active(singleton) VALUES(true) ON CONFLICT DO NOTHING;
-- 发布/回滚事件：保留切换前后版本及修订号；首次发布的 previous_generation 可空。
-- rollback 也是切换指针并递增 revision，不修改或重新生成历史业务数据。
-- READY 检查、指针更新与事件插入的原子性由 publishing.store.publish 保证。
CREATE TABLE IF NOT EXISTS control.publication (
 event_id text PRIMARY KEY, previous_generation text REFERENCES control.generation,
 generation text NOT NULL REFERENCES control.generation, revision bigint NOT NULL UNIQUE,
 action text NOT NULL CHECK(action IN ('publish','rollback')), created_at timestamptz NOT NULL DEFAULT now()
);
-- 场景上下文。服务表的复合主键让同一源 entity_id 可出现在多个 generation 中。
-- payload 为该行的业务 JSON；常用关联/筛选字段另存为关系列。
-- 主键包含的列会被 PostgreSQL 隐式设为 NOT NULL。
CREATE TABLE IF NOT EXISTS serving.scene (
 generation text REFERENCES control.generation, entity_id text, payload jsonb NOT NULL,
 PRIMARY KEY(generation, entity_id)
);
-- 关键帧：主要筛选粒度。timestamp_us 为 UTC 时间轴上的微秒整数。
-- (generation, scene_id) 复合外键确保帧只能关联同一版本的场景。
CREATE TABLE IF NOT EXISTS serving.frame (
 generation text REFERENCES control.generation, entity_id text, timestamp_us bigint NOT NULL,
 scene_id text NOT NULL, location text NOT NULL, payload jsonb NOT NULL,
 PRIMARY KEY(generation, entity_id), FOREIGN KEY(generation, scene_id) REFERENCES serving.scene
);
-- 匹配按版本、时间、实体 ID 的稳定排序；entity_id 用于相同时间的顺序裁决。
CREATE INDEX IF NOT EXISTS frame_order ON serving.frame(generation,timestamp_us,entity_id);
-- 媒体登记，不保存媒体二进制。payload 包含仓库外文件路径、摘要等元数据。
-- channel 是传感器通道，modality 是 camera/lidar 等类型；此处没有枚举 CHECK。
-- is_key_frame 区分关键帧媒体；复合外键禁止关联其他业务版本的帧。
CREATE TABLE IF NOT EXISTS serving.media (
 generation text REFERENCES control.generation, entity_id text, frame_id text NOT NULL,
 is_key_frame boolean NOT NULL, channel text NOT NULL, modality text NOT NULL,
 payload jsonb NOT NULL, PRIMARY KEY(generation,entity_id),
 FOREIGN KEY(generation,frame_id) REFERENCES serving.frame
);
-- 支持按版本、所属帧及是否关键帧查询媒体列表。
CREATE INDEX IF NOT EXISTS media_frame ON serving.media(generation,frame_id,is_key_frame);
-- 标注明细：category 支持类别筛选，payload 保留具体标注属性。
CREATE TABLE IF NOT EXISTS serving.annotation (
 generation text REFERENCES control.generation, entity_id text, frame_id text NOT NULL,
 category text NOT NULL, payload jsonb NOT NULL, PRIMARY KEY(generation,entity_id),
 FOREIGN KEY(generation,frame_id) REFERENCES serving.frame
);
CREATE INDEX IF NOT EXISTS annotation_frame ON serving.annotation(generation,frame_id);
-- 帧与类别的去重关系，供“该帧是否包含精确类别”的 EXISTS 条件使用。
-- 一帧同类多个标注只形成一条关系；标注数量仍应从明细或统计读取。
CREATE TABLE IF NOT EXISTS serving.frame_category (
 generation text, frame_id text, category text,
 PRIMARY KEY(generation,frame_id,category), FOREIGN KEY(generation,frame_id) REFERENCES serving.frame
);
-- 帧与已校验关键帧通道的去重关系，供多个必需通道取交集筛选。
-- “多个通道全部满足”由查询层组合条件实现，不由单个外键保证。
CREATE TABLE IF NOT EXISTS serving.frame_channel (
 generation text, frame_id text, channel text,
 PRIMARY KEY(generation,frame_id,channel), FOREIGN KEY(generation,frame_id) REFERENCES serving.frame
);
-- 预计算分布统计：dimension 为统计维度，value 为该维度的分组值。
-- payload 保存统计结果；避免在线查询反复执行整个离线聚合。
CREATE TABLE IF NOT EXISTS serving.statistics (
 generation text REFERENCES control.generation, dimension text, value text, payload jsonb NOT NULL,
 PRIMARY KEY(generation,dimension,value)
);
-- 通用维度字典：kind 区分类别、地点、传感器等，identity 是维度内身份。
CREATE TABLE IF NOT EXISTS serving.dimension (
 generation text REFERENCES control.generation, kind text, identity text, payload jsonb NOT NULL,
 PRIMARY KEY(generation,kind,identity)
);
-- 服务行写保护：八张结构化 serving 表共同使用的行级触发器函数。
-- DELETE 读取 OLD；INSERT/UPDATE 读取 NEW，防止访问不适用的行记录。
-- IS DISTINCT FROM 会把“版本不存在，查询结果为 NULL”也判为非 LOADING。
-- UPDATE 额外禁止把行从一个版本搬到另一个版本。
-- 这是普通行级 DML 的保护，不覆盖 TRUNCATE、禁用触发器等管理操作。
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
-- 为各表安装 BEFORE 行级触发器：不满足条件时抛异常，当前语句失败。
-- format 的 %I 按 SQL 标识符引用表名/触发器名，不作为字符串值拼接。
-- 当前存在性判断按 pg_trigger 中的名称检查，默认这些名称由项目独占。
DO $$ DECLARE t text; BEGIN
 FOREACH t IN ARRAY ARRAY['scene','frame','media','annotation','frame_category','frame_channel','statistics','dimension'] LOOP
  IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='immutable_'||t) THEN
   EXECUTE format('CREATE TRIGGER %I BEFORE INSERT OR UPDATE OR DELETE ON serving.%I FOR EACH ROW EXECUTE FUNCTION control.guard_service()', 'immutable_'||t, t);
  END IF;
 END LOOP;
END $$;
-- 版本元数据冻结：只要操作前已经 READY，就拒绝 UPDATE/DELETE。
-- LOADING -> READY 允许通过；本函数不负责核对装载完整性或强制初始状态，
-- 这些检查由装载代码完成。下方服务行触发器负责冻结版本内的数据。
CREATE OR REPLACE FUNCTION control.guard_generation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF OLD.status='READY' THEN RAISE EXCEPTION 'ready generation is immutable'; END IF;
 IF TG_OP='DELETE' THEN RETURN OLD; END IF;
 RETURN NEW;
END $$;
-- 重复初始化时复用已有触发器；CREATE OR REPLACE FUNCTION 更新其函数定义。
DO $$ BEGIN
 IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='immutable_generation') THEN
  CREATE TRIGGER immutable_generation BEFORE UPDATE OR DELETE ON control.generation FOR EACH ROW EXECUTE FUNCTION control.guard_generation();
 END IF;
END $$;
