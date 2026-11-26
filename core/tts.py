# -*- coding: utf-8 -*-
"""语音服务：外面只说「念这一句」，这里负责叫起 tts_edge.py、兜底 SAPI、
缓存常用句、超时熔断。念不出来就返回 None，调用方退回文字，绝不能让 QQ 卡住等语音。

提速思路：
  1. 常用回复落盘缓存（voice-cache），第二次起直接复制文件，不再走网络；
  2. 启动时预热几句最常说的，第一声也不冷场；
  3. 真慢就熔断，走 SAPI 兜底，绝不阻塞主链路。
"""
import hashlib
import json
import os
import re
import shutil
import threading
import time

from . import guard

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
def _paths():
    """读 config-paths.json；缺文件/坏 JSON 都返回空 dict。"""
    try:
        with open(os.path.join(BASE, "config-paths.json"), "r", encoding="utf-8-sig") as f:
            return json.load(f) or {}
    except Exception:
        return {}


_PATHS = _paths()


def _bp(key, fallback):
    v = _PATHS.get(key)
    return v if isinstance(v, str) and v.strip() else fallback
ASTROBOT_PY = _bp("astrbot_py", os.path.join("D:" + os.sep, "AstrBot", "AstrBot_4.29.1_windows_amd64_portable",
                     "backend", "python", "python.exe"))
SCRIPT = os.path.join(BASE, "tts_edge.py")
VOICE_DIR = os.path.join(BASE, "voice")
CACHE_DIR = os.path.join(BASE, "voice-cache")
CACHE_MAX = 300
LOG = os.path.join(BASE, "core.log")


def _log(msg):
    try:
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write("%s tts: %s\n" % (time.strftime("%H:%M:%S"), msg))
    except Exception:
        pass


def _ok(path):
    try:
        return os.path.exists(path) and os.path.getsize(path) > 1000
    except OSError:
        return False


def _key(text, engine="edge"):
    norm = re.sub(r"\s+", "", text or "")
    return hashlib.sha1((engine + "|pre2|" + norm).encode("utf-8")).hexdigest()[:24]


def _cache_path(text, engine="edge"):
    return os.path.join(CACHE_DIR, "%s.mp3" % _key(text, engine))


def cached(text, engine="edge"):
    """缓存命中就返回缓存路径，否则 None。"""
    p = _cache_path(text, engine)
    return p if _ok(p) else None


def _remember(text, path, engine="edge"):
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        dst = _cache_path(text, engine)
        if os.path.abspath(path) != os.path.abspath(dst):
            shutil.copyfile(path, dst)
        _trim_cache()
    except OSError as exc:
        _log("缓存写入失败 %r" % (exc,))


def _trim_cache():
    try:
        files = [os.path.join(CACHE_DIR, f) for f in os.listdir(CACHE_DIR) if f.endswith(".mp3")]
    except OSError:
        return
    if len(files) <= CACHE_MAX:
        return
    files.sort(key=lambda p: os.path.getmtime(p))
    for p in files[:len(files) - CACHE_MAX]:
        try:
            os.remove(p)
        except OSError:
            pass
    _log("缓存清理到 %d 条" % CACHE_MAX)


def edge(text, out, timeout=75.0):
    """主人预设的 edge-tts 嗓子（在 AstrBot 面板里配的音色）。"""
    def run():
        import subprocess
        r = subprocess.run([ASTROBOT_PY, SCRIPT, text, out],
                           capture_output=True, timeout=timeout)
        if not _ok(out):
            raise RuntimeError("empty output: %r" % ((r.stderr or b"")[:120],))
        return out
    return guard.call("tts-edge", run, timeout=timeout + 5, default=None)


def sapi(text, out, timeout=60.0):
    """兜底：Windows SAPI，离线。"""
    def run():
        import subprocess
        tmp = os.path.join(os.environ.get("TEMP", BASE), "dfy_tts.txt")
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
        ps = ("Add-Type -AssemblyName System.Speech;"
              "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer;"
              "try { $s.SelectVoice('Microsoft Huihui Desktop') } catch {};"
              "$s.Rate = 1;"
              "$t = Get-Content -LiteralPath '%s' -Raw -Encoding UTF8;"
              "$s.SetOutputToWaveFile('%s'); $s.Speak($t); $s.Dispose()" % (tmp, out))
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                       capture_output=True, timeout=timeout)
        if not _ok(out):
            raise RuntimeError("sapi empty")
        return out
    return guard.call("tts-sapi", run, timeout=timeout + 5, default=None)


def speak(text, stamp=None):
    """念一句：缓存 -> edge -> SAPI。返回 (路径, 引擎) 或 (None, 原因)。
    stamp 是文件名前缀；给了就不复用，不给就是一次性文件。"""
    text = (text or "").strip()
    if not text:
        return None, "empty"
    os.makedirs(VOICE_DIR, exist_ok=True)
    stamp = int(stamp or time.time() * 1000)
    mp3 = os.path.join(VOICE_DIR, "v%d.mp3" % stamp)
    t0 = time.time()
    hit = cached(text, "edge")
    if hit:
        try:
            shutil.copyfile(hit, mp3)
            _log("缓存命中 %.0fms %s" % ((time.time() - t0) * 1000, text[:18]))
            return mp3, "cache"
        except OSError as exc:
            _log("缓存读取失败 %r" % (exc,))
    path = edge(text, mp3)
    if path:
        _remember(text, path, "edge")
        _log("edge 合成 %.0fms %s" % ((time.time() - t0) * 1000, text[:18]))
        return path, "edge"
    wav = os.path.join(VOICE_DIR, "v%d.wav" % stamp)
    path = sapi(text, wav)
    if path:
        _remember(text, path, "sapi")
        _log("sapi 兜底 %.0fms %s" % ((time.time() - t0) * 1000, text[:18]))
        return path, "sapi"
    return None, "failed"


def prewarm(lines, tag="common"):
    """后台把常用句先合成好，别让孩子们等第一声。"""
    def run():
        n = 0
        for text in lines or []:
            text = (text or "").strip()
            if not text or cached(text, "edge"):
                continue
            tmp = os.path.join(VOICE_DIR, "prewarm-%d.mp3" % int(time.time() * 1000))
            path, engine = speak(text, stamp=time.time() * 1000)
            if path and engine == "edge":
                n += 1
            try:
                if path and os.path.exists(path):
                    os.remove(path)
            except OSError:
                pass
        if n:
            _log("预热完成 %d 句（%s）" % (n, tag))
    th = threading.Thread(target=run, name="tts-prewarm", daemon=True)
    th.start()
    return th
