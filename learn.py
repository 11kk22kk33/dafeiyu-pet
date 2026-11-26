# -*- coding: utf-8 -*-
"""小宠物的学习模块（模块四）。

两件事，都是纯本地、不调模型、不花 token：
1. 学主人说话的样子 —— 每轮悄悄统计句长、标点癖好、口头禅、怎么称呼你、最近爱聊什么，
   汇成一小段「说话习惯」，再由 proxy 拼进 system，让小宠物自己慢慢往那个味儿靠。
2. 学做事（以后装上手打游戏用）—— 一个技能库：观察到的动作序列、练过几次、成没成，
   熟练度用贝叶斯平滑算，模块五（理解画面）拿到熟练度和步骤就能开练。

数据都在 learn/ 下面：
  profile.json  说话习惯画像
  skills.json   技能库
"""
import json
import os
import re
import time

BASE = os.path.dirname(os.path.abspath(__file__))
LEARN_DIR = os.path.join(BASE, "learn")
PROFILE = os.path.join(LEARN_DIR, "profile.json")
SKILLS = os.path.join(LEARN_DIR, "skills.json")

UID_RE = re.compile(r"\[\[dfy:uid=\d+;nick=[^\]]*\]\]")

# 有点味道的语气词/口头禅候选：命中了才记，比全量 n-gram 干净
MARKERS = ("嘛", "呀", "哈", "唉", "诶", "欸", "哦", "噢", "嘞", "啦", "哎", "咯", "呜呜",
           "嘿嘿", "嘻嘻", "哼", "啧", "嗯", "好家伙", "真的", "其实", "反正", "就是说",
           "然后", "好吧", "算了", "天呐", "救命", "绝了", "离谱", "破防", "绷不住",
           "咋", "啥", "整", "搞", "明明", "估计", "大概")
# 常见称呼主人的绰号（他喊你的词）
NICKS = ("小宠物", "肥鱼", "小鱼", "蠢鱼", "鱼鱼", "宝贝", "宝宝", "宝", "亲爱的", "老婆", "鱼")
# 出现在二元组里就丢掉的功能字
STOP_CHARS = set("的了是我你在有和就不也都要会吗呢啊吧着个这那什么怎么可以你我他她它们"
                  "又还才再很太别没能想说看让给把被跟对从到些样每各某该")
STOP_PAIRS = set("""真的 其实 反正 然后 就是 可以 什么 怎么 这个 那个 我们 你们 他们 一个 没有 不是 就是 但是 因为 所以 现在 已经 觉得 知道 喜欢 有点 时候 东西 事情 要不 还是 应该 可能 一直 一下 这样 那样 感觉 不会 可以 不能 不要 就是""".split())

# 网感词表：按聊了多少轮逐级解锁，不一次全给（初始人设里一个都不写）
WANGAN = [
    (20, ("6", "笑死", "啊这")),
    (50, ("离谱", "麻了", "破防", "绷不住了")),
    (120, ("典", "乐", "栓Q", "小丑竟是我自己")),
]
WANGAN_ALL = tuple(w for _n, ws in WANGAN for w in ws)
QUOTE_RE = re.compile(r"[「『“\"]([^」』”\"]{2,10})[」』”\"]")
REMEMBER_RE = re.compile(r"(?:记住|记一下|以后(?:就)?叫|以后(?:就)?说|这叫)[：:，,]?\s*[「『“\"]?([^，。！？\s」』”\"]{2,10})")

STATE = {"turns": 0, "len_sum": 0, "len_n": 0, "q": 0, "exc": 0, "ellip": 0, "tilde": 0,
         "marks": {}, "nicks": {}, "words": {}, "tails": {}, "hours": [0] * 24,
         "slang": {}, "slang_used": 0, "updated": 0.0}

DAY = 86400.0
HALF_LIFE_DAYS = 30.0          # 和 longterm 一个半衰期：老习惯会淡，新习惯会顶上来
CORRECT_RE = re.compile(r"(别说|不要再说|别老说|换个说法|别这么说话|不许说|难听)")


def _load(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else default
    except Exception:
        return default


def _save(path, data):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except Exception:
        pass


def profile():
    data = _load(PROFILE, None)
    if not data:
        return dict(STATE, hours=[0] * 24)
    for k, v in STATE.items():
        data.setdefault(k, list(v) if isinstance(v, list) else v)
    return _decay(data)


def _bump(bag, key, n=1):
    bag[key] = int(bag.get(key, 0)) + n


def _decay(p):
    """超过一天就把计数按半衰期压一压，免得三年前的口头禅一直挂着。"""
    now = time.time()
    last = float(p.get("updated") or 0)
    if not last or now - last < DAY:
        return p
    k = 0.5 ** ((now - last) / DAY / HALF_LIFE_DAYS)
    for bag in ("marks", "words", "nicks", "tails"):
        d = p.get(bag) or {}
        p[bag] = {w: round(float(c) * k, 2) for w, c in d.items() if float(c) * k >= 0.8}
    return p


def looks_correction(text):
    return bool(CORRECT_RE.search(text or ""))


def correct(text=""):
    """主人说「别这么说话」时用的：指名了就删掉那条，没指名就把最显眼的口头禅砍掉。"""
    p = _decay(profile())
    quoted = re.findall(r"[「『“\"]([^」』”\"]{1,8})[」』”\"]", text or "")
    hit = False
    for q in quoted:
        marks = p.get("marks") or {}
        if q in marks:
            marks.pop(q)
            hit = True
        p["phrases"] = [x for x in (p.get("phrases") or []) if q not in x]
        p["marks"] = marks
    if not hit and not quoted:
        marks = p.get("marks") or {}
        for w, _c in sorted(marks.items(), key=lambda kv: -float(kv[1]))[:2]:
            marks[w] = round(float(marks[w]) * 0.4, 2)
            hit = True
        p["marks"] = marks
        p["phrases"] = (p.get("phrases") or [])[-6:]
    if hit:
        p["updated"] = time.time()
        _save(PROFILE, p)
    return hit


def observe(user_text, reply_text="", source="pet"):
    """每轮调一次。只统计，不判断，不联网。"""
    text = UID_RE.sub("", user_text or "").strip()
    if len(text) < 2 or len(text) > 400:
        return
    if text.startswith("/") or text.startswith("http"):
        return
    p = profile()
    # 长句多是要活儿的指令，不是说话的腔调：只数词，不数语气
    terse = len(text) <= 100
    if terse:
        p["turns"] = int(p.get("turns", 0)) + 1
    tone = text if terse else ""
    if tone:
        p["len_sum"] = int(p.get("len_sum", 0)) + len(tone)
        p["len_n"] = int(p.get("len_n", 0)) + 1
        if "?" in tone or "？" in tone:
            p["q"] = int(p.get("q", 0)) + 1
        if "!" in tone or "！" in tone:
            p["exc"] = int(p.get("exc", 0)) + 1
        if "…" in tone or "..." in tone:
            p["ellip"] = int(p.get("ellip", 0)) + 1
        if "~" in tone or "～" in tone:
            p["tilde"] = int(p.get("tilde", 0)) + 1
        for m in MARKERS:
            c = tone.count(m)
            if c:
                _bump(p.setdefault("marks", {}), m, c)
        for nk in NICKS:
            if nk in tone:
                _bump(p.setdefault("nicks", {}), nk)
    hours = p.get("hours") or [0] * 24
    hours[time.localtime().tm_hour] = int(hours[time.localtime().tm_hour]) + 1
    p["hours"] = hours
    words = p.setdefault("words", {})
    for i in range(len(text) - 1):
        a, b = text[i], text[i + 1]
        if a in STOP_CHARS or b in STOP_CHARS:
            continue
        pair = a + b
        if a == b or pair in STOP_PAIRS or not all("\u4e00" <= ch <= "\u9fff" for ch in pair):
            continue
        _bump(words, pair)
    if len(words) > 400:
        top = sorted(words.items(), key=lambda kv: -kv[1])[:200]
        p["words"] = dict(top)
    if terse:
        _bump(p.setdefault("tails", {}), text[-2:])
    if terse and 4 <= len(text) <= 24 and any(m in text for m in MARKERS):
        ps = [x for x in (p.get("phrases") or []) if x != text]
        ps.append(text)
        p["phrases"] = ps[-12:]
    # ---- 梗 / 黑话：命中就记；她没接住标 miss，接住了标 used ----
    slang = p.setdefault("slang", {})
    rep = reply_text or ""

    def _learn(term):
        term = (term or "").strip()
        if not (2 <= len(term) <= 10) or term in MARKERS:
            return
        ent = slang.get(term) or {"seen": 0, "used": 0, "miss": 0, "at": 0.0}
        ent["seen"] = int(ent.get("seen", 0)) + 1
        if term in rep:
            ent["used"] = int(ent.get("used", 0)) + 1
        else:
            ent["miss"] = int(ent.get("miss", 0)) + 1
        ent["at"] = time.time()
        slang[term] = ent

    for w in WANGAN_ALL:
        if w in text:
            _learn(w)
    for q in QUOTE_RE.findall(text):
        _learn(q)
    for q in REMEMBER_RE.findall(text):
        _learn(q)
    if re.search(r"懂|梗|啥意思|什么意思|没听过", text):
        tail = re.split(r"[，,。！？!?\s]+", text.strip())[-1]
        tail = re.sub(r"(你懂吗|你懂么|懂不懂|懂吗|知道吗|知道么|啥意思|什么意思|是不是|对不对|没听过|晓得不)$", "", tail)
        _learn(tail)
    if len(slang) > 120:
        keep = sorted(slang.items(), key=lambda kv: -float(kv[1].get("at", 0)))[:80]
        p["slang"] = dict(keep)
    p["updated"] = time.time()
    _save(PROFILE, p)


def _top(bag, k, least=1):
    return [w for w, c in sorted((bag or {}).items(), key=lambda kv: -kv[1])[:k] if c >= least]


def style_block(limit=150):
    """控制台看的旧版一句话画像。格式化逻辑复用 _habit_line，别再分叉一份。"""
    p = profile()
    n = int(p.get("turns", 0))
    if n < 8:
        return ""
    habit = _habit_line(p)
    if not habit:
        return ""
    return ("主人的说话习惯（观察了 %d 轮）：%s。你说话自然带一点这个味儿，别刻意装。" % (n, habit))[:limit]


STAGES = [
    (0, "刚认识", (), "还生涩：话少一点，别装熟，别抛网络梗，听不懂就直说没懂"),
    (8, "眼熟", (), "见过几回了，话可以自然点，但别硬接梗"),
    (20, "较熟", WANGAN[0][1], "可以稍微放开一点，允许阴阳怪气，不用太客气"),
    (50, "很熟", WANGAN[0][1] + WANGAN[1][1], "放开点，接得住梗，可以损他两句"),
    (120, "老熟人", WANGAN[0][1] + WANGAN[1][1] + WANGAN[2][1], "随便放飞，但别油腻"),
]


def stage_of(n=None):
    """聊到多少轮 = 什么熟悉度，以及这个阶段解锁哪些网感词。"""
    p = profile()
    n = int(p.get("turns", 0)) if n is None else int(n)
    cur = STAGES[0]
    for st in STAGES:
        if n >= st[0]:
            cur = st
    return {"name": cur[1], "from": cur[0], "words": cur[2], "hint": cur[3], "turns": n}


def _habit_line(p):
    """把画像压成一句话，给 learn_block 用。"""
    n = max(1, int(p.get("turns", 0)))
    bits = []
    avg = (p.get("len_sum", 0) / max(1, p.get("len_n", 1)))
    if avg <= 10:
        bits.append("句子很短")
    elif avg >= 22:
        bits.append("爱说长句")
    if p.get("q", 0) / n >= 0.3:
        bits.append("常反问")
    if p.get("ellip", 0) / n >= 0.2:
        bits.append("爱用「…」")
    if p.get("tilde", 0) / n >= 0.15:
        bits.append("爱拖个「~」")
    if p.get("exc", 0) / n >= 0.25:
        bits.append("感叹号多")
    marks = _top(p.get("marks"), 3, 2)
    if marks:
        bits.append("口头禅 " + "、".join("「%s」" % m for m in marks))
    nick = _top(p.get("nicks"), 1, 1)
    if nick:
        bits.append("喜欢叫你「%s」" % nick[0])
    words = _top(p.get("words"), 6, 2)
    if words:
        bits.append("常聊 " + "、".join(words[:4]))
    tails = [t for t in _top(p.get("tails"), 2, 4) if re.fullmatch(r"[\u4e00-\u9fa5]{2}", t)]
    if tails:
        bits.append("爱用「%s」收尾" % "」「".join(tails))
    ph = p.get("phrases") or []
    if ph:
        bits.append("最近一句原话「%s」" % ph[-1][:24])
    return "；".join(bits)


def learn_block(source="qq", limit=460):
    """每次请求拼进 system 的那块：习惯 + 学到的梗 + 熟悉度 + 这个阶段能说的网感词。"""
    p = profile()
    n = int(p.get("turns", 0))
    st = stage_of(n)
    out = ["【记忆与学习进展】"]
    habit = _habit_line(p)
    if habit:
        out.append("- 你观察到主人的说话习惯：" + habit + "。")
    slang = p.get("slang") or {}
    miss = [w for w, e in sorted(slang.items(),
                                 key=lambda kv: (-int(kv[1].get("miss", 0)), -float(kv[1].get("at", 0))))
            if int(e.get("miss", 0)) > 0]
    if miss:
        out.append("- 你最近学到的梗：" + "、".join("「%s」" % w for w in miss[:3])
                   + "。（不用解释，遇到合适的无语场面直接用）")
    if n < 8:
        out.append("- 你和主人还不熟（聊了 %d 轮）：%s。" % (n, st["hint"]))
    else:
        out.append("- 你们现在的熟悉度：%s（聊了 %d 轮）——%s。" % (st["name"], n, st["hint"]))
    if st["words"]:
        out.append("- 你现在能用的网感词：" + "、".join(st["words"][:10])
                   + "（一句最多一个，别堆，也别解释）")
    else:
        out.append("- 现在先别用网络梗，把话接住就行。")
    out.append("- 请自然地带一点主人的语气，别刻意，别像个人机。")
    return "\n".join(out)[:limit]

def report():
    """给人看的画像，也走 /learn 接口。"""
    p = profile()
    n = max(1, int(p.get("turns", 0)))
    hours = p.get("hours") or [0] * 24
    peak = sorted(range(24), key=lambda h: -int(hours[h]))[:2]
    return {
        "turns": int(p.get("turns", 0)),
        "avg_len": round(p.get("len_sum", 0) / max(1, p.get("len_n", 1)), 1),
        "ask_ratio": round(p.get("q", 0) / n, 2),
        "marks": _top(p.get("marks"), 8, 2),
        "nicks": _top(p.get("nicks"), 4, 1),
        "words": _top(p.get("words"), 12, 2),
        "peak_hours": [h for h in peak if hours[h]],
        "style": style_block(),
        "updated": p.get("updated", 0),
    }


# ---------- 技能库：给「装上手」用的学分 ----------

def skills():
    return _load(SKILLS, {})


def skill(name, kind="game", steps=None, outcome=None, note=None):
    """记一个技能：观察到的步骤、练一次成没成。outcome: True/False/None。"""
    data = skills()
    it = data.get(name) or {"kind": kind, "seen": 0, "tried": 0, "good": 0, "steps": [],
                            "notes": [], "last": 0.0}
    if steps:
        it["steps"] = [str(s)[:120] for s in steps][:24]
        it["seen"] = int(it.get("seen", 0)) + 1
    if outcome is not None:
        it["tried"] = int(it.get("tried", 0)) + 1
        if outcome:
            it["good"] = int(it.get("good", 0)) + 1
    if note:
        it.setdefault("notes", []).append(str(note)[:160])
        it["notes"] = it["notes"][-8:]
    it["kind"] = kind or it.get("kind", "game")
    it["last"] = time.time()
    data[name] = it
    _save(SKILLS, data)
    return it


def mastery(name):
    """熟练度 0~1，贝叶斯平滑，练得少不会吹成 0.8。"""
    it = skills().get(name)
    if not it:
        return 0.0
    return round((int(it.get("good", 0)) + 1.0) / (int(it.get("tried", 0)) + 2.0), 3)


def skill_report():
    data = skills()
    return [{"name": k, "kind": v.get("kind"), "seen": v.get("seen", 0),
             "tried": v.get("tried", 0), "good": v.get("good", 0),
             "mastery": mastery(k), "steps": len(v.get("steps", []))}
            for k, v in sorted(data.items(), key=lambda kv: -float(kv[1].get("last", 0)))]


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "skills":
        print(json.dumps(skill_report(), ensure_ascii=False, indent=1))
    else:
        print(json.dumps(report(), ensure_ascii=False, indent=1))
