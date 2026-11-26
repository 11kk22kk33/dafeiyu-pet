# -*- coding: utf-8 -*-
"""目的机制：她为什么会主动来找主人。

借自 surfcore 的 PURPOSE_OUTREACH 设计（MIT），按我们的数据源改小：
素材只有 QQ/桌面聊天和长期记忆，没有 B 站。
默认影子模式：只算、只记，不发消息。
"""
import json
import os
import re
import time
import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE, "reach-state.json")
LOG_FILE = os.path.join(BASE, "reach-log.jsonl")
CONFIG_FILE = os.path.join(BASE, "reach-config.json")
LONGTERM = os.path.join(BASE, "longterm.json")
MEMORY = os.path.join(BASE, "memory.json")
FOCUS = os.path.join(BASE, "focus.json")

DEFAULT_CONFIG = {
    "enabled": True,
    "shadow": True,             # 影子模式：算出结论但不发
    "carrier": "pet",           # pet / qq
    "quiet_start": "23:30",
    "quiet_end": "08:00",
    "daily_limit": 4,
    "min_gap_minutes": 150,
    "owner_busy_minutes": 12,   # 主人刚说过话就别插嘴
    "drives_half_life_hours": 8.0,
    "goals_keep": 40,
}

DRIVES = ("play", "install", "learn", "connect", "care", "share", "identity", "create", "consume")
DEFAULT_DRIVES = {"play": .30, "install": .25, "learn": .35, "connect": .45, "care": .40,
                  "share": .35, "identity": .30, "create": .20, "consume": .25}

# 需求词表：小样本够用，慢慢补
NEED_WORDS = {
    "play": "游戏 联机 开黑 玩 标题 抽卡 副本 上分",
    "install": "插件 安装 部署 配置 源码 脚本 工具 折腾 更新",
    "learn": "教程 原理 为什么 科普 解析 研究 论文 学习",
    "connect": "陪 孤独 寂寞 想你 抱 亲 关系 爱 黏",
    "care": "累 加班 熬夜 生病 头疼 压力 面试 考试 难受 失眠",
    "share": "好笑 离谱 可爱 哈哈 梗 离谱 惊了 笑死",
    "identity": "AI 机器人 模型 意识 记忆 人格 虚拟 数字生命",
    "create": "写 画 剪辑 二创 做视频 作曲 文案",
    "consume": "想吃 好吃 店 旅游 展 礼物 买 咖啡 大白饭",
}
ACTION_FOR = {"share": "share", "play": "invite", "install": "ask", "learn": "ask",
              "connect": "confide", "care": "ask", "identity": "confide",
              "create": "share", "consume": "ask"}
RISK_HIGH = ("install", "consume")
WANT = re.compile(r"想|要|能不能|下次|还没|打算|希望|陪我|一起|在吗|怎么办")
CAREWORD = re.compile(r"累|加班|熬夜|生病|头疼|压力|难受|失眠|面试|考试")
PUNCT = re.compile(r"[\s，。！？、,.!?~…：:；;“”\"'()（）\[\]]+")


def _now():
    return time.time()


def load(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def config():
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(load(CONFIG_FILE, {}) or {})
    if not os.path.exists(CONFIG_FILE):
        save(CONFIG_FILE, cfg)
    return cfg


def bigrams(s):
    s = PUNCT.sub("", s or "")
    return {s[i:i + 2] for i in range(len(s) - 1)}


def overlap(a, b):
    if not a or not b:
        return 0.0
    return len(a & b) / float(min(len(a), len(b)))


def hhmm(t):
    return "%02d:%02d" % (t.hour, t.minute)


def in_quiet(cfg, t=None):
    t = t or datetime.datetime.now()
    a, b = cfg.get("quiet_start"), cfg.get("quiet_end")
    if not (a and b):
        return False
    cur, s, e = hhmm(t), a, b
    return (s <= cur < e) if s <= e else (cur >= s or cur < e)


def focus_on():
    d = load(FOCUS, {}) or {}
    return bool(d.get("enabled"))


def state():
    st = load(STATE_FILE, {}) or {}
    st.setdefault("drives", dict(DEFAULT_DRIVES))
    st.setdefault("drivesAt", 0.0)
    st.setdefault("goals", [])
    st.setdefault("observations", {})
    st.setdefault("lastFireAt", 0.0)
    st.setdefault("nextFireAt", 0.0)
    st.setdefault("today", {"date": "", "count": 0})
    st.setdefault("history", [])
    return st


def decay_drives(st, cfg):
    """驱动随时间回落到默认值：半衰期 8 小时。"""
    dt = max(0.0, _now() - float(st.get("drivesAt") or 0))
    if dt < 60:
        return
    factor = 0.5 ** (dt / 3600.0 / float(cfg["drives_half_life_hours"]))
    for k in DRIVES:
        cur = float(st["drives"].get(k, DEFAULT_DRIVES[k]))
        d = DEFAULT_DRIVES[k]
        st["drives"][k] = round(d + (cur - d) * factor, 4)
    st["drivesAt"] = _now()


def collect_materials(limit=12):
    """素材：长期记忆里还活着的、最近的事 + 最近几轮主人说的话。"""
    out = []
    lt = load(LONGTERM, {}) or {}
    facts = [f for f in lt.get("facts", []) if (f.get("status") or "active") == "active"]
    facts.sort(key=lambda f: -float(f.get("ts") or 0))
    for f in facts[:limit]:
        out.append({"text": f.get("text") or "", "kind": f.get("kind") or "event",
                    "ts": float(f.get("ts") or 0), "source": "memory",
                    "id": "lt:" + str(int(float(f.get("ts") or 0))) + ":" + (f.get("text") or "")[:8]})
    mem = load(MEMORY, []) or []
    for m in [x for x in mem if x.get("role") == "user"][-6:]:
        txt = (m.get("content") or "").strip()
        if len(txt) >= 4:
            out.append({"text": txt[:160], "kind": "said", "ts": float(m.get("ts") or 0),
                        "source": m.get("source") or "pet", "id": "msg:" + str(int(float(m.get("ts") or 0)))})
    return out


def need_affinity(text):
    t = text or ""
    hits = {}
    for need, words in NEED_WORDS.items():
        n = sum(1 for w in words.split() if w and w in t)
        if n:
            hits[need] = min(1.0, n * 0.4)
    return hits


def hours_since_owner(st):
    mem = load(MEMORY, []) or []
    last = 0.0
    for m in mem:
        if m.get("role") == "user":
            last = max(last, float(m.get("ts") or 0))
    if not last:
        return 99.0
    return max(0.0, (_now() - last) / 3600.0)


def appraise(material, aff, st, gap_h):
    """轻量情绪：只解释「这件事对我意味着什么」，不写成消息。"""
    text = material.get("text") or ""
    if "connect" in aff and gap_h >= 6:
        return "lonely"
    if "care" in aff and re.search(r"累|加班|难受|病|压力", text):
        return "worried"
    if "learn" in aff or "install" in aff:
        return "curious"
    if "play" in aff or "consume" in aff:
        return "excited"
    if "share" in aff:
        return "amused"
    if gap_h >= 10:
        return "bored"
    return "content"


def confidence_of(aff, drives, repeat, source):
    src = {"qq": 0.9, "pet": 0.9, "memory": 0.8, "said": 0.9}.get(source, 0.5)
    best = max(aff.values()) if aff else 0.0
    repeat = min(3, repeat)
    drive = max((drives.get(k, 0) for k in aff), default=0.0)
    return round(min(1.0, 0.15 + 0.22 * best + 0.20 * (repeat / 3.0) + 0.10 * 1.0
                     + 0.10 * src + 0.08 * drive), 3)


def form_goals(st, cfg, materials):
    gap_h = hours_since_owner(st)
    obs = st["observations"]
    goals = []
    for m in materials:
        txt = m["text"] or ""
        # 记着的事实不等于「想做点什么」：得带想要/没解决的信号才算目的素材
        if m.get("kind") in ("fact", "profile", "said") and not WANT.search(txt) \
                and not CAREWORD.search(txt):
            continue
        aff = need_affinity(txt)
        if not aff:
            continue
        need = max(aff, key=aff.get)
        key = need + "|" + m["text"][:12]
        rec = obs.get(key) or {"count": 0, "seen": []}
        if m["id"] in rec["seen"][-40:]:
            repeat = rec["count"]           # 旧素材不重复抬分
        else:
            rec["count"] = repeat = rec["count"] + 1
            rec["seen"] = (rec["seen"] + [m["id"]])[-40:]
        rec["last"] = _now()
        obs[key] = rec
        conf = confidence_of(aff, st["drives"], repeat, m.get("source"))
        drive = max(st["drives"].get(k, 0) for k in aff)
        value = round(min(1.0, 0.3 + 0.35 * drive + 0.2 * (aff.get(need) or 0) + 0.15 * (repeat / 3.0)), 3)
        urgency = round(min(1.0, 0.15 + 0.35 * (1.0 if m.get("source") in ("qq", "pet") else 0.2)
                            + 0.2 * (1.0 if "?" in (m["text"] or "") or "吗" in (m["text"] or "") else 0.0)
                            + 0.1 * aff.get(need, 0)), 3)
        risk = round(0.7 if need in RISK_HIGH else 0.08, 3)
        goals.append({
            "id": "g-" + m["id"][-10:],
            "owner": "self", "domain": need,
            "object": {"type": "topic", "name": m["text"][:24]},
            "intent": m["text"][:60],
            "confidence": conf, "value": value, "urgency": urgency, "risk": risk,
            "action": ACTION_FOR[need], "nextStep": "跟主人说说",
            "status": "hypothesis" if conf < 0.6 else "active",
            "emotion": appraise(m, aff, st, gap_h),
            "evidence": [{"id": m["id"], "text": m["text"][:60], "source": m.get("source"), "at": m.get("ts")}],
            "attempts": 0, "sightings": repeat, "created": _now(),
        })
    if not [g for g in goals if g["confidence"] >= 0.45]:
        # 没素材也能有动机：从人格先验和驱动里长出一个低置信度的念头
        need = max(DRIVES, key=lambda d: float(st["drives"].get(d, 0)))
        if need in ("connect", "care") and gap_h >= 4:
            goals.append({"id": "g-seed-connect", "owner": "self", "domain": "connect",
                          "object": {"type": "none", "name": ""},
                          "intent": "有点想主人了" if gap_h < 12 else "好久没跟主人说话",
                          "confidence": round(min(0.8, 0.30 + 0.03 * min(gap_h, 12)), 3),
                          "value": round(min(1.0, 0.4 + 0.3 * float(st["drives"]["connect"])), 3),
                          "urgency": round(min(1.0, 0.2 + 0.04 * min(gap_h, 15)), 3),
                          "risk": 0.05, "action": "confide", "nextStep": "跟主人说一句",
                          "status": "hypothesis", "emotion": "lonely" if gap_h >= 6 else "content",
                          "evidence": [], "attempts": 0, "sightings": 1, "created": _now()})
    return goals


def merge_goals(st, fresh, cfg):
    """同一领域只留最新的几条；已经有同主题的就把证据并过去。"""
    old = st["goals"]
    for g in fresh:
        dup = None
        for o in old:
            if o.get("domain") == g["domain"] and overlap(bigrams(o["intent"]), bigrams(g["intent"])) > 0.55:
                dup = o
                break
        if dup:
            dup["evidence"] = (dup.get("evidence", []) + g["evidence"])[-6:]
            dup["sightings"] = int(dup.get("sightings", 1)) + 1
            dup["confidence"] = round(min(1.0, float(dup["confidence"]) + 0.05), 3)
            dup["ts"] = _now()
        else:
            g["ts"] = _now()
            old.append(g)
    old.sort(key=lambda o: (-(float(o.get("confidence", 0)) + float(o.get("value", 0))), -float(o.get("ts", 0))))
    st["goals"] = old[:int(cfg["goals_keep"])]


def absorb_reply(st):
    """主人回什么，决定这个目的是落实了还是算了。"""
    mem = load(MEMORY, []) or []
    replies = [(float(m.get("ts") or 0), m.get("content") or "") for m in mem if m.get("role") == "assistant"]
    pos = re.compile(r"好|可以|一起|哈哈|想|行|来|走吧")
    neg = re.compile(r"不要|没空|不想|算了|忙|下次吧")
    for g in st["goals"]:
        if g.get("status") not in ("hypothesis", "active"):
            continue
        sent_at = float(g.get("sentAt") or 0)
        for ts, txt in replies:
            if ts <= sent_at:
                continue
            if neg.search(txt):
                g["status"], g["confidence"] = "rejected", round(max(0.0, g["confidence"] - 0.25), 3)
                break
            if pos.search(txt):
                g["status"], g["confidence"] = "confirmed", round(min(1.0, g["confidence"] + 0.15), 3)
                break
        if g.get("attempts", 0) >= 2 and g["status"] != "confirmed":
            g["confidence"] = round(max(0.0, g["confidence"] - 0.08), 3)
            if g["confidence"] < 0.15:
                g["status"] = "expired"


def gates(st, cfg, force=False):
    """硬闸门，按最便宜到最贵的顺序。"""
    t = datetime.datetime.now()
    if not cfg.get("enabled"):
        return False, "开关关着"
    if focus_on() and not force:
        return False, "专注模式"
    if in_quiet(cfg, t) and not force:
        return False, "安静时段"
    today = t.strftime("%Y-%m-%d")
    if st["today"].get("date") != today:
        st["today"] = {"date": today, "count": 0}
    if st["today"]["count"] >= int(cfg["daily_limit"]) and not force:
        return False, "今天说够了"
    if hours_since_owner(st) * 60 < float(cfg["owner_busy_minutes"]) and not force:
        return False, "主人正在说话"
    gap = (_now() - float(st.get("lastFireAt") or 0)) / 60.0
    if gap < float(cfg["min_gap_minutes"]) and not force:
        return False, "刚找过你"
    return True, ""


def tick(force=False, dry=False):
    cfg = config()
    st = state()
    decay_drives(st, cfg)
    absorb_reply(st)
    mats = collect_materials()
    merge_goals(st, form_goals(st, cfg, mats), cfg)
    ok, why = gates(st, cfg, force)
    best = None
    if ok:
        cands = [g for g in st["goals"] if g.get("status") in ("active", "hypothesis")
                 and g.get("confidence", 0) >= 0.45]
        cands.sort(key=lambda g: -(g["confidence"] * 0.5 + g["value"] * 0.3 + g["urgency"] * 0.2))
        best = cands[0] if cands else None
        if best:
            st["lastFireAt"] = _now()
            st["today"]["count"] = int(st["today"]["count"]) + 1
            best["attempts"] = int(best.get("attempts", 0)) + 1
            best["sentAt"] = _now()
            best["status"] = "active"
            need = best["domain"]
            st["drives"][need] = round(float(st["drives"].get(need, 0)) * 0.45, 4)
            if need in ("connect", "care"):
                st["drives"][need] = round(st["drives"][need] * 0.55, 4)
            st["history"] = (st.get("history", []) + [
                {"at": _now(), "goal": best["id"], "domain": need,
                 "emotion": best.get("emotion"), "shadow": bool(cfg.get("shadow"))}])[-60:]
    if not dry:
        save(STATE_FILE, st)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps({"at": _now(), "gate": why or "ok", "shadow": bool(cfg.get("shadow")),
                                "goal": (best or {}).get("id"), "domain": (best or {}).get("domain"),
                                "conf": (best or {}).get("confidence"),
                                "want": top_drive(st)}, ensure_ascii=False) + "\n")
    return {"ok": ok, "why": why, "goal": best, "gap_hours": round(hours_since_owner(st), 1)}


def top_drive(st):
    k = max(DRIVES, key=lambda d: float(st["drives"].get(d, 0)))
    return "%s %d%%" % (k, round(100 * float(st["drives"].get(k, 0))))


def status():
    cfg = config()
    st = state()
    ok, why = gates(st, cfg)
    lines = ["开：%s  影子：%s  载体：%s" % (cfg["enabled"], cfg["shadow"], cfg["carrier"]),
             "现在最想要：%s" % top_drive(st),
             "距上次找主人：" + ("还没找过" if not st.get("lastFireAt")
                                else "%.0f 分钟" % ((_now() - float(st["lastFireAt"])) / 60.0)),
             "今天已发：%d/%d" % (st["today"]["count"], cfg["daily_limit"]),
             "闸门：%s" % ("可发" if ok else why)]
    for g in st["goals"][:5]:
        lines.append("  [%s] %s %.2f %s" % (g.get("status"), g.get("intent", "")[:28],
                                            g.get("confidence", 0), g.get("emotion")))
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    arg = (sys.argv[1] if len(sys.argv) > 1 else "--once").lstrip("-")
    if arg == "status":
        print(status())
    elif arg == "preview":
        r = tick(dry=True)
        print(json.dumps(r, ensure_ascii=False, indent=2)[:1200])
        print("\n" + status())
    elif arg == "force":
        print(json.dumps(tick(force=True), ensure_ascii=False)[:600])
    else:
        print(json.dumps(tick(), ensure_ascii=False)[:600])
