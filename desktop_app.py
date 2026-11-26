# -*- coding: utf-8 -*-
"""小宠物统一桌面客户端（P0 极简）：单实例 + Edge --app 壳 + F10 截屏。

零第三方依赖：不用 pywebview / keyboard / pystray / pywin32，只用 ctypes + PIL(已装) + stdlib。
启动：pythonw.exe desktop_app.py  （无控制台窗口）
"""
import base64
import ctypes
import io
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
from ctypes import wintypes

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
BASE = _bp("pet_shared", os.path.join("D:" + os.sep, "pet-shared"))
CONSOLE = "http://127.0.0.1:8787/console"
PROXY = "http://127.0.0.1:8787/v1/chat/completions"
EDGE = os.path.join(os.environ.get("ProgramFiles(x86)", "C:" + os.sep + "Program Files (x86)"),
                    "Microsoft", "Edge", "Application", "msedge.exe")
TITLE = "小宠物控制台"
FISH = "fb-merged.exe"
LOG = os.path.join(BASE, "desktop_app.log")
MUTEX = "Global\\DafeiyuConsoleShell"
MODEL = "deepseek-v4.1-flash-1017"

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

# ---------------- 日志（无控制台，只能落文件） ----------------
def log(msg):
    try:
        if os.path.exists(LOG) and os.path.getsize(LOG) > 300000:
            os.replace(LOG, LOG + ".1")
        with open(LOG, "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (time.strftime("%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


# ---------------- 找窗口：先精确标题，再模糊（Edge --app 标题可能带后缀） ----------------
def _real_shell(h):
    """只认 Edge --app 真窗口（Chrome_WidgetWin_1 且可见）。
    系统里会出现同名 TabProxyWindow 幽灵窗口，只按标题找会命中它，导致"点了没反应"。"""
    if not h or not user32.IsWindowVisible(h):
        return False
    cls = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(h, cls, 256)
    return cls.value == "Chrome_WidgetWin_1"


def find_window():
    hwnd = user32.FindWindowW(None, TITLE)
    if hwnd and _real_shell(hwnd):
        return hwnd
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(h, _l):
        if not user32.IsWindowVisible(h):
            return True
        n = user32.GetWindowTextLengthW(h)
        if n:
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(h, buf, n + 1)
            if TITLE in buf.value and _real_shell(h):
                found.append(h)
                return False
        return True

    user32.EnumWindows(cb, 0)
    return found[0] if found else 0


def focus_window():
    h = find_window()
    if not h:
        return False
    try:
        if user32.IsIconic(h):
            user32.ShowWindow(h, 9)      # SW_RESTORE：从最小化还原
        user32.ShowWindow(h, 9)
        user32.SetForegroundWindow(h)
        if user32.IsIconic(h):
            user32.ShowWindow(h, 9)      # 兜底：还被压着就再叫一次
        if user32.GetForegroundWindow() != h:
            user32.BringWindowToTop(h)
        return True
    except Exception:
        return False


# ---------------- 单实例：命名互斥，进程死了自动释放 ----------------
def single_instance():
    kernel32.CreateMutexW(None, False, MUTEX)
    return kernel32.GetLastError() != 183   # 183 = ERROR_ALREADY_EXISTS


# ---------------- 鱼搭子在跑 = 游戏模式（F10 才生效） ----------------
def fish_running():
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq " + FISH],
                             capture_output=True, text=True, timeout=6,
                             creationflags=0x08000000).stdout
        return FISH.lower() in (out or "").lower()
    except Exception:
        return False


# ---------------- 起壳 ----------------
def launch_shell():
    if not os.path.exists(EDGE):
        log("[shell] Edge 不在，退回默认浏览器")
        os.startfile(CONSOLE)
        return
    subprocess.Popen([EDGE, "--app=" + CONSOLE, "--window-size=1180,860",
                      "--window-position=180,60", "--no-first-run",
                      "--no-default-browser-check"], close_fds=True)
    log("[shell] Edge --app 已启动")


# ---------------- F10：截屏 -> 当轮发给 Proxy ----------------
_last_shot = 0.0
REPLY_LOG = os.path.join(BASE, "f10_replies.jsonl")


def _beep(freq, ms):
    try:
        import winsound
        winsound.Beep(freq, ms)
    except Exception:
        pass


def shot_and_send():
    global _last_shot
    from PIL import ImageGrab
    now = time.time()
    if now - _last_shot < 5.0:
        log("[F10] skip：5 秒内同一屏，已去重")
        return
    _last_shot = now
    _beep(1200, 60)
    t0 = time.time()
    buf = io.BytesIO()
    ImageGrab.grab(all_screens=True).convert("RGB").save(buf, "JPEG", quality=72)
    url = "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
    body = {"model": MODEL, "messages": [{"role": "user", "content": [
        {"type": "text", "text": "（我刚按了 F10，你看一眼屏幕）"},
        {"type": "image_url", "image_url": {"url": url}}]}]}
    req = urllib.request.Request(PROXY, json.dumps(body).encode("utf-8"),
                                 {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        raw = r.read()
    txt = ""
    try:
        rep = json.loads(raw.decode("utf-8"))
        txt = (rep.get("choices") or [{}])[0].get("message", {}).get("content") or ""
    except Exception:
        pass
    try:
        with open(REPLY_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps({"t": time.strftime("%m-%d %H:%M:%S"),
                                "kb": round(len(url) / 1024.0, 1),
                                "sec": round(time.time() - t0, 1),
                                "reply": txt}, ensure_ascii=False) + "\n")
    except Exception as e:
        log("[F10] 回执落盘失败: %r" % (e,))
    _beep(900, 40)
    log("[F10] shot %.0fKB ok in %.1fs reply=%s" % (len(url) / 1024.0, time.time() - t0,
                                                       (txt or "-").replace("\n", " ").strip()[:40]))


# ---------------- 全局热键（系统级，无需管理员、无需第三方库） ----------------
def hotkey_thread():
    MOD_NOREPEAT, VK_F10, WM_HOTKEY = 0x4000, 0x79, 0x0312
    if not user32.RegisterHotKey(None, 1, MOD_NOREPEAT, VK_F10):
        log("[F10] RegisterHotKey 失败（可能被其它程序占用）")
        return
    log("[F10] 已注册（仅鱼搭子运行时生效）tid=%d" % kernel32.GetCurrentThreadId())
    msg = wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
        if msg.message == WM_HOTKEY and msg.wParam == 1:
            log("[F10] 按下")
            if not fish_running():
                log("[F10] 忽略：鱼搭子没开")
                continue
            try:
                shot_and_send()
            except Exception as e:
                log("[F10] 失败: %r" % (e,))
    user32.UnregisterHotKey(None, 1)


def watch_shell():
    """Edge 窗口关掉后自己退，别留僵尸 pythonw。"""
    gone = 0
    while True:
        time.sleep(5)
        if find_window():
            gone = 0
        else:
            gone += 1
            if gone >= 3:
                log("[shell] 窗口已关闭，退出")
                os._exit(0)


def main():
    if not single_instance():
        ok = focus_window()
        log("[dup] 已有实例，前置=%s" % ok)
        return
    if focus_window():
        log("[shell] 窗口已在，只前置，不再开第二个")
    else:
        launch_shell()
    threading.Thread(target=hotkey_thread, daemon=True).start()
    watch_shell()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log("[fatal] %r" % (e,))
