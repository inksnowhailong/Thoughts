"""语音守护进程：常开监听 → 增益 → VAD → 流式 ASR → 整段精识别 → 声纹放行 → 主线会话 → 按句 TTS → 播放；随时可打断。

P2 起的调用链（front.py / backoffice.py 已退出调用链，文件暂留，验收通过后再删）：

  音频回调 ──(麦帧, 本块是否在出声)──> 前端线程 ── VAD/ASR/声纹 ──> 提示音 + text_q
                                          └─ 打断检测 BargeDetector ──> stop_speaking()：换代 + 清播放队列
  大脑线程：text_q ──> TurnRunner ──> 网关 /v1/chat/completions（x-openclaw-session-key: agent:main:main）
                        流式文本 → 切句 → speakable 过滤 → TTS → play_q（按代次播放，旧代次直接丢）
                        打断后照样把网关流读完：断开连接会让网关取消这一轮和它的待审批
  控制口 127.0.0.1:18795：/say（主程序让电脑念一句，走同一个播放队列）、/status、/sleep-state（P4）
  托盘：在听 / 说话中 / 静音 / 游戏中 / 出错（网关、TTS 是否在线），左键切静音，右键重启或退出

运行态（runtime_state.RuntimeMonitor，D25/D26）：进入 GAMING 时停音频流、丢弃积压、释放 ASR、通知 TTS /unload；离开时重建。
退出码（scripts/voice-supervisor.ps1 据此决定）：0 = 托盘「退出语音」不再拉起；3 = 托盘「重启语音」立即拉起；其它 = 崩溃，退避后拉起。
"""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import queue
import re
import sys
import threading
import time
import wave
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np
import sounddevice as sd
from pyaec import Aec

from guanjia.asr import StreamingASR
from guanjia.asr_final import FinalASR
from guanjia.audio_cues import earcon
from guanjia.barge import BargeConfig, BargeDetector
from guanjia.brain import MAIN_SESSION_KEY, Brain, tag_voice_text
from guanjia.control import CONTROL_PORT, start_control_server
from guanjia.runtime_state import ACTIVE, GAMING, RuntimeMonitor
from guanjia.speaker import SpeakerGate
from guanjia.status import Health, HealthMonitor, TrayView, tcp_alive, tray_view
from guanjia.tts_client import TTSClient
from guanjia.turns import SayItem, TurnRunner
from guanjia.vad import SR as SR16K
from guanjia.vad import VAD, Downsampler

log = logging.getLogger("guanjia.voice")

# ---- 声纹门（原则：宁可放过不可拦人；家里只有一位主人，危险操作另有分级插件兜底）----
SPK_THRESHOLD = 0.30     # 整句放行阈值：实测主人 0.60~0.74，离远/带情绪掉到 0.48；噪声段 0.06~0.32。可用 --spk-threshold 改
SPK_GATE_MODE = "enforce"  # enforce=低于阈值拦截；log=只记录分数不拦截（--gate-mode log）

# ---- 回声消除 ----
# speex AEC 默认关：音响一体麦自带 DSP 噪声门 + 回声消除，而 speex 预处理实测会伤识别。GUANJIA_AEC=1 或 --aec on 可重新打开
AEC_ENABLED = os.environ.get("GUANJIA_AEC", "0").lower() in ("1", "on", "true")

# ---- 前端电平 ----
MIC_GAIN = 3.0           # 增益后才进 VAD/ASR/声纹：音箱一体麦电平很低（mic_check 建议 x3）；--mic-gain 可调

# ---- 输出保活 / 播放记录 ----
COMFORT_NOISE_DBFS = float(os.environ.get("GUANJIA_COMFORT_DBFS", "-70"))  # 输出流常开时持续写的白噪声（听不见），防音响待机吞句
LEAD_SILENCE_MS = 150    # 每轮第一句前补的静音前导，给音响/驱动唤醒留余量

# ---- 识别取证：落盘每段送识别的音频 ----
SEG_DUMP_DIR = Path(__file__).resolve().parents[2] / "tmp" / "segments"  # 原始(设备采样率) + 处理后(增益+16k) 各一份
SEG_KEEP = 50            # 只保留最近这么多段
RAW_RING_SEC = 60        # 原始麦克风帧环形缓冲时长：要覆盖最长一段语音 + VAD 尾静音

# ---- 轮次 / TTS ----
TTS_TIMEOUT_SEC = 12.0   # 单句 TTS 合成超时：超时放弃这句，继续下一句
TTS_MAX_FAILS = 2        # 本轮 TTS 连续失败达到此数：剩下的句子不再合成（网关流照样读完）
FIRST_SENT_MAX_CHARS = 20  # 首句硬上限：回复开头没标点时攒到 20 字就先合成出声
MIN_UTT_SEC = 0.4        # 短于此的语音段不当作指令
PREROLL_MS = 500         # ASR 预滚：VAD 起判前这段音频先补喂 ASR，防首字丢失
STATS_EVERY_SEC = 30     # 丢帧 / 欠载计数的日志周期

# ---- 打断（只在我们出声时判定；不看声纹、不看 VAD，原因见 barge.py）----
BARGE_MS = 180           # 累计过线时长达到此值即打断
BARGE_GUARD_MS = 300     # 每次开始出声后的保护期：只学回声底噪
BARGE_TAIL_MS = 300      # 参考信号停了以后仍按"在播"算的尾巴
BARGE_RMS_MIN = float(os.environ.get("GUANJIA_BARGE_RMS_MIN", "0.004"))  # 触发线下限（原始输入 RMS）；验收时按 barge-mon 校准
BARGE_FLOOR_RATIO = 3.0  # 触发线 = 回声底噪 × 此倍数
BARGE_LOG_EVERY_MS = 1000  # 出声期间每隔这么久打一行 DEBUG barge-mon，用来校准

# ---- 提示音 / 健康检查 / 退出码 ----
EARCON_ENABLED = os.environ.get("GUANJIA_EARCON", "1") != "0"  # 说完话立即放提示音
SAY_MAX_PENDING = 3      # /say 最多排队这么多段，多了回 busy（模型循环调用 speak、微信刷屏时不至于念上几分钟）
HEALTH_EVERY_SEC = 10.0  # 网关、TTS 在线检查周期（托盘用）
GATEWAY_PORT = 18789
EXIT_QUIT, EXIT_CRASH, EXIT_RESTART = 0, 1, 3  # 与 scripts/voice-supervisor.ps1 约定
LOG_MAX_BYTES = 5_000_000  # voice.log 轮转大小（留 3 份）

# 只给 pipeline_test.py 用：P2 起守护进程不再发 system 提示，语音规则在 AGENTS.md 里按「[语音 …]」前缀生效
SYSTEM_PROMPT = (
    "你是家里的语音管家，通过音箱和用户对话。回答默认不超过两句话；内容长时先用一句话概括并问要不要展开。"
    "用户的话来自语音识别，可能有同音错字，按最合理的意思理解。不要输出表情符号、markdown 或列表。"
)


def pick_stream_rate(in_dev, out_dev) -> int:
    """选一个输入输出都接受、且是 16k 整数倍的流采样率。

    优先 16000（Windows 默认的 MME 通道会自行重采样，省掉降采样和 AEC 的高采样率开销）；
    WASAPI 这类只认原生采样率的设备退到 48000 / 32000；都不行就抛错说明设备默认率。
    """
    dev_in = sd.query_devices(in_dev if in_dev is not None else sd.default.device[0])
    dev_out = sd.query_devices(out_dev if out_dev is not None else sd.default.device[1])
    for rate in (16000, 48000, 32000):
        try:
            sd.check_input_settings(device=in_dev, samplerate=rate, channels=1, dtype="float32")
            sd.check_output_settings(device=out_dev, samplerate=rate, channels=1, dtype="float32")
            return rate
        except Exception:  # noqa: BLE001  该采样率不被接受，试下一个
            continue
    raise RuntimeError(
        f"输入 {dev_in['name']}({dev_in['default_samplerate']:.0f}) / 输出 {dev_out['name']}({dev_out['default_samplerate']:.0f}) "
        "都不接受 16k/48k/32k，请用 --in/--out 指定 MME 设备"
    )


def make_aec(sr: int) -> Aec:
    """按设备采样率建 speex AEC：10ms 帧、250ms 滤波长度、开启预处理（降噪）。守护进程与声纹注册共用。"""
    return Aec(sr // 100, sr // 4, sr, True)


def aec_cancel(aec: Aec, mic: np.ndarray, ref: np.ndarray) -> np.ndarray:
    """对一帧 float32 做回声消除，返回同长度 float32（截到帧长，防 pyaec 返回长度不符打乱降采样相位）。"""
    mic_i16 = (np.clip(mic, -1, 1) * 32767).astype(np.int16)
    ref_i16 = (np.clip(ref, -1, 1) * 32767).astype(np.int16)
    out = np.asarray(aec.cancel_echo(mic_i16.tolist(), ref_i16.tolist()), dtype=np.float32) / 32768.0
    return out[: len(mic)]


def apply_mic_gain(x: np.ndarray, gain: float) -> np.ndarray:
    """软件增益 + tanh 软限幅：小信号近似线性放大，接近满幅时平滑压缩，不硬削波。守护进程与声纹注册共用。"""
    if gain == 1.0:
        return x
    return np.tanh(x * gain).astype(np.float32, copy=False)


@dataclass
class Frames:
    mic: np.ndarray
    ref: np.ndarray
    playing: bool  # 本块是否写出了真实语音（不含保活噪声），打断判定据此认"我们在出声"


@dataclass
class VoiceDaemon:
    in_dev: int | None = None
    out_dev: int | None = None
    speaker_gate: bool = True
    spk_threshold: float = SPK_THRESHOLD  # 声纹放行阈值
    gate_mode: str = SPK_GATE_MODE  # enforce / log
    mic_gain: float = MIC_GAIN  # 软件增益
    aec_on: bool = AEC_ENABLED  # 是否走 speex AEC
    tray_on: bool = True  # 托盘图标（--no-tray 关）
    control_port: int = CONTROL_PORT  # 本机控制口
    # 运行态
    state: str = "LISTENING"  # LISTENING / SPEAKING（一轮从开始到播完都算 SPEAKING）
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _stop_evt: threading.Event = field(default_factory=threading.Event)

    def __post_init__(self) -> None:
        self.sr = pick_stream_rate(self.in_dev, self.out_dev)  # 优先 16k，见 pick_stream_rate
        self.frame = self.sr // 100
        self.in_q: queue.Queue[Frames] = queue.Queue(maxsize=500)
        self.play_q: queue.Queue[tuple[int, np.ndarray]] = queue.Queue()  # (代次, PCM)，回调按代次丢弃旧音频
        self.text_q: queue.Queue[str | SayItem] = queue.Queue()  # 识别出的话（str）或 /say 要念的话（SayItem）
        self._gen = 0  # 播放代次：打断后递增，旧代次的音频一律丢弃；只有 stop_speaking 写
        # 以下播放状态只由音频回调写，其他线程只读
        self._play_buf = np.zeros(0, dtype=np.float32)  # 正在播的那一句 PCM
        self._play_pos = 0  # _play_buf 已播到的位置
        self._buf_gen = 0  # _play_buf 所属代次；与 _gen 不符时回调自行清空
        self._played_gen = -1  # 最近真正出过声的代次
        # 保活噪声：预生成 1s 循环使用（回调里不做随机数生成）
        self._noise = (np.random.default_rng(0).standard_normal(self.sr) * 10 ** (COMFORT_NOISE_DBFS / 20)).astype(np.float32)
        self._noise_pos = 0
        self._play_events: deque[tuple[float, float, int]] = deque(maxlen=200)  # (墙钟, 时长秒, 代次)，回调追加、主循环打日志
        self._turn_first = False  # 本轮第一句还没入队（大脑线程读写）：第一句前补静音前导
        self._turn_t0 = 0.0  # 本轮开始的 monotonic 时刻，记首音延迟用
        # 识别取证：原始麦克风帧环形缓冲 (该帧对应的 16k 起始样本号, 原始帧)，只有前端线程读写
        self._raw_ring: deque[tuple[int, np.ndarray]] = deque(maxlen=RAW_RING_SEC * 100)
        self._vad_pos = 0  # 已喂给主 VAD 的 16k 样本数，与 VAD 段的 start 对齐
        self._dropped = 0  # in_q 满丢掉的帧数
        self._underflows = 0  # 输出欠载次数
        self._paused = threading.Event()  # 游戏态：由运行态监控置位，主循环据此停开音频流
        self._muted = threading.Event()  # 托盘静音：不听不说
        self._front_dirty = False  # 静音期间前端丢过帧，恢复时先复位 VAD/ASR（只有前端线程读写）
        self._exit_code: int | None = None  # 托盘要求的退出码
        self.sleep_state = "awake"  # P4 休眠状态位，由控制口 /sleep-state 设置，托盘显示
        self.last_error = ""  # 最近一轮的错误（网关 / TTS），托盘显示；下一轮成功出声后清空
        self.health = Health()  # 网关、TTS 是否在线（HealthMonitor 更新）
        self.runtime = RuntimeMonitor(self._on_runtime)

        self.down = Downsampler(self.sr)
        self.aec = make_aec(self.sr) if self.aec_on else None  # 关掉时前端直接用原始输入
        self.vad = VAD()
        self.gate = SpeakerGate(threshold=self.spk_threshold) if self.speaker_gate else None
        if self.gate and not self.gate.names():
            log.warning("声纹库为空，默认放行所有声音；请先运行 python -m guanjia.enroll 注册")
            self.gate = None
        self.asr = StreamingASR()
        self.asr_final = FinalASR()  # 整段精识别（SenseVoice int8）：段结束后替换流式结果，失败回退流式
        self.brain = Brain(session_key=MAIN_SESSION_KEY)  # 主线会话；流不可提前关（见 brain.py）
        self.tts = TTSClient(timeout=TTS_TIMEOUT_SEC)
        self.runner = TurnRunner(
            open_stream=lambda text: self.brain.stream(tag_voice_text(text)),
            synth=self.tts.synth, enqueue=self._enqueue_pcm, current_gen=lambda: self._gen,
            first_max_chars=FIRST_SENT_MAX_CHARS, max_tts_fails=TTS_MAX_FAILS)
        self._earcon = earcon(self.sr)
        self.barge = BargeDetector(BargeConfig(guard_ms=BARGE_GUARD_MS, tail_ms=BARGE_TAIL_MS, rms_min=BARGE_RMS_MIN,
                                               floor_ratio=BARGE_FLOOR_RATIO, trigger_ms=BARGE_MS))
        self._mon_ms, self._mon_peak = 0, 0.0  # barge-mon 日志计时与区间 RMS 峰值（只有前端线程读写）
        self.monitor = HealthMonitor({"gateway": lambda: tcp_alive("127.0.0.1", GATEWAY_PORT), "tts": self.tts.alive},
                                     on_change=self._on_health, interval=HEALTH_EVERY_SEC)
        self.tray = None  # run() 里按 tray_on 创建
        self.control = None  # run() 里启动
        # 前端线程状态（只有前端线程读写）
        self._asr_stream = self.asr.new_stream()  # 当前这句话的 ASR 流，每段结束换新流
        self._preroll: deque[np.ndarray] = deque(maxlen=PREROLL_MS // 10)  # 最近 0.5s 的 16k 块
        self._was_speech = False  # 上一块 VAD 是否处于人声状态，用于捕捉起判沿

    # ---------- 音频回调 ----------
    def _callback(self, indata, outdata, frames, time_info, status) -> None:
        if status.output_underflow:
            self._underflows += 1
        cur = self._gen
        if self._buf_gen != cur:  # 已换代：正在播的旧句作废
            self._play_buf, self._play_pos, self._buf_gen = np.zeros(0, dtype=np.float32), 0, cur
        out = np.zeros(frames, dtype=np.float32)
        filled = 0
        while filled < frames:
            if self._play_pos >= len(self._play_buf):
                try:
                    g, pcm = self.play_q.get_nowait()
                except queue.Empty:
                    break
                if g == cur:
                    self._play_buf, self._play_pos, self._buf_gen = pcm, 0, g
                    self._play_events.append((time.time(), len(pcm) / self.sr, g))  # 真正开始写入输出流的时刻
                continue
            k = min(frames - filled, len(self._play_buf) - self._play_pos)
            out[filled:filled + k] = self._play_buf[self._play_pos:self._play_pos + k]
            self._play_pos += k
            filled += k
        if filled:
            self._played_gen = cur
        voiced = filled > 0 and float(np.abs(out[:filled]).max()) > 1e-4  # 150ms 静音前导不算出声，打断底噪从真声开始学
        # 叠加保活噪声（循环取预生成缓冲）：输出流从不"纯零"，音响不会因静默进入待机
        n0 = self._noise_pos
        idx = (np.arange(n0, n0 + frames)) % len(self._noise)
        out += self._noise[idx]
        self._noise_pos = (n0 + frames) % len(self._noise)
        outdata[:, 0] = out
        try:
            self.in_q.put_nowait(Frames(mic=indata[:, 0].copy(), ref=out, playing=voiced))
        except queue.Full:
            self._dropped += 1

    def _reset_frontend(self) -> None:
        """VAD / ASR 流 / 预滚 / 打断状态全部复位（游戏态恢复、静音恢复时调用）。"""
        self.vad = VAD()
        self._vad_pos = 0
        self._raw_ring.clear()
        self._asr_stream = self.asr.new_stream()
        self._preroll.clear()
        self._was_speech = False
        self.barge.reset()

    def _playing(self) -> bool:
        return self._play_pos < len(self._play_buf) or not self.play_q.empty()

    # ---------- 前端线程 ----------
    def _frontend(self) -> None:
        while not self._stop_evt.is_set():
            try:
                fr = self.in_q.get(timeout=0.2)
            except queue.Empty:
                continue
            if self._paused.is_set():  # 游戏态：丢弃停流前残留的帧
                continue
            if self._muted.is_set():  # 静音：不听
                self._front_dirty = True
                continue
            if self._front_dirty:
                self._reset_frontend()
                self._front_dirty = False
            try:
                self._frontend_step(fr)
            except Exception:  # noqa: BLE001  单帧出错不能让整条听觉链"变聋"
                log.exception("frontend error")

    def _frontend_step(self, fr: Frames) -> None:
        """处理一帧：(AEC) → 16k → 增益 → VAD → 预滚/ASR → 打断判定 → 段结束出文字。"""
        x = aec_cancel(self.aec, fr.mic, fr.ref) if self.aec else fr.mic
        y = apply_mic_gain(self.down.process(x), self.mic_gain)
        self._raw_ring.append((self._vad_pos, fr.mic))
        self.vad.push(y)
        self._vad_pos += len(y)
        speaking_now = self.vad.is_speech()
        if speaking_now:
            if not self._was_speech:  # 起判沿：VAD 要人声满 0.25s 才翻 true，先把预滚补喂进 ASR
                for c in self._preroll:
                    self.asr.feed(self._asr_stream, c, SR16K)
            self.asr.feed(self._asr_stream, y, SR16K)
        self._preroll.append(y)
        rms = float(np.sqrt(np.mean(x * x))) if len(x) else 0.0  # 增益前的原始电平，打断阈值按它标定
        self._barge_step(rms, fr.playing)
        self._was_speech = speaking_now
        for seg in self.vad.pop_segments():
            stream, self._asr_stream = self._asr_stream, self.asr.new_stream()  # 每句一条新流，不带上一句残留
            if seg.duration < MIN_UTT_SEC:
                continue
            self.asr.feed(stream, np.zeros(SR16K * 3 // 10, dtype=np.float32), SR16K)  # 补 0.3s 静音冲刷尾字
            self.asr.finish(stream)
            stream_text = self.asr.text(stream)  # 流式结果，留着与精识别对比
            text = self.asr_final.transcribe(seg.samples, fallback=stream_text)
            self._dump_segment(seg, text)
            if not text:  # 精识别也为空：不过声纹、不算拒绝，直接丢
                log.debug("丢弃空文本段 %.2fs", seg.duration)
                continue
            who, score = self.gate.identify(seg.samples) if self.gate else ("anyone", 1.0)
            if self.gate and who is None:
                if self.gate_mode == "enforce":
                    log.info("拒绝：非注册声音 score=%.2f text=%r (stream: %r)", score, text, stream_text)
                    continue
                who = "未识别(放行)"  # log 模式：只记录不拦截
            log.info("[%s %.2f] %s  (stream: %s)", who, score, text, stream_text)
            self._accept_utterance(text)

    def _accept_utterance(self, text: str) -> None:
        """一句话通过声纹：正在说就停（只停播放）；立刻放提示音；文字排进大脑队列。"""
        if self.state == "SPEAKING":
            self.stop_speaking("新一轮")
        if EARCON_ENABLED:
            self.play_q.put((self._gen, self._earcon))
        self.text_q.put(text)

    def _raw_for(self, seg) -> np.ndarray:
        """从原始环形缓冲取出与 VAD 段对齐的设备采样率原始音频（AEC 前、无增益）。"""
        decim = self.sr // SR16K
        start16 = int(round(seg.start * SR16K))
        end16 = start16 + len(seg.samples)
        parts = [(i, m) for i, m in self._raw_ring if i < end16 and i + len(m) // decim > start16]
        if not parts:
            return np.zeros(0, dtype=np.float32)
        raw = np.concatenate([m for _, m in parts])
        off = max(0, (start16 - parts[0][0]) * decim)
        return raw[off: off + (end16 - start16) * decim]

    def _dump_segment(self, seg, text: str) -> None:
        """落盘一段送识别的音频：_raw.wav（设备采样率原始）+ _proc.wav（增益+16k），只留最近 SEG_KEEP 段。"""
        try:
            SEG_DUMP_DIR.mkdir(parents=True, exist_ok=True)
            label = re.sub(r'[\\/:*?"<>|\s]', "", text)[:10] or "空"
            stem = f"{datetime.now():%Y%m%d-%H%M%S-%f}"[:-3] + f"_{label}"
            for suffix, pcm, sr in (("raw", self._raw_for(seg), self.sr), ("proc", seg.samples, SR16K)):
                with wave.open(str(SEG_DUMP_DIR / f"{stem}_{suffix}.wav"), "wb") as w:
                    w.setnchannels(1)
                    w.setsampwidth(2)
                    w.setframerate(sr)
                    w.writeframes((np.clip(pcm, -1, 1) * 32767).astype(np.int16).tobytes())
            procs = sorted(SEG_DUMP_DIR.glob("*_proc.wav"))
            for old in procs[:-SEG_KEEP]:
                old.unlink(missing_ok=True)
                old.with_name(old.name.replace("_proc.wav", "_raw.wav")).unlink(missing_ok=True)
        except OSError as e:  # 取证失败不影响对话
            log.warning("segment dump failed: %s", e)

    def _barge_step(self, rms: float, ref_active: bool) -> None:
        """打断判定（每 10ms 一帧，前端线程）：交给 BargeDetector；出声期间每秒打一行 barge-mon 供校准。"""
        hit = self.barge.step(rms, ref_active)
        if self.barge.playing:
            self._mon_ms += 10
            self._mon_peak = max(self._mon_peak, rms)
            if self._mon_ms >= BARGE_LOG_EVERY_MS:
                log.debug("barge-mon rms_peak=%.5f energy=%.5f floor=%.5f line=%.5f hot=%dms", self._mon_peak,
                          self.barge.energy, self.barge.floor, self.barge.line, self.barge.hot_ms)
                self._mon_ms, self._mon_peak = 0, 0.0
        else:
            self._mon_ms, self._mon_peak = 0, 0.0
        if hit and self.state == "SPEAKING" and self._played_gen == self._gen:
            log.info("barge-in: energy=%.4f floor=%.4f line=%.4f", self.barge.energy, self.barge.floor, self.barge.line)
            self.stop_speaking("barge-in")

    # ---------- 大脑线程 ----------
    def _brainloop(self) -> None:
        while not self._stop_evt.is_set():
            try:
                item = self.text_q.get(timeout=0.2)
            except queue.Empty:
                continue
            if self._paused.is_set() or self._muted.is_set():  # 游戏态 / 静音：积压的话直接丢掉
                continue
            try:
                self._handle_item(item)
            except Exception:  # noqa: BLE001  一轮出错不能让大脑线程退出
                log.exception("brain loop error")

    def _handle_item(self, item: str | SayItem) -> None:
        """一轮：语音的话进主线会话 / /say 的话直接念；等本轮播完再回到监听（被打断时 stop_speaking 已经切了）。"""
        gen = self._begin_speaking()
        self._turn_first = True
        self._turn_t0 = time.monotonic()
        try:
            if isinstance(item, SayItem):
                res = self.runner.run_say(item.text, gen)
                log.info("say: %s", item.text)
            else:
                res = self.runner.run_reply(item, gen)
                log.info("reply: %s", res.reply)
            if res.unspoken:
                log.warning("unspoken %d (aborted=%s): %s", len(res.unspoken), res.aborted, " / ".join(res.unspoken)[:300])
            if res.error:
                self._set_error(res.error)
            elif res.spoken:
                self._set_error("")
            while gen == self._gen and self._playing() and not self._stop_evt.is_set():
                time.sleep(0.05)
        finally:
            with self._lock:
                if gen == self._gen:
                    self.state = "LISTENING"

    def _enqueue_pcm(self, gen: int, pcm: np.ndarray, sr: int) -> None:
        """TurnRunner 的入队回调：重采样到流采样率；本轮第一句前补静音前导并记首音延迟。"""
        if sr != self.sr:
            x_old = np.linspace(0, 1, len(pcm), endpoint=False)
            x_new = np.linspace(0, 1, int(len(pcm) * self.sr / sr), endpoint=False)
            pcm = np.interp(x_new, x_old, pcm).astype(np.float32)
        if self._turn_first:
            pcm = np.concatenate([np.zeros(self.sr * LEAD_SILENCE_MS // 1000, dtype=np.float32), pcm])
            self._turn_first = False
            log.info("first audio %.2fs after turn start", time.monotonic() - self._turn_t0)
        self.play_q.put((gen, pcm))  # 即使此刻恰好被打断，回调也会按代次丢掉它

    def _begin_speaking(self) -> int:
        with self._lock:
            self.state = "SPEAKING"
            return self._gen

    def stop_speaking(self, reason: str) -> None:
        """打断：换代（回调自行丢弃旧代音频）、断开进行中的 TTS 请求、清播放队列、回到监听。

        不碰网关流：断开连接会让网关取消这一轮（连同它等着的审批）；大脑线程会把流读完，只是不再合成。
        """
        with self._lock:
            self._gen += 1
            self.state = "LISTENING"
            self.tts.abort()
        while True:  # 清播放队列（回调也会按代次丢弃，这里立即清掉省得积压）
            try:
                self.play_q.get_nowait()
            except queue.Empty:
                break
        log.info("stop speaking (%s)", reason)

    # ---------- 控制口 / 托盘 ----------
    def say(self, text: str) -> tuple[bool, str]:
        """控制口 /say：静音或游戏中拒绝，否则排进大脑队列（与语音轮按顺序念，不会叠在一起）。"""
        if self._muted.is_set():
            return False, "muted"
        if self._paused.is_set():
            return False, "gaming"
        if sum(isinstance(x, SayItem) for x in list(self.text_q.queue)) >= SAY_MAX_PENDING:
            return False, "busy"
        self.text_q.put(SayItem(text))
        log.info("say queued: %s", text)
        return True, "queued"

    def status(self) -> dict:
        """控制口 /status：托盘同款状态。"""
        v = self.view()
        return {"view": v.key, "label": v.label, "state": self.state, "muted": self._muted.is_set(),
                "gaming": self._paused.is_set(), "runtime": self.runtime.state, "gateway": self.health.gateway,
                "tts": self.health.tts, "error": self.last_error, "sleep": self.sleep_state, "pending": self.text_q.qsize()}

    def set_sleep_state(self, state: str) -> bool:
        """控制口 /sleep-state：P4 休眠状态位，只做显示。"""
        self.sleep_state = state
        self._refresh_tray()
        return True

    def view(self) -> TrayView:
        return tray_view(muted=self._muted.is_set(), gaming=self._paused.is_set(), busy=self.state == "SPEAKING",
                         health=self.health, last_error=self.last_error, sleep_state=self.sleep_state)

    def toggle_mute(self) -> None:
        """托盘左键：静音（不听不说，立即停播）/ 取消静音。"""
        if self._muted.is_set():
            self._muted.clear()
            log.info("unmuted")
        else:
            self._muted.set()
            self.stop_speaking("静音")
            log.info("muted")
        self._refresh_tray()

    def request_exit(self, code: int) -> None:
        """托盘菜单：退出语音（0，看门狗不再拉起）/ 重启语音（3，看门狗立即拉起）。"""
        log.info("exit requested from tray: code=%d", code)
        self._exit_code = code
        self._stop_evt.set()

    def _set_error(self, msg: str) -> None:
        msg = msg[:120]
        if msg != self.last_error:
            self.last_error = msg
            if msg:
                log.warning("error: %s", msg)
            self._refresh_tray()

    def _on_health(self, h: Health) -> None:
        self.health = h
        log.info("health: gateway=%s tts=%s", h.gateway, h.tts)
        self._refresh_tray()

    def _refresh_tray(self) -> None:
        if self.tray:
            self.tray.refresh()

    # ---------- 运行态 ----------
    def _on_runtime(self, old: str, new: str) -> None:
        """监控线程回调：只置位/清标志并打断当前播放，真正停开音频流留给主循环做（流操作集中在一个线程）。"""
        if old == ACTIVE:
            log.info("runtime: 离开 ACTIVE → %s，主动侧静默", new)
        if new == GAMING:
            self._paused.set()
            self.stop_speaking("gaming")
        elif old == GAMING:
            self._paused.clear()

    def quiet(self) -> bool:
        """主动侧查询：游戏中或开机/游戏后 5 分钟静默期内返回 True，不许主动开口。"""
        return self.runtime.is_quiet()

    @property
    def quiet_until(self) -> float:
        """静默期截止时间戳（epoch 秒）；游戏中另由 quiet() 判定。"""
        return self.runtime.quiet_until

    def set_gaming(self, on: bool) -> None:
        """手动开关："我打游戏了" / "不打了"。"""
        self.runtime.set_manual_gaming(on)

    def _suspend_audio(self, s: sd.Stream) -> None:
        """进入游戏态：停流、清空积压、释放 ASR（CPU 内存）、通知 TTS 服务释放显存。主循环调用。"""
        s.stop()
        for q in (self.in_q, self.text_q):
            while not q.empty():
                q.get_nowait()
        self.asr = None
        self.asr_final = None
        threading.Thread(target=self.tts.control, args=("unload",), name="tts-unload", daemon=True).start()
        log.info("gaming: 音频流已停，ASR（流式+精识别）已释放")

    def _resume_audio(self, s: sd.Stream) -> None:
        """离开游戏态：重建 ASR/VAD 与前端状态，重开音频流，后台让 TTS 预加载。主循环调用。"""
        self.asr = StreamingASR()
        self.asr_final = FinalASR()
        self._reset_frontend()
        s.start()
        threading.Thread(target=self.tts.control, args=("load",), name="tts-load", daemon=True).start()
        log.info("gaming 结束: 音频流已恢复")

    # ---------- 主循环 ----------
    def run(self) -> None:
        log.info("device rate %d, in=%s out=%s, speakers=%s(%s), gate=%s@%.2f, mic_gain=%.1f, aec=%s, barge=%dms guard=%dms "
                 "rms_min=%.4f ratio=%.1f, tts_timeout=%.0fs, session=%s, control=127.0.0.1:%d", self.sr, self.in_dev,
                 self.out_dev, self.gate.names() if self.gate else "off", self.gate.model.stem if self.gate else "-",
                 self.gate_mode, self.spk_threshold, self.mic_gain, "on" if self.aec else "off", BARGE_MS, BARGE_GUARD_MS,
                 BARGE_RMS_MIN, BARGE_FLOOR_RATIO, TTS_TIMEOUT_SEC, MAIN_SESSION_KEY, self.control_port)
        try:
            self.control = start_control_server(self, self.control_port)
        except OSError as e:
            log.error("控制口 127.0.0.1:%d 绑不上（是不是已经有一个语音进程在跑？）：%s", self.control_port, e)
            sys.exit(EXIT_CRASH)
        threads = [threading.Thread(target=self._frontend, name="frontend", daemon=True),
                   threading.Thread(target=self._brainloop, name="brain", daemon=True)]
        for t in threads:
            t.start()
        self.monitor.start()
        if self.tray_on:
            from guanjia.tray import Tray  # 延迟导入：--no-tray 时不需要 pystray/Pillow

            self.tray = Tray(view=self.view, muted=self._muted.is_set, on_toggle_mute=self.toggle_mute,
                             on_restart=lambda: self.request_exit(EXIT_RESTART),
                             on_exit=lambda: self.request_exit(EXIT_QUIT))
            self.tray.start()
        failed = False
        shown = ""
        last_stats, stats = time.monotonic(), (0, 0)
        with sd.Stream(samplerate=self.sr, blocksize=self.frame, dtype="float32", channels=1, latency="low",
                       device=(self.in_dev, self.out_dev), callback=self._callback) as s:
            log.info("listening ... (latency in/out %.3f/%.3f s; output stream always-open, comfort noise %.0f dBFS, "
                     "lead %dms, earcon %s, dump %s)", *s.latency, COMFORT_NOISE_DBFS, LEAD_SILENCE_MS,
                     "on" if EARCON_ENABLED else "off", SEG_DUMP_DIR)
            self.runtime.start()  # 开机补写离线区间 + 5 分钟静默；之后每 2s 检测游戏态
            try:
                while not self._stop_evt.is_set():
                    time.sleep(0.5)
                    paused = self._paused.is_set()  # 本轮只读一次，防监控线程中途翻转导致误判"流死了"
                    if paused and s.active:
                        self._suspend_audio(s)
                    elif not paused and not s.active:
                        self._resume_audio(s)
                    dead = [t.name for t in threads if not t.is_alive()]
                    if dead or (not s.active and not paused):
                        log.error("worker died: threads=%s stream_active=%s，退出等待看门狗拉起", dead, s.active)
                        failed = True
                        break
                    key = self.view().key
                    if key != shown:  # 说话中 ↔ 在听 等状态变化同步到托盘
                        shown = key
                        self._refresh_tray()
                    while self._play_events:  # 回调记下的"真正写入输出流"的时刻与时长
                        t0, dur, g = self._play_events.popleft()
                        log.info("play: start=%s dur=%.2fs gen=%d", datetime.fromtimestamp(t0).strftime("%H:%M:%S.%f")[:-3],
                                 dur, g)
                    if time.monotonic() - last_stats >= STATS_EVERY_SEC:
                        last_stats = time.monotonic()
                        if (self._dropped, self._underflows) != stats:
                            stats = (self._dropped, self._underflows)
                            log.warning("累计丢帧 %d，输出欠载 %d", *stats)
            except KeyboardInterrupt:
                self._exit_code = EXIT_QUIT
        self._stop_evt.set()
        self.runtime.stop()  # 最后一次心跳，下次开机据此算离线区间
        self.monitor.stop()
        if self.tray:
            self.tray.stop()
        self.control.shutdown()
        code = EXIT_CRASH if failed else (EXIT_QUIT if self._exit_code is None else self._exit_code)
        log.info("voice daemon exit code=%d", code)
        sys.exit(code)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="in_dev", type=int, default=None)
    ap.add_argument("--out", dest="out_dev", type=int, default=None)
    ap.add_argument("--no-gate", action="store_true", help="关闭声纹放行（调试用）")
    ap.add_argument("--spk-threshold", type=float, default=SPK_THRESHOLD, help="声纹放行阈值")
    ap.add_argument("--gate-mode", choices=("enforce", "log"), default=SPK_GATE_MODE, help="log=只记录声纹分不拦截")
    ap.add_argument("--mic-gain", type=float, default=MIC_GAIN, help="软件增益（tanh 软限幅）")
    ap.add_argument("--aec", choices=("on", "off"), default="on" if AEC_ENABLED else "off", help="speex AEC 开关（默认关）")
    ap.add_argument("--no-tray", action="store_true", help="不显示托盘图标（调试用）")
    ap.add_argument("--control-port", type=int, default=CONTROL_PORT, help="本机控制口端口（/say、/status）")
    ap.add_argument("-v", action="store_true")
    ap.add_argument("--log-file", default=None, help="同时把日志写到这个文件（按 5MB 轮转，留 3 份）")
    a = ap.parse_args()
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]  # 走 stdout，避免 PowerShell 把 stderr 当错误
    if a.log_file:
        handlers.append(logging.handlers.RotatingFileHandler(a.log_file, maxBytes=LOG_MAX_BYTES, backupCount=3,
                                                             encoding="utf-8"))
    logging.basicConfig(level=logging.DEBUG if a.v else logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=handlers)
    for noisy in ("httpcore", "PIL"):  # 这两个的 DEBUG 太吵
        logging.getLogger(noisy).setLevel(logging.INFO)
    VoiceDaemon(in_dev=a.in_dev, out_dev=a.out_dev, speaker_gate=not a.no_gate, spk_threshold=a.spk_threshold,
                gate_mode=a.gate_mode, mic_gain=a.mic_gain, aec_on=a.aec == "on", tray_on=not a.no_tray,
                control_port=a.control_port).run()


if __name__ == "__main__":
    main()

# ---------- 评审未修项（voice-review.md 编号）----------
# P2-10 MIN_UTT_SEC 过滤无效：段内回看/尾静音的实际占比需实录测量后再定判据，盲调会误伤"开灯"这类短指令。
# P2-11 identify 仍在前端线程同步执行：长段 CAM++ 会短暂阻塞打断判定；已加丢帧计数日志，确有积压再移到独立线程。
# P2-15 TTS 仍用线性插值重采样：resample_poly 需要 scipy，本次不引入新依赖。
# P2（本期）打断阈值 BARGE_RMS_MIN 按 10-01 日志推算，验收时按 barge-mon 实测再定；TTS 服务端不会因客户端断开而中止合成。
