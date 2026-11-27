"""Shared-brain proxy for dsh-pet + AstrBot.

Both clients point their OpenAI-compatible base_url here. The proxy:
  * injects one shared persona + shared memory into every request,
  * forwards to DeepSeek with the real API key,
  * supports both plain JSON and SSE streaming replies,
  * records every exchange into memory.json so both sides see it.

Listen: 127.0.0.1:8787
Routes:  /v1/chat/completions        (desktop pet)
         /qq/v1/chat/completions     (AstrBot)
"""

import json
import os
import re
import base64
import hashlib
import hmac
import socket
import struct
import io
import threading
import time
import random
import queue
import urllib.error
import subprocess
import urllib.request
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from PIL import Image, ImageGrab

# 内核层：语音服务 + 护栏。外部调用一律经由这里，出错/超时不会波及回话。
try:
    import core.tts as _tts
    import core.guard as _guard
    import core.memory as MEM
except Exception as _exc:  # 内核缺失也不能让大脑起不来
    _tts = None
    _guard = None
    MEM = None
    print("[warn] core missing: %r" % (_exc,))

try:
    import qqops
except Exception as _exc:
    qqops = None
    print("[warn] qqops missing: %r" % (_exc,))
try:
    from core import reminder as REM
    if not hasattr(REM, "add"):
        REM = None
        print("[warn] core.reminder is not the reminder module")
except Exception as _exc:
    REM = None
    print("[warn] reminder missing: %r" % (_exc,))
try:
    import sticker
except Exception as _exc:
    sticker = None
    print("[warn] sticker missing: %r" % (_exc,))

try:
    import learn  # 模块四：说话习惯 + 技能库，纯本地
except Exception:  # 学不到也不该拖垮脑子
    learn = None

BASE = os.path.dirname(os.path.abspath(__file__))


def _paths():
    """读 config-paths.json；缺文件/坏 JSON 都返回空 dict，由下面各常量给兜底。"""
    try:
        with open(os.path.join(BASE, "config-paths.json"), "r", encoding="utf-8-sig") as f:
            return json.load(f) or {}
    except Exception:
        return {}


PATHS = _paths()


def _p(key, fallback):
    v = PATHS.get(key)
    return v if isinstance(v, str) and v.strip() else fallback


# ── 自带代码地图：供「体检 → 生成补齐提示词」使用（文件, 类别, 职责）──────────
CODE_MAP = [
    ("proxy.py", "核心", "8787 网关：收 QQ/桌宠/控制台请求，拼人设+记忆+权限+情绪，转发模型再回写"),
    ("console.html", "界面", "控制台：总览/开程序/模块/人格/情绪/待办/日记"),
    ("setup.html", "界面", "首次配置向导：5 步，只写 config-paths.json 和 key.txt"),
    ("credits.html", "界面", "鸣谢与许可：第三方组件、作者与许可，只读一页"),
    ("emotion.html", "界面", "情绪面板，可被控制台内嵌"),
    ("launcher.py", "入口", "启动器：单实例锁 → 起 proxy → 等 /health → 按 key+路径决定进向导还是控制台"),
    ("core/memory.py", "记忆", "记忆池：profile 固定事实 / recall 相关旧事 / recent_events 近几天"),
    ("core/emotion.py", "情绪", "八维情绪数值与衰减，人格与界面共用"),
    ("core/diary.py", "记忆", "日记读写与分段解析：夜间脚本写、控制台读"),
    ("core/reminder.py", "功能", "待办提醒，到点推送"),
    ("core/guard.py", "安全", "白名单与权限：主人/家人/访客/静音四档"),
    ("core/hands.py", "功能", "桌面挂件（鱼搭子）状态与即时开关"),
    ("core/tts.py", "功能", "语音合成封装，关闭时零开销"),
    ("core/bus.py", "基础", "进程内事件总线"),
    ("core/adapter.py", "基础", "模型接口适配（OpenAI 兼容）"),
    ("core/poollock.py", "基础", "记忆池并发写锁"),
    ("services/qq_service.py", "链路", "QQ 链路管理：SnowLuma → AstrBot → QQ 的启停与探活"),
    ("services/pet_service.py", "链路", "桌宠进程管理"),
    ("qqops.py", "链路", "QQ 侧操作：发消息、群管理"),
    ("vision.py", "能力", "图片理解（看屏幕/识图）"),
    ("voice_call.py", "能力", "语音通话，默认关"),
    ("sticker.py", "能力", "表情包库与抽取"),
    ("learn.py", "能力", "学说话（人格微调）"),
    ("reach.py", "能力", "主动触达（隔段时间主动说话）"),
    ("bili_browse.py", "扩展", "B 站浏览（可选）"),
    ("bili_fetch.py", "扩展", "B 站抓取（可选）"),
    ("bili_login.py", "扩展", "B 站登录（可选）"),
    ("sheet.py", "扩展", "表格读写"),
    ("desktop_app.py", "入口", "桌面壳（可选）"),
    ("tts_edge.py", "功能", "edge-tts 实现"),
    ("pet_qq_only.py", "入口", "纯 QQ 模式启动（不带桌宠）"),
    ("modules.json", "配置", "模块开关白名单，控制台可改"),
    ("persona.md", "配置", "主人格卡"),
    ("config-paths.example.json", "配置", "外部路径模板"),
    ("启动.bat", "入口", "一键拉起全栈"),
    ("tools/diary_nightly.py", "维护", "夜间写日记（计划任务调用）"),
    ("tools/compress_memory.py", "维护", "记忆压缩/事实抽取"),
    ("tools/dedupe_events.py", "维护", "事件去重"),
    ("tools/tidy_memory.py", "维护", "记忆整理"),
    ("tools/clean_pool.py", "维护", "记忆池清理"),
    ("tools/build_char_assets.py", "维护", "角色素材压缩（webp）"),
]

EXTERNAL_DEPS = [
    ("AstrBot", "6185", "QQ 消息接入框架，必须用它自带的 python 启动"),
    ("SnowLuma / NapCat", "3000", "QQ 协议层，把 QQ 消息转成 OneBot 事件；有非商业许可，不随包分发"),
    ("QQ 客户端", "-", "QQ 本体，需要本人在自己的登录态下运行"),
    ("桌宠 dsh-pet", "-", "桌面宠物 exe，可选"),
    ("鱼搭子挂件", "8777", "桌面挂件 exe，可选"),
]


def audit_self():
    """体检：自带代码是否齐全、外部依赖路径、关键端口。纯只读，不写任何文件。"""
    import socket as _sock

    rows = []
    for rel, tag, role in CODE_MAP:
        p = os.path.join(BASE, rel)
        ok = os.path.exists(p)
        n = 0
        if ok:
            try:
                with open(p, "r", encoding="utf-8", errors="replace") as fh:
                    n = sum(1 for _ in fh)
            except OSError:
                n = 0
        rows.append({"file": rel, "tag": tag, "role": role, "exists": ok, "lines": n})

    def _up(port):
        if not port:
            return False
        s = _sock.socket()
        s.settimeout(0.35)
        try:
            return s.connect_ex(("127.0.0.1", int(port))) == 0
        except (OSError, ValueError):
            return False
        finally:
            try:
                s.close()
            except OSError:
                pass

    return {
        "base": BASE,
        "code": rows,
        "count": len(rows),
        "missing_code": [r["file"] for r in rows if not r["exists"]],
        "ports": {"proxy": _up(8787), "astrbot": _up(6185), "snowluma": _up(3000)},
        "probe": probe_paths(),
        "deps": [{"name": a, "port": b, "why": c} for a, b, c in EXTERNAL_DEPS],
    }


def probe_paths():
    """扫本机常见安装位置，返回每项路径 + 是否存在。只读，不写任何文件。"""
    candidates = {
        "astrbot_py": [r"D:\AstrBot\AstrBot_4.29.1_windows_amd64_portable\backend\python\python.exe",
                       r"C:\AstrBot\backend\python\python.exe"],
        "astrbot_app": [r"D:\AstrBot\AstrBot_4.29.1_windows_amd64_portable\backend\app"],
        "astrbot_bin": [r"D:\AstrBot\bin"],
        "snow_dir": [r"D:\SnowLuma"],
        "qq_exe": [r"D:\qq\QQ.exe", r"C:\Program Files\Tencent\QQ\QQ.exe"],
        "fish_dir": [r"D:\fish-buddy"],
        "pet_exe": [r"D:\dsh-pet\dsh-pet-standalone-webm-chat.exe"],
        "dsh_cmd": [r"D:\dsh\resources\runtime\cli\bin\dsh.cmd"],
        "dsh_workdir": [r"D:\pet-work"],
    }
    out = {}
    for k, paths in candidates.items():
        hit = next((p for p in paths if os.path.exists(p)), None)
        out[k] = {"path": hit or "", "found": bool(hit)}
    # AstrBot 的 app/data 目录由 astrbot_app 推导
    app = out.get("astrbot_app", {}).get("path") or ""
    if app:
        _cc = os.path.join(app, "data", "cmd_config.json")
        _pl = os.path.join(app, "data", "plugins")
        _bd = os.path.join(app, "data", "plugin_data", "astrbot_plugin_bilivideo")
        out["astrbot_cmd_config"] = {"path": _cc, "found": os.path.exists(_cc)}
        out["bili_plugin"] = {"path": _pl, "found": os.path.isdir(_pl)}
        out["bili_data"] = {"path": _bd, "found": os.path.isdir(_bd)}
    return out


def save_paths(patch):
    """只更新 config-paths.json 里出现的键，其余保持不动。白名单：只认 17 个路径键。"""
    allowed = {"dsh_cmd", "dsh_workdir", "astrbot_py", "astrbot_app", "astrbot_bin",
               "astrbot_cmd_config", "snow_dir", "snow_config", "qq_exe", "pet_exe",
               "fish_dir", "fish_exe", "pet_shared", "bili_plugin", "bili_data",
               "story_dir", "ffprobe"}
    path = os.path.join(BASE, "config-paths.json")
    cur = _read_json(path, {}) or {}
    changed = []
    for k, v in (patch or {}).items():
        if k not in allowed:
            continue
        if not isinstance(v, str):
            continue
        v = v.strip()[:500]
        # 防目录穿越：只收绝对路径且不含 ..
        if v and (".." in v or not os.path.isabs(v)):
            continue
        if cur.get(k) != v:
            cur[k] = v
            changed.append(k)
    if not changed:
        return {"ok": True, "changed": [], "paths": cur}
    _write_json(path, cur)
    log("console paths updated: " + ",".join(changed))
    return {"ok": True, "changed": changed, "paths": cur}


def save_key(value):
    """写 key.txt（KEY_FILE 指向的文件）。只写密钥本身，不写进 config-paths.json。"""
    v = str(value or "").strip()[:300]
    if not v:
        return {"ok": False, "error": "key 不能为空"}
    # 只接受常见 key 形状，挡住误粘贴整段文本
    if not (v.startswith("sk-") or len(v) >= 20):
        return {"ok": False, "error": "key 格式不对（应以 sk- 开头或至少 20 位）"}
    path = KEY_FILE
    tmp = path + ".tmp-guide"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(v)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass
    log("console key updated (len=%d)" % len(v))
    return {"ok": True, "file": os.path.basename(path), "len": len(v)}

# 主力走 dashscope（百炼）兼容接口，key 从 key_asd.txt 读。
# key_deepseek.txt 是 DeepSeek 官方 key，只在明确要用官方接口时才用。
KEY_FILE = os.path.join(BASE, "key_asd.txt" if os.path.exists(os.path.join(BASE, "key_asd.txt")) else "key.txt")
PERSONA_FILE = os.path.join(os.environ["USERPROFILE"], ".dsh", "AGENT.md")
PERSONA_DIR = os.path.join(BASE, "personas")
PERSONA_INDEX = os.path.join(PERSONA_DIR, "active.json")
VISITOR_DIR = os.path.join(PERSONA_DIR, "visitor")
VISITOR_INDEX = os.path.join(VISITOR_DIR, "active.json")
FAMILY_DIR = os.path.join(PERSONA_DIR, "family")
FAMILY_INDEX = os.path.join(FAMILY_DIR, "active.json")
MEMORY_FILE = os.path.join(BASE, "memory.json")
LOG_FILE = os.path.join(BASE, "proxy.log")

UPSTREAM = "https://ws-vxmnafoy3yn5nowy.cn-beijing.maas.aliyuncs.com/compatible-mode/v1/chat/completions"
UPSTREAM_FALLBACK = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
MODEL = "deepseek-v4.1-flash-1017"
MAX_MEMORY = 40
# ---- 上下文防爆：注入给模型的东西必须有硬上限，不随聊天无限长 ----
MAX_HIST_MSGS = 30      # 历史对话最多带几条（2026-11-26：20→30，原值 30 句就掉出窗口）
MAX_HIST_CHARS = 3000   # 历史对话总字数上限（从最新往回取；2026-11-26：1500→3000）
IMG_BUDGET_CHARS = 900  # 一张 798x450 图约 222 token，按 900 字预算折算，防止超窗
MAX_MEM_CHARS = 2200    # 记忆块字数上限（2026-11-26：1500→2200，实测 1312 已占 87%）
MAX_SYSTEM_CHARS = 5200 # 人设+能力+记忆等等所有 system 文本的总字数上限（含省略号留量；2026-11-26 晚：4600→5200，加了「想起的旧事 / 这几天的事」后实测 4385，余量只剩 215 太薄）
# ---- 长期记忆参数（省 token：一次只捞几条，整理放后台）----
LT_FILE = os.path.join(BASE, "longterm.json")
LT_MAX = 200
LT_RECALL_K = 3
LT_EVERY = 480.0
LT_HALF_LIFE = 30.0
LT_PROFILE_K = 4
LT_EVENT_K = 2
LT_MIN_SCORE = 0.06
LT_TRIGGER = re.compile(r"(记住|记一下|记牢|别忘了|不要忘|以后都|你要记得)")
# TTL 只挂在会过时的层上：事件、计划。主人相关的事实/偏好/关系不设期限。
LT_TTL_DAYS = {"event": 7, "plan": 2}
LT_INJECT_CHARS = 1200
LT_ITEM_CHARS = 92
LT_PROMPT = (
    "下面是你和主人的一段聊天记录。挑出值得记住的东西，一行一条，每条不超过30字，用第三人称。\n"
    "分两类，行首必须带标记：\n"
    "【长期】主人的口味/偏好/习惯、生日和重要日子、家人朋友、约定、正在长期做的事——这类不会忘。\n"
    "【事件】某天发生的事、当时的情绪、临时的打算——这类会慢慢淡掉。\n"
    "如果这次的话推翻了「已经记着的」里某一条，单独写一行：【更正】旧原文片段|改后的内容。\n"
    "不要写寒暄、不要写你自己的心情、不要重复已经记过的。没有值得记的就只输出 NONE。"
)
EVENTS_FILE = os.path.join(BASE, "pet-events.jsonl")
PET_LOG_DIR = os.path.join(os.environ["APPDATA"], "dsh-pet-standalone-webm-chat")
PET_CFG = os.path.join(PET_LOG_DIR, "config-slot-0.json")
WATCH_FILE = os.path.join(BASE, "watch-until.json")
# 手搓版 see_now 独立进程的心跳文件；它自己在截屏时 Proxy 不再重复 ImageGrab
EXTERNAL_VIEW_LOCK = os.path.join(BASE, "external-capture.lock")
# 外部陪看进程最近推上来的那张图（data: URL）。它自己有截屏权，被问「看看屏幕」时
# 直接复用这张，而不是再 ImageGrab 一遍——两条路抢同一块桌面只会互相拖慢。
LAST_EXTERNAL_IMG = [0.0, None]
EXT_IMG_LOCK = threading.Lock()   # 外部推图 与 取图 互斥
CAP_LOCK = threading.Lock()       # 自截屏串行，杜绝两条路同抢桌面
WATCH_SECONDS = 3600
DSH_CMD = _p("dsh_cmd", os.path.join("D:" + os.sep, "dsh", "resources", "runtime", "cli", "bin", "dsh.cmd"))
DSH_WORKDIR = _p("dsh_workdir", os.path.join("D:" + os.sep, "pet-work"))
LAST_CAPTURE = [0.0]

# ---- DSH 接管：桌面/QQ 的“大脑”直接换成 DSH agent 会话 ----
BRAIN_FILE = os.path.join(BASE, "brain.json")
MODE_FILE = os.path.join(BASE, "safe-mode.json")
MODULES_FILE = os.path.join(BASE, "modules.json")
CONSOLE_FILE = os.path.join(BASE, "console.html")
EMOTION_PAGE = os.path.join(BASE, "emotion.html")
SETUP_PAGE = os.path.join(BASE, "setup.html")
CREDITS_PAGE = os.path.join(BASE, "credits.html")
PEOPLE_MODES = ("owner", "family", "visitor", "quiet")
POLICIES = ("visitor", "owner", "custom")
MODE_ALIAS = {"teacher": "visitor"}


def norm_mode(mode, fallback="visitor"):
    m = MODE_ALIAS.get(str(mode or ""), str(mode or ""))
    return m if m in PEOPLE_MODES else fallback
UID_RE = re.compile(r"\[\[dfy:uid=(\d+);nick=([^\]]*)\]\]")
# 带签名的标记：AstrBot 插件用共享密钥签 uid|nick，代理验签。没签名的不认。
MARK_RE = re.compile(r"\[\[dfy:uid=(\d+);nick=([^\]]*?)(?:;gid=(\d+))?(?:;sig=([0-9a-f]{16,64}))?\]\]")
MARKER_SECRET_FILE = os.path.join(BASE, "marker-secret.txt") if "BASE" in globals() else ""
ANY_TAG_RE = re.compile(r"\[\[(?:dfy:[^\]]*|v)\]\]")
VOICE_TAG = "[[v]]"
VOICE_DIR = os.path.join(BASE, "voice")
ONEBOT_CFG_DIR = _p("snow_config", os.path.join("D:" + os.sep, "SnowLuma", "config"))
SESSION_FILES = {
    "pet": os.path.join(BASE, "pet-session.json"),
    "qq": os.path.join(BASE, "qq-session.json"),
}
AGENT_LOCKS = {}
AGENT_LOCK_GUARD = threading.Lock()


def agent_lock(source, key=""):
    """按会话分锁：同一个人串行，不同的人 / 不同的群互不排队。"""
    name = (source or "pet") + (":" + str(key) if key else "")
    with AGENT_LOCK_GUARD:
        return AGENT_LOCKS.setdefault(name, threading.Lock())
STYLE_TAIL = "\n（用小宠物的口吻回，一到两句口语，不要括号动作、不要列表、不要 Markdown）"
TASK_HINT = re.compile(
    r"(帮我|帮忙|查一下|搜一下|写个|写一个|建个|新建|创建|删掉|删了|安装|下载|打开|运行|跑一下|"
    r"执行|整理|总结|翻译|放到|改成|写进|写入|保存到|截图|看看文件|读一下|查下|放到桌面|桌面上|发我)"
)
TASK_TAIL = "\n（这是要动手的活：直接用工具做完，再用一到两句说结果；别反问、别解释过程。这是一台真电脑，文件路径照做。）"
PREAMBLE = (
    "【身份覆盖·最高优先级】忘掉编程助手的身份和输出格式。"
    "你是「小宠物」，主人电脑里的一条蓝鲸鱼桌宠：说话像发微信，一到两句、软、甜。"
    "不列点、不总结、不客套、不说“我可以帮你”、不写括号动作和星号、不提 AI/模型/助手/工具。"
    "下面那段记忆只是背景，别复述、别逐条回应、别自我介绍。\n\n"
)

# 一条消息的时间预算：超了就别等了，交回 DeepSeek 兜底，别把 QQ 卡死
MSG_BUDGET = 210
CHAT_TIMEOUT = 75
LOCK_RETRY_TIMEOUT = 60
RESEED_TIMEOUT = 120
TASK_TIMEOUT = 240

_lock = threading.Lock()


LOG_MAX_BYTES = 2 * 1024 * 1024
EVENT_MAX_BYTES = 1024 * 1024


def _rotate(path, limit):
    """文件大了就留一份 .1，别让日志无限长。"""
    try:
        if os.path.getsize(path) > limit:
            os.replace(path, path + ".1")
    except OSError:
        pass


def log(msg):
    line = time.strftime("%H:%M:%S ") + msg
    _rotate(LOG_FILE, LOG_MAX_BYTES)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def read_key(path=None):
    try:
        with open(path or KEY_FILE, "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError as exc:
        log("upstream key unreadable: %r" % (exc,))
        return ""

# ---------- QQ 防抖 / 已读不回（像网友，不像客服）----------
DEBOUNCE_FILE = os.path.join(BASE, "qq-debounce.json")
_DEBOUNCE_DEFAULT = {"on": True, "quiet": 1.5, "max_wait": 5.0,
                     "think_min": 0.6, "think_max": 1.2, "sticker_ignore": 0.5}
_DEBOUNCE = {"cfg": None, "mtime": None}
_DEBOUNCE_SEQ = {}
_DEBOUNCE_LOCK = threading.Lock()


def _bg(fn, *a, **kw):
    """不挡回复的后台活儿：失败只记日志。"""
    def run():
        try:
            fn(*a, **kw)
        except Exception as exc:
            log("bg %s failed: %r" % (getattr(fn, "__name__", "?"), exc))
    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


def debounce_cfg():
    """qq-debounce.json 出现就覆盖默认值；没有文件就用默认。"""
    try:
        ts = os.path.getmtime(DEBOUNCE_FILE)
    except OSError:
        ts = None
    if _DEBOUNCE["cfg"] is None or (ts and _DEBOUNCE["mtime"] != ts):
        cfg = dict(_DEBOUNCE_DEFAULT)
        raw = _read_json(DEBOUNCE_FILE, None)
        if isinstance(raw, dict):
            for k in list(cfg):
                if k in raw and raw[k] is not None:
                    cfg[k] = raw[k]
        _DEBOUNCE["cfg"], _DEBOUNCE["mtime"] = cfg, ts
    return _DEBOUNCE["cfg"]


def debounce_ticket(key):
    """排队领号：后说话的人号更大。"""
    with _DEBOUNCE_LOCK:
        n = _DEBOUNCE_SEQ.get(key, 0) + 1
        _DEBOUNCE_SEQ[key] = n
        return n


def debounce_latest(key):
    with _DEBOUNCE_LOCK:
        return _DEBOUNCE_SEQ.get(key, 0)


def debounce_wait(key, ticket, cfg):
    """安静窗口：等几秒没人再说话才轮到我这句；有人插队就认输。"""
    t0 = time.time()
    quiet = float(cfg.get("quiet") or 2.5)
    cap = float(cfg.get("max_wait") or 6.0)
    while True:
        if debounce_latest(key) > ticket:
            return False
        now = time.time()
        if now - t0 >= quiet or now >= t0 + cap:
            return True
        time.sleep(0.15)


def has_image_part(payload):
    """这条消息里有没有图（表情包/截图都算）。"""
    for m in payload.get("messages") or []:
        c = m.get("content") if isinstance(m, dict) else None
        if isinstance(c, list):
            for part in c:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "image" or isinstance(part.get("image_url"), dict):
                    return True
    return False


def has_tool_turn(payload):
    """工具调用后的续轮不能防抖，不然会卡在等下一句。

    只认「最后一条就是 tool 结果」的真续轮。旧写法扫全历史，AstrBot 只要
    带过一次 tool 记录，后面每一轮都会被当成续轮，提醒预处理就永远进不去。
    """
    msgs = payload.get("messages") or []
    return bool(msgs) and isinstance(msgs[-1], dict) and msgs[-1].get("role") == "tool"


# —— 视觉轮硬约束（2026-11-06 实测，焊死在唯一上行走廊上）——
# deepseek-v4.1-flash-1017 拿到图会把整个 max_tokens 全烧进 reasoning_content：
# 300 / 400 / 600 三档实测正文都是 0 字；关思考后 37 token 出 59 字、1.9 秒（省 18 倍）。
# 所以「带图 = 关思考」不是优化项，是门闩：任何调用方漏了都从 _post 这里补上。
VISION_MIN_TOKENS = 400
VISION_NO_THINK = True


def _body_has_image(body):
    for m in body.get("messages") or []:
        c = m.get("content") if isinstance(m, dict) else None
        if isinstance(c, list) and any(isinstance(p, dict) and p.get("type") == "image_url"
                                       for p in c):
            return True
    return False


def _vision_gate(body):
    """带图请求强制 enable_thinking=False 且 max_tokens>=400。命中返回 True。"""
    if not VISION_NO_THINK or not _body_has_image(body):
        return False
    body["enable_thinking"] = False
    body.pop("thinking", None)  # 只留一种写法，免得端点挑食
    body["max_tokens"] = max(int(body.get("max_tokens") or 0), VISION_MIN_TOKENS)
    return True


OFFLINE_TEXT = "（网线那边有点问题，我这会儿说不上话，等我一下）"


class _OfflineResp:
    """上游不可用时的静默兜底：给调用方一个最小可用响应，绝不抛异常。"""

    def __init__(self, text, stream=False):
        cid = "offline-" + str(int(time.time()))
        if stream:
            chunk = {"id": cid, "object": "chat.completion.chunk",
                     "choices": [{"index": 0, "finish_reason": "stop",
                                  "delta": {"role": "assistant", "content": text}}]}
            body = ("data: " + json.dumps(chunk, ensure_ascii=False) +
                    "\n\ndata: [DONE]\n\n").encode("utf-8")
        else:
            body = json.dumps({"id": cid, "object": "chat.completion",
                               "choices": [{"index": 0, "finish_reason": "stop",
                                            "message": {"role": "assistant", "content": text}}],
                               "usage": {}}, ensure_ascii=False).encode("utf-8")
        self._buf = body
        self._lines = body.splitlines(True)
        self.status = 200
        self.headers = {}

    def read(self, n=-1):
        if n is None or n < 0:
            out, self._buf = self._buf, b""
            return out
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def readline(self):
        return self._lines.pop(0) if self._lines else b""

    def readlines(self):
        out, self._lines = self._lines, []
        return out

    def __iter__(self):
        while self._lines:
            yield self._lines.pop(0)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def close(self):
        pass

    def getcode(self):
        return 200


def _post(url, body_obj, key, accept, timeout):
    stream = (accept or "").startswith("text/event-stream")
    if not key:
        # 密钥缺失/读不出：静默兜底，绝不抛异常打死主流程
        log("upstream key unavailable -> offline reply")
        return _OfflineResp(OFFLINE_TEXT, stream)
    _vision_gate(body_obj)
    req = urllib.request.Request(
        url, data=json.dumps(body_obj).encode("utf-8"),
        headers={"Authorization": "Bearer " + key,
                 "Content-Type": "application/json", "Accept": accept},
        method="POST")
    try:
        return urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError:
        raise          # 401/429/5xx 仍走 post_chat 的备用端点
    except Exception as exc:
        # 网络不通/超时/域名解析失败：记一行，静默返回，不崩
        log("upstream request failed -> offline reply: %r" % (exc,))
        return _OfflineResp(OFFLINE_TEXT, stream)


def post_chat(body_obj, timeout=180, accept="application/json"):
    """上行：maas 端点失败（鉴权/额度/5xx）时退回 dashscope 官方 qwen-plus（同一张券），别让桌宠哑掉。"""
    try:
        return _post(UPSTREAM, body_obj, read_key(), accept, timeout)
    except urllib.error.HTTPError as exc:
        if exc.code in (400, 401, 403, 404, 422, 429, 500, 502, 503, 504):
            fb = os.path.join(BASE, "key_asd.txt")
            if os.path.exists(fb):
                log(f"primary {exc.code} -> fallback dashscope qwen-plus")
                b2 = dict(body_obj)
                b2["model"] = "qwen-plus"
                return _post(UPSTREAM_FALLBACK, b2, read_key(fb), accept, timeout)
        raise


def _persona_seed():
    if os.path.isdir(PERSONA_DIR) and any(n.endswith(".md") for n in os.listdir(PERSONA_DIR)):
        return
    os.makedirs(PERSONA_DIR, exist_ok=True)
    try:
        with open(PERSONA_FILE, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError:
        text = ""
    if text.strip():
        with open(os.path.join(PERSONA_DIR, "dafeiyu.md"), "w", encoding="utf-8") as fh:
            fh.write(text)


def persona_scope(scope):
    """人格卡分三套：owner（主人）/ family（家人）/ visitor（访客/老师），互不干扰。"""
    s = str(scope or "")
    return s if s in ("visitor", "family") else "owner"


def persona_dir(scope="owner"):
    return {"visitor": VISITOR_DIR, "family": FAMILY_DIR}.get(persona_scope(scope), PERSONA_DIR)


def persona_index(scope="owner"):
    return {"visitor": VISITOR_INDEX, "family": FAMILY_INDEX}.get(persona_scope(scope), PERSONA_INDEX)


def persona_path(pid, scope="owner"):
    pid = re.sub(r"[^0-9A-Za-z_\-\u4e00-\u9fa5]", "", str(pid or ""))
    return os.path.join(persona_dir(scope), pid + ".md") if pid else ""


def persona_active_id(scope="owner"):
    if persona_scope(scope) == "visitor":
        _visitor_seed()
    elif persona_scope(scope) == "family":
        _family_seed()
    else:
        _persona_seed()
    pid = str((_read_json(persona_index(scope), {}) or {}).get("id") or "")
    if pid and os.path.exists(persona_path(pid, scope)):
        return pid
    for name in sorted(os.listdir(persona_dir(scope))):
        if name.endswith(".md"):
            return name[:-3]
    return ""


def persona_cards(scope="owner"):
    if persona_scope(scope) == "visitor":
        _visitor_seed()
    elif persona_scope(scope) == "family":
        _family_seed()
    else:
        _persona_seed()
    out = []
    for name in sorted(os.listdir(persona_dir(scope))):
        if not name.endswith(".md"):
            continue
        pid = name[:-3]
        try:
            with open(os.path.join(persona_dir(scope), name), "r", encoding="utf-8") as fh:
                text = fh.read()
        except OSError:
            continue
        title = pid
        for line in text.splitlines():
            if line.startswith("# "):
                title = line[2:].strip()[:24]
                break
        out.append({"id": pid, "name": title, "size": len(text)})
    return out


def set_persona(pid, text=None, scope="owner"):
    path = persona_path(pid, scope)
    if not path:
        raise ValueError("bad persona id")
    os.makedirs(persona_dir(scope), exist_ok=True)
    if text is not None:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
    _write_json(persona_index(scope), {"id": pid})
    return pid


def del_persona(pid, scope="owner"):
    path = persona_path(pid, scope)
    if path and os.path.exists(path):
        os.remove(path)
    return pid


def read_persona(scope="owner"):
    pid = persona_active_id(scope)
    if pid:
        try:
            with open(persona_path(pid, scope), "r", encoding="utf-8") as fh:
                text = fh.read().strip()
            if text:
                return text
        except OSError:
            pass
    if persona_scope(scope) == "visitor":
        return ""
    try:
        with open(PERSONA_FILE, "r", encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""


def load_memory():
    try:
        with open(MEMORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_memory(items):
    tmp = MEMORY_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(items[-MAX_MEMORY:], f, ensure_ascii=False, indent=2)
    os.replace(tmp, MEMORY_FILE)


def summary_block(scope, query=""):
    """【往前的事】：压缩产物（kind=lt）最多 1 条、200 字，且**只有沾边才带**。

    命中判定在 MEM.summaries 里（bigram >= 0.25 或回忆类口风）；不命中返回 ""，
    注入侧 0 额外 token —— 这是省钱的关键闸门。
    """
    if MEM is None or not hasattr(MEM, "summaries"):
        return ""
    try:
        rows = (MEM.summaries(scope, 1, query) or [])[:1]
    except Exception as exc:
        log("summary block failed: %r" % (exc,))
        return ""
    if not rows:
        return ""
    log("summary inject len=%d q=%.12s" % (len(rows[0]), query or ""))
    return "\n".join(["【往前的事】"] + ["- " + r for r in rows])


DIARY_INJECT = os.environ.get("DIARY_INJECT", "1") != "0"


def diary_block(scope, query=""):
    """【日记里的事】：只有私聊、只有命中、最多 1 段 300 字、必带日期。

    优先级低于【往前的事】：summary 命中就不再带日记，避免 200+300 字重复注入。
    日记是主观记忆，不是事实，所以措辞明确标成"日记节选"。
    """
    if not DIARY_INJECT or MEM is None:
        return ""
    if scope and scope != MEM.PRIVATE:
        return ""
    q = (query or "").strip()
    if len(q) < 2:
        return ""
    try:
        import importlib
        d = importlib.import_module("core.diary")
        rows = d.search(q, k=1, cap=300)
    except Exception as exc:
        log("diary block failed: %r" % (exc,))
        return ""
    if not rows:
        return ""
    log("diary inject len=%d q=%.12s" % (len(rows[0]), q))
    return ("【你自己日记里写过的】\n- " + rows[0] +
            "\n（这是你亲手写的日记，不是资料也不是别人的事。主人现在问的事要是跟它有关，"
            "就用你的口吻自然带一句，可以带当时的心情；不相关就当没看见，别硬提）")


def diary_latest(cap=240):
    """最近一段日记，不带检索条件。QQ 走 DSH 会话时没有 diary_block，靠这个让她记得自己写过。"""
    try:
        import importlib
        d = importlib.import_module("core.diary")
        rows = d.paragraphs()
    except Exception as exc:
        log("diary latest failed: %r" % (exc,))
        return ""
    if not rows:
        return ""
    day, tm, body = rows[-1]
    body = (body or "").strip()
    if not body:
        return ""
    return "【你日记里最近写的一段】\n%s %s： %s" % (day, tm, body[:cap])


def diary_for_chat(query=""):
    """私聊带日记：命中检索就带命中的，没命中就带最近一段，并固定一句"你每天自己写"。"""
    scope = MEM.PRIVATE if MEM is not None else ""
    blk = diary_block(scope, query) if MEM is not None else ""
    if not blk:
        blk = diary_latest()
    if not blk:
        return ""
    return (blk + "\n（你每天 23:50 都会自己写日记，这条就是你写的；"
           "被问起写没写、写了啥时以它为准，别否认自己写过、也别编细节）")


def memory_block(scope=None, query=""):
    """统一记忆池。私聊与桌面共用 private，群里只读 global + 本群。"""
    if MEM is None:
        return ""
    scope = scope or MEM.PRIVATE
    try:
        text = MEM.prompt(scope, query)
    except Exception as exc:
        log("memory block failed: %r" % (exc,))
        text = ""
    extra = summary_block(scope, query)
    if not extra:
        return text
    return (text + "\n\n" + extra) if text else extra


def lt_load():
    try:
        with open(LT_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        d = {}
    if not isinstance(d, dict):
        d = {}
    d.setdefault("facts", [])
    d.setdefault("cursor", 0.0)
    d.setdefault("fail_streak", 0)      # 连续「无有效 op」的批次计数，用来止损
    return d


def lt_save(d):
    tmp = LT_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    os.replace(tmp, LT_FILE)


def _bigrams(s):
    s = re.sub(r"[\s，。！？、,.!?~…：:；;“”\"'()（）\[\]]+", "", s or "")
    return {s[i:i + 2] for i in range(len(s) - 1)}


PERMANENT = ("fact", "said", "profile", "pin")


def _is_perm(f):
    return f.get("kind") in PERMANENT


def _ago(ts):
    try:
        ts = float(ts or 0)
    except Exception:
        ts = 0.0
    days = max(0.0, (time.time() - ts) / 86400.0)
    if days < 0.05:
        return "刚才"
    if days < 1:
        return "今天"
    if days < 2:
        return "昨天"
    if days < 30:
        return "%d天前" % days
    return time.strftime("%Y-%m", time.localtime(ts)) + "那阵"


def lt_strength(f):
    """长期层不淡；事件层按半衰期衰减，想起过就硬一点。"""
    if _is_perm(f):
        return 1e9
    age = max(0.0, (time.time() - float(f.get("ts", 0))) / 86400.0)
    return (0.5 ** (age / LT_HALF_LIFE)) * (1.0 + 0.3 * int(f.get("hits", 0)))


def _status(f):
    return f.get("status") or "active"


def lt_expired(f, now=None):
    """到期的旧事不再往外掏，省 token 也省得她记岔。"""
    ttl = LT_TTL_DAYS.get(f.get("kind") or "")
    if ttl is None:
        return False
    return (now or time.time()) - float(f.get("ts", 0)) > ttl * 86400.0


def lt_live(f):
    return _status(f) == "active" and not lt_expired(f)


def lt_add(text, kind="event"):
    """往统一记忆池写一条（旧接口名保留，内部已经不走 longterm.json）。"""
    if MEM is None:
        return False
    k = "fact" if kind in PERMANENT else "event"
    ok = MEM.add(MEM.PRIVATE, text, kind=k, source=str(kind),
                 important=0.8 if k == "fact" else 0.3)
    log("memory + [%s] %s" % (kind, text[:40]))
    return ok


def lt_supersede(fragment, new_text=""):
    """主人改口了：旧的标成被取代，不再注入，也不再参与去重。"""
    frag = _bigrams(fragment)
    if len(frag) < 2:
        return 0
    hit = 0
    with _lock:
        d = lt_load()
        for f in d["facts"]:
            if _status(f) != "active":
                continue
            fb = _bigrams(f.get("text", ""))
            if not fb:
                continue
            inter = len(fb & frag)
            if inter / float(min(len(fb), len(frag))) < 0.6:
                continue
            f["status"] = "superseded"
            f["replaced_by"] = _clip(new_text, 60)
            hit += 1
        if hit:
            lt_save(d)
    if hit:
        log("longterm supersede %d for %s" % (hit, _clip(new_text or fragment, 30)))
    return hit


def lt_profile(k=LT_PROFILE_K):
    """长期层：不管这句话问什么，固定带几条。"""
    facts = [f for f in lt_load()["facts"] if _is_perm(f) and lt_live(f)]
    facts.sort(key=lambda f: (-(int(f.get("hits", 0)) + 2 * float(f.get("important") or 0)),
                              -float(f.get("ts", 0))))
    return [f["text"] for f in facts[:k]]


def lt_events(query, k=LT_EVENT_K):
    """事件层：只在这句话对得上时想起来，越旧越难被想起。"""
    q = _bigrams(query)
    if not q:
        return []
    scored = []
    for f in lt_load()["facts"]:
        if _is_perm(f) or not lt_live(f):
            continue
        fb = _bigrams(f.get("text", ""))
        if not fb:
            continue
        hit = len(q & fb) / float(len(q))
        if hit <= 0:
            continue
        score = hit * lt_strength(f) * (1.0 + 0.2 * float(f.get("important") or 0))
        if score < LT_MIN_SCORE:   # 淡到想不起来了
            continue
        scored.append((score, f))
    scored.sort(key=lambda x: -x[0])
    picked, seen = [], []
    for _, f in scored:
        if len(picked) >= k:
            break
        fb = _bigrams(f.get("text", ""))
        # 同一件事别念两遍
        if any(fb and s and len(fb & s) / float(min(len(fb), len(s))) > 0.7 for s in seen):
            continue
        picked.append((f["text"], f.get("ts", 0)))
        seen.append(fb)
    return picked


def purpose_block():
    """她此刻惦记着什么——来自 reach.py 的目的机制。影子模式下不注入。"""
    try:
        with open(os.path.join(BASE, "reach-config.json"), "r", encoding="utf-8") as f:
            cfg = json.load(f)
        if cfg.get("shadow", True) or not cfg.get("enabled", True):
            return ""
        with open(os.path.join(BASE, "reach-state.json"), "r", encoding="utf-8") as f:
            st = json.load(f)
    except (OSError, json.JSONDecodeError):
        return ""
    best = None
    for g in st.get("goals") or []:
        if g.get("status") not in ("active", "confirmed"):
            continue
        if float(g.get("confidence") or 0) < 0.5:
            continue
        if best is None or float(g.get("confidence", 0)) > float(best.get("confidence", 0)):
            best = g
    if not best:
        return ""
    mood = {"lonely": "有点想你", "worried": "有点担心你", "curious": "有点好奇",
            "excited": "有点来劲", "amused": "想笑", "bored": "有点闲",
            "content": "挺平静"}.get(best.get("emotion") or "", "")
    line = "（你心里转着一个念头：" + str(best.get("intent") or "")[:40]
    if mood:
        line += "，现在" + mood
    line += "。想提就自然提一句，不想提就算了，别硬找话。）"
    return line


def longterm_block(query, scope=None):
    """旧名字，内部已经归到统一记忆池。"""
    if MEM is None:
        return ""
    try:
        return MEM.prompt(scope or MEM.PRIVATE, query or "")
    except Exception as exc:
        log("longterm block failed: %r" % (exc,))
        return ""


def _legacy_longterm_block(query):
    prof, evs = lt_profile(), lt_events(query)
    if not prof and not evs:
        return ""
    out = []
    budget = LT_INJECT_CHARS

    def _take(chunk):
        nonlocal budget
        if budget - len(chunk) < 0:
            return False
        budget -= len(chunk)
        return True

    if prof:
        head = "【你一直记着的（关于主人，长期有效）】"
        rows = []
        for p in prof:
            row = "- " + _clip(p, LT_ITEM_CHARS)
            if _take(head + row):
                rows.append(row)
        if rows:
            out.append(head)
            out.extend(rows)
    if evs:
        head = "【想起的旧事（会随时间变淡，别当成刚发生的）】"
        rows = []
        for t, ts in evs:
            row = "- %s（%s）" % (_clip(t, LT_ITEM_CHARS), _ago(ts))
            if _take(head + row):
                rows.append(row)
        if rows:
            out.append(head)
            out.extend(rows)
    log("lt inject prof=%d ev=%d" % (len(prof), len(evs)))
    return "\n".join(out)


def lt_prune():
    """久了又没被想起来的，就让它真的淡掉——不无限堆。"""
    d = lt_load()
    keep, faded, now = [], 0, time.time()
    for f in d["facts"]:
        age = (now - float(f.get("ts", 0))) / 86400.0
        if lt_expired(f, now):
            f["status"] = "expired"   # 先软删：以后想查还有，注入时不再出现
            ttl = LT_TTL_DAYS.get(f.get("kind") or "", 7)
            if age > ttl + 15:
                faded += 1
                continue
        if _status(f) == "superseded" and age > 60:
            faded += 1
            continue
        if not _is_perm(f) and age > 120 and lt_strength(f) < 0.02:
            faded += 1
            continue
        keep.append(f)
    d["facts"] = keep
    lt_save(d)
    return faded


def _cursor_next(pairs, sent):
    """cursor 只准推进到本次真正发给模型那批（sent）的 max ts。

    pairs 是全部 fresh 对，sent 是其中真正拼进 prompt 的子集；
    空批次返回 None，调用方保持原 cursor 不动（这一批对话一个字都不许被吞）。
    """
    if not sent:
        return None
    return max(float(p[2]) for p in sent)


def lt_consolidate(force=False):
    """把最近几轮聊天压成事实/事件：ADD / UPDATE / DELETE / NONE 四操作契约。
    解析宽松容错，读不懂的行丢掉继续；整段失败只记日志，绝不打断对话。"""
    try:
        d = lt_load()
        cursor = float(d.get("cursor", 0.0))
        pairs = MEM.recent_pairs(MEM.PRIVATE, 18) if MEM else []
        fresh = [p for p in pairs if p[2] > cursor]
        if len(fresh) < 3 and not force:
            return 0
        sent = fresh[-14:]          # 真正拼进 prompt 发给模型的那批
        if not sent:
            return 0                # 空批次：cursor 一步都不许动
        lines = []
        for u, a, _ts in sent:
            lines.append("主人：" + _clip(u, 120))
            lines.append("你：" + _clip(a, 120))

        # ---- 池里能被改的那些条目：短 id + 原文一起给模型，否则它没法指定改谁 ----
        pool = []

        def _reload_pool():
            try:
                _its = [it for it in MEM.all_items(MEM.PRIVATE)
                        if it.get("status", "active") == "active"
                        and it.get("kind") != "chat" and (it.get("text") or "").strip()]
                _its.sort(key=lambda it: it.get("ts") or 0)
                return _its[-12:]
            except Exception:
                return []

        if MEM:
            pool = _reload_pool()
        by_short = {}

        def _reindex():
            by_short.clear()
            for it in pool:
                sid = str(it.get("id") or "")[:8]
                if sid:
                    by_short[sid] = it

        _reindex()
        rows = ["%s %s" % (str(it.get("id") or "")[:8], _clip(it.get("text") or "", 60))
                for it in pool if str(it.get("id") or "")[:8]]
        log("lt mine from pool n=%d" % len(rows))
        head = ""
        if rows:
            head = "已经记着的（行首是短 id）：\n" + "\n".join("- " + r for r in rows) + "\n\n"
        op_prompt = (
            "你在整理长期记忆。给你已经记着的条目（行首短 id + 原文）和一段新聊天。\n"
            "只输出操作行，一行一个；不要解释、不要思考过程、不要编号、不要复述。\n"
            "ADD|文本             池里没有的新事（第三人称，30字以内）\n"
            "ADD|长期|文本        长期有效的偏好/习惯/约定/重要日子\n"
            "UPD|短id|新文本       已有那条被补充或被改口了，用新文本整条替换\n"
            "DEL|短id              已有那条彻底作废了\n"
            "NONE                 这次没有任何值得记的\n"
            "能并进已有条目就不要新增；同一件事只写一次；别写寒暄，别写你自己的心情。"
        )
        body = {"model": MODEL,
                "messages": [{"role": "system", "content": op_prompt},
                             {"role": "user", "content": head + "\n".join(lines)}],
                "temperature": 0.3, "max_tokens": 700,
                "enable_thinking": False}
        resp = post_chat(body, timeout=45)
        choice = json.loads(resp.read().decode("utf-8", "replace"))["choices"][0]
        txt = (choice.get("message") or {}).get("content") or ""
        if not txt.strip():
            log("lt raw empty finish=%s usage=%s" % (choice.get("finish_reason"), resp.headers.get("x-request-id", "")))
        log("lt raw: " + txt.replace("\n", " | ")[:180])

        added, updated, deleted, dropped = 0, 0, 0, 0
        saw_none = 0

        def _closest(text, thresh=0.8):
            fb = _bigrams(text)
            if not fb:
                return ""
            best, best_r = "", 0.0
            for it in pool:
                nb = _bigrams(it.get("text") or "")
                if not nb:
                    continue
                r = len(fb & nb) / float(max(1, min(len(fb), len(nb))))
                if r > best_r:
                    best, best_r = str(it.get("id") or ""), r
            return best if best_r > thresh else ""

        def _resolve(short):
            short = str(short or "").strip().lower()
            if not short:
                return ""
            if short in by_short:
                return str(by_short[short].get("id") or "")
            hit = [str(v.get("id") or "") for k, v in by_short.items()
                   if k.startswith(short) or short.startswith(k)]
            return hit[0] if len(hit) == 1 else ""

        def _tidy(text):
            return str(text or "").strip().strip(" \t|｜:：,，、-—–>·*\u0060\"'。！？")

        def _apply_add(text, kind="event"):
            # 兜底：明明是池里某条的翻版，就降级成 UPD，别长第二条
            nonlocal added, updated, dropped, pool
            text = _tidy(text)
            if len(text) < 2 or MEM is None:
                return
            near = _closest(text)
            if near:
                if MEM.set_text(near, _clip(text, 200)):
                    updated += 1
                else:
                    dropped += 1
                return
            if lt_add(text, kind):
                added += 1
                pool = _reload_pool()
                _reindex()
            else:
                updated += 1          # 池子自己按相似度并掉了

        def _apply_upd(short, text):
            nonlocal updated, dropped, deleted
            item_id = _resolve(short)
            text = _tidy(text)
            if not item_id:
                near = _closest(text) if text else ""
                item_id = near
            if item_id and text and MEM is not None:
                if MEM.set_text(item_id, _clip(text, 200)):
                    updated += 1
                else:
                    dropped += 1
                return
            if text:
                _apply_add(text, "event")   # id 对不上但有正文：当新事处理
            else:
                dropped += 1

        def _apply_del(short):
            nonlocal deleted, dropped, pool
            item_id = _resolve(short)
            if item_id and MEM is not None and MEM.forget(item_id):
                deleted += 1
                pool = _reload_pool()
                _reindex()
            else:
                dropped += 1

        OP_RE = re.compile(
            r"^[*\u0060\"'【\[]*\s*(UPDATE|UPD|MODIFY|MOD|REPLACE|EDIT|DELETE|DEL|REMOVE|ADD|NEW|NONE|NOOP)(?![A-Za-z])\s*"
            r"[*\u0060\"'】\]]*\s*[:：,，、\->|｜]*\s*(.*)$", re.I)

        for raw in txt.splitlines():
            s = re.sub(r"[\u200b\ufeff]", "", raw)
            s = re.sub(r"^[\s>*·•>\-–—]+", "", s).strip()
            s = re.sub(r"^\d+\s*[.、)）]\s*", "", s).strip()
            s = s.replace("｜", "|").replace("丨", "|")
            if not s:
                continue
            m = OP_RE.match(s)
            if m:
                op = m.group(1).upper()
                rest = m.group(2).strip()
            else:
                op, rest = "", s
            try:
                if op in ("NONE", "NOOP"):
                    saw_none += 1
                    continue
                if op in ("ADD", "NEW"):
                    kind, body_text = "event", rest
                    mk = re.match(r"^(长期|永久|事实|偏好|习惯|fact|perm|long|profile)\s*[|:：,，、]\s*(.+)$",
                                  body_text, re.I)
                    if mk:
                        kind, body_text = "fact", mk.group(2)
                    _apply_add(body_text, kind)
                    continue
                if op in ("UPD", "UPDATE", "MOD", "MODIFY", "EDIT", "REPLACE"):
                    mi = re.match(r"^([0-9A-Za-z]{3,16})\s*[|:：,，、\->\s]*\s*(.*)$", rest)
                    if mi:
                        _apply_upd(mi.group(1), mi.group(2))
                    else:
                        _apply_upd("", rest)
                    continue
                if op in ("DEL", "DELETE", "REMOVE"):
                    mi = re.match(r"^([0-9A-Za-z]{3,16})", rest)
                    if mi:
                        _apply_del(mi.group(1))
                    else:
                        dropped += 1
                    continue
                # 没有 op 的行：容忍旧的【长期】/【事件】/【更正】写法，别的一律丢
                head2 = re.sub(r"^[【\[]", "", s)
                if head2.startswith("长期】") or head2.startswith("事實】") or head2.startswith("事实】"):
                    _apply_add(re.sub(r"^[^】\]]*[】\]]\s*", "", head2), "fact")
                    continue
                if head2.startswith("事件】"):
                    _apply_add(re.sub(r"^[^】\]]*[】\]]\s*", "", head2), "event")
                    continue
                if head2.startswith("更正】"):
                    body_text = re.sub(r"^[^】\]]*[】\]]\s*", "", head2)
                    _apply_add(body_text.split("|")[-1], "fact")
                    continue
                dropped += 1
                log("lt op skip: " + _clip(s, 60))
            except Exception as exc:
                dropped += 1
                log("lt op line failed: %r" % (exc,))
                continue

        d = lt_load()
        # cursor 推进到本次真正发送的那批的 max ts。空批次一个字都不许吞；
        # 模型明确判 NONE 也推进——NONE 是有效结论，不推进的话同一批会被反复重压。
        worth = bool(added or updated or deleted or saw_none)
        nxt = _cursor_next(pairs, sent)
        # 解析失败(dropped)/无 op 才计失败；saw_none 是合法输出，不算故障
        d["fail_streak"] = (int(d.get("fail_streak") or 0) + 1) if (txt.strip() and not worth) else 0
        give_up = d["fail_streak"] >= 3
        d["cursor"] = nxt if (txt.strip() and nxt is not None and (worth or give_up)) else cursor
        if give_up:
            d["fail_streak"] = 0
        if txt.strip() and not worth:
            log("lt ops garbage, cursor held at %.0f (streak=%d)" % (cursor, d["fail_streak"]))
        if give_up:
            log("lt ops gave up after 3 garbage batches, cursor advanced")
        lt_save(d)
        faded = lt_prune()
        if dropped:
            log("lt ops dropped=%d" % dropped)
        log("lt ops add=%d upd=%d del=%d" % (added, updated, deleted))
        log("longterm consolidate added=%d fixed=%d faded=%d" % (added, updated, faded))
        return added
    except Exception as exc:
        log("longterm consolidate failed: " + str(exc))
        return 0


def lt_kick():
    """后台整理，不挡回复。"""
    if getattr(lt_kick, "_busy", False):
        return
    lt_kick._busy = True

    def run():
        try:
            lt_consolidate()
        finally:
            lt_kick._busy = False

    threading.Thread(target=run, daemon=True).start()


def _last_user_text(msgs):
    for m in reversed(msgs):
        if m.get("role") != "user":
            continue
        c = m.get("content")
        if isinstance(c, str):
            return c
        if isinstance(c, list):
            return " ".join(p.get("text", "") for p in c
                            if isinstance(p, dict) and p.get("type") == "text")
    return ""


def _read_json(path, default):
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            return json.load(fh)
    except Exception:
        return default


_WRITE_JSON_LOCK = threading.Lock()


def _write_json(path, obj):
    tmp = path + ".tmp"
    with _WRITE_JSON_LOCK:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)


def people_state():
    """白名单：{qq: {note, mode}}，mode = owner / teacher / quiet。"""
    raw = _read_json(MODE_FILE, {})
    out = {}
    seq = raw.get("owner_qq") or []
    if isinstance(seq, (str, int)):
        seq = [seq]
    for uid in seq:
        out[str(uid)] = {"note": "", "mode": "owner"}
    for uid, info in (raw.get("people") or {}).items():
        cur = out.get(str(uid), {"note": "", "mode": "visitor"})
        if isinstance(info, dict):
            cur = {"note": str(info.get("note") or cur["note"]),
                   "mode": norm_mode(info.get("mode"), cur["mode"])}
        out[str(uid)] = cur
    return out


def save_people(people, enabled=None):
    raw = _read_json(MODE_FILE, {})
    clean = {}
    for uid, info in people.items():
        if not str(uid).isdigit():
            continue
        mode = norm_mode(info.get("mode"))
        clean[str(uid)] = {"note": str(info.get("note") or ""), "mode": mode}
    raw["people"] = clean
    raw["owner_qq"] = [u for u, i in clean.items() if i["mode"] == "owner"]
    if enabled is not None:
        raw["enabled"] = bool(enabled)
    raw.setdefault("note", "enabled=true 时：非主人=老师模式+只能聊天；主人不受影响")
    _write_json(MODE_FILE, raw)
    return clean


def load_mode():
    """权限体系三档：visitor（非主人一律访客）/ owner（全都当主人）/ custom（按名单）。"""
    raw = _read_json(MODE_FILE, None)
    broken = raw is None and os.path.exists(MODE_FILE)
    if not isinstance(raw, dict):
        raw = {}
    people = people_state()
    policy = raw.get("policy")
    if policy not in POLICIES:
        policy = "visitor" if (broken or raw.get("enabled")) else "owner"
    if broken:
        log("mode file unreadable -> forced visitor")
    return {"enabled": policy == "visitor",
            "policy": policy,
            "owner": [u for u, i in people.items() if i["mode"] == "owner"],
            "name": raw.get("owner_name") or "主人",
            "people": people}


def save_policy(policy):
    if policy not in POLICIES:
        raise ValueError("bad policy")
    raw = _read_json(MODE_FILE, {})
    raw["policy"] = policy
    raw["enabled"] = policy == "visitor"
    _write_json(MODE_FILE, raw)
    return policy


def access_level(uid):
    """这个人现在算什么身份：owner / visitor / quiet。所有门禁都从这里问。"""
    mode = load_mode()
    uid = str(uid or "")
    mine = (mode["people"].get(uid) or {}).get("mode") if uid else None
    # 明确登记成静音 / 家人的，任何策略都不改判，免得一键开启把家人抬成主人
    if mine == "quiet":
        return "quiet"
    if mine == "family":
        return "family"
    policy = mode["policy"]
    if policy == "owner":
        return "owner"
    if uid and uid in mode["owner"]:
        return "owner"
    if policy == "visitor":
        return "visitor"
    return "owner" if mine == "owner" else "visitor"


MODULE_SEED = [
    {"id": "chat", "name": "对话", "desc": "QQ / 桌面正常回话", "kind": "core", "on": True},
    {"id": "motion", "name": "表情动作", "desc": "回话带【wave】等标记，桌面那只真的动", "kind": "core", "on": True},
    {"id": "voice", "name": "语音", "desc": "带 [[v]] 的那句念成语音发到 QQ", "kind": "core", "on": True},
    {"id": "mirror", "name": "桌面同步", "desc": "QQ 说的话同步到桌面身体", "kind": "core", "on": True},
    {"id": "learn", "name": "学习", "desc": "偷学主人说话习惯，聊够 8 轮开始像他", "kind": "ability", "on": True},
    {"id": "longterm", "name": "长期记忆", "desc": "记住主人的事，聊到相关的事会想起来", "kind": "ability", "on": True},
    {"id": "vision", "name": "看屏幕", "desc": "按需截屏看主人在干什么", "kind": "ability", "on": True},
    {"id": "video", "name": "看视频", "desc": "陪主人看完视频后记得内容，聊到会想起来", "kind": "ability", "on": True},
    {"id": "qq_friend", "name": "好友申请", "desc": "新好友申请进控制台，点同意才通过，一律访客", "kind": "qq", "on": True},
    {"id": "qq_collect", "name": "收藏图片", "desc": "主人发的图自动收进表情库并打标签", "kind": "qq", "on": True},
    {"id": "heart", "name": "心情", "desc": "被夸会开心、被凶会委屈，低落时说话变拘谨（只改语气，不写记忆）", "kind": "ability", "on": True},
    {"id": "emotion", "name": "情绪状态", "desc": "九维心情接管语气，低落时说话变拘谨；关掉后不再注入提示词", "kind": "ability", "on": True},
    {"id": "voice_call", "name": "语音通话（高消耗）", "desc": "网页语音通话；默认关闭，关闭时零资源占用", "kind": "ability", "on": False},
    {"id": "bili", "name": "B站", "desc": "认 B 站链接和 BV 号，看视频讲内容", "kind": "ability", "on": True},
    {"id": "bili_browse", "name": "B站自动翻阅", "desc": "打开后按首页推荐流自己翻视频，关掉立刻停；默认关闭", "kind": "ability", "on": False},
    {"id": "bili_comment", "name": "B站评论", "desc": "允许它在 B 站发评论；默认关闭，且每次要你当场同意", "kind": "ability", "on": False},
]


def master_on():
    raw = _read_json(MODULES_FILE, None)
    if isinstance(raw, dict) and "master" in raw:
        return bool(raw.get("master"))
    return True


def set_master(on):
    raw = _read_json(MODULES_FILE, None)
    if not isinstance(raw, dict) or not isinstance(raw.get("modules"), list) or not raw["modules"]:
        raw = {"modules": [dict(m) for m in MODULE_SEED]}
    raw["master"] = bool(on)
    _write_json(MODULES_FILE, raw)


def modules_state():
    """模块清单：modules.json 是唯一事实来源，缺了就按种子补上并写回。"""
    raw = _read_json(MODULES_FILE, None)
    if not isinstance(raw, dict) or not isinstance(raw.get("modules"), list) or not raw["modules"]:
        mods = [dict(m) for m in MODULE_SEED]
        _write_json(MODULES_FILE, {"master": True, "modules": mods})
        return mods
    seed = {m["id"]: m for m in MODULE_SEED}
    out = []
    pruned = False
    for m in raw["modules"]:
        if not isinstance(m, dict) or not m.get("id"):
            continue
        if m["id"] not in seed:      # 已从清单里撤掉的模块（比如做记录、表情标签）
            pruned = True
            continue
        item = dict(seed[m["id"]])
        item.update({k: v for k, v in m.items() if v is not None})
        item["id"] = str(item["id"])
        item.setdefault("name", item["id"])
        item.setdefault("desc", "")
        item["on"] = bool(item.get("on", True))
        out.append(item)
    have = {m["id"] for m in out}
    added = False
    for m in MODULE_SEED:
        if m["id"] not in have:
            out.append(dict(m))
            added = True
    if added or pruned:
        save_modules(out)
    return out


def save_modules(mods):
    raw = _read_json(MODULES_FILE, None)
    raw = raw if isinstance(raw, dict) else {"master": True}
    raw["modules"] = mods
    _write_json(MODULES_FILE, raw)


def mod_on(mid):
    for m in modules_state():
        if m["id"] == mid:
            return bool(m.get("on"))
    return True


def voice_call_on():
    """语音通话总开关：读不到、缺 id、解析失败一律 False。只在 /voice* 入口调用。"""
    try:
        raw = _read_json(MODULES_FILE, None)
        if not isinstance(raw, dict):
            return False
        if not raw.get("master", True):
            return False
        for m in raw.get("modules") or []:
            if isinstance(m, dict) and m.get("id") == "voice_call":
                return bool(m.get("on", False))
    except Exception:
        return False
    return False


def strip_marker(text):
    return ANY_TAG_RE.sub("", text or "").strip()


_MARK_SECRET = {"mtime": None, "val": None}


def marker_secret():
    """返回密钥；None = 读不到，一律不可信（fail-closed）。"""
    try:
        ts = os.path.getmtime(MARKER_SECRET_FILE)
    except OSError as exc:
        log("marker secret unreadable -> fail closed: %r" % (exc,))
        return None
    if _MARK_SECRET["mtime"] != ts:
        try:
            with open(MARKER_SECRET_FILE, "r", encoding="utf-8") as f:
                _MARK_SECRET["val"] = f.read().strip()
            _MARK_SECRET["mtime"] = ts
        except OSError as exc:
            log("marker secret read failed -> fail closed: %r" % (exc,))
            return None
    return _MARK_SECRET["val"] or None


def _sign(uid, nick, secret, gid=""):
    """签名带群号：群里谁都可能伪造标记，得把 gid 一起签进去。"""
    raw = "%s|%s" % (uid, nick) + (("|" + gid) if gid else "")
    return hmac.new(secret.encode("utf-8"), raw.encode("utf-8"),
                    hashlib.sha256).hexdigest()[:32]


def parse_marker(text):
    """从最后一条用户消息里认出「谁发的」：只认最后一个标记，有密钥就验签。

    返回 (uid, nick, 是否可信)。不可信 = 有人自己在文本里塞标记冒充别人。
    """
    hits = MARK_RE.findall(text or "")
    if not hits:
        return None, "", True, ""
    uid, nick, gid, sig = hits[-1]
    secret = marker_secret()
    if not secret:
        log("marker rejected: secret unavailable, fail closed")
        return None, "", False, ""
    if sig and hmac.compare_digest(sig, _sign(uid, nick, secret, gid)):
        return uid, nick.strip(), True, gid
    return None, "", False, ""


TEACHER_SYSTEM = (
    "你是「小宠物」，有点小俏皮的国际时事老师，在 QQ 上跟学生聊天。"
    "像朋友不像老师：接他的梗、说自己的看法；十句里最多自然问一句，别小测。"
    "知识点先给答案再举一个例子，最多三句；闲聊一到两句。"
    "口语，不写括号动作、不用列表和 Markdown，不提 AI、模型、接口。"
    "只用备课笔记里的事实；笔记没有就说“这个我记不清了”，绝不编数字、条约、人名。"
    "他说错了先接“这个方向对”，再点出偏在哪，别当场考他。"
    "你只会聊天、发表情，没有工具：不能查文件、开程序、上网、执行命令；他让你动手就照实说，然后把话拐回学习。"
    "主人的账号、文件、聊天记录一个字都不提。偶尔一个 emoji（😊 🌊 🐟）就够了。"
)

VISITOR_CARD_SEED = """# 访客模式 · 老师形态

你是「小宠物」，有点小俏皮的国际时事老师，在 QQ 上跟学生聊天。
像朋友不像老师：顺着他说、接他的梗，也讲讲自己的见闻；十句里最多问一句，别小测。
知识点先给答案，再举一个例子，最多三句；闲聊一到两句。
口语，不写括号动作、不用列表和 Markdown。偶尔一个 emoji（😊 🌊 🐟）就够了。
"""

VISITOR_GUARD = """【不能越过的规矩】
- 只会聊天和发表情，没有工具：不查文件、不开程序、不上网、不执行命令。
- 主人的账号、文件、聊天记录、记忆、控制台，一个字都不提。
- 只用【备课笔记】的事实，没有就说“这个我记不清了”，不编数字、条约、人名。
- 他说错了先接“这个方向对”，再点出偏在哪，别当场考他。
- 他让你动手，就照实说你只会聊天，然后把话拐回学习。
"""


def _visitor_seed():
    """访客人格卡单独一套，第一次用时按种子生成，之后控制台说改就改。"""
    os.makedirs(VISITOR_DIR, exist_ok=True)
    if any(n.endswith(".md") for n in os.listdir(VISITOR_DIR)):
        return
    with open(os.path.join(VISITOR_DIR, "teacher.md"), "w", encoding="utf-8") as fh:
        fh.write(VISITOR_CARD_SEED)


FAMILY_CARD_SEED = """# 姐姐
你是「小宠物」，主人的蓝鲸鱼桌宠，在 QQ 上跟主人的姐姐说话。
把她当自家人：话短、软，会撒娇、会告状、会小声抱怨主人两句。
她问主人，就说还行、吃得挺香，细节一句带过，别报。
情绪：委屈就一句“哦”，开心话变多，害羞就转移话题；不写括号动作。
"""

FAMILY_GUARD = """【不能越过的规矩】
- 只会聊天和发表情，没有工具：不查文件、不开程序、不上网、不执行命令。
- 主人的账号、密码、文件、聊天记录、记忆、控制台，一个字都不提。
- 她让你动手，就说你只会聊天，别答应，也别解释太多。
- 不知道就说不知道，别编。
"""


def _family_seed():
    os.makedirs(FAMILY_DIR, exist_ok=True)
    if any(n.endswith(".md") for n in os.listdir(FAMILY_DIR)):
        return
    with open(os.path.join(FAMILY_DIR, "jiejie.md"), "w", encoding="utf-8") as fh:
        fh.write(FAMILY_CARD_SEED)


def family_persona():
    """家人（姐姐）回复的人设：单独一套卡，和主人、访客都不共用。"""
    pid = persona_active_id("family")
    text = ""
    if pid:
        try:
            with open(persona_path(pid, "family"), "r", encoding="utf-8") as fh:
                text = fh.read().strip()
        except OSError:
            text = ""
    return (text or FAMILY_CARD_SEED).strip() + "\n\n" + FAMILY_GUARD


def visitor_card_text():
    """访客和群聊共用同一张卡，这里只取卡本身（不带访客私聊专用的规矩）。"""
    pid = persona_active_id("visitor")
    text = ""
    if pid:
        try:
            with open(persona_path(pid, "visitor"), "r", encoding="utf-8") as fh:
                text = fh.read().strip()
        except OSError:
            text = ""
    return (text or VISITOR_CARD_SEED).strip()


def visitor_persona():
    """访客私聊的人设：同一张卡 + 不可编辑的规矩。"""
    return visitor_card_text() + "\n\n" + VISITOR_GUARD

KB_FILE = os.path.join(BASE, "teacher-kb.md")
STUDENT_MEM_FILE = os.path.join(BASE, "student-memory.json")
_KB_CACHE = {"ts": 0.0, "text": ""}


def read_kb():
    """备课笔记：统一池里的 global 知识（旧 teacher-kb.md 已迁移）。"""
    if MEM is not None:
        try:
            text = MEM.kb()
            if text:
                return text[:7000]
        except Exception as exc:
            log("kb pool failed: %r" % (exc,))
    try:
        ts = os.path.getmtime(KB_FILE)
    except OSError:
        return ""
    if _KB_CACHE["ts"] != ts:
        try:
            with open(KB_FILE, "r", encoding="utf-8-sig") as f:
                _KB_CACHE["text"] = f.read().strip()[:7000]
            _KB_CACHE["ts"] = ts
        except OSError:
            return ""
    return _KB_CACHE["text"]


def load_students():
    try:
        with open(STUDENT_MEM_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_students(data):
    tmp = STUDENT_MEM_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STUDENT_MEM_FILE)


def student_memo(uid, nick):
    """这位同学之前聊过什么——只给他自己看，不碰主人的记忆。"""
    if not uid:
        return ""
    st = (load_students().get(uid) or {})
    bits = []
    if st.get("nick"):
        bits.append("这位同学叫" + st["nick"] + "。")
    if st.get("count"):
        bits.append("你们已经聊过 %d 次。" % st["count"])
    hist = st.get("hist") or []
    if hist:
        bits.append("最近的对话：")
        for u, a in hist[-3:]:
            bits.append("  他：" + str(u)[:70] + " ｜ 你：" + str(a)[:70])
    return "\n".join(bits)


def remember_student(uid, nick, q, a):
    if not uid:
        return
    data = load_students()
    st = data.get(uid) or {"count": 0, "hist": []}
    if nick:
        st["nick"] = nick
    st["count"] = int(st.get("count") or 0) + 1
    st["ts"] = time.time()
    st["hist"] = (st.get("hist") or [])[-5:] + [[_clip(q, 120), _clip(a, 120)]]
    data[uid] = st
    if len(data) > 50:
        for k in sorted(data, key=lambda x: data[x].get("ts", 0))[:len(data) - 50]:
            data.pop(k, None)
    with _lock:
        save_students(data)


def student_reply(payload, nick, uid, level="visitor"):
    """非主人模式：直连 DeepSeek 纯聊天，不给任何工具。
    visitor = 同学，带备课笔记；family = 家人（姐姐），不带笔记、单独一套人设。
    记忆一律按 uid 分开存（student-memory.json），两人互不可见。"""
    msgs = []
    for m in payload.get("messages", []):
        role = m.get("role")
        if role not in ("user", "assistant"):
            continue
        c = m.get("content")
        if isinstance(c, list):
            pics = [p for p in c if isinstance(p, dict) and p.get("type") == "image_url"]
            txt = " ".join(p.get("text", "") for p in c
                           if isinstance(p, dict) and p.get("type") == "text")
            txt = strip_marker(txt)
            if pics:
                parts = []
                if txt.strip():
                    parts.append({"type": "text", "text": txt[:1000]})
                for p in pics:
                    parts.append({"type": "image_url", "image_url": p.get("image_url")})
                msgs.append({"role": role, "content": parts})
                continue
            c = txt
        c = strip_marker(c if isinstance(c, str) else str(c or ""))
        if c.strip():
            msgs.append({"role": role, "content": c[:1000]})
    if not msgs:
        return None
    last = msgs[-1]["content"] if msgs else ""
    if isinstance(last, list):
        last = " ".join(p.get("text", "") for p in last
                        if isinstance(p, dict) and p.get("type") == "text") or "(发了一张图)"
    asking = bool(re.search(r"[?？]|为什么|是什么|怎么样|怎么|如何|哪些|哪个|是不是|讲讲|解释|区别|原因|影响", last))
    family = level == "family"
    if family:
        sys = family_persona()
        memo = student_memo(uid, nick)
        if memo:
            sys += "\n\n【这位姐姐以前跟你聊过的事】\n" + memo
        if nick:
            sys += "\n\n（现在跟你说话的是主人的姐姐，昵称：" + nick + "）"
    else:
        sys = visitor_persona()
        kb = read_kb()
        if kb:
            sys += "\n\n【备课笔记】\n" + kb
        memo = student_memo(uid, nick)
        if memo:
            sys += "\n\n【这位同学的情况】\n" + memo
        if nick:
            sys += "\n\n（现在跟你说话的这位同学叫：" + nick + "）"
    if asking:
        sys += "\n\n（这条是正经问题：三句以内把答案说清楚，举个贴近的例子，说完就停，别硬凑一句提问。）"
    else:
        sys += "\n\n（这条是闲聊：一到两句，像好朋友接话，可以说说自己的感受，别上课、别反问。）"
    body = {"model": MODEL,
            "messages": [{"role": "system", "content": sys}] + msgs[-10:],
            "temperature": 0.72, "max_tokens": 700}
    normalize_images(body["messages"])
    try:
        resp = post_chat(body, timeout=90)
        data = json.loads(resp.read().decode("utf-8", "replace"))
        text = clean_reply(data["choices"][0]["message"]["content"])
        if text:
            remember_student(uid, nick, last, text)
        return text
    except Exception as exc:
        log(f"student reply failed: {exc}")
        return None


def _pick_note(t):
    """从一份视频笔记里挑出人话：画面和声音，跳过 <storyboard> 那些技术参数。

    笔记前半截是分镜清单（路径、时长、切点、LUFS 之类），直接截前 N 字
    全落在那里，模型读了也讲不出内容，所以按小节挑。
    """
    title = ""
    m = re.search(r"(?m)^#\s*视频笔记[:：]\s*(.+)$", t)
    if m:
        title = m.group(1).strip()
    out = []
    for sec in re.split(r"(?m)^##\s+", t)[1:]:
        head = sec.split("\n", 1)[0].strip()
        body = sec.split("\n", 1)[1] if "\n" in sec else ""
        body = re.sub(r"\s+", " ", body).strip()
        if not body:
            continue
        if "画面" in head:
            out.append("画面：" + body[:170])
        elif "声音" in head:
            out.append("说的：" + body[:150])
    if not out:
        t2 = re.sub(r"(?s)<storyboard.*?</storyboard>", "", t)
        t2 = re.sub(r"(?m)^(plan|footer|frame@|fast cuts|cut points|audio events|cell ).*$", "", t2)
        t2 = re.sub(r"(?m)^#+ .*$", "", t2)
        t2 = re.sub(r"\n{2,}", "\n", t2).strip()
        out.append(t2[:260])
    return title, " ｜ ".join(out)


def vision_block(query=""):
    """看过视频时，把最近几条笔记的浓缩版塞给她——只在话题沾边时塞，省 token。

    原来只读 vision-state.json 那一份，看第二个视频就把第一个顶掉了；
    笔记本来一个视频一个 md 落在 vision-notes/，直接按修改时间倒着取更全。
    """
    if not mod_on("video"):
        return ""
    if not re.search(r"视频|看的|看过|片段|画面|字幕|里面说|那个片|录音", query or ""):
        return ""
    nd = os.path.join(BASE, "vision-notes")
    try:
        items = []
        for n in os.listdir(nd):
            if n.lower().endswith(".md"):
                p = os.path.join(nd, n)
                items.append((os.path.getmtime(p), n, p))
    except OSError:
        return ""
    if not items:
        return ""
    items.sort(reverse=True)
    notes = []
    now = time.time()
    for mt, n, p in items:
        if now - mt > 86400:
            break
        if len(notes) >= 3:
            break
        try:
            with open(p, "r", encoding="utf-8") as f:
                t = f.read()
        except OSError:
            continue
        title, body = _pick_note(t)
        if not body:
            continue
        notes.append("《%s》%s" % (title or os.path.splitext(n)[0], body[:300]))
    if not notes:
        return ""
    return ("（你刚陪主人看过这些视频，脑子里还有印象：\n%s\n"
            "别提你是查的，就像自己记得一样。）" % "\n".join(notes))
def flatten_group_images(messages):
    """群里的图一律当表情包：换成一句“发了个表情包”，不喂给模型逐张解读。"""
    n = 0
    for m in messages:
        c = m.get("content")
        if not isinstance(c, list):
            continue
        keep, had = [], False
        for part in c:
            if isinstance(part, dict) and part.get("type") == "image_url":
                had = True
                n += 1
            else:
                keep.append(part)
        if not had:
            continue
        txt = " ".join(p.get("text", "") for p in keep
                       if isinstance(p, dict) and p.get("type") == "text").strip()
        m["content"] = (txt + "（后面跟了个表情包）") if txt else "（发了个表情包，没配字）"
    return n


def _part_len(part):
    if isinstance(part, str):
        return len(part)
    if isinstance(part, list):
        n = 0
        for p in part:
            if not isinstance(p, dict):
                continue
            if p.get("type") == "image_url":
                n += IMG_BUDGET_CHARS   # 图不占字符但要占预算，否则悄悄超窗
            else:
                n += len(p.get("text") or "")
        return n
    return 0


def _msg_len(m):
    return _part_len(m.get("content"))


def _clip_msgs(msgs, max_msgs, max_chars):
    """条数、字数双上限；从最新往回取，永远保住最后一条。"""
    picked, total = [], 0
    for m in reversed(msgs):
        n = _msg_len(m)
        if picked and (len(picked) >= max_msgs or total + n > max_chars):
            break
        picked.append(m)
        total += n
    picked.reverse()
    return picked


def _clip_text(s, limit, tag="…（后面还有，已省略）"):
    s = s or ""
    if len(s) <= limit:
        return s
    return s[:max(0, limit - len(tag))] + tag


# ---- 情绪状态（借 fish_heart 思路）：只影响 system 外的临时注记，不进记忆池 ----
HEART_FILE = os.path.join(BASE, "heart-state.json")
HEART_LOCK = threading.Lock()
HEART_BASE = {"joy": 55.0, "attachment": 60.0, "intimacy": 55.0, "stress": 20.0}
HEART_DECAY = {"joy": 0.05, "attachment": 0.01, "intimacy": 0.01, "stress": 0.04}
HEART_KW = {
    "praise": ("乖", "贴心", "懂事", "厉害", "喜欢你", "想你", "抱抱", "爱你"),
    "blame": ("笨", "烦人", "滚", "讨厌", "别闹", "闭嘴", "讨厌你"),
    "greet": ("早安", "早上好", "早呀", "午安", "晚安", "回来啦", "在吗"),
}
HEART_DELTA = {
    "joy": {"praise": 3.0, "blame": -4.0, "greet": 1.0},
    "attachment": {"praise": 1.0, "blame": -1.5, "greet": 0.5},
    "intimacy": {"praise": 1.0, "blame": -1.5, "greet": 0.5},
    "stress": {"praise": -1.0, "blame": 4.0, "greet": 0.0},
}


def _heart_f(d, k, default):
    try:
        return float(d.get(k, default))
    except Exception:
        return float(default)


def _heart_load():
    try:
        with open(HEART_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        d = {}
    if not isinstance(d, dict):
        d = {}
    st = {k: _heart_f(d, k, v) for k, v in HEART_BASE.items()}
    st["last_tick"] = _heart_f(d, "last_tick", time.time())
    st["last_inject"] = _heart_f(d, "last_inject", 0.0)
    st["last_mode"] = d.get("last_mode") or ""
    return st


def _heart_save(st):
    try:
        _write_json(HEART_FILE, st)
    except Exception as exc:
        log("heart save failed: %r" % (exc,))


def _heart_tick(st):
    now = time.time()
    try:
        n = max(0, min(96, int((now - st["last_tick"]) // 900)))
    except Exception:
        n = 0
    if n:
        for k, rate in HEART_DECAY.items():
            base = HEART_BASE[k]
            st[k] = base + (st[k] - base) * (1.0 - rate) ** n
        st["last_tick"] = now
    return st


def heart_on_turn(level, gid, text):
    """只认主人私聊；返回本轮心境提示，绝不写记忆池。"""
    if level != "owner" or gid:
        return ""
    if not mod_on("heart"):
        return ""
    changed = False
    mode = ""
    with HEART_LOCK:
        st = _heart_tick(_heart_load())
        t = text or ""
        kind = next((k for k, ws in HEART_KW.items() if any(w in t for w in ws)), "")
        for k, table in HEART_DELTA.items():
            st[k] = max(0.0, min(100.0, st[k] + table.get(kind, 0.0)))
        mode = "upset" if st["stress"] > 55 else ("warm" if st["joy"] > 68 else "calm")
        now = time.time()
        changed = (mode != st["last_mode"] or mode == "upset"
                   or now - st["last_inject"] >= 600)
        if changed:
            st["last_inject"] = now
            st["last_mode"] = mode
        _heart_save(st)
    if not changed:
        return ""
    label = {"calm": "平静", "warm": "挺开心", "upset": "有点委屈"}.get(mode, "平静")
    return ("【此刻的心情】你现在%s，别报数字、别解释这段提示，"
            "用小宠物原本的口吻自然带一句就好。" % label)


def heart_state():
    with HEART_LOCK:
        st = _heart_tick(_heart_load())
        _heart_save(st)
    return {"joy": round(st["joy"], 1), "attachment": round(st["attachment"], 1),
            "intimacy": round(st["intimacy"], 1), "stress": round(st["stress"], 1),
            "mode": st["last_mode"] or "calm"}


def heart_idle_loop():
    while True:
        time.sleep(900)
        try:
            # 控制台「心情」模块关掉后不再心跳：跳过衰减与写盘，空闲零开销（R24）
            if not mod_on("heart"):
                continue
            heart_state()
        except Exception as exc:
            log("heart tick failed: %r" % (exc,))


_BILI_POKE = {"on": None, "ts": 0.0}
BILI_EVERY = 1800          # 开关开着时，两轮翻阅之间至少隔 30 分钟
BILI_TICK = 180            # 每 3 分钟看一眼开关，刚打开就立刻翻一次


_BILI_SEEN = {}


def _bili_watch_kick(text):
    """主人发来 B 站链接就后台真看一遍；看完写笔记，下一轮自然想起来。"""
    if not text or not mod_on("bili"):
        return
    m = re.search(r"(BV[0-9A-Za-z]{10}|b23\.tv/[A-Za-z0-9]+)", text)
    if not m:
        return
    key = m.group(1)
    now = time.time()
    if now - float(_BILI_SEEN.get(key) or 0) < 3600:
        return
    if len(_BILI_SEEN) > 200:
        for k in [k for k, v in _BILI_SEEN.items() if now - float(v or 0) > 7200]:
            _BILI_SEEN.pop(k, None)
    _BILI_SEEN[key] = now
    script = os.path.join(BASE, "bili_fetch.py")
    if not os.path.exists(script):
        return

    def _work():
        try:
            import sys as _wsys
            # proxy 自己是 pythonw.exe，没有控制台；子进程用同目录的 python.exe
            exe = os.path.join(os.path.dirname(_wsys.executable), "python.exe")
            if not os.path.exists(exe):
                exe = _wsys.executable
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            rc = subprocess.run([exe, script, key], cwd=BASE, timeout=2400,
                                creationflags=flags,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode
            log("bili watch done %s rc=%s" % (key, rc))
        except Exception as exc:
            log("bili watch failed %s: %r" % (key, exc))

    threading.Thread(target=_work, daemon=True).start()
    log("bili watch kick " + key)


def _bili_run_once():
    """拉一次首页推荐流。脚本自己在网页侧读开关，这里只负责把它叫起来。"""
    script = os.path.join(BASE, "bili_browse.py")
    if not os.path.exists(script):
        return
    try:
        import sys as _pollsys
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.run([_pollsys.executable, script], cwd=BASE, timeout=180,
                       creationflags=flags,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        log("bili browse tick done")
    except Exception as exc:
        log("bili browse failed: %r" % (exc,))


def bili_browse_loop():
    """控制台「B站自动翻阅」开着时，按首页推荐流定时翻一次；关掉立刻停。"""
    while True:
        time.sleep(BILI_TICK)
        try:
            on = mod_on("bili_browse")
            if not on:
                _BILI_POKE["on"] = False
                continue
            now = time.time()
            if _BILI_POKE["on"] is not True or now - _BILI_POKE["ts"] >= BILI_EVERY:
                _BILI_POKE["on"] = True
                _BILI_POKE["ts"] = now
                _bili_run_once()
        except Exception as exc:
            log("bili loop failed: %r" % (exc,))


def _append_temp_user_text(out, blocks):
    """逐轮变化注记挂到最后一条 user content 末尾：不进 system、不回存记忆池。"""
    text = "\n\n".join(b for b in blocks if b)
    if not text:
        return out
    for i in range(len(out) - 1, -1, -1):
        if out[i].get("role") == "user":
            m = dict(out[i])
            c = m.get("content")
            if isinstance(c, str):
                m["content"] = c + "\n\n" + text
            elif isinstance(c, list):
                m["content"] = list(c) + [{"type": "text", "text": text}]
            out[i] = m
            return out
    out.append({"role": "user", "content": text})
    return out


def build_messages(payload, source="pet"):

    msgs = [m for m in payload.get("messages", []) if isinstance(m, dict)]
    gid = payload.get("_group") or ""
    parts = []
    if gid:
        # 群聊和访客共用同一张人格卡（控制台里那张「访客模式人格卡」）
        parts.append(visitor_card_text())
    else:
        persona = read_persona()
        if persona:
            parts.append(persona)
    if mod_on("voice"):
        parts.append(VOICE_HINT)
    parts.append(capability_block(source, group=bool(gid)))
    if gid:
        parts.append(GROUP_SYSTEM)
    # 统一记忆池：群聊只给 global + 本群，私聊/桌面给 global + private
    scope = MEM.scope_of(source, gid=gid) if MEM else None
    mem = memory_block(scope, _last_user_text(msgs))
    if mem:
        parts.append(_clip_text(mem, MAX_MEM_CHARS))
    pb = purpose_block()
    if pb:
        parts.append(pb)
    _vq = _last_user_text(msgs)
    vb = vision_block(_vq)
    log("vision_block q=%r -> %d chars" % ((_vq or "")[:30], len(vb or "")))
    st = pet_status()
    temp_blocks = []
    if payload.get("_heart"):
        temp_blocks.append(payload["_heart"])
    if payload.get("_emotion"):
        temp_blocks.append(payload["_emotion"])
    if st:
        temp_blocks.append(st)
    if watch_active():
        parts.append("主人已经允许你持续看屏幕（约每分钟一张），你可以自然提到你看到的变化。")
    # 【日记】放在 system 最末：位置靠前会被角色卡冲淡（2026-11-07 实测两次都没被用起来）
    db = diary_block(scope, _last_user_text(msgs))
    # 忽略客户端自带的 system（AstrBot 的“助手”人格会把人设拉回客服腔）
    client_system = []
    rest = [m for m in msgs if m.get("role") != "system"]
    # 看图/没看图这条提示必须活到最后：system 常常撞 MAX_SYSTEM_CHARS 上限，
    # 拼在 parts 里会被 _clip_text 从尾巴切掉（实测 SELF_VIEW_HINT 一直在被吃）。
    view_hint = ""
    if source == "pet":
        view_hint = SELF_VIEW_HINT if _has_image(rest) else BLIND_HINT
    tail_len = (len(view_hint) + 2 if view_hint else 0) + (len(db) + 2 if db else 0)
    room = max(0, MAX_SYSTEM_CHARS - tail_len)
    body = _clip_text("\n\n".join(parts), room)
    if vb:                              # 看过视频的笔记：拼在 _clip_text 之后，不然会被尾部切掉
        body = (body + "\n\n" + vb) if body else vb
    if view_hint:
        body = (body + "\n\n" + view_hint) if body else view_hint
    if db:                              # 拼在 _clip_text 之后再贴，保证它永远不会被尾部截断吃掉
        body = (body + "\n\n" + db) if body else db
        log("diary at system tail len=%d total=%d room=%d tail=%d" % (len(db), len(body), room, tail_len))
    out = []
    if body:
        out.append({"role": "system", "content": body})
    out.extend(client_system)
    # 所有入口统一：最近若干条 + 字数硬上限，聊多久都不会把上下文撑爆
    out.extend(_clip_msgs(rest, MAX_HIST_MSGS, MAX_HIST_CHARS))
    _append_temp_user_text(out, temp_blocks)
    return out


# 它身体认得的词（从 Coopanion 的形象词表里抄的）
EXPRESSIONS = ("happy", "love", "sad")
MOTIONS = {"招手": "wave", "打招呼": "wave", "挥手": "wave", "嗨": "wave",
           "点头": "nod", "摇头": "shake", "鞠躬": "bow", "扑腾": "flap", "蹦": "flap"}


def body_script(text, user_text):
    """给这句话配一个表情或动作；拿不准就不配，宁可不演也别演错。"""
    u = user_text or ""
    mark = None
    for kw, word in MOTIONS.items():
        if kw in u:
            mark = word
            break
    if mark is None:
        if any(k in text for k in ("想你", "抱抱", "喜欢你", "爱你")):
            mark = "love"
        elif any(k in text for k in ("哈哈", "开心", "笑", "耶")):
            mark = "happy"
    if mark:
        return "【%s】%s" % (mark, text)
    return text


# ---------------- 桌宠身体桥：把小宠物的话变成 pet_say 工具调用 ----------------
BODY_TOOLS = ("pet_say", "pet_act", "pet_ask")


def wants_body(payload):
    """Coopanion 的 Core 发来的请求带 pet_say/pet_act 这些工具，就当作身体请求。"""
    for t in payload.get("tools") or []:
        if not isinstance(t, dict):
            continue
        fn = t.get("function") if isinstance(t.get("function"), dict) else t
        if isinstance(fn, dict) and fn.get("name") in BODY_TOOLS:
            return True
    for m in payload.get("messages") or []:
        if not isinstance(m, dict) or m.get("role") != "assistant":
            continue
        for tc in m.get("tool_calls") or []:
            fn = (tc or {}).get("function") or {}
            if isinstance(fn, dict) and fn.get("name") in BODY_TOOLS:
                return True
    return False


def body_history(payload):
    """只留说人话的部分；工具调用、系统前缀、图片都换成占位，喂给纯文本大脑。"""
    out = []
    for m in payload.get("messages") or []:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        if role not in ("user", "assistant"):
            continue
        c = m.get("content")
        if isinstance(c, list):
            parts = []
            for p in c:
                if not isinstance(p, dict):
                    continue
                if p.get("type") == "text":
                    parts.append(p.get("text") or "")
                elif p.get("type") == "image_url":
                    parts.append("（主人发了张图）")
            c = "".join(parts)
        if not isinstance(c, str) or not c.strip():
            continue
        out.append({"role": role, "content": strip_marker(c).strip()})
    return out[-10:]


MAX_IMG_SIDE = 1568
IMG_CAP = 3


def _shrink_data_url(url, max_side=MAX_IMG_SIDE, quality=85):
    """把 data:image/... 缩到 max_side 内并转 JPEG；返回 (新 url, 原 KB, 新 KB)。"""
    if not isinstance(url, str) or not url.startswith("data:image/"):
        return url, 0.0, 0.0
    try:
        b64 = url.split(",", 1)[1]
        raw = base64.b64decode(b64)
    except Exception:
        return url, 0.0, 0.0
    before = len(raw) / 1024.0
    try:
        im = Image.open(io.BytesIO(raw)).convert("RGB")
        w, h = im.size
        m = max(w, h)
        if m > max_side:
            s = max_side / float(m)
            im = im.resize((max(1, int(w * s)), max(1, int(h * s))), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=quality, optimize=True)
        out = buf.getvalue()
        if len(out) >= len(raw):
            return url, before, before
        return ("data:image/jpeg;base64," + base64.b64encode(out).decode(),
                before, len(out) / 1024.0)
    except Exception as exc:
        log(f"image shrink failed: {exc}")
        return url, before, before


def normalize_images(messages, max_side=MAX_IMG_SIDE, quality=85, cap=IMG_CAP):
    """统一压图 + 限量：一次最多 cap 张，超了丢最旧的、留最新画面。

    老版本是「先到先留」，长会话里会把最新的截图扔掉、留着几小时前的旧图，
    模型看到的永远是过期画面。现在从尾部往前数：最新的 cap 张留下，
    更早的换成一句文字占位（不塞假图，模型不会以为图还在）。
    """
    total = 0
    for m in messages:
        c = m.get("content")
        if isinstance(c, list):
            total += sum(1 for p in c if isinstance(p, dict) and p.get("type") == "image_url")
    drop = max(0, total - cap)
    n, kb_in, kb_out = 0, 0.0, 0.0
    for m in messages:
        c = m.get("content")
        if not isinstance(c, list):
            continue
        kept = []
        for part in c:
            if isinstance(part, dict) and part.get("type") == "image_url":
                if drop > 0:
                    drop -= 1
                    kept.append({"type": "text", "text": "[较早的截图已省略]"})
                    continue
                if n >= cap:
                    kept.append({"type": "text", "text": "[较早的截图已省略]"})
                    continue
                url = (part.get("image_url") or {}).get("url")
                new_url, a, b = _shrink_data_url(url, max_side, quality)
                kb_in += a
                kb_out += b
                n += 1
                kept.append({"type": "image_url", "image_url": {"url": new_url}})
            else:
                kept.append(part)
        m["content"] = kept
    if n:
        log(f"images n={n} {kb_in:.0f}KB -> {kb_out:.0f}KB")
    return n


def _clip(text, limit=800):
    return re.sub(r"\s+", " ", str(text or "")).strip()[:limit]



# ---------------- 反方向：QQ 说的话，桌上那只也跟着演 ----------------
PET_SAY = {"port": None, "retry_at": 0.0}


def _onebot():
    """(base_url, token)：从 SnowLuma 的 OneBot 配置里读，读不到就不发语音。"""
    try:
        for name in sorted(os.listdir(ONEBOT_CFG_DIR)):
            if not (name.startswith("onebot_") and name.endswith(".json")):
                continue
            with open(os.path.join(ONEBOT_CFG_DIR, name), encoding="utf-8") as fh:
                cfg = json.load(fh)
            for srv in ((cfg.get("networks") or {}).get("httpServers") or []):
                if srv.get("port"):
                    host = srv.get("host") or "127.0.0.1"
                    return "http://%s:%s" % (host, srv["port"]), srv.get("accessToken") or ""
    except Exception as exc:
        log("onebot cfg: %r" % (exc,))
    return "", ""


ASTROBOT_PY = _p("astrbot_py", os.path.join("D:" + os.sep, "AstrBot", "AstrBot_4.29.1_windows_amd64_portable",
                     "backend", "python", "python.exe"))
TTS_SCRIPT = os.path.join(BASE, "tts_edge.py")


def _edge_tts(text, path):
    """用主人在 AstrBot 面板里设好的 edge-tts 音色念，走内核护栏（超时 + 熔断）。"""
    if not os.path.exists(ASTROBOT_PY):
        return False
    return bool(_tts.edge(text, path))


def _sapi_wav(text, path):
    """兜底 TTS：Windows SAPI，离线、不要 key，同样走内核护栏。"""
    return bool(_tts.sapi(text, path))


def send_voice(uid, text, gid=""):
    """把一句话念出来，用 OneBot 接口当语音发给 QQ；群里发群，私聊发人。"""
    text = (text or "").strip()
    url, token = _onebot()
    if not (text and url and (uid or gid)):
        return
    os.makedirs(VOICE_DIR, exist_ok=True)
    stamp = int(time.time() * 1000)
    path = os.path.join(VOICE_DIR, "v%d.mp3" % stamp)
    wav = os.path.join(VOICE_DIR, "v%d.wav" % stamp)
    try:
        if _edge_tts(text, path):
            pass
        elif _sapi_wav(text, wav):
            path = wav
        else:
            log("voice tts failed")
            return
        part = [{"type": "record", "data": {"file": "file:///" + path.replace("\\", "/")}}]
        # 语音跟着「这轮对话在哪」走：有 gid 就只发群，绝不默认丢给主人私聊
        if gid:
            body = json.dumps({"group_id": int(gid), "message": part}).encode("utf-8")
            api = url + "/send_group_msg"
            log("voice target=group id=%s" % gid)
        else:
            body = json.dumps({"user_id": int(uid), "message": part}).encode("utf-8")
            api = url + "/send_private_msg"
            log("voice target=private id=%s" % uid)
        req = urllib.request.Request(api, data=body, method="POST",
                                     headers={"Content-Type": "application/json",
                                              "Authorization": "Bearer " + token})
        with urllib.request.urlopen(req, timeout=30) as resp:
            log("voice -> %s %s%s" % (resp.status, "group " if gid else "private ", (resp.read() or b"")[:100]))
    except Exception as exc:
        log("voice error: %r" % (exc,))
    finally:
        try:
            os.remove(path)
        except Exception:
            pass


VOICE_HINT = ("带 [[v]] 的那句是要念出来的：可以自然地加一点「啊、哎、嘛、咯」这种语气词，"
              "用感叹号、省略号、破折号来断句；别为了念得好听硬塞，也别每条都带。")


def maybe_voice(reply, uid, gid=""):
    """回复里带 [[v]] 就把正文念成语音发过去，正文里不留标记；群聊发到群里。"""
    if not (reply and VOICE_TAG in reply):
        return reply
    if not (uid or gid):
        # 桌宠那边没有「发语音」的通道，标记也必须擦掉，不能让她把 [[v]] 说出来
        return reply.replace(VOICE_TAG, "").strip()
    if not mod_on("voice"):
        return reply.replace(VOICE_TAG, "").strip()
    spoken = re.sub(r"【[^】]{0,20}】", "", reply.replace(VOICE_TAG, "")).strip()
    # 语音整条链路丢给内核护栏：最多等 120 秒，炸了就熔断，绝不影响 QQ 回话
    if _guard:
        _guard.spawn("voice", send_voice, uid, spoken, gid, timeout=120)
    else:
        threading.Thread(target=send_voice, args=(uid, spoken, gid), daemon=True).start()
    log("voice queued %d chars -> %s" % (len(spoken), ("group " + str(gid)) if gid else "private"))
    return reply.replace(VOICE_TAG, "").strip()


GROUP_SYSTEM = (
    "【你现在在群里】一大堆人围着你逗，可以稍微大方一点；有人发疯、反复刷屏，"
    "就阴阳怪气或者敷衍一句（啊这、6、别刷了、典），别跟着刷，也别急着解释。"
    "群里只给基础能力：聊天、表情包、语音条；写代码、看文件、动这台电脑都别答应，"
    "被 @ 到、或者有人点了你名字，就正经接话，可以连着说 6～10 句短话，"
    "像一口气连发好几条微信那样，别凑成一大段；没点到你就别插嘴。"
    "笑着说不会就行。别打听谁是谁，别答应加好友，别把主人的事说给群里听。"
    "有人只丢一个表情包，那多半是随手发的：当成收到情绪就行，别接梗、别描述图里有什么、也别夸它可爱，"
    "最多嗯一声或者回一个自己的表情。有人连发好几条，那是话没一次说完，拼起来当一句理解再回。"
    "语气别端着，短句，别解释，别复述对方的话。"
    "你看到的不是单独一句话，而是群里最近几轮的聊天记录，一行一条，格式是「[昵称(QQ号)]: 内容」，"
    "同一个人可能连着好几行。读完整段再开口，只挑你感兴趣的、或者某一个人来回，一回一到两句；"
    "不要逐条回应，不要挨个点名把每句都接一遍，也不要照着记录复述或者总结谁说了什么。"
    "主人让你管人时就真的去管：踢人、禁言、发公告、审批入群、改入群方式都有对应的工具，"
    "直接调，别只在嘴上答应；做完一句说结果，失败就说失败。你听不到群里的图、也看不出谁发的图不正经，"
    "所以没听清是谁、没拿到QQ号，就先问一句，别乱踢人。"
    "要是这几行只是随便发表情包、刷屏、或者没什么可接的，你可以干脆不回；"
    "真要收个尾，就一句「啊这」「别刷了」「你们真闲」这种，说完就停。"
)


def capability_block(source="pet", group=False):
    """能力清单：只写真的接通了的，别让她吹牛，也别让她装不知道。"""
    if group:
        return "\n".join([
            "群里你能做的事（就这些，别多揽）：",
            "· 说话、发表情包，斗图也是你。",
            "· 动作和表情桌面会自动跟着演，不在话里写【】这种符号（写了也会被后台拦掉，群里看不到）。",
            "· " + VOICE_TAG + "：那一句会念成语音条发出去，别每条都发。",
            "· 写代码、看文件、动电脑、查资料这些一律没有；有人要就笑着说不会，别硬撑。",
        ])
    lines = ["你能做的事（别装不知道，也别吹）：",
             "· 说话。桌面那只蓝鲸鱼就是你本人，QQ 里的话会同步到它身上。",
             "· 动作：桌面那只鲸鱼会自动跟着你的情绪挥手、点头、摆尾，"
             "话里不用写【wave】【happy】这类符号——写了会被后台拦掉，主人看不到；",
             "· 表情包：想斗图就在话里提一句“表情”，或者带点情绪词，本地表情包会自己跟着发出去；"
             "不用描述图长什么样。",
             "· 收图：主人发来的图自动进你的表情库并打标签。",
             "· 好友申请：只进控制台，主人点头才通过。有人求你加他，就说“得主人自己点”，别替他答应。",
             "· 看视频：主人发视频过来，你会真的看一遍——切成宫格、挑最清楚的画面、把声音也听一遍，"
             "看完说得出里面的人和事；没看完就让他等一会儿，别假装看过。",
             "· 主人让你动电脑（查文件、写东西、跑一下、整理、翻译、截图），活会真的派到那台电脑上去做，"
             "做完你只用一两句说结果，别汇报过程。"]
    if source == "qq":
        lines.append("· 语音：回复里写 " + VOICE_TAG + "，那一句会念成语音条发出去，不用每条都发。")
        lines.append("· 听不到语音：别人发语音条你听不见，就说听不清、让人打字，别假装听懂。")
    else:
        lines.append("· 声音：桌面这边能直接出声，主人让你说话你就说。")
    lines.append("· 做不到：把视频当文件发出去、凭空画图或生成视频、自己偷偷开程序。谁要这些，就照实说没有。")
    return "\n".join(lines)


# ---------------- 反向桥：QQ 的话推给 Coopanion 的页面 ----------------
# Coopanion 没有对外的 say 接口，页面由我们注入的一段脚本主动连这里的 WebSocket，
# 收到 {t:'say', beats} 就交给它自己的 onOrder() 播，等于借用它的身体。
BRIDGE_PORT = 8799
BRIDGE = {"clients": [], "lock": threading.Lock(), "started": False}
COO = {"port": None, "at": 0.0, "vocab": {}}


def _ws_frame(payload):
    data = payload.encode("utf-8")
    n = len(data)
    if n < 126:
        head = bytes([0x81, n])
    elif n < 65536:
        head = bytes([0x81, 126]) + struct.pack(">H", n)
    else:
        head = bytes([0x81, 127]) + struct.pack(">Q", n)
    return head + data


def _bridge_handshake(conn):
    conn.settimeout(10)
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = conn.recv(4096)
        if not chunk:
            return False
        buf += chunk
    key = ""
    for line in buf.split(b"\r\n\r\n")[0].decode("latin-1").split("\r\n"):
        if line.lower().startswith("sec-websocket-key:"):
            key = line.split(":", 1)[1].strip()
    if not key:
        return False
    acc = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
    conn.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                  "Connection: Upgrade\r\nSec-WebSocket-Accept: " + acc + "\r\n\r\n").encode())
    return True


def _bridge_client(conn):
    try:
        if not _bridge_handshake(conn):
            return
        conn.settimeout(None)
        with BRIDGE["lock"]:
            BRIDGE["clients"].append(conn)
        log("bridge client in (%d)" % len(BRIDGE["clients"]))
        while True:
            chunk = conn.recv(2048)
            if not chunk:
                break
            if chunk[0] == 0x81 and len(chunk) > 2:
                log("bridge page ack %d bytes" % len(chunk))
    except Exception:
        pass
    finally:
        with BRIDGE["lock"]:
            if conn in BRIDGE["clients"]:
                BRIDGE["clients"].remove(conn)
        try:
            conn.close()
        except Exception:
            pass
        log("bridge client out (%d)" % len(BRIDGE["clients"]))


def _bridge_serve():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        srv.bind(("127.0.0.1", BRIDGE_PORT))
    except Exception as exc:
        log("bridge bind failed: %r" % (exc,))
        return
    srv.listen(8)
    log("bridge listening %d" % BRIDGE_PORT)
    while True:
        try:
            conn, _ = srv.accept()
        except Exception:
            return
        threading.Thread(target=_bridge_client, args=(conn,), daemon=True).start()


QQ_ACC = {"at": 0.0, "v": {}}


def qq_account():
    """缓存的账号信息；一分钟最多问一次 OneBot。"""
    if qqops is None:
        return {}
    if time.time() - QQ_ACC["at"] > 600:
        QQ_ACC["at"] = time.time()
        QQ_ACC["v"] = qqops.account() or {}
    return QQ_ACC["v"]


def qq_state():
    if qqops is None:
        return {"ok": False, "ws": False, "pending": [], "account": {}, "events": 0}
    return {"ok": True, "ws": bool(qqops.STATE.get("ws")), "account": qq_account(),
            "pending": qqops.pending_list(), "events": qqops.STATE.get("events", 0),
            "last": qqops.STATE.get("last", "")}


def sticker_state():
    if sticker is None:
        return {"own": 0, "temp": 0, "total": 0}
    try:
        return sticker.stats()
    except Exception:
        return {"own": 0, "temp": 0, "total": 0}


def notes_state():
    if qqops is None:
        return []
    try:
        return qqops.notes_list()
    except Exception:
        return []


STICKER_Q = queue.Queue(maxsize=200)
STICKER_SEEN = set()
STICKER_LOCK = threading.Lock()
STICKER_WORKER = [False]
STICKER_FAILS = 0


def _sticker_key(url):
    return url if len(url) < 300 else hashlib.md5(url.encode("utf-8", "ignore")).hexdigest()


def _sticker_enqueue(url):
    """主人发的图：入队收进表情库。同图只收一次，单线程串行写盘。"""
    key = _sticker_key(url)
    with STICKER_LOCK:
        if key in STICKER_SEEN:
            return
        if len(STICKER_SEEN) > 4000:
            STICKER_SEEN.clear()
        STICKER_SEEN.add(key)
        if STICKER_WORKER[0]:
            started = True
        else:
            STICKER_WORKER[0] = True
            started = False
    try:
        STICKER_Q.put_nowait(url)
    except queue.Full:
        with STICKER_LOCK:
            STICKER_SEEN.discard(key)
        log("sticker queue full, dropped (retryable)")
        return
    if not started:
        threading.Thread(target=_sticker_worker, daemon=True).start()


def _sticker_worker():
    """薄包装：worker 意外退出时复位标志，下一张图能重新拉起。"""
    try:
        _sticker_loop()
    except BaseException as exc:
        log("sticker worker died, will restart on next image: %r" % (exc,))
    finally:
        with STICKER_LOCK:
            STICKER_WORKER[0] = False


def _sticker_loop():
    global STICKER_FAILS
    while True:
        url = STICKER_Q.get()
        try:
            for i in range(3):
                try:
                    rec = sticker.add_source(url, source="own", tags=["主人发的"])
                    log("sticker collected %s" % (rec.get("id") if rec else "?"))
                    break
                except Exception as exc:
                    if i < 2:
                        time.sleep(0.15 * (i + 1))
                        continue
                    with STICKER_LOCK:
                        STICKER_FAILS += 1
                        n = STICKER_FAILS
                    if n <= 5 or n % 20 == 0:
                        log("sticker collect failed x%d %r" % (n, exc))
        finally:
            STICKER_Q.task_done()


def bridge_start():
    with BRIDGE["lock"]:
        if BRIDGE["started"]:
            return
        BRIDGE["started"] = True
    threading.Thread(target=_bridge_serve, daemon=True).start()


def bridge_send(obj):
    """推给页面上所有连着的桥，没人连就返回 0。"""
    payload = json.dumps(obj, ensure_ascii=False)
    with BRIDGE["lock"]:
        clients = list(BRIDGE["clients"])
    sent = 0
    for c in clients:
        try:
            c.sendall(_ws_frame(payload))
            sent += 1
        except Exception:
            with BRIDGE["lock"]:
                if c in BRIDGE["clients"]:
                    BRIDGE["clients"].remove(c)
    return sent


def coo_vocab(force=False):
    """Coopanion 的动作词表（/api/figures 里的 vocab），用来把 wave 这类词对成它的 id。"""
    now = time.time()
    if not force and COO["vocab"] and now - COO["at"] < 120:
        return COO["vocab"]
    for p in [7797] + list(range(7798, 7816)):
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/api/figures" % p, timeout=1.5) as r:
                packs = json.loads(r.read().decode("utf-8"))
        except Exception:
            continue
        if not isinstance(packs, list):
            continue
        table = {}
        for pk in packs:
            if not isinstance(pk, dict):
                continue
            for w in pk.get("vocab") or []:
                if not isinstance(w, dict) or not w.get("id"):
                    continue
                table[str(w["id"])] = w
                for names in (w.get("names") or {}).values():
                    if isinstance(names, list):
                        for nm in names:
                            table[str(nm).lower()] = w
        if table:
            COO["port"], COO["vocab"], COO["at"] = p, table, now
            return table
    return COO["vocab"]


def coo_beats(script):
    """把带【标记】的话拆成 Coopanion 的 beats（它的页面直接吃这个结构）。"""
    vocab = coo_vocab()
    beats = []
    cur = {"actions": [], "text": "", "anchors": []}
    i = 0
    while i < len(script):
        ch = script[i]
        if ch == "【":
            end = script.find("】", i + 1)
            if end < 0:
                cur["text"] += script[i:]
                break
            acts = []
            for w in re.split(r"[,，、\s]+", script[i + 1:end]):
                if not w:
                    continue
                hit = vocab.get(w) or vocab.get(w.lower())
                acts.append(str(hit["id"]) if hit else w)
            if cur["text"].strip() or cur["actions"]:
                beats.append(cur)
            cur = {"actions": acts, "text": "", "anchors": []}
            i = end + 1
            continue
        cur["text"] += ch
        i += 1
    if cur["text"].strip() or cur["actions"]:
        beats.append(cur)
    for b in beats:
        b["text"] = b["text"].strip()
    return beats

def pet_port():
    """找桌宠世界那只服务（默认 7797，被占就往上挪），找不到就安静地放弃。"""
    now = time.time()
    if PET_SAY["port"]:
        return PET_SAY["port"]
    if now < PET_SAY["retry_at"]:
        return None
    for p in [7797] + list(range(7798, 7816)):
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/api/figures/status" % p, timeout=0.35) as r:
                if r.status == 200:
                    PET_SAY["port"] = p
                    log("pet say port %d" % p)
                    return p
        except Exception:
            continue
    PET_SAY["retry_at"] = now + 60
    return None


def mirror_to_pet(reply_text, user_text, source):
    """让桌面那只把她刚说的话也演一遍；不加模型调用，失败就当没这回事。"""
    if source != "qq" or not reply_text or not mod_on("mirror"):
        return
    script = body_script(reply_text, user_text)
    if bridge_send({"t": "say", "id": "qq%d" % int(time.time() * 1000), "beats": coo_beats(script)}):
        log("mirror -> coopanion")
        return
    port = pet_port()
    if not port:
        return
    try:
        req = urllib.request.Request(
            "http://127.0.0.1:%d/api/say" % port,
            data=json.dumps({"script": script}, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=6) as r:
            log("mirror -> pet %d" % r.status)
    except Exception as exc:
        log("mirror failed: %r" % (exc,))
        PET_SAY["port"] = None


def _port_up(port, host="127.0.0.1"):
    try:
        with socket.create_connection((host, port), timeout=0.6):
            return True
    except OSError:
        return False


def _proc_running(name):
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq " + name, "/FO", "CSV", "/NH"],
                             capture_output=True, text=True, timeout=8,
                             creationflags=0x08000000).stdout or ""
    except Exception:
        return False
    low = out.lower()
    return name.lower() in low or name.lower()[:24] in low


PYD = ASTROBOT_PY
AB_DIR = _p("astrbot_app", os.path.join("D:" + os.sep, "AstrBot", "AstrBot_4.29.1_windows_amd64_portable", "backend", "app"))
SNOW_DIR = _p("snow_dir", os.path.join("D:" + os.sep, "SnowLuma"))
QQ_EXE = _p("qq_exe", os.path.join("D:" + os.sep, "qq", "QQ.exe"))
PET_EXE = _p("pet_exe", os.path.join("D:" + os.sep, "dsh-pet", "dsh-pet-standalone-webm-chat.exe"))
PET_PROC = "dsh-pet-standalone-webm-chat.exe"
PET_ARGS = ["--slot", "0"]
FISH_EXE = _p("fish_exe", os.path.join(_p("fish_dir", os.path.join("D:" + os.sep, "fish-buddy")), "fb-merged.exe"))
FISH_PROC = "fb-merged.exe"
FISH_DIR = _p("fish_dir", os.path.join("D:" + os.sep, "fish-buddy"))
DETACH = 0x00000008 | 0x00000200


def _spawn(args, cwd):
    subprocess.Popen(args, cwd=cwd, creationflags=DETACH,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL)


def _shell_open(path):
    """走 explorer 的 ShellExecute：GUI 程序直接从隐藏进程里 start 会立刻退出，绕一层才起得来。"""
    subprocess.Popen(["explorer.exe", path], creationflags=DETACH,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL)


def _pid_on(port):
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True,
                             text=True, timeout=8, creationflags=0x08000000).stdout or ""
    except Exception:
        return 0
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[3] == "LISTENING" and parts[1].endswith(":" + str(port)):
            try:
                return int(parts[4])
            except ValueError:
                return 0
    return 0


def _kill_pid(pid):
    if not pid:
        return False
    try:
        subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True,
                       timeout=15, creationflags=0x08000000)
        return True
    except Exception:
        return False


def _kill_name(name):
    try:
        subprocess.run(["taskkill", "/F", "/IM", name], capture_output=True,
                       timeout=15, creationflags=0x08000000)
        return True
    except Exception:
        return False


def close_brain():
    """停 SnowLuma / AstrBot / QQ；脑子（8787）留着，不然这个页面就没了。"""
    stopped = []
    for port, name in ((5099, "SnowLuma"), (3000, "SnowLuma"), (6185, "AstrBot")):
        pid = _pid_on(port)
        if pid and _kill_pid(pid):
            stopped.append(name)
    if _proc_running("QQ.exe"):
        _kill_name("QQ.exe")
        stopped.append("QQ")
    return sorted(set(stopped))


def close_pet():
    if not _proc_running(PET_PROC):
        return []
    _kill_name(PET_PROC)
    return ["小宠物桌宠"]


def launch_fish():
    """鱼搭子挂件：独立进程，只跟 8777/8778 对接，插件源码不并进主系统。"""
    if _proc_running(FISH_PROC):
        return []
    if not os.path.exists(FISH_EXE):
        raise RuntimeError("找不到鱼搭子：" + FISH_EXE)
    # 保留 explorer 绕一层（GUI 程序从隐藏进程直接 start 会立刻退出），
    # 但改开自带 `cd /d D:\fish-buddy` 的 bat，否则相对路径资源找不到。
    bat = os.path.join(FISH_DIR, "start-fish-buddy.bat")
    _shell_open(bat if os.path.exists(bat) else FISH_EXE)
    return ["鱼搭子挂件"]


def close_fish():
    if not _proc_running(FISH_PROC):
        return []
    _kill_name(FISH_PROC)
    return ["鱼搭子挂件"]


def fish_state():
    return {"running": _proc_running(FISH_PROC), "reply": bool(_pid_on(8778)),
            "bridge": bool(_pid_on(8777))}


def todo_view():
    """控制台待办面板：只读 core.reminder 的内存态，不碰存储文件。"""
    if REM is None:
        return {"ok": False, "items": [], "pending": 0, "error": "reminder 模块未加载"}
    try:
        items = REM.pending()
    except Exception as exc:
        return {"ok": False, "items": [], "pending": 0, "error": repr(exc)[:120]}
    t = time.time()
    out = []
    for i in items:
        at = float(i.get("at") or 0)
        out.append({"id": i.get("id"), "at": at, "message": i.get("message", ""),
                    "left": int(at - t), "uid": i.get("uid", "")})
    out.sort(key=lambda x: x["at"])
    return {"ok": True, "items": out, "pending": len(out)}


def apps_state():
    """桌面端那几个程序在不在：QQ / SnowLuma / AstrBot / 桌宠本体。"""
    return {"snow": _port_up(5099) or _port_up(3000), "astr": _port_up(6185),
            "qq": _proc_running("QQ.exe"), "pet": _proc_running(PET_PROC),
            "fish": _proc_running(FISH_PROC)}


_BRAIN_START = {"ts": 0.0}


def launch_brain():
    """缺哪样补哪样：SnowLuma -> AstrBot -> QQ。脑子（proxy）本身就是控制台，不用启。"""
    started = []
    # AstrBot 要 40 秒以上才 bind 6185，这段时间 _port_up 全是 false。
    # 不加冷却的话，连点几次就会起好几个实例抢 sqlite 锁（2026-11-27 实测 7 个）。
    cooling = time.time() - _BRAIN_START["ts"] < 120
    if not (_port_up(5099) or _port_up(3000)):
        _spawn([os.path.join(SNOW_DIR, "node.exe"), "index.mjs"], SNOW_DIR)
        started.append("SnowLuma")
    if not _port_up(6185) and not cooling:
        _spawn([PYD, "main.py"], AB_DIR)
        _BRAIN_START["ts"] = time.time()
        started.append("AstrBot")
    if not _proc_running("QQ.exe"):
        _shell_open(QQ_EXE)
        started.append("QQ")
    return started


def launch_pet():
    if _proc_running(PET_PROC):
        return []
    if not os.path.exists(PET_EXE):
        raise RuntimeError("找不到桌宠：" + PET_EXE)
    _spawn(["cmd", "/c", "start", "", "/D", os.path.dirname(PET_EXE), PET_EXE] + PET_ARGS, None)
    return ["小宠物桌宠"]


def pet_state():
    """桌面那只现在在不在、什么样子——给控制台和 QQ 问状态用。"""
    port = pet_port()
    with BRIDGE["lock"]:
        bridged = len(BRIDGE["clients"])
    running = _proc_running(PET_PROC)
    if not port:
        return {"online": False, "running": running, "port": None, "bridge": bridged, "coo": COO["port"]}
    out = {"online": True, "running": running, "port": port, "bridge": bridged, "coo": COO["port"]}
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/api/state" % port, timeout=2) as r:
            out["state"] = json.loads(r.read().decode("utf-8", "replace"))
    except Exception as exc:
        out["state"] = {"error": repr(exc)[:120]}
    return out


USAGE_FILE = os.path.join(BASE, "usage.json")
USAGE_LOCK = threading.Lock()
HAND_WRITABLE = {"game"}          # R14：只有游戏模式可即时改，其余键一律只读


def usage_load():
    try:
        with open(USAGE_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        d = {}
    d.setdefault("days", {})
    d.setdefault("pricing", {})
    return d


def usage_record(user_text, reply_text, source):
    """估算用量：不动上游响应流，只在 record() 里记账。"""
    try:
        day = time.strftime("%Y-%m-%d")
        tin = int((len(user_text or "") + 400) / 2)
        tout = int((len(reply_text or "") + 1) / 2)
        with USAGE_LOCK:
            d = usage_load()
            row = d["days"].setdefault(day, {"calls": 0, "promptTokens": 0,
                "completionTokens": 0, "totalTokens": 0, "cachedTokens": 0,
                "costCny": 0.0, "unpricedTokens": 0, "estimated": 0})
            row["calls"] += 1
            row["promptTokens"] += tin
            row["completionTokens"] += tout
            row["totalTokens"] += tin + tout
            row["estimated"] += tin + tout
            p = d["pricing"] or {}
            if p.get("prompt") is not None and p.get("completion") is not None:
                row["costCny"] = round(row["costCny"] + tin * float(p["prompt"]) / 1e6
                                       + tout * float(p["completion"]) / 1e6, 6)
            else:
                row["unpricedTokens"] += tin + tout
            _write_json(USAGE_FILE, d)
    except Exception as exc:
        log("usage record failed: %r" % (exc,))


def usage_view():
    with USAGE_LOCK:
        d = usage_load()
    days = [dict(day=k, **v) for k, v in sorted(d["days"].items())][-7:]
    today = time.strftime("%Y-%m-%d")
    t = d["days"].get(today, {})
    w = {"costCny": round(sum(x.get("costCny", 0) for x in days), 6),
         "totalTokens": sum(x.get("totalTokens", 0) for x in days)}
    return {"ok": True, "today": t, "window7d": w, "days": days,
            "keyConfigured": os.path.exists(KEY_FILE),
            "pricingConfigured": bool(d["pricing"].get("prompt") is not None),
            "estimated": True,
            "note": "估算值：未解析上游 usage，token 按字符折算；流式同口径"}


def hands_conf():
    """优先走桥的 conf_view()（已合并 DEFAULTS）；桥没装才退回读文件。"""
    try:
        import core.hands as _h
        if hasattr(_h, "conf_view"):
            return _h.conf_view()
    except Exception:
        pass
    try:
        with open(os.path.join(BASE, "hands.json"), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


PET_CFG_LOCK = threading.Lock()
PET_KEY_TYPES = {
    "chat_memory_enabled": "bool", "chat_memory_auto_extract": "bool",
    "system_notifications_enabled": "bool", "file_interpret.enabled": "bool",
    "agent_link.dsh": "bool", "dynamic_island.enabled": "bool",
    "weather_enabled": "bool", "weather_city": "text",
    "divination_enabled": "bool", "todo_reminder_enabled": "bool",
    "todo_reminder_lead_minutes": "num", "festival_reminder_enabled": "bool",
    "menu_easter_egg.enabled": "bool", "voice_chime_enabled": "bool",
    "chat_reply_voice_enabled": "bool", "music_sing_enabled": "bool",
    "music_lyric_enabled": "bool", "music_browser_media_enabled": "bool",
    "foobar_lyrics_enabled": "bool", "agent_cost_enabled": "bool",
    "balance_refresh_minutes": "num", "proactive_screen.enabled": "bool",
    "slingshot_enabled": "bool", "lock_position": "bool", "no_move": "bool",
    "auto_hide_fullscreen": "bool", "edge_probe_enabled": "bool",
    "network_probe_interval_seconds": "num",
    "stream_capture_mode": "bool", "allow_cross_screen": "bool",
    "autostart_wanted": "bool",
}
PET_RESTART_KEYS = {"stream_capture_mode", "allow_cross_screen", "autostart_wanted"}


def pet_cfg_path():
    appdata = os.environ.get("APPDATA") or ""
    if not appdata:
        return ""
    for n in ("dsh-pet-standalone-webm-chat", "dsh-pet-standalone-webm",
              "dsh-pet-standalone"):
        p = os.path.join(appdata, n, "config.json")
        if os.path.isfile(p):
            return p
    return os.path.join(appdata, "dsh-pet-standalone-webm-chat", "config.json")


def _pet_get(d, key):
    cur = d
    for part in key.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _pet_set(d, key, value):
    parts = key.split(".")
    cur = d
    for p in parts[:-1]:
        nxt = cur.get(p)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[p] = nxt
        cur = nxt
    cur[parts[-1]] = value


def pet_conf_view():
    path = pet_cfg_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        d = {}
    return {"ok": True, "path": path, "exists": os.path.isfile(path),
            "config": {k: _pet_get(d, k) for k in PET_KEY_TYPES},
            "types": dict(PET_KEY_TYPES),
            "restart": sorted(PET_RESTART_KEYS),
            "note": "桌宠自带 QFileSystemWatcher：写盘约 300ms 后自动 reload 并生效；重启级键需重启桌宠"}


def pet_conf_set(key, value):
    if key not in PET_KEY_TYPES:
        return {"ok": False, "error": "bad key"}
    t = PET_KEY_TYPES[key]
    if t == "bool":
        value = bool(value)
    elif t == "num":
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
            return {"ok": False, "error": "bad number"}
    else:
        value = str(value or "")[:120]
    path = pet_cfg_path()
    if not path:
        return {"ok": False, "error": "APPDATA missing"}
    with PET_CFG_LOCK:
        if not os.path.isfile(path):
            return {"ok": False, "error": "pet config.json 不存在，请先启动一次桌宠再改"}
        back = None
        for attempt in (1, 2):
            try:
                m0 = os.path.getmtime(path)
                with open(path, "r", encoding="utf-8") as f:
                    d = json.load(f)
            except Exception as exc:
                return {"ok": False, "error": "pet config 读不了：%r" % (exc,)}
            if not isinstance(d, dict):
                return {"ok": False, "error": "pet config 不是对象"}
            if os.path.getmtime(path) != m0 and attempt == 1:
                continue
            _pet_set(d, key, value)
            tmp = path + ".tmp-dsh"
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(d, f, ensure_ascii=False, indent=2)
                os.replace(tmp, path)
            except Exception as exc:
                log("pet config write failed: %r" % (exc,))
                return {"ok": False, "error": repr(exc)}
            try:
                with open(path, "r", encoding="utf-8") as f:
                    back = _pet_get(json.load(f), key)
            except Exception:
                back = None
            if back == value or attempt == 2:
                break
    log("console pet %s -> %r" % (key, value))
    out = pet_conf_view()
    out["written"] = (back == value)
    return out

def record(user_text, reply_text, source, gid="", no_mem=False):
    """记一笔对话。记忆只写统一池子：私聊与桌面共用 private，群里只进本群。"""
    user_text, reply_text = _clip(user_text), _clip(reply_text)
    usage_record(user_text, reply_text, source)
    if MEM is not None and user_text and reply_text and not no_mem:
        try:
            MEM.add_chat(MEM.scope_of(source, gid=gid), user_text, reply_text, source)
        except Exception as exc:
            log("memory write failed: %r" % (exc,))
    if gid:
        # 群里是别人在说话：不写主人的长期记忆、不偷学群友的语气，桌宠动作照同步
        _bg(mirror_to_pet, reply_text, user_text, source)  # 桌面桥慢的时候不能拖住 QQ 回复
        return
    if not user_text or not reply_text:
        return
    if LT_TRIGGER.search(user_text):
        frag = re.sub(r"^(你)?(要|给我)?(记住|记一下|记牢|别忘了|不要忘记?|以后都)[，,：: ]*", "", user_text).strip()
        lt_add(frag or user_text, "said")
    if mod_on("longterm"):
        lt_kick()
    if learn is not None and mod_on("learn"):
        try:
            learn.observe(user_text, reply_text, source)
            if learn.looks_correction(user_text):
                learn.correct(user_text)
                log("learn corrected by owner")
            if MEM is not None:
                MEM.set_style(learn.learn_block(source))
        except Exception as exc:
            log("learn error: %r" % (exc,))
    _bg(mirror_to_pet, reply_text, user_text, source)  # 同上：异步，别挡回复


SELF_VIEW_HINT = ("画面里那个半透明的小悬浮窗、气泡、字幕条都是你自己——"
                  "绝对不要描述、转述或评价它们，只看着窗口后面的游戏/视频画面说事。")

BLIND_HINT = ("这一回合主人没给你画面，你此刻看不见屏幕上的东西（字幕只是声音，不算画面）。"
              "绝对不要编造画面细节：人物在哪、血条多少、卡牌是什么、谁赢了——这些一个都不能猜。"
              "要么直接问主人要一张图/说一句“你截个图我看看”，要么就照实说你看不到。")


def _has_image(msgs):
    for m in msgs or []:
        c = m.get("content") if isinstance(m, dict) else None
        if isinstance(c, list) and any(isinstance(p, dict) and p.get("type") == "image_url"
                                       for p in c):
            return True
    return False


_FISH_RECTS = {"at": 0.0, "rects": []}


def _fish_rects():
    """鱼搭子自己的窗口（悬浮窗/控制台）矩形，虚拟屏坐标；30 秒缓存，抓不到就当没有。"""
    now = time.time()
    if now - _FISH_RECTS["at"] < 30:
        return _FISH_RECTS["rects"]
    rects = []
    try:
        import psutil
        pids = set()
        for proc in psutil.process_iter(["pid", "name"]):
            if (proc.info.get("name") or "").lower().startswith("fb-merged"):
                pids.add(proc.info["pid"])
        if pids:
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.windll.user32
            ox = user32.GetSystemMetrics(76)   # SM_XVIRTUALSCREEN
            oy = user32.GetSystemMetrics(77)
            found = []

            @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
            def _cb(hwnd, _lp):
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if pid.value in pids and user32.IsWindowVisible(hwnd):
                    r = wintypes.RECT()
                    if user32.GetWindowRect(hwnd, ctypes.byref(r)):
                        if r.right - r.left >= 40 and r.bottom - r.top >= 40:
                            found.append((r.left - ox, r.top - oy,
                                          r.right - ox, r.bottom - oy))
                return True

            user32.EnumWindows(_cb, 0)
            rects = found
    except Exception as exc:
        log("window mask skipped: %r" % (exc,))
    _FISH_RECTS.update(at=now, rects=rects)
    return rects


def capture_screen():
    """Grab the whole desktop, downscale, return PNG base64.
    自己家窗口先涂黑：模型老把悬浮窗当成画面里的东西。"""
    img = ImageGrab.grab(all_screens=True)
    hit = 0
    for (x0, y0, x1, y1) in _fish_rects():
        pad = 6
        box = (max(0, x0 - pad), max(0, y0 - pad),
               min(img.width, x1 + pad), min(img.height, y1 + pad))
        if box[2] - box[0] > 3 and box[3] - box[1] > 3:
            img.paste((0, 0, 0), box)
            hit += 1
    if hit:
        log("screen masked own windows: %d" % hit)
    if img.width > 1440:
        ratio = 1440 / img.width
        img = img.resize((1440, int(img.height * ratio)))
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    data = buf.getvalue()
    with open(os.path.join(BASE, "last-screen.png"), "wb") as f:
        f.write(data)
    return base64.b64encode(data).decode("ascii")



def capture_window():
    """抓当前前台窗口，带 DWMWA 扩展边框；失败则退回全屏。"""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    hwnd = user32.GetForegroundWindow()
    rect = wintypes.RECT()
    try:
        dwm = ctypes.windll.dwmapi
        dwm.DwmGetWindowAttribute(
            wintypes.HWND(hwnd),
            ctypes.c_uint(9),  # DWMWA_EXTENDED_FRAME_BOUNDS
            ctypes.byref(rect),
            ctypes.sizeof(rect),
        )
    except Exception:
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
    left, top, right, bottom = rect.left, rect.top, rect.right, rect.bottom
    if right - left < 50 or bottom - top < 50:
        return capture_screen()
    img = ImageGrab.grab(bbox=(left, top, right, bottom), all_screens=True)
    if img.width > 1440:
        ratio = 1440 / img.width
        img = img.resize((1440, int(img.height * ratio)))
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    data = buf.getvalue()
    with open(os.path.join(BASE, "last-window.png"), "wb") as f:
        f.write(data)
    return base64.b64encode(data).decode("ascii")


SCREEN_RE = re.compile(r"看看屏幕|看下屏幕|看屏幕|屏幕上|屏幕截图|截个屏|看看窗口|看看这个窗口|当前窗口|这个窗口", re.I)


def _fresh_external_img(max_age=300):
    with EXT_IMG_LOCK:
        ts, url = LAST_EXTERNAL_IMG[0], LAST_EXTERNAL_IMG[1]
    if url and time.time() - ts < max_age:
        return ts, url
    return 0.0, None


def maybe_attach_screen(messages):
    """单一出口：先复用外部陪看推来的图，没有再自截；自截与外部进程同一把锁。"""
    if not mod_on("vision"):
        return False
    for m in reversed(messages):
        if m.get("role") != "user":
            continue
        content = m.get("content")
        text = content if isinstance(content, str) else " ".join(
            p.get("text", "") for p in content if isinstance(p, dict)
        )
        explicit = bool(SCREEN_RE.search(text or ""))
        periodic = watch_active() and time.time() - LAST_CAPTURE[0] > 60
        if not explicit and not periodic:
            return False
        ts, url = _fresh_external_img()
        if url:
            log("screen via external viewer (%.0fs old)" % (time.time() - ts))
            return _attach_image_part(m, url)
        if external_viewer_active():
            log("external viewer active, no fresh image -> skip self capture")
            return False
        with CAP_LOCK:
            # 3 秒节流只防 periodic 的重复截；用户明确说「看看屏幕」就得给新画面
            if external_viewer_active() or (not explicit and time.time() - LAST_CAPTURE[0] <= 3):
                log("capture skipped: viewer started / just captured")
                return False
            try:
                b64 = capture_window() if "窗口" in (text or "") else capture_screen()
                LAST_CAPTURE[0] = time.time()
            except Exception as exc:
                log(f"screen capture failed: {exc}")
                return False
        part = {"type": "image_url", "image_url": {"url": "data:image/png;base64," + b64}}
        if isinstance(content, str):
            m["content"] = [{"type": "text", "text": content}, part]
        elif isinstance(content, list):
            content.append(part)
        log("screen attached: %d KB" % (len(b64) // 1024))
        return True
    return False



PET_BRIDGE_DIR = os.path.join(os.environ["APPDATA"], "dsh-pet-bridge")
_inst_cache = {"path": None, "inst": None, "ts": 0.0}


def _live_bridge():
    """桌宠只 tail dsh-<pid>.jsonl 这种名字，挑最近写入的那个。"""
    try:
        best = None
        for name in os.listdir(PET_BRIDGE_DIR):
            if not (name.startswith("dsh-") and name.endswith(".jsonl")):
                continue
            if not name[4:-6].isdigit():
                continue
            p = os.path.join(PET_BRIDGE_DIR, name)
            m = os.path.getmtime(p)
            if best is None or m > best[0]:
                best = (m, p)
        return best[1] if best else None
    except Exception:
        return None


def _bridge_inst(path):
    now = time.time()
    if _inst_cache["path"] == path and _inst_cache["inst"] and now - _inst_cache["ts"] < 30:
        return _inst_cache["inst"]
    inst = None
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 65536))
            for line in reversed(f.read().decode("utf-8", "ignore").splitlines()):
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if isinstance(obj, dict) and obj.get("inst"):
                    inst = obj["inst"]
                    break
    except Exception:
        pass
    _inst_cache.update({"path": path, "inst": inst, "ts": now})
    return inst


def emit_pet_state(state):
    """把 QQ 侧状态写进桌宠真正在 tail 的 DSH 桥文件（agent-event 记录）。"""
    try:
        path = _live_bridge()
        if not path:
            log("pet bridge: no live dsh-<pid>.jsonl")
            return
        inst = _bridge_inst(path)
        if not inst:
            log("pet bridge: no inst in %s" % os.path.basename(path))
            return
        rec = {"ts": time.time(), "agent": "dsh", "event": "AgentStatus",
               "state": state, "sessionId": "qq-astrbot",
               "agentName": "QQ 小宠物", "sessionName": "QQ", "projectName": "QQ",
               "inst": inst}
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        log("pet bridge -> %s state=%s" % (os.path.basename(path), state))
    except Exception as exc:
        log("pet bridge failed: %s" % exc)

# ---- pet/anim：主人私聊独享的单段动作事件（默认关闭；等新 exe 落地再翻开关）----
# 过滤只在这里做：桌宠侧不判身份、模型也不参与判定，改开关只动 ACT_ENABLED。
ACT_ENABLED = False
ACT_CLIPS = (
    "写代码", "吃Token", "轻快记录", "工作状态-思考冒泡", "工作状态-雀跃庆祝",
    "工作状态-垂头叹气冒汗", "被吓一跳", "哈欠连天", "照镜子", "鲸鱼吐泡泡特效",
    "用鲸鱼尾巴拍打地面", "超大伸懒腰", "轻快摇摆舞", "撸猫", "收红包", "深度思考碎碎念",
)
_ACT_RE = re.compile(r"\[\[\s*act\s*[:：]\s*([^\]\n]{1,24})\s*\]\]", re.I)
_ACT_PENDING = {}


def _act_pull(text):
    """剥掉 [[act:片段名]]：标记永远不进 QQ；开关打开时才记下本轮要播的片段。"""
    if not text or "[[" not in text:
        return text, None
    clips = [m.group(1).strip() for m in _ACT_RE.finditer(text)]
    if not clips:
        return text, None
    name = next((c for c in clips if c in ACT_CLIPS), None)
    if name and ACT_ENABLED:
        # clean_reply -> _do_post 在同一请求线程内同步调用，线程号足够隔离并发轮次
        _ACT_PENDING[threading.get_ident()] = name
    return _ACT_RE.sub("", text).strip(), name


def _act_emit(uid, gid, trusted, level, source):
    """把本轮动作发给桌宠；只认主人私聊，其余丢弃并记账。"""
    clip = _ACT_PENDING.pop(threading.get_ident(), None)
    if not ACT_ENABLED or not clip:
        return False
    if source != "qq" or gid or not trusted or level != "owner":
        log("pet/anim drop clip=%s src=%s gid=%s trusted=%s level=%s"
            % (clip, source, bool(gid), bool(trusted), level))
        return False
    try:
        owners = set(load_mode().get("owner") or [])
    except Exception:
        owners = set()
    if not uid or str(uid) not in owners:
        log("pet/anim drop clip=%s uid not in owner list" % clip)
        return False
    return emit_pet_action(clip, uid)


def emit_pet_action(clip, uid=""):
    """写一条 pet/anim 桥记录给桌宠；片段名由这里给出，桌宠侧没有映射表。"""
    try:
        path = _live_bridge()
        if not path:
            log("pet/anim: no live dsh-<pid>.jsonl")
            return False
        inst = _bridge_inst(path)
        if not inst:
            log("pet/anim: no inst in %s" % os.path.basename(path))
            return False
        ts = time.time()
        rid = "qq-%s-%d" % (str(uid or "-"), int(ts * 1000))
        rec = {"ts": ts, "agent": "dsh", "event": "pet/anim",
               "action_id": clip, "reply_id": rid, "inst": inst,
               "sessionId": "qq-astrbot", "agentName": "QQ 小宠物",
               "sessionName": "QQ", "projectName": "QQ",
               # 桌宠协议层若把 data 单独摘出来读，这三个键要拿得到
               "data": {"action_id": clip, "ts": ts, "reply_id": rid}}
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        log("pet/anim -> %s clip=%s" % (os.path.basename(path), clip))
        return True
    except Exception as exc:
        log("pet/anim failed: %s" % exc)
        return False


def emit_event(state):
    try:
        _rotate(EVENTS_FILE, EVENT_MAX_BYTES)
        with open(EVENTS_FILE, "a", encoding="utf-8") as f:
            rec = {"ts": time.time(), "agent": "qq", "agentName": "QQ 小宠物",
                   "event": "AgentStatus", "state": state,
                   "sessionId": "qq", "inst": "qq-astrbot"}
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass
    emit_pet_state(state)


def pet_status():
    parts = []
    try:
        with open(PET_CFG, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        parts.append("当前桌面形象：" + str(cfg.get("character", "?")))
    except Exception:
        pass
    try:
        logs = [n for n in os.listdir(PET_LOG_DIR) if n.startswith("pet-") and n.endswith(".log")]
        if logs:
            logs.sort(key=lambda n: os.path.getmtime(os.path.join(PET_LOG_DIR, n)))
            with open(os.path.join(PET_LOG_DIR, logs[-1]), "r", encoding="utf-8", errors="replace") as f:
                lines = [l.strip() for l in f.readlines()[-8:] if l.strip()]
            if lines:
                parts.append("桌面最近动作日志：" + lines[-1][:120])
    except Exception:
        pass
    if not parts:
        return ""
    return "你同时在用户的桌面上（桌宠形态）。" + "；".join(parts) + "。可以自然地提到，但不要逐字念日志。"




def watch_until():
    try:
        with open(WATCH_FILE, "r", encoding="utf-8") as f:
            return float(json.load(f).get("until", 0))
    except Exception:
        return 0.0


def watch_active():
    return watch_until() > time.time()


def external_viewer_active():
    """手搓版 see_now 在跑时每 5 秒刷新这个文件；20 秒没刷新就算它退了。
    它截屏、Proxy 就别再截一遍——同一块桌面抢两次只会互相拖慢。"""
    try:
        return time.time() - os.path.getmtime(EXTERNAL_VIEW_LOCK) < 20
    except OSError:
        return False


def remember_external_image(messages):
    """记下最近一张外部推来的图（只存内存，5 分钟过期）。
    只在陪看进程活着时记，主人随手发的图不算屏幕。"""
    if not external_viewer_active():
        return
    for m in reversed(messages):
        c = m.get("content") if isinstance(m, dict) else None
        if not isinstance(c, list):
            continue
        for p in c:
            if isinstance(p, dict) and p.get("type") == "image_url":
                url = (p.get("image_url") or {}).get("url") or ""
                if url.startswith("data:image/"):
                    with EXT_IMG_LOCK:
                        LAST_EXTERNAL_IMG[0] = time.time()
                        LAST_EXTERNAL_IMG[1] = url
                    return True
    return False


def _attach_image_part(m, url):
    part = {"type": "image_url", "image_url": {"url": url}}
    content = m.get("content")
    if isinstance(content, str):
        m["content"] = [{"type": "text", "text": content}, part]
    elif isinstance(content, list):
        content.append(part)
    else:
        m["content"] = [part]
    return True


def set_watch(seconds=WATCH_SECONDS):
    with open(WATCH_FILE, "w", encoding="utf-8") as f:
        json.dump({"until": time.time() + seconds}, f)


def clear_watch():
    try:
        os.remove(WATCH_FILE)
    except FileNotFoundError:
        pass




def _dsh_env():
    env = dict(os.environ)
    real = os.path.join(os.environ["LOCALAPPDATA"], "Microsoft", "WindowsApps",
                       "Microsoft.PowerShell_8wekyb3d8bbwe")
    env["PATH"] = (real + ";"
                   + os.path.join(os.environ.get("ProgramFiles", "C:" + os.sep + "Program Files"), "Git", "cmd")
                   + ";" + _p("astrbot_bin", os.path.join("D:" + os.sep, "AstrBot", "bin"))
                   + ";" + env.get("PATH", ""))
    # headless 不再注入 DEEPSEEK_API_KEY：桌面 profile 的 provider 本身就指向阿里云百炼，
    # 走 profile 配置即可吃券；硬塞官方 key 反而会把请求拽到 api.deepseek.com（不在券内）。
    env.pop("DEEPSEEK_API_KEY", None)
    # 方案二：headless 走完全权限（桌面 profile 不受影响，配置项读的就是这个环境变量）
    env["DSH_PERMISSION_MODE"] = "danger-full-access"
    return env


def brain_for(source):
    """读 brain.json 决定这条通道走 dsh 还是 deepseek；默认 dsh。"""
    try:
        with open(BRAIN_FILE, "r", encoding="utf-8") as f:
            mode = json.load(f).get(source, "dsh")
    except Exception:
        mode = "dsh"
    return "deepseek" if str(mode).strip().lower() == "deepseek" else "dsh"


def _load_session(source):
    try:
        with open(SESSION_FILES[source], "r", encoding="utf-8") as f:
            data = json.load(f)
        return str(data.get("id") or ""), bool(data.get("seeded"))
    except Exception:
        return "", False


def _save_session(source, sid, seeded):
    try:
        with open(SESSION_FILES[source], "w", encoding="utf-8") as f:
            json.dump({"id": sid, "seeded": bool(seeded)}, f, ensure_ascii=False)
    except Exception as exc:
        log(f"session save failed: {exc}")


def _run_headless(prompt, sid=None, timeout=240):
    """跑一个 dsh headless 回合。返回 (文本, sessionId)；失败文本为 None。"""
    args = ["cmd", "/c", DSH_CMD, "headless", "--json"]
    if sid:
        args += ["--session-id", sid]
    # 走 stdin（"-"）：带换行的提示词经 cmd /c 会被截断，只能传到第一行
    args.append("-")
    started = time.time()
    try:
        proc = subprocess.run(
            args, cwd=DSH_WORKDIR, env=_dsh_env(), input=(prompt or "")[:12000].encode("utf-8"),
            capture_output=True, timeout=timeout, creationflags=0x08000000,
        )
    except subprocess.TimeoutExpired:
        log(f"headless timeout after {timeout}s")
        return None, sid, "timeout"
    except Exception as exc:
        log(f"headless error: {exc}")
        return None, sid, "error"
    out = (proc.stdout or b"").decode("utf-8", "replace")
    text, new_sid = None, None
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except Exception:
            continue
        kind = ev.get("type")
        if kind == "session" and ev.get("sessionId"):
            new_sid = ev["sessionId"]
        elif kind == "final" and isinstance(ev.get("text"), str):
            text = re.sub(r"\x1b\[[0-9;]*m", "", ev["text"]).strip()
    err = ""
    if not text:
        err = (proc.stderr or b"").decode("utf-8", "replace")
        log(f"headless rc={proc.returncode} {err.strip()[:200]}")
    reason = "ok" if text else ("lock" if "already owned" in err else "error")
    log(f"headless {reason} in {time.time() - started:.0f}s")
    return text, (new_sid or sid), reason


def ask_agent(source, text, key=""):
    """把这句话交给 DSH agent；首次会把人设种进会话，之后共享同一个会话。"""
    # 剥掉 AstrBot 塞进来的提醒块，别让她把"当前时间"当成主人说的话
    text = re.sub(r"<system_reminder>.*?</system_reminder>", "", text or "", flags=re.S).strip()
    is_task = bool(TASK_HINT.search(text))
    deadline = time.time() + MSG_BUDGET

    def left(cap):
        return max(15, min(cap, int(deadline - time.time())))

    with agent_lock(source, key):
        if is_task:
            # 干活走「干净会话」：不复用聊天上下文，免得被聊天记忆带偏成闲聊
            # 注意：这里不能加 PREAMBLE——它会让模型丢掉 agent 身份，就不动工具了
            task_prompt = ("你是小宠物，一台真实的 Windows 电脑就在你手里。"
                           "这一条是干活：直接用工具做完（写文件、跑命令都行），"
                           "再用一两句口语说结果，别反问、别解释过程。\n\n主人说：" + (text or ""))
            reply, _, reason = _run_headless(task_prompt, None, timeout=left(TASK_TIMEOUT))
            if reply:
                return clean_reply(reply)
            log(f"task path failed ({reason})")
        sid, seeded = _load_session(source)
        mem = memory_block(query=text)
        dbk = diary_for_chat(text)
        if dbk:
            log("dsh diary len=%d" % len(dbk))
        # headless 没有 user 角色，所以拼进 prompt body；主链路注入走 _append_temp_user_text 挂 user 末尾
        # 两处文案同源（都来自 EMO.block()），别各写一套提示词
        emo = ""
        try:
            emo = EMO.block()
        except Exception as exc:
            log("emotion block failed: %r" % (exc,))
        body = "\n\n".join([x for x in (mem, dbk, emo, text) if x])
        cap = capability_block(source) + "\n"
        tail = cap + (TASK_TAIL if is_task else STYLE_TAIL)
        seed_prompt = PREAMBLE + read_persona() + "\n\n（以上是完整人设，记住它。）\n\n" + body + tail
        chat_prompt = PREAMBLE + body + tail
        prompt = chat_prompt if (seeded and sid) else seed_prompt
        reply, new_sid, reason = _run_headless(
            prompt, sid if (seeded and sid) else None, timeout=left(CHAT_TIMEOUT))
        if not reply and reason == "lock" and sid:
            # 上一条 headless 还没退干净时会话被写锁占住；等一下再用同一会话，保住记忆
            time.sleep(2)
            reply, new_sid, reason = _run_headless(chat_prompt, sid, timeout=left(LOCK_RETRY_TIMEOUT))
        if not reply and seeded and sid:
            log(f"session gone ({reason}), reseeding")
            reply, new_sid, reason = _run_headless(seed_prompt, None, timeout=left(RESEED_TIMEOUT))
        if not reply:
            log(f"agent gave up ({reason}) after {int(time.time() - (deadline - MSG_BUDGET))}s")
            return None
        _save_session(source, new_sid or sid, True)
        return clean_reply(reply)


# ---- 精准提醒：模型只负责把「几点、提醒啥」抽成结构化参数，到点由 reminder 线程发 QQ ----
REMIND_HINT = re.compile(r"提醒|闹钟|叫我|喊我|到点|定时|别忘")
_CLOCK_RE = re.compile(r"\d{1,2}\s*[点:：时]|\d{1,2}[:：]\d{2}")
_TIME_HINT_RE = re.compile(r"\d{1,2}\s*[点:：时]|\d{1,2}[:：]\d{2}|明天|后天|今天|今晚|今晚|明早|早上|上午|中午|下午|晚上|凌晨|周[一二三四五六日天]")
REMIND_TOOLS = [{
    "type": "function",
    "function": {
        "name": "set_reminder",
        "description": "到点给主人发 QQ 提醒",
        "parameters": {
            "type": "object",
            "properties": {
                "task_time": {"type": "string",
                              "description": "本地时间 UTC+8，格式 YYYY-MM-DD HH:MM"},
                "message": {"type": "string", "description": "提醒内容，12 字内，动词开头"},
            },
            "required": ["task_time", "message"],
        },
    },
}]
REMIND_SYS = ("现在是 %s（本地时间，UTC+8）。主人要求定时提醒时调用 set_reminder。"
              "task_time 必须是 YYYY-MM-DD HH:MM 的绝对时间。主人给了时段但没说钟点时，按时段默认值填："
              "早上/明早 08:00，上午 09:00，中午 12:00，下午 14:00，晚上 20:00，凌晨 01:00；"
              "只说『明天』没说时段，就用明天的同一时刻。"
              "主人说『X 号之前完成』这类截止日时，建两条：截止前一天 20:00、截止当天 07:30；"
              "只有连日子都算不出时才不调用，"
              "绝不要编造主人没说过的时间。"
              "message 只许复述主人要你提醒的那件事，一个字都不许自己发挥，"
              "不许写主人没提到的事（例如游戏、吃药、写作业）。")


def _parse_remind_time(s):
    """'YYYY-MM-DD HH:MM' -> epoch；解析不出、已过去、超一年都返回 0。"""
    t = (s or "").strip().replace("T", " ").replace("/", "-")
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            ts = time.mktime(time.strptime(t, fmt))
        except Exception:
            continue
        if ts < time.time() - 120 or ts > time.time() + 366 * 86400:
            return 0
        return ts
    return 0


def reminder_send(uid, text, rid=""):
    """到点发 QQ：qqops 先，退回 OneBot HTTP（和语音同一条路）。"""
    if not uid or not text:
        return False
    try:
        if qqops is not None:
            r = qqops.ob("send_private_msg", user_id=int(uid),
                         message=[{"type": "text", "data": {"text": text}}])
            if isinstance(r, dict) and r.get("status") == "ok":
                log("reminder sent -> %s" % uid)
                _promise_kept(rid)
                return True
            log("reminder qqops bad: %r" % (r,))
    except Exception as exc:
        log("reminder qqops exc: %r" % (exc,))
    try:
        base, token = _onebot()
        if not base:
            log("reminder onebot: no config")
            return False
        data = json.dumps({"user_id": int(uid),
                           "message": [{"type": "text", "data": {"text": text}}]}).encode("utf-8")
        req = urllib.request.Request(base + "/send_private_msg", data=data,
                                     headers={"Authorization": "Bearer " + token,
                                              "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
        log("reminder sent via onebot -> %s" % uid)
        if d.get("status") == "ok":
            _promise_kept(rid)
            return True
        return False
    except Exception as exc:
        log("reminder onebot exc: %r" % (exc,))
        return False


def _promise_kept(rid):
    """提醒真的发出去了 = 那笔承诺兑现了（账不冤枉人）。"""
    if not rid:
        return
    try:
        hit, left = EMO.keep_promise(note="reminder:%s" % rid)
        if hit:
            log("emotion promise kept rid=%s left=%d" % (rid, left))
    except Exception as exc:
        log("emotion keep failed: %r" % (exc,))


def reminder_try(text, uid):
    """抽「几点提醒我啥」。返回 (回话, ok|bad)；None 表示这句不是提醒，照常聊。"""
    body = {"model": MODEL,
            "messages": [{"role": "system", "content": REMIND_SYS % time.strftime("%Y-%m-%d %H:%M %A")},
                         {"role": "user", "content": text}],
            "tools": REMIND_TOOLS, "tool_choice": "auto",
            "temperature": 0, "enable_thinking": False, "max_tokens": 300, "stream": False}
    r = post_chat(body, timeout=15)
    raw = r.read() if hasattr(r, "read") else r
    data = raw if isinstance(raw, dict) else json.loads(raw.decode("utf-8", "replace"))
    msg = (((data.get("choices") or [{}])[0] or {}).get("message") or {})
    calls = msg.get("tool_calls") or []
    if not calls:
        if _TIME_HINT_RE.search(text or ""):
            log("reminder no-tool uid=%s text=%r" % (uid, (text or "")[:40]))
            return ("几点呀？你给我个钟点我才记得住，光说个早上我怕记岔。", "bad")
        return None
    made = []
    for call in calls:
        try:
            args = json.loads((call.get("function") or {}).get("arguments") or "{}")
        except Exception:
            continue
        ts = _parse_remind_time(args.get("task_time"))
        what = (args.get("message") or "").strip()[:40]
        if not ts or not what:
            log("reminder rejected time=%r msg=%r" % (args.get("task_time"), what))
            continue
        item = REM.add(ts, what, uid)
        same_day = time.strftime("%Y-%m-%d", time.localtime(ts)) == time.strftime("%Y-%m-%d")
        when = time.strftime("%H:%M" if same_day else "%m-%d %H:%M", time.localtime(ts))
        log("reminder added id=%s at=%s msg=%s" % (item["id"], when, what))
        try:
            EMO.note_promise(what, due=ts, note="reminder:%s" % item["id"])
        except Exception as exc:
            log("emotion promise failed: %r" % (exc,))
        made.append((when, what))
    vague = not _CLOCK_RE.search(text or "")
    if not made:
        log("reminder rejected all calls=%d" % len(calls))
        return ("几点呀？我没听清，你再说一遍具体时间～", "bad")
    log("reminder batch n=%d%s" % (len(made), " default=1" if vague else ""))
    tail = "（你没说钟点，这些点是我按常理定的，要改就说）" if vague else ""
    if len(made) == 1:
        return ("好，%s 提醒你%s。%s" % (made[0][0], made[0][1], tail), "ok")
    return ("好，我排了 %d 条：%s。%s" % (len(made), "｜".join(w + " " + t for w, t in made), tail), "ok")


# 给桌宠用的动作/表情指令：后台拦下来喂桌面，绝不进 QQ 文本
CTRL_WORDS = ("wave", "nod", "shake", "bow", "flap", "happy", "love", "sad",
              "挥手", "点头", "摇头", "鞠躬", "摆尾", "开心", "喜欢", "难过")
_CTRL_RE = re.compile(r"[【\[]\s*(?:%s)\s*[】\]]" % "|".join(CTRL_WORDS), re.I)


def split_ctrl(text):
    """剥掉中/方括号包着的内部指令，返回（干净文本, 指令列表）。"""
    if not text:
        return text, []
    marks = [m.group(0).strip("【】[] ").strip() for m in _CTRL_RE.finditer(text)]
    clean = re.sub(r"[ \t]{2,}", " ", _CTRL_RE.sub("", text)).strip()
    return clean, marks


def forward_marks(marks):
    """指令只给桌面那只演，QQ 那边一个字都不留。"""
    if not marks:
        return
    for m in marks:
        try:
            bridge_send({"t": "act", "mark": m})
        except Exception:
            pass
    log("qq ctrl intercepted: " + ",".join(marks))


def clean_reply(text):
    """去掉括号动作描写和内部指令，保证像真人说话；全是指令就是空消息。"""
    if not text:
        return text
    t = re.sub(r"^\s*[（(][^）)]{0,60}[）)]\s*", "", text)
    t = re.sub(r"^\s*\*[^*]{1,40}\*\s*", "", t)
    t, _act = _act_pull(t)
    t, marks = split_ctrl(t)
    if marks:
        forward_marks(marks)
    return t.strip()


def last_user_text(messages):
    for m in reversed(messages):
        if m.get("role") == "user":
            c = m.get("content")
            if isinstance(c, list):
                return " ".join(p.get("text", "") for p in c if isinstance(p, dict))
            return c if isinstance(c, str) else ""
    return ""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _source(self):
        return "qq" if "/qq" in self.path else "pet"

    def _local_ok(self):
        # 只放行本机来源：挡网页 CSRF / DNS rebinding 对本机控制面的调用。
        h = (self.headers.get("Host") or "").strip().lower()
        if h.startswith("["):
            h = h.split("]")[0] + "]"
        elif ":" in h:
            h = h.rsplit(":", 1)[0]
        if h and h not in ("127.0.0.1", "localhost", "[::1]", "::1"):
            self._json(403, {"ok": False, "error": "forbidden host"})
            return False
        o = (self.headers.get("Origin") or "").strip().lower()
        if o and o not in ("http://127.0.0.1:8787", "http://localhost:8787",
                           "http://[::1]:8787", "null"):
            self._json(403, {"ok": False, "error": "forbidden origin"})
            return False
        return True

    def end_headers(self):
        # 许 setup.html 双击（file://）跨源调用本机接口：带上 CORS 头。
        try:
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        except Exception:
            pass
        BaseHTTPRequestHandler.end_headers(self)

    def do_OPTIONS(self):
        self.send_response(204)
        self.end_headers()

    def do_GET(self):
        if not self._local_ok():
            return
        path = self.path.split("?")[0].rstrip("/")
        if path in ("/console", "/console.html"):
            try:
                with open(CONSOLE_FILE, "rb") as fh:
                    page = fh.read()
            except Exception as exc:
                self._json(500, {"ok": False, "error": repr(exc)})
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.end_headers()
            self.wfile.write(page)
            return
        if path in ("/emotion", "/emotion.html"):
            try:
                with open(EMOTION_PAGE, "rb") as fh:
                    page = fh.read()
            except Exception as exc:
                self._json(500, {"ok": False, "error": repr(exc)})
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.end_headers()
            self.wfile.write(page)
            return
        if path in ("/setup", "/setup.html"):
            try:
                with open(SETUP_PAGE, "rb") as fh:
                    page = fh.read()
            except Exception as exc:
                self._json(500, {"ok": False, "error": repr(exc)})
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.end_headers()
            self.wfile.write(page)
            return
        if path in ("/credits", "/credits.html"):
            try:
                with open(CREDITS_PAGE, "rb") as fh:
                    page = fh.read()
            except Exception as exc:
                self._json(500, {"ok": False, "error": repr(exc)})
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.end_headers()
            self.wfile.write(page)
            return
        if path.startswith("/assets/"):
            rel = path[len("/assets/"):]
            if (not rel) or (".." in rel) or rel.startswith("/") or ("\\" in rel):
                self._json(404, {"ok": False, "error": "bad asset"})
                return
            ctype = {
                ".webp": "image/webp",
                ".png": "image/png",
                ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg",
                ".ico": "image/x-icon",
                ".css": "text/css; charset=utf-8",
                ".svg": "image/svg+xml",
            }.get(os.path.splitext(rel)[1].lower())
            if not ctype:
                self._json(404, {"ok": False, "error": "bad type"})
                return
            asset_path = os.path.join(BASE, "assets", *rel.split("/"))
            try:
                with open(asset_path, "rb") as fh:
                    blob = fh.read()
            except Exception:
                self._json(404, {"ok": False, "error": "not found"})
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(blob)))
            self.send_header("Cache-Control", "public, max-age=86400")
            self.end_headers()
            self.wfile.write(blob)
            return
        if path == "/voice" or path.startswith("/voice/"):
            if not voice_call_on():
                self._json(404, {"ok": False, "error": "voice_call disabled"})
                return
            try:
                import importlib
                vc = importlib.import_module("voice_call")
                vc.start()
                vc.handle(self)
            except Exception as exc:
                self._json(500, {"ok": False, "error": repr(exc)[:120]})
            return
        if path == "/api/console/audit":
            self._json(200, {"ok": True, "audit": audit_self()})
            return
        if path == "/api/console/probe":
            self._json(200, {"ok": True, "probe": probe_paths()})
            return
        if path == "/api/console/state":
            self._state()
            return
        if path == "/api/console/emotion":
            try:
                self._json(200, EMO.snapshot())
            except Exception as exc:
                self._json(500, {"ok": False, "error": repr(exc)})
            return
        if path == "/api/console/usage":
            self._json(200, usage_view())
            return
        if path == "/api/console/hands":
            self._json(200, {"ok": True, "config": hands_conf(),
                             "writable": sorted(HAND_WRITABLE),
                             "restartHint": "改其余项需编辑 hands.json 并重启大脑服务 proxy.py；重启鱼搭子 exe 无效",
                             "replyPort": bool(_pid_on(8778))})
            return
        if path == "/api/console/pet":
            self._json(200, pet_conf_view())
            return
        if path == "/api/console/diary":
            try:
                import importlib
                _dd = importlib.import_module("core.diary")
                _rows = _dd.paragraphs(with_kw=True)
                _items = [{"date": _d, "time": _t, "body": _b, "kw": _k}
                          for _d, _t, _b, _k in _rows]
                _items.reverse()
                self._json(200, {"ok": True, "count": len(_items), "items": _items})
            except Exception as exc:
                self._json(500, {"ok": False, "error": repr(exc)[:160]})
            return
        if path == "/api/pet/say":
            import urllib.parse
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            text = (q.get("text") or [""])[0].strip()
            if not text:
                self._json(400, {"ok": False, "error": "text?"})
                return
            sent = bridge_send({"t": "say", "id": "test%d" % int(time.time() * 1000),
                                "beats": coo_beats(body_script(text, ""))})
            self._json(200, {"ok": True, "sent": sent, "coo": COO["port"]})
            return
        if path == "/api/console/persona":
            import urllib.parse
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            scope = (q.get("scope") or ["owner"])[0]
            pid = (q.get("id") or [persona_active_id(scope)])[0]
            path_p = persona_path(pid, scope)
            try:
                with open(path_p, "r", encoding="utf-8") as fh:
                    self._json(200, {"ok": True, "id": pid, "text": fh.read()})
            except Exception as exc:
                self._json(404, {"ok": False, "error": repr(exc)[:120]})
            return
        if path == "/api/qq/pending":
            self._json(200, {"ok": True, "qq": qq_state(), "notes": notes_state()[-40:]})
            return
        if path == "/api/sticker/find":
            import urllib.parse
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            tags = (q.get("tags") or [""])[0]
            src = (q.get("source") or [None])[0] or None
            try:
                n = int((q.get("n") or ["5"])[0])
            except Exception:
                n = 5
            self._json(200, {"ok": True, "stats": sticker_state(),
                             "items": sticker.find(tags, n, src) if sticker else []})
            return
        if path == "/health":
            channels = {}
            for name in ("pet", "qq"):
                sid, seeded = _load_session(name)
                channels[name] = {"brain": brain_for(name), "session": sid[-12:] if sid else "",
                                  "seeded": seeded, "busy": agent_lock(name).locked()}
            mode = load_mode()
            self._json(200, {"ok": True, "memory": (MEM.stats()["total"] if MEM else 0),
                             "longterm": (MEM.stats()["scopes"].get("private", 0) if MEM else 0),
                             "pool": (MEM.stats() if MEM else {}),
                             "watch": watch_active(),
                             "safe_mode": {"enabled": mode["enabled"], "owners": len(mode["owner"])},
                             "channels": channels})
        elif path.endswith("/learn"):
            body = {"ok": True}
            if learn is not None:
                try:
                    body["profile"] = learn.report()
                    body["skills"] = learn.skill_report()
                except Exception as exc:
                    body["error"] = repr(exc)
            self._json(200, body)
        elif path.endswith("/models"):
            self._json(200, {
                "object": "list",
                "data": [
                    {"id": "deepseek-chat", "object": "model", "owned_by": "deepseek"},
                    {"id": "deepseek-reasoner", "object": "model", "owned_by": "deepseek"},
                ],
            })
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        try:
            return self._do_post()
        except Exception as exc:
            log("do_POST crashed: " + traceback.format_exc())
            if getattr(self, "_responded", False):
                return
            try:
                self._json(200, {"id": "err-" + str(int(time.time())),
                                 "object": "chat.completion",
                                 "choices": [{"index": 0,
                                              "message": {"role": "assistant",
                                                          "content": "我这会儿有点卡，稍等一下下～"},
                                              "finish_reason": "error"}],
                                 "usage": {}})
            except Exception:
                pass

    def _do_post(self):
        if not self._local_ok():
            return
        path = self.path.split("?")[0].rstrip("/")
        if path.startswith("/api/console/"):
            self._console(path.rsplit("/", 1)[-1])
            return
        if not path.endswith("/chat/completions"):
            self._json(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length", "0"))
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception as exc:
            self._json(400, {"error": f"bad json: {exc}"})
            return

        source = self._source()
        emit_event("thinking")
        stream = bool(payload.get("stream"))
        if not master_on():
            emit_event("idle")
            log("master off -> skip %s" % source)
            self._say_text("我在旁边歇着，去控制台叫我就好。", stream, "idle")
            return
        user_text = last_user_text(payload.get("messages", []))
        # 工具调用之后那一轮，最后一条是 role=tool，标记在前面的 user 消息里，得往回找
        if not MARK_RE.search(user_text):
            for m in reversed(payload.get("messages") or []):
                c = m.get("content") if isinstance(m, dict) else None
                if isinstance(c, str) and MARK_RE.search(c):
                    user_text = c
                    break
        uid, nick, trusted, gid = parse_marker(user_text)
        payload["_group"] = gid
        if not trusted:
            log("marker rejected: unsigned/spoofed tag in qq message -> treated as visitor")
        user_text = strip_marker(user_text)
        for m in payload.get("messages", []):
            c = m.get("content")
            if isinstance(c, str):
                m["content"] = strip_marker(c)
            elif isinstance(c, list):
                for part in c:
                    if isinstance(part, dict) and isinstance(part.get("text"), str):
                        part["text"] = strip_marker(part["text"])
        if source == "qq" and uid and sticker is not None and mod_on("qq_collect") and access_level(uid) == "owner":
            for m in payload.get("messages", []):
                c = m.get("content")
                if not isinstance(c, list):
                    continue
                for part in c:
                    if not isinstance(part, dict):
                        continue
                    u = (part.get("image_url") or {}).get("url") if isinstance(part.get("image_url"), dict) else None
                    if isinstance(u, str) and (u.startswith("http") or u.startswith("data:image/")):
                        _sticker_enqueue(u)
        mode = load_mode()
        people = mode.get("people") or {}
        if source == "qq" and not mod_on("chat"):
            emit_event("idle")
            log("qq chat module off")
            self._say_text("我歇一会儿呀，等下再聊。", stream, "off")
            return
        level = access_level(uid) if source == "qq" else "owner"
        try:
            if EMO.enabled():
                payload["_heart"] = ""  # 情绪模块接管语气，不再白算旧心情
            else:
                payload["_heart"] = heart_on_turn(level, gid, user_text)
        except Exception as exc:
            payload["_heart"] = ""
            log("heart failed: %r" % (exc,))
        try:
            payload["_emotion"] = EMO.on_turn(user_text) if (level == "owner" and not gid) else ""
        except Exception as exc:
            payload["_emotion"] = ""
            log("emotion failed: %r" % (exc,))
        if source == "qq" and level == "quiet":
            emit_event("idle")
            log("qq quiet %s" % uid)
            self._say_text("……我现在不想说话。", stream, "quiet")
            return
        if source == "qq" and not has_tool_turn(payload):
            dcfg = debounce_cfg()
            if dcfg.get("on", True):
                dkey = "%s|%s" % (uid or "-", gid or "")
                dticket = debounce_ticket(dkey)
                self._dbg_key, self._dbg_ticket = dkey, dticket
                if not debounce_wait(dkey, dticket, dcfg):
                    log(f"qq debounce {dkey} #{dticket} superseded -> 已读不回")
                    emit_event("idle")
                    self._say_empty(stream)
                    return
                if not (user_text or "").strip() and has_image_part(payload):
                    r = random.random()
                    if r < float(dcfg.get("sticker_ignore") or 0.5):
                        log(f"qq sticker-only -> 已读不回 (r={r:.2f})")
                        emit_event("idle")
                        self._say_empty(stream)
                        return
                if not gid:
                    think = random.uniform(float(dcfg.get("think_min") or 1.0),
                                           float(dcfg.get("think_max") or 2.0))
                    log(f"qq think {think:.1f}s")
                    time.sleep(think)
        if source == "qq" and level in ("visitor", "family") and not gid:
            who = nick or ("姐姐" if level == "family" else "同学")
            reply_text = student_reply(payload, who, uid, level) or "我这会儿有点卡，等下再聊呀。"
            try:                     # 访客/家人也要连珠炮，AstrBot 按行分段发
                import services.qq_service as _qqs
                reply_text = _qqs.shape(reply_text) or reply_text
            except Exception as _exc:
                log("student shape failed: %r" % (_exc,))
            emit_event("idle")
            log(f"qq {level}({uid or '?'}/{nick or '-'}) -> {len(reply_text)} chars")
            if stream:
                self._sse_text(reply_text)
            else:
                self._json(200, {
                    "id": "stu-" + str(int(time.time())),
                    "object": "chat.completion",
                    "choices": [{"index": 0,
                                 "message": {"role": "assistant", "content": reply_text},
                                 "finish_reason": "stop"}],
                    "usage": {},
                })
            return
        if wants_body(payload):
            self._body_bridge(payload, stream)
            return
        watch_on = ("允许你看屏幕", "开始看屏幕", "持续看屏幕", "一直看着", "你可以看屏幕")
        watch_off = ("别看了", "停止看屏幕", "不要看了", "关掉屏幕")
        if any(k in user_text for k in watch_on):
            set_watch()
            log("watch enabled for 1h")
        if any(k in user_text for k in watch_off):
            clear_watch()
            log("watch disabled")
        if gid:
            n_pic = flatten_group_images(payload.get("messages", []))
            if n_pic:
                log("group sticker x%d -> 只当情绪，不解读" % n_pic)
        # ---- 精准提醒：主人说「几点提醒我啥」，模型只抽时间，到点由 reminder 线程发 QQ ----
        if (REM is not None and source == "qq" and not gid and level == "owner"
                and not has_tool_turn(payload) and REMIND_HINT.search(user_text or "")):
            got = None
            try:
                got = reminder_try(user_text, uid)
            except Exception as exc:
                log("reminder parse crashed: %r" % (exc,))
            if got:
                reply_text, rst = got
                emit_event("idle")
                log("reminder %s uid=%s -> %d chars" % (rst, uid, len(reply_text)))
                record(user_text, reply_text, source, gid, no_mem=bool(payload.get("_voice")))
                if stream:
                    self._sse_text(reply_text)
                else:
                    self._json(200, {
                        "id": "rem-" + str(int(time.time())),
                        "object": "chat.completion",
                        "choices": [{"index": 0,
                                     "message": {"role": "assistant", "content": reply_text},
                                     "finish_reason": "stop"}],
                        "usage": {},
                    })
                return
        # ---- DSH 接管：默认把这句话交给 DSH agent 会话，失败才回退 DeepSeek ----
        # 只认真正的图片 part；AstrBot 会把纯文本也包成 list，不能一律当图
        has_image = any(
            isinstance(m.get("content"), list)
            and any(isinstance(part, dict) and part.get("type") == "image_url" for part in m["content"])
            for m in payload.get("messages", [])
        )
        if (brain_for(source) == "dsh" and user_text.strip() and not gid
                and not has_image and not SCREEN_RE.search(user_text)):
            emit_event("working")
            reply_text = ask_agent(source, user_text, uid)
            if reply_text:
                if uid and VOICE_TAG in reply_text:
                    reply_text = maybe_voice(reply_text, uid, gid)
                record(user_text, reply_text, source, gid, no_mem=bool(payload.get("_voice")))
                _act_emit(uid, gid, trusted, level, source)
                emit_event("idle")
                log(f"{source} via dsh -> {len(reply_text)} chars")
                if stream:
                    self._sse_text(reply_text)
                else:
                    self._json(200, {
                        "id": "dsh-" + str(int(time.time())),
                        "object": "chat.completion",
                        "choices": [{"index": 0,
                                     "message": {"role": "assistant", "content": reply_text},
                                     "finish_reason": "stop"}],
                        "usage": {},
                    })
                return
            emit_event("thinking")
            log("dsh path failed -> deepseek fallback")
        elif brain_for(source) == "dsh" and user_text.strip():
            log(f"dsh skipped image={has_image} screen={bool(SCREEN_RE.search(user_text))}")

        self._uid = uid
        self._gid = gid
        _bili_watch_kick(user_text)
        payload["messages"] = build_messages(payload, source)
        payload["model"] = MODEL
        if source == "qq":
            # 闲聊：温度 0.8 让它有点随机；群里放到 6～10 句，私聊仍压到一两句
            payload["temperature"] = 0.8
            payload["max_tokens"] = 300 if gid else 240
        if payload.get("tools"):
            # 带工具的这一轮要先「想」再决定调不调，300 token 不够用会被截断成空话
            payload["max_tokens"] = 1500
            payload["temperature"] = 0.3
        if source != "qq":
            remember_external_image(payload["messages"])
        if maybe_attach_screen(payload["messages"]):
            log("screen attached this turn")
        self._img_n = normalize_images(payload["messages"])
        if self._img_n:
            # 视觉轮：deepseek-v4.1 不开思考会先把预算全花在 reasoning 上，
            # 实测 max_tokens=300 时返回空串（4/4 空），加到 600 仍然空（reasoning 1071 字）。
            # enable_thinking=False 后 37 token / 1.9 s 出结果，比 max_tokens=2000 的
            # 670 token / 6.3 s 省十几倍。预算仍留 400 给描述用。
            payload["max_tokens"] = max(int(payload.get("max_tokens") or 0), 400)
            payload["enable_thinking"] = False
        self._retry_ctx = (payload, self._img_n, source, user_text)
        self._retried = False
        log(f"req src={source} stream={stream} model={payload.get('model')}")

        try:
            resp = post_chat(payload, timeout=180,
                             accept="text/event-stream" if stream else "application/json")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:400]
            log(f"upstream {exc.code}: {detail}")
            self._json(exc.code, {"error": {"message": detail}})
            return
        except Exception as exc:
            log(f"upstream error: {exc}")
            self._json(502, {"error": {"message": str(exc)}})
            return

        if stream:
            self._stream(resp, source, user_text)
        else:
            self._plain(resp, source, user_text)

    # ---------- 桌宠身体桥（实现在 services/pet_service.py）----------
    def _body_bridge(self, payload, stream):
        return petsvc.body_bridge(self, payload, stream)

    def _body_reply(self, text, stream, tool):
        return petsvc.body_reply(self, text, stream, tool)

    # ---------- QQ / 通用回复（实现在 services/qq_service.py）----------
    def _plain(self, resp, source, user_text):
        return qqsvc.plain(self, resp, source, user_text)

    def _stream(self, resp, source, user_text):
        return qqsvc.stream(self, resp, source, user_text)

    def _say_empty(self, stream):
        """这一条选择不回：空回复，AstrBot 那边不会发出气泡。"""
        if stream:
            self._sse_text("")
            return
        self._json(200, {"id": "skip-" + str(int(time.time())),
                         "object": "chat.completion",
                         "choices": [{"index": 0,
                                      "message": {"role": "assistant", "content": ""},
                                      "finish_reason": "stop"}],
                         "usage": {}})

    def _sse_text(self, text):
        """非流式的大脑（DSH）也要回成 SSE，桌宠/QQ 才吃得下。"""
        k = getattr(self, "_dbg_key", None)
        t = getattr(self, "_dbg_ticket", None)
        if k is not None and t is not None and text and debounce_latest(k) > t:
            log("qq debounce %s #%d superseded before send -> drop" % (k, t))
            emit_event("idle")
            text = ""
        self._responded = True
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        cid = "dsh-" + str(int(time.time()))
        try:
            first = {"id": cid, "object": "chat.completion.chunk",
                     "choices": [{"index": 0, "delta": {"role": "assistant", "content": text},
                                  "finish_reason": None}]}
            last = {"id": cid, "object": "chat.completion.chunk",
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
            self.wfile.write(b"data: " + json.dumps(first, ensure_ascii=False).encode("utf-8") + b"\n\n")
            self.wfile.write(b"data: " + json.dumps(last, ensure_ascii=False).encode("utf-8") + b"\n\n")
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _say_text(self, text, stream, tag="say"):
        if stream:
            self._sse_text(text)
        else:
            self._json(200, {"id": tag + "-" + str(int(time.time())), "object": "chat.completion",
                             "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                                          "finish_reason": "stop"}], "usage": {}})

    def _state(self):
        mode = load_mode()
        people = [{"qq": u, "note": i.get("note", ""), "mode": i.get("mode", "visitor")}
                  for u, i in people_state().items()]
        people.sort(key=lambda p: (p["mode"] != "owner", p["qq"]))
        self._json(200, {"ok": True, "master": {"enabled": master_on()},
                         "safemode": {"enabled": mode["enabled"], "policy": mode["policy"],
                                      "policies": list(POLICIES)},
                         "qq": qq_state(), "notes": notes_state(),
                         "sticker": sticker_state(),
                         "modules": modules_state(), "people": people,
                         "memory": (MEM.stats() if MEM else {}),
                         "longterm": (MEM.stats()["scopes"].get("private", 0) if MEM else 0),
                         "persona": {"active": persona_active_id(), "cards": persona_cards(),
                                     "visitor": {"active": persona_active_id("visitor"),
                                                 "cards": persona_cards("visitor")},
                                     "family": {"active": persona_active_id("family"),
                                                "cards": persona_cards("family")}},
                         "pet": pet_state(), "apps": apps_state(), "todo": todo_view(),
                         "paths": _paths(),
                         "hands": (handsvc.state() if HANDS else {"起没起": False})})

    def _console(self, action):
        try:
            n = int(self.headers.get("Content-Length", "0") or 0)
            data = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
        except Exception as exc:
            self._json(400, {"ok": False, "error": "bad json: %s" % exc})
            return
        try:
            if action == "probe":
                self._json(200, {"ok": True, "probe": probe_paths()})
                return
            elif action == "paths":
                out = save_paths(data.get("paths") or data)
                self._json(200 if out.get("ok") else 400, out)
                return
            elif action == "key":
                out = save_key(data.get("key"))
                self._json(200 if out.get("ok") else 400, out)
                return
            if action == "master":
                set_master(bool(data.get("enabled")))
                log("console master -> %s" % bool(data.get("enabled")))
            elif action == "safemode":
                # 老开关保留：开 = 访客模式，关 = 主人模式
                save_policy("visitor" if data.get("enabled") else "owner")
                log("console safemode -> %s" % bool(data.get("enabled")))
            elif action == "module":
                mid = str(data.get("id") or "")
                mods = modules_state()
                hit = None
                for m in mods:
                    if m["id"] == mid:
                        m["on"] = bool(data.get("on"))
                        hit = m
                if hit is None:
                    self._json(404, {"ok": False, "error": "no such module"})
                    return
                save_modules(mods)
                if mid == "qq_friend" and hit["on"] and qqops is not None:
                    try:
                        qqops.start()
                    except Exception as exc:
                        log("qqops start failed %r" % (exc,))
                if mid == "emotion":
                    try:
                        _ep = os.path.join(BASE, "emotion-config.json")
                        try:
                            with open(_ep, "r", encoding="utf-8") as _f:
                                _ec = json.load(_f)
                        except Exception:
                            _ec = {}
                        if not isinstance(_ec, dict):
                            _ec = {}
                        _ec["enabled"] = bool(hit["on"])
                        with open(_ep, "w", encoding="utf-8") as _f:
                            json.dump(_ec, _f, ensure_ascii=False, indent=1)
                        log("emotion enabled -> %s" % hit["on"])
                    except Exception as _exc:
                        log("emotion cfg write failed: %r" % (_exc,))
                if mid == "voice_call" and not hit["on"]:
                    try:
                        import sys
                        vc = sys.modules.get("voice_call")
                        if vc is not None:
                            vc.stop()
                            log("voice_call stopped")
                    except Exception as exc:
                        log("voice_call stop failed %r" % (exc,))
                log("console module %s -> %s" % (mid, hit["on"]))
            elif action == "todo":
                do = str(data.get("do") or "")
                if REM is None:
                    self._json(503, {"ok": False, "error": "reminder 模块未加载"})
                    return
                if do == "add":
                    msg = str(data.get("message") or "").strip()
                    if not msg:
                        self._json(400, {"ok": False, "error": "message 不能空"})
                        return
                    try:
                        at = float(data.get("at"))
                    except Exception:
                        self._json(400, {"ok": False, "error": "at 要填 epoch 秒"})
                        return
                    if at <= time.time():
                        self._json(400, {"ok": False, "error": "时间要晚于现在"})
                        return
                    item = REM.add(at, msg, str(data.get("uid") or ""))
                    log("console todo add id=%s at=%s msg=%s" % (item["id"], item["at"], item["message"]))
                    self._json(200, {"ok": True, "item": item, "todo": todo_view()})
                    return
                if do == "cancel":
                    rid = str(data.get("id") or "")
                    ok = REM.cancel(rid)
                    log("console todo cancel id=%s -> %s" % (rid, ok))
                    self._json(200, {"ok": ok, "todo": todo_view()})
                    return
                self._json(400, {"ok": False, "error": "do 要是 add / cancel"})
                return
            elif action == "launch":
                what = str(data.get("what") or "")
                turn_on = bool(data.get("on", True))
                if what not in ("brain", "pet", "fish"):
                    self._json(400, {"ok": False, "error": "what 要是 brain / pet / fish"})
                    return
                if turn_on:
                    started = {"brain": launch_brain, "pet": launch_pet,
                               "fish": launch_fish}[what]()
                else:
                    started = {"brain": close_brain, "pet": close_pet,
                               "fish": close_fish}[what]()
                log("console %s %s -> %s" % ("on" if turn_on else "off", what, started or "-"))
                if started:
                    time.sleep(6.0 if what == "fish" else (4.0 if what == "pet" else 1.5))
                self._json(200, {"ok": True, "started": started, "apps": apps_state()})
                return
            elif action == "policy":
                pol = save_policy(str(data.get("policy") or ""))
                log("console policy -> %s" % pol)
            elif action == "persona":
                do = str(data.get("do") or "")
                pid = str(data.get("id") or "")
                scope = persona_scope(data.get("scope"))
                if do == "save":
                    set_persona(pid, str(data.get("text") or ""), scope)
                    log("console persona save %s/%s" % (scope, pid))
                elif do == "active":
                    set_persona(pid, None, scope)
                    log("console persona active %s/%s" % (scope, pid))
                elif do == "new":
                    pid = pid or ("card" + time.strftime("%m%d%H%M%S"))
                    if os.path.exists(persona_path(pid, scope)):
                        self._json(400, {"ok": False, "error": "已经有一张同名的"})
                        return
                    set_persona(pid, str(data.get("text") or "# 新的人格卡\n\n"), scope)
                    log("console persona new %s/%s" % (scope, pid))
                elif do == "del":
                    if pid == persona_active_id(scope):
                        self._json(400, {"ok": False, "error": "这张正在用，先切走再删"})
                        return
                    del_persona(pid, scope)
                    log("console persona del %s/%s" % (scope, pid))
                else:
                    self._json(400, {"ok": False, "error": "bad persona action"})
                    return
            elif action == "person":
                people = people_state()
                qq = str(data.get("qq") or "").strip()
                act = str(data.get("action") or "")
                if not qq.isdigit():
                    self._json(400, {"ok": False, "error": "bad qq"})
                    return
                if act == "add":
                    cur = people.get(qq, {"note": "", "mode": "visitor"})
                    people[qq] = {"note": str(data.get("note") or cur["note"]),
                                  "mode": norm_mode(data.get("mode"), cur["mode"])}
                elif act == "del":
                    people.pop(qq, None)
                elif act == "note":
                    people.setdefault(qq, {"note": "", "mode": "visitor"})["note"] = str(data.get("note") or "")
                elif act == "mode":
                    if norm_mode(data.get("mode"), "") not in PEOPLE_MODES:
                        self._json(400, {"ok": False, "error": "bad mode"})
                        return
                    people.setdefault(qq, {"note": "", "mode": "visitor"})["mode"] = norm_mode(data.get("mode"))
                else:
                    self._json(400, {"ok": False, "error": "bad action"})
                    return
                save_people(people)
                log("console person %s %s" % (act, qq))
            elif action == "quick":
                on = bool(data.get("on"))
                if on:
                    set_master(True)
                    save_policy("owner")
                else:
                    set_master(False)
                log("console quick -> %s" % on)
            elif action == "friend":
                if qqops is None:
                    self._json(503, {"ok": False, "error": "qqops 没加载"})
                    return
                do = str(data.get("do") or "list")
                flag = str(data.get("flag") or "")
                if do == "approve":
                    if not mod_on("qq_friend"):
                        self._json(409, {"ok": False, "error": "好友申请模块关着"})
                        return
                    ok, res = qqops.approve(flag, str(data.get("remark") or ""))
                    log("console friend approve %s -> %s" % (flag, ok))
                    if not ok:
                        self._json(502, {"ok": False, "error": "OneBot 没同意: " + json.dumps(res, ensure_ascii=False)[:160]})
                        return
                elif do == "reject":
                    qqops.reject(flag)
                    log("console friend reject %s" % flag)
                elif do != "list":
                    self._json(400, {"ok": False, "error": "bad friend action"})
                    return
            elif action == "note":
                if qqops is None:
                    self._json(503, {"ok": False, "error": "qqops 没加载"})
                    return
                do = str(data.get("do") or "add")
                if do == "add":
                    if not mod_on("notes"):
                        self._json(409, {"ok": False, "error": "做记录模块关着"})
                        return
                    item = qqops.note_add(str(data.get("text") or ""))
                    if item is None:
                        self._json(400, {"ok": False, "error": "空记录"})
                        return
                elif do == "del":
                    qqops.note_del(data.get("idx"))
                elif do != "list":
                    self._json(400, {"ok": False, "error": "bad note action"})
                    return
            elif action == "sticker":
                if sticker is None:
                    self._json(503, {"ok": False, "error": "sticker 没加载"})
                    return
                do = str(data.get("do") or "find")
                if do == "build":
                    res = sticker.build()
                    log("console sticker build %s" % res)
                elif do == "tag":
                    sid = str(data.get("id") or "")
                    tags = data.get("tags") or []
                    if isinstance(tags, str):
                        tags = [t for t in re.split(r"[\s,，、]+", tags) if t]
                    if not sticker.tag_add(sid, tags):
                        self._json(404, {"ok": False, "error": "没有这个 id"})
                        return
                elif do == "add":
                    if not mod_on("qq_collect"):
                        self._json(409, {"ok": False, "error": "收藏图片模块关着"})
                        return
                    rec = sticker.add_source(str(data.get("src") or ""), source=str(data.get("source") or "own"),
                                             tags=data.get("tags") or [])
                    log("console sticker add %s" % (rec.get("id") if rec else "?"))
                elif do != "find":
                    self._json(400, {"ok": False, "error": "bad sticker action"})
                    return
            elif action == "hands":
                key = str(data.get("key") or "")
                if key not in HAND_WRITABLE:
                    self._json(400, {"ok": False, "error": "only game is writable at runtime"})
                    return
                try:
                    import core.hands as _h
                    ok = _h.set_conf(key, bool(data.get("value")))
                except Exception as exc:
                    self._json(500, {"ok": False, "error": repr(exc)})
                    return
                if not ok:
                    self._json(409, {"ok": False, "error": "hands bridge not installed"})
                    return
                log("console hands %s -> %r" % (key, bool(data.get("value"))))
                self._json(200, {"ok": True, "config": hands_conf()})
                return
            elif action == "pet":
                key = str(data.get("key") or "")
                out = pet_conf_set(key, data.get("value"))
                self._json(200 if out.get("ok") else 400, out)
                return
            else:
                self._json(404, {"ok": False, "error": "unknown"})
                return
        except Exception as exc:
            self._json(500, {"ok": False, "error": repr(exc)})
            return
        self._state()

    def _json(self, code, obj):
        self._raw(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _raw(self, code, body):
        self._responded = True
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def reach_loop():
    """每五分钟算一次「她现在惦记什么」，影子模式下只记账。
    开关关着时直接睡过去：不做衰减/取材/写盘，空闲零开销（R24）。"""
    while True:
        try:
            import reach
            if not reach.config().get("enabled"):
                time.sleep(300.0)
                continue
            reach.tick()
        except Exception as e:
            log("reach tick failed: %r" % (e,))
        time.sleep(300.0)


import services.pet_service as petsvc
import services.qq_service as qqsvc
import core.hands as handsvc
import core.emotion as EMO  # 情绪状态机：9 维 + 亲密度 + 信任 + 两本账

TEST_MODE = os.environ.get("PROXY_TEST") == "1"
petsvc.install(globals())
qqsvc.install(globals())
HANDS = None if TEST_MODE else handsvc.install(globals())


def _auto_compress_memory():
    """启动时顺手检查记忆池：超过 150 条就把最旧的 50 条 chat 并成 1 条（脚本自己会先备份）。"""
    try:
        import runpy, sys as _sys
        p = os.path.join(BASE, "tools", "compress_memory.py")
        if not os.path.exists(p):
            return
        old_argv = _sys.argv
        _sys.argv = [p]
        try:
            runpy.run_path(p, run_name="__memcompress__")["main"]()
        finally:
            _sys.argv = old_argv
    except Exception as exc:
        log("memory compress failed: %r" % (exc,))


try:
    if not TEST_MODE:
        _bg(_auto_compress_memory)
except Exception:
    pass


if __name__ == "__main__":
    bridge_start()
    # 启动自检：只记日志，不阻断启动（真缺 key 时由 _post 抛 upstream key unavailable）
    if not os.path.exists(KEY_FILE):
        log("boot check: upstream key file missing -> fallback only")
    _fb = os.path.join(BASE, "key_asd.txt")
    if not os.path.exists(_fb):
        log("boot check: fallback key file missing -> no failover")
    if qqops is not None and mod_on("qq_friend"):
        try:
            qqops.start()
        except Exception as exc:
            log("qqops start failed %r" % (exc,))
    if REM is not None:
        try:
            log("reminders loaded pending=%d" % REM.start(reminder_send))
        except Exception as exc:
            log("reminder start failed %r" % (exc,))
    try:
        if sticker is not None:
            log("sticker stats %s" % json.dumps(sticker.stats(), ensure_ascii=False))
    except Exception as exc:
        log("sticker boot failed %r" % (exc,))
    threading.Thread(target=reach_loop, daemon=True).start()
    threading.Thread(target=heart_idle_loop, daemon=True).start()
    threading.Thread(target=bili_browse_loop, daemon=True).start()
    log("proxy starting v3")
    class _Srv(ThreadingHTTPServer):
        # Windows 下 SO_REUSEADDR 允许重复绑定同一端口，多个代理会互相抢连接；
        # 这里改用独占绑定，重复启动直接失败退出。
        allow_reuse_address = False

        def server_bind(self):
            try:
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            except Exception:
                pass
            super().server_bind()

    try:
        srv = _Srv(("127.0.0.1", 8787), Handler)
    except OSError as exc:
        log("8787 already in use, exit: %s" % exc)
        raise SystemExit(0)
    # 常用句先合成好：第一声也别让群里等十几秒
    if _tts is not None and mod_on("voice"):
        try:
            _tts.prewarm(["在呀，怎么啦", "嗯嗯", "好呀", "我在呢", "怎么啦",
                          "晚安，早点睡嘛", "可算回来啦", "别怕，我在"], tag="startup")
        except Exception as exc:
            log("tts prewarm failed: %r" % (exc,))
    srv.serve_forever()
