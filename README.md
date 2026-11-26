# 大肥鱼 · Windows QQ AI 宠物网关

把 AstrBot、QQ 协议端、桌面桌宠和语音通话缝成一条链的中间层。你在 QQ 里说话，桌面上那只真的会动、会念、会记住你。

> 本仓库只包含**中间层与配置**，不含 AstrBot / QQ / SnowLuma / 桌宠本体。这些要自己装，见下面的快速开始。

## 五层链路

```text
QQ 客户端（登录态）
   ↓ 协议层注入
SnowLuma ──WS──▶ proxy.py :8787（本仓库核心）
                    ├─▶ AstrBot :6185   人格 / 记忆 / 工具调用
                    ├─▶ 桌宠 :3000      身体、表情动作
                    ├─▶ 语音合成        本地 TTS → QQ 语音
                    └─▶ 鱼搭子         桌面挂件（开源派生）
   ↑ OpenAI 兼容 /v1/chat/completions，可被任何客户端复用
```

## 快速开始

1. 把本仓库解到磁盘（建议 `D:\你的目录`，路径别带中文和空格）。
2. **双击 `setup.html`**，跟着五步向导点：获取源码 → 装 QQ 与协议层 → 配路径 → 登录 QQ → 填 key。
3. 装完打开 <http://127.0.0.1:8787/console> 就是控制台。

不想用向导的话，手动做三件事：复制 `config-paths.example.json` 为 `config-paths.json` 并按本机改路径；把 key 写进 `key.txt`；双击 `start-stack.bat`。

朋友之前移植过一半、又自己改过 bug？先跑 `对账工具/三方对账.py`，读它生成的报告再动手。

## 功能清单

| 模块 | 能干什么 |
|---|---|
| 对话 | QQ 私聊 / 群聊接话，带人设与上下文 |
| 表情动作 | 回话里带标记，桌面那只真的做动作 |
| 语音 | 指定句子念成语音发到 QQ |
| 长期记忆 | 记住主人的事，聊到相关会想起来 |
| 心情 | 被夸会开心、被凶会委屈 |
| 提醒 | 到点主动找你 |
| 看屏幕 / 看视频 | 按需截屏、看完视频记得内容 |
| 好友申请 | 新申请进控制台，手动同意，一律当访客 |
| B 站 | 追更、评论、拆条 |
| 语音通话 | 默认关，关闭时零开销 |

## 目录

| 路径 | 作用 |
|---|---|
| `proxy.py` | 主网关，8787，拼人设 / 记忆 / 权限 / 防抖 |
| `console.html` | 浏览器控制台 |
| `setup.html` | 独立安装向导，双击即用 |
| `config-paths.json` | 17 个外部程序路径，唯一需要按本机改的文件 |
| `key.txt` | 模型密钥，自己填，绝不进配置 |
| `core/` `services/` `tools/` | 记忆、情绪、提醒、工具脚本 |
| `对账工具/` | 三方对账脚本 + 使用说明 |

## 许可边界（务必读完再用）

- 本仓库自研的中间层（`proxy.py` 等）：MIT，见 [LICENSE](LICENSE)。
- 桌面挂件 / 鱼搭子：派生自 [Yuuyuko-uu/dsh-fish-buddy](https://github.com/Yuuyuko-uu/dsh-fish-buddy)，**必须保留原作者署名，禁止宣称自研**。
- SnowLuma：非商业许可，**不随本仓库分发**，只给下载链接 <https://github.com/NapNeko/NapCatQQ-Desktop>。
- `vendor/` 下第三方插件：各自 LICENSE 已随附，不要删。

## 致谢

AstrBot、NapCat / SnowLuma、Yuuyuko-uu 的鱼搭子，以及所有把 QQ 机器人做到能用的人。

## 隐私

key、QQ 号、聊天记录都属于隐私。本仓库用 `.gitignore` 挡住 `key*.txt`、`config-paths.json`、日志与记忆文件，提交前请再确认一遍。
