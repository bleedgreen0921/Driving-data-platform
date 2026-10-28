"""单次运行的原子报告；运行身份不参与产物身份。"""

import json
import re
import signal
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime

from driving_data_platform.common.errors import describe
from driving_data_platform.ingestion.nuscenes import atomic_json


def now():
    return datetime.now(UTC).isoformat()


def show(root, run_id):
    if not re.fullmatch(r"[a-f0-9]{32}", run_id):
        raise ValueError("run-id 必须是 32 位十六进制身份")
    path = root / "reports" / ("pipeline-" + run_id + ".json")
    if path.is_symlink():
        raise ValueError("运行报告不得是符号链接")
    return json.loads(path.read_text())


class RunContext:
    def __init__(self, root, version):
        run_id = uuid.uuid4().hex
        self.path = root / "reports" / ("pipeline-" + run_id + ".json")
        self.report = {
            "report_schema_version": 1,
            "run_id": run_id,
            "command": "run",
            "status": "running",
            "processing_version": version,
            "started_at": now(),
            "report": str(self.path),
            "steps": {
                name: {"status": "pending"}
                for name in (
                    "capture",
                    "preflight",
                    "ods",
                    "warehouse",
                    "load",
                    "publish",
                )
            },
        }
        self.stage = None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.save()

    def save(self):
        atomic_json(self.path, self.report)

    @contextmanager
    def step(self, name):
        self.stage = name
        item = self.report["steps"][name]
        item.update(status="running", started_at=now())
        self.save()
        start = time.monotonic()
        try:
            yield item
        except BaseException:
            item["status"] = "failed"
            raise
        else:
            item["status"] = "passed"
        finally:
            item.update(finished_at=now(), seconds=time.monotonic() - start)
            if sys.exc_info()[0] is None:
                self.save()
            else:
                self.safe_save()

    def safe_save(self):
        try:
            self.save()
        except OSError:
            print(
                "运行报告写入失败，保留原始异常；检查产物磁盘和权限。", file=sys.stderr
            )

    @contextmanager
    def execute(self):
        previous = None
        # SIGTERM 用普通中断路径收尾；不承诺能处理 SIGKILL。
        if threading.current_thread() is threading.main_thread():

            def terminate(*_):
                raise KeyboardInterrupt

            previous = signal.signal(signal.SIGTERM, terminate)
        try:
            yield self
            self.report["status"] = "passed"
        except BaseException as error:
            self.report["status"] = (
                "interrupted"
                if isinstance(error, (KeyboardInterrupt, SystemExit))
                else "failed"
            )
            self.report["error"] = describe(error, self.stage)
            for item in self.report["steps"].values():
                if item["status"] == "pending":
                    item["status"] = "skipped"
            raise
        finally:
            self.report["finished_at"] = now()
            if previous is not None:
                signal.signal(signal.SIGTERM, previous)
            if sys.exc_info()[0] is None:
                self.save()
            else:
                self.safe_save()
