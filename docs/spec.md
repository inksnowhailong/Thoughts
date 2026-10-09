# 管家（guanjia）开发规格 v2

> 2026-09-29 深度访谈定稿，用户已批准开工 S1。架构总图见面板「自部署 AI · 定型架构」方案 B 页。

## 目标

在家里电脑上，用 OpenClaw 当大脑，做一个走过去就能说话、随时能被打断、能记事提醒和替你找片开游戏的语音管家。

## 能力线（拓扑）

| # | 能力线 | 切片 |
|---|---|---|
| 1 | 听：走过去说话 → 文字进网关会话（VAD、流式 ASR、声纹放行） | S1 |
| 2 | 说与打断：回复出声 + 它说话时一开口就停（AEC + stop） | S1 |
| 3 | 大脑接线：人设、"≤2 句"规则、家庭事实/规则/时间线结构化文件、embedding | S1 最小版 / S2 |
| 4 | 做事与安全：执行目标 + L0~L3 分级 + 确认通道 | S2 L1 / S3 完整 |
| 5 | 隐私闸门：脱敏钩子、上云记录 | S3 |
| 6a | 显式主动：记一下/到点提醒 → cron | S2 |
| 6b | 从记忆长出来的主动：夜间提炼周期性事件 → 草案 → 口头确认 → cron；天气雨雪规则 | S3（窄版） |
| 7 | 运行态：开机自启（已成）、游戏态静默、离线区间补写 | S4 |
| 8 | 评测与数据 | 暂缓 |

## 自写三件

```
语音守护进程（常驻）             分级中间层（OpenClaw hook/skill）   脱敏钩子（OpenClaw hook）
├─ 麦 → AEC → VAD → 声纹 → 流式 ASR
├─ 文字 → POST /v1/chat/completions（OpenClaw 网关）
├─ 回复文本 → 按句切 → IndexTTS-2.5 逐句合成 → 播放队列
└─ 打断：VAD 检出开口 → 清队列、停播放、abort 请求
```

## S1 验收（中档）

- [ ] 桌前 1~2 米说一句，2 秒内开口回复，10 次成 9 次
- [ ] 它说话时我开口，0.5 秒内停，10 次成 9 次
- [ ] 未注册声音（电视、访客）不触发、不进会话
- [ ] 识别文本进入 OpenClaw 会话
- [ ] 回复默认不超过两句，长内容先问要不要展开

## S2 验收

- [ ] "记一下明天下午三点交房租"，到点音箱条提醒；"我之前说过啥"能答
- [ ] "总结一下这篇/查一下某某，念给我听"，出声念，可打断
- [ ] "找个免费的某某电影"打开浏览器到可播放页；"开某游戏"启动，属 L1 直接做

## 技术选型与兑底（Windows + Python 3.12，均核过轮子；TTS 在 WSL2）

| 环节 | S1 选型 | 兑底 |
|---|---|---|
| 流式 ASR | sherpa-onnx streaming zipformer zh int8 (2025-06-30, 154M) | xlarge int8 726M 提精度 |
| Qwen3-ASR-0.6B | 后置 S3：WSL2 + vLLM 做异步整句复核 | — |
| VAD / 声纹 | sherpa-onnx Silero VAD + CAM++ | — |
| AEC | pyaec（speexdsp 绑定，win 轮子） | webrtc-apm 源码编译；再不行 S1 临时半双工 |
| 参考信号 | 自己播放的 PCM（首选）/ pyaudiowpatch WASAPI loopback | — |
| TTS | IndexTTS-2.5（WSL2 Ubuntu，uv，端口 18791，RTF 0.69~0.80） | IndexTTS 2.0（Windows，端口 18790，RTF 1.1~1.4）；再不行 sherpa-onnx 离线 TTS（Matcha 中文） |
| TTS 流式 | 按句切、逐句合成、边合成边播 | — |
| 播放/打断 | sounddevice 播放队列；stop = 清队列 + 停流 | — |
| 网关接口 | POST /v1/chat/completions（gateway.http.endpoints.chatCompletions.enabled） | 不支持 stream 则整句后合成 |
| 记忆 embedding | 硅基流动 Qwen/Qwen3-Embedding-0.6B（memory.search.provider=openai + remote.baseUrl） | — |

## 约束

- 代码：`F:\coding\guanjia`，本地 git，暂不接远程
- 运行时：守护进程为 Windows 原生 Python 3.12 + uv；TTS 默认在 WSL2（计划任务 GuanjiaTTS-WSL 前台挂 wsl.exe，WSL 后台进程会随 SSH 会话消失）；GPU 给 TTS（ASR 先 CPU）
- 本机 HTTP 客户端一律 trust_env=False，避免被 Clash 代理劫持成 502
- 协作：公司侧通过 SSH 写代码与自动测试；需要开口的验证弹到家里窗口，用户在家时验
- 硬约束：能随时打断；不买设备；回答默认 ≤2 句

## 事前验尸对应动作

- 怕听不准/误触发：声纹放行、家庭事实纠错提示词、"在对我说吗"判定三层，S1 就上
- 怕没事可做：S2 只做用户点名的三件真事，做完用两周再定第四件

## 主动侧硬规则

正在打游戏不开口（D26）、深夜不开口、开口前一声短提示音；所有从记忆长出来的主动行为必须经一次口头确认才生效，隐式信号只能收紧（D15）。
