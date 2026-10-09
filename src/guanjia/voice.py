"""语音守护进程：常开监听 → 回声消除 → 增益 → VAD → 流式 ASR → 整段精识别 → 声纹放行 → OpenClaw → 按句 TTS → 播放；随时可打断。

线程分工（都是简单的生产者/消费者）：
- 音频回调（sounddevice 全双工，设备原生采样率，10ms 一块，latency='low'）：把 (麦帧, 本块实际播出的帧) 放进 in_q；
  从 play_q 取 (代次, PCM) 填给输出，旧代次的音频直接丢弃。
- 前端线程：AEC(参考=我们自己播出的帧) → 降到 16k → VAD → 预滚 + 流式 ASR → 段结束过声纹；同时做打断判定。
- 大脑线程：拿一句文字 → 流式调网关 → 句子切分 → TTS → 推 play_q。

运行态（runtime_state.RuntimeMonitor，D25/D26）：进入 GAMING 时主循环停音频流、丢弃积压输入、释放 ASR、
通知 TTS 服务 /unload；离开 GAMING 时重建 ASR/VAD、重开音频流、后台 /load。主动侧通过 quiet() 查询是否允许开口。

打断：本轮已经出过声后，AEC 后的快速 VAD 检出人声、且平滑能量高于"回声残留自适应底噪 × 倍数"的累计时长 ≥ BARGE_MS，
即 stop_speaking()：换代 + 清播放队列、关闭网关流与进行中的 TTS 请求。打断不看声纹分（短片段分数不可靠）。
续说：还没出声（大脑在想）时用户又说完一整句，走整句声纹后按"用户续说"停掉当前轮并把上一句拼到新句前面。
"""

from __future__ import annotations

import argparse
import logging
import os
import queue
import sys
import threading
import re
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
from guanjia.backoffice import BackOffice, Task
from guanjia.brain import Brain
from guanjia.front import Front
from guanjia.runtime_state import ACTIVE, GAMING, RuntimeMonitor
from guanjia.speaker import SpeakerGate
from guanjia.textseg import SentenceSplitter
from guanjia.tts_client import TTSClient
from guanjia.vad import SR as SR16K
from guanjia.vad import VAD, Downsampler

log = logging.getLogger("guanjia.voice")

# ---- 声纹门（原则：宁可放过不可拦人；家里只有一位主人，危险操作另有 S3 分级守卫兜底）----
SPK_THRESHOLD = 0.30     # 整句放行阈值：实测主人 0.60~0.74，离远/带情绪掉到 0.48；噪声段 0.06~0.32（空文本段已提前丢弃，故可放到 0.30）。可用 --spk-threshold 改
SPK_GATE_MODE = "enforce"  # enforce=低于阈值拦截；log=只记录分数不拦截（--gate-mode log）

# ---- 回声消除 ----
# speex AEC 默认关：音响一体麦自带 DSP 噪声门 + 回声消除（halfduplex 探针：播放 0.42 峰值时原始输入仅 ~1e-4），
# 而 speex 预处理实测会伤识别（seg_eval：只过 AEC 那条链错字明显）。GUANJIA_AEC=1 或 --aec on 可重新打开
AEC_ENABLED = os.environ.get("GUANJIA_AEC", "0").lower() in ("1", "on", "true")

# ---- 前端电平 ----
MIC_GAIN = 3.0           # AEC 之后、VAD/ASR/声纹之前的软件增益：音箱一体麦电平很低（mic_check 建议 x3），远处说话 ASR 出空文本
                         # CAM++ 对增益不敏感（实测 x3 前后向量余弦 0.993），现有声纹不必重录；--mic-gain 可调

# ---- 输出保活 / 播放记录 ----
COMFORT_NOISE_DBFS = -70.0  # 输出流常开，空闲时也持续写这个电平的白噪声（听不见），防音响自动待机吞掉短句开头
LEAD_SILENCE_MS = 150    # 每轮第一句前补的静音前导，给音响/驱动唤醒留余量

# ---- 识别取证：落盘每段送识别的音频 ----
SEG_DUMP_DIR = Path(__file__).resolve().parents[2] / "tmp" / "segments"  # 原始(设备采样率,AEC 前) + 处理后(AEC+增益+16k) 各一份
SEG_KEEP = 50            # 只保留最近这么多段
RAW_RING_SEC = 60        # 原始麦克风帧环形缓冲时长：要覆盖最长一段语音 + VAD 尾静音

# ---- 续说 / 轮次 ----
CONTINUE_WINDOW_SEC = 2.5  # 上一句送出后这么久内、且大脑还没回首 token，新句才算"续说"拼到上一句后面；否则中止旧轮、新句单独发
TTS_TIMEOUT_SEC = 12.0   # 单句 TTS 合成超时：超时放弃这句，继续下一句
TTS_MAX_FAILS = 2        # 本轮 TTS 连续失败/超时达到此数：直接结束本轮并复位，不拖住下一轮

# ---- 打断（barge-in）：只在"本轮已出声"后判定，不依赖声纹分 ----
BARGE_MS = 180           # 快速 VAD 人声 + 能量过线的累计时长达到此值即打断（规格：开口 0.5s 内停）
BARGE_GUARD_MS = 300     # 每次播放（重新）开始后的保护期：AEC 要重新收敛，这段只学底噪不触发
BARGE_TAIL_MS = 300      # 参考信号归零后仍按"在播"算的尾巴（输出延迟 0.1s + 房间混响）
BARGE_RMS_MIN = 0.003    # 能量绝对下限（约 -50 dBFS，按增益前电平计，运行时乘 MIC_GAIN）：防静音里的小毛刺
BARGE_FLOOR_RATIO = 2.5  # 触发线 = 回声残留底噪 × 此倍数（约 +8 dB），挡住自己播放声的 AEC 残留
BARGE_FLOOR_ALPHA = 0.05  # 底噪 EMA 系数（每 10ms，约 200ms 时间常数）；只在"没过线"的播放帧上更新
BARGE_SMOOTH_ALPHA = 0.3  # 能量平滑 EMA 系数（约 30ms），压掉 10ms 帧 RMS 的尖峰
BARGE_VAD_MIN_SPEECH = 0.1  # 打断专用 VAD 的最短人声（主 VAD 要 0.25s，太慢）
BARGE_VAD_MIN_SILENCE = 0.15  # 打断专用 VAD 的最短静音：短停顿就复位累计
BARGE_LOG_EVERY_MS = 1000  # 播放中每隔这么久打一行 DEBUG 打断监测（底噪/能量峰值/过线毫秒），用来实测标定
PREROLL_MS = 500         # ASR 预滚：VAD 起判前这段音频先补喂 ASR，防首字丢失
FIRST_SENT_MAX_CHARS = 20  # 首句硬上限：回复开头没标点时攒到 20 字就先合成出声（默认 60 字太慢）
MIN_UTT_SEC = 0.4        # 短于此的语音段不当作指令
STATS_EVERY_SEC = 30     # 丢帧 / 欠载计数的日志周期
VOICE_SYS_FILE = Path(__file__).resolve().parents[2] / "tmp" / "voice_sys.txt"  # 语音入口系统提示，每轮现读，改文件即生效
SYSTEM_PROMPT = (  # voice_sys.txt 缺失或为空时的回退提示
    "你是家里的语音管家，通过音箱和用户对话。回答默认不超过两句话；内容长时先用一句话概括并问要不要展开。"
    "用户的话来自语音识别，可能有同音错字，按最合理的意思理解。不要输出表情符号、markdown 或列表。"
)
_WEEKDAY_CN = "一二三四五六日"


def _system_prompt() -> str:
    """本轮系统提示：优先 voice_sys.txt 的内容，读不到或为空回退 SYSTEM_PROMPT；末尾拼当前时间。"""
    try:
        text = VOICE_SYS_FILE.read_text(encoding="utf-8-sig").strip()
    except OSError:
        text = ""
    return (text or SYSTEM_PROMPT) + _now_suffix()


def _now_suffix() -> str:
    """拼到 system 提示词末尾的当前时间说明，防止模型凭训练数据编造时间。"""
    now = datetime.now()
    return f"当前时间：{now:%Y-%m-%d %H:%M}（星期{_WEEKDAY_CN[now.weekday()]}）。"


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
    mic_gain: float = MIC_GAIN  # AEC 后软件增益
    aec_on: bool = AEC_ENABLED  # 是否走 speex AEC
    # 运行态
    state: str = "LISTENING"
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _stop_evt: threading.Event = field(default_factory=threading.Event)

    def __post_init__(self) -> None:
        self.sr = pick_stream_rate(self.in_dev, self.out_dev)  # 优先 16k，见 pick_stream_rate
        self.frame = self.sr // 100
        self.in_q: queue.Queue[Frames] = queue.Queue(maxsize=500)
        self.play_q: queue.Queue[tuple[int, np.ndarray]] = queue.Queue()  # (代次, PCM)，回调按代次丢弃旧音频
        self.text_q: queue.Queue[str] = queue.Queue()
        self._gen = 0  # 播放代次：打断后递增，旧代次的音频一律丢弃；只有 stop_speaking 写
        # 以下播放状态只由音频回调写，其他线程只读，不再有跨线程清空的竞态
        self._play_buf = np.zeros(0, dtype=np.float32)  # 正在播的那一句 PCM
        self._play_pos = 0  # _play_buf 已播到的位置（逐块取，不再整句 concatenate）
        self._buf_gen = 0  # _play_buf 所属代次；与 _gen 不符时回调自行清空
        self._played_gen = -1  # 最近真正出过声的代次；用来判断打断时上一轮是否已开口
        # 统计计数：回调里只自增，主循环周期打日志
        # 保活噪声：预生成 1s 循环使用（回调里不做随机数生成）
        self._noise = (np.random.default_rng(0).standard_normal(self.sr) * 10 ** (COMFORT_NOISE_DBFS / 20)).astype(np.float32)
        self._noise_pos = 0
        self._play_events: deque[tuple[float, float, int]] = deque(maxlen=200)  # (墙钟, 时长秒, 代次)，回调追加、主循环打日志
        self._turn_first = False  # 本轮第一句还没入队（大脑线程读写）：第一句前补静音前导
        # 识别取证：原始麦克风帧环形缓冲 (该帧对应的 16k 起始样本号, 原始帧)，只有前端线程读写
        self._raw_ring: deque[tuple[int, np.ndarray]] = deque(maxlen=RAW_RING_SEC * 100)
        self._vad_pos = 0  # 已喂给主 VAD 的 16k 样本数，与 VAD 段的 start 对齐
        self._dropped = 0  # in_q 满丢掉的帧数
        self._underflows = 0  # 输出欠载次数
        # 半句合并：上一轮还没出声就被打断，说明用户话没说完，把上一轮文本拼到下一句前面重发
        self._last_text = ""  # 最近一次送进大脑的文本
        self._last_text_t = 0.0  # 最近一次送进大脑的时刻（monotonic），续说窗口从这里算
        self._carry = ""  # 待拼接到下一句前面的文本
        self._reply_gen = -1  # 最近收到首 token 的代次：等于当前代次说明大脑已开始回复，不再允许续说拼接
        self._tts_fails = 0  # 本轮 TTS 连续失败次数（只有大脑线程读写）
        # 运行态：_paused 由监控回调置位/清除，主循环据此停开音频流；前端/大脑线程见到它就丢弃输入
        self._paused = threading.Event()
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
        self.brain = Brain()
        # 大脑模式：front-back=前台直连硅基说话 + 后台网关干活（默认）；legacy=旧单线网关
        self.brain_mode = os.environ.get("GUANJIA_BRAIN_MODE", "front-back")
        self.front = Front() if self.brain_mode != "legacy" else None
        self.backoffice = BackOffice(on_done=self._on_task_done) if self.front else None
        self.tts = TTSClient(timeout=TTS_TIMEOUT_SEC)
        # 前端线程状态（只有前端线程读写）
        self._asr_stream = self.asr.new_stream()  # 当前这句话的 ASR 流，每段结束换新流
        self._preroll: deque[np.ndarray] = deque(maxlen=PREROLL_MS // 10)  # 最近 0.5s 的 16k 块
        self._was_speech = False  # 上一块 VAD 是否处于人声状态，用于捕捉起判沿
        self._reset_barge()

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
        # 叠加保活噪声（循环取预生成缓冲）：输出流从不"纯零"，音响不会因静默进入待机
        n0 = self._noise_pos
        idx = (np.arange(n0, n0 + frames)) % len(self._noise)
        out += self._noise[idx]
        self._noise_pos = (n0 + frames) % len(self._noise)
        outdata[:, 0] = out
        try:
            self.in_q.put_nowait(Frames(mic=indata[:, 0].copy(), ref=out, playing=filled > 0))
        except queue.Full:
            self._dropped += 1

    def _reset_barge(self) -> None:
        """打断检测状态归零（启动 / 游戏态恢复时调用，只有前端线程读写）。"""
        self.barge_vad = VAD(min_speech=BARGE_VAD_MIN_SPEECH, min_silence=BARGE_VAD_MIN_SILENCE)  # 只要门控，不取段
        self._since_ref_ms = 10**9  # 距最近一帧"参考信号非零"（我们在出声）的毫秒数
        self._play_ms = 0  # 本次连续播放已持续的毫秒数（用于保护期）
        self._floor = BARGE_RMS_MIN * self.mic_gain  # AEC 后回声残留的自适应底噪（RMS，增益后）
        self._energy = 0.0  # AEC 后 16k 信号的平滑 RMS
        self._hot_ms = 0  # 当前人声里"能量过线"的累计毫秒
        self._mon_ms, self._mon_peak, self._mon_raw = 0, 0.0, 0.0  # 监测日志计时、区间能量峰值、区间原始输入峰值

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
            try:
                self._frontend_step(fr)
            except Exception:  # noqa: BLE001  单帧出错不能让整条听觉链"变聋"
                log.exception("frontend error")

    def _frontend_step(self, fr: Frames) -> None:
        """处理一帧：AEC → 16k → 增益 → VAD → 预滚/ASR → 打断判定 → 段结束出文字。"""
        # 1) 回声消除（参考 = 我们刚播出的这帧）+ 2) 降采样到 16k + 软件增益（注册链 enroll 走同一条）
        x = aec_cancel(self.aec, fr.mic, fr.ref) if self.aec else fr.mic  # AEC 关：打断底噪等也按原始输入算
        y = apply_mic_gain(self.down.process(x), self.mic_gain)
        self._raw_ring.append((self._vad_pos, fr.mic))
        # 3) VAD
        self.vad.push(y)
        self._vad_pos += len(y)
        speaking_now = self.vad.is_speech()
        if speaking_now:
            if not self._was_speech:  # 起判沿：VAD 要人声满 0.25s 才翻 true，先把预滚补喂进 ASR
                for c in self._preroll:
                    self.asr.feed(self._asr_stream, c, SR16K)
            self.asr.feed(self._asr_stream, y, SR16K)
        self._preroll.append(y)
        self._barge_step(y, fr.playing, float(np.max(np.abs(fr.mic))) if len(fr.mic) else 0.0)
        self._was_speech = speaking_now
        # 4) 语音段结束 → 声纹 → 取文字
        for seg in self.vad.pop_segments():
            stream, self._asr_stream = self._asr_stream, self.asr.new_stream()  # 每句一条新流，不带上一句残留
            if seg.duration < MIN_UTT_SEC:
                continue
            self.asr.feed(stream, np.zeros(SR16K * 3 // 10, dtype=np.float32), SR16K)  # 补 0.3s 静音冲刷尾字
            self.asr.finish(stream)
            stream_text = self.asr.text(stream)  # 流式结果，留着与精识别对比
            # 精识别：只要段够长就送（远场时流式常出空文本，精识别更抗噪）；seg.samples 已是加过增益的信号
            text = self.asr_final.transcribe(seg.samples, fallback=stream_text)
            self._dump_segment(seg, text)
            if not text:  # 精识别也为空：不过声纹、不算拒绝，直接丢
                log.debug("丢弃空文本段 %.2fs", seg.duration)  # 流式、精识别均为空
                continue
            who, score = self.gate.identify(seg.samples) if self.gate else ("anyone", 1.0)
            if self.gate and who is None:
                if self.gate_mode == "enforce":
                    log.info("拒绝：非注册声音 score=%.2f text=%r (stream: %r)", score, text, stream_text)
                    continue
                who = "未识别(放行)"  # log 模式：只记录不拦截
            log.info("[%s %.2f] %s  (stream: %s)", who, score, text, stream_text)
            if self.state == "SPEAKING":
                self._new_turn_over_old()
            line_text = text  # 本句精识别文本（拼接前）
            with self._lock:
                if self._carry:
                    text, self._carry = self._carry + text, ""
                self._last_text, self._last_text_t = text, time.monotonic()
            if text != line_text:
                log.info("续说拼接后发送: %s", text)
            self.text_q.put(text)

    def _new_turn_over_old(self) -> None:
        """旧轮还在进行时用户又说完一句：窗口内且大脑没开口 → 续说（拼接）；否则中止旧轮，新句单独成一轮。"""
        in_window = time.monotonic() - self._last_text_t <= CONTINUE_WINDOW_SEC
        replied = self._reply_gen == self._gen or self._played_gen == self._gen
        if in_window and not replied:
            self.stop_speaking("用户续说", carry=True)
        else:
            self.stop_speaking("新一轮")

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
        """落盘一段送识别的音频：_raw.wav（设备采样率原始）+ _proc.wav（AEC+增益+16k），只留最近 SEG_KEEP 段。"""
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

    def _barge_step(self, y: np.ndarray, ref_active: bool, raw_peak: float = 0.0) -> None:
        """打断判定（每 10ms 一帧，前端线程）：

        1. 用回调标记的"本块写出了真实语音"判断我们是否在出声（帧同步；保活噪声不算），带 BARGE_TAIL_MS 尾巴；
        2. 播放中持续学习 AEC 后回声残留的底噪（只在没过线的帧上更新，用户开口时底噪不被抬高）；
        3. 本轮已出声 + 过了保护期 + 快速 VAD 判人声 + 平滑能量 > max(下限, 底噪×倍数)，累计 ≥ BARGE_MS 即打断。
        还没出声时（大脑在想）一律不打断，避免"没出声就被打断"；那种情况由整句结束时的"续说"处理。
        """
        self.barge_vad.push(y)
        self.barge_vad.pop_segments()  # 丢弃分段，防内部缓冲增长
        voice = self.barge_vad.is_speech()
        self._since_ref_ms = 0 if ref_active else self._since_ref_ms + 10
        playing = self._since_ref_ms <= BARGE_TAIL_MS
        rms = float(np.sqrt(np.mean(y * y))) if len(y) else 0.0
        self._energy += BARGE_SMOOTH_ALPHA * (rms - self._energy)
        if not playing:
            self._play_ms, self._hot_ms, self._mon_ms, self._mon_peak, self._mon_raw = 0, 0, 0, 0.0, 0.0
            return
        self._play_ms += 10
        line = max(BARGE_RMS_MIN * self.mic_gain, self._floor * BARGE_FLOOR_RATIO)
        hot = voice and self._energy > line
        if not hot:  # 没过线的播放帧 = 回声残留样本，更新底噪
            self._floor += BARGE_FLOOR_ALPHA * (self._energy - self._floor)
        self._hot_ms = self._hot_ms + 10 if hot else (self._hot_ms if voice else 0)
        self._mon_ms += 10
        self._mon_peak = max(self._mon_peak, self._energy)
        self._mon_raw = max(self._mon_raw, raw_peak)  # AEC 之前的原始输入峰值：区分"硬件给零"与"AEC 抹零"
        if self._mon_ms >= BARGE_LOG_EVERY_MS:
            log.debug("barge-mon raw_peak=%.5f floor=%.4f line=%.4f peak=%.4f hot=%dms voice=%s", self._mon_raw,
                      self._floor, line, self._mon_peak, self._hot_ms, voice)
            self._mon_ms, self._mon_peak, self._mon_raw = 0, 0.0, 0.0
        if (self.state == "SPEAKING" and self._played_gen == self._gen and self._play_ms >= BARGE_GUARD_MS
                and self._hot_ms >= BARGE_MS):
            log.info("barge-in: hot=%dms energy=%.4f floor=%.4f", self._hot_ms, self._energy, self._floor)
            self._hot_ms = 0
            self.stop_speaking("barge-in")

    # ---------- 大脑线程 ----------
    def _brainloop(self) -> None:
        while not self._stop_evt.is_set():
            try:
                text = self.text_q.get(timeout=0.2)
            except queue.Empty:
                continue
            if self._paused.is_set():  # 游戏态：不出声，积压的话直接丢掉
                continue
            try:
                self._handle_text(text)
            except Exception:  # noqa: BLE001  一轮出错不能让大脑线程退出
                log.exception("brain loop error")

    def _handle_text(self, text: str) -> None:
        """一轮对话：流式取回复 → 按句合成入队 → 等播完；任何时刻换代即放弃。"""
        gen = self._begin_speaking()
        self._tts_fails = 0
        self._turn_first = True
        try:
            splitter = SentenceSplitter(first_hard_max_chars=FIRST_SENT_MAX_CHARS)
            reply = []
            try:
                stream = self._open_stream(text)
                try:
                    alive = True  # False = 被换代或 TTS 连续失败，放弃本轮剩余内容
                    for piece in stream:
                        if gen != self._gen:
                            break
                        self._reply_gen = gen  # 已收到首 token：之后的新句不再按续说拼接
                        reply.append(piece)
                        for sent in splitter.push(piece):
                            if not self._say(sent, gen):
                                alive = False
                                break
                        if not alive:
                            break
                    else:
                        for sent in splitter.flush():
                            if not self._say(sent, gen):
                                break
                finally:
                    stream.close()
            except Exception as e:  # noqa: BLE001
                if gen == self._gen:
                    log.error("brain error: %s", e)
                else:
                    log.debug("brain stream aborted by barge-in: %s", e)
            log.info("reply: %s", "".join(reply))
            if self.front and not isinstance(text, Task):  # 前台本轮若派了任务，交后台异步执行
                task = self.front.take_task()
                if task:
                    self.backoffice.dispatch(task)  # 新任务 / 确认续跑 / 取消
            if gen == self._gen and self._tts_fails >= TTS_MAX_FAILS:  # TTS 挂了：本轮结束并复位，不阻塞下一轮
                self.stop_speaking("TTS 连续失败")
            # 等播完再回到 LISTENING（打断时 stop_speaking 已经切了）
            while gen == self._gen and self._playing() and not self._stop_evt.is_set():
                time.sleep(0.05)
        finally:
            with self._lock:
                if gen == self._gen:
                    self.state = "LISTENING"

    def _open_stream(self, item):
        """按模式选本轮的回复流：legacy 走网关；front-back 下用户话走前台，后台结果走前台改写。"""
        if self.front is None:
            return self.brain.stream(item, system=_system_prompt())
        if isinstance(item, Task):
            return self.front.rephrase(item.id, item.text, item.status, item.result)
        return self.front.stream(item, active=self.backoffice.active(), awaiting=self.backoffice.awaiting())

    def _on_task_done(self, t: Task) -> None:
        """后台线程回调：结果排进大脑队列，正在播报时自然排在当前这轮之后说。"""
        if t.reply_when_done or t.status in ("awaiting_confirm", "expired"):  # 要确认 / 作废必须说给主人
            self.text_q.put(t)
        else:
            self.front.note_done(t.id, t.text, t.status, t.result)

    def _say(self, sent: str, gen: int) -> bool:
        """合成并入队一句；代次不符则放弃。返回本轮是否该继续（被换代或 TTS 连续失败达上限返回 False）。"""
        if gen != self._gen:
            return False
        try:
            pcm, sr = self.tts.synth(sent)
        except Exception as e:  # noqa: BLE001
            if gen != self._gen:
                return False
            self._tts_fails += 1
            log.error("tts error (%d/%d): %s", self._tts_fails, TTS_MAX_FAILS, e)
            if self._tts_fails >= TTS_MAX_FAILS:
                log.warning("TTS 连续失败 %d 次，结束本轮：%r", self._tts_fails, sent)
                return False
            return True  # 放弃这一句，继续下一句
        self._tts_fails = 0
        if gen != self._gen:
            return False
        if sr != self.sr:
            x_old = np.linspace(0, 1, len(pcm), endpoint=False)
            x_new = np.linspace(0, 1, int(len(pcm) * self.sr / sr), endpoint=False)
            pcm = np.interp(x_new, x_old, pcm).astype(np.float32)
        if self._turn_first:  # 本轮第一句：补静音前导
            pcm = np.concatenate([np.zeros(self.sr * LEAD_SILENCE_MS // 1000, dtype=np.float32), pcm])
            self._turn_first = False
        self.play_q.put((gen, pcm))  # 即使此刻恰好被打断，回调也会按代次丢掉它
        return True

    def _begin_speaking(self) -> int:
        with self._lock:
            self.state = "SPEAKING"
            return self._gen

    def stop_speaking(self, reason: str, carry: bool = False) -> None:
        """打断：换代（回调自行丢弃旧代音频）、关闭进行中的网关流和 TTS 请求、清播放队列、回到监听。

        carry=True 仅用于续说：把上一句存进 _carry，由前端拼到新句前面重发。
        """
        with self._lock:
            self._gen += 1
            self.state = "LISTENING"
            if carry and self._last_text:
                self._carry = self._last_text
            # 在锁内关闭：_begin_speaking 也取锁，保证只关到旧代次的请求
            self.brain.abort()
            if self.front:
                self.front.abort()  # 只停前台流；后台任务不受打断影响
            self.tts.abort()
        while True:  # 清播放队列（回调也会按代次丢弃，这里立即清掉省得积压）
            try:
                self.play_q.get_nowait()
            except queue.Empty:
                break
        log.info("stop speaking (%s)", reason)

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
        """主动侧（S3）查询：游戏中或开机/游戏后 5 分钟静默期内返回 True，不许主动开口。"""
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
        self.vad = VAD()
        self._vad_pos = 0
        self._raw_ring.clear()
        self._asr_stream = self.asr.new_stream()
        self._preroll.clear()
        self._was_speech = False
        self._reset_barge()
        s.start()
        threading.Thread(target=self.tts.control, args=("load",), name="tts-load", daemon=True).start()
        log.info("gaming 结束: 音频流已恢复")

    # ---------- 主循环 ----------
    def run(self) -> None:
        log.info("device rate %d, in=%s out=%s, speakers=%s(%s), gate=%s@%.2f, mic_gain=%.1f, aec=%s, barge=%dms guard=%dms ratio=%.1f, "
                 "tts_timeout=%.0fs", self.sr, self.in_dev, self.out_dev, self.gate.names() if self.gate else "off",
                 self.gate.model.stem if self.gate else "-", self.gate_mode, self.spk_threshold, self.mic_gain, "on" if self.aec else "off", BARGE_MS, BARGE_GUARD_MS, BARGE_FLOOR_RATIO,
                 TTS_TIMEOUT_SEC)
        threads = [threading.Thread(target=self._frontend, name="frontend", daemon=True),
                   threading.Thread(target=self._brainloop, name="brain", daemon=True)]
        for t in threads:
            t.start()
        failed = False
        last_stats, stats = time.monotonic(), (0, 0)
        with sd.Stream(samplerate=self.sr, blocksize=self.frame, dtype="float32", channels=1, latency="low",
                       device=(self.in_dev, self.out_dev), callback=self._callback) as s:
            log.info("listening ... (latency in/out %.3f/%.3f s; output stream always-open, comfort noise %.0f dBFS, "
                     "lead %dms, dump %s)", *s.latency, COMFORT_NOISE_DBFS, LEAD_SILENCE_MS, SEG_DUMP_DIR)
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
                        log.error("worker died: threads=%s stream_active=%s，退出等待自启拉起", dead, s.active)
                        failed = True
                        break
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
                pass
        self._stop_evt.set()
        self.runtime.stop()  # 最后一次心跳，下次开机据此算离线区间
        if failed:
            sys.exit(1)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="in_dev", type=int, default=None)
    ap.add_argument("--out", dest="out_dev", type=int, default=None)
    ap.add_argument("--no-gate", action="store_true", help="关闭声纹放行（调试用）")
    ap.add_argument("--spk-threshold", type=float, default=SPK_THRESHOLD, help="声纹放行阈值")
    ap.add_argument("--gate-mode", choices=("enforce", "log"), default=SPK_GATE_MODE, help="log=只记录声纹分不拦截")
    ap.add_argument("--mic-gain", type=float, default=MIC_GAIN, help="AEC 后软件增益（tanh 软限幅）")
    ap.add_argument("--aec", choices=("on", "off"), default="on" if AEC_ENABLED else "off", help="speex AEC 开关（默认关）")
    ap.add_argument("-v", action="store_true")
    ap.add_argument("--log-file", default=None, help="同时把日志写到这个文件（控制台照常输出）")
    a = ap.parse_args()
    handlers = [logging.StreamHandler(sys.stdout)]  # 走 stdout，避免 PowerShell 把 stderr 日志当成红色错误
    if a.log_file:
        handlers.append(logging.FileHandler(a.log_file, encoding="utf-8"))
    logging.basicConfig(level=logging.DEBUG if a.v else logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=handlers)
    VoiceDaemon(in_dev=a.in_dev, out_dev=a.out_dev, speaker_gate=not a.no_gate, spk_threshold=a.spk_threshold,
                gate_mode=a.gate_mode, mic_gain=a.mic_gain, aec_on=a.aec == "on").run()


if __name__ == "__main__":
    main()

# ---------- 评审未修项（voice-review.md 编号）----------
# P2-10 MIN_UTT_SEC 过滤无效：段内回看/尾静音的实际占比需实录测量后再定判据，盲调会误伤"开灯"这类短指令。
# P2-11 identify 仍在前端线程同步执行：长段 CAM++ 会短暂阻塞打断判定；已加丢帧计数日志，移到独立线程待实测确有积压再做。
# P2-15 TTS 仍用线性插值重采样：resample_poly 需要 scipy，本次约束不引入新依赖；手写多相滤波超出顺手修范围。
# P1-4 余项：latency='low' 已设，但 AEC 对齐需在家用真实 TTS wav 重跑 aec_probe 实测；BARGE_FLOOR_RATIO / BARGE_RMS_MIN 按 barge-mon 日志实测标定。
