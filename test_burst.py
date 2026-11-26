
# 跑之前先停 proxy，否则会真写真池
# -*- coding: utf-8 -*-
"""burst() / split_ctrl() 假数据测试，不启动主程序、不连 QQ。"""
import sys, importlib.util
sys.path.insert(0, r"D:\pet-shared")
from services.qq_service import burst   # noqa: E402

CASES = [
    ("短句豁免", "在干嘛"),
    ("短句带标点", "我在收拾屋子呢～"),
    ("长句三顿", "我在收拾屋子呢。刚才把那件毛衣叠好了，有点歪，不过还行～"),
    ("带指令", "【happy】我在收拾屋子呢。今天有点冷，毯子叠了两条。"),
    ("带链接", "看这个 https://example.com/a?b=1 挺有意思的。"),
    ("CQ码", "[CQ:image,file=abc.jpg] 刚存的图"),
    ("超长不切", "这是一段很长的东西。" * 20),
    ("只有指令", "【happy】"),
    ("纯英文", "ok"),
]

bad = 0
for name, text in CASES:
    frags = burst(text)
    sizes = [len(f) for f in frags]
    over = [] if len(text) > 150 else [s for s in sizes if s > 40]
    ok_pieces = len(frags) <= 4 or len(text) > 150
    ok_long = (len(text) > 150 and len(frags) == 1) or len(text) <= 150
    flag = "OK" if (ok_pieces and not over and ok_long) else "BAD"
    if flag == "BAD":
        bad += 1
    print("[%s] %s  in=%d -> %d 片 %s" % (flag, name, len(text), len(frags), sizes))
    for f in frags:
        print("      | " + f)

print("--- 随机性：同一句跑 6 次，片数应不一样 ---")
s = "我在收拾屋子呢。刚才把那件毛衣叠好了，有点歪，不过还行～。你要是等急了就先喝口水。"
print([len(burst(s)) for _ in range(6)])
print("burst 用例失败数 =", bad)

print("--- clean_reply / split_ctrl ---")
try:
    import proxy
    for t in ["【happy】我在收拾屋子。", "[wave] 过来看看", "【happy】[wave]", "没有指令的一句 [CQ:image,file=x.jpg]"]:
        clean, marks = proxy.split_ctrl(t)
        print("  in=%r -> out=%r marks=%r" % (t, clean, marks))
    print("  clean_reply('【happy】') =", repr(proxy.clean_reply("【happy】")))
except Exception as exc:
    print("  SKIP proxy import:", repr(exc)[:200])
