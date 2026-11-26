
# 跑之前先停 proxy，否则会真写真池
# -*- coding: utf-8 -*-
"""并发压测：QQ(AstrBot) + 桌面宠物(鱼搭子) 同时打 8787，外加记忆池写冲突验证。
用法:
  python test_concurrent.py            # 全跑
  python test_concurrent.py --worker TMP TAG N   # 内部用：独立进程写池子
"""
import json, os, sys, time, threading, urllib.request, urllib.error, subprocess, shutil, tempfile

BASE = r"D:\pet-shared"
sys.path.insert(0, BASE)
PROXY = "http://127.0.0.1:8787"
HANDS = "http://127.0.0.1:8777"
POOL = os.path.join(BASE, "memory-pool.json")
PY = sys.executable

def post(url, payload, timeout=180):
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
            code = r.status
    except urllib.error.HTTPError as e:
        return {"ok": False, "code": e.code, "ms": (time.time()-t0)*1000, "err": "http%d" % e.code}
    except Exception as e:
        return {"ok": False, "code": 0, "ms": (time.time()-t0)*1000, "err": type(e).__name__}
    ms = (time.time()-t0)*1000
    try:
        txt = (json.loads(raw)["choices"][0]["message"].get("content") or "")
    except Exception:
        txt = ""
    return {"ok": True, "code": code, "ms": ms, "chars": len(txt)}

def qq_payload(i):
    import proxy
    sec = proxy.marker_secret()
    mark = "[[dfy:uid=1000000000;nick=测;sig=%s]] " % proxy._sign("1000000000", "测", sec)
    return {"model": "x", "stream": False,
            "messages": [{"role": "user", "content": mark + "并发测试第%d句，随便说一句" % i}]}

def pet_payload(i):
    return {"model": "x", "stream": False,
            "messages": [{"role": "user", "content": "桌面并发测试第%d句" % i}]}

def phase_http(n_pet=4, n_qq=4):
    print("== 1. 混合并发（宠物 %d + QQ %d 同时打）==" % (n_pet, n_qq))
    jobs = [("pet", pet_payload(i)) for i in range(n_pet)] + [("qq", qq_payload(i)) for i in range(n_qq)]
    res = {}
    def run(kind, payload, k):
        res[k] = (kind, post(PROXY + ("/v1/chat/completions" if kind == "pet" else "/qq/v1/chat/completions"), payload))
    th = []
    t0 = time.time()
    for k, (kind, payload) in enumerate(jobs):
        t = threading.Thread(target=run, args=(kind, payload, k)); t.start(); th.append(t)
        time.sleep(0.15)
    for t in th: t.join()
    wall = time.time() - t0
    ok = err = empty = 0
    for k in sorted(res):
        kind, r = res[k]
        if not r["ok"]:
            err += 1; print("  %-3s %-5s 失败 %s (%.0fms)" % (kind, "#%d" % k, r["err"], r["ms"]))
        else:
            empty += (r["chars"] == 0); ok += 1
            print("  %-3s %-5s %s %.0fms %d字" % (kind, "#%d" % k, r["code"], r["ms"], r["chars"]))
    print("  合计 %d 条 / 墙钟 %.1fs / 失败 %d / 空回 %d（空回=防抖被后一条顶掉，正常）" % (len(jobs), wall, err, empty))
    return err

def phase_hands():
    print("== 2. 8777 游戏模式连打 ==")
    for i in range(3):
        r = post(HANDS + "/", {"游戏": bool(i % 2)})
        print("  hands #%d %s %.0fms %s" % (i, r.get("code"), r["ms"], r.get("err", "")))
    return 0

def phase_pool_multiproc(n_proc=3, n_each=8):
    print("== 3. 多进程同写一个池子（临时副本，不动真库）==")
    tmp = tempfile.mkdtemp(prefix="pooltest-")
    pool = os.path.join(tmp, "memory-pool.json")
    shutil.copy(POOL, pool)
    procs = [subprocess.Popen([PY, os.path.abspath(__file__), "--worker", tmp, str(i), str(n_each)],
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT) for i in range(n_proc)]
    for p in procs: p.wait()
    d = json.load(open(pool, encoding="utf-8"))
    got = [it["text"] for it in d["items"] if it["text"].startswith("并发进程测试")]
    expect = n_proc * n_each
    print("  期望 %d 条 / 实际留下 %d 条 / 文件解析 OK（%d 条总量）" % (expect, len(set(got)), len(d["items"])))
    if len(set(got)) < expect:
        print("  -> 跨进程写会互相覆盖（进程内 RLock 管不到别的进程）")
    else:
        print("  -> 没丢条目")
    shutil.rmtree(tmp, ignore_errors=True)

def phase_intact():
    print("== 4. 真库体检 ==")
    n = len(json.load(open(POOL, encoding="utf-8"))["items"])
    tmp_left = os.path.exists(POOL + ".tmp")
    print("  memory-pool.json 可解析，%d 条，.tmp 残留=%s" % (n, tmp_left))
    h = post(PROXY + "/v1/chat/completions", pet_payload(99))
    print("  压测后仍可用: %s %.0fms" % (h.get("code"), h["ms"]))

def worker(tmp, tag, n):
    import core.memory as M
    M.POOL = os.path.join(tmp, "memory-pool.json")
    M.ARCHIVE = os.path.join(tmp, "archive.jsonl")
    for i in range(n):
        M.add("private", "并发进程测试 %s-%d" % (tag, i), kind="event", dedupe=False)
        time.sleep(0.02)

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--worker":
        worker(sys.argv[2], sys.argv[3], int(sys.argv[4]))
    else:
        e = phase_http() + phase_hands()
        phase_pool_multiproc()
        phase_intact()
        print("== 结论 ==")
        print("  HTTP 层失败数 = %d" % e)
