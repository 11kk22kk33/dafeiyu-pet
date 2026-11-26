# -*- coding: utf-8 -*-
"""池子跨进程锁：线程锁(MEM.LOCK)管不住别的进程，这里补一把文件锁。

用法：
    from core.poollock import pool_lock
    with pool_lock():
        ...   # load → 改 → save 全段持有

Windows 走 msvcrt.locking（字节锁，进程退出自动释放）；拿不到锁超时抛 TimeoutError，
调用方必须放弃写入并记日志，绝不许硬写。
"""
import contextlib
import os
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCK_PATH = os.path.join(BASE, "memory-pool.lock")


@contextlib.contextmanager
def pool_lock(timeout=10.0, path=None):
    p = path or LOCK_PATH
    f = open(p, "a+b")
    got = False
    t0 = time.time()
    try:
        while True:
            try:
                f.seek(0)
                f.write(b"0")
                f.flush()
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                got = True
                break
            except OSError:
                if time.time() - t0 > timeout:
                    raise TimeoutError("memory-pool.lock busy > %.1fs" % timeout)
                time.sleep(0.05)
        yield
    finally:
        if got:
            try:
                f.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        try:
            f.close()
        except Exception:
            pass
