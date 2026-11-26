# -*- coding: utf-8 -*-
"""统一记忆池：一套存储结构，三种作用域。

作用域（Scope）是硬边界，不是标签：
  global        全局，谁都能看（小宠物自己的身份、常识、备课知识）
  private       主人私聊 + 桌面，两者共用同一份（所以她 QQ 里记得的，桌面一定记得）
  group:<gid>   某个群，只在那个群里生效，绝不外流

铁律：
  - 群里只读 global + 本群；私聊/桌面只读 global + private。
  - 群友的话只进 group:<gid>，永远不进 private。
  - 主人的私聊内容永远不进任何 group。
  - 所有读写都走这里，谁也不许直接读 longterm.json / teacher-kb.md。
"""
import hashlib
import json
import os
import re
import threading
import time

try:
    from core.poollock import pool_lock
except ImportError:          # 以 core/ 为根被 import 时
    from poollock import pool_lock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
POOL = os.path.join(BASE, "memory-pool.json")
ARCHIVE = os.path.join(BASE, "memory-pool-archive.jsonl")
LOG = os.path.join(BASE, "core.log")

GLOBAL = "global"
PRIVATE = "private"
CHAT_KEEP = 40          # 每个作用域保留多少轮对话
ITEM_CHARS = 200        # 单条注入上限
LOCK = threading.RLock()


def log(msg):
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write("%s mem: %s\n" % (time.strftime("%H:%M:%S"), msg))
    except Exception:
        pass


def scope_of(source, uid="", gid=""):
    """来源 + 群号 -> 作用域。QQ 私聊和桌面共用 private，这是跨端同步的关键。"""
    gid = str(gid or "")
    if gid:
        return "group:" + gid
    return PRIVATE


def scopes_for(scope):
    """这个作用域能看见的东西，只有这两类。绝不包含别的作用域。"""
    return [GLOBAL, PRIVATE if scope == PRIVATE else scope]


def _load():
    try:
        with open(POOL, "r", encoding="utf-8") as f:
            d = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        d = {}
    d.setdefault("v", 2)
    d.setdefault("items", [])
    return d


def _write(d):
    """真正的原子写盘（不加锁；只给 _save 用）。"""
    tmp = POOL + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    os.replace(tmp, POOL)


def _save(d):
    """写盘外面再套一把跨进程锁；拿不到锁只降级直接写，绝不让异常冒泡到回复链路。

    注意：内部不许再有任何持 pool_lock 的路径（msvcrt 同进程重复锁同一区域会失败）。
    """
    try:
        with pool_lock(timeout=5):
            _write(d)
    except TimeoutError:
        log("pool lock busy >5s, fallback direct write")
        _write(d)
    except Exception as exc:   # 锁本身坏了也不能丢记忆
        log("pool lock error %r, direct write" % (exc,))
        _write(d)


def _clip(text, n=ITEM_CHARS):
    return re.sub(r"\s+", " ", str(text or "")).strip()[:n]


def _bigrams(s):
    s = re.sub(r"[\s，。！？、,.!?~…：:；;“”\"'()（）\[\]]+", "", s or "")
    return {s[i:i + 2] for i in range(len(s) - 1)}


def _new_id():
    return hashlib.sha1(("%f%s" % (time.time(), os.urandom(4))).encode()).hexdigest()[:12]


SYS_TAG_RE = re.compile(r"<\s*system_reminder\s*>.*?(<\s*/\s*system_reminder\s*>|$)", re.I | re.S)
SYS_TAG_ONLY = re.compile(r"<\s*/?\s*system_reminder\s*>", re.I)


def _strip_sys(t):
    """系统提示词严禁进记忆：<system_reminder>…</system_reminder> 整段剥掉（含没闭合的尾巴）。"""
    t = SYS_TAG_RE.sub(" ", t or "")
    t = SYS_TAG_ONLY.sub(" ", t)
    return re.sub(r"[ \t]{2,}", " ", t).strip()


def add(scope, text, kind="event", source="", important=0.3, meta=None, dedupe=True):
    """往池子里放一条。返回 True 表示真的新增了。"""
    text = _clip(_strip_sys(text))
    if not text or scope not in (GLOBAL, PRIVATE) and not scope.startswith("group:"):
        return False
    with LOCK:
        d = _load()
        items = d["items"]
        if dedupe:
            fb = _bigrams(text)
            for it in reversed(items[-400:]):
                if it.get("scope") != scope or it.get("kind") != kind:
                    continue
                if it.get("status", "active") != "active":
                    continue
                nb = _bigrams(it.get("text", ""))
                if not fb or not nb:
                    continue
                inter = len(fb & nb)
                if inter / float(max(1, min(len(fb), len(nb)))) > 0.8:
                    it["ts"] = time.time()
                    it["hits"] = int(it.get("hits", 0)) + 1
                    it["important"] = min(1.0, float(it.get("important") or 0.3) + 0.1)
                    _save(d)
                    return False
        items.append({"id": _new_id(), "scope": scope, "kind": kind, "text": text,
                      "ts": time.time(), "hits": 0, "important": float(important),
                      "status": "active", "source": source, "meta": meta or {}})
        _trim(d)
        _save(d)
    return True


def _trim(d):
    """聊天只留最近，整体超量就归档，不无限长胖。"""
    items = d["items"]
    seen = {}
    for it in list(items):
        if it.get("kind") != "chat":
            continue
        sc = it.get("scope")
        seen.setdefault(sc, []).append(it)
    dead = []
    for sc, group in seen.items():
        if len(group) > CHAT_KEEP:
            dead.extend(group[:len(group) - CHAT_KEEP])
    if len(items) > 5000:
        dead.extend([it for it in items if it not in dead][:len(items) - 5000])
    if not dead:
        return
    d["items"] = [it for it in items if it not in dead]
    try:
        with open(ARCHIVE, "a", encoding="utf-8") as f:
            for it in dead:
                f.write(json.dumps(it, ensure_ascii=False) + "\n")
    except OSError:
        pass
    log("归档 %d 条" % len(dead))


def add_chat(scope, user_text, reply_text, source=""):
    """一轮对话。私聊和桌面写同一份，所以两端看到的是同一段记忆。"""
    user_text, reply_text = _clip(_strip_sys(user_text), 600), _clip(_strip_sys(reply_text), 600)
    if not user_text or not reply_text:
        return False
    # 同话题 24h 内只留一条：防止流水账把池子撑爆（hits 不在这里刷）
    def _norm(t):
        return re.sub(r"[\s，。！？、,.!?~…：:；;“”\"'()（）\[\]]+", "", t or "")
    key = _norm(user_text)
    # 短句（“嗯”“好”）和占位句（“（看了一眼屏幕）”）不参与去重：宁可留重复，
    # 也不能让这些真实发生过的回合被吞掉——但同一条最多留 3 份，否则 10 格上下文会被它占满。
    bare = user_text.strip()
    placeholder = bare[:1] in ("（", "(") and bare[-1:] in ("）", ")")
    if len(key) >= 4 and not placeholder:
        now = time.time()
        for it in (_load().get("items") or []):
            if it.get("kind") != "chat" or it.get("scope") != scope:
                continue
            if now - float(it.get("ts") or 0) > 86400:
                continue
            prev = _norm((it.get("meta") or {}).get("user") or "")
            if prev and (prev == key or _bigrams(prev) == _bigrams(key)):
                log("chat dup skip: " + user_text[:20])
                return False
    else:
        now = time.time()
        same = [it for it in (_load().get("items") or [])
                if it.get("kind") == "chat" and it.get("scope") == scope
                and now - float(it.get("ts") or 0) <= 86400
                and _norm((it.get("meta") or {}).get("user") or "") == key]
        if len(same) >= 3:
            newest = max(same, key=lambda it: float(it.get("ts") or 0))
            with LOCK:
                d = _load()
                for it in d["items"]:
                    if it.get("id") == newest.get("id"):
                        it["ts"] = now
                _save(d)
            log("chat keep-refresh: " + user_text[:20])
            return False
    return add(scope, "对方：%s ｜ 你：%s" % (user_text, reply_text),
               kind="chat", source=source, important=0.1, dedupe=False,
               meta={"user": user_text, "assistant": reply_text})


def recent_pairs(scope, n=14):
    """给「压事实」用的成对对话，按时间正序。"""
    chats = [it for it in _visible(scope, "chat")]
    chats.sort(key=lambda it: float(it.get("ts") or 0))
    out = []
    for it in chats[-n:]:
        meta = it.get("meta") or {}
        out.append((meta.get("user", ""), meta.get("assistant", ""), float(it.get("ts") or 0)))
    return out


def _visible(scope, kind=None):
    want = set(scopes_for(scope))
    out = []
    for it in _load()["items"]:
        if it.get("scope") not in want or it.get("status", "active") != "active":
            continue
        if kind and it.get("kind") != kind:
            continue
        out.append(it)
    return out


def profile(scope, k=12):
    """重要事实：不分话题，固定带上。"""
    facts = [it for it in _visible(scope, "fact")]
    facts.sort(key=lambda it: (-(int(it.get("hits", 0)) + 2 * float(it.get("important") or 0)),
                               -float(it.get("ts", 0))))
    return [_clip(it["text"]) for it in facts[:k]]


def recall(scope, query, k=6):
    """旧事：这句话对得上才想起来。"""
    fb = _bigrams(query)
    if len(fb) < 2:
        return []
    scored = []
    for it in _visible(scope, "event"):
        nb = _bigrams(it.get("text", ""))
        if not nb:
            continue
        inter = len(fb & nb)
        if not inter:
            continue
        base = inter / float(len(fb) + 1.0)
        age_days = max(0.0, (time.time() - float(it.get("ts") or 0)) / 86400.0)
        scored.append((base * (1.0 + 0.5 * float(it.get("important") or 0)) / (1.0 + age_days / 30.0),
                       it))
    scored.sort(key=lambda x: -x[0])
    return [_clip(it["text"]) for _, it in scored[:k]]


def recent_events(scope, days=4, k=6):
    """最近几天的事：还没过去，不管这句话问什么都在场。

    recall() 只认字面命中，主人换个说法就召不回来；这条按时间兜底，
    保证「昨天刚说的」不会因为措辞不同就从她脑子里消失。
    """
    cut = time.time() - days * 86400.0
    rows = [it for it in _visible(scope, "event")
            if float(it.get("ts") or 0) >= cut]
    rows.sort(key=lambda it: -float(it.get("ts") or 0))
    return [_clip(it["text"]) for it in rows[:k]]


# 问过去的口头线索：命中就带摘要（比 0.25 的字面命中更符合"她是不是想起了从前"）
RECALL_HINT = re.compile(r"以前|之前|从前|过去|那时候|当年|还记得|旧事|早年|上次")


def summaries(scope, k=1, query="", thresh=0.4, limit=200):
    """「往前的事」：压缩出来的长期摘要（kind="lt"）。

    只有当前这句话**真的沾边**才带：查询 bigram 被摘要覆盖率 >= thresh(0.4)，或命中回忆类口风
    （RECALL_HINT）。不命中返回 []，注入侧整节省略 —— 省钱靠这条闸门，不靠截断。
    命中也只给 limit 字（默认 200，约 125 tokens）。
    """
    rows = [it for it in _visible(scope, "lt")]
    if not rows:
        return []
    q = (query or "").strip()
    if not q:
        return []
    rows.sort(key=lambda it: -float(it.get("ts") or 0))
    fq = _bigrams(q)
    hit = bool(RECALL_HINT.search(q))
    best = None
    for it in rows:
        if hit:
            best = it
            break
        fb = _bigrams(it.get("text") or "")
        if fq and fb:
            # 覆盖率（不是 Jaccard）：摘要是长文，Jaccard 永远接近 0；
            # 这里问的是"这句话的词有几成能在旧摘要里找到"
            cover = len(fq & fb) / float(len(fq))
            if cover >= thresh:
                best = it
                log("summary hit cover=%.2f" % cover)
                break
    if best is None:
        return []
    return [_clip(best.get("text"), limit)]


def recent(scope, n=6):
    """最近几轮对话（同作用域）。QQ 私聊和桌面共用 private，所以天然一致。"""
    chats = [it for it in _visible(scope, "chat")]
    chats.sort(key=lambda it: float(it.get("ts") or 0))
    return [it["text"] for it in chats[-n:]]


def kb():
    """备课知识：只进 global，谁都能用。"""
    rows = [_clip(it["text"], 4000) for it in _visible(GLOBAL, "kb")]
    return "\n".join(rows).strip()


def set_style(text):
    """说话习惯（learn 分析出来的结论）：放 private，群里不注入。"""
    text = _clip(text, 800)
    if not text:
        return False
    with LOCK:
        d = _load()
        for it in list(d["items"]):
            if it.get("scope") == PRIVATE and it.get("kind") == "style":
                if it.get("text") == text:
                    return False
                d["items"].remove(it)
        d["items"].append({"id": _new_id(), "scope": PRIVATE, "kind": "style",
                           "text": text, "ts": time.time(), "hits": 0,
                           "important": 0.4, "status": "active", "source": "learn",
                           "meta": {}})
        _save(d)
    return True


def style():
    rows = _visible(PRIVATE, "style")
    return rows[-1]["text"] if rows else ""


def prompt(scope, query=""):
    """拼成给模型看的一段。群聊里永远看不到 private 的任何东西。"""
    parts = []
    prof = profile(scope)
    if prof:
        head = "【主人的基本设定】" if scope == GLOBAL else "【你一直记着的】"
        if scope != PRIVATE:
            head = "【这个群里你记得的】"
        parts.append(head)
        parts.extend("- " + t for t in prof)
    evs = recall(scope, query)
    if evs:
        parts.append("【想起的旧事（会随时间变淡，别当成刚发生的）】")
        parts.extend("- " + t for t in evs)
    rev = [t for t in recent_events(scope) if t not in set(evs)]
    if rev:
        parts.append("【这几天的事（还热乎，可以自然提起，别说成很久以前）】")
        parts.extend("- " + t for t in rev)
    if scope == PRIVATE:
        st = style()
        if st:
            parts.append("【你说话的习惯】")
            parts.append(st)
    chats = recent(scope, 16)
    if chats:
        parts.append("【最近聊过的（可能来自另一个端，QQ 或桌面）】")
        parts.extend("- " + t for t in chats)
    if parts:
        parts.insert(0, "（下面这些是你真的经历过、记着的事；被问起就自然说出来，不许说自己没有记忆。）")
    return "\n".join(parts)


def stats():
    d = _load()
    out = {}
    for it in d["items"]:
        sc = it.get("scope", "?")
        out[sc] = out.get(sc, 0) + 1
    return {"total": len(d["items"]), "scopes": out}


def all_items(scope=None):
    items = _load()["items"]
    if scope:
        items = [it for it in items if it.get("scope") == scope]
    return items


def set_text(item_id, text):
    """原地改一条的正文（UPD 操作）。id 不存在就返回 False，别的字段一律不动。"""
    item_id = str(item_id or "").strip()
    text = _clip(text)
    if not item_id or not text:
        return False
    with LOCK:
        d = _load()
        for it in d["items"]:
            if it.get("id") != item_id:
                continue
            if it.get("text") == text:
                return False
            it["text"] = text
            it["ts"] = time.time()
            it["source"] = it.get("source") or "update"
            _save(d)
            log("set_text %s -> %s" % (item_id, text[:30]))
            return True
    return False


def forget(item_id):
    with LOCK:
        d = _load()
        n = len(d["items"])
        d["items"] = [it for it in d["items"] if it.get("id") != item_id]
        if len(d["items"]) != n:
            _save(d)
            return True
    return False


def migrate():
    """把三套旧记忆搬进池子，幂等：搬过的不会重复搬。"""
    moved = {"facts": 0, "chat": 0, "kb": 0}
    # 1. longterm.json 里的事实 -> private（主人自己的记忆，群里永不出现）
    try:
        with open(os.path.join(BASE, "longterm.json"), "r", encoding="utf-8") as f:
            lt = json.load(f)
        for fact in lt.get("facts") or []:
            if fact.get("status", "active") != "active":
                continue
            kind = "fact" if fact.get("kind") == "fact" else "event"
            if add(PRIVATE, fact.get("text", ""), kind=kind, source="longterm",
                   important=float(fact.get("important") or 0.3), dedupe=True):
                moved["facts"] += 1
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    # 2. memory.json 里的对话 -> private（QQ 私聊 + 桌面共用）
    try:
        with open(os.path.join(BASE, "memory.json"), "r", encoding="utf-8") as f:
            hist = json.load(f)
        buf = []
        for m in hist if isinstance(hist, list) else []:
            buf.append(m)
        pairs, cur = [], []
        for m in buf:
            if m.get("role") == "user":
                cur = [m.get("content", "")]
            elif m.get("role") == "assistant" and cur:
                pairs.append((cur[0], m.get("content", ""), m.get("source", "")))
                cur = []
        for u, a, s in pairs[-20:]:
            if add_chat(PRIVATE, u, a, s):
                moved["chat"] += 1
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    # 3. teacher-kb.md -> global（备课知识，不是主人的私事）
    try:
        with open(os.path.join(BASE, "teacher-kb.md"), "r", encoding="utf-8-sig") as f:
            md = f.read()
        chunks = [c.strip() for c in re.split(r"\n\s*\n", md) if c.strip()]
        for c in chunks:
            if len(c) < 8:
                continue
            if add(GLOBAL, c, kind="kb", source="teacher-kb", important=0.8, dedupe=True):
                moved["kb"] += 1
    except OSError:
        pass
    if any(moved.values()):
        _save(_load())
        log("迁移完成 %s" % moved)
    return moved
