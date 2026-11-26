# -*- coding: utf-8 -*-
"""精准定时提醒：存 reminders.json，到点由一条后台线程直接发 QQ。

约束（2026-11-13）：
- 只有 1 个 daemon 线程，靠 Condition.wait(到点差值) 睡到点，没有 while+sleep(1)；
- 新增/取消只 notify 唤醒，不空转；
- 重启：未过期的重新挂起，过期 <=5 分钟立刻补发，超过记 missed；
- 发送失败重试 1 次（隔 20 秒），仍失败记 failed，不吞。
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH = os.path.join(BASE, "reminders.json")
KEEP_DAYS = 7
LATE_GRACE = 300
LATE_MAX = 1800  # 迟到超过 30 分钟不再补发，直接记 missed

_cond = threading.Condition()
_items = []
_send = None
_started = False


def now():
    return time.time()


def _load():
    """读盘：文件不存在=空库返回 []；存在但读坏返回 None（调用方不得据此覆盖）。"""
    if not os.path.exists(PATH):
        return []
    try:
        with open(PATH, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    rows = []
    for it in data.get("items") or []:
        if isinstance(it, dict) and it.get("id") and isinstance(it.get("at"), (int, float)):
            rows.append(it)
    return rows


def _save():
    """原子写；调用方持锁。"""
    tmp = PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"items": _items}, f, ensure_ascii=False, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, PATH)


def _prune():
    cut = now() - KEEP_DAYS * 86400
    return [i for i in _items
            if i.get("status") == "pending"
            or float(i.get("fired_ts") or i.get("at") or 0) >= cut]


def add(at, message, uid=""):
    """挂一个提醒；at 是 epoch 秒。返回存下来的那条。"""
    item = {"id": uuid.uuid4().hex[:10], "at": round(float(at), 3),
            "message": (message or "").strip()[:100], "uid": str(uid or ""),
            "status": "pending", "created": now(), "fired_ts": 0, "tries": 0}
    with _cond:
        _items.append(item)
        _save()
        _cond.notify_all()
    return dict(item)


def pending():
    with _cond:
        return [dict(i) for i in _items if i.get("status") == "pending"]


def cancel(rid):
    with _cond:
        for i in _items:
            if i.get("id") == rid and i.get("status") == "pending":
                i["status"] = "canceled"
                i["fired_ts"] = now()
                _save()
                _cond.notify_all()
                return True
    return False


def _deliver(item):
    """发一条；失败重试一次（隔 20 秒）。"""
    text = "到点啦，%s" % item["message"]
    for attempt in (1, 2):
        try:
            if _send and _send(item.get("uid") or "", text, item.get("id") or ""):
                return True
        except Exception:
            pass
        if attempt == 1:
            time.sleep(20)
    return False


def _loop():
    while True:
        try:
            with _cond:
                t = now()
                due = [i for i in _items if i.get("status") == "pending" and float(i["at"]) <= t]
                if not due:
                    future = [float(i["at"]) for i in _items if i.get("status") == "pending"]
                    wait = (min(future) - t) if future else None
                    # Windows 的等待计时在睡眠期间不走，必须封顶重算，否则睡一夜会把
                    # 到点时刻整体推到几小时后（2026-11-14 早上的漏发就是这么来的）
                    if wait is None or wait > 60:
                        step = 60.0
                    elif wait > 0:
                        step = wait
                    else:
                        step = 0.05
                    _cond.wait(timeout=step)
                    continue
                keep = []
                changed = False
                for i in due:
                    if t - float(i["at"]) > LATE_MAX:
                        i["status"] = "missed"
                        i["fired_ts"] = t
                        changed = True
                        print("[reminder] missed id=%s late=%.0fs msg=%s"
                              % (i.get("id"), t - float(i["at"]), i.get("message")), flush=True)
                    else:
                        i["status"] = "firing"
                        keep.append(i)
                if changed:
                    _save()
                due = keep
            for i in due:
                ok = _deliver(i)
                with _cond:
                    i["status"] = "fired" if ok else "failed"
                    i["fired_ts"] = now()
                    i["tries"] = int(i.get("tries") or 0) + 1
                    _items[:] = _prune()
                    _save()
        except Exception:
            # 单条异常不许把整条调度线程带走；退避 1 秒继续
            time.sleep(1)

def start(send_fn, resume=True):
    """装载 + 起调度线程；返回重新挂起的条数。重复调用无副作用。"""
    global _send, _started
    if _started:
        return 0
    _send = send_fn
    _started = True
    with _cond:
        rows = _load()
        if rows is None:
            # 读坏了就不写盘：宁可这次少载几条，也不能把待办清空
            print("[reminder] unreadable, keep file as-is")
            rows = []
        _items[:] = rows
        t = now()
        changed = False
        for i in _items:
            if i.get("status") == "firing":
                # 上个进程发到一半被重启：放回待发，别让它永久失联
                i["status"] = "pending"
                changed = True
            if not resume or i.get("status") != "pending":
                continue
            if float(i["at"]) <= t - LATE_GRACE:
                i["status"] = "missed"
                i["fired_ts"] = t
                changed = True
        if changed:
            _save()
        threading.Thread(target=_loop, name="reminder", daemon=True).start()
        _cond.notify_all()
        return len([i for i in _items if i.get("status") == "pending"])
