# -*- coding: utf-8 -*-
"""小宠物的内核层：总线、护栏、适配器、语音服务。
规矩：外面（QQ、桌面、插件）只跟这一层说话，互相之间不许直接调。"""
from . import bus, guard, adapter, tts   # noqa: F401
