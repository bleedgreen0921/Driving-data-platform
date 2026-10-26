"""统一验证入口可传递实际锁描述符；独立运行也必须取得同一把锁。"""

import fcntl
import os
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def operation_lock(root: Path):
    path = root / ".operation.lock"
    inherited = os.environ.get("DDP_OPERATION_LOCK_FD")
    if inherited is not None:
        descriptor = int(inherited)
        actual = os.fstat(descriptor)
        expected = path.stat()
        if (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino):
            raise ValueError("串行锁描述符不属于当前产物根")
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # 不释放父进程持有的共享 open-file-description 锁。
        yield
    else:
        root.mkdir(parents=True, exist_ok=True)
        with path.open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield
