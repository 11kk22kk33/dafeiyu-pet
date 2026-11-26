# -*- coding: utf-8 -*-
"""QQ / 通用回复分支：非流式（_plain）与 SSE 流式（_stream）。

QQ 与桌面共用这条回复管线（QQ 走 /qq/*，桌宠走 /pet/* 时由桌宠分支接走），
只认宿主交过来的 NEEDED，不 import proxy。
"""
import json
import re
import time

NEEDED = ("MODEL", "log", "clean_reply", "split_ctrl", "post_chat", "normalize_images",
          "VOICE_TAG", "maybe_voice", "record", "emit_event")


def install(ns):
    g = globals()
    for name in NEEDED:
        if name in ns:
            g[name] = ns[name]


# ---------- 连珠炮：分句 ----------
import random

BURST_SHORT = 20      # 短句豁免：整段不超过这么多字，一条发完
BURST_MAX = 35        # 单片上限，随机合并后也不超过
BURST_CUTS = 3        # 长句最多切几刀，不按字数硬凑
BURST_PIECES = 4      # 一条回复最多出几片
BURST_LONG = 150      # 超长（清单/代码/备课）整段发，绝不切片
BURST_MERGE = 0.45    # 相邻两片并回去的概率
_SENT = re.compile(r"(?<=[。！？!?~～…])")
_CLAUSE = re.compile(r"(?<=[，,、；;：:])")
_PEEL = "。，,、；;：: "


def _clean_bit(bit):
    bit = (bit or "").strip()
    while len(bit) > 1 and bit[-1] in _PEEL:
        bit = bit[:-1].rstrip()
    return bit


def _split_line(line):
    """一行里怎么切：先句末标点，再逗号，最后才按字数硬切。"""
    if len(line) <= BURST_SHORT:
        return [line]
    sents = [b for b in (_clean_bit(x) for x in _SENT.split(line)) if b]
    if len(sents) <= 1:
        sents = [b for b in (_clean_bit(x) for x in _CLAUSE.split(line)) if b]
    if len(sents) <= 1:
        return [line[i:i + BURST_MAX] for i in range(0, len(line), BURST_MAX)]
    out = []
    for s in sents:
        out.extend(_split_line(s) if len(s) > BURST_MAX else [s])
    return out


def _merge(bits):
    """随机把相邻两片并回去——真人不按固定节奏发消息。"""
    if len(bits) < 2:
        return bits
    out = []
    for b in bits:
        if out and len(out[-1]) + 1 + len(b) <= BURST_MAX and random.random() < BURST_MERGE:
            out[-1] = out[-1] + "，" + b
        else:
            out.append(b)
    return out


def burst(text):
    """切成 QQ 连珠炮碎片：长短随机，不为了凑片数硬切。

    短句（<20 字）整条发；长句按句读切，最多 3 刀、最多 4 片；
    切完再随机合并，所以同一句话每次节奏都不一样。
    """
    text = (text or "").replace("\r", "").strip()
    if not text:
        return []
    if len(text) > BURST_LONG:
        return [text]
    lines = [x.strip() for x in text.split("\n") if x.strip()]
    if not lines:
        return []
    if len(lines) == 1 and len(text) <= BURST_SHORT:
        return [text]
    out = []
    for line in lines:
        if ("【" in line and "】" in line) or "http" in line or "[CQ:" in line:
            out.append(line)
            continue
        bits = _split_line(line)
        if len(bits) > BURST_CUTS:
            bits = bits[:BURST_CUTS] + ["，".join(bits[BURST_CUTS:])]
        out.extend(_merge(bits))
    if len(out) > BURST_PIECES:
        out = out[:BURST_PIECES - 1] + ["，".join(out[BURST_PIECES - 1:])]
    return [x for x in out if x]


def shape(text):
    """切成换行分隔的碎片；AstrBot 的分段回复会按行逐条发出。"""
    frags = burst(text)
    return "\n".join(frags) if frags else text


# ---------- non-stream ----------
def plain(h, resp, source, user_text):
    body = resp.read()
    reply = ""
    ctrl_only = False
    try:
        reply = json.loads(body.decode("utf-8"))["choices"][0]["message"].get("content") or ""
    except Exception:
        pass
    try:
        data = json.loads(body.decode("utf-8"))
        content = data["choices"][0]["message"].get("content") or ""
        fixed = clean_reply(content)
        if fixed != content:
            data["choices"][0]["message"]["content"] = fixed
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            reply = fixed
        # 整条回复全是内部指令：没有正文，当空消息，不发也不兜底
        if content.strip() and not fixed.strip() and split_ctrl(content)[1]:
            ctrl_only = True
            log(f"{source} 全是指令 -> 不发")
    except Exception:
        pass
    try:
        has_tool = bool(json.loads(body.decode("utf-8"))["choices"][0]["message"].get("tool_calls"))
    except Exception:
        has_tool = False
    if has_tool:
        log(f"{source} -> tool_calls，交给 AstrBot 执行")
    if (not has_tool) and (not (reply or "").strip()) and getattr(h, "_retry_ctx", None) \
            and h._retry_ctx[1] and not getattr(h, "_retried", False):
        h._retried = True
        p2, n2, src2, ut2 = h._retry_ctx
        normalize_images(p2["messages"], max_side=1024, quality=72)
        log(f"{source} empty with {n2} image(s) -> retry harder-compressed")
        try:
            resp2 = post_chat(p2, timeout=180)
            body = resp2.read()
            reply = json.loads(body.decode("utf-8"))["choices"][0]["message"].get("content") or ""
            log(f"{source} retry -> {len(reply)} chars")
        except Exception as exc:
            log(f"retry failed: {exc}")
    if (not has_tool) and not ctrl_only and not (reply or "").strip():
        log(f"{source} upstream empty -> soft fallback line")
        try:
            data = json.loads(body.decode("utf-8"))
            reply = "唔……我这会儿有点卡壳，你再说一遍嘛。"
            data["choices"][0]["message"]["content"] = reply
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        except Exception:
            pass
    if (getattr(h, "_uid", None) or getattr(h, "_gid", None)) and VOICE_TAG in (reply or ""):
        reply = maybe_voice(reply, h._uid, getattr(h, "_gid", ""))
        try:
            data = json.loads(body.decode("utf-8"))
            data["choices"][0]["message"]["content"] = reply
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        except Exception:
            pass
    if (not has_tool) and (reply or "").strip():
        frags = burst(reply)
        if len(frags) > 1:
            reply = "\n".join(frags)
            try:
                data = json.loads(body.decode("utf-8"))
                data["choices"][0]["message"]["content"] = reply
                body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            except Exception:
                pass
            log(f"{source} burst -> {len(frags)} frags: " + "/".join(str(len(x)) for x in frags))
    if not has_tool:  # 工具调用这一轮没有正文，别写进记忆
        record(user_text, reply, source, getattr(h, "_gid", ""))
    emit_event("idle")
    log(f"{source} -> {len(reply)} chars")
    try:
        _chk = json.loads(body.decode("utf-8"))
        if not isinstance(_chk.get("choices"), list) or not _chk["choices"]:
            raise ValueError("no choices")
    except Exception as exc:
        log(f"{source} upstream body not openai-json ({exc}) -> synthesized")
        h._json(200, {"id": "fix-" + str(int(time.time())), "object": "chat.completion",
                      "choices": [{"index": 0,
                                   "message": {"role": "assistant", "content": reply or ""},
                                   "finish_reason": "stop"}],
                      "usage": {}})
        return
    h._raw(200, body)

# ---------- stream (SSE) ----------
def stream(h, resp, source, user_text):
    h._responded = True
    h.send_response(200)
    h.send_header("Content-Type", "text/event-stream; charset=utf-8")
    h.send_header("Cache-Control", "no-cache")
    h.send_header("Connection", "close")
    h.end_headers()
    h.close_connection = True

    chunks = []
    broken = False        # 流断了就别把半句写进记忆
    try:
        for raw_line in resp:
            line = raw_line.rstrip(b"\r\n")
            if not line:
                continue
            if VOICE_TAG.encode() in line:
                line = line.replace(VOICE_TAG.encode(), b"")
            h.wfile.write(line + b"\n\n")
            h.wfile.flush()
            if line.startswith(b"data: "):
                data = line[6:]
                if data == b"[DONE]":
                    break
                try:
                    delta = json.loads(data.decode("utf-8"))["choices"][0].get("delta", {})
                    piece = delta.get("content")
                    if piece:
                        chunks.append(piece)
                except Exception:
                    pass
    except (BrokenPipeError, ConnectionResetError):
        broken = True
        log("stream client gone, memory not written")
    except Exception as exc:
        broken = True
        log(f"stream error: {exc}")
        try:
            h.wfile.write(b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"error"}]}\n\n')
            h.wfile.write(b"data: [DONE]\n\n")
            h.wfile.flush()
        except Exception:
            pass
    finally:
        emit_event("idle")
        if broken:
            log(f"{source} stream broken after {len(''.join(chunks))} chars, memory skipped")
        else:
            reply = clean_reply("".join(chunks))
            if (getattr(h, "_uid", None) or getattr(h, "_gid", None)) and VOICE_TAG in (reply or ""):
                reply = maybe_voice(reply, h._uid, getattr(h, "_gid", ""))
            frags = burst(reply)
            record(user_text, reply, source, getattr(h, "_gid", ""))
            log(f"{source} -> {len(reply)} chars (stream), would burst into {len(frags)}")
