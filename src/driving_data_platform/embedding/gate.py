"""跨 CLI 与 HTTP 的单检索互斥；进程退出后由内核释放。"""

import fcntl
import os
from contextlib import contextmanager

from driving_data_platform.common.errors import PlatformError
from driving_data_platform.embedding.artifacts import safe_parent


@contextmanager
def acquire(root):
    path = safe_parent(root, "runtime") / "text-search.lock"
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise PlatformError(
                "OPERATION_CONFLICT", "search-busy", "当前检索繁忙，请稍后手动重试。"
            ) from None
        yield
    finally:
        os.close(descriptor)
