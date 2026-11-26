# -*- coding: utf-8 -*-
"""bili_fetch.py —— 把一个 B 站视频真的看一遍。

bvid/链接 -> 插件 API 拿单文件 mp4 直链（绕开 mcdn P2P）-> 下载到本地
-> 交给 sheet.py 抽帧+读图+转写 -> 写笔记
-> sheet.py 同时更新 vision-state.json，proxy 的 vision_block() 就会自动想起来。

跑一次就退出，不常驻。
"""
import asyncio, io, json, os, re, shutil, subprocess, sys, time, urllib.request

try:
    if sys.stdout is not None:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

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
PET = _bp("pet_shared", os.path.join("D:" + os.sep, "pet-shared"))
PY = _bp("astrbot_py", os.path.join("D:" + os.sep, "AstrBot", "AstrBot_4.29.1_windows_amd64_portable",
                     "backend", "python", "python.exe"))
PLUG = _bp("bili_plugin", os.path.join("D:" + os.sep, "AstrBot", "AstrBot_4.29.1_windows_amd64_portable",
                     "backend", "app", "data", "plugins"))
DATA = _bp("bili_data", os.path.join("D:" + os.sep, "AstrBot", "AstrBot_4.29.1_windows_amd64_portable",
                     "backend", "app", "data", "plugin_data", "astrbot_plugin_bilivideo"))
INCOMING = os.path.join(PET, "vision-tmp", "incoming")
SHEET = os.path.join(PET, "sheet.py")
MAX_SEC = 1800
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


def norm(s):
    s = (s or "").strip()
    m = re.search(r"(BV[0-9A-Za-z]{10})", s)
    if m:
        return m.group(1)
    m = re.search(r"b23\.tv/[A-Za-z0-9]+", s)
    if m:
        try:
            req = urllib.request.Request("https://" + m.group(0),
                                         headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=15) as r:
                final = r.geturl()
        except Exception:
            return ""
        m2 = re.search(r"(BV[0-9A-Za-z]{10})", final or "")
        if m2:
            return m2.group(1)
    return ""


async def _resolve(bv):
    if PLUG not in sys.path:
        sys.path.insert(0, PLUG)
    from astrbot_plugin_bilivideo.bilivideo.auth.cookies import CookieJar
    from astrbot_plugin_bilivideo.bilivideo.api.client import BilibiliHTTPClient
    from astrbot_plugin_bilivideo.bilivideo.api.wbi import sign_params
    from astrbot_plugin_bilivideo.bilivideo.core.constants import API_BASE

    ck = CookieJar(DATA)
    if not ck.is_logged_in():
        return None, "cookie 没登录态"
    async with BilibiliHTTPClient(ck.get()).lifespan() as c:
        v = await c.request_json("GET", API_BASE + "/x/web-interface/view",
                                 params={"bvid": bv}, expect_code_zero=False)
        if v.get("code") != 0:
            return None, "取视频信息失败 code=%s %s" % (v.get("code"), v.get("message"))
        d = v.get("data") or {}
        cid = d.get("cid")
        title = (d.get("title") or "").strip()
        dur = d.get("duration") or 0
        if not cid:
            return None, "没有 cid"
        if dur and dur > MAX_SEC:
            return None, "视频 %d 秒，超过 %d 秒不看" % (dur, MAX_SEC)
        p = {"bvid": bv, "cid": cid, "qn": 32, "fnval": 1, "fourk": 0,
             "platform": "html5", "high_quality": 1}
        sp = await sign_params(p, client=c)
        r = await c.request_json("GET", API_BASE + "/x/player/wbi/playurl",
                                 params=sp, expect_code_zero=False)
        if r.get("code") != 0:
            return None, "取播放地址失败 code=%s %s" % (r.get("code"), r.get("message"))
        durl = (r.get("data") or {}).get("durl") or []
        if not durl:
            return None, "没有可下的单文件流"
        return {"url": durl[0].get("url") or "", "title": title,
                "dur": dur, "size": durl[0].get("size") or 0}, "ok"


def download(bv):
    os.makedirs(INCOMING, exist_ok=True)
    info, status = asyncio.run(_resolve(bv))
    if not info:
        return None, status
    if not info["url"]:
        return None, "直链是空的"
    dst = os.path.join(INCOMING, bv + ".mp4")
    req = urllib.request.Request(info["url"], headers={
        "User-Agent": UA,
        "Referer": "https://www.bilibili.com/video/" + bv,
        "Origin": "https://www.bilibili.com",
    })
    try:
        with urllib.request.urlopen(req, timeout=180) as resp, open(dst, "wb") as f:
            shutil.copyfileobj(resp, f)
    except Exception as exc:
        return None, "下载中断 %r" % (exc,)
    if not os.path.exists(dst) or os.path.getsize(dst) < 1024:
        return None, "下载出来是空文件"
    return {"path": dst, "title": info["title"], "dur": info["dur"]}, "ok"


def retitle_note(title):
    """sheet.py 拿文件名当笔记标题，这里换成视频真名，方便以后认。"""
    if not title:
        return
    try:
        with open(os.path.join(PET, "vision-state.json"), "r", encoding="utf-8") as f:
            note = (json.load(f) or {}).get("note") or ""
        if not note or not os.path.exists(note):
            return
        with open(note, "r", encoding="utf-8") as f:
            lines = f.read().split("\n")
        if lines and lines[0].startswith("# 视频笔记"):
            lines[0] = "# 视频笔记：" + title
            with open(note, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
    except (OSError, ValueError):
        pass


def watch(path):
    p = subprocess.run([PY, SHEET, path], capture_output=True, timeout=2400)
    return (p.returncode,
            (p.stdout or b"").decode("utf-8", "replace"),
            (p.stderr or b"").decode("utf-8", "replace"))


def main():
    if len(sys.argv) < 2:
        print("用法: python bili_fetch.py <BV号 或链接> [--keep]")
        return 1
    bv = norm(sys.argv[1])
    if not bv:
        print("认不出这是哪个视频：" + sys.argv[1])
        return 1
    keep = "--keep" in sys.argv
    t0 = time.time()
    print("目标 " + bv)
    info, status = download(bv)
    if not info:
        print(status)
        return 1
    print("下载好了 %.1fMB  %.0fs  《%s》 %d 秒" % (
        os.path.getsize(info["path"]) / 1048576.0, time.time() - t0,
        info["title"][:40], info["dur"]))
    rc, out, err = watch(info["path"])
    print("sheet rc=%s  总用时 %.0fs" % (rc, time.time() - t0))
    if rc != 0:
        print("看失败：" + (err or out)[-800:])
        return 1
    retitle_note(info["title"])
    print(out[-3000:])
    if not keep:
        try:
            os.remove(info["path"])
            print("临时文件已清：" + os.path.basename(info["path"]))
        except OSError as exc:
            print("清理失败 %r" % (exc,))
    return 0


sys.exit(main())
