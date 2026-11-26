# -*- coding: utf-8 -*-
"""小宠物的日记（30 分钟极简版）。

设计红线：
- 物理隔离：日记只写 diary/dafeiyu_diary.md，永不进 memory-pool.json；
  池子管"事实"，日记管"情绪与场景"，两边互不读取、互不压缩。
- 只追加：打开文件用 "a"，不重写、不排序、不删除；要改只能人工改。
- 检索只给命中的段落，单次硬上限 300 字，且必带日期前缀。
- 零依赖：正则 + 字符 bigram，扫全文；真超过 50 万字再上 FTS5。

CLI：
    python core/diary.py stats
    python core/diary.py read "以前那些累的事" [--k 3]
    python core/diary.py write "今天……"
"""
from __future__ import annotations

import os
import re
import sys
import time
from datetime import datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIARY_DIR = os.path.join(BASE, "diary")
DIARY = os.path.join(DIARY_DIR, "dafeiyu_diary.md")
LOCK = os.path.join(DIARY_DIR, "diary.lock")

CAP = 300
THRESH = 0.6
MIN_SHARED = 3     # 正文太大，只有 >=3 个共享 bigram 才算真命中（防"现在几点了"这种撞词）
MAX_K = 3

ENTRY_RE = re.compile(r"^-\s+(\d{4}-\d{2}-\d{2})(?:\s+(\d{2}:\d{2}))?\s+(.*)$")
KW_RE = re.compile(r"<!--\s*kw:\s*([^>]*?)\s*-->")


HEAD = (
    "# 小宠物的日记\n\n"
    "> 只追加。每段格式：- YYYY-MM-DD HH:MM 正文\n"
    "> 这是她的私人记忆，不是事实池；引用时必须带日期。\n\n"
)


def _punct(s):
    return re.sub(r"[\s，。！？、,.!?~～…：:；;“”\"'()（）\[\]【】—\-]+", "", s or "")


def _bigrams(s):
    t = _punct(s)
    if len(t) > 1:
        return {t[i:i + 2] for i in range(len(t) - 1)}
    return {t} if t else set()


def _ensure():
    os.makedirs(DIARY_DIR, exist_ok=True)
    if not os.path.exists(DIARY):
        with open(DIARY, "w", encoding="utf-8") as f:
            f.write(HEAD)


def _lock(timeout=5.0):
    if BASE not in sys.path:      # 直接 python core/diary.py 时 sys.path[0] 是 core/
        sys.path.insert(0, BASE)
    from core.poollock import pool_lock
    return pool_lock(timeout=timeout, path=LOCK)


def stamp(at=None):
    d = datetime.fromtimestamp(at if at else time.time())
    return "%s %s" % (d.strftime("%Y-%m-%d"), d.strftime("%H:%M"))


def append(text, at=None, note="", kw=""):
    """追加一段日记，返回日期前缀。空或过短直接忽略。"""
    body = (text or "").strip()
    body = re.sub(r"\s*\n\s*", " ", body)
    if len(body) < 4:
        return ""
    line = "- %s %s" % (stamp(at), body)
    tags = []
    if kw:
        kws = [k.strip() for k in re.split(r"[,，;；、/|]+", kw) if k.strip()]
        if kws:
            tags.append("kw: " + ",".join(kws[:8]))
    if note:
        tags.append(note.replace("-->", "->"))
    if tags:
        line += "  <!-- %s -->" % "; ".join(tags)
    _ensure()
    with _lock():
        with open(DIARY, "a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())
    return "- " + stamp(at)


def paragraphs(with_kw=False):
    """[(date, time, body, kw), ...]（with_kw=False 时只返回前三个），只认 '- YYYY-MM-DD' 行。"""
    _ensure()
    out = []
    with open(DIARY, "r", encoding="utf-8") as f:
        for line in f:
            m = ENTRY_RE.match(line.strip())
            if m:
                body = m.group(3).strip()
                km = KW_RE.search(body)
                kw = km.group(1) if km else ""
                body = KW_RE.sub("", body).strip()
                out.append((m.group(1), m.group(2) or "", body, kw))
    if with_kw:
        return out
    return [(a, b, c) for a, b, c, _k in out]


def search(query, k=MAX_K, cap=CAP, thresh=THRESH):
    """字符 bigram 覆盖率召回，返回带日期前缀的段落，每条 <= cap 字。"""
    q = _punct(query or "")
    if not q:
        return []
    if len(q) < 2:                      # 单字查询（"累"/"猫"）bigram 无效，退化成子串命中
        return ["%s： %s" % (d, b[:cap - 12]) for d, _t, b in paragraphs() if q in b][:max(1, min(k, MAX_K))]
    fq = _bigrams(q)
    rows = []
    for d, t, body, kw in paragraphs(with_kw=True):
        fb = _bigrams(body)
        hits = len(fq & fb)
        cover = hits / float(len(fq)) if fq else 0.0
        ok = hits >= MIN_SHARED and cover >= thresh
        if not ok and kw:                       # 关键词整词命中：精确、不会撞"现在几点了"
            for kwit in re.split(r"[,，;；、/|]+", kw):
                kwit = kwit.strip()
                if not kwit:
                    continue
                bg = _bigrams(kwit)
                head = _punct(kwit)[:2]
                if kwit in q or (len(head) == 2 and head in fq) or len([b for b in bg if b in fq]) >= 2:
                    ok = True
                    cover = 1.0
                    break
        if ok:
            rows.append((cover, d, t, body))
    rows.sort(key=lambda r: (-r[0], -time.mktime(time.strptime(r[1], "%Y-%m-%d"))))
    out = []
    for cover, d, t, body in rows[:max(1, min(k, MAX_K))]:
        head = "%s： " % d
        room = cap - len(head)
        out.append(head + (body[:room] if len(body) > room else body))
    return out


def stats():
    ps = paragraphs()
    return {"entries": len(ps), "chars": sum(len(b) for _, _, b in ps),
            "days": len({d for d, _, _ in ps}),
            "path": DIARY,
            "bytes": os.path.getsize(DIARY) if os.path.exists(DIARY) else 0}


def _main(argv):
    cmd = (argv[1] if len(argv) > 1 else "stats").lower()
    if cmd == "stats":
        s = stats()
        print("日记 %d 段 / %d 天 / %d 字 / %d 字节" % (s["entries"], s["days"], s["chars"], s["bytes"]))
        print(s["path"])
        return 0
    if cmd == "read":
        args = [a for a in argv[2:] if not a.startswith("--")]
        rows = search(" ".join(args))
        print("\n".join(rows) if rows else "（日记里没有和「%s」沾边的段落）" % " ".join(args))
        return 0
    if cmd == "write":
        text = " ".join(argv[2:]).strip() or sys.stdin.read()
        print("已写入 " + (append(text) or "（空，忽略）"))
        return 0
    print("usage: diary.py stats|read <query>|write <text>")
    return 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
