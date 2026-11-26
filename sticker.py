# -*- coding: utf-8 -*-
r"""图片 / 表情包的离线标签索引。

- 文件：收藏（自己的）在 02-素材\表情\收藏，临时收到的在 ...\临时
- 索引：D:\pet-shared\sticker-index.json，离线生成并缓存；运行时只查标签和 id，不读图片本体
- 标签来源：文件名分词 + 可视事实（动图/横竖/主色）+ 手动补的标签，不用模型，不花 token
"""
import hashlib
import io
import json
import os
import re
import time
import urllib.request

HERE = r"D:\pet-shared"
ROOT = r"D:\大肥鱼的自留地\02-素材\表情"
OWN = os.path.join(ROOT, "收藏")
TEMP = os.path.join(ROOT, "临时")
INDEX = os.path.join(HERE, "sticker-index.json")
EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")

STOP = set("表情 包 图 图片 表情包 img image pic gif png jpg jpeg webp sticker emoji 发 的 了 一个 一张".split())


def _read(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _write(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def load():
    raw = _read(INDEX, {"items": [], "at": 0})
    if not isinstance(raw, dict):
        raw = {"items": [], "at": 0}
    raw.setdefault("items", [])
    return raw


def save(raw):
    raw["at"] = time.time()
    _write(INDEX, raw)


def _name_tags(name):
    base = os.path.splitext(name)[0]
    parts = re.split(r"[_\-\s.,()[\]（）【】]+", base)
    out = []
    for p in parts:
        p = p.strip()
        if not p or p.lower() in STOP or len(p) > 12:
            continue
        if p.isdigit():
            continue
        if re.fullmatch(r"[0-9a-fA-F]{8,}", p):
            continue
        out.append(p.lower() if p.isascii() else p)
    return out[:8]


def _visual(path):
    """宽高、是否动图、主色 —— 离线可视事实。"""
    out = {"w": 0, "h": 0, "animated": False, "color": ""}
    try:
        from PIL import Image
        with Image.open(path) as im:
            out["w"], out["h"] = im.size
            out["animated"] = bool(getattr(im, "n_frames", 1) > 1)
            rgb = im.convert("RGB").resize((1, 1)).getpixel((0, 0))
            out["color"] = "#%02x%02x%02x" % rgb
    except Exception:
        pass
    return out


def _dhash(path):
    try:
        from PIL import Image
        with Image.open(path) as im:
            g = im.convert("L").resize((9, 8))
            px = list(g.getdata())
        bits = 0
        for row in range(8):
            for col in range(8):
                bits = (bits << 1) | (1 if px[row * 9 + col] < px[row * 9 + col + 1] else 0)
        return "%016x" % bits
    except Exception:
        return ""


def _entry(path, source):
    st = os.stat(path)
    h = hashlib.md5(("%s|%d" % (path, st.st_size)).encode("utf-8")).hexdigest()[:10]
    vis = _visual(path)
    return {
        "id": h,
        "src": source,
        "file": path,
        "name": os.path.basename(path),
        "tags": _name_tags(os.path.basename(path)),
        "bytes": st.st_size,
        "hash": _dhash(path),
        "at": time.strftime("%Y-%m-%d %H:%M"),
        **vis,
    }


def _merge(raw, entry):
    for old in raw["items"]:
        if old.get("id") == entry["id"]:
            tags = list(dict.fromkeys((old.get("tags") or []) + (entry.get("tags") or [])))
            old.update(entry)
            old["tags"] = tags
            return old
    raw["items"].append(entry)
    return entry


def add_source(src, source="own", tags=None):
    """把一张图/动图收进库：src 是本地路径或 http(s)。"""
    dst_dir = OWN if source == "own" else TEMP
    os.makedirs(dst_dir, exist_ok=True)
    name = ""
    if (src or "").startswith("data:image/"):
        import base64
        head, _, b64 = src.partition(",")
        ext = ".png" if "png" in head else (".jpg" if ("jpeg" in head or "jpg" in head) else ".webp")
        blob = base64.b64decode(b64)
        name = hashlib.md5(blob).hexdigest()[:10] + ext
        path = os.path.join(dst_dir, name)
        with open(path, "wb") as f:
            f.write(blob)
        entry = _entry(path, source)
        if tags:
            entry["tags"] = list(dict.fromkeys(entry["tags"] + [str(t) for t in tags]))
        raw = load()
        rec = _merge(raw, entry)
        save(raw)
        return rec
    if re.match(r"^https?://", src or ""):
        req = urllib.request.Request(src, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            blob = r.read()
        name = hashlib.md5(blob).hexdigest()[:10] + ".img"
        path = os.path.join(dst_dir, name)
        with open(path, "wb") as f:
            f.write(blob)
    else:
        import shutil
        name = os.path.basename(src)
        path = os.path.join(dst_dir, name)
        if os.path.abspath(src) != os.path.abspath(path):
            shutil.copyfile(src, path)
    entry = _entry(path, source)
    if tags:
        entry["tags"] = list(dict.fromkeys(entry["tags"] + [str(t) for t in tags]))
    raw = load()
    rec = _merge(raw, entry)
    save(raw)
    return rec


def build():
    raw = load()
    keep = [it for it in raw["items"] if os.path.exists(it.get("file") or "")]
    dropped = len(raw["items"]) - len(keep)
    raw["items"] = keep
    added = 0
    for src, d in (("own", OWN), ("temp", TEMP)):
        if not os.path.isdir(d):
            continue
        for name in os.listdir(d):
            if not name.lower().endswith(EXTS):
                continue
            path = os.path.join(d, name)
            e = _entry(path, src)
            before = len(raw["items"])
            _merge(raw, e)
            added += len(raw["items"]) - before
    save(raw)
    return {"added": added, "dropped": dropped, "total": len(raw["items"]), "at": raw["at"]}


def _score(entry, tags):
    hay = set(t.lower() for t in (entry.get("tags") or []))
    hay.add((entry.get("name") or "").lower())
    s = 0
    for t in tags:
        t = str(t).lower()
        if t in hay:
            s += 3
        elif any(t in h for h in hay):
            s += 2
        elif any(h in t for h in hay if len(h) >= 2):
            s += 1
    return s


def find(tags, n=5, source=None):
    """按标签找候选：只回 id / 标签 / 归类，不回图片本体。"""
    if isinstance(tags, str):
        tags = [t for t in re.split(r"[\s,，、]+", tags) if t]
    tags = [t for t in (tags or []) if str(t).strip()]
    raw = load()
    pool = [e for e in raw["items"] if not source or e.get("src") == source]
    if tags:
        scored = [( _score(e, tags), e) for e in pool]
        scored = [p for p in scored if p[0] > 0]
        scored.sort(key=lambda p: (-p[0], p[1].get("at") or ""))
        pool = [e for _, e in scored]
    return [{
        "id": e.get("id"),
        "src": e.get("src"),
        "tags": (e.get("tags") or [])[:8],
        "name": e.get("name"),
        "file": e.get("file"),
    } for e in pool[: max(1, int(n))]]


def tag_add(sid, tags):
    raw = load()
    for e in raw["items"]:
        if e.get("id") == sid:
            e["tags"] = list(dict.fromkeys((e.get("tags") or []) + [str(t) for t in tags]))
            save(raw)
            return e
    return None


def stats():
    raw = load()
    own = sum(1 for e in raw["items"] if e.get("src") == "own")
    temp = sum(1 for e in raw["items"] if e.get("src") == "temp")
    return {"own": own, "temp": temp, "total": len(raw["items"]), "at": raw.get("at") or 0}


if __name__ == "__main__":
    import sys
    if "--build" in sys.argv:
        print(json.dumps(build(), ensure_ascii=False))
    elif "--find" in sys.argv:
        i = sys.argv.index("--find")
        print(json.dumps(find(sys.argv[i + 1:]), ensure_ascii=False))
    else:
        print(json.dumps(stats(), ensure_ascii=False))
