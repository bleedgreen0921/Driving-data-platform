"""只读运行证据查询；不探测进程，不补写历史报告。"""

import hashlib
import json
import math
import os
import re
import stat
from datetime import UTC, datetime
from pathlib import Path

from driving_data_platform.common.errors import ADVICE

STAGES = ("capture", "preflight", "ods", "warehouse", "load", "publish")
STATUSES = {"running", "passed", "failed", "interrupted"}
MAX_BYTES = 8 * 1024**2
MAX_REPORTS = 5000


class EvidenceError(ValueError):
    """证据无法安全读取或不支持。"""


def identity(value, pattern=r"[a-f0-9]{32}"):
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise ValueError("非法身份参数")
    return value


def read_json(root, relative, *, expected_type=dict):
    """逐级以 NOFOLLOW 打开，避免链接和读取时路径替换。"""
    relative = Path(relative)
    if relative.is_absolute() or ".." in relative.parts:
        raise EvidenceError("证据路径越界")
    if any(p.is_symlink() for p in [root, *root.parents]):
        raise EvidenceError("证据根目录包含符号链接")
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in relative.parts[:-1]:
            child = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
            )
            os.close(fd)
            fd = child
        file = os.open(
            relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd
        )
        with os.fdopen(file, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
                raise EvidenceError("证据类型非法或超过 8 MiB")
            data = stream.read(MAX_BYTES + 1)
            if len(data) > MAX_BYTES:
                raise EvidenceError("证据超过 8 MiB")
        value = json.loads(data)
        if not isinstance(value, expected_type):
            raise EvidenceError("证据必须是 JSON 对象")
        return value, hashlib.sha256(data).hexdigest(), info.st_mtime
    except FileNotFoundError:
        raise
    except (OSError, ValueError, UnicodeError):
        raise EvidenceError("证据不可读、损坏、不支持或包含符号链接") from None
    finally:
        os.close(fd)


def raw_run(root, run_id):
    identity(run_id)
    report, checksum, mtime = read_json(root, f"reports/pipeline-{run_id}.json")
    if (
        report.get("report_schema_version") != 1
        or report.get("run_id", run_id) != run_id
        or not isinstance(report.get("status"), str)
        or report.get("status") not in STATUSES
        or not isinstance(report.get("steps"), dict)
        or any(not isinstance(v, dict) for v in report["steps"].values())
    ):
        raise EvidenceError("运行报告 schema 或身份不支持")

    # 校验公开字段的类型和取值；拒绝把敏感文本伪装成身份传给 API。
    def token(value):
        return value is None or (
            isinstance(value, str) and re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", value)
        )

    def timestamp(value):
        if value is not None:
            parsed = datetime.fromisoformat(value)
            if parsed.tzinfo is None:
                raise ValueError()

    try:
        if not token(report.get("processing_version")):
            raise ValueError()
        for key in ("started_at", "finished_at"):
            timestamp(report.get(key))
        for stage in STAGES:
            item = report["steps"].get(stage, {})
            if item.get("status") not in (
                None,
                "pending",
                "running",
                "passed",
                "failed",
                "skipped",
            ):
                raise ValueError()
            for key in ("started_at", "finished_at"):
                timestamp(item.get(key))
            seconds = item.get("seconds")
            if seconds is not None and (
                type(seconds) not in (int, float)
                or not math.isfinite(seconds)
                or seconds < 0
            ):
                raise ValueError()
            if item.get("reused") is not None and type(item["reused"]) is not bool:
                raise ValueError()
            for key, pattern in (
                ("generation", r"g_[a-f0-9]{64}"),
                ("catalog_id", r"[a-f0-9]{32}"),
                ("attempt_id", r"[a-f0-9]{32}"),
                ("completion_sha256", r"[a-f0-9]{64}"),
            ):
                if item.get(key) is not None:
                    identity(item[key], pattern)
            if not isinstance(item.get("result", {}), dict):
                raise TypeError()
            if not token(item.get("result", item).get("snapshot_id")):
                raise ValueError()
        error = report.get("error", {})
        if error is None:
            error = {}
        if (
            not isinstance(error, dict)
            or error.get("category") not in (None, *ADVICE)
            or error.get("stage") not in (None, *STAGES)
        ):
            raise ValueError()
    except (ValueError, TypeError, OverflowError):
        raise EvidenceError("运行报告字段非法或不支持") from None
    return report, checksum, mtime


def normalized(root, run_id):
    report, checksum, mtime = raw_run(root, run_id)
    steps = report["steps"]
    result = {
        key: report.get(key)
        for key in ("status", "processing_version", "started_at", "finished_at")
    }
    started = result["started_at"]
    try:
        stamp = datetime.fromisoformat(started).timestamp() if started else mtime
    except (ValueError, TypeError):
        raise EvidenceError("报告时间格式非法") from None
    result.update(
        run_id=run_id,
        sort_time=stamp,
        time_source="started_at" if started else "file_mtime",
        display_time=started or datetime.fromtimestamp(mtime, UTC).isoformat(),
        evidence={"reference": f"reports/pipeline-{run_id}.json", "sha256": checksum},
        process_liveness="未核实进程存活",
        steps=[],
    )
    for stage in STAGES:
        item = steps.get(stage, {})
        step = {
            k: item.get(k)
            for k in ("status", "started_at", "finished_at", "seconds", "reused")
        }
        step["name"] = stage
        if step["status"] is None:
            step["status"] = "历史未记录"
        if step["status"] == "running" and step["started_at"]:
            try:
                step["estimated_seconds"] = max(
                    0,
                    datetime.now(UTC).timestamp()
                    - datetime.fromisoformat(step["started_at"]).timestamp(),
                )
            except (ValueError, TypeError):
                pass
        step["identities"] = {
            k: item[k]
            for k in ("generation", "catalog_id", "attempt_id", "completion_sha256")
            if k in item
        }
        ods = item.get("result", item)
        if stage == "ods":
            step["identities"]["snapshot_id"] = ods.get("snapshot_id")
        result["steps"].append(step)
    error = report.get("error") or {}
    category = error.get("category")
    # 不回传原始异常文本：历史错误可能包含连接信息或宿主路径。
    result["error"] = (
        {
            "category": category,
            "stage": error.get("stage"),
            "message": "阶段执行失败，请在本机核对原始报告。" if error else None,
            "advice": ADVICE.get(category, "历史未记录"),
        }
        if error
        else None
    )
    result["current_stage"] = next(
        (s["name"] for s in result["steps"] if s["status"] in ("running", "failed")),
        None,
    )
    result["recorded_seconds"] = sum(
        s["seconds"] for s in result["steps"] if isinstance(s["seconds"], (int, float))
    )
    result["candidate_generation"] = steps.get("load", {}).get("generation")
    result["published_generation"] = steps.get("publish", {}).get("generation")
    result["ever_published"] = None
    result["is_active"] = None
    return result


def list_runs(root, status=None, limit=20, offset=0):
    if status is not None and status not in STATUSES:
        raise ValueError("不支持的运行状态")
    if not 1 <= limit <= 100 or offset < 0:
        raise ValueError("limit 必须为 1–100，offset 不得为负")
    items, warnings = scan_runs(root, status)
    return {
        "schema_version": 1,
        "items": items[offset : offset + limit],
        "total": len(items),
        "limit": limit,
        "offset": offset,
        "warnings": warnings,
        "skipped": len(warnings),
    }


def scan_runs(root, status=None):
    directory = root / "reports"
    if directory.is_symlink():
        raise EvidenceError("报告目录不得是符号链接")
    items, warnings = [], []
    count = 0
    for path in directory.glob("pipeline-*.json"):
        match = re.fullmatch(r"pipeline-([a-f0-9]{32})\.json", path.name)
        if not match:
            continue
        count += 1
        if count > MAX_REPORTS:
            raise EvidenceError("运行报告超过 5000 份，请归档后查询")
        try:
            value = normalized(root, match[1])
            if status is None or value["status"] == status:
                items.append(value)
        except (EvidenceError, FileNotFoundError):
            warnings.append(
                {"run_id": match[1], "message": "报告损坏、不可读或不支持，已跳过"}
            )
    items.sort(key=lambda x: (x["sort_time"], x["run_id"]), reverse=True)
    return items, warnings


def publication_status(root, items):
    """数据库离线不影响文件查询，未知状态保留为 null。"""
    import psycopg

    from driving_data_platform.publishing.store import active, connect

    try:
        with connect(root) as conn:
            pointer = active(conn)["generation"]
            published = {
                r[0]
                for r in conn.execute(
                    "SELECT DISTINCT generation FROM control.publication"
                )
            }
        for item in items:
            g = item["candidate_generation"]
            if g:
                item.update(ever_published=g in published, is_active=g == pointer)
        return []
    except (psycopg.Error, ValueError, OSError):
        return ["数据库不可用：发布及活动状态未知"]
