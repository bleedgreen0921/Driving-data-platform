"""正式及恢复演练环境登记；连接身份来自本地登记和数据库双重核对。"""

import json
import re
import subprocess
from contextvars import ContextVar
from pathlib import Path

from driving_data_platform.common.config import port
from driving_data_platform.common.run_query import identity
from driving_data_platform.embedding import artifacts as a

CURRENT = ContextVar("embedding_deployment", default=None)


def registration(root, deployment):
    identity(deployment)
    value = a.read(root, "embedding/deployments/" + deployment + ".json")
    if (
        value.get("deployment_id") != deployment
        or value.get("kind") not in {"production", "rehearsal"}
        or value.get("host") != "127.0.0.1"
        or value.get("user") != "ddp"
        or not re.fullmatch(r"sha256:[a-f0-9]{64}", value.get("image", ""))
    ):
        raise ValueError("部署登记身份不合法")
    if value["kind"] == "production" and (
        value["database"] != "ddp"
        or value["port"] != port("postgres")
        or value["container"] != "driving-data-platform-mac-postgres-1"
        or value["volume"] != "driving-data-platform-mac_postgres-data"
    ):
        raise ValueError("正式数据库名称或端口不一致")
    if value["kind"] == "rehearsal" and (
        value["database"] != "ddp_deploy_" + deployment
        or value["port"] != port("restore")
        or value["container"] != "ddp-deploy-" + deployment
        or value["volume"] != "ddp-deploy-" + deployment
    ):
        raise ValueError("恢复演练数据库名称或端口不一致")
    return value


def container(state, *, running=True):
    repo = Path(__file__).resolve().parents[3]
    if state["image"] != a.read(repo, "infra/embedding/runtime.lock.json")["image"]:
        raise ValueError("部署镜像与已验证的pgvector锁不一致")
    info = json.loads(
        subprocess.check_output(["docker", "inspect", state["container"]])
    )[0]
    labels = info["Config"].get("Labels") or {}
    valid_label = (
        labels.get("com.docker.compose.project") == "driving-data-platform-mac"
        and labels.get("com.docker.compose.service") == "postgres"
        if state["kind"] == "production"
        else labels.get("ddp.embedding.deployment") == state["deployment_id"]
    )
    if (
        info["Image"] != state["image"]
        or not valid_label
        or (running and not info["State"]["Running"])
        or not any(
            m.get("Name") == state["volume"]
            and m["Destination"] == "/var/lib/postgresql/data"
            for m in info["Mounts"]
        )
        or info["HostConfig"]["PortBindings"].get("5432/tcp")
        != [{"HostIp": "127.0.0.1", "HostPort": str(state["port"])}]
    ):
        raise ValueError("部署容器、镜像、数据卷、标签或端口不一致")
    return info


def database_identity(conn):
    return conn.execute(
        "SELECT current_database(), (SELECT oid::text FROM pg_database WHERE datname=current_database()), system_identifier::text FROM pg_control_system()"
    ).fetchone()


def validate_database(conn, state, *, registered=True):
    expected = (state["database"], state["database_oid"], state["system_identifier"])
    if database_identity(conn) != expected:
        raise ValueError("数据库名称、OID或集群身份与部署登记不一致")
    if registered:
        actual = conn.execute(
            "SELECT deployment_id,kind,database_name,database_oid,system_identifier FROM control.embedding_deployment WHERE singleton"
        ).fetchone()
        if actual != (state["deployment_id"], state["kind"], *expected):
            raise ValueError("数据库内部部署登记与本地身份不一致")
        allowed = conn.execute(
            "SELECT test_only_allowed FROM control.embedding_environment WHERE singleton AND database_name=current_database()"
        ).fetchone()
        if allowed != (False,):
            raise ValueError("正式和恢复演练环境必须拒绝人工测试空间")
