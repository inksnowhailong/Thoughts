"""IndexTTS 2 本地合成服务（跑在 F:\\coding\\index-tts 的 uv 环境里）。

POST /tts  {"text": "..."}  -> wav 字节（16bit 单声道）；模型已卸载时先自动加载
POST /unload               -> 进游戏态信号（守护进程进入游戏态时调用）：空闲显存够就保持不动；真不够（< GUANJIA_TTS_GAMING_FREE_MB，
                              缺省 3072MB，0=关闭）就让进程退出（退出码 75，内存归零），退出游戏后由启动器重新拉起。
                              不再在进程内卸载 / 重载（10-13 反复开关游戏把内存撑到 13.8GB 被 OOM 杀）；
                              /unload?force=1 才在进程内卸载（手工排障用，卸载后会 malloc_trim）
POST /load                 -> 退出游戏态：取消显存看守；若模型被 force 卸载过则重新加载（阻塞到加载完成）
GET  /health               -> 200，正文 ok / unloaded
GET  /stats                -> JSON：是否已加载、RSS / 峰值 RSS（MB）、空闲显存（MB）、加载 / 卸载次数

模型只加载一次；音色用 voices/ref.wav（5~10 秒参考音频，零样本克隆）。
启动：cd F:\\coding\\index-tts && uv run python F:\\coding\\guanjia\\tts_server\\tts_server.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from tts_guard import GamingGuard, rss_mb, trim_heap  # noqa: E402  部署时和本文件放同一目录
REF_WAV = Path(os.environ.get("GUANJIA_TTS_REF", HERE / "voices" / "ref.wav"))
if os.environ.get("GUANJIA_TTS_VOICE"):  # 按名字选默认音色：GUANJIA_TTS_VOICE=scv_zh -> voices/scv_zh.wav
    REF_WAV = REF_WAV.parent / f"{Path(os.environ['GUANJIA_TTS_VOICE']).name}.wav"
PORT = int(os.environ.get("GUANJIA_TTS_PORT", "18790"))
INDEX_TTS_DIR = Path(os.environ.get("INDEX_TTS_DIR", r"F:\coding\index-tts"))
MODEL_DIR = os.environ.get("GUANJIA_TTS_MODEL_DIR", "checkpoints")  # 相对 INDEX_TTS_DIR；2.5 版权重放 checkpoints_25
USE_DEEPSPEED = os.environ.get("GUANJIA_TTS_DEEPSPEED", "0") == "1"
USE_ACCEL = os.environ.get("GUANJIA_TTS_ACCEL", "0") == "1"          # GPT 加速引擎
USE_COMPILE = os.environ.get("GUANJIA_TTS_COMPILE", "0") == "1"      # torch.compile
TTS_VERSION = os.environ.get("GUANJIA_TTS_VERSION", "2")             # "2" 或 "2.5"
VOICES_DIR = REF_WAV.parent  # 音色目录：请求里 {"voice": "xxx"} 选 voices/xxx.wav；注意 IndexTTS 按路径缓存音色特征，同名文件被替换需重启服务
# 提速/语速默认值，单次请求可用同名字段覆盖：
# - diffusion_steps：s2mel 扩散步数（官方写死 25，15 步耗时约减 40%，听感差异很小）
# - num_beams：GPT 束搜索宽度（官方默认 3，1 即纯采样，首句更快）
# - duration_factor：时长系数，<1 语速更快（0.92 ≈ 快 8%）
DEFAULT_STEPS = int(os.environ.get("GUANJIA_TTS_STEPS", "15"))
DEFAULT_GEN = {
    "num_beams": int(os.environ.get("GUANJIA_TTS_BEAMS", "1")),
    "duration_factor": float(os.environ.get("GUANJIA_TTS_SPEED", "0.92")),
}

sys.path.insert(0, str(INDEX_TTS_DIR))
os.chdir(INDEX_TTS_DIR)
if TTS_VERSION == "2.5":
    from indextts.infer_v2_5 import IndexTTS2  # noqa: E402  2.5 的类同名，精度参数叫 use_bf16
    _prec = {"use_bf16": True}
    _infer_extra = {"lang": "zh"}  # 2.5 必填语种
else:
    from indextts.infer_v2 import IndexTTS2  # noqa: E402
    _prec = {"use_fp16": True}
    _infer_extra = {}

_steps = {"n": DEFAULT_STEPS}  # 当前请求的扩散步数，由 _patch_steps 注入的包装读取（合成在 _lock 内串行，全局变量安全）
_lock = threading.Lock()  # GPU 串行：合成与加载/卸载互斥
tts = None  # 模型实例；只有 /unload?force=1 后为 None，下一次 /load 或 /tts 时重新加载
_counts = {"loads": 0, "unloads": 0}
GAMING_FREE_MB = int(os.environ.get("GUANJIA_TTS_GAMING_FREE_MB", "3072"))  # 游戏态空闲显存低于它就退出进程；0=关闭


def _cuda_free_mb():
    """全卡空闲显存 MB（含游戏等其它进程的占用）；读不到返回 None。"""
    try:
        import torch

        return int(torch.cuda.mem_get_info()[0] // (1024 * 1024))
    except Exception:  # noqa: BLE001
        return None


def _release_process(code: int, reason: str) -> None:
    """为游戏腾显存：回应完 /unload 后退出进程（内存 + 显存一次性归零），启动器等显存空出来再拉起。"""
    print(f"released for gaming: {reason}; rss={rss_mb()}MB, exit {code}", flush=True)
    t = threading.Timer(0.3, os._exit, args=(code,))
    t.daemon = True
    t.start()


def _load() -> None:
    """加载模型并预热一次（调用方持有 _lock）；已加载则直接返回。"""
    global tts
    if tts is not None:
        return
    print(f"loading IndexTTS2 ... (rss={rss_mb()}MB)", flush=True)
    t0 = time.perf_counter()
    tts = IndexTTS2(cfg_path=f"{MODEL_DIR}/config.yaml", model_dir=MODEL_DIR, use_cuda_kernel=False,
                    use_deepspeed=USE_DEEPSPEED, use_accel=USE_ACCEL, use_torch_compile=USE_COMPILE, **_prec)
    print(f"version={TTS_VERSION} model_dir={MODEL_DIR} deepspeed={USE_DEEPSPEED} accel={USE_ACCEL} compile={USE_COMPILE}", flush=True)
    print(f"loaded in {time.perf_counter() - t0:.1f}s, ref={REF_WAV}", flush=True)
    _patch_steps()
    print(f"defaults: steps={DEFAULT_STEPS} {DEFAULT_GEN}", flush=True)
    # 预热一次，把首句的编译/缓存开销吃掉
    _warm = Path(tempfile.gettempdir()) / "guanjia_tts_warm.wav"
    t0 = time.perf_counter()
    tts.infer(spk_audio_prompt=str(REF_WAV), text="预热。", output_path=str(_warm), verbose=False, **_infer_extra, **DEFAULT_GEN)
    print(f"warmup {time.perf_counter() - t0:.1f}s", flush=True)
    _counts["loads"] += 1
    print(f"loaded #{_counts['loads']} rss={rss_mb()}MB", flush=True)  # 每次加载后的 RSS，排查内存涨幅用


def _patch_steps() -> None:
    """IndexTTS 的扩散步数写死在 infer_generator 局部变量里，这里包一层 cfm.inference 改写 n_timesteps，免改上游源码。"""
    cfm = tts.s2mel.models["cfm"]
    orig = cfm.inference

    def inference(mu, x_lens, prompt, style, f0, n_timesteps, *a, **k):
        return orig(mu, x_lens, prompt, style, f0, _steps["n"] or n_timesteps, *a, **k)

    cfm.inference = inference


def _unload() -> None:
    """进程内释放模型与显存（仅 /unload?force=1）：丢掉实例引用 → gc → 清 CUDA 缓存 → 把堆里的空闲页还给系统。调用方持有 _lock。"""
    global tts
    if tts is None:
        return
    tts = None
    import gc

    import torch

    gc.collect()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
    trim_heap()  # 不 trim 的话卸载后 RSS 基本不降（glibc 堆碎片），反复重载会越攒越高
    _counts["unloads"] += 1
    print(f"unloaded #{_counts['unloads']} rss={rss_mb()}MB", flush=True)

guard = GamingGuard(_cuda_free_mb, _release_process, keep_free_mb=GAMING_FREE_MB, log=lambda m: print(m, flush=True))

with _lock:
    _load()


class H(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # 安静一点
        pass

    def do_GET(self):
        if self.path == "/health":
            self.send_response(200); self.end_headers(); self.wfile.write(b"ok" if tts is not None else b"unloaded")
        elif self.path == "/stats":
            hwm = None
            try:
                hwm = next(int(ln.split()[1]) // 1024 for ln in open("/proc/self/status") if ln.startswith("VmHWM:"))
            except (OSError, StopIteration, ValueError):
                pass
            body = json.dumps({"loaded": tts is not None, "rss_mb": rss_mb(), "hwm_mb": hwm, "cuda_free_mb": _cuda_free_mb(),
                               "gaming_watch": guard.watching, "gaming_free_mb": GAMING_FREE_MB, **_counts}).encode()
            self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
        else:
            self.send_response(404); self.end_headers()

    def do_POST(self):
        url = urlsplit(self.path)
        if url.path == "/unload":
            if parse_qs(url.query).get("force") == ["1"]:  # 手工排障：进程内卸载
                with _lock:
                    _unload()
                res = "ok"
            else:  # 守护进程进游戏态：显存够就不动，不够才退出进程（见 tts_guard.py）
                res = guard.enter()
            self.send_response(200); self.end_headers(); self.wfile.write(res.encode())
            return
        if url.path == "/load":
            guard.leave()
            with _lock:
                _load()  # 已加载则立刻返回
            self.send_response(200); self.end_headers(); self.wfile.write(b"ok")
            return
        if self.path != "/tts":
            self.send_response(404); self.end_headers(); return
        n = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(n) or b"{}")
        text = (body.get("text") or "").strip()
        if not text:
            self.send_response(400); self.end_headers(); return
        # 可选：生成参数透传（top_k/top_p/temperature/max_mel_tokens/do_sample 等）与 verbose 分阶段计时
        gen_kwargs = {**DEFAULT_GEN, **{k: v for k, v in body.items() if k not in ("text",)}}
        verbose = bool(gen_kwargs.pop("verbose", False))
        steps = int(gen_kwargs.pop("diffusion_steps", DEFAULT_STEPS))
        voice = gen_kwargs.pop("voice", None)
        ref = VOICES_DIR / f"{Path(voice).name}.wav" if voice else REF_WAV  # 只取文件名，防目录穿越
        if not ref.is_file():
            self.send_response(400); self.end_headers(); self.wfile.write(f"no voice {voice}".encode()); return
        out = Path(tempfile.gettempdir()) / f"guanjia_tts_{threading.get_ident()}_{int(time.time() * 1000)}.wav"
        try:
            with _lock:  # GPU 串行
                _load()  # 卸载后首句自动加载，/load 没调到也不至于合成失败
                _steps["n"] = steps
                t = time.perf_counter()
                tts.infer(spk_audio_prompt=str(ref), text=text, output_path=str(out), verbose=verbose, **_infer_extra, **gen_kwargs)
                dt = time.perf_counter() - t
            data = out.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("X-Synth-Seconds", f"{dt:.2f}")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:  # noqa: BLE001
            msg = str(e).encode("utf-8")
            self.send_response(500); self.send_header("Content-Length", str(len(msg))); self.end_headers(); self.wfile.write(msg)
        finally:
            try:
                out.unlink(missing_ok=True)
            except OSError:
                pass


if __name__ == "__main__":
    print(f"serving on http://127.0.0.1:{PORT}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
