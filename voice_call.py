# -*- coding: utf-8 -*-
"""语音通话懒加载模块：只在控制台 voice_call 开关打开时被 import。

硬契约（关闭态零开销）：
1. 模块顶层不 import 重依赖、不开端口/线程、不读模型/证书、不写文件。
2. 重资源只在 start() 里初始化，由 /voice 首次访问触发。
3. stop() 收干净 start() 起的东西，可重复调用。
4. 只服务一个静态页；识别与朗读都由浏览器做，回话走现有 /v1/chat/completions。
   页面请求带 _voice=true，proxy 的 record() 见到就跳过写记忆池。
"""
_running = False

PAGE = '<!doctype html>\n<html lang="zh-CN"><head><meta charset="utf-8">\n<meta name="viewport" content="width=device-width,initial-scale=1">\n<title>小宠物 · 语音通话</title>\n<style>\n:root{--bg:#0f1218;--card:#171b23;--line:#252b36;--tx:#e8ecf3;--dim:#8b95a6;--brand:#d94a76}\n*{box-sizing:border-box}\nbody{margin:0;background:radial-gradient(circle at 50% 0%,#1c2230,#0c0f14 70%);color:var(--tx);\nfont:15px/1.7 "Microsoft YaHei",system-ui,sans-serif;height:100vh;display:flex;align-items:center;justify-content:center}\n.wrap{width:min(560px,92vw);display:flex;flex-direction:column;gap:18px;align-items:center}\n.orb{width:150px;height:150px;border-radius:50%;background:radial-gradient(circle at 35% 30%,#ff9db8,#d94a76 60%,#8d2547);\nbox-shadow:0 0 40px rgba(217,74,118,.35),inset 0 -8px 24px rgba(0,0,0,.35);display:flex;align-items:center;justify-content:center;\nfont-size:52px;animation:breath 4s ease-in-out infinite;transition:box-shadow .3s,filter .3s}\n@keyframes breath{0%,100%{transform:scale(1);filter:brightness(.95)}50%{transform:scale(1.06);filter:brightness(1.12)}}\n.orb.say{animation-duration:1.1s;box-shadow:0 0 60px rgba(217,74,118,.6),inset 0 -8px 24px rgba(0,0,0,.35)}\n.orb.listen{box-shadow:0 0 60px rgba(90,200,140,.6),inset 0 -8px 24px rgba(0,0,0,.35)}\n.log{width:100%;height:38vh;overflow-y:auto;background:var(--card);border:1px solid var(--line);border-radius:14px;\npadding:14px 16px;display:flex;flex-direction:column;gap:10px;font-size:14px}\n.m{max-width:86%;padding:8px 12px;border-radius:12px;white-space:pre-wrap;word-break:break-word}\n.me{align-self:flex-end;background:#2a3140;color:#dfe6f2}\n.fish{align-self:flex-start;background:#3a1f2b;color:#ffd8e4}\n.bar{width:100%;display:flex;gap:8px}\n.bar input{flex:1;background:var(--card);border:1px solid var(--line);color:var(--tx);border-radius:12px;padding:11px 14px;font-size:15px;outline:none}\n.bar input:focus{border-color:var(--brand)}\n.bar button{background:var(--brand);border:0;color:#fff;border-radius:12px;padding:0 18px;font-size:15px;cursor:pointer}\n.bar button.gh{background:var(--card);border:1px solid var(--line);color:var(--tx)}\n.bar button:disabled{opacity:.45;cursor:default}\n.hint{margin:0;color:var(--dim);font-size:12px;text-align:center}\n</style></head><body>\n<div class="wrap">\n  <div class="orb" id="orb"><span>🐟</span></div>\n  <div class="log" id="log"><div class="m fish">说话吧，我听着。</div></div>\n  <div class="bar">\n    <button class="gh" id="mic" title="按住说话">🎤</button>\n    <input id="txt" placeholder="打字也行，回车发送" autocomplete="off">\n    <button id="send">发送</button>\n  </div>\n  <p class="hint" id="hint">语音识别由浏览器提供，国内网络可能不通；不通就打字。回复会自动念出来。</p>\n</div>\n<script>\nvar logEl=document.getElementById(\'log\'),txtEl=document.getElementById(\'txt\'),\n    micEl=document.getElementById(\'mic\'),sendEl=document.getElementById(\'send\'),\n    orb=document.getElementById(\'orb\'),hintEl=document.getElementById(\'hint\'),busy=false;\nfunction add(who,t){\n  var d=document.createElement(\'div\');d.className=\'m \'+(who===\'me\'?\'me\':\'fish\');\n  d.textContent=t;logEl.appendChild(d);logEl.scrollTop=logEl.scrollHeight;\n}\nfunction clean(t){return (t||\'\').replace(/【[^】]{0,20}】/g,\'\').replace(/\\[\\[v\\]\\]/g,\'\').trim();}\nfunction speak(t){\n  var s=clean(t);if(!s||!window.speechSynthesis)return;\n  try{speechSynthesis.cancel();var u=new SpeechSynthesisUtterance(s);\n  u.lang=\'zh-CN\';u.rate=1.05;u.pitch=1.05;\n  u.onstart=function(){orb.classList.add(\'say\');};\n  u.onend=function(){orb.classList.remove(\'say\');};\n  speechSynthesis.speak(u);}catch(e){}\n}\nasync function send(){\n  if(busy)return;\n  var t=(txtEl.value||\'\').trim();if(!t)return;\n  txtEl.value=\'\';add(\'me\',t);busy=true;sendEl.disabled=true;orb.classList.add(\'say\');\n  try{\n    var r=await fetch(\'/v1/chat/completions\',{method:\'POST\',headers:{\'Content-Type\':\'application/json\'},\n      body:JSON.stringify({model:\'deepseek-v4.1-flash-1017\',stream:false,_voice:true,\n        messages:[{role:\'user\',content:t}]})});\n    var j=await r.json();\n    var c=((j.choices||[{}])[0].message||{}).content||\'（没说话）\';\n    add(\'fish\',c);speak(c);\n  }catch(e){add(\'fish\',\'断了：\'+e.message);}\n  finally{busy=false;sendEl.disabled=false;orb.classList.remove(\'say\');txtEl.focus();}\n}\nsendEl.onclick=send;\ntxtEl.addEventListener(\'keydown\',function(e){if(e.key===\'Enter\')send();});\nvar SR=window.SpeechRecognition||window.webkitSpeechRecognition;\nif(!SR){micEl.style.display=\'none\';hintEl.textContent=\'这个浏览器没有语音识别，打字吧；回复还是会被念出来。\';}\nelse{\n  var rec=null,on=false;\n  micEl.onclick=function(){\n    if(on){try{rec.stop();}catch(e){};return;}\n    try{\n      rec=new SR();rec.lang=\'zh-CN\';rec.interimResults=false;rec.maxAlternatives=1;rec.continuous=false;\n      rec.onstart=function(){on=true;orb.classList.add(\'listen\');micEl.textContent=\'⏹\';};\n      rec.onerror=function(e){hintEl.textContent=\'识别失败：\'+(e.error||\'unknown\')+\'（国内一般走不通，打字吧）\';};\n      rec.onend=function(){on=false;orb.classList.remove(\'listen\');micEl.textContent=\'🎤\';};\n      rec.onresult=function(ev){var s=ev.results[0][0].transcript;if(s){txtEl.value=s;send();}};\n      rec.start();\n    }catch(e){hintEl.textContent=\'启动识别失败：\'+e.message;}\n  };\n}\n</script></body></html>'


def start():
    """开关打开后首次 GET /voice 时调用。幂等。"""
    global _running
    if _running:
        return True
    _running = True
    return True


def stop():
    """开关被关掉时调用：停线程、关端口、释放设备。幂等。"""
    global _running
    _running = False
    return True


def handle(handler):
    """接管 /voice* 请求。只回一个静态页，不起线程、不碰音频设备。"""
    path = getattr(handler, "path", "/voice").split("?")[0].rstrip("/") or "/voice"
    if path != "/voice":
        handler._json(404, {"ok": False, "error": "not found"})
        return
    body = PAGE.encode("utf-8")
    handler.send_response(200)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)
