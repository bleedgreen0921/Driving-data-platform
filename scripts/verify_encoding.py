"""真实模型文件边界与恢复验证；故障仅作用于复制文件和本轮项目模型。"""

import argparse
import fcntl
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

from driving_data_platform.common.config import (
    application_config,
    port,
)
from driving_data_platform.common.errors import PlatformError
from driving_data_platform.embedding import artifacts as a
from driving_data_platform.embedding import build, model
from driving_data_platform.ingestion.nuscenes import atomic_json


def verify(root, input_id, faults=False, model_start_receipt=None):
    selected = a.read(root, "embedding/selection.json")
    sid = selected["space_id"]
    client = model.encoder(root, sid)
    if faults:
        if model_start_receipt is None:
            raise ValueError(
                "模型故障验证必须提供本轮启动回执 --model-start-receipt；不能停止原本运行的服务"
            )
        receipt = json.loads(model_start_receipt.read_text())
        actual = model.checked(root)
        if receipt.get("reused") is not False or any(
            receipt.get(k) != actual[k] for k in ("pid", "process_identity", "space_id")
        ):
            raise ValueError("启动回执不是本轮新启且身份一致的模型，拒绝停服故障")
    report = {
        "status": "running",
        "checks": [],
        "scope": "真实模型、独立复制媒体及缓存，不写数据库",
    }
    directory = root / "reports" / ("encoding-recovery-" + uuid.uuid4().hex)
    directory.mkdir()
    path = directory / "report.json"

    def check(name):
        report["checks"].append(name)
        atomic_json(path, report)

    try:
        good = client.encode_text("路口有汽车")
        assert len(good) == 2048
        state = model.checked(root)
        log = Path(state["log"])
        before = log.stat().st_size
        for data in (b"fake.jpg", b"\xff\xd8\xffbroken"):
            try:
                client.encode_image(data)
            except PlatformError as error:
                assert error.category == "INPUT_INVALID"
            else:
                raise AssertionError("坏图未被拒绝")
        assert log.stat().st_size == before
        client.encode_text("后续正常请求")
        check("真实坏图在本地拒绝且无服务请求；后续正常文本成功")
        source = build.source(root, input_id)
        dataset = Path(application_config(root)["dataset"]["root"])
        client.encode_image(build.image(dataset, source["entries"][0]))
        check("真实图片编码通过")
        if not faults:
            report["status"] = "passed"
            return report
        trial = directory / "trial"
        trial.mkdir()
        for relative in (
            "embedding/selection.json",
            selected["report"],
            ("embedding/model-runtime/" + str(port("model")) + ".json"),
            f"embedding/spaces/{sid}/space.json",
        ):
            target = trial / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(root / relative, target)
        copied = trial / "dataset"
        copied.mkdir()
        subset = source | {"entries": source["entries"][:8], "count": 8}
        iid = a.checksum(subset)
        a.commit(trial, "inputs", iid, {"input.json": a.json_bytes(subset)})
        for entry in subset["entries"]:
            target = copied / entry["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(dataset / entry["path"], target)
        (trial / "application.yaml").write_text(
            "dataset:\n  root: " + str(copied) + "\n"
        )
        first = subset["entries"][0]
        bad = copied / first["path"]
        bad.write_bytes(b"broken copied media")
        result = build.execute(trial, iid, sid)
        assert (
            result["failed"] == 1
            and result["success"] == 7
            and result["status"] == "failed"
        )
        assert not list((trial / "embedding").glob("artifacts/*/complete.json"))
        check("单图摘要失败继续其余七张且无完成标记")
        shutil.copyfile(dataset / first["path"], bad)
        result = build.execute(trial, iid, sid)
        assert (
            result["reused"] == 7
            and result["success"] == 1
            and result["status"] == "passed"
        )
        artifact = result["artifact"]["artifact_id"]
        a.verify_artifact(trial, artifact)
        result = build.execute(trial, iid, sid)
        assert result["reused"] == 8 and result["artifact"]["reused"]
        check("修复重跑复用七项；提交后再次重跑八项全部复用且产物校验通过")
        cache_id = a.checksum({"sha256": first["sha256"], "space_id": sid})
        cache = trial / "embedding/cache" / cache_id / "result.json"
        original = cache.read_bytes()
        cache.write_text("{}")
        result = build.execute(trial, iid, sid)
        assert (
            result["failed"] == 1
            and result["reused"] == 7
            and cache.read_text() == "{}"
        )
        cache.write_bytes(original)
        check("损坏缓存不复用、不覆盖，其他七项继续复用")
        # 新的独立缓存目录验证中断；已验证缓存移入证据区保留。
        (trial / "embedding/cache").rename(trial / "embedding/cache-before-interrupt")
        result = build.execute(trial, iid, sid)
        assert (
            result["reused"] == 8
            and result["success"] == 0
            and result["artifact"]["reused"]
        )
        assert not list((trial / "embedding/cache").glob("*/result.json"))
        check("成功产物在缓存移出后直接核验复用，不重新编码")
        subset = source | {"entries": source["entries"][:7], "count": 7}
        iid = a.checksum(subset)
        a.commit(trial, "inputs", iid, {"input.json": a.json_bytes(subset)})
        script = (
            "from pathlib import Path; from driving_data_platform.embedding.build import execute; execute(Path("
            + repr(str(trial))
            + "),"
            + repr(iid)
            + ","
            + repr(sid)
            + ")"
        )
        with (directory / "interrupt.log").open("w") as stream:
            process = subprocess.Popen(
                [sys.executable, "-c", script], stdout=stream, stderr=subprocess.STDOUT
            )
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline and not list(
                (trial / "embedding/cache").glob("*/result.json")
            ):
                if process.poll() is not None:
                    raise AssertionError("中断试验提前退出")
                time.sleep(0.02)
            process.send_signal(signal.SIGTERM)
            process.wait(timeout=30)
        interrupted = max(
            (trial / "embedding/build-runs").glob("*.json"),
            key=lambda p: p.stat().st_mtime,
        )
        assert json.loads(interrupted.read_text())["status"] == "interrupted"
        count = len(list((trial / "embedding/cache").glob("*/result.json")))
        assert 0 < count < 7
        assert not any(
            a.read(p.parent, p.name).get("input_id") == iid
            for p in (trial / "embedding/artifacts").glob("*/complete.json")
        )
        result = build.execute(trial, iid, sid)
        assert result["reused"] == count and result["status"] == "passed"
        check("SIGTERM 保存 interrupted 报告；显式重跑复用中断前成功缓存")
        # 只停止本轮已核验服务；停止期间仍逐项核验复制文件并记录受阻。
        model.stop(root)
        (trial / "embedding/cache").rename(trial / "embedding/cache-before-stop")
        subset = source | {"entries": source["entries"][:6], "count": 6}
        iid = a.checksum(subset)
        a.commit(trial, "inputs", iid, {"input.json": a.json_bytes(subset)})
        result = build.execute(trial, iid, sid)
        assert (
            result["failed"] >= 1
            and result["blocked"] >= 1
            and result["status"] == "failed"
        )
        restored = model.up(root, sid)
        atomic_json(directory / "restored-model-start.json", restored)
        shutil.copyfile(
            root / ("embedding/model-runtime/" + str(port("model")) + ".json"),
            trial / ("embedding/model-runtime/" + str(port("model")) + ".json"),
        )
        result = build.execute(trial, iid, sid)
        assert result["status"] == "passed"
        check("模型停止后无错误完成标记；扫描全部文件并记录受阻；恢复模型后通过")
        # 暂停本轮重新启动的真实进程，使用显式短超时验证连续三次失败的门禁。
        paused = model.checked(root)
        assert paused["pid"] == restored["pid"] and not restored["reused"]
        (trial / "embedding/cache").rename(trial / "embedding/cache-before-timeout")
        subset = source | {"entries": source["entries"][:5], "count": 5}
        iid = a.checksum(subset)
        a.commit(trial, "inputs", iid, {"input.json": a.json_bytes(subset)})
        model_client = model.LocalLlama

        def short_timeout(config):
            return model_client(config | {"timeout_seconds": 0.2})

        os.kill(paused["pid"], signal.SIGSTOP)
        try:
            model.LocalLlama = short_timeout
            result = build.execute(trial, iid, sid)
            assert result["failed"] == 3 and result["blocked"] == 2
        finally:
            model.LocalLlama = model_client
            os.kill(paused["pid"], signal.SIGCONT)
        result = build.execute(trial, iid, sid)
        assert result["status"] == "passed"
        check(
            "真实模型 SIGSTOP，显式 0.2 秒测试超时：连续三次失败后健康核对并阻断两项；SIGCONT 恢复通过"
        )
        report["status"] = "passed"
        return report
    except BaseException as error:
        report.update(status="failed", error=str(error))
        raise
    finally:
        atomic_json(path, report)
        print(path, flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--input-id", required=True)
    parser.add_argument("--faults", action="store_true")
    parser.add_argument("--model-start-receipt", type=Path)
    args = parser.parse_args()
    with (args.root / ".operation.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        verify(args.root, args.input_id, args.faults, args.model_start_receipt)


if __name__ == "__main__":
    main()
