"""正式向量部署：只读快照、独立恢复演练、身份登记与维护窗口升级。"""

import hashlib
import json
import os
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

import psycopg
from embedding_search_resources import inventory
from verify_backup import TABLES, fingerprint
from verify_embedding_search import dump, restore

from driving_data_platform.common.config import port
from driving_data_platform.common.errors import PlatformError, describe
from driving_data_platform.embedding import artifacts as a
from driving_data_platform.embedding import deployment as d
from driving_data_platform.embedding import store
from driving_data_platform.ingestion.nuscenes import atomic_json
from driving_data_platform.warehouse.catalog import command

SERVICE = "driving-data-platform-mac-postgres-1"


def configured_artifacts(root):
    from driving_data_platform.common.config import application_config
    from driving_data_platform.common.run_query import identity

    value = (
        application_config(root).get("embedding", {}).get("deployment_artifacts", {})
    )
    if not isinstance(value, dict) or set(value) != {"main", "auxiliary"}:
        raise ValueError(
            "embedding.deployment_artifacts requires explicit main and auxiliary artifact IDs"
        )
    for artifact in value.values():
        identity(artifact, r"[a-f0-9]{64}")
    if value["main"] == value["auxiliary"]:
        raise ValueError("主/辅助产物必须不同，以便真实发布与回滚验证")
    return value


def run(*args, **kwargs):
    return subprocess.run(list(args), check=True, capture_output=True, **kwargs)


def fields(root):
    return dict(
        line.split("=", 1) for line in (root / "compose.env").read_text().splitlines()
    )


def raw_connect(root, state):
    return psycopg.connect(
        host=state["host"],
        port=state["port"],
        user="ddp",
        dbname=state["database"],
        password=fields(root)["DDP_POSTGRES_PASSWORD"],
        connect_timeout=5,
        autocommit=True,
    )


def source_connect(root):
    conn = raw_connect(
        root,
        {
            "host": "127.0.0.1",
            "port": port("postgres"),
            "database": "ddp",
            "user": "ddp",
        },
    )
    # 导出快照必须由持续打开的事务持有，不能使用自动提交连接。
    conn.autocommit = False
    return conn


def register(root, state):
    if "artifacts" not in state:
        state["artifacts"] = configured_artifacts(root)
    with raw_connect(root, state) as conn:
        _, state["database_oid"], state["system_identifier"] = d.database_identity(conn)
        store.migrate_deployment(conn, Path(__file__).resolve().parents[1], state)
        store.migrate_deployment(conn, Path(__file__).resolve().parents[1], state)
    folder = root / "embedding/deployments"
    folder.mkdir(exist_ok=True)
    path = folder / (state["deployment_id"] + ".json")
    if path.exists():
        raise ValueError("部署登记已经存在，拒绝覆盖")
    atomic_json(path, state)
    return state


def source_info(root):
    info = json.loads(run("docker", "inspect", SERVICE).stdout)[0]
    labels = info["Config"]["Labels"]
    if (
        labels.get("com.docker.compose.project") != "driving-data-platform-mac"
        or labels.get("com.docker.compose.service") != "postgres"
    ):
        raise ValueError("正式容器标签不一致")
    if info["HostConfig"]["PortBindings"].get("5432/tcp") != [
        {"HostIp": "127.0.0.1", "HostPort": (str(port("postgres")))}
    ]:
        raise ValueError("正式数据库端口不一致")
    volumes = [
        m["Name"]
        for m in info["Mounts"]
        if m["Destination"] == "/var/lib/postgresql/data" and m["Type"] == "volume"
    ]
    if volumes != ["driving-data-platform-mac_postgres-data"]:
        raise ValueError("正式数据卷身份不一致")
    return info, volumes[0]


def validate_vectors(root, conn, artifact):
    _, source, contract, rows = a.verify_artifact(root, artifact)
    if (
        contract["test_only"]
        or len(rows) != 2424
        or len({x["frame_id"] for x in source["entries"]}) != 404
    ):
        raise ValueError("真实产物空间或覆盖范围不一致")
    if a.media_input(conn, source["generation"]) != source:
        raise ValueError("数据库与产物绑定的业务清单不一致")
    receipt = store.load(root, conn, artifact)
    eg = receipt["embedding_generation"]
    actual = conn.execute(
        "SELECT media_id,image_sha256,embedding::text FROM serving.image_embedding WHERE embedding_generation=%s ORDER BY media_id",
        (eg,),
    ).fetchall()
    expected = [(r["media_id"], r["sha256"], r["vector"]) for r in rows]
    if expected != [
        (mid, sha, a.vector32(json.loads(vector), contract["dimension"]))
        for mid, sha, vector in actual
    ]:
        raise ValueError("真实向量逐值回读不一致")
    return eg, source


def rehearsal(root, repo):
    artifacts = configured_artifacts(root)
    MAIN, AUX = artifacts["main"], artifacts["auxiliary"]
    identifier = uuid.uuid4().hex
    folder = root / "reports" / ("embedding-deploy-rehearsal-" + identifier)
    folder.mkdir()
    report = {
        "status": "running",
        "deployment_id": identifier,
        "artifacts": artifacts,
        "checks": [],
        "folder": str(folder),
    }
    state = None
    started = False
    try:
        report["resources_before"] = inventory(root, repo, 2 * 1024**3)
        if report["resources_before"]["status"] != "passed":
            raise PlatformError(
                "RESOURCE_BLOCKED", "deploy-budget", "部署恢复演练资源预算不足"
            )
        info, _ = source_info(root)
        started = not info["State"]["Running"]
        if started:
            run(
                *command(root, repo),
                "up",
                "-d",
                "--wait",
                "--pull",
                "never",
                "postgres",
            )
        image = a.read(repo, "infra/embedding/runtime.lock.json")["image"]
        old_image = a.read(repo, "infra/docker/versions.lock.json")["POSTGRES"]["image"]
        expected_image = json.loads(
            run("docker", "image", "inspect", old_image).stdout
        )[0]["Id"]
        if info["Image"] != expected_image:
            raise ValueError("正式容器镜像与现有锁不一致")
        with source_connect(root) as conn:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            snapshot = conn.execute("SELECT pg_export_snapshot()").fetchone()[0]
            report["source_fingerprint"] = fingerprint(conn)
            report["source_extensions"] = conn.execute(
                "SELECT extname,extversion FROM pg_extension ORDER BY extname"
            ).fetchall()
            if any(
                k.startswith("control.embedding") for k in report["source_fingerprint"]
            ):
                raise ValueError("已有正式向量部署须走独立升级流程，禁止覆盖")
            report["backup"] = dump(
                ["docker", "exec", "-i", SERVICE],
                "ddp",
                folder / "service.dump",
                snapshot,
            )
        state = {
            "deployment_id": identifier,
            "kind": "rehearsal",
            "database": "ddp_deploy_" + identifier,
            "host": "127.0.0.1",
            "port": port("restore"),
            "user": "ddp",
            "container": "ddp-deploy-" + identifier,
            "volume": "ddp-deploy-" + identifier,
            "image": image,
        }
        env = folder / "container.env"
        env.write_text(
            "POSTGRES_USER=ddp\nPOSTGRES_DB="
            + state["database"]
            + "\nPOSTGRES_PASSWORD="
            + fields(root)["DDP_POSTGRES_PASSWORD"]
            + "\n"
        )
        env.chmod(0o600)
        run(
            "docker",
            "run",
            "-d",
            "--pull",
            "never",
            "--name",
            state["container"],
            "--label",
            "ddp.embedding.deployment=" + identifier,
            "--memory",
            "1g",
            "--cpus",
            "1",
            "--env-file",
            str(env),
            "--publish",
            ("127.0.0.1:" + str(port("restore")) + ":5432"),
            "--volume",
            state["volume"] + ":/var/lib/postgresql/data",
            image,
            "-c",
            "max_wal_size=128MB",
            "-c",
            "min_wal_size=32MB",
        )
        for _ in range(60):
            try:
                with raw_connect(root, state):
                    pass
                break
            except psycopg.OperationalError:
                time.sleep(0.5)
        else:
            raise ValueError("恢复演练数据库启动超时")
        restore(
            ["docker", "exec", "-i", state["container"]],
            state["database"],
            folder / "service.dump",
        )
        with raw_connect(root, state) as conn:
            if fingerprint(conn) != report["source_fingerprint"]:
                raise ValueError("业务备份恢复指纹不一致")
        register(root, state)
        d.container(state)
        with store.connect(root, deployment=identifier) as conn:
            eg, source = validate_vectors(root, conn, MAIN)
            if not store.load(root, conn, MAIN)["reused"]:
                raise ValueError("重复装载未复用")
            aux, _ = validate_vectors(root, conn, AUX)
            store.publish(conn, eg, 0)
            store.publish(conn, aux, 1)
            store.publish(conn, eg, 2, rollback=True)
            actual_fingerprint = fingerprint(conn)
            for key, value in report["source_fingerprint"].items():
                if actual_fingerprint[key] != value:
                    raise ValueError("业务指纹改变")
            try:
                store.check_environment(conn, True)
            except ValueError:
                pass
            else:
                raise ValueError("未拒绝人工测试空间")
            with conn.transaction(force_rollback=True):
                conn.execute(
                    "UPDATE control.embedding_deployment SET system_identifier='invalid'"
                )
                try:
                    with conn.transaction():
                        conn.execute(
                            "INSERT INTO control.embedding_active(generation) VALUES(%s) ON CONFLICT DO NOTHING",
                            (source["generation"],),
                        )
                except psycopg.Error:
                    pass
                else:
                    raise ValueError("SQL未拒绝错误部署身份")
            report["deployed_fingerprint"] = fingerprint(conn)
            report["generation"] = source["generation"]
            report["space_id"] = a.validate_space(a.verify_artifact(root, MAIN)[2])
        report["checks"] = [
            "一致性快照完整备份及恢复业务指纹",
            "001摘要保持和002迁移幂等",
            "数据库与本地/集群/容器双重身份",
            "主辅助2424条向量逐值回读和重复复用",
            "真实版本发布与回滚",
            "Python及SQL拒绝错误环境与人工测试空间",
        ]
        with source_connect(root) as conn:
            if fingerprint(conn) != report["source_fingerprint"]:
                raise ValueError("正式业务库被改变")
        report["status"] = "passed"
    except BaseException as error:
        report["status"] = "failed"
        report["error"] = describe(error, "deploy-rehearsal")
        raise
    finally:
        if state:
            existing = subprocess.run(
                ["docker", "inspect", state["container"]],
                capture_output=True,
                check=False,
            )
            if existing.returncode == 0:
                run("docker", "stop", state["container"])
        if started:
            run(*command(root, repo), "stop", "postgres")
        report["resources_after"] = inventory(root, repo)
        if report["resources_after"]["status"] != "passed":
            report["status"] = "failed"
        atomic_json(folder / "report.json", report)
    return report


def verify_http(root, repo, deployment_id, folder):
    """在真实目标数据库上编码固定中文，并逐项对照独立余弦基准。"""
    from urllib.request import Request, urlopen

    import api_process
    from verify_embedding_search import baseline

    from driving_data_platform.embedding import runtime as model

    if api_process.status(root)["running"]:
        raise ValueError(
            "部署验收要求" + str(port("api")) + " API原本停止，不接管已有进程"
        )
    artifacts = d.registration(root, deployment_id).get(
        "artifacts"
    ) or configured_artifacts(root)
    MAIN = artifacts["main"]
    mr = ar = None
    result = []
    try:
        _, source, contract, rows = a.verify_artifact(root, MAIN)
        mr = model.up(root, a.validate_space(contract))
        ar = api_process.start(root, repo, deployment=deployment_id)
        queries = [
            q["text"]
            for q in a.read(root, "embedding/evaluation-labels.json")["queries"]
            if q["positive_media_ids"]
        ]
        for text in queries:
            body = {"generation": source["generation"], "text": text, "limit": 20}
            request = Request(
                ("http://127.0.0.1:" + str(port("api")) + "/api/embedding/search"),
                data=json.dumps(body).encode(),
                headers={"Content-Type": "application/json"},
            )
            with urlopen(request, timeout=70) as response:
                value = json.load(response)
            vector = model.encoder(root, a.validate_space(contract)).encode_text(text)
            conditions = {
                "location": None,
                "start_us": None,
                "end_us": None,
                "category": None,
                "channels": [],
            }
            with store.connect(root, deployment=deployment_id) as conn:
                expected = baseline(conn, source, rows, vector, conditions, None, 20)
            if [x["media_id"] for x in value["items"]] != [x[0] for x in expected]:
                raise ValueError("正式检索独立排序不一致")
            if any(
                abs(x["score"] - score) > 1e-5
                for x, (_, score) in zip(value["items"], expected, strict=True)
            ):
                raise ValueError("正式检索独立分数不一致")
            item = value["items"][0]
            with urlopen(
                ("http://127.0.0.1:" + str(port("api"))) + item["media_url"], timeout=10
            ) as response:
                media = response.read()
            entry = next(
                x for x in source["entries"] if x["media_id"] == item["media_id"]
            )
            if hashlib.sha256(media).hexdigest() != entry["sha256"]:
                raise ValueError("正式同源媒体摘要不一致")
            result.append(
                {
                    "query_sha256": hashlib.sha256(text.encode()).hexdigest(),
                    "count": len(value["items"]),
                    "timings": value["timings"],
                }
            )
        run(
            os.sys.executable,
            "scripts/verify_api.py",
            env=dict(
                os.environ,
                DDP_POSTGRES_DSN=api_dsn(root, d.registration(root, deployment_id)),
            ),
        )
        atomic_json(folder / "http.json", {"status": "passed", "queries": result})
        return result
    finally:
        try:
            if ar and not ar["reused"]:
                api_process.stop(root)
        finally:
            if mr and not mr["reused"]:
                model.stop(root)


def api_dsn(root, state):
    from psycopg.conninfo import make_conninfo

    return make_conninfo(
        host=state["host"],
        port=state["port"],
        dbname=state["database"],
        user=state["user"],
        password=fields(root)["DDP_POSTGRES_PASSWORD"],
    )


def restore_rehearsal(root, state, backup, expected, source_state):
    """只重建登记的恢复演练数据库；指纹匹配后显式重新绑定部署身份。"""
    from psycopg import sql

    d.container(state)
    with psycopg.connect(
        api_dsn(root, {**state, "database": "postgres"}), autocommit=True
    ) as conn:
        if conn.execute(
            "SELECT count(*) FROM pg_stat_activity WHERE datname=%s",
            (state["database"],),
        ).fetchone() != (0,):
            raise ValueError("恢复演练数据库存在其他连接")
        conn.execute(
            sql.SQL("DROP DATABASE {}").format(sql.Identifier(state["database"]))
        )
        conn.execute(
            sql.SQL("CREATE DATABASE {}").format(sql.Identifier(state["database"]))
        )
    restore(["docker", "exec", "-i", state["container"]], state["database"], backup)
    with raw_connect(root, state) as conn:
        if fingerprint(conn) != expected:
            raise ValueError("含向量备份恢复指纹不一致，禁止重新登记")
        previous = conn.execute(
            "SELECT deployment_id,kind,database_name,database_oid,system_identifier FROM control.embedding_deployment WHERE singleton"
        ).fetchone()
        if previous != (
            source_state["deployment_id"],
            source_state["kind"],
            source_state["database"],
            source_state["database_oid"],
            source_state["system_identifier"],
        ):
            raise ValueError("恢复来源环境与指定源不一致")
        _, oid, system = d.database_identity(conn)
        new = {**state, "database_oid": oid, "system_identifier": system}
        with conn.transaction():
            conn.execute(
                "UPDATE control.embedding_environment SET database_name=%s,test_only_allowed=false WHERE singleton",
                (new["database"],),
            )
            conn.execute(
                "UPDATE control.embedding_deployment SET deployment_id=%s,kind=%s,database_name=%s,database_oid=%s,system_identifier=%s WHERE singleton",
                (new["deployment_id"], new["kind"], new["database"], oid, system),
            )
            d.validate_database(conn, new)
    atomic_json(
        root / "embedding/deployments" / (state["deployment_id"] + ".json"), new
    )
    return new


def verify_rehearsal(root, repo, identifier):
    report_id = uuid.uuid4().hex
    folder = root / "reports" / ("embedding-deploy-verify-" + report_id)
    folder.mkdir()
    report = {"status": "running", "deployment_id": identifier, "folder": str(folder)}
    report["resources_before"] = inventory(root, repo, 1024**3)
    if report["resources_before"]["status"] != "passed":
        report["status"] = "blocked"
        atomic_json(folder / "report.json", report)
        raise PlatformError(
            "RESOURCE_BLOCKED", "deploy-restore-budget", "恢复验证预算不足"
        )
    state = d.registration(root, identifier)
    if state["kind"] != "rehearsal":
        raise ValueError("此验证入口只操作恢复演练环境")
    d.container(state, running=False)
    run("docker", "start", state["container"])
    try:
        for _ in range(60):
            try:
                with raw_connect(root, state):
                    pass
                break
            except psycopg.OperationalError:
                time.sleep(0.5)
        report["http"] = verify_http(root, repo, identifier, folder)
        with store.connect(root, deployment=identifier) as conn:
            conn.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
            snapshot = conn.execute("SELECT pg_export_snapshot()").fetchone()[0]
            expected = fingerprint(conn)
            report["backup"] = dump(
                ["docker", "exec", "-i", state["container"]],
                state["database"],
                folder / "vectors.dump",
                snapshot,
            )
            conn.execute("ROLLBACK")
        state = restore_rehearsal(root, state, folder / "vectors.dump", expected, state)
        run("docker", "restart", state["container"])
        for _ in range(60):
            try:
                with store.connect(root, deployment=identifier) as conn:
                    if fingerprint(conn) != (
                        {
                            **expected,
                            "control.embedding_deployment": fingerprint(conn)[
                                "control.embedding_deployment"
                            ],
                        }
                    ):
                        raise ValueError("重启持久化指纹不一致")
                break
            except psycopg.OperationalError:
                time.sleep(0.5)
        else:
            raise ValueError("重启后数据库不可用")
        report["http_after_restore"] = verify_http(root, repo, identifier, folder)
        report["status"] = "passed"
    except BaseException as error:
        report["status"] = "failed"
        report["error"] = describe(error, "deploy-verify")
        raise
    finally:
        run("docker", "stop", state["container"])
        report["resources_after"] = inventory(root, repo)
        if report["resources_after"]["status"] != "passed":
            report["status"] = "failed"
        atomic_json(folder / "report.json", report)
    return report


def passed_report(root, prefix, identifier):
    reports = []
    for path in (root / "reports").glob(prefix + "*/report.json"):
        value = json.loads(path.read_text())
        if value.get("status") == "passed" and value.get("deployment_id") == identifier:
            reports.append((path.stat().st_mtime, value))
    if not reports:
        raise ValueError("缺少该部署身份已通过的独立演练报告")
    return max(reports, key=lambda x: x[0])[1]


def set_image(root, image):
    path = root / "compose.env"
    lines = path.read_text().splitlines()
    updated = [
        "DDP_POSTGRES_IMAGE=" + image
        if line.startswith("DDP_POSTGRES_IMAGE=")
        else line
        for line in lines
    ]
    if sum(line.startswith("DDP_POSTGRES_IMAGE=") for line in lines) != 1:
        raise ValueError("正式镜像配置缺失或重复")
    with tempfile.NamedTemporaryFile(
        mode="w", dir=root, prefix=".compose-deploy-", delete=False
    ) as stream:
        stream.write("\n".join(updated) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
        temporary = stream.name
    os.replace(temporary, path)
    path.chmod(0o600)


def restore_business_rehearsal(root, state, backup, expected):
    """在正式升级前实际恢复本轮最终业务备份，保留演练卷和原备份。"""
    from psycopg import sql

    with psycopg.connect(
        api_dsn(root, {**state, "database": "postgres"}), autocommit=True
    ) as conn:
        if conn.execute(
            "SELECT count(*) FROM pg_stat_activity WHERE datname=%s",
            (state["database"],),
        ).fetchone() != (0,):
            raise ValueError("恢复目标存在其他连接")
        conn.execute(
            sql.SQL("DROP DATABASE {}").format(sql.Identifier(state["database"]))
        )
        conn.execute(
            sql.SQL("CREATE DATABASE {}").format(sql.Identifier(state["database"]))
        )
    restore(["docker", "exec", "-i", state["container"]], state["database"], backup)
    with raw_connect(root, state) as conn:
        if fingerprint(conn) != expected:
            raise ValueError("本轮正式业务备份实际恢复不一致")
        _, oid, system = d.database_identity(conn)
        state = {**state, "database_oid": oid, "system_identifier": system}
        store.migrate_deployment(conn, Path(__file__).resolve().parents[1], state)
    atomic_json(
        root / "embedding/deployments" / (state["deployment_id"] + ".json"), state
    )
    return state


def apply(root, repo, rehearsal_id):
    """仅在恢复及HTTP演练通过后升级正式实例；维护期间保留所有备份。"""
    import api_process

    initial = passed_report(root, "embedding-deploy-rehearsal-", rehearsal_id)
    artifacts = initial.get("artifacts")
    if not artifacts:
        raise ValueError("旧演练报告缺少显式产物身份，请重新执行演练")
    MAIN, AUX = artifacts["main"], artifacts["auxiliary"]
    passed_report(root, "embedding-deploy-verify-", rehearsal_id)
    if api_process.status(root)["running"]:
        raise ValueError("正式部署需要API停止的维护窗口")
    if (root / "embedding/production.json").exists() and a.read(
        root, "embedding/production.json"
    ).get("enabled"):
        raise ValueError("已有正式部署，禁止重复首次升级")
    identifier = uuid.uuid4().hex
    folder = root / "reports" / ("embedding-deploy-apply-" + identifier)
    folder.mkdir()
    report = {
        "status": "running",
        "deployment_id": identifier,
        "rehearsal_id": rehearsal_id,
        "artifacts": artifacts,
        "folder": str(folder),
    }
    state = None
    started = False
    upgraded = False
    target_started = False
    target = d.registration(root, rehearsal_id)
    try:
        report["resources_before"] = inventory(root, repo, 1024**3)
        if report["resources_before"]["status"] != "passed":
            raise PlatformError(
                "RESOURCE_BLOCKED", "formal-deploy-budget", "正式升级增长预算不足"
            )
        info, volume = source_info(root)
        started = not info["State"]["Running"]
        original_image = a.read(repo, "infra/docker/versions.lock.json")["POSTGRES"][
            "image"
        ]
        if (
            info["Image"]
            != json.loads(run("docker", "image", "inspect", original_image).stdout)[0][
                "Id"
            ]
        ):
            raise ValueError("正式容器镜像与当前锁不一致")
        if started:
            run(
                *command(root, repo),
                "up",
                "-d",
                "--wait",
                "--pull",
                "never",
                "postgres",
            )
        report["initial_postgres_running"] = not started
        with source_connect(root) as conn:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            snapshot = conn.execute("SELECT pg_export_snapshot()").fetchone()[0]
            expected = fingerprint(conn)
            if expected != initial["source_fingerprint"]:
                raise ValueError("正式业务数据自恢复演练以来已改变，须重新演练")
            report["source_fingerprint"] = expected
            report["source_extensions"] = conn.execute(
                "SELECT extname,extversion FROM pg_extension ORDER BY extname"
            ).fetchall()
            report["backup_before"] = dump(
                ["docker", "exec", "-i", SERVICE],
                "ddp",
                folder / "before.dump",
                snapshot,
            )
            source_identity = d.database_identity(conn)
        (folder / "compose.env.before").write_bytes((root / "compose.env").read_bytes())
        (folder / "compose.env.before").chmod(0o600)
        (folder / "versions.lock.before.json").write_bytes(
            (repo / "infra/docker/versions.lock.json").read_bytes()
        )
        d.container(target, running=False)
        run("docker", "start", target["container"])
        target_started = True
        for _ in range(60):
            try:
                with raw_connect(root, target):
                    pass
                break
            except psycopg.OperationalError:
                time.sleep(0.5)
        else:
            raise ValueError("本轮备份恢复目标启动失败")
        with store.connect(root, deployment=rehearsal_id) as conn:
            expected_migrations = [
                (
                    number,
                    hashlib.sha256(
                        (repo / "sql/postgres/migrations" / name).read_bytes()
                    ).hexdigest(),
                )
                for number, name in (
                    (1, "001_embedding.sql"),
                    (2, "002_embedding_deployment.sql"),
                    (3, "003_embedding_deployment_guard.sql"),
                )
            ]
            if (
                conn.execute(
                    "SELECT version,sha256 FROM control.embedding_migration ORDER BY version"
                ).fetchall()
                != expected_migrations
            ):
                raise ValueError("恢复演练迁移摘要与当前部署代码不一致")
        report["rehearsal_backup"] = dump(
            ["docker", "exec", "-i", target["container"]],
            target["database"],
            folder / "rehearsal-before.dump",
        )
        target = restore_business_rehearsal(
            root, target, folder / "before.dump", expected
        )
        report["before_backup_restore_verified"] = True
        run("docker", "stop", target["container"])
        target_started = False
        image = a.read(repo, "infra/embedding/runtime.lock.json")["image"]
        set_image(root, image)
        run(
            *command(root, repo),
            "up",
            "-d",
            "--wait",
            "--pull",
            "never",
            "--no-deps",
            "postgres",
        )
        upgraded = True
        state = {
            "deployment_id": identifier,
            "kind": "production",
            "database": "ddp",
            "host": "127.0.0.1",
            "port": port("postgres"),
            "user": "ddp",
            "container": SERVICE,
            "volume": volume,
            "image": image,
            "database_oid": source_identity[1],
            "system_identifier": source_identity[2],
        }
        d.container(state)
        with raw_connect(root, state) as conn:
            d.validate_database(conn, state, registered=False)
            if fingerprint(conn) != expected:
                raise ValueError("镜像升级后业务指纹变化")
        state["artifacts"] = artifacts
        register(root, state)
        lock = a.read(repo, "infra/docker/versions.lock.json")
        lock["POSTGRES"] = {
            "tag": "ddp-pgvector:pg16.15-vector0.8.7",
            "image": image,
            "architecture": "arm64",
            "size_bytes": json.loads(run("docker", "image", "inspect", image).stdout)[
                0
            ]["Size"],
        }
        atomic_json(repo / "infra/docker/versions.lock.json", lock)
        with store.connect(root, deployment=identifier) as conn:
            eg, source = validate_vectors(root, conn, MAIN)
            aux, _ = validate_vectors(root, conn, AUX)
            store.publish(conn, eg, 0)
            store.publish(conn, aux, 1)
            store.publish(conn, eg, 2, rollback=True)
            actual = fingerprint(conn)
            if {k: actual[k] for k in TABLES} != expected:
                raise ValueError("正式装载改变业务表")
            report["deployed_fingerprint"] = actual
            report["generation"] = source["generation"]
            report["artifact_id"] = MAIN
            conn.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
            snapshot = conn.execute("SELECT pg_export_snapshot()").fetchone()[0]
            report["backup_after"] = dump(
                ["docker", "exec", "-i", SERVICE],
                "ddp",
                folder / "vectors.dump",
                snapshot,
            )
            conn.execute("ROLLBACK")
        d.container(target, running=False)
        run("docker", "start", target["container"])
        target_started = True
        for _ in range(60):
            try:
                with raw_connect(root, target):
                    pass
                break
            except psycopg.OperationalError:
                time.sleep(0.5)
        else:
            raise ValueError("独立恢复目标启动失败")
        target = restore_rehearsal(
            root, target, folder / "vectors.dump", report["deployed_fingerprint"], state
        )
        report["restored_http"] = verify_http(root, repo, rehearsal_id, folder)
        run("docker", "stop", target["container"])
        target_started = False
        report["formal_http"] = verify_http(root, repo, identifier, folder)
        with store.connect(root, deployment=identifier) as conn:
            if fingerprint(conn) != report["deployed_fingerprint"]:
                raise ValueError("正式读验收后指纹变化")
        atomic_json(
            root / "embedding/production.json",
            {
                "enabled": True,
                "deployment_id": identifier,
                "report": str(folder / "report.json"),
            },
        )
        ar = api_process.start(root, repo)
        try:
            if ar.get("deployment") != identifier:
                raise ValueError("默认API未绑定正式登记")
        finally:
            if not ar["reused"]:
                api_process.stop(root)
        report["status"] = "passed"
    except BaseException as error:
        report["status"] = "failed"
        report["error"] = describe(error, "formal-deploy")
        report["upgraded"] = upgraded
        if not upgraded and (folder / "compose.env.before").exists():
            (root / "compose.env").write_bytes(
                (folder / "compose.env.before").read_bytes()
            )
        raise
    finally:
        if target_started:
            run("docker", "stop", target["container"])
        if started:
            run(*command(root, repo), "stop", "postgres")
        report["resources_after"] = inventory(root, repo)
        if report["resources_after"]["status"] != "passed":
            report["status"] = "failed"
        atomic_json(folder / "report.json", report)
    return report


def rollback(root, repo, identifier):
    """只回退尚无后续写入的首次部署；先备份现状，再恢复原库与镜像。"""
    import api_process
    from psycopg import sql

    previous = passed_report(root, "embedding-deploy-apply-", identifier)
    state = d.registration(root, identifier)
    if state["kind"] != "production" or api_process.status(root)["running"]:
        raise ValueError("回退仅允许正式登记且API已停止的维护窗口")
    pointer = a.read(root, "embedding/production.json")
    if not pointer.get("enabled") or pointer.get("deployment_id") != identifier:
        raise ValueError("正式活动部署身份已改变，拒绝回退")
    folder = root / "reports" / ("embedding-deploy-rollback-" + uuid.uuid4().hex)
    folder.mkdir()
    report = {"status": "running", "deployment_id": identifier, "folder": str(folder)}
    started = False
    try:
        report["resources_before"] = inventory(root, repo, 1024**3)
        if report["resources_before"]["status"] != "passed":
            raise PlatformError(
                "RESOURCE_BLOCKED", "formal-rollback-budget", "完整回退备份增长预算不足"
            )
        info = d.container(state, running=False)
        started = not info["State"]["Running"]
        if started:
            run(
                *command(root, repo),
                "up",
                "-d",
                "--wait",
                "--pull",
                "never",
                "postgres",
            )
        old_folder = Path(previous["folder"])
        backup = old_folder / "before.dump"
        if (
            hashlib.sha256(backup.read_bytes()).hexdigest()
            != previous["backup_before"]["sha256"]
        ):
            raise ValueError("原库备份摘要不一致，禁止回退")
        with store.connect(root, deployment=identifier) as conn:
            conn.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
            snapshot = conn.execute("SELECT pg_export_snapshot()").fetchone()[0]
            current = fingerprint(conn)
            if current != previous["deployed_fingerprint"]:
                raise ValueError(
                    "部署后数据已改变，禁止用旧备份覆盖；须制定保留新写入的恢复方案"
                )
            report["backup_current"] = dump(
                ["docker", "exec", "-i", SERVICE],
                "ddp",
                folder / "current.dump",
                snapshot,
            )
            conn.execute("ROLLBACK")
        with psycopg.connect(
            api_dsn(root, {**state, "database": "postgres"}), autocommit=True
        ) as conn:
            if conn.execute(
                "SELECT count(*) FROM pg_stat_activity WHERE datname='ddp'"
            ).fetchone() != (0,):
                raise ValueError("正式数据库仍有其他连接，禁止重建")
            conn.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier("ddp")))
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier("ddp")))
        restore(["docker", "exec", "-i", SERVICE], "ddp", backup)
        with source_connect(root) as conn:
            if fingerprint(conn) != previous["source_fingerprint"]:
                raise ValueError("原业务备份恢复指纹不一致")
            extensions = [
                list(row)
                for row in conn.execute(
                    "SELECT extname,extversion FROM pg_extension ORDER BY extname"
                ).fetchall()
            ]
            if extensions != previous["source_extensions"]:
                raise ValueError("扩展状态未恢复")
        original = json.loads((old_folder / "versions.lock.before.json").read_text())
        current_lock = a.read(repo, "infra/docker/versions.lock.json")
        if {k: v for k, v in current_lock.items() if k != "POSTGRES"} != {
            k: v for k, v in original.items() if k != "POSTGRES"
        }:
            raise ValueError("其他组件镜像锁已改变，拒绝覆盖")
        set_image(root, original["POSTGRES"]["image"])
        run(
            *command(root, repo),
            "up",
            "-d",
            "--wait",
            "--pull",
            "never",
            "--no-deps",
            "postgres",
        )
        with source_connect(root) as conn:
            if fingerprint(conn) != previous["source_fingerprint"]:
                raise ValueError("原镜像恢复后业务指纹不一致")
        atomic_json(repo / "infra/docker/versions.lock.json", original)
        atomic_json(
            root / "embedding/production.json",
            {
                "enabled": False,
                "previous_deployment_id": identifier,
                "rollback_report": str(folder / "report.json"),
            },
        )
        report["status"] = "passed"
        report["restored_business_fingerprint"] = previous["source_fingerprint"]
    except BaseException as error:
        report["status"] = "failed"
        report["error"] = describe(error, "formal-rollback")
        raise
    finally:
        if started:
            run(*command(root, repo), "stop", "postgres")
        report["resources_after"] = inventory(root, repo)
        if report["resources_after"]["status"] != "passed":
            report["status"] = "failed"
        atomic_json(folder / "report.json", report)
    return report


def services(root, repo, action):
    """管理已发布正式部署的数据库、Metastore、模型和原API。"""
    import api_process

    from driving_data_platform.embedding import runtime as model

    pointer = a.read(root, "embedding/production.json")
    if not pointer.get("enabled"):
        raise ValueError("尚无启用的正式向量部署")
    identifier = pointer["deployment_id"]
    state = d.registration(root, identifier)
    info = d.container(state, running=False)
    if state["kind"] != "production":
        raise ValueError("统一服务入口只接受正式部署")
    if action == "status":
        model_running = False
        model_error = None
        try:
            value = model.active_space(root, identifier)
            model.encoder(root, value["space_id"]).probe()
            model_running = True
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            model_error = describe(error, "model-status")
        return {
            "status": "passed",
            "deployment_id": identifier,
            "postgres_running": info["State"]["Running"],
            "api": api_process.status(root),
            "model_running": model_running,
            "model_error": model_error,
        }
    if action == "stop":
        current = api_process.status(root)
        if current["running"] and current.get("deployment") != identifier:
            raise ValueError("API属于其他环境，拒绝停止")
        receipt = root / "embedding/service-runtime.json"
        value = a.read(receipt.parent, receipt.name) if receipt.exists() else None
        if value and value.get("deployment_id") != identifier:
            raise ValueError("统一服务运行登记属于其他部署，拒绝停止")
        api_process.stop(root)
        model.stop(root, space_id=value["space_id"] if value else None)
        run(*command(root, repo), "stop", "metastore", "postgres")
        return {"status": "passed", "deployment_id": identifier, "running": False}
    budget = inventory(root, repo)
    if budget["status"] != "passed":
        raise PlatformError(
            "RESOURCE_BLOCKED", "service-budget", "统一服务资源预算未通过"
        )
    initial = {
        x
        for x in run("docker", "ps", "--format", "{{.Names}}")
        .stdout.decode()
        .splitlines()
    }
    mr = ar = None
    try:
        run(
            *command(root, repo),
            "up",
            "-d",
            "--wait",
            "--pull",
            "never",
            "postgres",
            "metastore",
        )
        value = model.active_space(root, identifier)
        mr = model.up(root, value["space_id"])
        ar = api_process.start(root, repo)
        atomic_json(
            root / "embedding/service-runtime.json",
            {"deployment_id": identifier, "space_id": value["space_id"]},
        )
        return {
            "status": "passed",
            "deployment_id": identifier,
            "url": ("http://127.0.0.1:" + str(port("api"))),
            "space_id": value["space_id"],
        }
    except BaseException:
        try:
            if ar and not ar["reused"]:
                api_process.stop(root)
        finally:
            if mr and not mr["reused"]:
                model.stop(root)
            for service in ("metastore", "postgres"):
                if "driving-data-platform-mac-" + service + "-1" not in initial:
                    run(*command(root, repo), "stop", service)
        raise


def final_preflight(root, repo):
    """最终组合验收的最低增长门禁；不以资源通过冒充长期验收。"""
    pointer = a.read(root, "embedding/production.json")
    if not pointer.get("enabled"):
        raise ValueError("正式部署尚未启用")
    identifier = pointer["deployment_id"]
    deployed = passed_report(root, "embedding-deploy-apply-", identifier)
    with store.connect(root, deployment=identifier) as conn:
        database_bytes = conn.execute(
            "SELECT pg_database_size(current_database())"
        ).fetchone()[0]
    # 一份独立恢复库、一份完整备份和最小恢复WAL；尚不计两轮新业务产物。
    backup_bytes = deployed["backup_after"]["bytes"]
    minimum_growth = database_bytes + backup_bytes + 128 * 1024**2
    measured = inventory(root, repo, minimum_growth)
    report = {
        "status": measured["status"],
        "scope": "仅最终组合验收资源前置检查，未执行1800秒联调",
        "deployment_id": identifier,
        "database_bytes": database_bytes,
        "backup_bytes": backup_bytes,
        "wal_reserve_bytes": 128 * 1024**2,
        "minimum_growth_bytes": minimum_growth,
        "new_pipeline_growth_included": False,
        "resources": measured,
    }
    if measured["status"] != "passed":
        report["error"] = {
            "category": "RESOURCE_BLOCKED",
            "code": "final-integration-growth",
            "message": "完整备份和独立恢复库的最低增长超过项目预算，未启动长期联调",
        }
    path = (
        root / "reports" / ("embedding-final-preflight-" + uuid.uuid4().hex + ".json")
    )
    atomic_json(path, report)
    return {**report, "evidence": str(path)}
