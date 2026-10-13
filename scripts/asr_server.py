"""FireRedASR2-AED GPU 识别服务（跑在 WSL 的独立 venv：~/guanjia-asr/venv，不碰 IndexTTS 的环境）。

POST /asr      body = 16k 单声道 float32 小端 PCM（application/octet-stream）-> JSON
               {"text", "conf"(逐 token 概率均值), "min_conf", "tokens": [[token, conf], ...], "dur_s", "ms"(服务端推理耗时)}
POST /warm     开始给显卡加热（听见有人开口时调；识别/卸载请求一到就停，最长 10 秒）
POST /unload   释放模型与显存（守护进程进入游戏态、显存紧张时调用）
POST /load     重新加载模型（退出游戏时调用，阻塞到加载完成）
GET  /health   200，正文 ok / unloaded
GET  /stats    JSON：模型是否加载、显卡空闲/总显存（MB）、本进程已用显存（MB）、本进程常驻内存 rss_mb

用 HTTP/1.1 长连接（客户端复用连接，省掉每段一次 TCP 握手）。模型只加载一次；卸载后来的第一个 /asr 会自动加载。
启动：计划任务 GuanjiaASR-WSL → scripts/wsl-asr-launcher.ps1（带重试）。环境变量：
  GUANJIA_ASR_PORT=18798  GUANJIA_ASR_MODEL_DIR  GUANJIA_ASR_REPO  GUANJIA_ASR_DTYPE=fp16|bf16|fp32  GUANJIA_ASR_BEAM=3
"""

from __future__ import annotations

import gc
import inspect
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import numpy as np

HOME = Path.home()
PORT = int(os.environ.get("GUANJIA_ASR_PORT", "18798"))
MODEL_DIR = os.environ.get("GUANJIA_ASR_MODEL_DIR", str(HOME / "guanjia-asr" / "models" / "FireRedASR2-AED"))
REPO = os.environ.get("GUANJIA_ASR_REPO", str(HOME / "guanjia-asr" / "repo"))
DTYPE = os.environ.get("GUANJIA_ASR_DTYPE", "fp16")  # bf16 在 4070 Ti Super 上也行，但 fp16 精度更好
BEAM = int(os.environ.get("GUANJIA_ASR_BEAM", "3"))
MAX_BYTES = 16000 * 4 * 60  # 单段最长 60 秒，防止畸形请求吃光内存

# 直接以顶层包 fireredasr2 导入：官方 fireredasr2s/__init__ 会把 VAD/LID/标点整套（和一堆依赖）都拉进来，这里只要 AED
sys.path.insert(0, str(Path(REPO) / "fireredasr2s"))
import torch  # noqa: E402

if DTYPE == "fp16":  # 官方 use_half 在支持 bf16 的卡上走 bf16；要 fp16 就让它认为不支持 bf16
    torch.cuda.is_bf16_supported = lambda *a, **k: False
from fireredasr2.asr import FireRedAsr2, FireRedAsr2Config  # noqa: E402
from fireredasr2.models.module import transformer_decoder as _td  # noqa: E402

# 所有 GPU 工作（加载/卸载/识别）都在这一个常驻线程里串行执行：HTTP 处理线程每个新连接一个，而 cuBLAS 句柄等是按线程
# 初始化的，在新线程里做识别每次要多花 ~100ms+；固定一个线程，句柄只初始化一次。
_gpu_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="gpu")


def gpu(fn, *args):
    return _gpu_pool.submit(fn, *args).result()
asr: FireRedAsr2 | None = None
_captured: dict = {}  # 最近一次解码的 hyps（拿逐 token 置信度）


def _patch_token_conf() -> bool:
    """官方 batch_beam_search 只回传整句平均置信度；运行时改写源码，把逐 token 置信度也带出来（失败就只有整句均值）。"""
    try:
        import re
        import textwrap

        # 官方源码里有顶格的注释行，textwrap.dedent 会因此不脱缩进，所以手动去掉类体的 4 格缩进
        src = "\n".join(ln[4:] if ln.startswith("    ") else "" for ln in inspect.getsource(_td.TransformerDecoder.batch_beam_search).split("\n"))
        src, n1 = re.subn(r"^(\s*)confidence = confidence\.mean\(\)", r"\1tok_conf = confidence\n\1confidence = confidence.mean()", src, flags=re.M)
        src, n2 = re.subn(r'("confidence": confidence)\n', r'\1, "tok_conf": tok_conf\n', src)
        if n1 != 1 or n2 != 1:
            raise RuntimeError(f"source layout changed ({n1},{n2})")
        ns: dict = dict(_td.__dict__)
        exec(src, ns)  # noqa: S102
        _td.TransformerDecoder.batch_beam_search = ns["batch_beam_search"]
        return True
    except Exception as e:  # noqa: BLE001
        print(f"token-conf patch failed: {e!r}", flush=True)
        return False


TOKEN_CONF = _patch_token_conf()


def _patch_fast_attention() -> bool:
    """解码提速（输出与官方逐字一致，实测 16/16 段文本相同）：官方解码器每一步为每层重算全部编码帧的 K/V、并用 6~7 个小 kernel
    拼出注意力，WSL2 里 kernel 启动开销大，一步要 ~16ms。这里 ① 注意力换成 F.scaled_dot_product_attention（融合成 1~2 个 kernel）
    ② 交叉注意力的 K/V 每个请求只算一次（以编码输出张量的身份为键，请求结束清掉）。单条语音（batch=1）无 padding，所以掩码可以省。
    整体 ~2 倍速，3~4 秒的一句从 ~350ms 降到 ~190ms。"""
    try:
        import torch.nn.functional as F

        _td.DecoderScaledDotProductAttention.forward = lambda self, q, k, v, mask=None: F.scaled_dot_product_attention(q, k, v)

        def cross_forward(self, q, k, v, mask=None):
            bs = q.size(0)
            qq = self.w_qs(q).view(bs, -1, self.n_head, self.d_k).transpose(1, 2)
            if getattr(self, "_enc", None) is not k:  # 这个请求第一步：算一次并缓存（持有 k 的引用，所以身份比较不会被地址复用骗到）
                self._enc = k
                self._kv = (self.w_ks(k).view(bs, -1, self.n_head, self.d_k).transpose(1, 2),
                            self.w_vs(v).view(bs, -1, self.n_head, self.d_k).transpose(1, 2))
            o = F.scaled_dot_product_attention(qq, self._kv[0], self._kv[1])
            return self.fc(o.transpose(1, 2).contiguous().view(bs, -1, self.d_model))

        for layer in asr.model.decoder.layer_stack:
            layer.cross_attn.forward = cross_forward.__get__(layer.cross_attn)
        return True
    except Exception as e:  # noqa: BLE001
        print(f"fast-attention patch failed: {e!r}", flush=True)
        return False


def _clear_cross_cache() -> None:
    if asr is not None:
        for layer in asr.model.decoder.layer_stack:
            layer.cross_attn._enc = layer.cross_attn._kv = None


def _gpu_mb() -> dict:
    if not torch.cuda.is_available():
        return {}
    free, total = torch.cuda.mem_get_info()
    return {"gpu_free_mb": int(free / 2**20), "gpu_total_mb": int(total / 2**20),
            "proc_mb": int(torch.cuda.memory_reserved() / 2**20), "rss_mb": _rss_mb()}


_heat_go = threading.Event()  # 常驻加热线程的"开工"信号
_heat_stop = threading.Event()
_heat_deadline = 0.0
_heater_started = False
_heater_mu = threading.Lock()


def _heat_loop() -> None:
    """显卡加热线程（常驻，平时睡在 _heat_go 上）：满载小矩阵乘直到被叫停或到期。空闲时显卡在 P8（210MHz），实测 0.3 秒满载就升到
    2880MHz，之后约 2 秒内保持；说话期间持续加热，说完（识别请求一到）立刻停，识别就能在高频下跑（冷启动 ~600ms → ~230ms）。
    必须常驻：新建线程第一次用 cuBLAS 要初始化句柄，在 P8 下要 1 秒以上，会把 /warm 的 HTTP 响应也拖住。"""
    a = torch.randn(2048, 2048, device="cuda", dtype=torch.half)
    (a @ a).sum().item()  # 在这个线程里把 cuBLAS 初始化掉
    while True:
        _heat_go.wait()
        while not _heat_stop.is_set() and time.monotonic() < _heat_deadline:
            for _ in range(4):
                a @ a
            torch.cuda.synchronize()
        _heat_go.clear()


def _ensure_heater() -> None:
    global _heater_started
    with _heater_mu:
        if not _heater_started:
            _heater_started = True
            threading.Thread(target=_heat_loop, name="gpu-heat", daemon=True).start()


def _start_heat(max_sec: float = 10.0) -> None:
    global _heat_deadline
    if asr is None:
        return
    _heat_deadline = time.monotonic() + max_sec
    _heat_stop.clear()
    _heat_go.set()


def _stop_heat() -> None:
    _heat_stop.set()  # 加热循环每个小批次检查一次，几毫秒内停；不 join，免得识别请求等它

def _trim_heap() -> None:
    """把空闲堆还给系统：glibc 释放大块内存后不一定归还，WSL 里内存总共 16GB 要和 TTS（~7.5GB）共用。"""
    try:
        import ctypes

        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except Exception:  # noqa: BLE001
        pass


def _rss_mb() -> int:
    try:
        with open("/proc/self/statm") as f:
            return int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE") // 2**20
    except Exception:  # noqa: BLE001
        return -1


def _build_asr(cfg) -> FireRedAsr2:
    """等价于 FireRedAsr2.from_pretrained("aed", ...)，但内存友好：官方做法把 4GB 的 fp32 权重先整个读进内存、再建一份
    fp32 模型拷贝、再转 fp16 上 GPU，峰值 ~8GB、之后常驻 ~4GB（实测，并在 WSL 里和 TTS 一起把 16GB 撑爆，被 OOM killer 杀掉）。
    这里用 mmap 读权重（页缓存，可回收），模型直接建在 GPU 上，权重从 mmap 流式拷进去。"""
    from fireredasr2.data.asr_feat import ASRFeatExtractor
    from fireredasr2.models.fireredasr_aed import FireRedAsrAed
    from fireredasr2.tokenizer.aed_tokenizer import ChineseCharEnglishSpmTokenizer

    feat = ASRFeatExtractor(os.path.join(MODEL_DIR, "cmvn.ark"))
    path = os.path.join(MODEL_DIR, "model.pth.tar")
    try:
        package = torch.load(path, map_location="cpu", mmap=True, weights_only=False)
    except Exception as e:  # noqa: BLE001  老格式不支持 mmap：退回整个读入
        print(f"mmap load failed ({e!r}), falling back to a full read", flush=True)
        package = torch.load(path, map_location="cpu", weights_only=False)
    with torch.device("cuda"):
        model = FireRedAsrAed.from_args(package["args"])
    model.load_state_dict(package["model_state_dict"], strict=False)
    del package
    model.eval()
    tokenizer = ChineseCharEnglishSpmTokenizer(os.path.join(MODEL_DIR, "dict.txt"), os.path.join(MODEL_DIR, "train_bpe1000.model"))
    return FireRedAsr2("aed", feat, model, tokenizer, None, cfg)  # __init__ 里按配置转 fp16（逐个参数原地转，不会再占一份）


def _load() -> None:
    """加载并预热（只在 GPU 工作线程里调用）；已加载则直接返回。"""
    global asr
    if asr is not None:
        return
    t0 = time.perf_counter()
    cfg = FireRedAsr2Config(use_gpu=True, use_half=DTYPE != "fp32", beam_size=BEAM, nbest=1, decode_max_len=0,
                            softmax_smoothing=1.25, aed_length_penalty=0.6, eos_penalty=1.0, return_timestamp=False)
    asr = _build_asr(cfg)
    torch.cuda.empty_cache()  # 建模型时 fp32 的临时显存（~4.4GB）转 fp16 后还留在缓存里，还给系统，常驻只剩 ~2.3GB
    _trim_heap()
    orig = asr.model.transcribe

    def wrapped(*a, **k):
        h = orig(*a, **k)
        _captured["hyps"] = h
        return h

    asr.model.transcribe = wrapped
    fast = os.environ.get("GUANJIA_ASR_FAST", "1") == "1" and _patch_fast_attention()
    _ensure_heater()
    print(f"loaded in {time.perf_counter() - t0:.1f}s dtype={DTYPE} beam={BEAM} token_conf={TOKEN_CONF} fast={fast} {_gpu_mb()}", flush=True)
    t0 = time.perf_counter()
    _run(np.random.default_rng(0).standard_normal(16000).astype(np.float32) * 0.05)  # 预热：吃掉首次 CUDA 初始化/内核选择
    print(f"warmup {time.perf_counter() - t0:.2f}s {_gpu_mb()}", flush=True)


def _unload() -> None:
    """释放模型与显存（只在 GPU 工作线程里调用）。"""
    global asr
    if asr is None:
        return
    asr = None
    _captured.clear()
    gc.collect()
    torch.cuda.empty_cache()
    _trim_heap()
    print(f"unloaded {_gpu_mb()}", flush=True)


def _run(samples: np.ndarray) -> dict:
    """识别一段 16k float32（-1~1）。fbank 需要 int16 量级的幅度，所以 ×32768。"""
    t0 = time.perf_counter()
    wav = (samples.astype(np.float32) * 32768.0)
    # 没有结束符的"跑飞"解码（近乎静音的段常见）会一直解到编码帧数那么多步（6 秒 ≈ 3.5 秒耗时）：按语音时长给个上限，
    # 正常说话每秒不超过 ~8 个 token
    asr.config.decode_max_len = max(12, int(len(samples) / 16000 * 10) + 8)
    try:
        res = asr.transcribe(["u"], [(16000, wav)])[0]
    finally:
        _clear_cross_cache()
    out = {"text": res.get("text", ""), "conf": res.get("confidence"), "dur_s": round(len(samples) / 16000, 3)}
    try:
        hyp = _captured["hyps"][0][0]
        ids = [int(i) for i in hyp["yseq"].cpu()]
        tc = [round(float(c), 3) for c in hyp["tok_conf"].cpu()] if "tok_conf" in hyp else []
        if tc and len(tc) == len(ids):
            out["tokens"] = [[asr.tokenizer.detokenize([i], "", False).lower(), c] for i, c in zip(ids, tc)]
            out["min_conf"] = min(tc)
    except Exception:  # noqa: BLE001  拿不到逐 token 置信度不影响识别结果
        pass
    out["ms"] = round((time.perf_counter() - t0) * 1000, 1)
    print(f"asr {out['dur_s']:.1f}s {out['ms']:.0f}ms conf={out['conf']}", flush=True)  # 每段一行：以后查慢请求用
    return out


def _asr_job(samples: np.ndarray) -> dict:
    _load()  # 卸载后首段自动加载（客户端一般会先 /load）
    return _run(samples)


gpu(_load)  # 启动时在 GPU 工作线程里加载并预热


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # 安静
        pass

    def _send(self, code: int, body: bytes = b"", ctype: str = "text/plain") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/health":
            self._send(200, b"ok" if asr is not None else b"unloaded")
        elif path == "/stats":
            self._send(200, json.dumps({"loaded": asr is not None, **_gpu_mb()}).encode(), "application/json")
        else:
            self._send(404)

    def do_POST(self):
        path = urlparse(self.path).path
        n = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(n) if n else b""
        if path == "/warm":  # 守护进程听见有人开口时调：显卡空闲时在 P8（210MHz），第一次识别会慢一倍多；先让它满载跑一阵把频率拉起来
            _start_heat()
            self._send(200, b"ok")
            return
        _stop_heat()  # 识别/加载/卸载前先停加热（它只为说话期间别让显卡睡着）
        if path in ("/unload", "/load"):
            gpu(_unload if path == "/unload" else _load)
            self._send(200, b"ok")
            return
        if path != "/asr":
            self._send(404)
            return
        if not body or len(body) % 4 or len(body) > MAX_BYTES:
            self._send(400, b"need float32 PCM")
            return
        samples = np.frombuffer(body, dtype="<f4")
        try:
            out = gpu(_asr_job, samples)
            self._send(200, json.dumps(out, ensure_ascii=False).encode("utf-8"), "application/json")
        except Exception as e:  # noqa: BLE001
            self._send(500, f"{type(e).__name__}: {e}".encode("utf-8"))


if __name__ == "__main__":
    print(f"serving on http://127.0.0.1:{PORT}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
