# -*- coding: utf-8 -*-
"""桌宠分支：身体桥 + 身体回复（桌宠推来的对话和它要的身体动作）。

只认宿主交过来的 NEEDED 这几个名字，不 import proxy、也不碰 QQ 的任何东西。
协议不动：/pet/* 的请求体、回复 JSON、tool_calls(pet_say) 原样保留。
"""
import json
import time

NEEDED = ("MODEL", "log", "body_history", "build_messages", "post_chat",
          "clean_reply", "split_ctrl", "body_script", "record")


def install(ns):
    """宿主注入依赖。少给名字就少一份能力，但不会把整个大脑带崩。"""
    g = globals()
    for name in NEEDED:
        if name in ns:
            g[name] = ns[name]


# ---------- 桌宠身体桥 ----------
def body_bridge(h, payload, stream):
    try:
        if any(isinstance(m, dict) and m.get("role") == "tool"
               for m in payload.get("messages") or []):
            body_reply(h, "", stream, tool=False)
            return
        hist = body_history(payload)
        if not hist:
            body_reply(h, "", stream, tool=False)
            return
        last_user = hist[-1]["content"]
        msgs = build_messages({"messages": hist}, "pet")
        body = {"model": MODEL, "messages": msgs, "temperature": 0.75,
                "max_tokens": 220, "stream": False}
        resp = post_chat(body, timeout=90)
        data = json.loads(resp.read().decode("utf-8"))
        raw = data["choices"][0]["message"].get("content") or ""
        text = clean_reply(raw)
        text = (text or "").strip()
        # 桌面这只还要演：指令只留在这条通道，QQ 那边一个字都看不到
        if split_ctrl(raw)[1]:
            text = "".join("【%s】" % m for m in split_ctrl(raw)[1]) + text
        if not text:
            body_reply(h, "", stream, tool=False)
            return
        text = body_script(text, last_user)
        record(last_user, text, "pet")
        log(f"body -> {len(text)} chars")
        body_reply(h, text, stream, tool=True)
    except Exception as exc:
        log(f"body error: {exc}")
        try:
            body_reply(h, "我这会儿有点卡。", stream, tool=True)
        except Exception:
            pass

def body_reply(h, text, stream, tool):
    cid = "body-" + str(int(time.time()))
    call_id = "call_" + str(int(time.time() * 1000))
    if tool:
        tool_calls = [{"index": 0, "id": call_id, "type": "function",
                       "function": {"name": "pet_say",
                                    "arguments": json.dumps({"script": text}, ensure_ascii=False)}}]
        finish = "tool_calls"
        delta = {"role": "assistant", "content": None, "tool_calls": tool_calls}
    else:
        tool_calls = []
        finish = "stop"
        delta = {"role": "assistant", "content": ""}
    if stream:
        h.send_response(200)
        h.send_header("Content-Type", "text/event-stream; charset=utf-8")
        h.send_header("Cache-Control", "no-cache")
        h.send_header("Connection", "close")
        h.end_headers()
        h.close_connection = True
        try:
            first = {"id": cid, "object": "chat.completion.chunk", "model": MODEL,
                     "choices": [{"index": 0, "delta": delta, "finish_reason": None}]}
            last = {"id": cid, "object": "chat.completion.chunk", "model": MODEL,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]}
            h.wfile.write(b"data: " + json.dumps(first, ensure_ascii=False).encode("utf-8") + b"\n\n")
            h.wfile.write(b"data: " + json.dumps(last, ensure_ascii=False).encode("utf-8") + b"\n\n")
            h.wfile.write(b"data: [DONE]\n\n")
            h.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        return
    msg = {"role": "assistant", "content": "" if tool else text}
    if tool:
        msg["content"] = None
        msg["tool_calls"] = [{"id": call_id, "type": "function",
                              "function": tool_calls[0]["function"]}]
    h._json(200, {"id": cid, "object": "chat.completion", "model": MODEL,
                     "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
                     "usage": {}})
