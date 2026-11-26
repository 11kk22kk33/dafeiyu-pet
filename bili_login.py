# -*- coding: utf-8 -*-
import asyncio, json, os, sys, time
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
sys.path.insert(0, _bp("bili_plugin", os.path.join("D:" + os.sep, "AstrBot", "AstrBot_4.29.1_windows_amd64_portable",
                     "backend", "app", "data", "plugins")))
from astrbot_plugin_bilivideo.bilivideo.auth.qrlogin import QRLoginService, LoginStatus
from astrbot_plugin_bilivideo.bilivideo.auth.cookies import CookieJar
import segno

DATA = _bp("bili_data", os.path.join("D:" + os.sep, "AstrBot", "AstrBot_4.29.1_windows_amd64_portable",
                     "backend", "app", "data", "plugin_data", "astrbot_plugin_bilivideo"))
QR = os.path.join(_bp("pet_shared", os.path.join("D:" + os.sep, "pet-shared")), "bili-login-qr.png")

async def main():
    svc = QRLoginService()
    qr = await svc.generate()
    if not qr:
        print("GENERATE_FAILED", flush=True)
        return 2
    segno.make(qr.url).save(QR, scale=16, border=4)
    print("QR_READY %s" % time.strftime("%H:%M:%S"), flush=True)
    res = await svc.run_until_complete(qr.key, total_timeout=175)
    print("RESULT %s" % res.status.value, flush=True)
    if res.status == LoginStatus.SUCCESS and res.cookies:
        CookieJar(DATA).save(res.cookies)
        print("COOKIES_SAVED keys=" + ",".join(sorted(res.cookies.keys())), flush=True)
        return 0
    return 1

sys.exit(asyncio.run(main()))
