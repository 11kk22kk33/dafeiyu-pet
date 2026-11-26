# -*- coding: utf-8 -*-
"""护栏：任何一次外部调用（TTS、截图、视频、插件）都必须从这里过。
超时、异常、熔断都在这一层吃掉，调用方永远拿到一个「能用或 None」，不会被卡死。"""
import os
import threading
import time

LOG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "core.log")
FAIL_LIMIT = 3
COOL = 60.0


def log(msg):
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (time.strftime("%H:%M:%S"), msg))
    except Exception:
        pass


class Breaker(object):
    """连续失败就歇一会儿，别每条消息都去撞同一堵墙。"""

    def __init__(self, name, fail_limit=FAIL_LIMIT, cool=COOL):
        self.name = name
        self.fail_limit = fail_limit
        self.cool = cool
        self.fails = 0
        self.open_until = 0.0
        self.last_error = ""

    def ready(self):
        if time.time() >= self.open_until:
            return True
        return False

    def ok(self):
        if self.fails:
            log("恢复 %s" % self.name)
        self.fails = 0
        self.last_error = ""

    def bad(self, why):
        self.fails += 1
        self.last_error = why
        if self.fails >= self.fail_limit:
            self.open_until = time.time() + self.cool
            log("熔断 %s（连续 %d 次，歇 %ds）: %s" % (self.name, self.fails, self.cool, why))
        else:
            log("出错 %s: %s" % (self.name, why))


_BREAKERS = {}
_LOCK = threading.Lock()


def breaker(name):
    with _LOCK:
        b = _BREAKERS.get(name)
        if b is None:
            b = _BREAKERS[name] = Breaker(name)
        return b


def call(name, fn, *args, timeout=20.0, default=None, **kwargs):
    """带超时与熔断地跑一件事；出错/超时/熔断都返回 default，绝不抛异常。"""
    br = breaker(name)
    if not br.ready():
        log("跳过 %s（熔断中）" % name)
        return default
    box = {"v": default}
    t0 = time.time()

    def run():
        try:
            box["v"] = fn(*args, **kwargs)
            br.ok()
        except Exception as exc:
            br.bad("%s: %s" % (type(exc).__name__, exc))

    th = threading.Thread(target=run, name="guard-" + name, daemon=True)
    th.start()
    th.join(timeout)
    if th.is_alive():
        br.bad("超时 %.0fs" % timeout)
        return default
    ms = int((time.time() - t0) * 1000)
    if ms > 3000:
        log("%s 慢 %dms" % (name, ms))
    return box["v"]


def spawn(name, fn, *args, timeout=60.0, **kwargs):
    """丢到后台跑，别挡住回复。异常和超时同样被吃掉。"""
    th = threading.Thread(target=call, args=(name, fn) + tuple(args),
                          kwargs={"timeout": timeout, **kwargs}, daemon=True)
    th.start()
    return th


def wrap(name, timeout=20.0, default=None):
    """装饰器写法：@guard.wrap("vision", timeout=15)"""
    def deco(fn):
        def inner(*a, **kw):
            return call(name, fn, *a, timeout=timeout, default=default, **kw)
        inner.__name__ = getattr(fn, "__name__", name)
        return inner
    return deco


def states():
    with _LOCK:
        return {k: {"fails": v.fails, "open": not v.ready(),
                    "last_error": v.last_error} for k, v in _BREAKERS.items()}
