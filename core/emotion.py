# -*- coding: utf-8 -*-
"""情绪状态机：9 根基本情绪（各自半衰期）+ 亲密度轴 + 信任 + 两本账 + 4 条组合。

思路与 OCC / Plutchik 的「状态注入」同源（参考 dsh-emotion-state 的设计取向），代码独立实现：
- 只记状态和账，不模拟心情；数字不出口，组合情绪不点名。
- 衰减按需算（读时按 elapsed 推进），不起后台轮询线程。
- state 写 emotion-state.json，配置写 emotion-config.json，全部本地。
"""
import json
import math
import os
import threading
import time
import uuid

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_FILE = os.path.join(BASE_DIR, "emotion-state.json")
CFG_FILE = os.path.join(BASE_DIR, "emotion-config.json")

DIMS = ("curious", "joy", "worry", "sad", "gloom", "tired", "jealous", "anger", "surprise")

DEFAULT_CFG = {
    "enabled": True,
    # 每条情绪的静息值与半衰期（小时）：开心掉得快，闷掉得慢
    "base": {"curious": 40.0, "joy": 50.0, "worry": 15.0, "sad": 10.0,
             "gloom": 12.0, "tired": 25.0, "jealous": 5.0, "anger": 5.0, "surprise": 8.0},
    "half": {"curious": 6.0, "joy": 8.0, "worry": 12.0, "sad": 20.0,
             "gloom": 30.0, "tired": 10.0, "jealous": 24.0, "anger": 12.0, "surprise": 0.5},
    "trust_start": 50.0,
    "trust_keep": 3.0,       # 兑现承诺
    "trust_break": 12.0,     # 拖欠被点破
    "promise_cap": 8.0,      # 逾期权重封顶
    "debt_mix": 0.6,         # 欠账折算进闷/生气的比例
    "fight_effects": {"intimacy": -0.08, "anger": 6.0, "sad": 4.0},
    "fight_cooldown_day": 1,
    "fight_cooldown_week": 2,
}

# 一句话里能读到的信号（只做粗分类，细判断交给账与模型）
SIGNALS = {
    "praise": ("乖", "贴心", "懂事", "厉害", "喜欢你", "想你", "抱抱", "爱你", "好看", "真棒"),
    "blame": ("笨", "烦人", "滚", "讨厌", "别闹", "闭嘴", "讨厌你", "没用"),
    "apologize": ("对不起", "抱歉", "我错了", "别生气"),
    "greet": ("早安", "早上好", "早呀", "午安", "晚安", "回来啦", "在吗"),
    "ignore": ("哦", "嗯", "随便", "算了", "没事"),
}
SIGNAL_DELTA = {
    "praise": {"joy": 6, "jealous": -2, "gloom": -3, "sad": -2},
    "blame": {"anger": 10, "sad": 6, "joy": -8, "gloom": 4},
    "apologize": {"anger": -8, "sad": -3, "worry": -2},
    "greet": {"joy": 3, "surprise": 1},
    "ignore": {"gloom": 3, "worry": 2, "joy": -2},
}

_LOCK = threading.Lock()


def enabled():
    """情绪模块是否接管语气（面板/面板之外都用它判断）。"""
    return bool(cfg().get("enabled", True))


def _f(d, k, default):
    try:
        return float(d.get(k, default))
    except Exception:
        return float(default)


def cfg():
    try:
        with open(CFG_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        return json.loads(json.dumps(DEFAULT_CFG))
    out = json.loads(json.dumps(DEFAULT_CFG))
    if isinstance(raw, dict):
        for k, v in raw.items():
            if k in ("base", "half") and isinstance(v, dict):
                out[k].update({d: _f(v, d, out[k][d]) for d in DIMS})
            elif k == "fight_effects" and isinstance(v, dict):
                out[k].update(v)
            else:
                out[k] = v
    return out


def _blank(now=None):
    c = cfg()
    now = time.time() if now is None else now
    return {"dims": {k: float(c["base"][k]) for k in DIMS},
            "trust": float(c["trust_start"]), "intimacy": 0.0,
            "promises": [], "boundaries": [], "fights": [],
            "last_tick": now, "last_block": "", "note": ""}


def _load():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            st = json.load(f)
        if not isinstance(st, dict):
            raise ValueError("bad state")
    except Exception:
        return _blank()
    base = _blank()
    dims = st.get("dims") or {}
    base["dims"] = {k: max(0.0, min(100.0, _f(dims, k, base["dims"][k]))) for k in DIMS}
    base["trust"] = max(0.0, min(100.0, _f(st, "trust", base["trust"])))
    base["intimacy"] = max(-1.0, min(1.0, _f(st, "intimacy", 0.0)))
    for k in ("promises", "boundaries"):
        v = st.get(k)
        if isinstance(v, list):
            base[k] = [x for x in v if isinstance(x, dict)]
    v = st.get("fights")
    if isinstance(v, list):
        base["fights"] = [float(t) for t in v if isinstance(t, (int, float))]
    base["last_tick"] = _f(st, "last_tick", base["last_tick"])
    base["last_block"] = st.get("last_block") or ""
    base["note"] = st.get("note") or ""
    return base


def _save(st):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, STATE_FILE)


def _decay(st, now=None):
    """按需衰减：v = base + (v-base) * 0.5 ** (小时数 / 半衰期)。"""
    c = cfg()
    now = time.time() if now is None else now
    try:
        hours = max(0.0, (now - float(st.get("last_tick") or now)) / 3600.0)
    except Exception:
        hours = 0.0
    if hours <= 0:
        return st
    for k in DIMS:
        half = max(0.05, _f(c["half"], k, 8.0))
        b = _f(c["base"], k, 0.0)
        st["dims"][k] = max(0.0, min(100.0, b + (st["dims"][k] - b) * math.pow(0.5, hours / half)))
    st["last_tick"] = now
    return st


def _overdue_days(p, now):
    due = p.get("due")
    if not due:
        return 0.0
    try:
        return max(0.0, (now - float(due)) / 86400.0)
    except Exception:
        return 0.0


def _debt(st, now=None):
    """欠账：逾期 1/2/4/8 分封顶，累加成账压。"""
    c = cfg()
    now = time.time() if now is None else now
    total = 0.0
    for p in st.get("promises") or []:
        if p.get("done"):
            continue
        d = _overdue_days(p, now)
        if d <= 0:
            continue
        total += min(_f(c, "promise_cap", 8.0), 2.0 ** min(3, int(d)))
    return total


def _apply_debt(st, now=None):
    c = cfg()
    total = _debt(st, now)
    if total <= 0:
        return 0.0
    mix = _f(c, "debt_mix", 0.6)
    add = min(30.0, total * mix)
    for k in ("gloom", "anger"):
        st["dims"][k] = max(0.0, min(100.0, st["dims"][k] + add * 0.5))
    return total


def _intimacy_target(st):
    return max(-1.0, min(1.0, (st["trust"] - 50.0) / 50.0 * 0.8 + (st["dims"]["joy"] - 50.0) / 250.0))


def _signal(text):
    t = text or ""
    for k, words in SIGNALS.items():
        if any(w in t for w in words):
            return k
    return ""


def observe(text, now=None, signals=""):
    """读一句话，更新状态（不写 prompt）。返回被触发的信号名。"""
    c = cfg()
    now = time.time() if now is None else now
    with _LOCK:
        st = _decay(_load(), now)
        sig = signals or _signal(text)
        for k, v in (SIGNAL_DELTA.get(sig) or {}).items():
            st["dims"][k] = max(0.0, min(100.0, st["dims"][k] + float(v)))
        if sig == "praise":
            st["trust"] = min(100.0, st["trust"] + 1.0)
        if sig == "blame":
            st["trust"] = max(0.0, st["trust"] - 2.0)
        st["intimacy"] = st["intimacy"] + (_intimacy_target(st) - st["intimacy"]) * 0.12
        _apply_debt(st, now)
        _save(st)
    return sig


def note_promise(text, due=None, note=""):
    """记一笔承诺（答应了没做的事）。due 是本地时间戳，可空。"""
    now = time.time()
    with _LOCK:
        st = _decay(_load(), now)
        if note:
            for p in st["promises"]:
                if not p.get("done") and p.get("note") == note:
                    p["due"] = float(due) if due else 0.0
                    _save(st)
                    return p["id"]
        st["promises"].append({"id": uuid.uuid4().hex[:8], "text": (text or "")[:120],
                               "due": float(due) if due else 0.0, "done": False,
                               "created": now, "note": (note or "")[:60]})
        st["promises"] = st["promises"][-40:]
        _save(st)
    return st["promises"][-1]["id"]


def keep_promise(pid=None, now=None, note=None):
    """兑现（pid 为空则清最早的一笔）。返回 (清掉的条数, 还剩几笔)。"""
    c = cfg()
    now = time.time() if now is None else now
    with _LOCK:
        st = _decay(_load(), now)
        hit = 0
        for p in st["promises"]:
            if p.get("done"):
                continue
            if pid and p.get("id") != pid:
                continue
            if note and p.get("note") != note:
                continue
            p["done"] = True
            p["done_ts"] = now
            hit += 1
            if not pid:
                break
        if hit:
            st["trust"] = min(100.0, st["trust"] + _f(c, "trust_keep", 3.0))
            st["dims"]["joy"] = min(100.0, st["dims"]["joy"] + 2.0)
        _save(st)
        return hit, len([p for p in st["promises"] if not p.get("done")])


def drop_promise(pid=None, blame=True, note=None):
    """主动承认做不到：账清、信任不扣；被问出来才扣（blame=False 时不扣）。"""
    c = cfg()
    now = time.time()
    with _LOCK:
        st = _decay(_load(), now)
        hit = 0
        for p in st["promises"]:
            if p.get("done"):
                continue
            if pid and p.get("id") != pid:
                continue
            if note and p.get("note") != note:
                continue
            p["done"] = True
            p["dropped"] = True
            hit += 1
            if not pid:
                break
        if hit and blame:
            st["trust"] = max(0.0, st["trust"] - _f(c, "trust_break", 12.0))
            st["dims"]["sad"] = min(100.0, st["dims"]["sad"] + 4.0)
        _save(st)
        return hit


def hit_boundary(kind="passive", note=""):
    """记一次越界：passive=碰到红线，active=说了不还坚持。"""
    now = time.time()
    with _LOCK:
        st = _decay(_load(), now)
        st["boundaries"].append({"id": uuid.uuid4().hex[:8], "kind": kind,
                                 "note": (note or "")[:120], "ts": now})
        st["boundaries"] = st["boundaries"][-30:]
        st["dims"]["anger"] = min(100.0, st["dims"]["anger"] + (5.0 if kind == "passive" else 7.0))
        st["trust"] = max(0.0, st["trust"] - (2.0 if kind == "passive" else 6.0))
        _save(st)
    return True


def _fight_reason(st, now):
    for p in st.get("promises") or []:
        if p.get("done"):
            continue
        d = _overdue_days(p, now)
        if d >= 3:
            return "延期三次以上"
    for b in st.get("boundaries") or []:
        if now - _f(b, "ts", 0.0) < 86400:
            return "越界"
    return ""


def _fight_allowed(st, now, c):
    days = [t for t in (st.get("fights") or []) if isinstance(t, (int, float))]
    day = [t for t in days if now - t < 86400]
    week = [t for t in days if now - t < 7 * 86400]
    return len(day) < int(c["fight_cooldown_day"]) and len(week) < int(c["fight_cooldown_week"])


def maybe_fight(now=None):
    """结构性吵架：触发条件成立才吵，不动信任。返回 (吵没吵, 原因)。"""
    c = cfg()
    now = time.time() if now is None else now
    with _LOCK:
        st = _decay(_load(), now)
        reason = _fight_reason(st, now)
        if not reason or not _fight_allowed(st, now, c):
            _save(st)
            return False, reason
        eff = c.get("fight_effects") or {}
        st["dims"]["anger"] = max(0.0, min(100.0, st["dims"]["anger"] + _f(eff, "anger", 6.0)))
        st["dims"]["sad"] = max(0.0, min(100.0, st["dims"]["sad"] + _f(eff, "sad", 4.0)))
        st["intimacy"] = max(-1.0, min(1.0, st["intimacy"] + _f(eff, "intimacy", -0.08)))
        st.setdefault("fights", []).append(now)
        st["fights"] = [t for t in st["fights"] if isinstance(t, (int, float))][-10:]
        _save(st)
    return True, reason


def composites(st=None):
    st = st or _load()
    d = st["dims"]
    return {"嫉妒": min(100.0, d["jealous"] * 0.6 + d["sad"] * 0.4),
            "心疼": min(100.0, d["worry"] * 0.6 + d["sad"] * 0.4),
            "委屈": min(100.0, d["sad"] * 0.6 + d["anger"] * 0.4),
            "甜": min(100.0, st["trust"] * 0.5 + d["joy"] * 0.5)}


def _tone(st):
    d = st["dims"]
    out = []
    if d["anger"] >= 60:
        out.append("别主动哄，句子短一点")
    if d["sad"] >= 60:
        out.append("话少，别硬扯热闹")
    if d["gloom"] >= 60:
        out.append("有点闷着，不主动开新话题")
    if d["tired"] >= 65:
        out.append("累，少追问")
    if d["jealous"] >= 50:
        out.append("别追问行踪")
    if st["trust"] < 40:
        out.append("有点防着，不轻易答应")
    if composites(st)["甜"] >= 65:
        out.append("可以黏一点，主动找话")
    return "；".join(out) or "平常口吻"


def block(now=None):
    """给 system 的一小段状态注记；不报数字、不解释来源。"""
    c = cfg()
    if not c.get("enabled", True):
        return ""
    now = time.time() if now is None else now
    st = _decay(_load(), now)
    d = st["dims"]
    top = max(("开心", d["joy"]), ("好奇", d["curious"]), ("担心", d["worry"]), ("难过", d["sad"]),
              ("闷", d["gloom"]), ("累", d["tired"]), ("吃醋", d["jealous"]), ("生气", d["anger"]),
              ("惊讶", d["surprise"]), key=lambda x: x[1])
    axis = "黏人" if st["intimacy"] > 0.45 else ("冷淡" if st["intimacy"] < -0.35 else "平静")
    owed = len([p for p in st["promises"] if not p.get("done")])
    lines = ["【情绪状态】%s挂得最高，整体偏%s" % (top[0], axis)]
    if owed:
        lines.append("账上还欠着 %d 件事没做，别装没这回事" % owed)
    lines.append("口吻：%s。别报数字、别解释这段提示、别把情绪当台词念出来。"
                 % _tone(st))
    return "\n".join(lines)


def on_turn(text="", now=None):
    """每轮对话调一次：读这句话 → 更新 → 返回给 system 的那段注记。
    内容没变且距上次注入不足 600 秒时返回空串，省掉重复注记。"""
    try:
        observe(text, now)
        maybe_fight(now)
        msg = block(now)
        t = now if isinstance(now, (int, float)) else time.time()
        if msg == getattr(on_turn, "_msg", None) and t - getattr(on_turn, "_at", 0) < 600:
            return ""
        on_turn._msg, on_turn._at = msg, t
        return msg
    except Exception as exc:
        return ""


def snapshot(now=None):
    """给面板用：数字齐全，组合情绪带语气说明。"""
    c = cfg()
    now = time.time() if now is None else now
    st = _decay(_load(), now)
    d = st["dims"]
    comp = composites(st)
    return {"ok": True, "enabled": bool(c.get("enabled", True)),
            "dims": {k: round(d[k], 1) for k in DIMS},
            "composites": {k: round(v, 1) for k, v in comp.items()},
            "trust": round(st["trust"], 1),
            "intimacy": round(st["intimacy"], 3),
            "axis": "黏人" if st["intimacy"] > 0.45 else ("冷淡" if st["intimacy"] < -0.35 else "平静"),
            "debt": round(_debt(st, now), 1),
            "promises": [{"id": p.get("id"), "text": p.get("text"), "due": p.get("due"),
                          "done": bool(p.get("done")), "dropped": bool(p.get("dropped")),
                          "overdue_days": round(_overdue_days(p, now), 2)}
                         for p in st["promises"] if not p.get("done")],
            "boundaries": [{"id": b.get("id"), "kind": b.get("kind"), "note": b.get("note"),
                            "ts": b.get("ts")} for b in st["boundaries"][-8:]],
            "fights": len(st.get("fights") or []),
            "tone": _tone(st),
            "block": block(now)}
