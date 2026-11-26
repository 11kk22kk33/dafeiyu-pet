# -*- coding: utf-8 -*-
"""适配器：外面进来的东西（QQ 请求、桌面请求、插件回调）先变成 Msg，
里面的东西出去时再变成各家要的格式。模块之间不直接传别人家的字典。"""
import json
import os
import re
import time

MARKER = re.compile(r"\[\[dfy:[^\]]*\]\]")
VOICE_TAG = "[[v]]"


class Msg(object):
    """统一消息：不管是 QQ 群、QQ 私聊还是桌面，进内核都长这样。"""

    __slots__ = ("source", "uid", "gid", "nick", "text", "images", "ts", "meta")

    def __init__(self, source, text, uid="", gid="", nick="", images=None, meta=None):
        self.source = source          # "qq" / "pet"
        self.text = text or ""
        self.uid = str(uid or "")
        self.gid = str(gid or "")
        self.nick = nick or ""
        self.images = images or []
        self.ts = time.time()
        self.meta = meta or {}

    @property
    def key(self):
        """会话键：群里按群，私聊按人，桌面单独一条。"""
        if self.gid:
            return "g:" + self.gid
        if self.uid:
            return "u:" + self.uid
        return "src:" + self.source

    @property
    def is_group(self):
        return bool(self.gid)

    def as_dict(self):
        return {"source": self.source, "uid": self.uid, "gid": self.gid,
                "nick": self.nick, "text": self.text, "images": len(self.images),
                "key": self.key, "ts": self.ts}

    def __repr__(self):
        return "<Msg %s %s %r>" % (self.source, self.key, self.text[:24])


def from_openai_body(body, source):
    """OpenAI 风格的请求体 -> Msg（QQ 和桌面走同一个口子）。"""
    msgs = body.get("messages") or []
    text = ""
    images = []
    for m in reversed(msgs):
        if m.get("role") != "user":
            continue
        c = m.get("content")
        if isinstance(c, str):
            text = MARKER.sub("", c)
        elif isinstance(c, list):
            for part in c:
                if part.get("type") == "text":
                    text += MARKER.sub("", part.get("text") or "")
                elif part.get("type") == "image_url":
                    images.append(part.get("image_url", {}).get("url", ""))
        if text or images:
            break
    uid = str(body.get("user") or "")
    gid = str(body.get("_group") or body.get("group_id") or "")
    return Msg(source, text, uid=uid, gid=gid, images=images)


def reply_payload(text, model="dafiyu"):
    """统一回复体：谁要都拿这一份，别各写各的。"""
    return {"id": "dfy-%d" % int(time.time() * 1000), "object": "chat.completion",
            "created": int(time.time()), "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                         "finish_reason": "stop"}], "usage": {}}


def strip_voice(text):
    """把语音标记从人话里擦掉（不念标记，也不让标记露出来）。"""
    return (text or "").replace(VOICE_TAG, "").strip()


def log(msg):
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "core.log")
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (time.strftime("%H:%M:%S"), msg))
    except Exception:
        pass
