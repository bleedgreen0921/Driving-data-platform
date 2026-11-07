"""embedding 子命令调度；业务查询与独立试验连接明确区分。"""

from driving_data_platform.common.config import (
    application_config,
)
from driving_data_platform.embedding import artifacts, store
from driving_data_platform.publishing.store import connect as service_connect


def _execute(root, repo, args):
    if args.command == "embedding-space-register":
        from driving_data_platform.embedding.runtime import register

        if not args.space_file:
            raise ValueError("必须指定 --space-file")
        return register(root, args.space_file, test_only=args.target_test_only)
    if args.command == "embedding-trial":
        import embedding_api_process
        from embedding_ops import trial_state, trial_stop, trial_up

        if not args.trial or not args.action:
            raise ValueError("必须显式指定 --trial 和 --action")
        if args.action == "up":
            return trial_up(root, repo, args.trial)
        if args.action == "stop":
            return trial_stop(root, args.trial)
        if args.action == "status":
            state = trial_state(root, args.trial)
            import json
            import subprocess

            value = json.loads(
                subprocess.check_output(
                    ["docker", "container", "inspect", state["container"]]
                )
            )[0]
            if value["Config"]["Labels"].get("ddp.embedding.trial") != args.trial:
                raise ValueError("试验实例身份不一致")
            return {
                "status": "passed",
                "trial": args.trial,
                "running": value["State"]["Running"],
            }
        if args.action == "api-up":
            return embedding_api_process.start(root, repo, args.trial)
        if args.action == "api-stop":
            return embedding_api_process.stop(root, args.trial)
        return embedding_api_process.status(root, args.trial)
    if args.command == "embedding-deploy-final-preflight":
        from embedding_deploy import final_preflight

        return final_preflight(root, repo)
    if args.command.startswith("embedding-service-"):
        from embedding_deploy import services

        return services(root, repo, args.command.removeprefix("embedding-service-"))

    if args.command == "embedding-deploy-rollback":
        from embedding_deploy import rollback

        if not args.deployment or not args.faults:
            raise ValueError(
                "必须指定 --deployment 和 --faults 以选择维护窗口的完整数据库回退"
            )
        return rollback(root, repo, args.deployment)
    if args.command == "embedding-deploy-apply":
        from embedding_deploy import apply

        if not args.deployment:
            raise ValueError("必须指定已通过演练的 --deployment")
        return apply(root, repo, args.deployment)
    if args.command == "embedding-deploy-verify":
        from embedding_deploy import verify_rehearsal

        if not args.deployment or not args.faults:
            raise ValueError(
                "须显式指定 --deployment 和 --faults 以选择独立恢复演练重建"
            )
        return verify_rehearsal(root, repo, args.deployment)
    if args.command == "embedding-deploy-rehearse":
        from embedding_deploy import rehearsal

        return rehearsal(root, repo)

    if args.command == "embedding-search":
        from driving_data_platform.embedding.search import execute as search

        return search(root, args)
    if args.command in (
        "embedding-model-up",
        "embedding-model-status",
        "embedding-model-stop",
    ):
        from driving_data_platform.embedding import model

        if args.command.endswith("-up"):
            return model.up(root, args.space_id)
        if args.command.endswith("-stop"):
            return model.stop(root)
        import os

        path = model.state_path(root)
        if not path.exists():
            return {"status": "passed", "process_status": "stopped"}
        state = artifacts.read(path.parent, path.name)
        try:
            os.kill(state["pid"], 0)
        except ProcessLookupError:
            return {
                "status": "passed",
                "process_status": "stopped",
                "space_id": state["space_id"],
            }
        return model.checked(root)
    if args.command in ("embedding-build", "embedding-build-show"):
        from driving_data_platform.embedding import build

        return (
            build.show(root, args.run_id)
            if args.command.endswith("-show")
            else build.execute(root, args.input_id, args.space_id)
        )
    if args.command == "embedding-evaluate":
        from driving_data_platform.embedding.evaluate import execute

        return execute(root, args.input_id, space_id=args.space_id)
    if args.command == "embedding-input":
        if args.trial:
            manager = store.connect(root, trial=args.trial)
        else:
            manager = service_connect(root)
        with manager as conn:
            dataset = None
            if args.verify_files:
                dataset = application_config(root)["dataset"]["root"]
            return artifacts.capture(
                root,
                conn,
                args.generation,
                verify_files=args.verify_files,
                dataset_root=dataset,
            )
    if args.command == "embedding-prepare":
        from embedding_ops import prepare

        return prepare(root, repo)
    with store.connect(root, trial=args.trial, deployment=args.deployment) as conn:
        if args.command == "embedding-migrate":
            if args.deployment:
                from driving_data_platform.embedding import deployment as d

                if args.test_database or args.restore_from_database:
                    raise ValueError("正式迁移不得使用试验恢复参数")
                return store.migrate_deployment(
                    conn, repo, d.registration(root, args.deployment)
                )
            return store.migrate(
                conn,
                repo,
                test_database=args.test_database,
                restore_from_database=args.restore_from_database,
            )
        if args.command == "embedding-load":
            return store.load(root, conn, args.artifact_id)
        if args.command == "embedding-show":
            return store.show(conn, args.embedding_generation)
        if args.command in ("embedding-publish", "embedding-rollback"):
            return store.publish(
                conn,
                args.embedding_generation,
                args.expected_revision,
                rollback=args.command == "embedding-rollback",
            )
    raise ValueError("未知 embedding 命令")


def execute(root, repo, args):
    """单项向量任务独立报告，不混入 pipeline 六阶段运行。"""
    import time
    import uuid
    from datetime import UTC, datetime

    from driving_data_platform.common.errors import describe
    from driving_data_platform.ingestion.nuscenes import atomic_json

    task_id = uuid.uuid4().hex
    directory = artifacts.safe_parent(root, "reports")
    path = directory / ("task-" + task_id + ".json")
    report = {
        "report_schema_version": 1,
        "run_id": task_id,
        "command": args.command,
        "stage": args.command.removeprefix("embedding-"),
        "status": "running",
        "started_at": datetime.now(UTC).isoformat(),
        "input": {
            key: getattr(args, key, None)
            for key in (
                "generation",
                "artifact_id",
                "embedding_generation",
                "trial",
                "deployment",
                "input_id",
                "space_id",
                "run_id",
            )
        },
    }
    if args.command == "embedding-search" and isinstance(args.text, str):
        import hashlib

        report["input"].update(
            query_sha256=hashlib.sha256(
                args.text.encode("utf-8", errors="surrogatepass")
            ).hexdigest(),
            query_characters=len(args.text),
        )
    atomic_json(path, report)
    start = time.monotonic()
    try:
        result = _execute(root, repo, args)
        report["status"] = result.get("status", "passed")
        report["result"] = {
            key: value
            for key, value in result.items()
            if key
            in (
                "query_sha256",
                "query_characters",
                "timings",
                "space_id",
                "input_id",
                "artifact_id",
                "embedding_generation",
                "generation",
                "count",
                "reused",
                "migration",
                "revision",
                "trial",
            )
        }
        result["report"] = str(path)
        return result
    except (Exception, KeyboardInterrupt) as error:
        report["status"] = (
            "interrupted" if isinstance(error, KeyboardInterrupt) else "failed"
        )
        report["error"] = describe(error, args.command)
        raise
    finally:
        report["seconds"] = time.monotonic() - start
        report["finished_at"] = datetime.now(UTC).isoformat()
        try:
            atomic_json(path, report)
        except OSError:
            if report["status"] == "passed":
                raise
