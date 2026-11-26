# -*- coding: utf-8 -*-
"""鱼搭子挂件桥（8777）：挂件说话，我们把话交给本机大脑，再把回话递回 8778。

挂件那边只会 POST 六个口子（say / talk / frame / mode / subtitle / watch），
回话口是它自己的 8778，只收 {"text": ...}。插件原本负责接线，这里换个实现：
走我们的 proxy 管线（build_messages + post_chat + record），记忆照进统一池。
"""

import base64
import difflib
import io
import json
import os
import queue
import re
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONF_FILE = os.path.join(BASE, "hands.json")
DEFAULTS = {
    "on": True,
    "port": 8777,
    "reply": "http://127.0.0.1:8778/hands/reply",
    "voice": True,
    "max_tokens": 220,
    "history_turns": 6,       # 带多少轮上下文（她得记得上句话，不然每句都从零开始）
    "game": False,            # 鱼搭子是否在游戏模式（持久化，Proxy 重启不丢）
    "game_at": 0.0,
    "game_ttl": 21600,        # 游戏标记最多认 6 小时，鱼搭子崩了不会一直以为在打游戏
    "sub_seconds": 90,
    "sub_lines": 40,
    "frame_max": 6,
    "frame_min_gap": 30,      # 陪看/陪玩时自己抓帧的最小间隔（秒）
    "frame_per_hour": 30,     # 每小时最多抓几张，省 token
    "frame_hash_gap": 8,      # 和上一张的 dHash 距离小于这个就算"画面没变"，不发
    "frame_reuse": 180,       # 3 分钟内的那张图还能白嫖，不重抓
    "frame_resend_gap": 60,   # 同一张别 60s 内连发两次
}

NEEDED = (
    "MODEL", "log", "build_messages", "post_chat", "clean_reply", "record",
    "mod_on", "capture_window", "capture_screen",
    "heart_on_turn", "heart_state",
)

_ns = {}
_conf = {}
_inst = None

# R14：proxy 控制台与桥共用这把锁；hands.json 只有一个写入口（set_conf / _save_conf）。
CONF_LOCK = threading.RLock()


def conf_view():
    """返回已合并 DEFAULTS 的配置，避免控制台漏显示未写进文件的默认项。"""
    with CONF_LOCK:
        return dict(DEFAULTS, **_conf)


def conf_lock():
    """供外部写者显式取锁（单一写者场景下一般不需要）。"""
    return CONF_LOCK


def set_conf(key, value):
    """proxy 控制台唯一写入口：改内存 + 落盘，与桥共用 CONF_LOCK。"""
    if key != "game":
        return False
    if _inst is None:
        return False
    _inst.set_game(bool(value))
    return True


def conf():
    return _conf


def _load_conf():
    c = dict(DEFAULTS)
    try:
        with open(CONF_FILE, "r", encoding="utf-8") as f:
            got = json.load(f)
        if isinstance(got, dict):
            for k in DEFAULTS:
                if k in got:
                    c[k] = got[k]
    except FileNotFoundError:
        try:
            with open(CONF_FILE, "w", encoding="utf-8") as f:
                json.dump(c, f, ensure_ascii=False, indent=2)
        except OSError:
            pass
    except Exception as exc:
        _ns.get("log", print)("hands.json unreadable: %r" % (exc,))
    return c


def _save_conf(**kw):
    """把运行期状态写回 hands.json（游戏模式这类标志不能一重启就归零）。"""
    with CONF_LOCK:
        return _save_conf_locked(**kw)


def _save_conf_locked(**kw):
    if not kw:
        return
    cur = {}
    try:
        with open(CONF_FILE, "r", encoding="utf-8") as f:
            got = json.load(f)
        if isinstance(got, dict):
            cur = got
    except FileNotFoundError:
        pass
    except Exception as exc:
        _log("hands.json 读不动，只写内存: %r" % (exc,))
    cur.update(kw)
    try:
        tmp = CONF_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cur, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CONF_FILE)
    except Exception as exc:
        _log("hands.json 存不下: %r" % (exc,))
    _conf.update(kw)


def _log(msg):
    fn = _ns.get("log")
    try:
        if fn:
            fn("[hands] " + str(msg))
    except Exception:
        pass


def _play(path):
    """用 Windows 自带的 MCI 放一段 mp3/wav，不引第三方。"""
    if not path or not os.path.exists(path):
        return
    try:
        import ctypes
        mci = ctypes.windll.winmm.mciSendStringW
        buf = ctypes.create_unicode_buffer(512)
        alias = "dfyhands%d" % int(time.time() * 1000)

        def cmd(s):
            return mci(s, buf, 512, 0)

        kind = "waveaudio" if path.lower().endswith(".wav") else "mpegvideo"
        if cmd('open "%s" type %s alias %s' % (path, kind, alias)) != 0:
            return
        cmd("play %s wait" % alias)
        cmd("close %s" % alias)
    except Exception as exc:
        _log("play failed: %r" % (exc,))


def _shrink(b64, max_side=1280, quality=80):
    """挂件发来的截图先压一道，省 token 也省上行时间。"""
    if not b64:
        return ""
    raw = b64.strip()
    if raw.startswith("data:") and "," in raw:
        raw = raw.split(",", 1)[1]
    try:
        from PIL import Image
        data = base64.b64decode(raw)
        img = Image.open(io.BytesIO(data)).convert("RGB")
        if max(img.size) > max_side:
            ratio = max_side / float(max(img.size))
            img = img.resize((max(1, int(img.width * ratio)), max(1, int(img.height * ratio))))
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=quality)
        return base64.b64encode(out.getvalue()).decode("ascii")
    except Exception as exc:
        _log("image shrink failed: %r" % (exc,))
        return ""


FRAME = {"last": 0.0, "marks": [], "prev": 0, "prev_ok": False, "shots": 0,
         "skip": 0, "reuse": 0, "sent": 0.0}


def _dhash(b64):
    """9x8 灰度差分哈希，用来判断画面有没有真的变。纯 Pillow，无新依赖。"""
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(base64.b64decode(b64))).convert("L").resize((9, 8))
        px = list(img.getdata())
        bits = 0
        for r in range(8):
            row = r * 9
            for c in range(8):
                bits = (bits << 1) | (1 if px[row + c] > px[row + c + 1] else 0)
        return bits
    except Exception:
        return 0


def _hdist(a, b):
    return bin(int(a) ^ int(b)).count("1")


class Hands(object):
    def __init__(self, ns):
        global _ns
        _ns = ns
        self.lock = threading.Lock()
        self.turns = queue.Queue()
        self.subs = []          # [(ts, text)]
        self.frames = []        # [b64] 攒着的图
        self.last_frame = (0.0, "")   # (ts, b64) 最近一张，90s 内可复用，不重抓
        self.speaking_until = 0.0     # 朗读窗口，自己听自己的挡板
        self.last_reply = ("", 0.0)   # (文本, 时刻)，歌词式回声比对
        self.game = False
        self.watching = False
        self.said = 0
        self.failed = 0
        self.hist = []           # [{"role","content"}]，最近几轮原文，重启不跨进程
        # 游戏模式是持久状态：Proxy 重启后从 hands.json 恢复，不能让标志位归零
        try:
            at = float(_conf.get("game_at") or 0)
            ttl = float(_conf.get("game_ttl") or 21600)
            fresh = (not at) or (time.time() - at <= ttl)
            self.game = bool(_conf.get("game")) and fresh
            if _conf.get("game") and not fresh:
                _log("游戏标记已过期 %.0f 分钟，按关闭处理"
                     % ((time.time() - at) / 60.0))
        except Exception as exc:
            _log("游戏标记恢复失败: %r" % (exc,))
        self.state = {"起没起": False, "错": "", "收到": 0, "回话": 0, "历史": 0}
        self._srv = None
        self._worker = threading.Thread(target=self._work, daemon=True)
        self._worker.start()

    # ---------- 口子 ----------
    def start(self):
        port = int(_conf.get("port") or 8777)
        hands = self

        class 处理(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _回(self, code, obj):
                try:
                    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                    self.send_response(code)
                    self.send_header("content-type", "application/json; charset=utf-8")
                    self.send_header("content-length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except Exception:
                    pass

            def _体(self):
                try:
                    n = int(self.headers.get("content-length") or 0)
                    raw = self.rfile.read(n) if n else b"{}"
                    return json.loads(raw.decode("utf-8") or "{}")
                except Exception:
                    return {}

            def do_GET(self):
                if self.path.startswith("/hands/ping"):
                    self._回(200, {"ok": True, "口子": port, "收到": hands.state["收到"]})
                elif self.path.startswith("/hands/devices"):
                    self._回(200, {"ok": True, "设备": [], "版本": "hands-bridge"})
                else:
                    self._回(404, {"ok": False, "说": "没有这个口子"})

            def do_POST(self):
                if not _conf.get("on", True):
                    self._回(409, {"ok": False, "说": "桥关着呢"})
                    return
                body = self._体()
                path = self.path.split("?")[0].rstrip("/")
                try:
                    if path in ("/hands/say", "/hands/talk"):
                        text = str(body.get("text") or "").strip()
                        img = _shrink(body.get("图片") or body.get("image") or "")
                        if not text and not img:
                            self._回(400, {"ok": False, "说": "这话是空的"})
                            return
                        hands.push(text, [img] if img else [], kind="talk" if path.endswith("talk") else "say")
                        self._回(200, {"ok": True, "说": "送进去了"})
                    elif path == "/hands/frame":
                        img = _shrink(body.get("图片") or body.get("image") or "")
                        if not img:
                            self._回(400, {"ok": False, "说": "这张图没读到"})
                            return
                        if body.get("存档") or not body.get("立即"):
                            hands.keep_frame(img)
                            self._回(200, {"ok": True, "说": "攒着了"})
                        else:
                            hands.push("", [img], kind="peek")
                            self._回(200, {"ok": True, "说": "看了"})
                    elif path in ("/hands/mode", "/hands/model"):
                        hands.set_game(bool(body.get("游戏")))
                        self._回(200, {"ok": True, "说": "游戏模式" + ("开" if hands.game else "关")})
                    elif path == "/hands/subtitle":
                        lines = body.get("字幕") or []
                        hands.feed_subs(lines)
                        self._回(200, {"ok": True, "说": "记下了"})
                    elif path == "/hands/watch":
                        hands.watching = bool(body.get("watch"))
                        self._回(200, {"ok": True, "说": "陪看" + ("开" if hands.watching else "关")})
                    else:
                        self._回(404, {"ok": False, "说": "没有这个口子"})
                except Exception as exc:
                    _log("handler error: %r" % (exc,))
                    self._回(500, {"ok": False, "说": str(exc)})

        try:
            srv = ThreadingHTTPServer(("127.0.0.1", port), 处理)
        except OSError as exc:
            self.state["错"] = "口子 %d 占着：%s" % (port, exc)
            _log(self.state["错"])
            return False
        self._srv = srv
        self.state["起没起"] = True
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        _log("8777 起来了")
        return True

    # ---------- 状态 ----------
    def push(self, text, images, kind="say"):
        if text and kind in ("say", "talk") and self._echo(text):
            self.state["回声"] = self.state.get("回声", 0) + 1
            _log("挡了一句回声（%s）" % text[:12])
            return
        self.state["收到"] += 1
        _log("收到[%s]: %s%s" % (kind, (text or "（无字）")[:60],
                                 "  图%d" % len(images) if images else ""))
        if images:
            self.last_frame = (time.time(), images[-1])
        self.turns.put((text, images, self.sub_text(), kind))

    def set_game(self, on):
        """游戏模式：改内存 + 写盘，Proxy 重启后能恢复。"""
        with self.lock:
            self.game = bool(on)
        _save_conf(game=self.game, game_at=time.time())
        _log("游戏模式" + ("开" if self.game else "关") + "（已落盘）")

    def keep_frame(self, img):
        with self.lock:
            self.last_frame = (time.time(), img)
            self.frames.append(img)
            if len(self.frames) > DEFAULTS["frame_max"] * 2:
                self.frames = self.frames[-DEFAULTS["frame_max"] * 2:]

    def take_frames(self, extra):
        with self.lock:
            out = list(self.frames) + list(extra or [])
            self.frames = []
        return out[-int(_conf.get("frame_max") or 6):]

    def frames_left(self):
        now = time.time()
        cap = int(_conf.get("frame_per_hour") or 30)
        with self.lock:
            FRAME["marks"] = [t for t in FRAME["marks"] if now - t < 3600]
            return max(0, cap - len(FRAME["marks"]))

    def ledger(self, imgs):
        """仅统计鱼搭子链路的图（30/h 上限）；proxy 主视觉链路走 IMG_BUDGET_CHARS，不走这里。"""
        cap = int(_conf.get("frame_per_hour") or 30)
        now = time.time()
        with self.lock:
            FRAME["marks"] = [t for t in FRAME["marks"] if now - t < 3600]
            room = max(0, cap - len(FRAME["marks"]))
            keep = list(imgs)[:room]
            FRAME["marks"].extend([now] * len(keep))
            over = len(list(imgs)) > len(keep)
        if keep:
            FRAME["sent"] = now
        if over:
            _log("账本满了：本小时已 %d/%d 张，这次只发 %d 张图" % (len(FRAME["marks"]), cap, len(keep)))
        return keep

    def game_shot(self):
        """游戏模式下由 proxy 自己全屏抓一张。

        fb-merged 的定时抓图在部分全屏游戏里只拿到桌面壁纸（她会说"你拿我当壁纸"），
        proxy 的 capture_screen 抓的是当前屏幕，实测能拿到游戏画面。
        """
        if not self.game:
            return ""
        grab = _ns.get("capture_screen") or _ns.get("capture_window")
        if not callable(grab):
            return ""
        try:
            return _shrink(grab(), 1280, 80) or ""
        except Exception as exc:
            _log("游戏模式抓帧失败: %r" % (exc,))
            return ""

    def save_peek(self, b64):
        """把她这次真正看到的图留一张到 vision-tmp/peek-last.jpg，主人能自己核对。"""
        try:
            d = os.path.join(BASE, "vision-tmp")
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "peek-last.jpg"), "wb") as f:
                f.write(base64.b64decode(b64))
        except Exception as exc:
            _log("存档失败: %r" % (exc,))

    def auto_frame(self, images):
        """双源互斥：有图就用；90s 内抓过就白嫖那张；游戏模式 proxy 一律不截；只有陪看才补位。"""
        if images:
            return images
        now = time.time()
        with self.lock:
            ts, cached = self.last_frame
        fresh = float(_conf.get("frame_reuse") or 180)
        regap = float(_conf.get("frame_resend_gap") or 60)

        def 白嫖():
            if cached and now - ts <= fresh and now - FRAME["sent"] >= regap:
                FRAME["reuse"] += 1
                _log("复用 %.0fs 前那张图（不重抓）" % (now - ts))
                return [cached]
            return images

        if self.game:
            return 白嫖()          # 游戏模式：鱼搭子是唯一图源，proxy 一律不截
        if not self.watching or not _ns.get("mod_on", lambda x: True)("vision"):
            return images
        gap = float(_conf.get("frame_min_gap") or 30)
        with self.lock:
            if now - FRAME["last"] < gap:
                return 白嫖()      # 陪看但刚抓过，退而求其次用上一张
        grab = _ns.get("capture_window") or _ns.get("capture_screen")
        if not callable(grab):
            return images
        try:
            png = grab()
        except Exception as exc:
            _log("抓帧失败: %r" % (exc,))
            return images
        jpg = _shrink(png, 1280, 80)
        if not jpg:
            return images
        h = _dhash(jpg)
        min_gap = int(_conf.get("frame_hash_gap") or 8)
        with self.lock:
            if FRAME["prev"] and h and _hdist(FRAME["prev"], h) < min_gap:
                FRAME["skip"] += 1
                FRAME["last"] = now
                _log("抓帧跳过（画面没变 %d）" % _hdist(FRAME["prev"], h))
                return images
            FRAME["last"] = now
            FRAME["prev"] = h or FRAME["prev"]
            FRAME["shots"] += 1
            self.last_frame = (now, jpg)
        _log("补位抓帧 1 张（陪看模式）")
        return [jpg]

    def feed_subs(self, lines):
        now = time.time()
        with self.lock:
            for item in lines if isinstance(lines, list) else []:
                if isinstance(item, dict):
                    t = str(item.get("文本") or item.get("text") or "").strip()
                else:
                    t = str(item or "").strip()
                if t:
                    self.subs.append((now, t))
            self._trim_subs()

    def _trim_subs(self):
        now = time.time()
        win = float(_conf.get("sub_seconds") or 90)
        keep = int(_conf.get("sub_lines") or 40)
        self.subs = [x for x in self.subs if now - x[0] <= win][-keep:]

    def sub_text(self):
        with self.lock:
            self._trim_subs()
            lines = [t for _, t in self.subs][-12:]
        return "\n".join(lines)

    # ---------- 干活 ----------
    def _work(self):
        while True:
            item = self.turns.get()
            try:
                self._turn(*item)
            except Exception as exc:
                self.failed += 1
                _log("turn failed: %r" % (exc,))
            finally:
                self.turns.task_done()

    def _turn(self, text, images, subs, kind):
        if not self.turns.empty():
            _log("排队 %d 句，先做这一句" % self.turns.qsize())
        images = list(images or [])
        had = len(images)
        if kind == "peek":
            shot = self.game_shot()
            if shot:
                images = [shot]
                had = 1
        if kind == "peek" and images:
            # 主动解说：画面和上一张几乎一样就别开口（省 token，也别念流水账）
            h = _dhash(images[-1])
            gap = int(_conf.get("frame_hash_gap") or 8)
            with self.lock:
                # prev_ok 单独标记：纯色/过暗的帧哈希就是 0，不能拿 0 当"还没算过"
                if FRAME.get("prev_ok"):
                    dist = _hdist(FRAME["prev"], h)
                    unchanged = dist < gap
                else:
                    dist = 999
                    unchanged = False
                if unchanged:
                    FRAME["skip"] += 1
                    FRAME["last"] = time.time()
                if not unchanged:
                    FRAME["prev"] = h
                    FRAME["prev_ok"] = True
            if unchanged:
                _log("peek 跳过（画面没变 %d）" % dist)
                return
        images = self.auto_frame(images)
        auto = len(images) > had
        imgs = self.ledger(self.take_frames(images))
        if imgs:
            self.save_peek(imgs[-1])
        spoken = text
        shown = text if text else "（她主动看了一眼主人的屏幕）"
        if auto:
            shown = shown + "\n\n（附了一张主人现在的屏幕截图，看到什么就说什么，别念字）"
        if subs:
            shown = shown + "\n\n【你正在看的】\n" + subs
        def _ask(img_list):
            content = [{"type": "text", "text": shown}]
            for b in img_list:
                content.append({"type": "image_url",
                                "image_url": {"url": "data:image/jpeg;base64," + b}})
            # 关键：把最近几轮一起发过去。proxy 的 build_messages 只收 system 之外的
            # 消息，所以历史必须由这边补，否则她每句话都从零开始（ASR 语序一乱就像听不懂）
            history = list(self.hist)
            heart = _ns.get("heart_on_turn")
            htxt = ""
            if callable(heart):
                try:
                    htxt = heart("owner", "", spoken) or ""
                except Exception as exc:
                    _log("heart failed: %r" % (exc,))
            payload = {"messages": history + [{"role": "user", "content": content}],
                       "_heart": htxt}
            msgs = build(payload, "pet")
            body = {
                "model": _ns["MODEL"],
                "messages": msgs,
                "temperature": 0.8,
                "max_tokens": int(_conf.get("max_tokens") or 220),
                "stream": False,
            }
            resp = _ns["post_chat"](body, timeout=120, accept="application/json")
            data = json.loads(resp.read().decode("utf-8", "replace"))
            txt = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
            return _ns["clean_reply"](txt) or txt

        build = _ns["build_messages"]
        t0 = time.time()
        reply = _ask(imgs)
        if not reply.strip() and imgs:
            # maas 那边的视觉偶尔交白卷：压小一点再问一次，别让挂件干等
            _log("空回话（图%d），压小重试" % len(imgs))
            small = [_shrink(b, 768, 70) or b for b in imgs]
            reply = _ask(small)
        if not reply.strip():
            _log("空回话，跳过")
            return
        try:
            _ns["record"](spoken or "（看了一眼屏幕）", reply, "pet")
        except Exception as exc:
            _log("record failed: %r" % (exc,))
        self._remember(spoken, reply)
        self.deliver(reply)
        _log("%s %.1fs -> %d 字 图%d 历史%d" % (kind, time.time() - t0, len(reply),
                                               len(imgs), len(self.hist)));

    def _remember(self, user_text, reply):
        """留最近几轮原文（只留字，不留图），下一句一起发过去。"""
        u = (user_text or "").strip()
        if not u or not (reply or "").strip():
            return
        keep = max(1, int(_conf.get("history_turns") or 6))
        with self.lock:
            self.hist.append({"role": "user", "content": u[:300]})
            self.hist.append({"role": "assistant", "content": reply.strip()[:300]})
            self.hist = self.hist[-keep * 2:]
            self.state["历史"] = len(self.hist) // 2

    def _echo(self, text):
        """自己听自己：朗读期间 + 和上一句回话高度相似 → 认为麦克风听见了她自己。"""
        now = time.time()
        if now < self.speaking_until:
            return True
        said, at = self.last_reply
        if not said or now - at > 20:
            return False
        a, b = text.strip(), said.strip()
        if not a or not b:
            return False
        if a in b or b.startswith(a[:max(4, len(a) // 2)]):
            return True
        return difflib.SequenceMatcher(None, a, b).ratio() >= 0.55

    def deliver(self, reply):
        self.last_reply = (reply, time.time())
        url = str(_conf.get("reply") or "")
        if url:
            try:
                req = urllib.request.Request(
                    url,
                    data=json.dumps({"text": reply}, ensure_ascii=False).encode("utf-8"),
                    headers={"content-type": "application/json; charset=utf-8"},
                    method="POST",
                )
                urllib.request.urlopen(req, timeout=5).read()
                self.state["回话"] += 1
            except Exception as exc:
                _log("回话没递到 %s: %r" % (url, exc))
        if _conf.get("voice", True) and _ns.get("mod_on", lambda x: True)("voice"):
            threading.Thread(target=self._speak, args=(reply,), daemon=True).start()

    def _speak(self, text):
        """分句流水线：第一句合成完就开口，后面的边播边合成，不等整段。"""
        # 中文 TTS 约 5 字/秒，留 1.5s 尾巴
        # edge 合成实测 4~14s，按字数预估会留出空窗漏回声；挡板改在 _play 前按当前分句设置
        self.speaking_until = 0.0
        try:
            from core import tts
        except Exception as exc:
            _log("朗读失败: %r" % (exc,))
            return
        parts = _sentences(text)
        t_all = time.time()
        box = queue.Queue()

        def synth():
            for i, s in enumerate(parts):
                try:
                    mp3, how = tts.speak(s, stamp=int(time.time() * 1000) + i)
                except Exception as exc:
                    _log("朗读失败(%d/%d): %r" % (i + 1, len(parts), exc))
                    mp3, how = None, "err"
                box.put((mp3, how, s))
            box.put(None)

        threading.Thread(target=synth, name="tts-synth", daemon=True).start()
        played = 0
        while True:
            item = box.get()
            if item is None:
                break
            mp3, how, s = item
            if not mp3:
                continue
            if played == 0:
                _log("开口 %.0fms（共 %d 句，%s）"
                     % ((time.time() - t_all) * 1000, len(parts), how))
            played += 1
            try:
                self.speaking_until = time.time() + min(30.0, 1.5 + len(s) / 5.0)
                _play(mp3)
            except Exception as exc:
                _log("播放失败: %r" % (exc,))
            finally:
                self.speaking_until = time.time() + 2.0
        if not played:
            _log("朗读没出声：%s" % (text[:20],))


def _sentences(text):
    """把回复切成能马上开口的小句：先按标点断，太长的再按逗号补切。"""
    text = (text or "").strip()
    if not text:
        return []
    out = []
    for seg in re.split(r"(?<=[。！？!?；;\n])", text):
        seg = seg.strip()
        while len(seg) > 40:
            cut = max(seg.rfind("，", 0, 40), seg.rfind(",", 0, 40), seg.rfind(" ", 0, 40))
            if cut <= 0:
                cut = 40
            out.append(seg[:cut + 1].strip())
            seg = seg[cut + 1:].strip()
        if seg:
            out.append(seg)
    return out or [text]


def install(ns):
    global _inst, _conf
    missing = [k for k in NEEDED if k not in ns]
    if missing:
        ns["log"]("hands 没装：缺 %s" % ", ".join(missing))
        return None
    _conf = _load_conf()
    _inst = Hands(ns)
    if _conf.get("on", True):
        _inst.start()
    else:
        _log("hands.json 里 on=false，不开口子")
    return _inst


def state():
    if _inst is None:
        return {"起没起": False, "错": "没装"}
    d = dict(_inst.state)
    d["游戏"] = _inst.game
    d["陪看"] = _inst.watching
    d["字幕"] = len(_inst.subs)
    d["抓帧"] = FRAME["shots"]
    d["抓帧跳过"] = FRAME["skip"]
    d["复用"] = FRAME["reuse"]
    d["配额"] = _inst.frames_left()
    d["攒图"] = len(_inst.frames)
    d["口子"] = int(_conf.get("port") or 8777)
    d["回声"] = _inst.state.get("回声", 0)
    d["失败"] = _inst.failed
    hs = _ns.get("heart_state")
    if callable(hs):
        try:
            d["心情"] = hs()
        except Exception as exc:
            _log("heart state failed: %r" % (exc,))
    return d
