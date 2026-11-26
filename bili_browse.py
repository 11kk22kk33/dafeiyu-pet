# -*- coding: utf-8 -*-
"""B站首页推荐流翻阅：跑一次就退出。

只做三件事：读开关 → 拉首页推荐 → 追加到 bili-browse.jsonl。
开关关着直接退出，不联网。
"""
import asyncio, io, json, os, sys, time

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
MODULES = os.path.join(PET, "modules.json")
OUT = os.path.join(PET, "bili-browse.jsonl")
PLUG = _bp("bili_plugin", os.path.join("D:" + os.sep, "AstrBot", "AstrBot_4.29.1_windows_amd64_portable",
                     "backend", "app", "data", "plugins"))
DATA = _bp("bili_data", os.path.join("D:" + os.sep, "AstrBot", "AstrBot_4.29.1_windows_amd64_portable",
                     "backend", "app", "data", "plugin_data", "astrbot_plugin_bilivideo"))
KEEP = 300

def gate(mid):
    try:
        raw = json.load(open(MODULES, encoding="utf-8"))
    except Exception:
        return False
    if not isinstance(raw, dict) or not raw.get("master", True):
        return False
    for m in raw.get("modules") or []:
        if isinstance(m, dict) and m.get("id") == mid:
            return bool(m.get("on"))
    return False

def load_seen():
    seen = set()
    try:
        with open(OUT, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    b = json.loads(line).get("bvid")
                except Exception:
                    continue
                if b:
                    seen.add(b)
    except FileNotFoundError:
        pass
    return seen

async def fetch():
    sys.path.insert(0, PLUG)
    from astrbot_plugin_bilivideo.bilivideo.auth.cookies import CookieJar
    from astrbot_plugin_bilivideo.bilivideo.api.client import BilibiliHTTPClient
    from astrbot_plugin_bilivideo.bilivideo.api.wbi import sign_params
    from astrbot_plugin_bilivideo.bilivideo.core.constants import API_BASE

    ck = CookieJar(DATA)
    if not ck.is_logged_in():
        return None, "not_logged_in"
    url = API_BASE + "/x/web-interface/wbi/index/top/feed/rcmd"
    async with BilibiliHTTPClient(ck.get()).lifespan() as client:
        params = {"web_location": 1430650, "y_num": 5, "fresh_type": 3, "ps": 12,
                  "fresh_idx": 1, "fresh_idx_1h": 1, "fetch_row": 1, "brush": 0, "homepage_ver": 1}
        signed = await sign_params(params, client=client)
        payload = await client.request_json("GET", url, params=signed, expect_code_zero=False)
        if payload.get("code") != 0:
            return None, "code=%s %s" % (payload.get("code"), payload.get("message"))
        items = (payload.get("data") or {}).get("item") or []
        rows = []
        for it in items:
            bv = it.get("bvid")
            if not bv:
                continue
            rows.append({
                "ts": int(time.time()),
                "bvid": bv,
                "title": (it.get("title") or "").strip(),
                "up": ((it.get("owner") or {}).get("name") or "").strip(),
                "dur": it.get("duration") or 0,
            })
        return rows, "ok"

def main():
    if not gate("bili_browse"):
        print("skip: bili_browse off")
        return 0
    rows, status = asyncio.run(fetch())
    if rows is None:
        print("fail: " + status)
        return 1
    seen = load_seen()
    fresh = [r for r in rows if r["bvid"] not in seen]
    if not fresh:
        print("no new: %d items all seen" % len(rows))
        return 0
    with open(OUT, "a", encoding="utf-8") as fh:
        for r in fresh:
            fh.write(json.dumps(r, ensure_ascii=False) + chr(10))
    total = sum(1 for _ in open(OUT, encoding="utf-8"))
    if total > KEEP:
        keep = []
        with open(OUT, encoding="utf-8") as fh:
            for line in fh:
                keep.append(line)
        with open(OUT, "w", encoding="utf-8") as fh:
            fh.writelines(keep[-KEEP:])
    print("ok: +%d new (total %d)" % (len(fresh), min(total, KEEP)))
    for r in fresh[:5]:
        print("   " + r["bvid"] + " " + r["title"][:40])
    return 0

sys.exit(main())
