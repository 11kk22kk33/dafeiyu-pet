# -*- coding: utf-8 -*-
"""一次性语义去重：只整理 kind == "event" 的池子条目。

设计要点：
- 不 import proxy（模块级会起后台线程），只照抄它的上行写法（UPSTREAM / KEY_FILE /
  temperature=0 / max_tokens<=300 / enable_thinking=False），用 urllib 直接发。
- 全部 event 条目连着短 id 一次发给模型（一次请求，不按对循环），模型只回 DEL|id / KEEP|id。
- 默认 --dry-run：打印原始输出 + 计划；--apply 才备份、归档、原子写回。
- 安全闸：只删 event；指向别的 kind 的 id 忽略并记日志；解析出 0 条 DEL 不写盘。
"""
import argparse
import collections
import json
import os
import re
import shutil
import sys
import time
import urllib.request

BASE = r"D:\pet-shared"
sys.path.insert(0, BASE)
from core.poollock import pool_lock  # noqa: E402  跨进程池锁

POOL = os.path.join(BASE, "memory-pool.json")
ARCHIVE = os.path.join(BASE, "memory-pool-archive.jsonl")
BACKUP_DIR = os.path.join(BASE, "backup")

# 照抄 proxy.py：74-76 行 的上行地址与模型；199-201 行 的 key 读取顺序
UPSTREAM = "https://ws-vxmnafoy3yn5nowy.cn-beijing.maas.aliyuncs.com/compatible-mode/v1/chat/completions"
MODEL = "deepseek-v4.1-flash-1017"

SYSTEM = (
    "你在给长期记忆池做语义去重。下面每行是 [短id] + 一条 event 原文。\n"
    "只判断：哪些行是同一件事的重复表述（意思基本一致、只是换了说法）。\n"
    "同一件事只留信息量最大的一条（细节最多、最具体），被留的一律 KEEP。\n"
    "不是同一件事的（哪怕时间相近、话题相关）都 KEEP，不要为了少条目而合并。\n"
    "输出格式：只输出逐行操作，一行一个，不要解释、不要编号、不要复述原文：\n"
    "DEL|短id\n"
    "KEEP|短id\n"
    "必须给每一行一条结论。"
)


def read_key():
    for name in ("key_asd.txt", "key.txt"):
        p = os.path.join(BASE, name)
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                return f.read().strip()
    raise SystemExit("no key file: key_asd.txt / key.txt")


def log(msg):
    print(msg, flush=True)


def load_pool():
    with open(POOL, "r", encoding="utf-8") as f:
        return json.load(f)


def atomic_write(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def short(it):
    return str(it.get("id") or "")[:8].lower()


def call_model(rows):
    body = {"model": MODEL,
            "messages": [{"role": "system", "content": SYSTEM},
                         {"role": "user", "content": "\n".join(rows)}],
            "temperature": 0, "max_tokens": 300,
            "enable_thinking": False}
    req = urllib.request.Request(
        UPSTREAM, data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": "Bearer " + read_key(),
                 "Content-Type": "application/json", "Accept": "application/json"},
        method="POST")
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode("utf-8", "replace"))
    return (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""


LINE_RE = re.compile(r"^[\s\-\*>\.\)]*\s*(DEL|KEEP)\s*[|｜:：,，;；\s]+\s*([0-9A-Za-z]{4,40})",
                     re.IGNORECASE)


def parse(txt):
    plan = []
    for raw in (txt or "").replace("\r", "\n").split("\n"):
        line = raw.strip().replace("｜", "|")
        if not line:
            continue
        m = LINE_RE.match(line)
        if not m:
            continue  # 读不懂的行直接丢
        plan.append((m.group(1).upper(), m.group(2).lower()))
    return plan


def resolve(shortid, by_short):
    if shortid in by_short:
        return by_short[shortid]
    hit = [v for k, v in by_short.items() if k.startswith(shortid) or shortid.startswith(k)]
    return hit[0] if len(hit) == 1 else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真的写盘（默认 dry-run）")
    ap.add_argument("--dry-run", action="store_true", help="默认行为，显式写出来也行")
    args = ap.parse_args()
    apply = bool(args.apply)

    pool = load_pool()
    items = pool.get("items") or []
    events = [it for it in items if it.get("kind") == "event"]
    if not events:
        log("no event items; nothing to do")
        return 0
    by_short = {}
    for it in events:
        if short(it):
            by_short[short(it)] = it
    rows = ["%s %s" % (short(it), (it.get("text") or "").replace("\n", " ").strip())
            for it in events if short(it)]
    log("events=%d  one request -> %s" % (len(events), MODEL))
    for r in rows:
        log("  - " + r)

    txt = call_model(rows)
    log("\n===== model raw output =====")
    log(txt if txt.strip() else "(empty)")
    log("============================")

    plan = parse(txt)
    del_items, keep_items, ignored = [], [], []
    seen = set()
    for op, sid in plan:
        it = resolve(sid, by_short)
        if it is None:
            ignored.append((op, sid, "unresolved id"))
            continue
        if it.get("kind") != "event":
            ignored.append((op, sid, "kind=%s not event" % it.get("kind")))
            continue
        key = str(it.get("id"))
        if key in seen:
            continue
        seen.add(key)
        (del_items if op == "DEL" else keep_items).append(it)

    # 安全闸：绝不允许把 event 全删光（至少留一条信息量最大的）
    if del_items and len(del_items) >= len(events):
        longest = max(events, key=lambda it: len(it.get("text") or ""))
        del_items = [it for it in del_items if it is not longest]
        log("[warn] plan deleted every event -> force KEEP %s" % short(longest))

    if ignored:
        log("\n[ignored lines]")
        for op, sid, why in ignored:
            log("  %s|%s  -> %s" % (op, sid, why))

    del_ids = {str(it.get("id")) for it in del_items}
    log("\n计划删除 %d 条" % len(del_items))
    for it in del_items:
        log("  DEL %s %s" % (short(it), it.get("text")))
    log("保留 %d 条" % (len(events) - len(del_items)))
    for it in events:
        if str(it.get("id")) not in del_ids:
            log("  KEEP %s %s" % (short(it), it.get("text")))

    if not del_items:
        log("\n0 DEL -> 不写盘")
        return 0
    if not apply:
        log("\n[dry-run] 未写盘；加 --apply 才动手")
        return 0

    # 写盘段（重新 load -> 备份/归档 -> os.replace）全段持锁；模型调用留在锁外，别堵住真池
    try:
        with pool_lock():
            os.makedirs(BACKUP_DIR, exist_ok=True)
            ts = time.strftime("%Y%m%d-%H%M%S")
            bak = os.path.join(BACKUP_DIR, "memory-pool-before-dedupe-%s.json" % ts)
            shutil.copy2(POOL, bak)
            log("\n[backup] " + bak)

            with open(ARCHIVE, "a", encoding="utf-8") as f:
                for it in del_items:
                    f.write(json.dumps(it, ensure_ascii=False) + "\n")
            log("[archive] %s (appended %d)" % (ARCHIVE, len(del_items)))

            pool = load_pool()                    # 锁内重读，别覆盖别人刚写的新记忆
            pool["items"] = [it for it in (pool.get("items") or [])
                             if str(it.get("id")) not in del_ids]
            atomic_write(POOL, pool)
    except TimeoutError:
        log("[skip] pool busy")
        return 0

    c = collections.Counter(it.get("kind") for it in pool["items"])
    log("[done] pool now %d items  %s" % (len(pool["items"]), dict(c)))
    log("\nrollback: copy \"%s\" \"%s\"" % (bak, POOL))
    return 0


if __name__ == "__main__":
    sys.exit(main())
