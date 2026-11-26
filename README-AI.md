# 给接手的 AI：这套源码是什么、怎么让它跑起来

你拿到的是一套 **Windows 上运行的 QQ 陪伴宠物** 的全部功能源码，Python 3.12。
**不含**：任何人设文案（已清空为占位）、任何记忆/日记/聊天记录、任何账号凭据、任何历史日志。
**含**：五层链路里第 4 层（业务中间层）和第 5 层（桌宠前端）的全部代码，以及三层外设脚本。

你的任务不是照抄，而是按本文件把环境配起来，然后让人写人设。

## 1. 链路

QQ 客户端 → SnowLuma（注入 QQ 进程，输出 OneBot）→ AstrBot（消息分发）→ **proxy.py（8787，OpenAI 兼容端点）** → 模型 → 原路回 QQ。

proxy.py 是这套东西的大脑：拼人格、灌记忆、分级权限、防抖、连发切片、提醒、看图看视频，全在这一个文件里。AstrBot 只当搬运工。

## 2. 目录与职责

| 文件 | 作用 | 备注 |
|---|---|---|
| proxy.py | 主服务，4774 行，HTTP 8787，路由 /qq/v1 与 /api/console/* | 核心，先读它 |
| console.html | 控制台单页，模块开关、人格编辑、心跳 | 由 8787 直接吐 |
| emotion.html | 情绪面板 | 同上 |
| core/hands.py | 工具调用执行器（文件、进程、屏幕） | 权限最重，慎改 |
| core/memory.py | 短期/长期记忆池 | |
| core/reminder.py | 定时提醒队列，到点回调发 QQ | |
| core/diary.py | 每日摘要 | |
| core/emotion.py | 情绪状态机 | |
| core/bus.py, adapter.py, guard.py, poollock.py, tts.py | 事件总线、协议适配、护栏、文件锁、TTS | |
| services/qq_service.py | 回复发送管线：连发切片 burst()、按行重组 shape() | 连发逻辑在这 |
| services/pet_service.py | 桌宠通道 | |
| reach.py, qqops.py | 主动发起 QQ 消息 / 群操作 | |
| sticker.py | 表情包收集与发送 | |
| learn.py | 从对话里学事实 | |
| vision.py | 视频转写与视觉笔记 | 需 VL 模型 |
| sheet.py | 抽帧拼图，喂给视觉模型 | 需 ffmpeg |
| voice_call.py, tts_edge.py | 语音通话、edge-tts 合成 | 默认关 |
| bili_fetch.py, bili_browse.py, bili_login.py | B站下载/自动翻阅/扫码登录 | **有外部依赖，见第 6 节** |
| desktop_app.py | 桌面宠物窗口 | |
| pet_qq_only.py | 只跑 QQ 通道的瘦入口 | |
| tools/*.py | 记忆清洗、去重、日记批处理等一次性脚本 | |
| tests/, test_burst.py, test_concurrent.py | 契约测试与连发测试 | 先跑这两个 |
| personas/, persona.md | 人格卡 | **已清空，需人写** |

## 3. 依赖

Python 3.12。第三方包：`pillow`, `psutil`, `numpy`, `edge-tts`, `imageio-ffmpeg`。
其余 import 都是标准库或仓库内模块。
`core/poollock.py` 里的 `fcntl` 在 Windows 不可用，代码有 msvcrt 回退分支，不要动它。

## 4. 首次启动

1. 装依赖：`pip install pillow psutil numpy edge-tts imageio-ffmpeg`
2. 改死路径：见第 5 节。
3. 启动：`python proxy.py`（正式跑用 pythonw.exe 免得黑框）。
4. 自检：`GET http://127.0.0.1:8787/health` 应返回 200。
5. 跑测试：`python test_burst.py`、`python tests/test_contract.py`。
6. 打开 `http://127.0.0.1:8787/api/console` 或项目根的控制台页面。

## 5. 必须改的硬编码

| 位置 | 现在是什么 | 改成 |
|---|---|---|
| 各文件顶部 BASE 常量 | `D:\pet-shared` | 你的目录 |
| bili_fetch.py / bili_browse.py / bili_login.py | `D:\AstrBot\...\plugin_data\astrbot_plugin_bilivideo` | 见第 6 节 |
| start-stack.bat | 五层各自的启动命令与本机路径 | 你的路径 |
| proxy.py 里的模型名 | deepseek / qwen 系列 | 你的 provider 同名模型 |
| proxy.py 里的称呼措辞 | 原主人的人格措辞（"主人"等） | 全局替换成你要的称呼 |

配置文件（brain / memory / modules / safe-mode / reminders / qq-session / qq-debounce / emotion-config / hands / heart-state / longterm / pet-session / reach-config / reach-state / student-memory / usage / watch-until）首次运行会自行生成，**不要从别处拷现成的**，那些是别人的数据。

## 6. B站三个脚本的外部依赖

`bili_fetch.py` / `bili_browse.py` / `bili_login.py` 依赖 AstrBot 插件 `astrbot_plugin_bilivideo` 内部的 `bilivideo` 包（CookieJar / BilibiliHTTPClient / sign_params / API_BASE），并且该包要从 AstrBot 的 plugin_data 目录导入。**这两个脚本不是自包含的**，要么把那个插件一起装进 AstrBot，要么自己实现：cookies 读写、WBI 签名、playurl 取流。

WBI 签名是这个功能的关键，别省。

## 7. 人格怎么填

三处，逐级覆盖：`personas/dafeiyu.md`（主人档，最详细）→ `persona.md`（默认档）→ `personas/family/jiejie.md`、`personas/visitor/teacher.md`（家人、访客）。
访客档默认不给记忆，这是设计，不要为了"更热情"把它打开。

## 8. 十个坑（省你一晚上）

1. **它突然不理人但端口全活**：SnowLuma 的 hook 掉了，消息在 QQ 内部被丢。日志搜 `private-message reporting is unavailable`。处理：杀光 QQ.exe，等 8 秒重开。
2. AstrBot 要用它自带的 python、从它自己的目录启动。
3. 启动脚本别单独跑，先查端口，否则起第二份抢端口。
4. 系统提示词按总长从尾部截断时，中间追加的块会被吃掉——追加要放在截断之后。
5. 带图的请求把思考关掉，否则 flash 类模型会把额度全烧在 reasoning 里。
6. 默认关闭的开关要 fail-closed，不能用"查不到就当开"的函数。
7. 跨进程读别人配置不可靠，用共享 JSON 当桥。
8. 工具调用会让"续轮"判断失真：只看最后一条是不是工具结果。
9. ffmpeg 不在 PATH 时用 imageio_ffmpeg 取路径。
10. 连发切片靠 AstrBot 的"按行分段"落地，切成多行才会变成多条消息。

## 9. 安全

- QQ 协议层是注入式非官方方案，**先用小号跑两周**。
- cookies、token、密码哈希一律不要进 git，也不要发给别人。
- 一切主动行为（定时翻阅、发评论、主动找人）默认关，打开要有明确开关位。

## 10. 验收

能私聊收到回复、能记住隔天的事、连发能切成两三条、提醒到点真的发出——四条都过，这就算接上了。

## 11. 示例提示词在哪

`proxy.py` 里还留着一些**示例**措辞（人设名已统一换成占位「小宠物」）。要改就改这几处，别只改 personas/：
第 167 行（回复语气尾缀）、175 行（默认人设）、1435 / 1447 行（老师档）、1472 行（家人档）、1896 行（主动搭话）。
`core/memory.py` 第 465 行起会读 `teacher-kb.md`，包内已给空模板。

## 12. 控制台怎么用

控制台不是独立程序，是 `proxy.py` 顺手吐出来的两张网页（`proxy.py:3916` 与 `:3929` 两个路由）。**proxy 不起，控制台就是 404。**

| 入口 | 地址 | 文件 | 能干什么 |
|---|---|---|---|
| 主控制台 | http://127.0.0.1:8787/console | console.html | 模块开关、人格编辑、待办提醒、心跳、用量、账号 |
| 情绪面板 | http://127.0.0.1:8787/emotion | emotion.html | 看/调情绪状态机 |

用法：先 `python proxy.py`，等到 `/health` 返回 200，再用浏览器开上面两个地址。
页面里所有按钮打的都是 `/api/console/*`，和 proxy 同源，不需要另外配服务器。
监听地址写死在 `proxy.py:9`（127.0.0.1:8787），要换端口就改这一行，控制台地址跟着变。

控制台里**默认关闭**的开关（自动翻阅、发评论、语音通话）是设计如此，不是坏了。

## 13. 挂件桥（可选）

`core/hands.py` 里有一段"挂件桥"：本机在 8777 收挂件发来的话，处理完 POST 到挂件自己的 8778 回话口。
这部分是**服务端**，源码在本包里。挂件（客户端）不在本包，需要你自己找或自己写一个；
只要能往 8777 发、并在 8778 收 `{"text": ...}`，就能接上。不用挂件的话这段逻辑闲置，不影响其他功能。

## 14. vendor 里的两个第三方（不是你写的，也不是我写的）

这两个是 MIT 开源项目，原样放在 `vendor\` 下，改不了也别去改；出问题先查它们自己的 README 和 CHANGELOG。

| 目录 | 项目 | 版本 | 上游 | 谁在用它 |
|---|---|---|---|---|
| vendor\storyboard | dsh-video-storyboard | 0.4.2 | github.com/SAOS-3624/dsh-video-storyboard | `sheet.py:12` 的 STORY 常量 |
| vendor\astrbot_plugin_biliVideo | biliVideo 视频总结 | 2.1.0（作者 storyAura） | AstrBot 插件市场同名 | `bili_fetch.py` / `bili_browse.py` / `bili_login.py` |

**storyboard** 需要 Node（版本见它自己的 package.json）。把 `sheet.py:12` 指向你放 vendor\storyboard 的实际位置即可，别改它内部代码。

**biliVideo** 要放到 AstrBot 的 `data\plugin_data\` 下，且**目录名必须叫 `astrbot_plugin_bilivideo`**（小写 v，和压缩包里的名字不一样）。因为三个 bili 脚本用的是 `from astrbot_plugin_bilivideo.bilivideo...` 这种导入。它自带扫码登录，第一次用要先扫码，cookies 存进它自己的数据目录，别进 git。

两个目录里的 `SESSDATA`、`DedeUserID` 全都只是代码里的**字段名**，没有任何真实凭据，已核对。
