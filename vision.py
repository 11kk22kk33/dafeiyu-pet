# -*- coding: utf-8 -*-
"""第三模块 · 视觉：让她真的看一段视频，画面和声音都懂。

两条路：
- 短视频（≤90s 且 ≤12MB）：整段丢给 qwen3.5-omni-flash，一次同时听声音、看画面，最准。
- 长视频：画面用 ffmpeg 均匀抽帧交给 qwen3-vl-plus；声音转 16k 单声道切成 ≤110s，
  每段配一帧黑底视频再问 omni 转写（这个接口只吃 video_url，不吃 audio）。
看完写一份 md 笔记，并往长期记忆里记一条（她真的记得看过）。
"""
import base64
import json
import os
import re
import subprocess
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
KEY_FILE = os.path.join(BASE, "key_asd.txt")

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
CHAT = "https://ws-vxmnafoy3yn5nowy.cn-beijing.maas.aliyuncs.com/compatible-mode/v1/chat/completions"
OMNI = "qwen3.5-omni-flash"
VL_MODEL = "qwen3-vl-plus"
NOTE_DIR = os.path.join(BASE, "vision-notes")
STATE = os.path.join(BASE, "vision-state.json")
TMP = os.path.join(BASE, "vision-tmp")
WHOLE_MAX_SEC = 90
WHOLE_MAX_BYTES = 12 * 1024 * 1024
CHUNK_SEC = 110
_FF = None


def _key():
    with open(KEY_FILE, "r", encoding="utf-8") as f:
        return f.read().strip()


def ffmpeg():
    global _FF
    if _FF:
        return _FF
    cands = [os.environ.get("FFMPEG"),
             os.path.join(_bp("astrbot_bin", os.path.join("D:" + os.sep, "AstrBot", "bin")), "ffmpeg.exe")]
    for c in cands:
        if c and os.path.exists(c):
            _FF = c
            return c
    try:
        import imageio_ffmpeg
        _FF = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        _FF = "ffmpeg"
    return _FF


def run(args, timeout=240):
    p = subprocess.run([ffmpeg()] + args, capture_output=True, timeout=timeout)
    return p.returncode, (p.stdout or b"").decode("utf-8", "replace"), (p.stderr or b"").decode("utf-8", "replace")


def _post(body, timeout=300):
    import urllib.request
    req = urllib.request.Request(CHAT, data=json.dumps(body).encode("utf-8"),
                                 headers={"Authorization": "Bearer " + _key(),
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _text(resp):
    try:
        return (resp["choices"][0]["message"].get("content") or "").strip()
    except (KeyError, IndexError, TypeError):
        return ""


def _b64(path):
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode()


def mmss(sec):
    sec = int(max(0, float(sec or 0)))
    return "%d:%02d" % (sec // 60, sec % 60)


def probe(path):
    _, _, err = run(["-hide_banner", "-i", path], timeout=90)
    dur = 0.0
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", err)
    if m:
        dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    v = re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", err)
    return {"duration": round(dur, 1), "size": (v.group(1) + "x" + v.group(2)) if v else "",
            "audio": "Audio:" in err, "bytes": os.path.getsize(path)}


def scene_cells(path, dur, outdir, max_cells=10):
    """按真实剪辑点切格：64x36 低清扫全片，HSV 三通道差找切点，再在每格中间挑最清晰的一帧。

    借 dsh-video-storyboard 的做法：只看亮度会漏掉"同亮度换色"的切点；阈值按速率反推，
    免得快剪片子一个切点都找不到。
    """
    import numpy as np
    os.makedirs(outdir, exist_ok=True)
    fps = 6
    w, h = 64, 36
    limit = int(min(dur, 1200))
    p = subprocess.run([ffmpeg(), "-hide_banner", "-loglevel", "error", "-t", str(limit), "-i", path,
                        "-vf", "fps=%d,scale=%d:%d" % (fps, w, h), "-f", "rawvideo",
                        "-pix_fmt", "rgb24", "-"], capture_output=True, timeout=600)
    if p.returncode != 0 or not p.stdout:
        return []
    raw = np.frombuffer(p.stdout, dtype=np.uint8)
    n = raw.size // (w * h * 3)
    if n < 4:
        return []
    budget = (max_cells - 2) if (dur > limit + 5 and max_cells >= 4) else max_cells
    fr = raw[:n * w * h * 3].reshape(n, h, w, 3).astype(np.float32) / 255.0
    mx = fr.max(axis=3)
    mn = fr.min(axis=3)
    df = mx - mn
    sat = np.where(mx > 0, df / np.maximum(mx, 1e-6), 0.0)
    r_, g_, b_ = fr[:, :, :, 0], fr[:, :, :, 1], fr[:, :, :, 2]
    hue = np.zeros_like(mx)
    d = np.maximum(df, 1e-6)
    hue = np.where(mx == r_, ((g_ - b_) / d) % 6, np.where(mx == g_, (b_ - r_) / d + 2, (r_ - g_) / d + 4)) * 60.0
    hue = np.where(df < 1e-4, 0.0, hue)
    dd = np.zeros(n - 1, dtype=np.float32)
    for c in range(n - 1):
        dh = np.abs(hue[c + 1] - hue[c])
        dh = np.minimum(dh, 360.0 - dh) / 360.0
        ds = np.abs(sat[c + 1] - sat[c])
        dv = np.abs(mx[c + 1] - mx[c])
        dd[c] = float((dh.mean() + ds.mean() + dv.mean()) * 255.0)
    med = float(np.median(dd)) if dd.size else 0.0
    thr = max(10.0, med + 4 * float(np.median(np.abs(dd - med)) or 0.0))
    m = len(dd)
    cand = [i for i in range(1, m - 1) if dd[i] >= thr and dd[i] >= dd[i - 1] and dd[i] >= dd[i + 1]]
    gap = max(1, int(1.2 * fps))
    cuts = []
    for i in sorted(cand, key=lambda k: -dd[k]):
        t = i / float(fps)
        if all(abs(t - c) >= gap / float(fps) for c in cuts):
            cuts.append(t)
        if len(cuts) >= budget - 1:
            break
    cuts.sort()
    scan_end = min(dur, float(limit))
    bounds = [0.0] + cuts + [scan_end]
    cells = []
    for i in range(len(bounds) - 1):
        a, b = bounds[i], bounds[i + 1]
        if b - a < 0.3 or len(cells) >= budget:
            continue
        mid = (a + b) / 2.0
        best = None
        for k, dt in enumerate((-0.12, 0.0, 0.12)):
            t = min(max(mid + dt, a + 0.05), b - 0.05)
            p = os.path.join(outdir, "c%02d_%d.jpg" % (i, k))
            rc, _, _ = run(["-hide_banner", "-loglevel", "error", "-ss", "%.2f" % t, "-i", path,
                            "-frames:v", "1", "-vf", "scale='min(720,iw)':-2", "-q:v", "4", "-y", p], timeout=90)
            if rc == 0 and os.path.exists(p) and os.path.getsize(p) > 800:
                try:
                    from PIL import Image
                    gimg = np.asarray(Image.open(p).convert("L"), dtype=np.float32)
                    lap = np.abs(4 * gimg[1:-1, 1:-1] - gimg[:-2, 1:-1] - gimg[2:, 1:-1] - gimg[1:-1, :-2] - gimg[1:-1, 2:])
                    sharp = float(lap.var())
                except Exception:
                    sharp = 0.0
                if best is None or sharp > best[2]:
                    best = (t, p, sharp)
        if best:
            cells.append((best[0], best[1]))
    if dur > scan_end + 5 and len(cells) < max_cells:
        tail_n = max(1, max_cells - len(cells) - 1)
        for k in range(tail_n):
            t = scan_end + (dur - scan_end) * (k + 0.5) / tail_n
            p = os.path.join(outdir, "t%02d.jpg" % k)
            rc, _, _ = run(["-hide_banner", "-loglevel", "error", "-ss", "%.2f" % t, "-i", path,
                            "-frames:v", "1", "-vf", "scale='min(720,iw)':-2", "-q:v", "4", "-y", p], timeout=120)
            if rc == 0 and os.path.exists(p) and os.path.getsize(p) > 800:
                cells.append((t, p))
    return cells[:max_cells]


def frames(path, n, dur, outdir):
    os.makedirs(outdir, exist_ok=True)
    out = []
    n = max(2, min(int(n), 12))
    for i in range(n):
        t = dur * (i + 0.5) / n
        p = os.path.join(outdir, "f%02d.jpg" % i)
        rc, _, _ = run(["-hide_banner", "-loglevel", "error", "-ss", "%.2f" % t, "-i", path,
                        "-frames:v", "1", "-vf", "scale='min(720,iw)':-2", "-q:v", "4", "-y", p], timeout=90)
        if rc == 0 and os.path.exists(p) and os.path.getsize(p) > 800:
            out.append((t, p))
    return out


def vl_note(shots):
    out = []
    for i in range(0, len(shots), 6):
        batch = shots[i:i + 6]
        content = [{"type": "text", "text":
                    "这是一段视频里按时间顺序截的 %d 帧。逐帧描述（每帧一两句）：画面里发生了什么、"
                    "人物在做什么、屏幕上的文字/字幕/界面字样原样抄下来。" % len(batch)}]
        for t, p in batch:
            content.append({"type": "text", "text": "[%s]" % mmss(t)})
            content.append({"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + _b64(p)}})
        content.append({"type": "text", "text": "最后单独一行：这段视频整体在讲什么（一句话）。"})
        try:
            txt = _text(_post({"model": VL_MODEL, "messages": [{"role": "user", "content": content}],
                               "max_tokens": 900}, timeout=300))
        except Exception as e:
            txt = "（画面没看成：%s）" % repr(e)[:120]
        out.append(txt)
    return "\n".join(x for x in out if x)


def omni_video(mp4, prompt, timeout=300, model=None):
    content = [{"type": "video_url", "video_url": {"url": "data:video/mp4;base64," + _b64(mp4)}},
               {"type": "text", "text": prompt}]
    return _text(_post({"model": model or OMNI, "messages": [{"role": "user", "content": content}],
                        "max_tokens": 1200}, timeout=timeout))


def whole_watch(path):
    return omni_video(path,
                      "你在看一段视频。用中文回答两件事：一、按时间顺序说画面里发生了什么，"
                      "屏幕上的字（字幕、标题、界面文字）原样抄下来；二、复述里面的人说的话。"
                      "不要评价，不要漏掉字。", timeout=420)


def _extract_wav(path, max_sec, outdir):
    os.makedirs(outdir, exist_ok=True)
    wav = os.path.join(outdir, "a.wav")
    rc, _, err = run(["-hide_banner", "-loglevel", "error", "-i", path, "-t", str(int(max_sec)),
                      "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", "-y", wav], timeout=600)
    if rc != 0 or not os.path.exists(wav):
        return None, err[-200:]
    return wav, ""


def transcribe(wav, max_sec, outdir):
    """这个接口只吃 video_url，所以每段音频配一帧 320x240 黑底视频再问。"""
    os.makedirs(outdir, exist_ok=True)
    rc, _, err = run(["-hide_banner", "-loglevel", "error", "-i", wav, "-f", "segment",
                      "-segment_time", str(CHUNK_SEC), "-c", "copy", "-y",
                      os.path.join(outdir, "seg%03d.wav")], timeout=300)
    if rc != 0:
        return "", "切分失败：" + err[-120:]
    segs = sorted(f for f in os.listdir(outdir) if re.match(r"seg\d+\.wav$", f))
    parts, used = [], 0.0
    for i, s in enumerate(segs):
        if used >= max_sec:
            break
        src = os.path.join(outdir, s)
        blk = os.path.join(outdir, "blk%03d.mp4" % i)
        rc, _, _ = run(["-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                        "-i", "color=c=black:s=320x240:r=2", "-i", src, "-shortest",
                        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                        "-c:a", "aac", "-b:a", "64k", blk], timeout=300)
        if rc != 0 or not os.path.exists(blk):
            continue
        txt, last = "", ""
        for model in (OMNI, "qwen3-omni-flash"):
            for attempt in (0, 1):
                try:
                    txt = omni_video(blk, "只转写这段音频里说的话，原样输出，不要描述画面、不要总结。",
                                     timeout=180, model=model)
                    if txt:
                        break
                except Exception as exc:
                    last = repr(exc)[:110]
            if txt:
                break
        if txt:
            parts.append("[%s] %s" % (mmss(i * CHUNK_SEC), txt))
            used += CHUNK_SEC
        else:
            parts.append("[%s] （这段没听清：%s）" % (mmss(i * CHUNK_SEC), last or "空回复"))
        os.remove(blk)
    return "\n".join(parts), ("%d 段" % len(parts))


def watch(src, n=None, with_audio=True, max_audio=600, remember=True, quiet=False, force_split=False):
    if re.match(r"^https?://", src):
        return {"ok": False, "error": "先下载到本地再看（网络链接直读没做）"}
    if not os.path.exists(src):
        return {"ok": False, "error": "找不到文件：" + src}
    os.makedirs(TMP, exist_ok=True)
    for f in os.listdir(TMP):
        try:
            os.remove(os.path.join(TMP, f))
        except OSError:
            pass
    meta = probe(src)
    dur = meta["duration"] or 60.0
    small = dur <= WHOLE_MAX_SEC and meta["bytes"] <= WHOLE_MAX_BYTES
    if small and not force_split:
        try:
            txt = whole_watch(src)
            mode, vtext, atext, ainfo, nf = "整段", txt, "", "整段一起听", 0
        except Exception as e:
            small = False
            mode = "拆开"
    if not small or force_split:
        mode = mode if "mode" in dir() else "拆开"
        count = n or max(3, min(10, int(dur // 20) + 2))
        shots = []
        if not n:
            try:
                shots = scene_cells(src, dur, TMP, max_cells=count)
            except Exception:
                shots = []
        if not shots:
            shots = frames(src, count, dur, TMP)
        nf = len(shots)
        vtext = vl_note(shots) if shots else "（抽帧失败）"
        atext, ainfo = "", "跳过"
        if with_audio and meta["audio"]:
            wav, err = _extract_wav(src, max_audio, TMP)
            atext, ainfo = transcribe(wav, max_audio, TMP) if wav else ("", err)
    name = os.path.splitext(os.path.basename(src))[0]
    note = "# 视频笔记：%s\n\n- 时长 %s ｜ 画面 %s ｜ 模式 %s ｜ 帧 %d\n\n## 画面\n%s\n\n## 声音（%s）\n%s\n" % (
        name, mmss(dur), meta["size"] or "?", mode, nf, vtext,
        ainfo if not atext else "转写", atext or "（没人说话）")
    os.makedirs(NOTE_DIR, exist_ok=True)
    note_path = os.path.join(NOTE_DIR, name + ".md")
    with open(note_path, "w", encoding="utf-8") as f:
        f.write(note)
    result = {"ok": True, "path": src, "mode": mode, "duration": dur, "frames": nf,
              "visual": vtext, "audio": atext, "audio_info": ainfo, "note": note_path, "at": time.time()}
    try:
        with open(STATE, "w", encoding="utf-8") as f:
            json.dump({"path": src, "note": note_path, "at": result["at"],
                       "digest": note[:3000]}, f, ensure_ascii=False, indent=1)
    except OSError:
        pass
    if remember:
        try:
            import proxy
            gist = (vtext.splitlines()[-1] if vtext else "")[:70]
            proxy.lt_add("我看过主人的视频《%s》：%s" % (name[:20], gist), "event")
        except Exception as e:
            result["remember_error"] = repr(e)[:120]
    if not quiet:
        print(json.dumps({k: v for k, v in result.items() if k not in ("visual", "audio")}, ensure_ascii=False)[:700])
        print("--- 画面 ---\n" + vtext[:1400])
        if atext:
            print("--- 声音 ---\n" + atext[:800])
    return result


if __name__ == "__main__":
    a = sys.argv[1:]
    if not a:
        print("用法: python vision.py <视频文件> [--frames N] [--no-audio] [--max-audio 秒] [--split]")
        sys.exit(1)
    src = a[0]
    nf = int(a[a.index("--frames") + 1]) if "--frames" in a else None
    ma = int(a[a.index("--max-audio") + 1]) if "--max-audio" in a else 600
    watch(src, n=nf, with_audio="--no-audio" not in a, max_audio=ma, force_split="--split" in a)
