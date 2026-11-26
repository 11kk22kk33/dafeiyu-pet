# -*- coding: utf-8 -*-
"""用主人在 AstrBot 面板里设好的 edge-tts 音色念一句话，带语调起伏。

用法: python tts_edge.py "文本" "输出.mp3"

做法：按标点切成小句，每句单独合成，感叹句提高音量音调、省略号放慢、
疑问句上扬，句间用 ffmpeg 生成的静音补呼吸感，最后拼成一个 mp3。
音色/基础语速/音调全部读 cmd_config.json，主人改了面板就跟着变。
"""
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

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
CONFIG = _bp("astrbot_cmd_config", os.path.join("D:" + os.sep, "AstrBot", "AstrBot_4.29.1_windows_amd64_portable", "backend", "app", "data", "cmd_config.json"))
FFMPEG = os.path.join(_bp("astrbot_bin", os.path.join("D:" + os.sep, "AstrBot", "bin")), "ffmpeg.exe")

# 句末停顿（秒）：越「重」的标点停得越久
PAUSE = {"，": 0.18, "、": 0.14, "；": 0.26, "。": 0.34, "！": 0.28, "？": 0.34,
         "…": 0.5, "—": 0.45, ":": 0.2, ",": 0.16}

CLAUSE_RE = re.compile(r"[^，。！？；、…—,:!?]*(?:…+|——|[，。！？；、,:!?]|$)")


def conf():
    try:
        with open(CONFIG, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        return {}
    for p in cfg.get("provider") or []:
        if isinstance(p, dict) and p.get("type") == "edge_tts":
            return p
    return {}


def pct(v):
    try:
        return int(str(v).replace("%", "").replace("+", "").strip())
    except (TypeError, ValueError):
        return 0


def hz(v):
    try:
        return int(str(v).replace("Hz", "").replace("+", "").strip())
    except (TypeError, ValueError):
        return 0


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def clauses(text):
    """切小句，保留句末标点；太碎的并回去，避免一字一句。"""
    out = []
    for m in CLAUSE_RE.finditer(text):
        s = m.group(0).strip()
        if s:
            out.append(s)
    merged = []
    for s in out:
        if merged and len(merged[-1]) < 4 and merged[-1][-1] in "，、；,":
            merged[-1] += s
        else:
            merged.append(s)
    return merged or [text]


def mood(clause):
    """按句子情绪给 (语速增量%, 音调增量Hz, 音量增量%) 和句后停顿。"""
    tail = clause[-1] if clause else ""
    body = clause.rstrip("，。！？；、…—,:!?")
    rate, pitch, vol = 0, 0, 0
    if tail in "！!":
        rate, pitch, vol = 6, 10, 6
    elif tail in "？?":
        rate, pitch = 2, 12
    elif tail in "…—" or body.endswith("…"):
        rate, pitch = -10, -6
    elif body.endswith(("嘛", "咯", "呀", "哦", "啦")):
        rate, pitch = -4, 4
    elif body.endswith(("啊", "哎", "唉")):
        rate, pitch = -6, 2
    elif tail in "，、；,":
        rate = -2
    pause = PAUSE.get(tail, 0.18)
    if body.endswith(("…", "—")):
        pause = max(pause, 0.5)
    return rate, pitch, vol, pause


def silence(path, seconds):
    cmd = [FFMPEG, "-y", "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono",
           "-t", "%.2f" % seconds, "-c:a", "libmp3lame", "-b:a", "48k",
           "-ar", "24000", "-ac", "1", path]
    r = subprocess.run(cmd, capture_output=True, timeout=60)
    return r.returncode == 0 and os.path.exists(path)


def concat(parts, out, tmp):
    lst = os.path.join(tmp, "list.txt")
    with open(lst, "w", encoding="utf-8") as f:
        for p in parts:
            f.write("file '%s'\n" % p.replace("\\", "/"))
    r = subprocess.run([FFMPEG, "-y", "-f", "concat", "-safe", "0", "-i", lst,
                        "-c", "copy", out], capture_output=True, timeout=120)
    return r.returncode == 0 and os.path.exists(out) and os.path.getsize(out) > 1000


async def synth(text, voice, rate, volume, pitch, out):
    import edge_tts
    cm = edge_tts.Communicate(text, voice, rate=rate, volume=volume, pitch=pitch)
    await cm.save(out)


def main():
    if len(sys.argv) < 3:
        print("usage: tts_edge.py <text> <out.mp3>")
        return 2
    text, out = sys.argv[1].strip(), sys.argv[2]
    c = conf()
    voice = c.get("edge-tts-voice") or "zh-CN-XiaoyiNeural"
    base_rate, base_vol, base_pitch = pct(c.get("rate")), pct(c.get("volume")), hz(c.get("pitch"))

    parts = clauses(text)
    if not shutil.which(FFMPEG) and not os.path.exists(FFMPEG):
        asyncio.run(synth(text, voice, "%+d%%" % base_rate, "%+d%%" % base_vol, "%+dHz" % base_pitch, out))
        return 0 if os.path.getsize(out) > 1000 else 1

    tmp = tempfile.mkdtemp(prefix="tts_")
    pieces = []
    try:
        jobs = []
        for i, s in enumerate(parts):
            dr, dp, dv, pause = mood(s)
            jobs.append((i, s, os.path.join(tmp, "c%02d.mp3" % i),
                         "%+d%%" % clamp(base_rate + dr, -30, 30),
                         "%+d%%" % clamp(base_vol + dv, -20, 20),
                         "%+dHz" % clamp(base_pitch + dp, -20, 30), pause))

        async def batch():
            # 小句一起合成，别一句一句排队等，不然语音比文字慢半分钟
            await asyncio.gather(*[synth(s, voice, r, v, p, path)
                                   for _i, s, path, r, v, p, _pa in jobs],
                                 return_exceptions=True)

        asyncio.run(batch())
        for i, _s, seg, _r, _v, _p, pause in jobs:
            if os.path.exists(seg) and os.path.getsize(seg) > 500:
                pieces.append(seg)
            if i != len(parts) - 1:
                sp = os.path.join(tmp, "p%02d.mp3" % i)
                if silence(sp, pause):
                    pieces.append(sp)
        if not concat(pieces, out, tmp):
            asyncio.run(synth(text, voice, "%+d%%" % base_rate, "%+d%%" % base_vol,
                              "%+dHz" % base_pitch, out))
        if not (os.path.exists(out) and os.path.getsize(out) > 1000):
            print("empty output")
            return 1
        print("%s | %d 句 | %d bytes" % (voice, len(parts), os.path.getsize(out)))
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
