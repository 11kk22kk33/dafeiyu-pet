# -*- coding: utf-8 -*-
"""把素材夹里的 DeepSeek 鲸鱼娘图，压成网页能用的体积。

用法：
    python tools/build_char_assets.py

输出到 assets/char/：
    mood/<情绪>.webp     8 张全身立绘，高 640px，情绪页大图
    mascot.webp          Q 版方图 240px，侧栏底部
    avatar.webp          Q 版方图 128px，顶部品牌图标
    favicon.png          64px 方形，浏览器标签页

源素材不在项目内（在素材夹），所以这个脚本是"本地执行一次"的工具，
生成物不随仓库分发。assets/char/ 已写进 .gitignore。
"""
import os
import sys

from PIL import Image

SRC_MOOD = r"D:\小w工桌地\ppt\素材夹\AI娘图鉴\DeepSeek鲸鱼娘\02_表情包"
SRC_Q = os.path.join(SRC_MOOD, "073_transparent-expression_深海女仆工坊·开心.png")

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(HERE, "assets", "char")

# 文件名前缀 -> 情绪键（和前端 mood 字段对应）
MOODS = {
    "025": "angry",
    "026": "cheerful",
    "027": "confused",
    "028": "exasperated",
    "029": "frightened",
    "030": "serious",
    "031": "shy",
    "032": "starry",
}

MOOD_HEIGHT = 640
QUALITY = 82


def find_src(prefix):
    for f in os.listdir(SRC_MOOD):
        if f.startswith(prefix) and f.lower().endswith(".png"):
            return os.path.join(SRC_MOOD, f)
    return None


def trim(im):
    """按 alpha 裁掉四周空白，避免透明边把人物挤小。"""
    if im.mode not in ("RGBA", "LA"):
        return im
    box = im.getchannel("A").getbbox()
    return im.crop(box) if box else im


def resize_h(im, h):
    w = max(1, round(im.width * h / im.height))
    return im.resize((w, h), Image.LANCZOS)


def save_webp(im, path, quality=QUALITY):
    im.save(path, "WEBP", quality=quality, method=6)
    return os.path.getsize(path)


def main():
    if not os.path.isdir(SRC_MOOD):
        sys.exit("素材目录不存在：" + SRC_MOOD)

    for sub in ("mood",):
        os.makedirs(os.path.join(OUT, sub), exist_ok=True)

    total = 0
    missing = []

    for prefix, key in MOODS.items():
        src = find_src(prefix)
        if not src:
            missing.append(prefix)
            continue
        im = trim(Image.open(src).convert("RGBA"))
        im = resize_h(im, MOOD_HEIGHT)
        out = os.path.join(OUT, "mood", key + ".webp")
        n = save_webp(im, out)
        total += n
        print(f"  mood/{key:12s} {im.width}x{im.height}  {n/1024:6.1f} KB")

    q = trim(Image.open(SRC_Q).convert("RGBA"))
    side = max(q.width, q.height)
    square = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    square.paste(q, ((side - q.width) // 2, (side - q.height) // 2))

    for name, size in (("mascot.webp", 240), ("avatar.webp", 128)):
        im = square.resize((size, size), Image.LANCZOS)
        out = os.path.join(OUT, name)
        n = save_webp(im, out)
        total += n
        print(f"  {name:18s} {size}x{size}  {n/1024:6.1f} KB")

    fav = square.resize((64, 64), Image.LANCZOS)
    fav_path = os.path.join(OUT, "favicon.png")
    fav.save(fav_path, "PNG", optimize=True)
    total += os.path.getsize(fav_path)
    print(f"  avatar 64 png        {os.path.getsize(fav_path)/1024:6.1f} KB")

    print(f"\n合计 {total/1024:.0f} KB  ->  {OUT}")
    if missing:
        print("缺失前缀：", missing)


if __name__ == "__main__":
    main()
