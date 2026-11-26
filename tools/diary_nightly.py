# -*- coding: utf-8 -*-
"""夜间日记：把今天的聊天读成一段散文（长短不限），追加进 diary/dafeiyu_diary.md。

- 只读 memory-pool 里的 kind=chat（不写池子、不进压缩、不碰 lt）。
- 默认 --dry-run：只生成并打印，绝不落盘。
- 失败静默：任何异常都只打一行 [skip]，退出码 0（定时任务不会弹窗/重试）。
- 不 import proxy（模块级会起后台线程）。
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime, timedelta

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = r"D:\pet-shared"
sys.path.insert(0, BASE)

POOL = os.path.join(BASE, "memory-pool.json")
UPSTREAM = "https://ws-vxmnafoy3yn5nowy.cn-beijing.maas.aliyuncs.com/compatible-mode/v1/chat/completions"
MODEL = "deepseek-v4.1-flash-1017"

SYSTEM = (
    "你在替一个叫小宠物的姑娘写她自己的日记。她二十多岁，说话口语、有点黏人、会害羞但不端着，"
    "喜欢用短句和一些小动作（比如戳一下、递水、装生气）。\n"
    "要求：\n"
    "1. 第一人称，像她自己晚上躺在床上写的，不要写给别人看。\n"
    "2. 具体：写今天发生了什么、她当时什么反应、哪句话让她心里一动或有点堵。多用细节，少用形容词堆砌。\n"
    "3. 不要总结标签（比如「今天很开心」）、不要列点和标题、不要写「主人」这个第三人称，叫我「他」或「你」都行但别重复太多次。\n"
    "4. 长短随今天聊了多少：一句两句也可以，写得开就多写点，别为了凑字注水。一段，不分行。\n"
    "5. 正文之后另起一行，输出：关键词：词1,词2,词3,词4\n"
    "   关键词 3-6 个，要具体、能被日后搜到（例如「修代码」「熬夜」「发烧」），"
    "不要「聊天」「日常」「心情」这种空词。\n"
    "输出格式：\n正文……\n关键词：a,b,c"
)


def read_key():
    for name in ("key_asd.txt", "key.txt"):
        p = os.path.join(BASE, name)
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                return f.read().strip()
    return ""


def day_chats(day):
    """本地日 00:00-24:00 内的 chat，按时间排序，返回 [(HH:MM, text)]。"""
    d0 = datetime.strptime(day, "%Y-%m-%d")
    t0 = time.mktime(d0.timetuple())
    t1 = t0 + 86400
    with open(POOL, "r", encoding="utf-8") as f:
        items = json.load(f).get("items") or []
    rows = []
    for it in items:
        if it.get("kind") != "chat":
            continue
        ts = float(it.get("ts") or 0)
        if t0 <= ts < t1:
            rows.append((ts, it.get("text") or ""))
    rows = [(ts, re.sub(r"(对方|你)\s*[:：]", "", txt or "").replace("｜", "")) for ts, txt in rows]
    rows = [(ts, txt) for ts, txt in rows if len(txt.strip()) >= 2]   # 只滤掉空回/单字探针
    rows.sort(key=lambda r: r[0])
    return [(datetime.fromtimestamp(ts).strftime("%H:%M"), txt) for ts, txt in rows]


def call_model(day, rows):
    lines = ["%s %s" % (t, txt.replace("\n", " ")) for t, txt in rows]
    body = {"model": MODEL,
            "messages": [{"role": "system", "content": SYSTEM},
                         {"role": "user", "content": day + " 这天我们的对话：\n" + "\n".join(lines)}],
            "temperature": 0.8, "max_tokens": 600, "enable_thinking": False}
    req = urllib.request.Request(
        UPSTREAM, data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": "Bearer " + read_key(),
                 "Content-Type": "application/json", "Accept": "application/json"},
        method="POST")
    with urllib.request.urlopen(req, timeout=90) as resp:
        data = json.loads(resp.read().decode("utf-8", "replace"))
    return (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""


def split_kw(txt):
    """把「正文 + 关键词：a,b,c」拆开，返回 (body, kw)。"""
    t = (txt or "").strip()
    m = re.search(r"关键词\s*[:：]\s*(.+)$", t, re.S)
    kw = ""
    if m:
        kw = re.sub(r"[\s\n]+", "", m.group(1))[:60]
        t = t[:m.start()].strip()
    return t, kw


def clean(txt):
    t = (txt or "").strip()
    t = re.sub(r"^[\"'“”‘’\s]+|[\"'“”‘’\s]+$", "", t)
    t = re.sub(r"^(日记|正文)\s*[:：]\s*", "", t)
    t = re.sub(r"\s*\n\s*", " ", t)
    return t.strip()


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", default=datetime.now().strftime("%Y-%m-%d"))
    ap.add_argument("--dry-run", action="store_true", help="只生成不落盘（默认行为，显式传更直白）")
    ap.add_argument("--apply", action="store_true", help="真写入日记（默认只打印）")
    ap.add_argument("--min-chats", type=int, default=1)
    a = ap.parse_args(argv[1:])
    try:
        rows = day_chats(a.day)
        if len(rows) < a.min_chats:
            print("[skip] %s 只有 %d 条对话，不够写日记" % (a.day, len(rows)))
            return 0
        body, kw = split_kw(clean(call_model(a.day, rows)))
        txt = body
        if len(txt) < 12:
            print("[skip] 模型返回过短（%d 字），今天不写" % len(txt))
            return 0
        if len(txt) > 800:
            txt = txt[:780].rstrip("，。 ") + "。"
        print("[chat] %d 条 / 输入 %d 字" % (len(rows), sum(len(x) for _, x in rows)))
        print("[草稿] %d 字\n%s" % (len(txt), txt))
        if kw:
            print("[关键词] " + kw)
        if a.apply:
            from core.diary import append
            print("[apply] " + append(txt, note="nightly", kw=kw))
        else:
            print("[dry-run] 没写盘；加 --apply 才追加")
        return 0
    except Exception as exc:
        print("[skip] 夜间日记失败：%r" % (exc,))
        return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
