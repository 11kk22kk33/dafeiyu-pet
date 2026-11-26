# -*- coding: utf-8 -*-
"""QQ 账号侧的小管家：好友申请、收藏图、笔记。

- OneBot v11 HTTP 走 SnowLuma 的 http-default（127.0.0.1:3000）
- 事件（好友申请 / 新好友 / 进群）从 SnowLuma 的 ws-default（3001）直接读，不经过 AstrBot
- 新来的（好友通过、进群）一律登记成「访客」，不继承主人
"""
import json
import os
import socket
import threading
import time
import base64
import os as _os
import urllib.request
import urllib.parse

_BASE = os.path.dirname(os.path.abspath(__file__))


def _paths():
    """读 config-paths.json；缺文件/坏 JSON 都返回空 dict。"""
    try:
        with open(os.path.join(_BASE, "config-paths.json"), "r", encoding="utf-8-sig") as f:
            return json.load(f) or {}
    except Exception:
        return {}


_PATHS = _paths()


def _bp(key, fallback):
    v = _PATHS.get(key)
    return v if isinstance(v, str) and v.strip() else fallback
HERE = _bp("pet_shared", os.path.join("D:" + os.sep, "pet-shared"))
BASE = "http://127.0.0.1:3000"
WS_HOST, WS_PORT = "127.0.0.1", 3001
TOKEN = "RMuGL-lwrx1A04YhsD4Qkv9nKVNYt929-E7j3bl8VA0"
WS_TOKEN = "8TKQ4gwkVZd2wrzfcGcA6f094J_s8OvjZGpQnzirfPM"
SAFE = _os.path.join(HERE, "safe-mode.json")
PENDING = _os.path.join(HERE, "qq-pending.json")
NOTES = _os.path.join(HERE, "notes.json")

STATE = {"ws": False, "at": 0.0, "last": "", "events": 0}
_lock = threading.Lock()


def _read(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _write(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def ob(action, **params):
    """调 OneBot 动作，失败返回 None。"""
    try:
        req = urllib.request.Request(
            BASE + "/" + action,
            data=json.dumps(params, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + TOKEN},
        )
        with urllib.request.urlopen(req, timeout=8) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as exc:
        STATE["last"] = "%s: %s" % (action, exc)
        return None


def account():
    j = ob("get_login_info")
    if isinstance(j, dict) and j.get("status") == "ok":
        d = j.get("data") or {}
        return {"uin": str(d.get("user_id") or ""), "nick": d.get("nickname") or ""}
    return {}


# ---------- 访客登记 ----------
def register_visitor(uid, nick=""):
    """新来的一律访客；已经在名单里的不动。"""
    uid = str(uid or "").strip()
    if not uid:
        return False
    raw = _read(SAFE, {})
    if not isinstance(raw, dict):
        raw = {}
    people = raw.get("people")
    if not isinstance(people, dict):
        people = {}
    if uid in people:
        return False
    people[uid] = {"note": ("新来的：" + nick)[:40] if nick else "新来的", "mode": "visitor"}
    raw["people"] = people
    _write(SAFE, raw)
    return True


# ---------- 待处理好友申请 ----------
def pending_list():
    return _read(PENDING, {"items": []}).get("items", [])


def pending_add(item):
    raw = _read(PENDING, {"items": []})
    items = raw.get("items") or []
    for it in items:
        if it.get("flag") == item.get("flag"):
            return
    items.append(item)
    raw["items"] = items[-50:]
    _write(PENDING, raw)


def pending_drop(flag):
    raw = _read(PENDING, {"items": []})
    raw["items"] = [i for i in (raw.get("items") or []) if i.get("flag") != flag]
    _write(PENDING, raw)


def approve(flag, remark=""):
    j = ob("set_friend_add_request", flag=flag, approve=True, remark=remark or "小宠物")
    ok = bool(isinstance(j, dict) and j.get("status") == "ok")
    if ok:
        for it in pending_list():
            if it.get("flag") == flag:
                register_visitor(it.get("uid"), it.get("nick") or "")
        pending_drop(flag)
    return ok, j


def reject(flag):
    j = ob("set_friend_add_request", flag=flag, approve=False)
    ok = bool(isinstance(j, dict) and j.get("status") == "ok")
    if ok:
        pending_drop(flag)
    return ok, j


# ---------- 笔记 ----------
def notes_list():
    return _read(NOTES, {"items": []}).get("items", [])


def note_add(text, who="主人"):
    text = (text or "").strip()
    if not text:
        return None
    raw = _read(NOTES, {"items": []})
    items = raw.get("items") or []
    item = {"t": time.strftime("%Y-%m-%d %H:%M"), "who": who, "text": text}
    items.append(item)
    raw["items"] = items[-500:]
    _write(NOTES, raw)
    return item


def note_del(idx):
    raw = _read(NOTES, {"items": []})
    items = raw.get("items") or []
    try:
        items.pop(int(idx))
    except Exception:
        return False
    raw["items"] = items
    _write(NOTES, raw)
    return True


# ---------- 收藏图 ----------
def collect(src, source="own", tags=None):
    """src 可以是本地路径或 http(s) 地址；存进表情库并登记标签。"""
    import sticker

    rec = sticker.add_source(src, source=source, tags=tags)
    return rec


# ---------- 事件监听 ----------
def _mask(payload):
    key = os.urandom(4)
    head = bytes([0x81])
    n = len(payload)
    if n < 126:
        head += bytes([0x80 | n])
    elif n < 65536:
        head += bytes([0x80 | 126]) + n.to_bytes(2, "big")
    else:
        head += bytes([0x80 | 127]) + n.to_bytes(8, "big")
    return head + key + bytes(b ^ key[i % 4] for i, b in enumerate(payload))


def _recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("closed")
        buf += chunk
    return buf


def _read_frame(sock):
    h = _recv_exact(sock, 2)
    op = h[0] & 0x0F
    n = h[1] & 0x7F
    masked = bool(h[1] & 0x80)
    if n == 126:
        n = int.from_bytes(_recv_exact(sock, 2), "big")
    elif n == 127:
        n = int.from_bytes(_recv_exact(sock, 8), "big")
    key = _recv_exact(sock, 4) if masked else b""
    data = _recv_exact(sock, n) if n else b""
    if masked:
        data = bytes(b ^ key[i % 4] for i, b in enumerate(data))
    return op, data


def _handle(ev):
    STATE["events"] += 1
    pt = ev.get("post_type")
    if pt == "request" and ev.get("request_type") == "friend":
        pending_add({
            "flag": str(ev.get("flag") or ""),
            "uid": str(ev.get("user_id") or ""),
            "nick": ev.get("comment") or "",
            "comment": ev.get("comment") or "",
            "t": time.strftime("%Y-%m-%d %H:%M"),
        })
    elif pt == "notice" and ev.get("notice_type") == "friend_add":
        register_visitor(ev.get("user_id"), ev.get("nickname") or "")
    elif pt == "notice" and ev.get("notice_type") == "group_increase":
        register_visitor(ev.get("user_id"), ev.get("nickname") or "")


def _watch():
    while True:
        try:
            sock = socket.create_connection((WS_HOST, WS_PORT), timeout=10)
            key = base64.b64encode(os.urandom(16)).decode()
            path = "/?access_token=" + urllib.parse.quote(WS_TOKEN)
            req = ("GET %s HTTP/1.1\r\nHost: %s:%d\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                   "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n") % (path, WS_HOST, WS_PORT, key)
            sock.sendall(req.encode())
            head = b""
            while b"\r\n\r\n" not in head:
                head += sock.recv(2048)
                if len(head) > 8192:
                    raise ConnectionError("big header")
            if b"101" not in head.split(b"\r\n")[0]:
                raise ConnectionError(head.split(b"\r\n")[0].decode("latin-1"))
            sock.settimeout(70)
            STATE["ws"] = True
            STATE["at"] = time.time()
            while True:
                try:
                    op, data = _read_frame(sock)
                except socket.timeout:
                    sock.sendall(_mask(b"{}"))
                    continue
                if op == 0x8:
                    break
                if op == 0x9:
                    sock.sendall(_mask(data))
                    continue
                if op != 0x1:
                    continue
                try:
                    ev = json.loads(data.decode("utf-8"))
                except Exception:
                    continue
                _handle(ev)
        except Exception as exc:
            STATE["last"] = "ws: %s" % exc
        finally:
            STATE["ws"] = False
        time.sleep(5)


def start():
    with _lock:
        if STATE.get("started"):
            return
        STATE["started"] = True
    threading.Thread(target=_watch, daemon=True).start()


if __name__ == "__main__":
    print(json.dumps({"account": account(), "pending": pending_list()}, ensure_ascii=False))
