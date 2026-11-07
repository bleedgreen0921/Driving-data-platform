"""真实运行/血缘验收；不执行持续压测或备份恢复。"""

import argparse
import json
import shutil
import threading
import uuid
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import urlopen

from driving_data_platform.common.config import application_config, output_root, port
from driving_data_platform.common.run_query import list_runs
from driving_data_platform.lineage import lineage
from driving_data_platform.pipeline import DEFAULT_VERSION, run
from driving_data_platform.publishing.store import active, connect


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--faults", action="store_true")
    args = parser.parse_args()
    root = output_root()
    repo = Path(__file__).resolve().parents[1]
    directory = root / "reports" / ("observability-" + uuid.uuid4().hex)
    directory.mkdir()
    checks = []
    report = {
        "status": "running",
        "checks": checks,
        "not_executed": [
            "持续并发观察",
            "5 分钟替代测试",
            "两库备份恢复",
            "浏览器（工具权限不可用，需单独验证）",
        ],
    }

    def save():
        (directory / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2)
        )

    def http(path, expected=200):
        try:
            with urlopen(
                ("http://127.0.0.1:" + str(port("api"))) + path, timeout=15
            ) as response:
                code, data = response.status, response.read()
        except HTTPError as error:
            code, data = error.code, error.read()
        assert code == expected, (path, code)
        return json.loads(data)

    save()
    try:
        with connect(root) as conn:
            before = active(conn)
            generations = conn.execute(
                "SELECT generation,processing_version FROM control.generation ORDER BY created_at"
            ).fetchall()
        completed, errors, observed = [], [], []
        prior = {i["run_id"] for i in list_runs(root, limit=100)["items"]}

        def rerun():
            try:
                completed.append(run(root, repo, DEFAULT_VERSION))
            except Exception as error:  # noqa: BLE001 -- 将线程失败传回主验证
                errors.append(type(error).__name__)

        worker = threading.Thread(target=rerun)
        worker.start()
        while worker.is_alive():
            for item in list_runs(root, limit=100)["items"]:
                if item["run_id"] not in prior and item["status"] == "running":
                    observed.append(
                        {"run_id": item["run_id"], "stage": item["current_stage"]}
                    )
            worker.join(0.5)
        assert not errors, errors
        value = completed[0]
        assert value["status"] == "passed"
        assert all(value["steps"][s]["reused"] for s in ("ods", "warehouse", "load"))
        assert value["steps"]["load"]["generation"] == before["generation"]
        assert observed, "未观察到阶段更新"
        report["rerun_id"] = value["run_id"]
        report["observed_running"] = observed
        checks.append("当前默认版本 重跑、身份复用及阶段更新")
        current = lineage(root, repo, generation=before["generation"])
        assert current["completeness"] == "matched", current["warnings"]
        assert len(current["table_dependencies"]["items"]) == 11
        assert len(current["related_runs"]) > 1
        assert (
            next(n for n in current["nodes"] if n["id"] == "source_files")["identity"][
                "file_count"
            ]
            == 31223
        )
        (directory / "current-lineage.json").write_text(
            json.dumps(current, ensure_ascii=False, indent=2)
        )
        # Historical business data remains readable, but a new machine need not contain an old fixture.
        old = next((g for g, v in generations if v == "warehouse-v2"), None)
        if old:
            historical = lineage(root, repo, generation=old)
            assert not any(n["kind"] == "hms_catalog" for n in historical["nodes"])
            (directory / "historical-lineage.json").write_text(
                json.dumps(historical, ensure_ascii=False, indent=2)
            )
            checks.append("已有旧 JSON generation只读血缘")
        else:
            report["not_executed"].append(
                "当前环境无旧 JSON generation；不创建历史伪数据"
            )
        checks.append("当前默认版本、多运行复用")
        for path, code in [
            ("/api/runs", 200),
            ("/api/runs?status=failed", 200),
            ("/api/runs?limit=101", 422),
            ("/api/runs?status=bad", 422),
            ("/api/runs/no", 422),
            ("/api/runs/" + "0" * 32, 404),
            ("/api/lineage", 422),
            ("/api/lineage?run_id=x&generation=y", 422),
            ("/api/lineage?generation=g_" + "0" * 64, 404),
        ]:
            http(path, code)
        assert http("/api/runs/" + value["run_id"])["status"] == "passed"
        data = http("/api/lineage?" + urlencode({"generation": before["generation"]}))
        assert data["completeness"] == "matched"
        assert str(root) not in json.dumps(data)
        checks.append("真实只读 HTTP 成功、404、422、敏感字段过滤")
        if args.faults:
            import yaml

            trial = directory / "input-failure"
            trial.mkdir()
            config = application_config(root)
            config["dataset"]["root"] = str(trial / "missing-input")
            config["storage"]["output_root"] = str(trial)
            (trial / "application.yaml").write_text(yaml.safe_dump(config))
            shutil.copy2(root / "compose.env", trial / "compose.env")
            (trial / "compose.env").chmod(0o600)
            try:
                run(trial, repo)
            except (ValueError, OSError):
                pass
            failed = list_runs(trial)["items"][0]
            assert (
                failed["status"] == "failed" and failed["current_stage"] == "preflight"
            )
            assert all(s["status"] == "skipped" for s in failed["steps"][2:])
            partial = lineage(trial, repo, run_id=failed["run_id"])
            assert partial["generation"] is None and not any(
                n["kind"] == "warehouse_parquet" for n in partial["nodes"]
            )
            (directory / "failed-lineage.json").write_text(
                json.dumps(partial, ensure_ascii=False, indent=2)
            )
            checks.append("独立配置真实输入失败、跳过阶段和部分链路")
            # 只复制清单；冲突和缺失操作绝不触碰正式证据。
            evidence = directory / "copied-evidence"
            evidence.mkdir()
            shutil.copy2(root / "compose.env", evidence / "compose.env")
            (evidence / "compose.env").chmod(0o600)
            for n in current["nodes"]:
                ref = (n.get("evidence") or {}).get("reference", "")
                if ref.endswith(".json") and (root / ref).is_file():
                    target = evidence / ref
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(root / ref, target)
            wh = next(
                n["evidence"]["reference"]
                for n in current["nodes"]
                if n["id"] == "warehouse"
            )
            target = evidence / wh
            original = target.read_bytes()
            target.write_bytes(original + b"\n")
            conflict = lineage(evidence, repo, generation=before["generation"])
            assert conflict["completeness"] == "inconsistent"
            assert conflict["table_dependencies"]["status"] == "unknown"
            (directory / "conflict-lineage.json").write_text(
                json.dumps(conflict, ensure_ascii=False, indent=2)
            )
            target.unlink()
            missing = lineage(evidence, repo, generation=before["generation"])
            assert missing["completeness"] == "partial"
            (directory / "missing-lineage.json").write_text(
                json.dumps(missing, ensure_ascii=False, indent=2)
            )
            checks.append("独立复制清单摘要冲突与缺失")
        with connect(root) as conn:
            after = active(conn)
        assert after["generation"] == before["generation"]
        report["active_before"], report["active_after"] = before, after
        report["status"] = "passed"
    except BaseException:
        report["status"] = "failed"
        raise
    finally:
        save()
        print(
            json.dumps(
                {"status": report["status"], "directory": str(directory)},
                ensure_ascii=False,
            )
        )


if __name__ == "__main__":
    main()
