#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""记忆池存量清洗：event 近重复合并 / chat 保留 7 天且同话题只留一条。

用法：
    python tools/clean_pool.py --dry-run   # 只看计划，不写盘（默认）
    python tools/clean_pool.py --apply     # 真动手：先整份备份再改

红线：
- fact / rule / kb / style / profile / pin / lt 一个字都不碰
- 动手前必整份备份 memory-pool.json，备份路径会打印出来
- 只做合并与去重，合并时 hits 求和、ts 取最早（chat 取最新），绝不静默丢内容
"""
import argparse
import json
import os
import re
import shutil
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
from core.poollock import pool_lock  # noqa: E402  跨进程池锁

POOL = os.path.join(BASE, "memory-pool.json")
BACKUP_DIR = os.path.join(BASE, "backup")
TOUCH_KINDS = ("event", "chat")     # 只有这两类会被合并/裁剪
THRESH = 0.8
CHAT_DAYS = 7


def bg(s):
    s = re.sub(r"[\s，。！？、,.!?~…：:；;“”\"'()（）\[\]]+", "", s or "")
    return {s[i:i + 2] for i in range(len(s) - 1)}


def sim(a, b):
    x, y = bg(a), bg(b)
    if not x or not y:
        return 0.0
    return len(x & y) / float(len(x | y))


def topic_clusters(items, key):
    """按话题（user 文本）聚类，阈值放低到 0.5：'今天好累'/'今天好累啊' 算同一话题。"""
    out = []
    for it in sorted(items, key=lambda i: float(i.get("ts") or 0)):
        for cl in out:
            if any(sim(key(it), key(m)) >= 0.5 for m in cl):
                cl.append(it)
                break
        else:
            out.append([it])
    return out


def clusters(items):
    """贪心聚类：与簇内任一条相似度 >= THRESH 就并进去。"""
    out = []
    for it in sorted(items, key=lambda i: float(i.get("ts") or 0)):
        for cl in out:
            if any(sim(it.get("text") or "", m.get("text") or "") >= THRESH for m in cl):
                cl.append(it)
                break
        else:
            out.append([it])
    return out


def _run(args):
    """load -> 改 -> 原子替换 全段；调用方必须已经持有 pool_lock。"""
    with open(POOL, "r", encoding="utf-8") as f:
        doc = json.load(f)
    items = doc.get("items") or []
    before = len(items)

    keep, plan = [], []
    # ---------- event：近重复合并 ----------
    ev = [it for it in items if it.get("kind") == "event"]
    drop_ids = set()
    for cl in clusters(ev):
        if len(cl) == 1:
            continue
        base = max(cl, key=lambda i: len(i.get("text") or ""))
        hits = sum(int(i.get("hits") or 0) for i in cl)
        ts = min(float(i.get("ts") or 0) for i in cl)
        base["hits"] = hits
        base["ts"] = ts
        base["meta"] = dict(base.get("meta") or {}, merged=len(cl))
        plan.append(("event", ["%s(%s)" % (i["id"][:8], (i.get("text") or "")[:14]) for i in cl],
                     (base.get("text") or "")[:40], hits))
        for i in cl:
            if i is not base:
                drop_ids.add(i["id"])

    # ---------- chat：7 天 + 同话题一条 ----------
    cutoff = time.time() - CHAT_DAYS * 86400
    ch = [it for it in items if it.get("kind") == "chat"]
    older = [it for it in ch if float(it.get("ts") or 0) < cutoff]
    for it in older:
        drop_ids.add(it["id"])
    live = [it for it in ch if it["id"] not in drop_ids]
    by_scope = {}
    for it in live:
        by_scope.setdefault(it.get("scope") or "private", []).append(it)
    chat_merged = 0
    for it in live:
        _u = ((it.get("meta") or {}).get("user") or "")
        _u = re.sub(r"<system_reminder>.*?</system_reminder>", "", _u, flags=re.S).strip()
        _u = re.sub(r"\s*<\s*$", "", _u).strip()
        if _u != ((it.get("meta") or {}).get("user") or ""):
            it.setdefault("meta", {})["user"] = _u
            it["text"] = "对方：%s ｜ 你：%s" % (_u, (it.get("meta") or {}).get("assistant", ""))
    def topic(it):
        return ((it.get("meta") or {}).get("user") or it.get("text") or "")
    for scope, group in by_scope.items():
        for cl in topic_clusters(group, topic):
            if len(cl) == 1:
                continue
            base = max(cl, key=lambda i: float(i.get("ts") or 0))
            base["hits"] = sum(int(i.get("hits") or 0) for i in cl)
            base["meta"] = dict(base.get("meta") or {}, merged=len(cl))
            chat_merged += len(cl) - 1
            plan.append(("chat", ["%s(%s)" % (i["id"][:8], (i.get("meta") or {}).get("user", "")[:12]) for i in cl],
                         (base.get("text") or "")[:40], int(base["hits"])))
            for i in cl:
                if i is not base:
                    drop_ids.add(i["id"])

    # 只删允许的两类，核心永久类即使命中 id 也强制保留
    final = [it for it in items if it["id"] not in drop_ids or it.get("kind") not in TOUCH_KINDS]
    doc["items"] = final

    print("池子 %d -> %d（event 合并 %d 组 / chat 过期 %d 条 / chat 合并 %d 条）" % (
        before, len(final), sum(1 for p in plan if p[0] == "event"), len(older), chat_merged))
    for kind, ids, text, hits in plan[:20]:
        print("  [%s] %s => %s hits=%d" % (kind, " + ".join(ids), text, hits))
    if len(plan) > 20:
        print("  ...另有 %d 组" % (len(plan) - 20))
    core = [it for it in items if it.get("kind") not in TOUCH_KINDS]
    print("核心类保留 %d 条（fact/rule/kb/style/lt/profile），未参与任何合并" % len(core))
    if not args.apply:
        print("[dry-run] 没写盘。加 --apply 才动手（会先整份备份）。")
        return
    os.makedirs(BACKUP_DIR, exist_ok=True)
    dst = os.path.join(BACKUP_DIR, "memory-pool-before-clean-" + time.strftime("%Y%m%d-%H%M%S") + ".json")
    shutil.copy2(POOL, dst)
    tmp = POOL + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    os.replace(tmp, POOL)
    print("已写入。备份：%s" % dst)
    print("回滚：copy \"%s\" \"%s\"" % (dst, POOL))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="只出计划，不写盘（默认行为）")
    args = ap.parse_args()
    try:
        with pool_lock():
            return _run(args)
    except TimeoutError:
        print("[skip] pool busy")
        return 0


if __name__ == "__main__":
    main()
