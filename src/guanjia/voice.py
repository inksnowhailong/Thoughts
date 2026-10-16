"""语音守护进程：常开监听 → 增益 → VAD → 流式 ASR → 整段精识别 → 碎音过滤 → 声纹 → 唤醒词 → 主线会话 → 按句 TTS → 播放；随时可打断。

P2.1 起的调用链：

  音频回调 ──(麦帧, 本块是否在出声)──> 前端线程 ── VAD/ASR ∥ 声纹认人（并行）──> decide()：碎音过滤 → 唤醒词（免喊窗口内可省）；声纹只标「是谁说的」，不拦截
                                          │                          ├─ 放行 ──> 提示音 + text_q（去掉唤醒词的话）
                                          │                          ├─ 只喊了唤醒词 ──> 应答「在呢」+ 打开免喊窗口
                                          │                          └─ 不理（记一条带原因的记录）
                                          └─ 打断检测 BargeDetector ──> stop_speaking()：换代 + 清播放队列
  大脑线程：text_q ──> TurnRunner ──> 网关 /v1/chat/completions（x-openclaw-session-key: agent:main:main）
                        流式文本 → 切句 → speakable 过滤 → TTS → play_q；打断后照样把网关流读完
  免喊窗口：它回完最后一句后 follow_up_sec 秒内、或一轮正在进行时，不用再喊唤醒词
  控制口 127.0.0.1:18795：/say /status /sleep-state（本机程序）+ /panel 和 /api/*（管家面板，要令牌）
  调试面板：每个环节发结构化事件到 EventHub（events.py），面板用 SSE 实时显示；工具调用从分级插件审计文件跟随（audittail.py）
  可调参数在 data\\voice-settings.json（settings.py），面板改了热生效
  托盘：在听 / 说话中 / 静音 / 游戏中 / 出错，左键切静音，右键打开面板、重启或退出

运行态（runtime_state.RuntimeMonitor，D25/D26）：进入 GAMING 时停音频流、丢弃积压、释放 ASR、通知 TTS /unload；离开时重建。
退出码（scripts/voice-supervisor.ps1 据此决定）：0 = 托盘/面板「退出语音」不再拉起；3 = 「重启语音」立即拉起；其它 = 崩溃，退避后拉起。
"""

from __future__ import annotations

import argparse
import faulthandler
import inspect
import logging
import logging.handlers
import os
import queue
import re
import sys
import threading
import time
import wave
import webbrowser
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path

import numpy as np
import sounddevice as sd
from pyaec import Aec

from guanjia import elog
from guanjia.asr import StreamingASR
from guanjia.asr_final import FinalASR, FinalResult
from guanjia.audio_cues import earcon
from guanjia.announcement import AnnouncementPolicy
from guanjia.audio_watchdog import AudioGuard, InputWatchdog
from guanjia.audittail import AuditTail, PendingConfirm
from guanjia.approval_ipc import ApprovalIpc
from guanjia.barge import BargeConfig, BargeDetector
from guanjia.brain import MAIN_SESSION_KEY, Brain, tag_voice_text
from guanjia.control import CONTROL_PORT, start_control_server
from guanjia.enroll_session import (MAX_ZERO_RATIO, MIN_EFFECTIVE_SEC, MIN_PEAK, MIN_SELF_SIM, MIN_TOTAL, EnrollSession)
from guanjia.events import RECORDS_FILE, EventHub, HubLogHandler, NullHub
from guanjia.panel import PANEL_URL, PanelBackend
from guanjia.memory_page import MemoryService
from guanjia.prompts import PromptService
from guanjia.program_context import classify_media, foreground_app, snapshot
from guanjia.program_quiet import ProgramQuietMonitor
from guanjia.runtime_state import ACTIVE, GAMING, RuntimeMonitor
from guanjia.sleep.rehearsal import SleepRehearsal
from guanjia.sleep.scheduler import RehearsalScheduler
from guanjia.sleep.watermark import read_main_watermark
from guanjia.runtrace import run_summary
from guanjia.seg_filter import is_hollow_segment, is_unconfirmed_by_stream
from guanjia.settings import SETTINGS_PATH, Settings, SettingsStore, to_dict
from guanjia.speech_shape import SHAPED_PREFIX, SpeechShaper
from guanjia.speaker import SPK_MODEL, STORE, SpeakerGate, backup_path
from guanjia.status import DISPLAY_NAME, Health, HealthMonitor, TrayView, tcp_alive, tray_view
from guanjia.task_manager import TaskManager
from guanjia.task_dispatch import TaskDispatchScheduler
from guanjia.task_executor import TaskNotice, TaskWorkerExecutor
from guanjia.task_ipc import TaskIpc
from guanjia.task_service import TaskService
from guanjia.tts_client import TTSClient
from guanjia.turns import (NUDGE_TEXT, NUDGE_TEXT_FAKE_BLOCK, Heard, SayItem, TurnRunner, claims_blocked, has_promise,
                           needs_fake_block_nudge, needs_nudge)
from guanjia.vad import SR as SR16K
from guanjia.vad import VAD, Downsampler
from guanjia.wake import WakeMatcher, decide
from guanjia.wsfiles import WorkspaceFiles

log = logging.getLogger("guanjia.voice")
_PROC_T0 = time.monotonic()  # 进程（本模块被导入）的起点，voice.ready 的 boot_ms 据此计算

# ---- 声纹认人（只标是谁说的，不拦截；认人门槛在 data\voice-settings.json 的 spk_threshold，面板可调）----
SPK_GATE_MODE = "enforce"  # 已废弃：声纹不再拦截，两个值效果相同；只为启动参数 --gate-mode 兼容而留
SPK_TIMEOUT_SEC = 1.0  # 声纹最多等这么久（从与二次识别同时起算）：超时 / 出错就当没有标签继续，不阻塞这一句
OWNER_NAME = "我"  # 声纹库里主人注册时用的名字
OWNER_LABEL = "主人"  # 库里名字是「我」的，对模型和面板显示成这个
UNKNOWN_LABEL = "未知"  # 有声纹库、但最高分没过认人门槛


def speaker_label(name: str | None, score: float | None) -> str:
    """声纹结果 → 说话人标签。score=None（没有声纹库 / 没过声纹 / 超时出错）→ ""（不标）；
    过了认人门槛 → 库里的名字（「我」显示为「主人」）；没过 → 「未知」。"""
    if score is None:
        return ""
    if name is None:
        return UNKNOWN_LABEL
    return OWNER_LABEL if name == OWNER_NAME else name


def _identify_job(gate: SpeakerGate, samples: np.ndarray) -> tuple[str | None, float | None, int]:
    """声纹线程里跑：(过门槛的名字或 None, 最高分, 耗时 ms)。"""
    t0 = time.monotonic()
    name, score = gate.identify(samples)
    return name, score, round((time.monotonic() - t0) * 1000)

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
FIRST_SENT_SOFT_MIN = 6  # 首句在逗号处最早第 6 个字就切（P2 是 8）：首句短一点，合成快 0.3~0.6 秒
PLAY_OVERLAP_GUARD_SEC = 0.3  # 注册时：段开始于最后一次出声后不到这么久，当作混进了回声/播放，不收
MIN_UTT_SEC = 0.4        # 短于此的语音段不当作指令
PREROLL_MS = 500         # ASR 预滚：VAD 起判前这段音频先补喂 ASR，防首字丢失
STATS_EVERY_SEC = 30     # 丢帧 / 欠载计数的日志周期
AUDIT_LAG_SEC = 0.8      # 判「零工具调用」前再等这么久：工具调用从审计文件跟随（每 0.5 秒读一次），可能比回复结束晚到
APPROVAL_FOLLOWUP_SEC = 30.0
APPROVAL_RETRY_MARKER = "__guanjia_approved_retry__"
_APPROVE_WORDS = frozenset({"确认", "确定", "允许", "可以", "行", "好", "好的", "好呀", "好啊", "嗯", "对", "执行", "执行吧", "同意", "没问题", "去吧", "弄吧", "做吧", "关吧", "删吧", "装吧", "开吧", "放行"})
_REJECT_WORDS = frozenset({"不", "不要", "别", "取消", "算了", "等等", "等一下", "暂停", "停", "慢着", "不允许", "不行"})
_APPROVE_PREFIX = ("允许", "确认", "可以", "行", "好")
_DENY_OR_QUESTION = ("不", "别", "算了", "取消", "暂停", "等等", "等一下", "停", "慢着", "吗", "？", "?")


def approval_decision(text: str) -> str | None:
    """确定性审批词表；只在守卫已存在 pending 时调用。"""
    normalized = re.sub(r"[\s，,。.!！?？~～、】【、]", "", text)
    if normalized in _REJECT_WORDS or (len(normalized) <= 12 and any(word in normalized for word in _REJECT_WORDS)):
        return "reject"
    if any(word in normalized for word in _DENY_OR_QUESTION):
        return None
    if normalized.startswith(("好了好了", "好家伙")):
        return None
    if len(normalized) <= 12 and (normalized in _APPROVE_WORDS or any(normalized.startswith(prefix) for prefix in _APPROVE_PREFIX)):
        return "approve"
    return None

# ---- 打断（只在我们出声时判定；不看声纹、不看 VAD，原因见 barge.py；下限在设置 barge_rms_min）----
BARGE_MS = 180           # 累计过线时长达到此值即打断
BARGE_GUARD_MS = 300     # 每次开始出声后的保护期：只学回声底噪
BARGE_TAIL_MS = 300      # 参考信号停了以后仍按"在播"算的尾巴
BARGE_FLOOR_RATIO = 3.0  # 触发线 = 回声底噪 × 此倍数
BARGE_LOG_EVERY_MS = 1000  # 出声期间每隔这么久打一行 DEBUG barge-mon，用来校准

# ---- 提示音 / 健康检查 / 退出码 ----
SAY_MAX_PENDING = 3      # /say 最多排队这么多段，多了回 busy（模型循环调用 speak、微信刷屏时不至于念上几分钟）
HEALTH_EVERY_SEC = 10.0  # 网关、TTS 在线检查周期（托盘用）
SETTINGS_POLL_SEC = 2.0  # 手工改 voice-settings.json 的发现周期
LAUNCH_SELFCHECK_SEC = 15.0  # launch_app 启动游戏后过这么久记一条输入流自检（只记不动作；2026-10-11 事故：拉起 WeGame 后音频输入悄悄断了）
EXIT_DEADMAN_SEC = 8.0  # 退出清理（关音频流、停托盘…）超过这么久还没完成就强制退出：卡死的驱动会让 stream.stop() 永远不返回
GATEWAY_PORT = 18789
EXIT_QUIT, EXIT_CRASH, EXIT_RESTART = 0, 1, 3  # 与 scripts/voice-supervisor.ps1 约定
LOG_MAX_BYTES = 5_000_000  # voice.log 轮转大小（留 3 份）
TASK_JOURNAL_PATH = Path(__file__).resolve().parents[2] / "data" / "tasks.jsonl"


_hard_exit = os._exit  # 单独留个名字：测试里换成假的，别真把测试进程杀了


def _arm_exit_deadman(code: int, sec: float | None = None) -> threading.Timer:
    """退出清理（关音频流、停托盘…）的保险：到点还没退出就记日志 + 事件后强制以同一个退出码退出，外层看门狗照常处理。
    音频驱动卡死时 stream.stop() / close() 可能永远不返回——进程一直活着、看门狗也看不出异常，就是静悄悄地「聋了」。"""
    sec = EXIT_DEADMAN_SEC if sec is None else sec

    def fire() -> None:
        log.error("退出清理超过 %.0fs 仍未完成，强制退出；voice exit code=%d reason=exit_hang", sec, code)
        elog.emit("voice", "voice.exit", "退出清理卡死，强制退出", level="error", code=code, reason="exit_hang")
        logging.shutdown()
        elog.get().close()  # os._exit 不跑 atexit：先把事件队列写完
        _hard_exit(code)

    t = threading.Timer(sec, fire)
    t.daemon = True
    t.start()
    return t


def install_crash_hooks() -> None:
    """让「无声无息地死」不再发生：未捕获异常、线程崩溃、原生崩溃都先留痕。

    未捕获异常默认只把堆栈打到 stderr，而看门狗每次拉起新进程都会清空 voice-stderr.txt，堆栈就丢了
    （2026-10-11/12 事故：退出码 1、日志和事件流都没有任何痕迹）。这里先写 WARNING+ 日志和 voice.exit 事件，
    再交还给默认钩子打到 stderr。faulthandler 负责段错误之类的原生崩溃（Python 栈打到 stderr）。
    """
    try:
        faulthandler.enable(all_threads=True)
    except (RuntimeError, ValueError, OSError):  # stderr 不是真文件（被测试框架接管等）时不开，不影响启动
        pass

    def on_uncaught(et, ev, tb) -> None:
        if issubclass(et, KeyboardInterrupt):
            sys.__excepthook__(et, ev, tb)
            return
        log.critical("voice exit code=%d reason=uncaught_exception: %s: %s", EXIT_CRASH, et.__name__, ev, exc_info=(et, ev, tb))
        elog.emit("voice", "voice.exit", "未捕获异常，进程退出", level="error", code=EXIT_CRASH, reason="uncaught_exception",
                  error=f"{et.__name__}: {ev}")
        sys.__excepthook__(et, ev, tb)

    def on_thread(args) -> None:
        if args.exc_type is SystemExit:
            return
        name = args.thread.name if args.thread else "?"
        log.error("thread %s crashed: %s: %s", name, args.exc_type.__name__, args.exc_value,
                  exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
        elog.emit("voice", "thread.crash", f"线程 {name} 崩溃", level="error", thread=name,
                  error=f"{args.exc_type.__name__}: {args.exc_value}")

    sys.excepthook = on_uncaught
    threading.excepthook = on_thread

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
    gate_mode: str = SPK_GATE_MODE  # 已废弃（声纹不再拦截），保留字段和 --gate-mode 参数只为兼容
    mic_gain: float = MIC_GAIN  # 软件增益
    aec_on: bool = AEC_ENABLED  # 是否走 speex AEC
    tray_on: bool = True  # 托盘图标（--no-tray 关）
    control_port: int = CONTROL_PORT  # 本机控制口
    # 运行态
    state: str = "LISTENING"  # LISTENING / SPEAKING（一轮从开始到播完都算 SPEAKING）
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _stop_evt: threading.Event = field(default_factory=threading.Event)

    # 类级缺省值：单测用 object.__new__ 拼的半个守护进程没有这些属性时用它们（面板事件变成空操作、设置用默认值）
    hub = NullHub()
    store = SettingsStore()
    wake = WakeMatcher(Settings().wake_words)
    _turn_uid = None
    _turn_is_say = False  # 这一轮是 /say（主程序让念）：念的时候、念完都不算免喊窗口
    _turn_tools = 0  # 这一轮已见到的工具调用数（审计跟随累加），「只说没做」兜底据此判断
    _turn_gate: frozenset = frozenset()  # 这一轮已见到的拦截 / 确认类审计 decision，「编造拦截」兜底据此判断
    watchdog = None  # InputWatchdog | None：run() 里创建；音频回调往里喂心跳
    guard = None  # AudioGuard | None：run() 里创建；主循环每 0.5 秒 tick()，确认回调停摆就以重启码退出
    _audio_names = {}  # 输入 / 输出设备名（流打开后查一次），随 audio.* 事件一起记；run() 里整个换掉（不加类型注解：不是 dataclass 字段）
    pending = PendingConfirm()  # 待确认状态（托盘变色 + 气泡）；__post_init__ 换成每个进程自己的一份
    approval_ipc = None
    _turn_gen = -1
    enroll = None  # EnrollSession | None：声纹注册模式，非 None 时前端线程的段只拿来注册（见 _frontend_step）
    _spare_gate = None  # SpeakerGate | None：库为空/读不出时守护进程不用的那把声纹门，留给注册用
    _spk_pool = None  # ThreadPoolExecutor | None：声纹认人的单线程池（只有前端线程提交，首次用到时才建）
    _turn_speaker = ""  # 这一轮语音标记里的说话人（大脑线程读写；追发的话没有）
    _last_play_t = -1.0  # 前端线程：最近一次真正出声时的 VAD 时间轴（秒），用来判断一段话有没有和播放重叠
    _turn_marks = None
    _last_health: dict | None = None  # 上一次记过的依赖状态（B4 dep.change 只记变化）
    _last_program_snapshot: dict | None = None
    program_quiet = None
    announcements = None
    task_manager = None
    tasks = None
    task_ipc = None
    task_executor = None
    _followup_until = 0.0
    _ack_pcm = None

    def __post_init__(self) -> None:
        self.sr = pick_stream_rate(self.in_dev, self.out_dev)  # 优先 16k，见 pick_stream_rate
        self.frame = self.sr // 100
        self.in_q: queue.Queue[Frames] = queue.Queue(maxsize=500)
        self.play_q: queue.Queue[tuple[int, np.ndarray]] = queue.Queue()  # (代次, PCM)，回调按代次丢弃旧音频
        self.text_q: queue.Queue[str | Heard | SayItem] = queue.Queue()  # 放行的话（Heard）或 /say 要念的话（SayItem）
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
        self._muted = threading.Event()  # 托盘/面板静音：不听不说
        self._front_dirty = False  # 静音期间前端丢过帧，恢复时先复位 VAD/ASR（只有前端线程读写）
        self._exit_code: int | None = None  # 托盘/面板要求的退出码
        self.sleep_state = "awake"  # P4 休眠状态位，由控制口 /sleep-state 设置，托盘和面板显示
        self.last_error = ""  # 最近一轮的错误（网关 / TTS），托盘显示；下一轮成功出声后清空
        self.health = Health()  # 网关、TTS 是否在线（HealthMonitor 更新）
        self.runtime = RuntimeMonitor(self._on_runtime)
        # 当前没有覆盖微信/其他入口的全主线实时水位，故即使开关打开也 fail-closed。
        self._main_activity_at: float | None = None
        self.sleep_rehearsal = RehearsalScheduler(
            SleepRehearsal(), watermark=lambda: read_main_watermark().last_activity_at,
            watermark_reliable=lambda: read_main_watermark().valid,
            emit=lambda event, message, data: elog.emit("sleep", event, message, **data),
            enabled=os.environ.get("GUANJIA_SLEEP_REHEARSAL", "") == "1",
        )

        self.store = SettingsStore(SETTINGS_PATH)
        self.program_quiet = ProgramQuietMonitor(lambda: self.store.current.program_quiet, on_change=self._on_program_quiet_change)
        self.announcements = AnnouncementPolicy(lambda: self.program_quiet.quiet_active)
        self.task_manager = TaskManager.restore(TASK_JOURNAL_PATH)
        self.tasks = TaskService(self.task_manager)
        self.task_executor = TaskWorkerExecutor(self.tasks, policy=self.announcements, on_notice=self._on_task_notice)
        self.task_dispatch = TaskDispatchScheduler(self._emit_task_dispatch)
        self.hub = EventHub(records_file=RECORDS_FILE, elog=elog.get())  # 面板记录的每次变化同时派生统一事件日志
        s = self.store.current
        self.wake = WakeMatcher(s.wake_words)
        self.down = Downsampler(self.sr)
        self.aec = make_aec(self.sr) if self.aec_on else None  # 关掉时前端直接用原始输入
        self.vad = VAD()
        self.gate = SpeakerGate(threshold=s.spk_threshold) if self.speaker_gate else None
        self._enroll_lock = threading.Lock()  # 串行化面板的声纹注册动作（控制口是多线程的）
        if self.gate and not self.gate.names():
            log.warning("声纹库为空，默认放行所有声音；请先用面板「声纹」卡片或 python -m guanjia.enroll 注册")
            self._spare_gate, self.gate = self.gate, None  # 门不用，但留着给注册（省一次模型加载，也能报出读库失败的原因）
        self.asr = StreamingASR()
        # 整段精识别：FireRedASR2（WSL 里的 GPU 服务）为主、SenseVoice 回退；段结束后替换流式结果，失败回退流式
        self.asr_final = FinalASR(on_event=self._asr_event)
        self.brain = Brain(session_key=MAIN_SESSION_KEY)  # 主线会话；流不可提前关（见 brain.py）
        self.tts = TTSClient(timeout=TTS_TIMEOUT_SEC)
        self.runner = TurnRunner(
            open_stream=self._open_stream,
            synth=self.tts.synth, enqueue=self._enqueue_pcm, current_gen=lambda: self._gen,
            first_max_chars=FIRST_SENT_MAX_CHARS, max_tts_fails=TTS_MAX_FAILS, first_soft_min=FIRST_SENT_SOFT_MIN,
            on_event=self._on_turn_event, shaper=self._new_shaper)
        self._earcon = earcon(self.sr)
        self._ack_text = ""  # _ack_pcm 是哪句应答合成的
        self.barge = BargeDetector(BargeConfig(guard_ms=BARGE_GUARD_MS, tail_ms=BARGE_TAIL_MS, rms_min=s.barge_rms_min,
                                               floor_ratio=BARGE_FLOOR_RATIO, trigger_ms=BARGE_MS))
        self._mon_ms, self._mon_peak = 0, 0.0  # barge-mon 日志计时与区间 RMS 峰值（只有前端线程读写）
        self.monitor = HealthMonitor({"gateway": lambda: tcp_alive("127.0.0.1", GATEWAY_PORT), "tts": self.tts.alive},
                                     on_change=self._on_health, interval=HEALTH_EVERY_SEC)
        self.panel = PanelBackend(self.hub, self.store, status=self.status, control=self._panel_control, inject=self.inject)
        self.panel.register_get("/api/speaker", self._speaker_info)
        self.panel.register_post("/api/speaker", self._speaker_action)
        ws_files = WorkspaceFiles()  # 提示词页和记忆页共用同一套锁 / 备份 / 审计
        self.panel.attach_memory(MemoryService(ws_files, elog.get()))  # B3：记忆页
        self.panel.attach_prompts(PromptService(ws_files), self._param_consts)  # B2：参数表 + 提示词编辑
        self.panel.attach_flags(elog.get())  # 「这句有问题」+ 面板改设置 / 控制进事件流
        self.pending = PendingConfirm()
        self.approval_ipc = ApprovalIpc()
        self.audit = AuditTail(self._on_tool, on_gate=self._on_gate)
        self.store.subscribe(self._apply_settings)
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
        wd = self.watchdog
        if wd is not None:  # 喂输入流看门狗：回调还在来 / 输入有没有非零样本 / 本块是否出了真语音
            wd.beat(bool(indata.any()), voiced)
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

    def _open_stream(self, text: str):
        """一轮语音对话发给网关：加上「[语音 日期 时间 周几 说话人]」标记（没有说话人时与以前一致）。"""
        return self.brain.stream(tag_voice_text(text, speaker=self._turn_speaker))

    def _reset_frontend(self) -> None:
        """VAD / ASR 流 / 预滚 / 打断状态全部复位（游戏态恢复、静音恢复时调用）。"""
        self.vad = VAD()
        self._vad_pos = 0
        self._last_play_t = -1.0
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
        if fr.playing:
            self._last_play_t = self._vad_pos / SR16K
        speaking_now = self.vad.is_speech()
        if speaking_now:
            if not self._was_speech:  # 起判沿：VAD 要人声满 0.25s 才翻 true，先把预滚补喂进 ASR
                self.hub.stage(None, "vad", "active", "有人在说话")
                self._prewarm_final()  # 让 GPU 识别服务趁这段话还没说完先给显卡加热（空闲时显卡在 P8，冷启动慢一倍多）
                for c in self._preroll:
                    self.asr.feed(self._asr_stream, c, SR16K)
            self.asr.feed(self._asr_stream, y, SR16K)
        elif self._was_speech:
            self.hub.stage(None, "vad", "done", "说完了")
        self._preroll.append(y)
        rms = float(np.sqrt(np.mean(x * x))) if len(x) else 0.0  # 增益前的原始电平，打断阈值按它标定
        self._barge_step(rms, fr.playing)
        self._was_speech = speaking_now
        for seg in self.vad.pop_segments():
            stream, self._asr_stream = self._asr_stream, self.asr.new_stream()  # 每句一条新流，不带上一句残留
            if seg.duration < MIN_UTT_SEC:
                continue
            if self._enroll_take(seg):
                continue
            self.hub.stage(None, "asr", "active", "识别中")
            t_asr = time.monotonic()
            self.asr.feed(stream, np.zeros(SR16K * 3 // 10, dtype=np.float32), SR16K)  # 补 0.3s 静音冲刷尾字
            self.asr.finish(stream)
            stream_text = self.asr.text(stream)  # 流式结果，留着与精识别对比
            # 注册模式的段在上面已被 _enroll_take 拿走，不会走到这里：掏空的段由注册自己的质量检查拒收，这两个过滤只管识别链路
            raw = self._raw_for(seg)
            if is_hollow_segment(seg.samples, raw):  # 噪声门掏空的假触发：不做二次识别（2024 版 SenseVoice 在静音段爱编句子）
                self._drop_segment(seg, "hollow", "噪声门掏空的假触发，丢弃", "hollow", stream_text=stream_text,
                                   zero_frac=round(float(np.mean(raw == 0)), 2) if len(raw) else None)
                continue
            spk_fut = self._submit_identify(seg.samples)  # 声纹认人与二次识别同时跑（推理释放 GIL），都完成后再判定
            asr_res = self.asr_final.transcribe_ex(seg.samples, fallback=stream_text)
            text = asr_res.text
            asr_ms = round((time.monotonic() - t_asr) * 1000)
            if is_unconfirmed_by_stream(text, stream_text):  # 流式一个字没出、二次识别却出了中文：疑似幻觉，当没识别出字
                self._drop_segment(seg, "unconfirmed", "没识别出字（流式没出字，疑似幻觉）", text, text=text, stream_text=stream_text)
                continue
            self._dump_segment(seg, text)
            if not text:  # 精识别也为空：不过声纹、不记录，直接丢
                self.hub.stage(None, "asr", "done", "没识别出字")
                log.debug("丢弃空文本段 %.2fs", seg.duration)
                continue
            self.hub.stage(None, "asr", "done", text[:20])
            speaker, score, spk_ms = self._collect_speaker(spk_fut)
            front_ms = round((time.monotonic() - t_asr) * 1000)  # 识别 ∥ 声纹 并行后的总耗时
            log.debug("front asr=%dms speaker=%sms parallel_total=%dms", asr_ms, spk_ms, front_ms)
            self._on_heard(text, score, source="mic", stream_text=stream_text, seg_sec=seg.duration,
                           asr_ms=asr_ms, spk_ms=spk_ms, front_ms=front_ms, speaker=speaker, asr=asr_res)

    def _prewarm_final(self) -> None:
        """起判沿时调：二次识别器若是 GPU 后端，非阻塞地让服务给显卡加热；没有该能力（老式假识别器）或已释放时什么都不做。"""
        prewarm = getattr(getattr(self, "asr_final", None), "prewarm", None)
        if prewarm is not None:
            prewarm()

    def _asr_event(self, name: str, msg: str, level: str = "info", **data) -> None:
        """二次识别的后端事件（asr.fallback 回退 / asr.backend 熔断恢复、游戏态释放加载）→ 统一事件日志。"""
        elog.emit("asr", name, msg, level=level, **data)

    def _submit_identify(self, samples: np.ndarray) -> tuple[Future, float] | None:
        """把这一段交给声纹线程认人；没有声纹库返回 None。返回 (Future, 提交时刻)。"""
        gate = self.gate
        if gate is None:
            return None
        if self._spk_pool is None:
            self._spk_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="speaker")
        return self._spk_pool.submit(_identify_job, gate, samples), time.monotonic()

    def _collect_speaker(self, job: tuple[Future, float] | None) -> tuple[str, float | None, int | None]:
        """等声纹结果：(说话人标签, 最高分, 声纹耗时 ms)。从提交起最多等 SPK_TIMEOUT_SEC，超时 / 出错当没有标签，不阻塞判定。"""
        if job is None:
            return "", None, None
        fut, t_submit = job
        try:
            name, score, spk_ms = fut.result(timeout=max(0.0, t_submit + SPK_TIMEOUT_SEC - time.monotonic()))
        except FutureTimeout:
            fut.cancel()
            log.warning("声纹超过 %.1fs 还没出结果，这一句不标说话人", SPK_TIMEOUT_SEC)
            elog.emit("speaker", "spk.fail", "声纹超时，不标说话人", level="warn", reason="timeout", limit_sec=SPK_TIMEOUT_SEC)
            return "", None, None
        except Exception as e:  # noqa: BLE001  声纹出错不能影响这一句的去留
            log.warning("声纹识别出错，这一句不标说话人：%s: %s", type(e).__name__, e)
            elog.emit("speaker", "spk.fail", "声纹出错，不标说话人", level="warn", reason=type(e).__name__, error=str(e)[:200])
            return "", None, None
        return speaker_label(name, score), score, spk_ms

    def _drop_segment(self, seg, why: str, note: str, dump_label: str, **data) -> None:
        """识别链路上直接丢掉一段（不判定、不进主线）：面板写原因、取证保留音频、记一条 seg.drop 事件。"""
        self.hub.stage(None, "asr", "done", note)
        self._dump_segment(seg, dump_label)
        log.info("丢弃 %s 段 %.2fs %s", why, seg.duration, {k: v for k, v in data.items() if v not in (None, "")})
        elog.emit("asr", "seg.drop", note, why=why, sec=round(seg.duration, 2), **data)

    def _enroll_take(self, seg) -> bool:
        """注册模式：同一条链路的段只拿来注册，不识别、不判定、不送主线，返回 True。
        没在注册（含刚超时）返回 False，这一段照常往下走。和播放重叠的段（开始时间早于最后出声 + 0.3 秒）不收。"""
        sess = self._active_enroll()
        if sess is None:
            return False
        overlap = seg.start < self._last_play_t + PLAY_OVERLAP_GUARD_SEC
        playing = self._playing()
        old_state = sess.state
        r = sess.feed(seg.samples, playing=playing or overlap)
        self.hub.stage(None, "speaker", "done" if r.get("ok") else "blocked",
                       "注册：收下" if r.get("ok") else f"注册：{r.get('reason')}")
        # 每一段都留证据：收下 / 为什么拒（有效语音秒数、零值占比、峰值、是否和播放重叠），声纹录不上时靠它定位
        self._enroll_seg_event(r, seg, playing, overlap)
        if sess.state != old_state:
            elog.emit("enroll", "enroll.state", f"{old_state} -> {sess.state}", old=old_state, new=sess.state, total=r.get("total"))
        return True

    def _enroll_seg_event(self, r: dict, seg, playing: bool, overlap: bool) -> None:
        """注册模式下一段的结果 → enroll.seg 事件（info 里的 sec / effective_sec / zero_ratio / peak 直接并入 data）。"""
        ok = bool(r.get("ok"))
        gap = None if self._last_play_t < 0 else round(getattr(seg, "start", 0.0) - self._last_play_t, 2)  # 段开始距最后一次出声多久（VAD 时间轴）
        elog.emit("enroll", "enroll.seg", "收下" if ok else str(r.get("reason")), level="info" if ok else "warn", ok=ok,
                  group=r.get("group"), total=r.get("total"), self_score=r.get("self_score"), playing=playing, overlap=overlap,
                  play_gap=gap, **(r.get("info") or {}))

    def _on_heard(self, text: str, score: float | None, *, source: str, stream_text: str = "",
                  seg_sec: float | None = None, asr_ms: int | None = None, spk_ms: int | None = None,
                  front_ms: int | None = None, speaker: str = "", dry_run: bool = False,
                  asr: FinalResult | None = None) -> dict:
        """一句识别结果的去留（麦克风和面板注入共用）：记一条面板记录，放行就交给大脑，只喊唤醒词就应答。
        去留只看碎音、唤醒词、免喊窗口；声纹不拦截，speaker（"主人" / "未知" / 库里的名字 / ""=没有）只做标签，
        随记录和事件落盘，放行时带进语音标记告诉模型是谁在说话。"""
        s = self.store.current
        in_window = self._in_followup()
        d = decide(text, matcher=self.wake, wake_required=s.wake_required,
                   min_chars=s.min_chars, in_window=in_window, allow_short_answer=self.state != "SPEAKING",
                   alt_text=stream_text or None)  # 二次识别漏掉唤醒词、流式识别抓到了：靠流式结果救回
        timings = {k: v for k, v in (("asr_ms", asr_ms), ("speaker_ms", spk_ms), ("front_ms", front_ms)) if v is not None}
        # 二次识别用了哪个后端、耗时、置信度（FireRedASR2 才有）：写进记录和事件，以后做"没把握时第二意见"用；暂不参与决策
        asr_fields = ({"asr_backend": asr.backend, "asr_conf": asr.conf, "asr_min_conf": asr.min_conf, "asr_fallback": asr.fallback or None}
                      if asr is not None else {})
        uid = self.hub.new_utterance(source, text=text, stream_text=stream_text,
                                     score=None if score is None else round(score, 3), speaker=speaker, in_window=in_window,
                                     timings=timings, **asr_fields)
        if seg_sec is not None:
            self.hub.stage(uid, "vad", "done", f"{seg_sec:.1f} 秒")
        asr_data = ({"backend": asr.backend, "ms": asr.ms, "server_ms": asr.server_ms, "conf": asr.conf, "min_conf": asr.min_conf,
                     "fallback": asr.fallback or None}
                    if asr is not None else {})
        self.hub.stage(uid, "asr", "blocked" if d.stage == "filler" else "done",
                       d.reason if d.stage == "filler" else text[:20], **asr_data)
        if d.stage == "filler":
            self.hub.stage(uid, "speaker", "skipped")
        elif score is None:
            self.hub.stage(uid, "speaker", "skipped", "未过声纹（注入/无声纹库/超时）")
        else:  # 声纹永不拦截：只展示是谁和分数，例如「主人 0.52」「未知 0.18」
            self.hub.stage(uid, "speaker", "done", f"{speaker} {score:.2f}" if speaker else f"{score:.2f}")
        if d.stage == "filler":
            self.hub.stage(uid, "wake", "skipped")
        else:
            self.hub.stage(uid, "wake", "blocked" if d.stage == "wake" else "done", d.reason)
        label = "注入" if source == "inject" else (speaker or ("未过声纹" if score is None else ""))
        shown = f"{label} {score:.2f}".strip() if score is not None else label
        if d.verdict == "accept":
            log.info("[%s] %s  → 送主线「%s」（%s）(stream: %s)", shown, text, d.text, d.reason, stream_text)
        elif d.verdict == "wake_only":
            log.info("唤醒：[%s] %s（%s），免喊 %.0f 秒", shown, text, d.reason, s.follow_up_sec)
        elif d.verdict == "ignored":
            log.info("忽略：%s text=%r", d.reason, text)
        else:
            log.info("拒绝：%s text=%r (stream: %r)", d.reason, text, stream_text)
        result = {"uid": uid, "verdict": d.verdict, "stage": d.stage, "reason": d.reason, "text": d.text,
                  "wake": d.wake, "in_window": in_window, "dry_run": dry_run, "speaker": speaker}
        # pending 存在时，确认词先走守卫 IPC；原话绝不进模型，参数也始终留在守卫。
        ipc = getattr(self, "approval_ipc", None)
        pending = ipc.latest() if ipc is not None else None
        decision = approval_decision(d.text or text) if pending else None
        if decision:
            ok = ipc.decide(pending["id"], decision)
            if not ok:
                self.hub.finish(uid, verdict="rejected", reason="审批 IPC 拒绝或已过期")
                return {**result, "verdict": "rejected", "reason": "审批 IPC 拒绝或已过期"}
            self._followup_until = time.monotonic() + APPROVAL_FOLLOWUP_SEC
            if decision == "approve":
                self.hub.update(uid, verdict="approval", reason="已批准，要求重试原调用", sent=APPROVAL_RETRY_MARKER)
                self._accept_utterance(APPROVAL_RETRY_MARKER, uid, speaker)
            else:
                self.hub.finish(uid, verdict="approval", reason="已拒绝")
            return {**result, "verdict": "approval", "reason": "已批准" if decision == "approve" else "已拒绝"}
        if dry_run:
            self.hub.finish(uid, verdict=d.verdict, reason=d.reason + "（模拟，未执行）", sent=d.text)
        elif d.verdict == "accept":
            monitor = getattr(self, "sleep_rehearsal", None)
            if monitor is not None:
                monitor.note_activity()
            self.hub.update(uid, verdict="accept", reason=d.reason, sent=d.text)
            self._accept_utterance(d.text, uid, speaker)
        elif d.verdict == "wake_only":
            self.hub.update(uid, verdict="wake_only", reason=d.reason)
            self._wake_ack(uid)
        else:
            self.hub.finish(uid, verdict=d.verdict, reason=d.reason)
        return result

    def inject(self, text: str, dry_run: bool = True) -> dict:
        """面板「模拟一句话」/上线验证用：当作识别出的文字走一遍去留判定（不过声纹）。dry_run 只判定不执行。"""
        return self._on_heard(text, None, source="inject", dry_run=dry_run)

    def _accept_utterance(self, text: str, uid: str | None = None, speaker: str = "") -> None:
        """一句话放行：正在说就停（只停播放）；立刻放提示音；文字排进大脑队列。"""
        if self.state == "SPEAKING":
            self.stop_speaking("新一轮")
        if self.store.current.earcon:
            self.play_q.put((self._gen, self._earcon))
        self.text_q.put(Heard(text, uid, speaker) if (uid or speaker) else text)

    def _wake_ack(self, uid: str | None) -> None:
        """只喊了唤醒词：应答一声（合成好的「在呢」，TTS 不在时用提示音），从应答结束起打开免喊窗口。"""
        if self.state == "SPEAKING":
            self.stop_speaking("唤醒")
        pcm = self._ack_pcm if self._ack_pcm is not None else self._earcon
        self.play_q.put((self._gen, pcm))
        self._followup_until = time.monotonic() + len(pcm) / self.sr + self.store.current.follow_up_sec
        self.hub.stage(uid, "play", "done", "应答「在呢」" if self._ack_pcm is not None else "提示音")
        self.hub.finish(uid)
        self.hub.status(self.status())

    def _prepare_ack(self) -> None:
        """后台合成应答语并缓存（TTS 上线时、改了应答语时调用）；失败就继续用提示音。"""
        text = self.store.current.ack_text
        if text == self._ack_text and self._ack_pcm is not None:
            return

        def work() -> None:
            try:
                pcm, sr = self.tts.synth(text)
            except Exception as e:  # noqa: BLE001
                log.warning("tts ack synth failed: %s", e)
                return
            if sr != self.sr:
                x_old = np.linspace(0, 1, len(pcm), endpoint=False)
                x_new = np.linspace(0, 1, int(len(pcm) * self.sr / sr), endpoint=False)
                pcm = np.interp(x_new, x_old, pcm).astype(np.float32)
            self._ack_pcm, self._ack_text = pcm, text
            log.info("tts ack ready: %s (%.2fs)", text, len(pcm) / self.sr)

        threading.Thread(target=work, name="ack-synth", daemon=True).start()

    def _in_followup(self) -> bool:
        """免喊窗口：它回完最后一句后 follow_up_sec 秒内，或者一轮对话正在进行时（/say 念的不算对话）。"""
        return time.monotonic() < self._followup_until or (self.state == "SPEAKING" and not self._turn_is_say)

    def _open_followup(self) -> None:
        self._followup_until = time.monotonic() + self.store.current.follow_up_sec
        self.hub.status(self.status())

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

    def _handle_item(self, item: str | Heard | SayItem, nudged: bool = False) -> None:
        """一轮：放行的话进主线会话 / /say 的话直接念；等本轮播完再回到监听，然后打开免喊窗口。
        只说没做（回复含承诺词、本轮零工具调用）时，本轮收尾后在同一会话追发一次 NUDGE_TEXT；
        编造拦截（回复声称被拦 / 要审批，本轮审计里却没有拦截记录）时追发一次 NUDGE_TEXT_FAKE_BLOCK；
        两种共用「每轮最多追一次」：nudged=True 是追发的那一轮，不再追。"""
        uid = item.uid if isinstance(item, (Heard, SayItem)) else None
        self._turn_is_say = isinstance(item, SayItem)
        self._turn_tools = 0
        self._turn_gate = frozenset()
        nudge = ""  # "" 不追 / "promise" 只说没做 / "fake-block" 编造拦截
        gen = self._begin_speaking()
        self._turn_first = True
        self._turn_t0 = time.monotonic()
        self._turn_uid, self._turn_gen = uid, gen
        self._turn_marks = marks = {"turn_start": time.time()}
        try:
            if isinstance(item, SayItem):
                res = self.runner.run_say(item.text, gen)
                log.info("say: %s", item.text)
            else:
                text = item.text if isinstance(item, Heard) else item
                self._turn_speaker = item.speaker if isinstance(item, Heard) else ""  # open_stream 加语音标记时读
                self.hub.stage(uid, "gateway", "active", "请求已发出")
                res = self.runner.run_reply(text, gen)
                log.info("reply: %s", res.reply)
            real_unspoken = [x for x in res.unspoken if not x.startswith(SHAPED_PREFIX)]  # 口语整形主动省略的不算「没念出来」
            if real_unspoken:
                log.warning("unspoken %d (aborted=%s): %s", len(real_unspoken), res.aborted, " / ".join(real_unspoken)[:300])
            if len(real_unspoken) != len(res.unspoken):
                log.info("speech shaped: %d chunk(s) left unread (full reply is on the panel)", len(res.unspoken) - len(real_unspoken))
            if res.error:
                self._set_error(res.error)
            elif res.spoken:
                self._set_error("")
            while gen == self._gen and self._playing() and not self._stop_evt.is_set():
                time.sleep(0.05)
            current = gen == self._gen
            # O5b：仅本轮 task_start 经 IPC 登记的候选，且首句真实入队并完整播完才领取。
            # 任何模型/TTS 错误或主人打断都会清空候选，任务保持 queued。
            scheduler = getattr(self, "task_dispatch", None)
            if scheduler is not None and not isinstance(item, SayItem):
                delivered = bool(res.spoken) and not res.error and not res.aborted and current
                scheduler.finish_turn(uid, reply_delivered=delivered, submit=self.start_task_worker)
            if current and not isinstance(item, SayItem):  # 主程序让念的话不是在跟主人对话，不开免喊窗口
                self._open_followup()
            if not nudged and current and not isinstance(item, SayItem):
                if self._should_nudge(res):
                    nudge = "promise"
                    self.hub.stage(uid, "gateway", "done", "只说没做，已追问一次")
                elif self._should_nudge_fake_block(res):
                    nudge = "fake-block"
                    self.hub.stage(uid, "gateway", "done", "称被拦但审计里没有拦截记录，已追问一次")
            if res.spoken and current:
                self.hub.stage(uid, "play", "done", f"念完 {len(res.spoken)} 句")
            elif res.aborted or not current:
                self.hub.stage(uid, "play", "blocked", "被打断")
            else:
                self.hub.stage(uid, "play", "skipped", "没有能念的句子")
            self.hub.finish(uid, reply=res.reply, spoken=res.spoken, unspoken=res.unspoken, error=res.error or "",
                            timings=self._turn_timings(uid, marks))
            run_id = getattr(self.brain, "last_run_id", None)
            if uid and run_id and not isinstance(item, SayItem):
                threading.Thread(target=self._fill_gateway_phases, args=(uid, run_id, dict(marks)), name="runtrace",
                                 daemon=True).start()
        finally:
            self._turn_uid, self._turn_marks, self._turn_is_say, self._turn_speaker = None, None, False, ""
            with self._lock:
                if gen == self._gen:
                    self.state = "LISTENING"
        if nudge:
            self._send_nudge(nudge, uid)

    def _nudge_allowed(self, res: TurnResult) -> bool:
        """追问的共同前提：没被打断没出错；主人已排了新话、静音、游戏中也不追。"""
        return not (res.aborted or res.error or self._muted.is_set() or self._paused.is_set() or not self.text_q.empty())

    def _nudge_block_reason(self, res: TurnResult) -> str:
        """_nudge_allowed 为假时的原因（只给 nudge.check 事件看，判定仍由 _nudge_allowed 决定）。"""
        if res.aborted:
            return "aborted"
        if res.error:
            return "error"
        if self._muted.is_set():
            return "muted"
        if self._paused.is_set():
            return "gaming"
        return "queued"

    def _nudge_check(self, kind: str, decision: bool, why: str, res: TurnResult) -> bool:
        """记一条 nudge.check 事件（只在回复含承诺词 / 声称被拦时才会走到这里，不是每轮都记），原样返回判定。"""
        elog.emit("nudge", "nudge.check", f"{kind} -> {'追问' if decision else '不追'}（{why}）", uid=self._turn_uid,
                  kind=kind, decision=decision, why=why, tools=self._turn_tools, gate=sorted(self._turn_gate) or None,
                  reply=res.reply[:80])
        return decision

    def _should_nudge(self, res: TurnResult) -> bool:
        """本轮是否「只说没做」：回复含承诺词、零工具调用、没被打断没出错；主人已排了新话、静音、游戏中也不追。"""
        if self.pending.current():
            return self._nudge_check("promise", False, "pending_approval", res)
        if not has_promise(res.reply):
            return False
        if not self._nudge_allowed(res):
            return self._nudge_check("promise", False, self._nudge_block_reason(res), res)
        time.sleep(AUDIT_LAG_SEC)  # 审计跟随有 0.5 秒轮询延迟，等一下再数工具调用
        ok = needs_nudge(res.reply, self._turn_tools, aborted=res.aborted, error=res.error)
        return self._nudge_check("promise", ok, "nudge" if ok else "has_tools", res)

    def _should_nudge_fake_block(self, res: TurnResult) -> bool:
        """本轮是否「编造拦截」：回复声称被拦 / 要审批 / 要放行 / 要确认，而本轮审计里没有拦截或确认记录、
        也没有一件还在等主人确认的事（上一轮真拦下、这一轮主人只是嗯了一声，模型再问一次是正常的）。"""
        if not claims_blocked(res.reply):
            return False
        if not self._nudge_allowed(res):
            return self._nudge_check("fake-block", False, self._nudge_block_reason(res), res)
        time.sleep(AUDIT_LAG_SEC)  # 拦截记录也是从审计文件跟随来的，同样可能比回复结束晚到
        ok = needs_fake_block_nudge(res.reply, bool(self._turn_gate) or bool(self.pending.current()),
                                    aborted=res.aborted, error=res.error)
        return self._nudge_check("fake-block", ok, "nudge" if ok else "gate_seen", res)

    def _send_nudge(self, kind: str = "promise", parent_uid: str | None = None) -> None:
        """在同一会话追发一条语音标记的话（只说没做 → 让模型把说过的事做完；编造拦截 → 告诉它没有任何东西拦着）；
        它的回复照常念出来，面板上单独一条记录。parent_uid 是被追问的那一轮，记进 nudge.sent 方便 logq 串起来。"""
        decision = self.should_announce("nudge", user_initiated=False)
        if not decision.speak:
            elog.emit("program", "program.quiet_suppressed", "影音中，未自动追问", kind="nudge", reason=decision.reason,
                      parent=parent_uid)
            if self.tray and not self._paused.is_set():
                self.tray.notify(f"{DISPLAY_NAME}：影音中，未自动追问")
            return
        if kind == "fake-block":
            text, reason = NUDGE_TEXT_FAKE_BLOCK, "称被拦但没有拦截记录，自动追问一次"
            log.info("nudge sent: fake-block")
        else:
            text, reason = NUDGE_TEXT, "只说没做，自动追问一次"
            log.info("nudge sent: 只说没做，追问一次")
        uid = self.hub.new_utterance("nudge", text=text, verdict="accept", reason=reason, sent=text)
        elog.emit("nudge", "nudge.sent", reason, uid=uid, kind=kind, parent=parent_uid, child=uid)
        self._handle_item(Heard(text, uid), nudged=True)

    def _turn_timings(self, uid: str | None, m: dict) -> dict:
        """耗时拆分（毫秒）：听完 → 排队 → 网关首字 → 首句成形 → 合成首句 → 真正出声。"""
        rec = self.hub.record(uid) if uid else None
        heard = rec["t0"] if rec else None

        def ms(a: float | None, b: float | None) -> int | None:
            return round((b - a) * 1000) if a is not None and b is not None else None

        t = {"queue_ms": ms(heard, m.get("turn_start")), "gateway_first_ms": ms(m.get("turn_start"), m.get("first_token")),
             "first_sentence_ms": ms(m.get("first_token"), m.get("first_sentence")), "tts_first_ms": m.get("tts_first_ms"),
             "play_lag_ms": ms(m.get("first_enqueue"), m.get("first_play")),
             "first_sound_ms": ms(heard if heard is not None else m.get("turn_start"), m.get("first_play"))}
        return {k: v for k, v in t.items() if v is not None}

    def _fill_gateway_phases(self, uid: str, run_id: str, marks: dict) -> None:
        """轮次结束后（后台线程，不碰首字）去网关会话库（只读）读这一轮的 trajectory：
        1) 把网关首字拆成"网关准备"和"模型首字"；2) 摘要成一条 gateway.run 事件（模型 / stop / token / 每次工具调用成败），
        发给模型的上下文另存 blob。trajectory 是 10MiB 滚动窗口，所以必须趁现在摘下来；写出要等 session.ended 前后，最多重试 5 次。"""
        s = None
        for i in range(5):
            try:
                s = run_summary(run_id)
            except Exception:  # noqa: BLE001  读网关库出任何问题都不能影响语音
                log.exception("run_summary failed")
                s = None
            if s:
                break
            if i < 4:
                time.sleep(1.0)
        if not s:
            elog.emit("gateway", "gateway.run_missing", "trajectory 5 次都没读到", level="warn", uid=uid, run_id=run_id)
            return
        ph = s["phases"]
        if "prompt_submitted" in ph:
            t = {"gateway_prep_ms": round((ph["prompt_submitted"] - marks["turn_start"]) * 1000)}
            if "first_token" in marks:
                t["model_first_ms"] = round((marks["first_token"] - ph["prompt_submitted"]) * 1000)
            self.hub.update(uid, run_id=run_id, timings=t)
        blob = elog.blob(uid, "gwctx", s.pop("ctx", None)) if s.get("ctx") else None
        tools = s.get("tools") or []
        bad = [x for x in tools if x.get("ok") is False]
        warn = bool(bad) or s.get("aborted") or s.get("timed_out") or s.get("prompt_error") \
            or (s.get("final_status") not in (None, "success"))
        short_model = str(s.get("model") or "").split("/")[-1]
        elog.emit("gateway", "gateway.run", f"{short_model} stop={s.get('stop')} 工具 {len(tools)} 次" + (f"（失败 {len(bad)}）" if bad else ""),
                  level="warn" if warn else "info", uid=uid, blob=blob,
                  **{k: s.get(k) for k in ("run_id", "provider", "model", "channel", "stop", "final_status", "usage", "tools",
                                           "aborted", "timed_out", "prompt_error", "texts", "phases")})

    def _new_shaper(self) -> SpeechShaper | None:
        """每轮语音回复调一次（TurnRunner.run_reply）：按当前设置建口语整形器；speech_max_sentences=0 = 这轮不整形。"""
        n = self.store.current.speech_max_sentences
        return SpeechShaper(n) if n > 0 else None

    def _on_turn_event(self, name: str, data: dict) -> None:
        """TurnRunner 的进度 → 面板环节状态与耗时记点。"""
        uid, m = self._turn_uid, self._turn_marks
        if m is None:
            return
        t = data.get("t", time.time())
        if name == "first_token":
            m["first_token"] = t
            self.hub.stage(uid, "gateway", "active", f"首字 {round((t - m['turn_start']) * 1000)}ms")
            # uid ↔ 网关 run 的关联点：流里第一个 chunk 的 id 就是 trajectory 的 run_id
            elog.emit("brain", "brain.run", uid=uid, run_id=getattr(self.brain, "last_run_id", None),
                      first_ms=round((t - m["turn_start"]) * 1000))
        elif name == "shaped":  # 口语整形动过这一轮：原句数 / 字数、念出句数、省略块数
            elog.emit("voice", "speech.shaped",
                      f"整形：原 {data.get('sentences')} 句 → 念 {data.get('spoken_sentences')} 句，省略 {data.get('omitted')} 块，保留提问 {data.get('kept_questions')} 块",
                      uid=uid, **{k: v for k, v in data.items() if k != "t"})
        elif name == "sentence":
            m.setdefault("first_sentence", t)
        elif name == "tts_start":
            self.hub.stage(uid, "tts", "active", data.get("text", "")[:16])
        elif name == "tts_done":
            m.setdefault("tts_first_ms", data.get("ms"))
            self.hub.stage(uid, "tts", "done", f"{data.get('text', '')[:12]} {data.get('ms')}ms")
        elif name == "tts_fail":
            self.hub.stage(uid, "tts", "blocked", data.get("error", ""))
        elif name == "enqueued" and data.get("n") == 1:
            m.setdefault("first_enqueue", t)
            self.hub.stage(uid, "play", "active", "开始播放")
        elif name == "stream_end":
            self.hub.stage(uid, "gateway", "done" if data.get("ok") else "blocked",
                           "回复完毕" if data.get("ok") else data.get("error", ""))
            elog.emit("brain", "brain.stream_end", "回复完毕" if data.get("ok") else "断流", uid=uid,
                      level="info" if data.get("ok") else "error", ok=bool(data.get("ok")), error=data.get("error"),
                      ms=round((t - m["turn_start"]) * 1000), run_id=getattr(self.brain, "last_run_id", None),
                      **dict(getattr(self.brain, "last_meta", None) or {}))

    def _on_tool(self, entry: dict) -> None:
        """审计跟随：主线会话的工具调用记到当前这一轮上（没有进行中的语音轮就只打日志）。"""
        log.info("tool %s %s %s: %s", entry["tool"], entry["level"], entry["decision"], entry["summary"])
        if entry["tool"] == "launch_app" and self.guard is not None:  # 拉起游戏会动音频：过一会儿记一条输入流自检（只记不动作）
            self.guard.request_selfcheck("launch_app", LAUNCH_SELFCHECK_SEC)
        if self._turn_marks is not None:  # 有进行中的一轮：先计数（不依赖面板记录 id）
            self._turn_tools += 1
        uid = self._turn_uid
        # 注意：emit 的 level 是日志级别，审计的分级（L1/L2…）放进 data.tool_level，避免撞名
        elog.emit("tool", "tool.audit", f"{entry['tool']} {entry['level']} {entry['decision']}", uid=uid,
                  level="warn" if entry["state"] == "blocked" else "info", tool=entry["tool"],
                  tool_level=entry["level"], decision=entry["decision"], summary=entry["summary"], audit_ts=entry.get("ts"))
        if uid:
            self.hub.add_tool(uid, entry)
            note = f"{entry['tool']} {entry['level']} {entry['decision']}"
            if entry["decision"] == "approval-requested":
                note = f"{entry['tool']} {entry['level']} 等审批"
            self.hub.stage(uid, "tools", entry["state"], note)

    def _on_gate(self, ev: dict) -> None:
        """审计跟随：拦截 / 确认类记录。记到本轮（「编造拦截」兜底的依据），并维护「待确认」状态：
        新出现一件待确认 → 托盘变紫 + 弹气泡「思绪在等你确认：…」（游戏中只变色不弹）；了结（确认 / 否认 / 超时）→ 恢复。"""
        if self._turn_marks is not None:
            self._turn_gate = self._turn_gate | {ev["decision"]}
        change = self.pending.feed(ev)
        elog.emit("tool", "tool.gate", f"{ev['decision']} {ev['summary'][:60]}", uid=self._turn_uid, decision=ev["decision"],
                  summary=ev["summary"], pending=change or None)
        if change == "opened":
            summary = self.pending.current()
            gaming = self._paused.is_set()
            decision = self.should_announce("pending", user_initiated=False)
            log.info("pending confirm: %s%s", summary, "（游戏中，只变色不弹气泡）" if gaming else "")
            elog.emit("tool", "pending.open", summary, level="warn", uid=self._turn_uid, decision=ev["decision"],
                      gaming=gaming or None)
            if not decision.speak:
                elog.emit("program", "program.quiet_suppressed", "影音中，待确认只托盘提示", kind="pending",
                          reason=decision.reason, summary=summary)
            if self.tray and not gaming:
                self.tray.notify(f"{DISPLAY_NAME}在等你确认：{summary}")
        elif change == "closed":
            log.info("pending confirm cleared (%s)", ev["decision"])
            elog.emit("tool", "pending.close", ev["decision"], uid=self._turn_uid, decision=ev["decision"])
        if change:
            self._refresh_tray()
            self.hub.status(self.status())

    def should_announce(self, event_kind: str, user_initiated: bool):
        """Q2 唯一播报门；任务完成/失败待 O3 真实接线后才会调用，不能伪造任务行为。"""
        policy = getattr(self, "announcements", None)
        if policy is None:
            return AnnouncementPolicy(lambda: False).should_announce(event_kind, user_initiated)
        return policy.should_announce(event_kind, user_initiated)

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
        if reason == "barge-in":
            self.hub.stage(self._turn_uid, "barge", "done", "被主人打断")
            if not self._turn_is_say:
                self._open_followup()

    # ---------- 控制口 / 面板 / 托盘 ----------
    def say(self, text: str) -> tuple[bool, str]:
        """控制口 /say：静音或游戏中拒绝，排队超过上限回 busy，否则排进大脑队列（与语音轮按顺序念）。"""
        if self._muted.is_set():
            return False, "muted"
        if self._paused.is_set():
            return False, "gaming"
        if self._active_enroll() is not None:  # 声纹注册中：不让电脑出声，免得盖住主人的话
            return False, "enrolling"
        if sum(isinstance(x, SayItem) for x in list(self.text_q.queue)) >= SAY_MAX_PENDING:
            return False, "busy"
        uid = self.hub.new_utterance("say", text=text, verdict="say", reason="主程序让电脑念")
        self.text_q.put(SayItem(text, uid))
        log.info("say queued: %s", text)
        return True, "queued"

    # ---------- 声纹注册（面板「声纹」卡片） ----------
    def _active_enroll(self) -> "EnrollSession | None":
        """当前的注册会话；超时（10 分钟没人收尾）就清掉并记日志。say、面板接口、前端都经过这里，不靠新语音段来触发超时。"""
        sess = self.enroll
        if sess is not None and sess.expired():
            if self.enroll is sess:
                self.enroll = None
                log.info("enroll timeout, cancelled")
                elog.emit("enroll", "enroll.abort", "10 分钟没人收尾，注册超时取消", level="warn", reason="timeout", total=len(sess.samples),
                          state=sess.state, last=sess.last)
            return None
        return sess

    def _ensure_gate(self) -> SpeakerGate:
        """注册用的声纹门：平时那把；库为空/读不出时守护进程没在用门，就用留着的备用那把（没有再建），保存后接上。"""
        if self.gate is not None:
            return self.gate
        if self._spare_gate is None:
            self._spare_gate = SpeakerGate(threshold=self.store.current.spk_threshold)
        return self._spare_gate

    def _param_consts(self) -> dict[str, tuple]:
        """面板参数表里的只读常量：{key: (当前值, 默认值)}。代码常量、环境变量、命令行参数，改了要重启语音。"""
        vad = inspect.signature(VAD.__init__).parameters  # 守护进程直接用 VAD()，所以当前值就是默认值
        same = lambda v: (v, v)  # noqa: E731
        return {"MIC_GAIN": (self.mic_gain, MIC_GAIN), "AEC_ENABLED": (self.aec_on, AEC_ENABLED),
                "VAD_THRESHOLD": same(vad["threshold"].default), "VAD_MIN_SILENCE": same(vad["min_silence"].default),
                "VAD_MIN_SPEECH": same(vad["min_speech"].default), "MIN_UTT_SEC": same(MIN_UTT_SEC),
                "FIRST_SENT_MAX_CHARS": same(FIRST_SENT_MAX_CHARS), "FIRST_SENT_SOFT_MIN": same(FIRST_SENT_SOFT_MIN),
                "BARGE_MS": same(BARGE_MS), "BARGE_GUARD_MS": same(BARGE_GUARD_MS)}

    def _speaker_info(self, query: dict) -> tuple[int, dict]:
        g = self.gate or self._spare_gate
        sess = self._active_enroll()
        return 200, {"ok": True, "model": SPK_MODEL.stem, "store": STORE.name, "people": g.counts() if g else {},
                     "legacy": bool(g and g.legacy), "backup": backup_path(STORE).exists(),
                     "load_error": g.load_error if g else None,
                     "threshold": self.store.current.spk_threshold, "enabled": self.speaker_gate,
                     "session": sess.snapshot() if sess else None}

    def _speaker_action(self, body: dict) -> tuple[int, dict]:
        """面板「声纹」卡片：start / discard_last / enough / cancel / commit / rollback。整个动作在锁内，控制口多线程也不会错乱。"""
        with self._enroll_lock:
            return self._speaker_action_locked(body)

    def _speaker_action_locked(self, body: dict) -> tuple[int, dict]:
        action = str(body.get("action") or "")
        sess = self._active_enroll()
        if action == "start":
            if not self.speaker_gate:
                return 400, {"ok": False, "reason": "声纹已关闭（--no-gate），不能注册"}
            if sess is not None:
                return 409, {"ok": False, "reason": "已经在注册了"}
            if self.state == "SPEAKING" or not self.text_q.empty():
                return 409, {"ok": False, "reason": "正在回话，等它说完再开始"}
            name = str(body.get("name") or "我").strip()[:8] or "我"
            gate = self._ensure_gate()
            old_refs = gate.refs(name)
            self.enroll = EnrollSession(name, gate.embed, old_refs=old_refs)
            log.info("enroll start: %s", name)
            elog.emit("enroll", "enroll.start", name, name=name, old_refs=0 if old_refs is None else len(old_refs),
                      min_effective_sec=MIN_EFFECTIVE_SEC, max_zero_ratio=MAX_ZERO_RATIO, min_peak=MIN_PEAK, min_self_sim=MIN_SELF_SIM,
                      min_total=MIN_TOTAL)
            return 200, {"ok": True, "session": self.enroll.snapshot()}
        if action == "rollback":
            if sess is not None:
                return 409, {"ok": False, "reason": "先结束当前注册"}
            gate = self._ensure_gate()
            try:
                if not gate.rollback():
                    return 404, {"ok": False, "reason": "没有注册前的备份"}
            except OSError as e:
                return 500, {"ok": False, "reason": f"回滚失败：{e}，库没有变化"}
            if self.gate is None and gate.names():
                gate.threshold = self.store.current.spk_threshold
                self.gate = gate
            log.warning("speaker store rolled back to %s", backup_path(STORE).name)
            elog.emit("enroll", "enroll.rollback", "声纹库回滚到注册前", level="warn", backup=backup_path(STORE).name)
            return 200, {"ok": True, "people": gate.counts(), "legacy": gate.legacy}
        if sess is None:
            return 409, {"ok": False, "reason": "没有在注册"}
        if action == "cancel":
            self.enroll = None
            log.info("enroll cancelled")
            elog.emit("enroll", "enroll.abort", "主人取消注册", level="warn", reason="cancelled", total=len(sess.samples), state=sess.state,
                      last=sess.last)
            return 200, {"ok": True}
        if action == "discard_last":
            sess.discard_last()
            elog.emit("enroll", "enroll.discard", "撤销上一段", total=len(sess.samples))
        elif action == "enough":
            if not sess.to_testing():
                return 409, {"ok": False, "reason": "至少要 8 段才能自测"}
            elog.emit("enroll", "enroll.state", "recording -> testing（主人点了够了）", old="recording", new="testing", total=len(sess.samples))
        elif action == "commit":
            if sess.state != "ready":
                return 409, {"ok": False, "reason": "先说一句自测"}
            gate = self._ensure_gate()
            try:
                backup = gate.save_person(sess.name, sess.embs())  # 锁内：备份 + 写盘 + 换库；提取向量只在前端线程做
            except ValueError as e:  # 现有库读不出来：不覆盖它
                return 409, {"ok": False, "reason": str(e)}
            except OSError as e:  # 写盘失败（权限、被占用、磁盘满）：库和内存都没变，会话保留可重试
                log.error("enroll save failed: %s", e)
                elog.emit("enroll", "enroll.save_failed", "写声纹库失败", level="error", error=str(e), n=len(sess.samples))
                return 500, {"ok": False, "reason": f"写声纹库失败：{e}。库没有变化，可以再点一次保存"}
            if self.gate is None:
                gate.threshold = self.store.current.spk_threshold
                self.gate = gate
            self.enroll = None
            log.info("enroll saved: %s n=%d test=%.3f old=%s backup=%s", sess.name, len(sess.samples), sess.test_score,
                     sess.test_old_score, backup.name if backup else "-")
            elog.emit("enroll", "enroll.save", sess.name, name=sess.name, n=len(sess.samples), test_score=sess.test_score,
                      test_old_score=sess.test_old_score, backup=backup.name if backup else None)
            return 200, {"ok": True, "people": gate.counts(), "test_score": sess.test_score,
                         "test_old_score": sess.test_old_score}
        else:
            return 400, {"ok": False, "reason": f"不认识的操作：{action}"}
        return 200, {"ok": True, "session": sess.snapshot()}

    def status(self) -> dict:
        """控制口 /status 与面板状态栏。"""
        v = self.view()
        monitor = getattr(self, "program_quiet", None)
        program = monitor.snapshot() if monitor is not None else self._program_status()
        return {"view": v.key, "label": v.label, "state": self.state, "muted": self._muted.is_set(),
                "gaming": self._paused.is_set(), "runtime": self.runtime.state, "gateway": self.health.gateway,
                "tts": self.health.tts, "error": self.last_error, "sleep": self.sleep_state, "pending": self.text_q.qsize(),
                "followup_left": max(0.0, round(self._followup_until - time.monotonic(), 1)),
                "wake_words": list(self.store.current.wake_words), "panel": PANEL_URL,
                "audio": self.guard.info() if self.guard else None,
                "program": program,
                "tasks": self._panel_tasks() if self.tasks is not None else None,
                "sleep_rehearsal": getattr(self, "sleep_rehearsal", None).snapshot() if getattr(self, "sleep_rehearsal", None) else None}

    def _panel_tasks(self) -> dict:
        """面板专用任务 DTO：不把目标、worker session 或任何工具参数送进浏览器。"""
        raw = self.tasks.task_status()

        def public(task: dict) -> dict:
            item = {key: task[key] for key in ("summary", "status", "progress", "progress_percent", "result", "error",
                                                "created_at", "updated_at") if key in task}
            item["id"] = task["id"][-8:]  # 只作人工核对，不可用于任何控制接口
            if task.get("approval"):
                item["approval"] = {key: task["approval"][key] for key in ("description", "expires_at")
                                    if key in task["approval"]}
            return item

        return {key: raw[key] for key in ("counts", "max_concurrent", "running_slots")
                if key in raw} | {key: [public(task) for task in raw.get(key, [])]
                                  for key in ("active_tasks", "interrupted_tasks", "recent_tasks")}

    # ---------- O3 任务接口（仅供后续受控 OpenClaw 工具接线；本批不派发模型或进程） ----------
    def task_start(self, summary: str, goal: str) -> dict:
        """创建 queued 任务，立即返回；主线不等待任何任务执行。"""
        return self._task_service().task_start(summary, goal)

    def task_status(self, task_id: str | None = None) -> dict:
        """查看任务摘要或单项状态。"""
        return self._task_service().task_status(task_id)

    def task_cancel(self, task_id: str, reason: str = "") -> dict:
        """记录取消请求；本批不终止任何进程。"""
        executor = getattr(self, "task_executor", None)
        return executor.request_cancel(task_id, reason) if executor is not None else self._task_service().task_cancel(task_id, reason)

    def task_report(self, task_id: str, progress: str, percent: float | None = None) -> dict:
        """未来 worker 经受控执行器领取后才能上报进度。"""
        return self._task_service().task_report(task_id, progress, percent)

    def start_task_worker(self, task_id: str):
        """O5 唯一执行入口；未暴露给主线工具，后续调度器必须显式调用。"""
        executor = getattr(self, "task_executor", None)
        if executor is None:
            executor = TaskWorkerExecutor(self._task_service(), policy=self.announcements, on_notice=self._on_task_notice)
            self.task_executor = executor
        return executor.submit(task_id)

    def _on_task_created(self, task: dict) -> None:
        """Task IPC 的 /start 成功回调：只接受正在处理的主线语音轮。"""
        if self._turn_is_say:
            return
        self.task_dispatch.record_start(self._turn_uid, task["id"])

    def _on_task_approval_decision(self, task_id: str, decision: str) -> None:
        """审批 IPC 已按 pending id 确认后才到这里；UI 和语音不接触 worker session。"""
        if decision == "approve":
            self.task_executor.resume_after_approval(task_id)
        else:
            self.task_executor.request_cancel(task_id, "用户拒绝审批")

    def _emit_task_dispatch(self, event: str, task_id: str, reason: str) -> None:
        """调度证据只记摘要和 id；不记录任务目标、会话正文或工具参数。"""
        level = "info" if event == "task.dispatched" else "warn"
        elog.emit("task", event, reason, level=level, task_id=task_id, uid=self._turn_uid)

    def _task_service(self) -> TaskService:
        service = getattr(self, "tasks", None)
        if service is not None:
            return service
        manager = TaskManager.restore(TASK_JOURNAL_PATH)
        self.task_manager, self.tasks = manager, TaskService(manager)
        return self.tasks

    def _on_task_notice(self, notice: TaskNotice) -> None:
        """O5 worker 完成/失败通知统一经过 Q2 唯一播报门。"""
        task = notice.task
        failed = notice.kind == "task_failed"
        text = f"任务{task['summary']}{'失败' if failed else '完成'}"
        elog.emit("task", "task.notice", text, level="warn" if failed else "info", task_id=task["id"], kind=notice.kind,
                  spoken=notice.speak, status=task["status"])
        if notice.speak:
            self.say(text)
        elif self.tray:
            self.tray.notify(f"{DISPLAY_NAME}：{text}")

    def _on_program_quiet_change(self, old: str, new: str, program: dict) -> None:
        """Q1 仅留状态变化证据；Q2 才能把 quiet_active 接到播报门。"""
        event = "program.quiet_enter" if new == "quiet_active" else "program.quiet_exit"
        elog.emit("program", event, f"{old} -> {new}", old=old, new=new, rule=program.get("media_rule"),
                  exe=program.get("exe_name") or None, state_for_sec=program.get("state_for_sec"))

    def _program_status(self) -> dict:
        """前台程序/影音候选仅供状态与诊断；失败 fail-open，不参与任何语音决策。"""
        app = foreground_app()
        item = snapshot(app, classify_media(app, self.store.current.program_quiet))
        previous = getattr(self, "_last_program_snapshot", None)
        if item != previous:
            self._last_program_snapshot = item
            level = "warn" if item["error"] else "info"
            elog.emit("program", "program.foreground", item["exe_name"] or "未知前台程序", level=level,
                      pid=item["pid"], exe=item["exe_name"] or None, title=item["title"] or None,
                      fullscreen=item["fullscreen"], visible=item["visible"], error=item["error"] or None,
                      media_candidate=item["media_candidate"], media_rule=item["media_rule"])
        return item

    def set_sleep_state(self, state: str) -> bool:
        """控制口 /sleep-state：P4 休眠状态位，只做显示。"""
        self.sleep_state = state
        self._refresh_tray()
        self.hub.status(self.status())
        return True

    def view(self) -> TrayView:
        return tray_view(muted=self._muted.is_set(), gaming=self._paused.is_set(), busy=self.state == "SPEAKING",
                         health=self.health, last_error=self.last_error, sleep_state=self.sleep_state,
                         pending=self.pending.current())

    def toggle_mute(self) -> None:
        """托盘左键 / 面板按钮：静音（不听不说，立即停播）/ 取消静音。"""
        if self._muted.is_set():
            self._muted.clear()
            log.info("unmuted")
        else:
            self._muted.set()
            self.stop_speaking("静音")
            log.info("muted")
        self._refresh_tray()
        self.hub.status(self.status())

    def request_exit(self, code: int, reason: str = "") -> None:
        """托盘/面板：退出语音（0，看门狗不再拉起）/ 重启语音（3，看门狗立即拉起）。reason 记进 voice.exit 事件（tray / panel）。"""
        log.warning("exit requested: code=%d reason=%s", code, reason or "-")  # 任何退出路径都留一行 WARNING（含原因）
        elog.emit("voice", "voice.exit", f"请求退出 code={code}", level="info" if code in (EXIT_QUIT, EXIT_RESTART) else "warn",
                  code=code, reason=reason or None)
        self._exit_code = code
        self._stop_evt.set()

    def _panel_control(self, action: str) -> tuple[bool, str]:
        """面板按钮。重启/退出延迟半秒执行，先让 HTTP 回应发出去。"""
        if action in ("mute", "unmute"):
            if self._muted.is_set() != (action == "mute"):
                self.toggle_mute()
            return True, "已静音" if action == "mute" else "已取消静音"
        code = EXIT_RESTART if action == "restart" else EXIT_QUIT
        threading.Timer(0.5, self.request_exit, args=(code, "panel")).start()
        return True, "正在重启语音" if action == "restart" else "语音已退出，看门狗不会再拉起（双击桌面「思绪面板」可重新打开）"

    def open_panel(self) -> None:
        """托盘「打开面板」：用默认浏览器打开。"""
        webbrowser.open(PANEL_URL)

    def _apply_settings(self, s: Settings) -> None:
        """设置变了（面板或手工改文件）：唤醒词、声纹门槛、打断线、应答语立即生效。"""
        self.wake = WakeMatcher(s.wake_words)
        if self.gate:
            self.gate.threshold = s.spk_threshold
        self.barge.cfg = replace(self.barge.cfg, rms_min=s.barge_rms_min)
        if s.ack_text != self._ack_text and self.health.tts:
            self._prepare_ack()
        log.info("settings applied: %s", to_dict(s))
        self.hub.status(self.status())

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
        # 只记变化（轮询成功不记）：TTS 没起来、网关掉线都在这里留下时间点；第一次是启动时的初始状态
        now = {"gateway": bool(h.gateway), "tts": bool(h.tts)}
        prev, self._last_health = self._last_health, now
        if prev is None:
            elog.emit("health", "dep.change", f"启动时 gateway={'up' if now['gateway'] else 'DOWN'} tts={'up' if now['tts'] else 'DOWN'}",
                      level="info" if all(now.values()) else "warn", initial=True, **now)
        else:
            for dep in ("gateway", "tts"):
                if prev[dep] != now[dep]:
                    elog.emit("health", "dep.change", f"{dep} {'恢复' if now[dep] else '掉线'}", level="info" if now[dep] else "warn",
                              dep=dep, up=now[dep], **now)
        if h.tts:
            self._prepare_ack()
        self._refresh_tray()
        self.hub.status(self.status())

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
        if self.watchdog is not None:
            self.watchdog.disarm()  # 停流期间没有回调是正常的
        s.stop()
        for q in (self.in_q, self.text_q):
            while not q.empty():
                q.get_nowait()
        self.asr = None
        final = getattr(self, "asr_final", None)
        if final is not None:  # 精识别保留（SenseVoice 回退很小）；GPU 上的 FireRedASR2 显存紧张时由它自己让服务卸载
            final.enter_gaming()
        threading.Thread(target=self.tts.control, args=("unload",), name="tts-unload", daemon=True).start()
        log.info("gaming: 音频流已停，流式 ASR 已释放；精识别按显存余量决定是否卸载 GPU 模型")

    def _resume_audio(self, s: sd.Stream) -> None:
        """离开游戏态：重建 ASR/VAD 与前端状态，重开音频流，后台让 TTS 预加载。主循环调用。"""
        self.asr = StreamingASR()
        final = getattr(self, "asr_final", None)
        if final is not None:  # 已卸载的话后台让服务重新加载，加载完成前走 SenseVoice
            final.leave_gaming()
        self._reset_frontend()
        s.start()
        if self.watchdog is not None:
            self.watchdog.arm()  # 从重新启动那刻起重新计时
        threading.Thread(target=self.tts.control, args=("load",), name="tts-load", daemon=True).start()
        log.info("gaming 结束: 音频流已恢复")

    def _audio_devices(self) -> dict:
        """当前输入 / 输出设备名，随 audio.* 事件一起记（知道故障时用的是哪个设备）；查不到不影响启动。"""
        try:
            din = sd.query_devices(self.in_dev if self.in_dev is not None else sd.default.device[0])
            dout = sd.query_devices(self.out_dev if self.out_dev is not None else sd.default.device[1])
            return {"in_name": din["name"], "out_name": dout["name"], "rate": self.sr}
        except Exception:  # noqa: BLE001
            return {}

    # ---------- 主循环 ----------
    def run(self) -> None:
        logging.getLogger().addHandler(HubLogHandler(self.hub))
        s = self.store.current
        elog.emit("voice", "voice.start", f"pid {os.getpid()}", pid=os.getpid(), argv=sys.argv[1:], in_dev=self.in_dev,
                  out_dev=self.out_dev, rate=self.sr, gate_mode="identify-only", gate=bool(self.gate), aec=self.aec_on,
                  mic_gain=self.mic_gain, control_port=self.control_port,
                  settings={k: getattr(s, k) for k in ("spk_threshold", "wake_required", "min_chars", "follow_up_sec", "earcon")})
        log.info("device rate %d, in=%s out=%s, speakers=%s(%s) legacy=%s n=%s, gate=%s@%.2f, wake=%s(required=%s) followup=%.0fs, "
                 "mic_gain=%.1f, aec=%s, barge=%dms guard=%dms rms_min=%.4f ratio=%.1f, tts_timeout=%.0fs, session=%s, "
                 "control=127.0.0.1:%d", self.sr, self.in_dev, self.out_dev, self.gate.names() if self.gate else "off",
                 self.gate.model.stem if self.gate else "-", self.gate.legacy if self.gate else "-",
                 self.gate.counts() if self.gate else "-", "identify-only", s.spk_threshold, "/".join(s.wake_words),
                 s.wake_required, s.follow_up_sec, self.mic_gain, "on" if self.aec else "off", BARGE_MS, BARGE_GUARD_MS,
                 s.barge_rms_min, BARGE_FLOOR_RATIO, TTS_TIMEOUT_SEC, MAIN_SESSION_KEY, self.control_port)
        try:
            self.control = start_control_server(self, self.control_port, panel=self.panel)
        except OSError as e:
            log.error("控制口 127.0.0.1:%d 绑不上（是不是已经有一个语音进程在跑？）：%s；voice exit code=%d reason=control_port_busy",
                      self.control_port, e, EXIT_CRASH)
            elog.emit("voice", "voice.exit", "控制口绑不上", level="error", code=EXIT_CRASH, reason="control_port_busy",
                      port=self.control_port, error=str(e))
            sys.exit(EXIT_CRASH)
        try:
            self.task_ipc = TaskIpc(self._task_service(), on_started=self._on_task_created,
                                    on_cancelled=self.task_cancel, on_approval_decision=self._on_task_approval_decision)
            self.task_ipc.start()
        except OSError as e:
            self.task_ipc = None
            log.warning("任务 IPC 启动失败；task_* 工具将 fail-closed：%s", e)
            elog.emit("task", "task.ipc_unavailable", "任务 IPC 启动失败", level="warn", error=str(e))
        threads = [threading.Thread(target=self._frontend, name="frontend", daemon=True),
                   threading.Thread(target=self._brainloop, name="brain", daemon=True)]
        for t in threads:
            t.start()
        self.monitor.start()
        self.sleep_rehearsal.start()
        self.audit.start()
        if self.tray_on:
            from guanjia.tray import Tray  # 延迟导入：--no-tray 时不需要 pystray/Pillow

            self.tray = Tray(view=self.view, muted=self._muted.is_set, on_toggle_mute=self.toggle_mute,
                             on_restart=lambda: self.request_exit(EXIT_RESTART, "tray"),
                             on_exit=lambda: self.request_exit(EXIT_QUIT, "tray"), on_open_panel=self.open_panel)
            self.tray.start()
        failed = False
        shown = ""
        last_stats, stats = time.monotonic(), (0, 0)
        last_settings_poll = time.monotonic()
        self.watchdog = InputWatchdog()
        self.guard = AudioGuard(self.watchdog, emit=elog.emit, describe=lambda: self._audio_names)
        with sd.Stream(samplerate=self.sr, blocksize=self.frame, dtype="float32", channels=1, latency="low",
                       device=(self.in_dev, self.out_dev), callback=self._callback) as s:
            self._audio_names = self._audio_devices()  # 启动时查一次设备名缓存：停摆时不再去问 PortAudio（它可能正是卡住的那个）
            self.watchdog.arm()
            log.info("listening ... (latency in/out %.3f/%.3f s; output stream always-open, comfort noise %.0f dBFS, "
                     "lead %dms, panel %s, dump %s)", *s.latency, COMFORT_NOISE_DBFS, LEAD_SILENCE_MS, PANEL_URL,
                     SEG_DUMP_DIR)
            self.hub.stage(None, "mic", "active", "在听")
            elog.emit("voice", "voice.ready", f"监听中，启动 {round((time.monotonic() - _PROC_T0) * 1000)}ms",
                      boot_ms=round((time.monotonic() - _PROC_T0) * 1000), latency_in=round(s.latency[0], 3),
                      latency_out=round(s.latency[1], 3))
            self.runtime.start()  # 开机补写离线区间 + 5 分钟静默；之后每 2s 检测游戏态
            self.program_quiet.start()  # Q1：只读前台候选状态，不抑制任何语音
            try:
                while not self._stop_evt.is_set():
                    time.sleep(0.5)
                    paused = self._paused.is_set()  # 本轮只读一次，防监控线程中途翻转导致误判"流死了"
                    if paused and s.active:
                        self._suspend_audio(s)
                    elif not paused and not s.active:
                        self._resume_audio(s)
                    dead = [t.name for t in threads if not t.is_alive()]
                    # 托盘/面板要求退出时工作线程会跟着结束，这不是崩溃：误判成崩溃会回退出码 1，看门狗又把它拉起来
                    if not self._stop_evt.is_set() and (dead or (not s.active and not paused)):
                        log.error("worker died: threads=%s stream_active=%s，退出等待看门狗拉起；voice exit code=%d reason=worker_died",
                                  dead, s.active, EXIT_CRASH)
                        elog.emit("voice", "voice.exit", "工作线程/音频流死了", level="error", code=EXIT_CRASH,
                                  reason="worker_died", threads=dead, stream_active=s.active)
                        failed = True
                        break
                    # 输入流看门狗：确认回调停摆（>5s 没回调且复核仍没动）→ 记证据，以重启码退出让外层看门狗拉起整个进程；
                    # 不在进程内重开流，也不碰 sounddevice 私有 API。出声时输入全零只记事件不动作。
                    if not self._stop_evt.is_set() and self.guard.tick(paused) == "stall":
                        log.warning("voice exit code=%d reason=audio_callback_stall: %s", EXIT_RESTART, self.guard.last_stall)
                        elog.emit("voice", "voice.exit", "输入流回调停摆，请求重启进程", level="error", code=EXIT_RESTART,
                                  reason="audio_callback_stall", **(self.guard.last_stall or {}))
                        self._exit_code = EXIT_RESTART
                        break
                    key = self.view().key
                    if key != shown:  # 说话中 ↔ 在听 等状态变化同步到托盘和面板
                        shown = key
                        self._refresh_tray()
                        self.hub.status(self.status())
                        # 待确认压过静音 / 游戏中的颜色，但麦克风是否在听要看真实状态，不能看图标 key
                        mic_off = self._muted.is_set() or self._paused.is_set()
                        self.hub.stage(None, "mic", "blocked" if mic_off else "active", self.view().label)
                    while self._play_events:  # 回调记下的"真正写入输出流"的时刻与时长
                        t0, dur, g = self._play_events.popleft()
                        marks = self._turn_marks
                        if marks is not None and g == self._turn_gen and "first_enqueue" in marks:
                            marks.setdefault("first_play", t0)
                        log.info("play: start=%s dur=%.2fs gen=%d", datetime.fromtimestamp(t0).strftime("%H:%M:%S.%f")[:-3],
                                 dur, g)
                    if time.monotonic() - last_settings_poll >= SETTINGS_POLL_SEC:
                        last_settings_poll = time.monotonic()
                        self.store.maybe_reload()
                    if time.monotonic() - last_stats >= STATS_EVERY_SEC:
                        last_stats = time.monotonic()
                        wd = self.watchdog  # 留证据：下次再出事就能分清「回调停了」还是「回调在来但输入全零」
                        log.debug("audio-mon frames=%d cb_age=%.2fs signal_age=%.0fs zero_play=%.1fs", wd.count,
                                  time.monotonic() - wd.last_beat, time.monotonic() - wd.last_signal, wd.zero_play)
                        if (self._dropped, self._underflows) != stats:
                            stats = (self._dropped, self._underflows)
                            log.warning("累计丢帧 %d，输出欠载 %d", *stats)
            except KeyboardInterrupt:
                log.warning("voice exit code=%d reason=keyboard_interrupt", EXIT_QUIT)
                elog.emit("voice", "voice.exit", "收到 Ctrl+C，退出", level="warn", code=EXIT_QUIT, reason="keyboard_interrupt")
                self._exit_code = EXIT_QUIT
            finally:
                self.watchdog.disarm()
                # 离开 with 要 stop()/close() 音频流，卡死的驱动会让它永远不返回：先定好保险，到点强制退出（退出码不变）
                deadman = _arm_exit_deadman(EXIT_CRASH if (failed or sys.exc_info()[0] is not None) else
                                            (EXIT_QUIT if self._exit_code is None else self._exit_code))
        self._stop_evt.set()
        if self._spk_pool is not None:
            self._spk_pool.shutdown(wait=False, cancel_futures=True)  # 声纹线程若卡在推理里，不拖住退出
        self.runtime.stop()  # 最后一次心跳，下次开机据此算离线区间
        self.program_quiet.stop()
        self.monitor.stop()
        self.audit.stop()
        if self.task_ipc is not None:
            self.task_ipc.stop()
        if self.task_executor is not None:
            self.task_executor.shutdown()
        if self.tray:
            self.tray.stop()
        self.control.shutdown()
        deadman.cancel()  # 清理都完成了，保险撤掉（不撤的话测试里 run() 返回后进程还会被它强杀）
        code = EXIT_CRASH if failed else (EXIT_QUIT if self._exit_code is None else self._exit_code)
        log.warning("voice daemon exit code=%d", code)
        sys.exit(code)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="in_dev", type=int, default=None)
    ap.add_argument("--out", dest="out_dev", type=int, default=None)
    ap.add_argument("--no-gate", action="store_true", help="关闭声纹放行（调试用）")
    ap.add_argument("--gate-mode", choices=("enforce", "log"), default=SPK_GATE_MODE, help="已废弃（声纹不再拦截，只认人），保留只为启动参数兼容")
    ap.add_argument("--mic-gain", type=float, default=MIC_GAIN, help="软件增益（tanh 软限幅）")
    ap.add_argument("--aec", choices=("on", "off"), default="on" if AEC_ENABLED else "off", help="speex AEC 开关（默认关）")
    ap.add_argument("--no-tray", action="store_true", help="不显示托盘图标（调试用）")
    ap.add_argument("--control-port", type=int, default=CONTROL_PORT, help="本机控制口端口（/say、/status、/panel）")
    ap.add_argument("-v", action="store_true")
    ap.add_argument("--log-file", default=None, help="同时把日志写到这个文件（按 5MB 轮转，留 3 份）")
    a = ap.parse_args()
    log_ev = elog.init("voice")  # 统一事件日志（tmp\logs\events-voice-<日期>.jsonl），先于 VoiceDaemon 创建，EventHub 才拿得到
    install_crash_hooks()  # 未捕获异常 / 线程崩溃 / 原生崩溃：先记 voice.exit 事件 + 日志，再退出
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout), elog.LogBridge(log_ev)]  # stdout：避免 PowerShell 把 stderr 当错误；LogBridge：WARNING 以上同步进事件流
    if a.log_file:
        handlers.append(logging.handlers.RotatingFileHandler(a.log_file, maxBytes=LOG_MAX_BYTES, backupCount=3,
                                                             encoding="utf-8"))
    logging.basicConfig(level=logging.DEBUG if a.v else logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=handlers)
    for noisy in ("httpcore", "PIL"):  # 这两个的 DEBUG 太吵
        logging.getLogger(noisy).setLevel(logging.INFO)
    VoiceDaemon(in_dev=a.in_dev, out_dev=a.out_dev, speaker_gate=not a.no_gate, gate_mode=a.gate_mode,
                mic_gain=a.mic_gain, aec_on=a.aec == "on", tray_on=not a.no_tray, control_port=a.control_port).run()


if __name__ == "__main__":
    main()

# ---------- 评审未修项（voice-review.md 编号）----------
# P2-10 MIN_UTT_SEC 过滤无效：段内回看/尾静音的实际占比需实录测量后再定判据，盲调会误伤"开灯"这类短指令。
# P2-11 identify 已移到独立声纹线程与二次识别并行（_submit_identify）；前端线程仍会等两者都完成再判定，长段可能短暂阻塞打断判定，有丢帧计数日志。
# P2-15 TTS 仍用线性插值重采样：resample_poly 需要 scipy，本次不引入新依赖。
# P2（本期）打断阈值按 10-01 日志推算，验收时按 barge-mon 实测再定；TTS 服务端不会因客户端断开而中止合成。
