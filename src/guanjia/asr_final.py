"""整段精识别：VAD 切出一句话后，用更大的离线模型重新识别整段，替换流式小模型的结果。

两个后端（环境变量 GUANJIA_FINAL_ASR_MODEL 选择，缺省 fireredasr2）：
- fireredasr2：FireRedASR2-AED 跑在 WSL 的 GPU 小服务里（scripts/asr_server.py，端口 18798，约 3GB 显存），本进程用 HTTP 调用；
  同批实录句子上比 SenseVoice 更准，并给出置信度。服务不可用 / 超时（>0.8s）/ 出错时，当段自动回退 SenseVoice 并记
  asr.fallback 事件；连续失败 BREAKER_FAILS 次就暂时只用 SenseVoice，每隔 BREAKER_PROBE_SEC 探活，起来了自动恢复。
- sensevoice：本进程内 CPU 的 SenseVoice int8（2024-07-17 版；GUANJIA_FINAL_ASR_MODEL=2024 / 2025 / 模型目录路径）。
  永远会加载，作为 fireredasr2 的回退。

为什么要两段式：
- 流式 zipformer 小模型延迟低，适合"有人在说话"的即时反馈与打断，但字准率一般；
- 非流式大模型拿到整句上下文再识别，数字/时间/同音字明显更准；
- 实测低增益 + 噪声下 SiliconFlow 云端接口会直接返回空串，本地模型反而稳，且无网络与费用依赖。

SenseVoice 版本（2026-10-11 诊断）：2024-07-17 版同一批实录句子字错率 3.6%，2025-09-09 版 9.9%
（后者会丢首字，「管家」→「家」导致唤醒失败，英文还拆成「H A L O」）。识别入口前后各补 PAD_SEC 秒零；
输出里被空格拆开的单字母英文合并成一个词。2024 版输出阿拉伯数字（「7点半」「30%」），FireRedASR2 输出中文数词，
下游（wake.py 的拼音匹配/碎音判定）都不依赖数字写法，已核对。

游戏态：enter_gaming() 起一个后台检查，显卡空闲显存低于 gaming_free_mb（缺省 2048MB）就让服务卸载模型、改走 SenseVoice；
leave_gaming() 让服务重新加载，加载完成前仍走 SenseVoice。

失败兜底：识别超时、异常或结果为空时，原样返回调用方传入的流式结果，保证对话链路不断。
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import numpy as np

from guanjia.asr_text import merge_spaced_letters
from guanjia.ort_preload import preload_onnxruntime

preload_onnxruntime()
import sherpa_onnx  # noqa: E402  必须在预加载之后

log = logging.getLogger(__name__)

_MODELS = Path(__file__).resolve().parents[2] / "models"
MODEL_DIR_2024 = _MODELS / "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17"  # SenseVoice 默认（回退）
MODEL_DIR_2025 = _MODELS / "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2025-09-09"  # 旧：保留，环境变量切回
MODEL_ENV = "GUANJIA_FINAL_ASR_MODEL"  # fireredasr2（缺省）/ 2024 / 2025 / 一个 SenseVoice 模型目录路径
BACKEND_FIRERED = "fireredasr2"
BACKEND_SENSEVOICE = "sensevoice"
URL_ENV = "GUANJIA_ASR_URL"
DEFAULT_URL = "http://127.0.0.1:18798"  # WSL2 里的 FireRedASR2 服务（18791 是 TTS）
GAMING_FREE_MB_ENV = "GUANJIA_ASR_GAMING_FREE_MB"
SR = 16000
# 识别前给音频前后各补这么长的零：VAD 段头静音中位仅 ~40ms、段尾 ~80ms，2025 版 SenseVoice 会因此丢首字/尾字
# （实测 3/3 丢首字恢复）；流式识别器有 0.5s 预滚与真实尾静音，二次识别以前没有。
PAD_SEC = 0.4
REMOTE_TIMEOUT_SEC = 0.8  # GPU 服务单段超时（3 秒以内的语音）：超过就当段回退 SenseVoice
REMOTE_TIMEOUT_PER_SEC = 0.12  # 超过 3 秒的部分，每秒语音多给这么多超时
BREAKER_FAILS = 3  # 连续失败这么多次，熔断（暂时只用 SenseVoice）
BREAKER_PROBE_SEC = 30.0  # 熔断后每隔这么久探一次活
PREWARM_MIN_INTERVAL_SEC = 1.0  # 预热（给显卡加热）的最小间隔
GAMING_FIRST_CHECK_SEC = 8.0  # 进游戏态后先等 TTS 等别的卸载完，再看显存
GAMING_POLL_SEC = 20.0  # 游戏态里每隔这么久看一次空闲显存（游戏加载资源时显存是逐步涨上去的）


def resolve_model_dir(env: Mapping[str, str] | None = None) -> Path:
    """SenseVoice 用哪个模型目录：GUANJIA_FINAL_ASR_MODEL=2025 切回旧版；认不出的值回退默认并记警告，不让守护进程起不来。"""
    v = (os.environ if env is None else env).get(MODEL_ENV, "").strip()
    if v in ("", "2024", BACKEND_FIRERED, "firered"):
        return MODEL_DIR_2024
    if v == "2025":
        return MODEL_DIR_2025
    if Path(v).is_dir():
        return Path(v)
    log.warning("%s=%r 不认识，回退默认（FireRedASR2 + SenseVoice 2024 回退）", MODEL_ENV, v)
    return MODEL_DIR_2024


def resolve_backend(env: Mapping[str, str] | None = None) -> str:
    """主后端：2024 / 2025 / 模型目录路径 = 只用 SenseVoice；其余（缺省、fireredasr2、认不出的）= FireRedASR2 + SenseVoice 回退。"""
    v = (os.environ if env is None else env).get(MODEL_ENV, "").strip()
    if v in ("2024", "2025") or (v not in ("", BACKEND_FIRERED, "firered") and Path(v).is_dir()):
        return BACKEND_SENSEVOICE
    return BACKEND_FIRERED


MODEL_DIR = resolve_model_dir()


def pad_audio(samples: np.ndarray, sec: float = PAD_SEC) -> np.ndarray:
    """前后各补 sec 秒零（float32）；空音频原样返回。"""
    if len(samples) == 0 or sec <= 0:
        return np.ascontiguousarray(samples, dtype=np.float32)
    z = np.zeros(int(SR * sec), dtype=np.float32)
    return np.concatenate([z, np.asarray(samples, dtype=np.float32), z])


def _clean(text: str) -> str:
    """规整识别文字：合并被空格拆开的单字母英文；没有任何字词（只剩标点）当作没识别出来。"""
    text = merge_spaced_letters(text.strip())
    return text if re.search(r"\w", text) else ""


@dataclass
class FinalASRConfig:
    """精识别参数：language 固定 zh 避免把含糊段落判成粤语/日语；use_itn=True 输出标点规整的文本。"""

    model_dir: Path = MODEL_DIR
    num_threads: int = 4
    provider: str = "cpu"
    language: str = "zh"
    use_itn: bool = True
    timeout: float = 3.0  # SenseVoice 单句识别超时（秒），超时回退流式结果
    pad_sec: float = PAD_SEC  # 识别前后各补的零（秒）
    remote_timeout: float = REMOTE_TIMEOUT_SEC  # GPU 服务单段超时（秒，3 秒以内的语音）
    remote_timeout_per_sec: float = REMOTE_TIMEOUT_PER_SEC  # 语音超过 3 秒后，每多 1 秒语音多给的超时（秒）
    gaming_free_mb: int = field(default_factory=lambda: int(os.environ.get(GAMING_FREE_MB_ENV, "2048")))  # 游戏态下空闲显存低于它就卸载


@dataclass
class FinalResult:
    """一段的精识别结果。fallback 非空表示本想用 FireRedASR2 但这段走了 SenseVoice（timeout/error/breaker_open/gpu_released）。"""

    text: str
    backend: str  # fireredasr2 / sensevoice / none（空音频）
    ms: int = 0  # 含传输的总耗时
    conf: float | None = None  # FireRedASR2 逐 token 概率均值（只记录，暂不参与决策）
    min_conf: float | None = None  # 最没把握的那个 token 的概率
    fallback: str = ""
    server_ms: float | None = None  # GPU 服务端的推理耗时；ms − server_ms = 传输 + 排队


class FireRedClient:
    """FireRedASR2 GPU 服务（scripts/asr_server.py）的 HTTP 客户端：长连接，不走系统代理。任何网络/服务端错误都以异常或 False/None 返回。"""

    def __init__(self, base_url: str | None = None, timeout: float = 5.0) -> None:
        self.base = (base_url or os.environ.get(URL_ENV, DEFAULT_URL)).rstrip("/")
        # httpx 默认空闲 5 秒就断开长连接，说话间隔常常更长；放宽，省得每句话重新握手
        self._c = httpx.Client(timeout=timeout, trust_env=False, limits=httpx.Limits(keepalive_expiry=300.0))

    def recognize(self, samples: np.ndarray, timeout: float | None = None) -> dict:
        """识别一段 16k float32；返回服务端 JSON（text/conf/min_conf/tokens/ms）。失败抛异常。"""
        r = self._c.post(self.base + "/asr", content=np.ascontiguousarray(samples, dtype="<f4").tobytes(),
                         headers={"Content-Type": "application/octet-stream"}, timeout=timeout if timeout is not None else httpx.USE_CLIENT_DEFAULT)
        r.raise_for_status()
        return r.json()

    def alive(self, timeout: float = 0.3) -> bool:
        """服务在线且模型已加载（/health 正文 ok）。"""
        try:
            r = self._c.get(self.base + "/health", timeout=timeout)
            return r.status_code == 200 and r.text.strip() == "ok"
        except httpx.HTTPError:
            return False

    def stats(self, timeout: float = 2.0) -> dict | None:
        """显存/加载状态；服务不在线返回 None。"""
        try:
            r = self._c.get(self.base + "/stats", timeout=timeout)
            return r.json() if r.status_code == 200 else None
        except (httpx.HTTPError, ValueError):
            return None

    def warm(self, timeout: float = 1.0) -> bool:
        """让服务给显卡加热（听见有人开口时调；服务端在识别请求到来时自动停）。失败只返回 False。"""
        try:
            return self._c.post(self.base + "/warm", timeout=timeout).status_code == 200
        except httpx.HTTPError:
            return False

    def control(self, action: str, timeout: float = 120.0) -> bool:
        """让服务 load / unload 模型；失败只返回 False。"""
        try:
            return self._c.post(f"{self.base}/{action}", timeout=timeout).status_code == 200
        except httpx.HTTPError:
            return False


class FinalASR:
    """常驻的整段精识别器：SenseVoice 永远加载（回退），FireRedASR2 经 HTTP 调用；识别都在工作线程里跑，以便施加超时。"""

    # 类级默认值：测试里 FinalASR.__new__ 绕过 __init__ 时这些也有定义
    _remote: FireRedClient | None = None
    _rpool: ThreadPoolExecutor | None = None
    on_event: Callable[..., None] | None = None
    _clock: Callable[[], float] = staticmethod(time.monotonic)
    _fails = 0
    _open_until = 0.0  # 熔断到什么时刻（clock 时间）；0 = 没熔断
    _gpu_released = False  # 游戏态里显存紧张，服务端模型已卸载
    _last_warm = -1e9  # 上次预热的 clock 时间
    _gaming = False
    _guard: threading.Thread | None = None
    _guard_stop: threading.Event | None = None

    def __init__(self, cfg: FinalASRConfig | None = None, *, remote: FireRedClient | None = None,
                 on_event: Callable[..., None] | None = None, clock: Callable[[], float] = time.monotonic) -> None:
        self.cfg = cfg or FinalASRConfig()
        d = self.cfg.model_dir
        self._rec = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=str(d / "model.int8.onnx"),
            tokens=str(d / "tokens.txt"),
            num_threads=self.cfg.num_threads,
            provider=self.cfg.provider,
            language=self.cfg.language,
            use_itn=self.cfg.use_itn,
        )
        # 单线程执行器：识别串行进行；超时的那次仍会在后台跑完，后续请求排队
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="asr-final")
        if remote is None and resolve_backend() == BACKEND_FIRERED:
            remote = FireRedClient()
        self._setup_remote(remote, on_event, clock)
        log.info("二次识别后端: %s（SenseVoice 模型 %s，回退常备）", BACKEND_FIRERED if self._remote else BACKEND_SENSEVOICE, d.name)

    def _setup_remote(self, remote: FireRedClient | None, on_event: Callable[..., None] | None, clock: Callable[[], float]) -> None:
        self._remote = remote
        self.on_event = on_event
        self._clock = clock
        self._fails, self._open_until, self._gpu_released, self._gaming = 0, 0.0, False, False
        self._guard = self._guard_stop = None
        self._rpool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="asr-remote") if remote is not None else None

    # ---------- 事件 ----------
    def _emit(self, name: str, msg: str, level: str = "info", **data) -> None:
        """asr.fallback / asr.backend 事件（由 voice 接到 elog）；回调出错不能影响识别。"""
        (log.warning if level == "warn" else log.info)("%s: %s %s", name, msg, data or "")
        if self.on_event is not None:
            try:
                self.on_event(name, msg, level=level, **data)
            except Exception:  # noqa: BLE001
                pass

    # ---------- FireRedASR2 的可用性（熔断 + 探活） ----------
    def _remote_ready(self) -> tuple[bool, str]:
        if self._remote is None:
            return False, ""
        if self._gpu_released:
            return False, "gpu_released"
        if self._open_until:
            now = self._clock()
            if now < self._open_until:
                return False, "breaker_open"
            if self._remote.alive():  # 冷却到了：用 /health 探活，别拿真实语音去试
                self._fails, self._open_until = 0, 0.0
                self._emit("asr.backend", "FireRedASR2 服务恢复，改回 GPU 识别", state="recovered")
            else:
                self._open_until = now + BREAKER_PROBE_SEC
                return False, "breaker_open"
        return True, ""

    def remote_timeout_for(self, dur_sec: float) -> float:
        """GPU 服务这一段的超时：解码耗时和说了多少字成正比（实测 ≈ 每秒语音 ~0.1s，3 秒以内 <0.3s），所以长句放宽。"""
        return self.cfg.remote_timeout + self.cfg.remote_timeout_per_sec * max(0.0, dur_sec - 3.0)

    def _remote_failed(self, reason: str, exc: BaseException | None, count: bool = True) -> None:
        if count:
            self._fails += 1
        self._emit("asr.fallback", f"FireRedASR2 {reason}，这一段改用 SenseVoice", level="warn", reason=reason, fails=self._fails,
                   counted=count, error=(f"{type(exc).__name__}: {exc}"[:200] if exc else None))
        if count and self._fails >= BREAKER_FAILS and not self._open_until:
            self._open_until = self._clock() + BREAKER_PROBE_SEC
            self._emit("asr.fallback", f"FireRedASR2 连续失败 {self._fails} 次，暂时只用 SenseVoice（每 {BREAKER_PROBE_SEC:.0f}s 探活）",
                       level="warn", reason="breaker_open", fails=self._fails)

    def prewarm(self) -> None:
        """听见有人开口（VAD 起判）时调：让 GPU 服务给显卡加热。显卡空闲时在 P8（210MHz），不加热的话这一段识别要 ~480ms，
        说话期间加热后 ~220ms（实测）。非阻塞、限速、失败静默；服务不可用/已卸载/熔断中时不打扰。"""
        if self._remote is None or self._rpool is None or self._gpu_released or self._open_until:
            return
        now = self._clock()
        if now - self._last_warm < PREWARM_MIN_INTERVAL_SEC:
            return
        self._last_warm = now
        self._rpool.submit(self._warm_quiet)

    def _warm_quiet(self) -> None:
        try:
            self._remote.warm()
        except Exception:  # noqa: BLE001
            pass

    # ---------- 识别 ----------
    def _decode(self, samples: np.ndarray) -> str:
        """SenseVoice 识别（补零、字母合并、纯标点当空）。"""
        s = self._rec.create_stream()
        s.accept_waveform(SR, pad_audio(samples, self.cfg.pad_sec))
        self._rec.decode_stream(s)
        # 2024 版对近乎静音的段会只输出一个「。」：没有任何字词，按"没识别出来"处理，别让它冒充一条识别结果
        return _clean(s.result.text)

    def _transcribe_sv(self, x: np.ndarray, fallback: str, timeout: float | None) -> str:
        fut = self._pool.submit(self._decode, x)
        try:
            text = fut.result(timeout=self.cfg.timeout if timeout is None else timeout)
        except FutureTimeout:
            log.warning("精识别超时（%.1fs 语音），回退流式结果", len(x) / SR)
            return fallback
        except Exception:
            log.exception("精识别失败，回退流式结果")
            return fallback
        return text or fallback

    def transcribe_ex(self, samples: np.ndarray, fallback: str = "", timeout: float | None = None) -> FinalResult:
        """识别一整段语音，返回带后端名/耗时/置信度的结果。

        Args:
            samples: 16k 单声道 float32（-1~1），通常是 VAD Segment.samples。
            fallback: 流式小模型对同一句的结果；精识别失败/超时/为空时原样返回它。
            timeout: SenseVoice 本次超时秒数，缺省用配置值。
        """
        if samples is None or len(samples) == 0:
            return FinalResult(fallback, "none")
        t0 = time.perf_counter()
        x = np.ascontiguousarray(samples, dtype=np.float32)
        use_remote, why = self._remote_ready()
        if use_remote:
            fut = self._rpool.submit(self._remote.recognize, pad_audio(x, self.cfg.pad_sec))
            try:
                r = fut.result(timeout=self.remote_timeout_for(len(x) / SR))
            except FutureTimeout:
                why = "timeout"
                # 服务还活着只是这一段慢（长句、没有结束符跑飞的解码）→ 回退这一段，但不算"服务故障"，不计入熔断；
                # 服务连 /health 都不应才算失败
                self._remote_failed(why, None, count=not self._remote.alive())
            except Exception as e:  # noqa: BLE001  连接被拒、5xx、JSON 坏了……都当这一段回退
                why = "error"
                self._remote_failed(why, e)
            else:
                self._fails = 0
                text = _clean(r.get("text") or "")
                return FinalResult(text or fallback, BACKEND_FIRERED, round((time.perf_counter() - t0) * 1000),
                                   r.get("conf"), r.get("min_conf"), server_ms=r.get("ms"))
        text = self._transcribe_sv(x, fallback, timeout)
        return FinalResult(text, BACKEND_SENSEVOICE, round((time.perf_counter() - t0) * 1000), fallback=why)

    def transcribe(self, samples: np.ndarray, fallback: str = "", timeout: float | None = None) -> str:
        """同 transcribe_ex，只返回文字。"""
        return self.transcribe_ex(samples, fallback, timeout).text

    # ---------- 游戏态 ----------
    def enter_gaming(self, start_thread: bool = True) -> None:
        """进入游戏态：后台盯着空闲显存，低于 gaming_free_mb 就让服务卸载模型。没有 GPU 服务时什么都不做。"""
        if self._remote is None:
            return
        self._gaming = True
        if start_thread and (self._guard is None or not self._guard.is_alive()):
            stop = self._guard_stop = threading.Event()

            def loop() -> None:
                if stop.wait(GAMING_FIRST_CHECK_SEC):
                    return
                while not stop.is_set():
                    self.gaming_check()
                    if stop.wait(GAMING_POLL_SEC):
                        return

            self._guard = threading.Thread(target=loop, name="asr-gaming-guard", daemon=True)
            self._guard.start()

    def gaming_check(self) -> None:
        """看一眼空闲显存；紧张且模型还在显存里，就让服务卸载，之后走 SenseVoice CPU。"""
        if self._remote is None or self._gpu_released:
            return
        st = self._remote.stats()
        free = (st or {}).get("gpu_free_mb")
        if free is None or free >= self.cfg.gaming_free_mb:
            return
        if self._remote.control("unload"):
            self._gpu_released = True
            self._emit("asr.backend", f"游戏态显存紧张（空闲 {free}MB < {self.cfg.gaming_free_mb}MB），已释放 FireRedASR2，改走 SenseVoice CPU",
                       state="released", free_mb=free)

    def leave_gaming(self, sync: bool = False) -> None:
        """退出游戏态：停掉显存检查；已释放的话让服务重新加载（后台，加载完成前仍走 SenseVoice）。"""
        if self._remote is None:
            return
        self._gaming = False
        if self._guard_stop is not None:
            self._guard_stop.set()
        if self._gpu_released:
            if sync:
                self._reload()
            else:
                threading.Thread(target=self._reload, name="asr-reload", daemon=True).start()

    def _reload(self) -> None:
        ok = self._remote.control("load", timeout=120.0)
        self._gpu_released = False  # 加载失败也放开：服务收到第一段会自动加载，真不行熔断会接管
        self._fails, self._open_until = 0, 0.0
        if ok:
            self._emit("asr.backend", "游戏结束，FireRedASR2 已重新加载", state="loaded")
        else:
            self._emit("asr.fallback", "游戏结束但 FireRedASR2 加载没成功，先走 SenseVoice 由熔断探活接管", level="warn", reason="load_failed")

    def close(self) -> None:
        if self._guard_stop is not None:
            self._guard_stop.set()
        if self._rpool is not None:
            self._rpool.shutdown(wait=False, cancel_futures=True)


if __name__ == "__main__":
    # 自测：.venv\Scripts\python.exe -m guanjia.asr_final [wav ...]，缺省跑 tmp/mic_check.wav 与 SenseVoice 自带 test_wavs
    import sys
    import wave

    def load16k(p: Path) -> np.ndarray:
        with wave.open(str(p), "rb") as w:
            sr, ch = w.getframerate(), w.getnchannels()
            x = np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) / 32768.0
        if ch > 1:
            x = x.reshape(-1, ch).mean(axis=1)
        if sr != SR:  # 自测用线性插值重采样即可
            x = np.interp(np.linspace(0, len(x) - 1, int(len(x) * SR / sr)), np.arange(len(x)), x).astype(np.float32)
        return x

    t0 = time.perf_counter()
    asr = FinalASR(on_event=lambda n, m, level="info", **d: print("EVENT", n, m, d))
    print(f"load {time.perf_counter() - t0:.2f}s, remote={'on' if asr._remote else 'off'}")
    root = Path(__file__).resolve().parents[2]
    files = [Path(p) for p in sys.argv[1:]] or [root / "tmp" / "mic_check.wav", *sorted((MODEL_DIR / "test_wavs").glob("zh*.wav"))]
    for f in files:
        x = load16k(f)
        r = asr.transcribe_ex(x, fallback="<流式兜底>")
        print(f"{f.name} ({len(x) / SR:.1f}s): {r.text}   [{r.backend} {r.ms}ms conf={r.conf} {r.fallback}]")
    # 兜底路径：超时与空输入都应返回 fallback
    print("timeout ->", asr.transcribe(load16k(files[0]), fallback="<流式兜底>", timeout=0.001))
    print("empty   ->", asr.transcribe(np.zeros(0, np.float32), fallback="<流式兜底>"))
