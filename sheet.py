# -*- coding: utf-8 -*-
"""sheet.py —— 用 dsh-video-storyboard 的宫格 + 看图模型，替我把视频“看”一遍。

思路：storyboard 负责切点、挑帧、量响度、出文字清单（我本来就能读），
图我自己看不见，就交给看图模型读那张宫格，再把两边拼成一份笔记。
"""
import json, os, re, shutil, subprocess, sys, time

import vision

HERE = os.path.dirname(os.path.abspath(__file__))
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
STORY = _bp("story_dir", os.path.join("D:" + os.sep, "大肥鱼的自留地", "03-脚本", "storyboard"))
BIN = os.path.join(STORY, "bin", "storyboard.mjs")
NODE = os.environ.get("SHEET_NODE") or os.path.join(
    os.environ.get("USERPROFILE", "C:" + os.sep), ".dsh", "dsh-runtimes",
    "dsh-primary-runtime", "dependencies", "node", "bin", "node.exe")
FFPROBE = _bp("ffprobe", os.path.join("D:" + os.sep, "大肥鱼的自留地", "03-脚本", "bin", "ffprobe.exe"))
OUT = os.path.join(HERE, "vision-tmp", "sheet.png")
MAN = os.path.join(HERE, "vision-tmp", "sheet.evidence.json")

LAYOUT_PROMPT = """这是一张视频分镜宫格图，每格左上角有格号，底部有音频条。请按格号顺序描述：
1) 每一格里到底画着什么（人物、场景、界面文字原样抄下来）；
2) 连起来讲了什么事；
3) 哪几格的字幕/标题最关键。
用中文，紧凑，不要客套话。"""


def _env():
    e = dict(os.environ)
    e["DSH_FFMPEG_PATH"] = vision.ffmpeg()
    e["DSH_FFPROBE_PATH"] = FFPROBE
    e["PYTHONIOENCODING"] = "utf-8"
    return e


def grid(src, cells=9, frm=0, to=0, transcript="off"):
    """跑 storyboard，返回 (清单文字, 宫格图路径)。"""
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    args = [NODE, BIN, src, "--cells", str(cells), "--out", OUT,
            "--manifest", MAN, "--text", "--transcript", transcript]
    if frm:
        args += ["--from", str(frm)]
    if to:
        args += ["--to", str(to)]
    p = subprocess.run(args, capture_output=True, cwd=STORY, env=_env(), timeout=900)
    out = p.stdout.decode("utf-8", "replace")
    err = p.stderr.decode("utf-8", "replace").strip()
    if not os.path.exists(OUT):
        raise RuntimeError("storyboard 没出图：" + (err[-400:] or out[-400:]))
    m = re.search(r"<storyboard[\s\S]*?</storyboard>", out)
    return (m.group(0) if m else out), OUT


def read_grid(png, prompt=None):
    body = {
        "model": vision.VL_MODEL,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt or LAYOUT_PROMPT},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + vision._b64(png)}},
        ]}],
        "temperature": 0.2,
    }
    return vision._text(vision._post(body, timeout=300))


def sheet(src, cells=9, frm=0, to=0, with_audio=True, max_audio=600, remember=True, quiet=False):
    if re.match(r"^https?://", src):
        return {"ok": False, "error": "先下载到本地再看"}
    if not os.path.exists(src):
        return {"ok": False, "error": "找不到文件：" + src}
    meta = vision.probe(src)
    dur = meta["duration"] or 0
    span = (to or dur) - frm
    if not to and dur > 1800:
        to = 1800
        span = to - frm
    n = cells or max(4, min(12, int(span // 30) + 4))
    t0 = time.time()
    man, png = grid(src, cells=n, frm=frm, to=to)
    vtext = read_grid(png)
    atext, ainfo = "", "跳过"
    if with_audio and meta["audio"]:
        wav, err = vision._extract_wav(src, max_audio, vision.TMP)
        if wav:
            atext, ainfo = vision.transcribe(wav, max_audio, vision.TMP)
        else:
            ainfo = err
    name = os.path.splitext(os.path.basename(src))[0]
    note = ("# 视频笔记：%s\n\n- 时长 %s ｜ 画面 %s ｜ 宫格 %d 格 ｜ 用时 %.0fs\n\n"
            "## 分镜清单（理性部分）\n%s\n\n## 画面（看图读出来的）\n%s\n\n## 声音（%s）\n%s\n") % (
        name, vision.mmss(dur), meta["size"] or "?", n, time.time() - t0,
        man.strip(), vtext, "转写" if atext else ainfo, atext or "（没人说话）")
    os.makedirs(vision.NOTE_DIR, exist_ok=True)
    note_path = os.path.join(vision.NOTE_DIR, name + ".md")
    with open(note_path, "w", encoding="utf-8") as f:
        f.write(note)
    res = {"ok": True, "path": src, "mode": "宫格", "duration": dur, "cells": n,
           "visual": vtext, "manifest": man, "audio": atext, "audio_info": ainfo,
           "note": note_path, "png": png, "at": time.time()}
    try:
        with open(vision.STATE, "w", encoding="utf-8") as f:
            json.dump({"path": src, "note": note_path, "at": res["at"],
                       "digest": note[:3000]}, f, ensure_ascii=False, indent=1)
    except OSError:
        pass
    if remember:
        try:
            import proxy
            gist = (vtext.splitlines()[-1] if vtext else "")[:70]
            proxy.lt_add("我看过主人的视频《%s》：%s" % (name[:20], gist), "event")
        except Exception as e:
            res["remember_error"] = repr(e)[:120]
    if not quiet:
        print(json.dumps({k: v for k, v in res.items() if k not in ("visual", "manifest", "audio")},
                         ensure_ascii=False)[:600])
        print("--- 清单 ---\n" + man.strip()[:1200])
        print("--- 画面 ---\n" + vtext[:1400])
        if atext:
            print("--- 声音 ---\n" + atext[:600])
    return res


if __name__ == "__main__":
    a = sys.argv[1:]
    if not a:
        print("用法: python sheet.py <视频> [--cells N] [--from 秒] [--to 秒] [--no-audio] [--max-audio 秒]")
        sys.exit(1)
    src = a[0]
    cells = int(a[a.index("--cells") + 1]) if "--cells" in a else 0
    frm = int(a[a.index("--from") + 1]) if "--from" in a else 0
    to = int(a[a.index("--to") + 1]) if "--to" in a else 0
    ma = int(a[a.index("--max-audio") + 1]) if "--max-audio" in a else 600
    sheet(src, cells=cells, frm=frm, to=to, with_audio="--no-audio" not in a, max_audio=ma)
