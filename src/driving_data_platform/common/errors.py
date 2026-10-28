"""稳定错误分类，保留原始异常链；不推测子进程失败原因。"""

import errno
import subprocess

import psycopg

ADVICE = {
    "INPUT_INVALID": "修正输入、配置或路径后重跑。",
    "CONTRACT_CONFLICT": "核对契约、代码和 schema；变更后使用新处理版本。",
    "DEPENDENCY_UNAVAILABLE": "恢复依赖，核实已提交结果后重跑。",
    "RESOURCE_BLOCKED": "调整磁盘或内存资源后重跑。",
    "RECONCILIATION_FAILED": "检查独立对账差异，禁止发布。",
    "OPERATION_CONFLICT": "等待当前操作结束后重新执行。",
    "PUBLICATION_CONFLICT": "检查当前活动版本后重新决定，不能自动刷新预期指针。",
    "INTERRUPTED": "核实已提交产物后重跑。",
    "EXECUTION_FAILED": "查看阶段日志，核实子进程及提交状态后重跑。",
    "INTERNAL_ERROR": "查看日志排查，不盲目重试。",
}


class PlatformError(ValueError):
    def __init__(self, category, code, message):
        super().__init__(message)
        self.category = category
        self.code = code


def describe(error, stage=None):
    if isinstance(error, PlatformError):
        category, code, message = error.category, error.code, str(error)
    else:
        category, code = "INTERNAL_ERROR", "unclassified"
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            category, code = "INTERRUPTED", "signal-or-interrupt"
        elif isinstance(error, BlockingIOError):
            category, code = "OPERATION_CONFLICT", "operation-locked"
        elif isinstance(error, psycopg.OperationalError):
            category, code = (
                "DEPENDENCY_UNAVAILABLE",
                "database-unavailable-result-unknown",
            )
        elif isinstance(error, OSError) and error.errno == errno.ENOSPC:
            category, code = "RESOURCE_BLOCKED", "disk-full"
        elif isinstance(error, (FileNotFoundError, PermissionError, ValueError)):
            category, code = "INPUT_INVALID", "invalid-input"
        elif isinstance(error, subprocess.SubprocessError):
            category, code = "EXECUTION_FAILED", "subprocess-failed"
        message = (
            str(error)
            if isinstance(error, (ValueError, KeyboardInterrupt))
            else "操作失败，查看本轮日志；不自动重试。"
        )
    return {
        "category": category,
        "code": code,
        "message": message,
        "stage": stage,
        "advice": ADVICE[category],
    }
