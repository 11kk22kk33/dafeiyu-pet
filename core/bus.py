# -*- coding: utf-8 -*-
"""极简事件总线：订阅 / 发布，每个订阅者独立线程、独立超时、独立熔断。
谁炸了只影响自己，发消息那一侧永远不会被拖住，也不会被异常带走。"""
import os
import threading
import time
import traceback

LOG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "core.log")
FAIL_LIMIT = 3          # 连续失败几次开始熔断
COOL = 60.0             # 熔断后冷静多久再放行
DEFAULT_TIMEOUT = 20.0


def log(msg):
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (time.strftime("%H:%M:%S"), msg))
    except Exception:
        pass


class _Sub(object):
    __slots__ = ("fn", "timeout", "name", "fails", "open_until")

    def __init__(self, fn, timeout, name):
        self.fn = fn
        self.timeout = timeout
        self.name = name or getattr(fn, "__name__", "?")
        self.fails = 0
        self.open_until = 0.0

    def ready(self):
        return time.time() >= self.open_until

    def ok(self):
        self.fails = 0

    def bad(self, why):
        self.fails += 1
        if self.fails >= FAIL_LIMIT:
            self.open_until = time.time() + COOL
            log("熔断 %s（连续 %d 次）: %s" % (self.name, self.fails, why))
        else:
            log("出错 %s: %s" % (self.name, why))


class Bus(object):
    def __init__(self):
        self._subs = {}
        self._lock = threading.Lock()

    def on(self, topic, fn=None, timeout=DEFAULT_TIMEOUT, name=None):
        """用法：bus.on("语音", admin.notify) 或当装饰器 bus.on("语音")。"""
        if fn is None:
            return lambda f: self.on(topic, f, timeout, name)
        with self._lock:
            self._subs.setdefault(topic, []).append(_Sub(fn, timeout, name))
        return fn

    def off(self, topic, fn):
        with self._lock:
            self._subs[topic] = [s for s in self._subs.get(topic, []) if s.fn is not fn]

    def _snapshot(self, topic):
        with self._lock:
            return list(self._subs.get(topic, []))

    def emit(self, topic, **kw):
        """异步广播：立刻返回，订阅者各自在守护线程里跑，异常只进日志。"""
        for sub in self._snapshot(topic):
            if not sub.ready():
                continue

            def run(s=sub):
                try:
                    s.fn(**kw)
                    s.ok()
                except Exception as exc:
                    s.bad("%s: %s" % (type(exc).__name__, exc))
                    log(traceback.format_exc()[-400:])

            th = threading.Thread(target=run, name="bus-" + sub.name, daemon=True)
            th.start()

    def call(self, topic, timeout=None, **kw):
        """同步广播：全部并发跑，每个订阅者各自超时，谁失败都不抛。
        返回 [{"name", "ok", "ms", "value"}]，调用方自己决定怎么用。"""
        subs = self._snapshot(topic)
        if not subs:
            return []
        results = [None] * len(subs)
        threads = []
        for i, sub in enumerate(subs):
            if not sub.ready():
                results[i] = {"name": sub.name, "ok": False, "ms": 0, "value": None,
                              "skipped": "熔断中"}
                continue

            def run(idx=i, s=sub):
                t0 = time.time()
                try:
                    val = s.fn(**kw)
                    s.ok()
                    results[idx] = {"name": s.name, "ok": True,
                                    "ms": int((time.time() - t0) * 1000), "value": val}
                except Exception as exc:
                    s.bad("%s: %s" % (type(exc).__name__, exc))
                    results[idx] = {"name": s.name, "ok": False,
                                    "ms": int((time.time() - t0) * 1000), "value": None,
                                    "error": "%s: %s" % (type(exc).__name__, exc)}

            th = threading.Thread(target=run, name="bus-" + sub.name, daemon=True)
            th.start()
            threads.append((sub, th))

        hard = timeout or max([s.timeout for s, _ in threads] + [DEFAULT_TIMEOUT])
        end = time.time() + hard
        for sub, th in threads:
            th.join(max(0.0, min(sub.timeout, end - time.time())))
            if th.is_alive():
                sub.bad("超时 %ss" % sub.timeout)
        for i, sub in enumerate(subs):
            if results[i] is None:
                results[i] = {"name": sub.name, "ok": False, "ms": int(hard * 1000),
                              "value": None, "error": "超时"}
        return results

    def topics(self):
        with self._lock:
            return {k: [s.name for s in v] for k, v in self._subs.items()}


# 全局唯一：所有模块共用这一条
BUS = Bus()
