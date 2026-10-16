# 家庭管家「语音模块」来龙去脉（2026-10-10 只读整理）

- 来源：Claude Code 会话 `f1a0ad24-21a4-4180-92f4-e222c3fe1be6.jsonl`（复制到 `%TEMP%\guanjia-audit\session-copy.jsonl` 后解析），以及同一会话的子 agent 记录 `…\f1a0ad24…\subagents\*.jsonl`（scv-voice / fix-gate-barge / fix-latency-voice / fix-asr / front-back / s4-runtime 等）和该会话写的记忆文件 `C:\Users\moxue\.claude\projects\D--coding-inksnow-tvs-AIConfig\memory\guanjia-s1-progress.md`。
- 时间写法：`09-30 15:23Z` 是记录里的 UTC 时间戳；括号里是**家里时间（UTC+8）**，家里日志（voice.log 等）用的是家里时间。
- 全程只读：没改文件、没重启、没发模型/微信请求、没启动 WSL、没打印密钥。

---

## 0. 一句话结论：东西没丢，是"从来没有自启 + 会话被封停后没人再拉起"

1. **文件都在**：SCV 参考音（`scv_zh.wav`、`scv_zh_clean.wav`）、声纹库 `speakers.npz`、启动脚本 `voice.ps1` / `run-voice.vbs` / `run-wsl-tts.vbs`、代码全部还在被控机上（见第 7 节）。
2. **语音链路从来没有开机自启**：真正跑过的守护进程一直是**一次性**计划任务 `GuanjiaVoiceTest`（触发器 One Time Only，靠 AI 手动 `schtasks /Run /I`），TTS 也是一次性任务 `GuanjiaTTS-WSL`。唯一开机自启的是 `OpenClaw Gateway`。
3. **`GuanjiaVoice` 是故意"建即禁用"的**：09-30 14:30Z（家里 22:30）由主会话在用户放行后创建并立刻 `/DISABLE`，理由是"麦克风/打断没稳定前不真跑"；10-01 06:07 家里时间又明确说"等打断这些坑填完稳定了再开"。之后再没人启用。
4. **工作中断点**：10-01 01:26:33Z（家里 09:26）Claude Code 回复 `Your account is on hold and can't use Claude Code`，用户 01:27Z 再问一次同样被拒，会话到此结束。被控机 `voice.log` 最后一行 10-01 09:25:46、心跳 `last-seen.txt` 09:28:48，之后这台机器重启/关机后没有任何东西再把 TTS 和守护进程拉起来。
5. **SCV 音色的配置不在 Windows 侧，而在 WSL 里**：`~/guanjia-tts/env` 里的 `export GUANJIA_TTS_VOICE=scv_zh_clean`。WSL 不运行，TTS 不在，就"听不到 SCV"。WSL 侧文件本次**未核对（WSL 未运行）**，但 F 盘上的 `tts_wsl.log` 最后一次加载记录是 `ref=/home/inksnow/guanjia-tts/voices/scv_zh_clean.wav`。

---

## 1. 时间线（关键事件 · 验证结果 · 用户反馈）

### 前置（09-24 ~ 09-29）
| 时间 | 事件 |
|---|---|
| 09-24 23:32Z | 用户要求从公司电脑连家里电脑跑命令；按 Tailscale + SSH 方案搭，SSH 别名 `home`（家里用户 `inksn`，机器名 inksnow）。 |
| 09-25 13:59Z~16:16Z | 选型：家里装 Claude Code + **原版 OpenClaw 2026.9.6**（不用腾讯 QClaw），模型走硅基流动 DeepSeek-V4-Flash；微信双账号接入 OpenClaw。 |
| 09-29 10:05Z | 用户：没有摄像头，麦克风和音响是一体的（EDIFIER Halo Soundbar）。定 D27：软件 AEC + 打断，回答默认 ≤2 句。 |
| 09-29 10:40Z | 用户："B 作为现行，A 作为后续迭代，B 我们尝试开始开发吧" → 方案 B「借壳 OpenClaw」开工。 |
| 09-29 11:01Z | 深度访谈产出规格：S1 听说打断 / S2 三件真事 / S3 安全隐私 / S4 运行态；S1 验收"1~2 米说一句 2 秒内回复、0.5 秒内可打断、未注册声音不触发"。 |
| 09-29 11:10Z | 选型核定：流式 ASR = sherpa-onnx zipformer 中文 int8；VAD = Silero；声纹 = CAM++；AEC = pyaec(speexdsp)；TTS = IndexTTS 2（按句切、逐句合成）；大脑走 OpenClaw `POST /v1/chat/completions`。仓库 `F:\coding\guanjia`（本地 git，**一直没提交**）。 |
| 09-29 11:30Z | 卡点：音箱条麦电平极低（约 -80 dBFS）。用户："有没有可能麦克风有问题？我玩游戏同学也经常说我声音小"。11:43Z 用户："麦克风声音小之后再解决"。 |
| 09-29 12:11Z~12:24Z | IndexTTS 2 装在 Windows `F:\coding\index-tts`，服务 `GuanjiaTTS` 端口 18790；实测 RTF 1.1~1.4，太慢。参考音是 Windows Huihui 合成的占位音 `tts_server/voices/ref.wav`。 |

### 09-30（家里时间 19:00 ~ 次日 01:02）
| 时间 | 事件 / 验证 / 反馈 |
|---|---|
| 11:01Z~12:31Z（19:01~20:31） | TTS 搬进 **WSL2 Ubuntu**：`~/index-tts`、权重 `checkpoints_25`（**IndexTTS-2.5**），服务 `~/guanjia-tts/tts_server.py` 端口 **18791**，计划任务 `GuanjiaTTS-WSL`（隐藏窗口 wscript 包一层）。RTF 降到 0.69~0.80；torch.compile 只快一点、预热 41s，默认关；DeepSpeed 因无 nvcc 搁置。守护进程默认改连 18791。 |
| 12:33Z~12:41Z | Opus 只读评审 voice.py 找出 16 个缺陷（3 P0），修了 13 个（打断门槛 80ms、0.5s 预滚防丢首字、播放代次防旧句复活等）。 |
| 13:24Z | 用户："s2 s3 s4 都开始吧。耳麦的我试了试效果不行，还得嘴贴近麦克风"。 |
| 13:38Z | S4 回执：状态机/游戏静默/离线补写完成；**`GuanjiaVoice` 子 agent 创建被权限分类器拒**，要求用户亲手建并禁用。 |
| 14:29Z~14:30Z | 用户："结束了 你给我跑" → 主会话装 S3 守卫插件、**创建 `GuanjiaVoice` 并立即禁用**、重启 WSL TTS（/unload 实测显存 7.7GB→1.3GB）。 |
| 14:41Z | 用户："那就用我的音响话筒试试啊，或者应该是我用什么输入设备都能自动生效"。 |
| 14:46Z~15:03Z（22:46~23:03） | 弹窗做 5 秒电平检查（几次弹窗失败，用户"没窗口出来"）。15:03Z 用户"按了，录完了"；结果 `peak -12.3 dBFS，建议增益 x3`，录音存 `tmp/mic_check.wav`。 |
| 15:05Z~15:17Z（23:05~23:17） | **声纹注册**（见第 3 节）。`speakers.npz` 1028 字节，写于家里 23:16:04。 |
| 15:17Z~15:25Z（23:17~23:25） | 创建一次性任务 `GuanjiaVoiceTest` 跑 `C:\Users\inksn\voice.ps1`；**23:23 整条链路首次真机跑通**（声纹放行 0.64~0.73、杂音 0.48 被拒；首响 6~7 秒；一次未出声就"打断"的误触发）。 |
| 15:29Z | **用户反馈**："1 对我声纹识别不准，就是我说的也拒绝，我远点或者有点情绪更会拦截我；2 我说话她不会暂停；3 说话前加载很慢，说话速度也很人机感，也比较慢"。 |
| 15:38Z | 修：声纹阈值 0.5→0.40（他那句 0.48 被拒），打断不再看声纹（出声后 300ms 保护、能量 > 回声底噪 ×2.5、持续 180ms）。 |
| 15:43Z | **用户反馈**："远一点就听不懂我说话了，转换成文本效果还是不好，会有错误，再者他不说话了，我接下来的每次说话都会触发 stop 用户续说"。根因之一：TTS 服务被另一个 agent 的实验占着，30s 超时。 |
| 15:47Z | 用户："他怎么不说话？我看弹窗有文字回答我，但是没说话"。 |
| 15:48Z~15:51Z | ASR 对比：本地 **SenseVoice int8** 错字率 0.20（流式 0.37），接入为"整段精识别"；加 3 倍软件增益；续说窗口 2.5s；TTS 超时 12s。 |
| 15:50Z | 用户："这个声音我能选别的吗？或者自定义"。 |
| **15:52Z** | 用户："不不不，我想要的是**星际争霸2里面 SCV 的那个声音**，网上给我找对应录音，用这个的音色"。15:54Z："**我只要中配**"。 |
| 15:54Z~15:56Z | 用户："声纹拒绝阈值再低点" → **阈值降到 0.30**（23:55:43 生效）。 |
| 15:57Z~16:11Z | 发现多实例抢麦（`/End` 不带走子进程）→ `voice.ps1` 改为 Job Object（家里 10-01 00:08）；试 ERes2NetV2 声纹模型，区分度反而更差，**默认回 CAM++**。 |
| 16:06Z~16:14Z | TTS 提速：扩散步数 25→15、束搜索 3→1、语速系数 0.92，首句 8 字从 2.6s→1.2s。模型对比 Qwen3.8-27B 质量最好。 |
| 16:16Z~16:20Z | 用户问成本"这个模型有点贵呀"；16:20Z 用户："不，先用着这个大脑" → `voice.ps1` 里 `GUANJIA_VOICE_MODEL=""`（不加换模型头）。 |
| 16:26Z~16:31Z（00:26~00:31） | **SCV 中配 `scv_zh.wav` 做好并切为默认**（WSL env 写 `GUANJIA_TTS_VOICE=scv_zh`，第一次没加 export 无效，16:30Z 修好）。16:29Z 用户："那我现在能试试这个 scv 声色的了没"。 |
| **16:35Z（00:35）** | **用户反馈**："声音转文字效果奇差，内容总是错的，以及 **scv 效果一般，音色一样，但是明显有录音感和背景杂音**。以及，会高概率出现**出文字但是不出声音**"。 |
| 16:37Z | 日志发现播报期间麦克风读数全零 → 怀疑音响半双工/DSP。 |
| 16:39Z~16:42Z | 用户提出"前台快回话 + 后台干活"两段式；16:42Z："开工吧，前台后台都做。半小时后停工……**模型就全先用 deepseek 那个便宜**"。 |
| 16:47Z~16:53Z（00:47~00:53） | 半双工探针：音响内部有 DSP 噪声门/回声消除；speex AEC 伤识别 → **AEC 默认关**（00:51:36）；输出流常开 -70 dBFS 保活噪声、每段音频落盘 `tmp/segments`。关 AEC 后声纹分 0.75→0.61，建议重录（**没重录**）。 |
| 16:53Z~16:57Z（00:53~00:57） | **SCV 洗净版 `scv_zh_clean.wav` 切为默认**（DeepFilterNet3 降噪，DNSMOS 背景分 1.9→4.0）。 |
| 17:00Z~17:01Z（01:00~01:01） | 前台/后台双线大脑上线（前台直连硅基 DeepSeek-V4-Flash 首字 0.5~0.9s）。01:02 全部停工，写停工快照。 |

### 10-01 早上（最后一次真实运行）
| 时间 | 事件 |
|---|---|
| 22:05Z（06:05） | 用户："我重启了，语音管家怎么开"。 |
| 22:06:14Z（06:06） | AI 远程执行：`schtasks /Run /TN "OpenClaw Gateway"`、`/Run /TN GuanjiaTTS-WSL`、`/Run /I /TN GuanjiaVoiceTest`。06:06:44 守护进程 listening（配置见第 5 节）。 |
| 22:07Z | AI 告诉用户以后开机自己跑这三条；并说 `GuanjiaVoice` "当时留着没启用，等打断这些坑填完稳定了再开"。日志里播报时 `raw_peak=0.013` 非零 → 软件打断"还有戏"。 |
| 22:07Z | **用户**："嘿，我试了一下，他循环了"（前台反复说"我这就去查天气"）。 |
| 22:24Z / 22:34Z（06:24 / 06:34） | front-back 修复后重启两次。查天气卡住根因：守卫插件不认 `tool_call` 包装器 + 微信审批发不出去（审批目标微信 ID 大小写不一致）。守卫修复已备好，但部署被权限策略拦下，等用户说"部署"。 |
| 22:35Z | 确认链路上线；最后一步仍卡在微信审批 90 秒超时。 |
| 10-01 01:26Z / 01:27Z（09:26 / 09:27） | 用户："哈进度了？s"、"？" → 两次都是 **`Your account is on hold and can't use Claude Code`**。会话结束。 |
| 家里 09:25:46 / 09:28:48 | voice.log 最后一行 / 心跳 `last-seen.txt` 最后写入，之后守护进程不再运行。 |
| 10-10 09:31Z | 记录里只剩一条"上个会话未完成的后台命令已停止"的系统通知。 |

---

## 2. SCV 音色

- **来源**（子 agent scv-voice，09-30 15:53Z~16:53Z）：
  - 主料：B 站「国服 SC2 人族单位语音演示」**BV1mx411c7pS**（第 1P 约 513~542 秒、第 3P 开头 0~5 秒），游戏内录音。
  - 「SCV准备就绪」取自 **BV1uK411p7Px**（像解包原音，句间纯静音）。
  - 看过但没用：爱给网（抓不下来）、BV1Jm4y1B7nm（几乎全英文）、BV1TxHYeDEFJ（星际 1 英文）、BV1Uh41157En（UP 主解说）。英配（Fandom wiki ogg）做过一版，16:27Z 前按用户"只要中配"**已删除**。
  - 筛选：15 条中配台词逐条测信噪比，去掉带无线电杂音（4~13dB）和带惊吓/不耐烦情绪的句子（IndexTTS2 会学参考音的情绪）。
- **文件**：
  - `scv_zh.wav`：「SCV准备就绪」「任务艰巨」「当然可以」「他们在无线电里都叫我大钻头」，9.82s，24k/16bit/单声道，信噪比 23~27dB（16:27Z 版本；16:26Z 曾有一版 8.45s 的其他台词组合，已被覆盖）。
  - `scv_zh_clean.wav`：「他们在无线电里都叫我大钻头」「任务艰巨」「你说了算」，6.21s；DeepFilterNet3（衰减上限 24dB，不限会吞掉"大""任务"）+ 80Hz 高通 + 句间置零。**降噪在公司电脑上做**，合成在家里 WSL。
  - 试听：`F:\coding\guanjia\tmp\scv_zh_sample.wav`（旧）、`scv_zh_clean_sample.wav`（洗净）、`scv_zh_calm_sample.wav`（洗净 + 情绪向量"平静"0.6）；试听脚本 `tmp\scv_try.py`、`tmp\scv_clean_try.py`。
- **存放路径（Windows 和 WSL 各一份）**：
  - Windows：`F:\coding\guanjia\tts_server\voices\scv_zh.wav`、`scv_zh_clean.wav`
  - WSL：`~/guanjia-tts/voices/scv_zh.wav`、`scv_zh_clean.wav`（即 `/home/inksnow/guanjia-tts/voices/`）。**正式服务读的是 WSL 这份。**
  - 公司电脑另有完整副本和原始素材：`C:\Users\moxue\AppData\Local\Temp\claude\D--coding-inksnow-tvs-AIConfig\f1a0ad24-…\scratchpad\scv\`（含 `zh\` 原始 B 站音频、`enh\` 降噪中间件、`build.py`/`build_clean.py`/`enhance.py`）。
- **IndexTTS 配置怎么指向它**：
  - 启动链：`GuanjiaTTS-WSL` → `wscript //B F:\coding\guanjia\tmp\run-wsl-tts.vbs` → `wsl.exe -d Ubuntu -- bash /home/inksnow/guanjia-tts/run.sh`。
  - `run.sh` 设 `GUANJIA_TTS_REF=$HOME/guanjia-tts/voices/ref.wav`、`GUANJIA_TTS_VERSION=2.5`、`GUANJIA_TTS_MODEL_DIR=checkpoints_25`、`GUANJIA_TTS_PORT=18791`，然后 `. ~/guanjia-tts/env`。
  - `~/guanjia-tts/env` 写 `export GUANJIA_TTS_VOICE=scv_zh_clean`（16:53:33Z 由 `scv_zh` 改为 `scv_zh_clean`）。
  - `tts_server.py`（fix-latency-voice 改）：`GUANJIA_TTS_VOICE=名字` → 用 `voices/名字.wav` 替换默认 ref；单次请求可带 `{"voice":"scv_zh_clean"}`；默认 15 步扩散、束搜索 1、语速系数 0.92。
  - 证据：F 盘 `tmp\tts_wsl.log` 最后加载行 `loaded in 15.3s, ref=/home/inksnow/guanjia-tts/voices/scv_zh_clean.wav`。
- **效果确认了吗**：
  - `scv_zh`：用户 16:35Z 亲耳听过，"**音色一样，但是明显有录音感和背景杂音**"。
  - `scv_zh_clean`：只有客观分（DNSMOS 背景分 1.9→4.0，ASR 回读一字不差），**记录里用户没有给过试听反馈**。平静情绪版要正式服务支持情绪向量才能用，"留到明天"，没做。

## 3. 声纹

- **怎么录的**：09-30 15:04Z~15:17Z（家里 23:04~23:17）。主会话写 `C:\Users\inksn\enroll.ps1`（按回车后连续说 12 秒，`uv run python -m guanjia.enroll --name 我 --seconds 12` 跑**两轮叠加**），用一次性计划任务 `GuanjiaEnroll`（`/IT` 桌面会话）弹窗。中间修过一次采样率问题（改为 16k 直接采集，15:11Z）。设备是音箱条麦 `Microphone (EDIFIER Halo Soundbar)`。
  - 15:14Z 用户说"没看见啊"（窗口没弹出来），15:15:47Z 重建任务后弹出；`speakers.npz` 写于家里 **23:16:04**。15:16Z 还从 SSH 会话直接跑过一次 enroll.ps1，两轮都提示没检测到足够人声，没写库（SSH 会话拿不到桌面音频）。
  - 录的时候 speex AEC 是开着的，还没有 3 倍增益。增益影响很小（余弦 0.993，不用重录）；但 **00:51 关掉 AEC 后分数 0.75→0.61**，建议在 AEC 关闭状态下重录，**记录里没重录**。
- **存在哪**：`F:\coding\guanjia\data\speakers.npz`（说话人只有一个「我」，CAM++ 192 维）。
  - `speakers.campp.npz`：换模型实验前（家里 00:04）的 CAM++ 原库备份，与原始注册同一内容。
  - `speakers.eres2netv2.npz`：ERes2NetV2 实验库，**是用 2 秒的 `mic_check.wav` 自动注册的**，不是用户正式录音。
  - 家里 00:15 执行 `move speakers.npz speakers.eres2netv2.npz && copy speakers.campp.npz speakers.npz`，所以现在的 `speakers.npz` = 23:16 那次真实注册（修改时间仍是 23:16:04）。
  - 模型文件：`F:\coding\guanjia\models\3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx`（默认）；`GUANJIA_SPK_MODEL=eres2netv2` 可切。
- **放行阈值变化**：0.50（初始，写死在 speaker.py）→ 0.40（15:38Z）→ **0.30**（15:55Z，`voice.py` 常量 `SPK_THRESHOLD = 0.30`，`--spk-threshold` 可调，另有 `--gate-mode log` 只记分不拦）。
  - 实测：主人 0.60~0.74，离远/带情绪掉到 0.48，噪声段 0.06~0.32。AI 说 0.30 是"敢放的底线"，再拒就改成"只记分不拦"。
  - 副作用：10-01 06:07 背景杂音/旁人说话 0.41~0.48 也被放行（误放）。
- **打断不看声纹**（15:38Z 起）：出声后 300ms 保护期 + 打断专用 VAD + 能量 > 回声底噪 ×2.5 且 ≥0.003，累计 180ms 就停。

## 4. 语音链路怎么启动

| 组件 | 计划任务（全在被控机） | 实际命令 | 端口 / 日志 |
|---|---|---|---|
| OpenClaw 网关（大脑 + 工具 + 微信） | `OpenClaw Gateway`，**登录时自启** | `C:\Users\inksn\.openclaw\gateway.vbs` | 127.0.0.1:**18789**（`/v1/chat/completions`） |
| TTS（IndexTTS-2.5，WSL） | `GuanjiaTTS-WSL`，**One Time Only**（不自启） | `wscript.exe //B F:\coding\guanjia\tmp\run-wsl-tts.vbs` → `wsl.exe -d Ubuntu -- bash /home/inksnow/guanjia-tts/run.sh` → `~/index-tts/.venv/bin/python -u ~/guanjia-tts/tts_server.py` | **18791**（`/tts` `/health` `/unload` `/load`）；日志 `F:\coding\guanjia\tmp\tts_wsl.log`；加载约 15s |
| TTS 备用（IndexTTS 2.0，Windows） | `GuanjiaTTS`，One Time Only | `wscript.exe //B F:\coding\guanjia\tmp\run-win-tts.vbs` | 18790；约 5 分钟加载 |
| 语音守护进程（实际在用） | **`GuanjiaVoiceTest`**，One Time Only，`/IT` 桌面会话 | `powershell.exe -NoExit -ExecutionPolicy Bypass -File C:\Users\inksn\voice.ps1` → Job Object 绑定 + `uv run python -m guanjia.voice -v --log-file F:/coding/guanjia/tmp/voice.log`；设 `GUANJIA_TTS_URL=http://127.0.0.1:18791/tts`、`GUANJIA_VOICE_MODEL=""` | 日志 `tmp\voice.log`，窗口转录 `tmp\voice-window.txt` |
| 语音守护进程（开机自启，未启用） | **`GuanjiaVoice`**，At logon，**Disabled** | `wscript.exe //B F:\coding\guanjia\scripts\run-voice.vbs` → `cmd /c cd /d F:\coding\guanjia && F:\python\Scripts\uv.exe run python -m guanjia.voice >> tmp\voice.log` | 同上 |

- **记录里最后一次教给用户的手动开启方法**（10-01 22:07Z）：
  ```
  schtasks /Run /TN "OpenClaw Gateway"
  schtasks /Run /TN GuanjiaTTS-WSL
  schtasks /Run /I /TN GuanjiaVoiceTest
  ```
- **重启 TTS 的正确姿势**（记忆文件）：`schtasks /End` 只杀 wscript 壳，必须先 `wsl -d Ubuntu -- pkill -f guanjia-tts/tts_server.py` 再 `/Run`。家里 WSL 默认发行版是 docker-desktop，所以必须写 `-d Ubuntu`。
- **`GuanjiaVoice` 为什么是禁用的**：
  1. 09-30 13:38Z，S4 子 agent 的 `schtasks /Create` 被权限分类器判为"未授权持久化"而拒绝。
  2. 方案一直是"**建完即禁用，麦克风没解决前不会真跑**"（13:27Z 派工约束、14:16Z/14:29Z 给用户的命令里都写着 `/Change /DISABLE`）。
  3. 14:29:53Z 用户说"结束了 你给我跑"，**14:30:16Z 主会话亲自创建并禁用**（`schtasks /Create … /SC ONLOGON /IT /F` + `/Change /DISABLE`）。
  4. 10-01 22:07Z AI 重申"等打断这些坑填完稳定了再开"。
  5. 之后会话被封停，没人启用；被控机显示 Last Run 1999/11/30、Last Result 267011（从未运行）。
- **如果以后要启用 `GuanjiaVoice`**：`run-voice.vbs` 和 `voice.ps1` 不一样。它没有 Job Object（`/End` 会残留 python），没有 `GUANJIA_VOICE_MODEL=""`，没有 `--log-file`；而且 TTS 任务不是登录自启，守护进程起来时 TTS 可能还没在。需要先对齐。

## 5. 最后一次已知工作状态与遗留问题

**最后一次运行配置**（voice.log 10-01 06:34:38 启动行，与 06:06:44、06:24:10 相同）：
`speakers=['我'](3dspeaker_speech_campplus_sv_zh-cn_16k-common), gate=enforce@0.30, mic_gain=3.0, aec=off, barge=180ms guard=300ms ratio=2.5, tts_timeout=12s；output stream always-open, comfort noise -70 dBFS, lead 150ms, dump tmp\segments`。

另外，按停工快照：front-back 双线大脑默认开启（前台 `front.py` 直连硅基 DeepSeek-V4-Flash，两段式协议 `<<task>>` + JSON；后台 `backoffice.py` 每任务一个网关会话；`GUANJIA_BRAIN_MODE=legacy` 可回旧单线）；SenseVoice 整段精识别（`asr_final.py`）；系统提示每轮读 `tmp\voice_sys.txt`（禁止念思考过程、每句 ≤30 字、做不到就老实说）；TTS 默认 `scv_zh_clean`、15 步、语速 0.92。

**用户说过效果不好的地方，以及最后状态**：
| 用户原话（时间） | 做了什么 | 最后状态 |
|---|---|---|
| "声纹识别不准，我说的也拒绝；远点/有情绪更会拦"（15:29Z） | 阈值 0.5→0.4→0.3；空文本段先丢弃 | 不再拦他，但开始误放背景人声（06:07）；关 AEC 后应重录声纹，未做 |
| "我说话她不会暂停"（15:29Z） | 打断改能量 + VAD；发现音响 DSP 噪声门 | 06:07 日志播放时 raw_peak 0.013 非零，"软件打断还有戏"；**用户没再做插嘴实测** |
| "说话前加载很慢，说话很人机感也比较慢"（15:29Z） | 首句 8/20 字切、TTS 15 步、前台直连 | 前台首字 0.5~0.9s；人机感改为 SCV 音色 |
| "远一点就听不懂"（15:43Z） | 3 倍软件增益 | 软件上限；根因是音箱条麦弱，**远场麦是 S1 唯一硬阻塞**（耳麦也要贴嘴） |
| "转换成文本效果还是不好"（15:43Z）/"声音转文字效果奇差，内容总是错的"（16:35Z） | SenseVoice 精识别；关 speex AEC（它伤识别）；段音频落盘 + `seg_eval.py` | 未验证；待用户多说几句后跑 `uv run python -m guanjia.seg_eval` |
| "每次说话都触发 stop 用户续说"（15:43Z） | 续说窗口 2.5s；多实例问题用 Job Object 修 | 已修 |
| "他怎么不说话？有文字没声音"（15:47Z）/"高概率出文字但不出声音"（16:35Z） | 叫停占用 TTS 的实验；输出流常开 + 保活噪声防音响休眠 | 未经用户验证 |
| "scv 效果一般，音色一样，但明显有录音感和背景杂音"（16:35Z） | 做 `scv_zh_clean` 并设为默认 | 用户未反馈 |
| "他循环了"（10-01 22:07Z） | 已派任务不进历史、杂音回"没听清" | 06:24 修好 |

**其他遗留**：
- 后台查天气卡住：守卫把 `tool_call` 包装器判成 L2，`exec curl` 是 L3 要发微信审批，但审批**一直发不出去**（配置里审批目标微信 ID 全小写，网关会话键大小写混合）。修复文件在公司电脑 `scratchpad\lat\guard_new\`，**部署被拦，等用户说"部署"**。
- "打开记事本"后台 150s 超时（网关 exec 等 GUI 退出），可能残留测试打开的记事本。
- 心跳文件偶发 PermissionError（不影响）。
- 精简语音 agent 方案（`oc8.ps1`）未应用，被控机上已不存在该文件。
- 代码在家里**从未 git commit**；公司电脑 `scratchpad\guanjia\` 有一份副本。

## 6. 和语音无关、但可能不知道的重要决定

- **硬件**：家里电脑 inksnow，RTX 4070 Ti SUPER 16GB / 32GB 内存；音频是 EDIFIER Halo Soundbar（USB 一体麦 + 喇叭，内部有 DSP 噪声门/疑似回声消除，麦很弱）；VGN 耳麦要贴嘴；**无摄像头**；用户硬约束"暂不买设备"、"必须能随时打断，最忌讳 AI 说半天"。
- **网络/远程**：Tailscale + SSH（别名 home）；路由器 H3C Magic RT3000 移动定制版**无网络唤醒**，远程开机搁置；Windows 快速启动开着；国际网只能走 Clash 7890；httpx 必须 `trust_env=False`（否则 Clash 返回 502）；WSL 默认发行版是 docker-desktop。
- **唤醒词**：记录里**没有唤醒词设计**，是"走过去就说"的常开监听 + 声纹放行。
- **人设**：`tmp\voice_sys.txt`（"你是家里的语音管家，通过音箱和用户说话。只输出要直接念出来的口语句子……"，每句 ≤30 字、不念思考过程）；回答默认 ≤2 句、长内容先问要不要展开；试听台词称用户为"主人"。OpenClaw 工作区 `AGENTS.md` 末尾加了管家规则（原文件备份 `AGENTS.md.bak-s2`）。`family-facts` 家庭事实文件是空的，等用户填。
- **架构**：方案 B「借壳 OpenClaw」为现行（09-29 10:40Z），方案 A 自研编排转后续。面板 https://claude.ai/artifact/KTawgFYi2MsoHzxR5Qhk3X （B 页有数据库驱动的"开发进度"P1~P18）。
- **模型**：硅基流动 DeepSeek-V4-Flash 为主（provider 加了 `enable_thinking:false`）。对比测试 Qwen3.8-27B 质量最好（约 ¥3/¥12 每百万 token，估 ¥55/月），但**用户 16:42Z 定"全先用 DeepSeek，便宜"**。embedding 用 `Qwen/Qwen3-Embedding-0.6B`。本地模型不给干活 agent 用。
- **安全（S3）**：分级插件 guanjia-guard 已装（L0/L1 直接做、L2 先问、L3 发微信审批；cron 触发降一档；改 `guanjia/` 自身一律 L3）。脱敏本地代理（127.0.0.1:18797）写了**没切流量**。装插件前 exec 是全局放开的。
- **运行态（S4）**：游戏白名单只有 `League of Legends.exe`（故意不放 LeagueClient）；进游戏态自动释放 ASR、TTS `/unload` 让出约 6GB 显存；手动游戏态 3 小时自动过期。
- **用户的工作偏好**（记忆文件）：并行/复杂派 Opus，简单执行派 Sonnet，**绝不用 Haiku**；他打游戏时不弹窗、不出声、不抢焦点；**不按进程名杀进程**；同模块复用同一个子 agent。
- **密钥**：09-30 12:46Z 一个 Sonnet 子 agent 读网关 token 时，硅基流动 key 短暂进过它的记录（随后删了本地副本），AI 建议"介意可以去硅基后台换一把"。用户 09-25 也在聊天里直接贴过 key。**建议轮换**（本文不含密钥）。
- 搜狗输入法已装（浏览器是误装，确认从未装上）；安装包 `F:\coding\guanjia\tmp\SogouPinyin_Setup.exe` 可删。

## 7. 被控机只读核对结果（2026-10-10，`ssh home`，只列文件，家里时间）

**SCV 参考音 / 试听**
| 文件 | 大小 | 修改时间 |
|---|---|---|
| `F:\coding\guanjia\tts_server\voices\scv_zh.wav` | 471,564 | 2026-10-01 00:26:44 |
| `F:\coding\guanjia\tts_server\voices\scv_zh_clean.wav` | 298,120 | 2026-10-01 00:51:37 |
| `F:\coding\guanjia\tts_server\voices\ref.wav`（占位音） | 701,064 | 2026-10-01 00:07:53 |
| `…\voices\huihui.wav` / `v05` / `v06` / `v09` / `v11.wav`（其他候选音色） | 701,064 / 346,844 / 299,052 / 265,244 / 377,440 | 2026-09-30 23:51:39 |
| `F:\coding\guanjia\tmp\scv_zh_sample.wav` | 225,324 | 2026-10-01 00:27:21 |
| `F:\coding\guanjia\tmp\scv_zh_clean_sample.wav` | 186,412 | 2026-10-01 00:52:21 |
| `F:\coding\guanjia\tmp\scv_zh_calm_sample.wav` | 198,700 | 2026-10-01 00:52:24 |
| `F:\coding\guanjia\tmp\scv_try.py` / `scv_clean_try.py` | 999 / 1,073 | 00:12:14 / 00:51:46 |
| WSL `~/guanjia-tts/voices/scv_zh*.wav`、`~/guanjia-tts/env`、`~/guanjia-tts/run.sh`、`tts_server.py` | — | **未核对（WSL 未运行）**；`wsl -l -v` 显示 Ubuntu、docker-desktop 都是 Stopped |

（文件大小与记录一致：24k/16bit 单声道 → 9.82s 和 6.21s。）

**声纹**
| 文件 | 大小 | 修改时间 |
|---|---|---|
| `F:\coding\guanjia\data\speakers.npz`（现用，CAM++，「我」） | 1,028 | 2026-09-30 23:16:04 |
| `F:\coding\guanjia\data\speakers.campp.npz`（同一份的备份） | 1,028 | 2026-09-30 23:16:04 |
| `F:\coding\guanjia\data\speakers.eres2netv2.npz`（实验库，2 秒 mic_check 注册） | 1,028 | 2026-10-01 00:04:22 |
| `F:\coding\guanjia\tmp\mic_check.wav`（电平检查录音，也是实验库样本） | 441,044 | 2026-09-30 23:03:23 |
| `F:\coding\guanjia\models\3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx` | 28,281,138 | 2026-09-29 19:14:57 |
| `F:\coding\guanjia\models\3dspeaker_speech_eres2netv2_sv_zh-cn_16k-common.onnx` | 71,441,526 | 2026-10-01 00:02:52 |
| `C:\Users\inksn\enroll.ps1` | 511 | 2026-09-30 23:04:18 |

**启动脚本 / 日志**
| 文件 | 大小 | 修改时间 |
|---|---|---|
| `C:\Users\inksn\voice.ps1`（GuanjiaVoiceTest 用，Job Object 版） | 2,305 | 2026-10-01 00:21:16 |
| `C:\Users\inksn\voice.ps1.bak`（旧版） | 669 | 2026-09-30 23:23:14 |
| `F:\coding\guanjia\scripts\run-voice.vbs`（GuanjiaVoice 用） | 273 | 2026-09-30 21:36:32 |
| `F:\coding\guanjia\tmp\run-wsl-tts.vbs`（GuanjiaTTS-WSL 用） | 106 | 2026-09-30 20:30:10 |
| `C:\Users\inksn\run.sh`（WSL run.sh 的 Windows 侧中转副本） | 458 | 2026-09-30 20:21:25 |
| `F:\coding\guanjia\tmp\voice_sys.txt`（人设提示词） | 571 | 2026-10-01 00:08:56 |
| `F:\coding\guanjia\tmp\voice.log` | 116,857 | 2026-10-01 09:25:46 |
| `F:\coding\guanjia\tmp\tts_wsl.log` | 39,227 | 2026-10-01 08:52:29 |
| `F:\coding\guanjia\data\last-seen.txt`（守护进程心跳） | 64 | 2026-10-01 09:28:48 |
| `F:\coding\guanjia\src\guanjia\voice.py` / `front.py` / `backoffice.py` | 40,313 / 13,422 / 9,669 | 10-01 06:34:16 / 06:30:50 / 06:30:51 |
| `F:\coding\guanjia\tmp\oc8.ps1`（精简 agent 方案） | — | **不存在** |

**计划任务**
| 任务 | 状态 | 触发 | 上次运行 / 结果 |
|---|---|---|---|
| GuanjiaVoice | **Disabled** | At logon | 1999/11/30（从未运行）/ 267011 |
| GuanjiaVoiceTest | Ready（Enabled） | One Time Only | 2026-10-01 06:34:32 / -1073741510（窗口被关/强制结束） |
| GuanjiaTTS-WSL | Ready | One Time Only | 2026-10-01 06:06:23 / 0 |
| GuanjiaTTS（Windows 2.0） | Ready | One Time Only | **2026-10-01 11:45:14 / -2147020576（0x800710E0 操作员拒绝）**，记录里没有这次运行（发生在会话被封停之后，来源不明） |
| GuanjiaEnroll | Ready | One Time Only | 2026-09-30 23:16:00 / -1073741510 |
| GuanjiaHalfDuplex / GuanjiaProbe / GuanjiaMicCheck | Ready | One Time Only | 10-01 00:43:02 / 0；00:41:33 / 1；09-30 23:03:05 / -1073741510 |
| OpenClaw Gateway | Running | At logon | 2026-10-10 16:54:35 |

**公司电脑上的备份**（`C:\Users\moxue\AppData\Local\Temp\claude\D--coding-inksnow-tvs-AIConfig\f1a0ad24-21a4-4180-92f4-e222c3fe1be6\scratchpad\`）：`scv\scv_zh.wav`（471,564）、`scv\scv_zh_clean.wav`（298,120）、三个试听、`scv\zh\` B 站原始音频、`scv\enh\` 降噪中间件、构建脚本；`voice.ps1`（2,271）、`voice.ps1.orig`（669）；`guanjia\` 代码副本。注意这里是 %TEMP%，可能会被清理。
