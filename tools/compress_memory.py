#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""记忆池压缩：池里 chat 超过阈值（默认 120 条）时，把最旧的 N 条（默认 50）chat 压成 1 条长期记忆。

用法：
    python tools/compress_memory.py            # 正常跑：超阈值才动手
    python tools/compress_memory.py --dry-run  # 只看会压什么，不落盘
    python tools/compress_memory.py --force    # 不管多少条都压一次

红线：动手前一定先备份整份 pool；合并只做归并，绝不凭空丢弃记忆内容。
"""
import argparse
import hashlib
import json
import os
import shutil
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
from core.poollock import pool_lock  # noqa: E402  跨进程池锁

POOL = os.path.join(BASE, "memory-pool.json")
LT_FILE = os.path.join(BASE, "longterm.json")

# 核心事实/规则/知识/风格永远不压，只压流水账
PERMANENT = ("fact", "rule", "kb", "profile", "pin", "style")
# event 不再被压缩：事件交给 lt_consolidate 的 op 契约（ADD/UPD/DEL）管，避免"合并后语义丢失"
COMPRESSIBLE = ("chat",)


def backup_dir():
    cand = os.path.join(os.path.dirname(BASE), "小宠物的自留地", "05-备份", "记忆历史")
    if os.path.isdir(os.path.dirname(cand)):
        return cand
    return os.path.join(BASE, "backup")


def load():
    with open(POOL, "r", encoding="utf-8") as f:
        return json.load(f)


def save(doc):
    tmp = POOL + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    os.replace(tmp, POOL)


def backup():
    d = backup_dir()
    os.makedirs(d, exist_ok=True)
    name = "memory-pool-before-compress-" + time.strftime("%Y%m%d-%H%M%S") + ".json"
    dst = os.path.join(d, name)
    shutil.copy2(POOL, dst)
    return dst


def summarize(group):
    """把一组旧记忆压成一条；按 scope 分开，私聊内容绝不流进群记忆。"""
    by_scope = {}
    for it in group:
        by_scope.setdefault(it.get("scope") or "private", []).append(it)
    merged = []
    for scope, items in by_scope.items():
        items = sorted(items, key=lambda x: x.get("ts") or 0)
        texts, seen = [], set()
        for it in items:
            t = (it.get("text") or "").strip()
            if not t or t in seen:
                continue
            seen.add(t)
            texts.append(t)
        days = [time.strftime("%m-%d", time.localtime(it.get("ts") or 0)) for it in items]
        span = (days[0] + "→" + days[-1]) if days else ""
        body = "；".join(texts)
        if len(body) > 700:
            body = body[:700] + "…"
        ts = max((it.get("ts") or 0) for it in items)
        rid = "cmp" + hashlib.md5((scope + span + body[:60]).encode("utf-8")).hexdigest()[:10]
        merged.append({
            "id": rid,
            "scope": scope,
            "kind": "lt",
            "text": "（" + span + " 的旧记忆合并，共 " + str(len(items)) + " 条）" + body,
            "ts": ts,
            "hits": sum((it.get("hits") or 0) for it in items),
            "important": 0.45,
            "status": "active",
            "source": "compress",
            "meta": {"merged": len(items)},
        })
    return merged


def load_cursor():
    """lt_consolidate 的游标：可能是 float，也可能是 {scope: ts}；读不到返回 None（不卡时间线）。"""
    try:
        with open(LT_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(d, dict):
        return None
    c = d.get("cursor")
    if isinstance(c, dict):
        return {str(k): float(v) for k, v in c.items() if isinstance(v, (int, float))}
    if isinstance(c, (int, float)):
        return float(c)
    return None


def within_cursor(it, cur):
    """能读到游标时只压游标之前的 chat（游标之后的还没被 consolidate，压了等于吞掉）。"""
    if cur is None:
        return True
    c = cur.get(it.get("scope") or "private") if isinstance(cur, dict) else cur
    if c is None:
        return True
    return float(it.get("ts") or 0) <= float(c)


def absorb_lt(items, merged):
    """同一 scope 只留 1 条 lt：池里已有就更新成「旧 lt 文本 + 新合并文本」，绝不 +1。"""
    keep, fresh = list(items), []
    for m in merged:
        old = None
        for it in keep:
            if (it.get("kind") == "lt" and it.get("scope") == m.get("scope")
                    and (it.get("status") or "active") == "active"):
                old = it
                break
        if old is None:
            fresh.append(m)
            continue
        old_text = (old.get("text") or "").strip()
        new_text = (m.get("text") or "").strip()
        text = (old_text + "；" + new_text) if old_text else new_text
        if len(text) > 1500:          # 摘要不能无限长胖，超了留最新的尾巴
            text = text[-1500:]
        old["text"] = text
        old["ts"] = max(float(old.get("ts") or 0), float(m.get("ts") or 0))
        old["hits"] = int(old.get("hits") or 0) + int(m.get("hits") or 0)
        old_meta = old.get("meta") or {}
        new_meta = m.get("meta") or {}
        old["meta"] = dict(old_meta,
                           merged=int(old_meta.get("merged") or 0) + int(new_meta.get("merged") or 0))
        old["source"] = "compress"
    return keep + fresh


def run(a):
    """load -> 改 -> save 全段；调用方必须已经持有 pool_lock。"""
    doc = load()
    items = doc.get("items") or []
    total = len(items)
    chats = [it for it in items
             if it.get("kind") == "chat" and (it.get("status") or "active") == "active"]
    print("当前记忆池：" + str(total) + " 条（其中 chat " + str(len(chats)) + " 条）")
    if len(chats) <= a.threshold and not a.force:
        print("chat 只有 " + str(len(chats)) + " 条，没超过 " + str(a.threshold) + " 条，不用压。")
        return 0

    now = time.time()
    cur = load_cursor()
    cand = [it for it in items
            if (it.get("kind") or "") in COMPRESSIBLE
            and (it.get("status") or "active") == "active"
            and float(it.get("ts") or 0) < now - 3600
            and float(it.get("important") or 0) < 0.6
            and within_cursor(it, cur)]
    cand.sort(key=lambda x: x.get("ts") or 0)
    batch = cand[:a.batch]
    if len(batch) < 2:
        print("可压缩的旧流水只有 " + str(len(batch)) + " 条，先不动。")
        return 0

    merged = summarize(batch)
    drop_ids = {it.get("id") for it in batch}
    keep = [it for it in items if it.get("id") not in drop_ids]
    new_items = absorb_lt(keep, merged)
    doc["items"] = new_items

    reused = len(merged) - (len(new_items) - len(keep))
    print("压缩：" + str(len(batch)) + " 条 → " + str(len(merged)) + " 条（新 lt " + str(len(merged) - reused)
          + " / 更新已有 lt " + str(reused) + "），池子 " + str(total) + " → " + str(len(doc["items"])))
    if a.dry_run:
        print("[dry-run] 没写盘。备份会放在：" + backup_dir())
        for m in merged:
            print("  · " + (m.get("text") or "")[:80])
        return 0

    bp = backup()
    print("备份：" + bp)
    save(doc)
    print("完成，池子现在 " + str(len(doc["items"])) + " 条。")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--threshold", type=int, default=120, help="chat 条数阈值（不是池子总条数）")
    ap.add_argument("--batch", type=int, default=50)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--apply", action="store_true", help="真写盘（本来就是默认行为，留着兼容）")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    try:
        with pool_lock():
            return run(a)
    except TimeoutError:
        # 拿不到锁就一个字都不写：宁可这轮不压，也不许覆盖别人的新记忆
        print("[skip] pool busy")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())