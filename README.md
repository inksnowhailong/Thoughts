# guanjia · 家里的语音管家

走过去就能说话、随时能被打断、OpenClaw 当大脑的桌面语音管家。规格见 `docs/spec.md`，架构面板见「自部署 AI · 定型架构」方案 B 页。

## 结构

```
src/guanjia/
  voice.py       语音守护进程：采集 → AEC → VAD → 声纹 → 流式 ASR → 网关 → 按句 TTS → 播放；打断
  asr.py         sherpa-onnx 流式 zipformer（中文）
  vad.py         Silero VAD（sherpa-onnx）
  speaker.py     CAM++ 声纹注册与放行
  brain.py       OpenClaw 网关客户端（/v1/chat/completions，流式）
  textseg.py     流式文本按句切分
  tts_client.py  调本机 IndexTTS 服务（地址由 GUANJIA_TTS_URL 决定）
  ort_preload.py Windows 上先载 pip 的 onnxruntime，避开 System32 的旧版
  aec_probe.py / tone_test.py / rec_test.py / audio_state.py / mic_default.py  音频排障小工具
tts_server/
  tts_server.py  IndexTTS 合成服务（默认 WSL2 跑 2.5，端口 18791；Windows 跑 2.0 备用，端口 18790）
  voices/ref.wav 参考音色（5~15 秒干净人声；换音色只换这个文件）
models/          sherpa-onnx 模型（不入库）
data/            声纹库 speakers.npz（不入库）
```

## 运行

### 1. TTS 服务：WSL2 + IndexTTS-2.5（默认）

- 环境：WSL2 Ubuntu 的 `~/index-tts`，uv 建的 venv；出网只能走清华 PyPI 镜像
- 权重：`~/index-tts/checkpoints_25`；辅助模型 `hf_cache` 从 `checkpoints/` 拷来
- 服务脚本：`~/guanjia-tts/tts_server.py`；启动脚本：`~/guanjia-tts/run.sh`
- 环境变量覆盖写在 `~/guanjia-tts/env`：

| 变量 | 取值 |
|---|---|
| GUANJIA_TTS_VERSION | `2.5` |
| GUANJIA_TTS_MODEL_DIR | `checkpoints_25` |
| GUANJIA_TTS_COMPILE | `0` / `1`（torch.compile） |
| GUANJIA_TTS_ACCEL | GPT 加速引擎开关（需 nvcc，暂不可用） |
| GUANJIA_TTS_DEEPSPEED | DeepSpeed 开关（需 nvcc，暂不可用） |
| GUANJIA_TTS_PORT | `18791` |
| GUANJIA_TTS_REF | 参考音色 wav 路径 |

- 常驻：Windows 计划任务 `GuanjiaTTS-WSL`（桌面会话，`/IT`）前台挂着 wsl.exe 运行，日志 `F:\coding\guanjia\tmp\tts_wsl.log`。WSL 后台进程会随 SSH 会话消失，所以必须由计划任务前台挂住。

```powershell
# 启动 / 停止（Windows 侧）
schtasks /Run /TN GuanjiaTTS-WSL
schtasks /End /TN GuanjiaTTS-WSL

# 手动前台跑（排障用）
wsl.exe -d Ubuntu -- bash -lc "~/guanjia-tts/run.sh"
```

### 1'. TTS 服务：Windows + IndexTTS 2.0（备用）

环境在 `F:\coding\index-tts`，计划任务 `GuanjiaTTS`，端口 18790。加载约需 5 分钟。

```powershell
schtasks /Run /TN GuanjiaTTS
# 或手动：
cd F:\coding\index-tts
uv run python F:\coding\guanjia\tts_server\tts_server.py
```

### 2. 守护进程（本仓库环境）

`tts_client` 按环境变量 `GUANJIA_TTS_URL` 选服务，默认 `http://127.0.0.1:18791/tts`（WSL 2.5）；要用 Windows 2.0 备用时设为 18790。

```powershell
cd F:\coding\guanjia
# 默认走 WSL 2.5（18791）；要用 Windows 2.0 备用：$env:GUANJIA_TTS_URL = "http://127.0.0.1:18790/tts"
# 设备号用 uv run python -c "import sounddevice as sd; print(sd.query_devices())" 查
uv run python -m guanjia.voice --in 20 --out 16
# 调试时关掉声纹放行：--no-gate
```

### 实测速度

| 版本 | RTF | 备注 |
|---|---|---|
| IndexTTS 2.0 | 1.1~1.4 | Windows 与 WSL 相同 |
| IndexTTS-2.5 | 0.69~0.80 | gpt_gen 2~3 秒 |

DeepSpeed 与 accel 引擎都需要 nvcc，WSL 无 root 装不了 CUDA 工具链，搁置。

## 声纹注册

```powershell
uv run python -m guanjia.enroll --name 我 --seconds 12 --in 20
```

## 已知约束

- 音频必须跑在桌面会话里；SSH 会话拿不到设备（远程排障用 `schtasks /IT` 跑）。
- WASAPI 共享模式只接受设备原生采样率（音箱条 48k），链路在 48k 采集、16k 识别。
- 回答默认 ≤ 2 句，由 `voice.py` 的 SYSTEM_PROMPT 约束。
- 本机 HTTP 客户端一律 `trust_env=False`，否则会被 Clash 代理劫持成 502。
- OpenClaw 记忆向量检索接硅基流动 `Qwen/Qwen3-Embedding-0.6B`（`memory.search.provider=openai` + `remote.baseUrl`）。改配置后依次执行 `openclaw gateway restart`、`openclaw memory index --force`。
