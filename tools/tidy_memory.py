# -*- coding: utf-8 -*-
"""记忆池瘦身：只删纯招呼/应答的废话和完全重复的条目，动手前先备份。

用法：
    python tools/tidy_memory.py --dry-run   # 只看会删什么
    python tools/tidy_memory.py             # 真删

红线：永久类（fact/rule/kb/profile/pin/style）、important>=0.6、以及每个作用域最后的
20 条一律不动；绝不整池清空。
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
BACKUP = os.path.join(BASE, "backup")
KEEP_KINDS = ("fact", "rule", "kb", "profile", "pin", "style")
KEEP_LAST = 20
JUNK = re.compile(
    r"^(?:[哈嘿呵嗯哦啊额呃]{1,6}[。！!…~～]*|早安|早上好|午安|晚安|在吗|在不在|在么|"
    r"好的?|行|收到|了解|明白|拜拜|再见|88|谢谢|谢啦|你好|哈喽|hi|hello|ok|okay|"
    r"[?？]+|[.。…]+|[1１]+|[0-9]{1,2})$",
    re.I,
)


def load():
    with open(POOL, "r", encoding="utf-8") as f:
        return json.load(f)


def items_of(doc):
    for scope, val in (doc or {}).items():
        if isinstance(val, dict):
            yield scope, val, val.get("items") or []
        elif isinstance(val, list):
            yield scope, None, val


def save(doc):
    tmp = POOL + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    os.replace(tmp, POOL)


def pick(doc):
    """返回 {scope: set(要删的 id)} 和明细。"""
    drop, detail = {}, []
    for scope, val, items in items_of(doc):
        keep_ids, seen = set(), {}
        for it in items:
            text = (it.get("text") or "").strip()
            if text:
                seen.setdefault(text, []).append(it)
        for it in items:
            text = (it.get("text") or "").strip()
            iid = it.get("id")
            if not iid:
                continue
            kind = (it.get("kind") or "").lower()
            if kind in KEEP_KINDS or float(it.get("important") or 0) >= 0.6:
                keep_ids.add(iid)
                continue
            dup = seen.get(text) or []
            if text and len(dup) > 1:
                newest = max(dup, key=lambda x: float(x.get("ts") or 0))
                if newest.get("id") != iid:
                    drop.setdefault(scope, set()).add(iid)
                    detail.append((scope, "重复", text))
                    continue
            if text and len(text) <= 12 and JUNK.match(text):
                drop.setdefault(scope, set()).add(iid)
                detail.append((scope, "废话", text))
    # 每个作用域至少留 KEEP_LAST 条：从新到旧标够数就收手
    for scope, val, items in items_of(doc):
        gone = drop.get(scope) or set()
        room = len(items) - len(gone) - KEEP_LAST
        if room >= 0:
            continue
        need = -room
        recent = sorted(items, key=lambda x: float(x.get("ts") or 0), reverse=True)
        for it in recent:
            if need <= 0:
                break
            if it.get("id") in gone:
                gone.discard(it.get("id"))
                need -= 1
        if not gone:
            drop.pop(scope, None)
    return drop, detail


def _run(args):
    """load -> 改 -> save 全段；调用方必须已经持有 pool_lock。"""
    doc = load()
    before = sum(len(v) for _, _, v in items_of(doc))
    drop, detail = pick(doc)
    n = sum(len(s) for s in drop.values())
    print("当前 %d 条，可删 %d 条" % (before, n))
    for scope, why, text in detail[:20]:
        print("  [%s] %s %s" % (scope, why, text[:30]))
    if args.dry_run or not n:
        print("dry-run，不动盘" if args.dry_run else "没什么可删")
        return 0
    os.makedirs(BACKUP, exist_ok=True)
    bak = os.path.join(BACKUP, "memory-pool.%s.json" % time.strftime("%Y%m%d-%H%M%S"))
    shutil.copyfile(POOL, bak)
    print("备份 ->", bak)
    for scope, val, items in items_of(doc):
        gone = drop.get(scope) or set()
        if not gone:
            continue
        kept = [it for it in items if it.get("id") not in gone]
        if isinstance(val, dict):
            val["items"] = kept
        else:
            items[:] = kept
    save(doc)
    after = sum(len(v) for _, _, v in items_of(doc))
    print("删完：%d -> %d 条（备份 %s）" % (before, after, os.path.basename(bak)))
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    try:
        with pool_lock():
            return _run(args)
    except TimeoutError:
        print("[skip] pool busy")
        return 0


if __name__ == "__main__":
    sys.exit(main())
