"""离线 ASR 对比台（诊断用，留在工作区、不提交）。

子命令：
  sync    把 tmp/segments 补拷进 tmp/asr_eval/pool（去重），并把 utterances 记录并入 pool/../utterances_all.jsonl（按 id 去重）
  pool    生成无标答清单（pool 里所有段，带上守护进程当时的 最终/流式 文字）
  gold    标答集定位：给定开始时间，把之后的段按时间顺序对齐到 15 句标准答案，生成带标答的清单
  run     对清单里的每段跑各候选，输出并排对比表（markdown + json），有标答时给 CER

候选写法 `名字[:变体]`，变体：
  proc      送 ASR 的那份（增益后 16k），缺省
  raw       设备原始（无增益）
  raw_norm  原始按峰值归一到 0.5（只看"增益不足"的影响，不含 tanh 压缩）
  proc_pad  proc 前后各补 0.4 秒零（模拟"段头段尾没留缓冲"的修复）；proc_pad800 即补 0.8 秒
  raw_pad   同上，用 raw_norm 作底

不改 src/、不碰守护进程；云端 key 只在内存里用，任何输出都不含它。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import time
import unicodedata
import wave
from datetime import datetime
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
EVAL = ROOT / "tmp" / "asr_eval"
POOL = EVAL / "pool"
SEG_SRC = ROOT / "tmp" / "segments"
UTT_LIVE = ROOT / "tmp" / "utterances.jsonl"
UTT_ALL = EVAL / "utterances_all.jsonl"
MODELS = ROOT / "models"
EVAL_MODELS = MODELS / "asr_eval"
SR = 16000

GOLD_SENTENCES = [
    "帮我把漫步者音箱的软件打开",
    "打开网易云音乐放一首周杰伦的晴天",
    "明天早上七点半叫我起床",
    "今天北京的天气怎么样要不要带伞",
    "把电脑音量调到百分之三十",
    "打开英雄联盟",
    "帮我在桌面建一个记事本写上买牛奶和鸡蛋",
    "我的 HALO 音箱声音太小了",
    "给我老婆发微信说我晚上不回家吃饭",
    "现在几点了",
    "帮我查一下二零二六年国庆节放几天假",
    "关掉所有正在运行的游戏",
    "把 D 盘下载文件夹里的压缩包解压一下",
    "刚才那个不对我说的是 TempoHub",
    "你听得清我说话吗",
]
HOTWORDS = ["管家", "漫步者", "HALO", "TempoHub", "网易云", "英雄联盟", "周杰伦"]


# ───────────────────────── 文本归一与 CER ─────────────────────────

_CN = "零一二三四五六七八九"


def _cn_int(n: int) -> str:
    """0~9999 按读数转中文（30→三十，15→十五，100→一百）。"""
    if n < 10:
        return _CN[n]
    if n < 20:
        return "十" + (_CN[n % 10] if n % 10 else "")
    if n < 100:
        return _CN[n // 10] + "十" + (_CN[n % 10] if n % 10 else "")
    if n < 1000:
        s = _CN[n // 100] + "百"
        r = n % 100
        if r == 0:
            return s
        return s + ("零" if r < 10 else "") + _cn_int(r)
    s = _CN[n // 1000] + "千"
    r = n % 1000
    if r == 0:
        return s
    return s + ("零" if r < 100 else "") + _cn_int(r)


def _num_sub(m: re.Match) -> str:
    s = m.group(0)
    if len(s) >= 4 or s.startswith("0"):
        return "".join(_CN[int(c)] for c in s)  # 年份/编号：逐位读
    return _cn_int(int(s))


def norm_text(s: str) -> str:
    """CER 用归一：NFKC、小写、百分号与阿拉伯数字转中文数词、「两」→「二」，去标点空格。"""
    s = unicodedata.normalize("NFKC", s).lower()
    s = re.sub(r"(\d+(?:\.\d+)?)\s*%", lambda m: "百分之" + m.group(1), s)
    s = re.sub(r"\d+", _num_sub, s)
    s = s.replace("两", "二").replace("〇", "零")
    return re.sub(r"[\W_]+", "", s)


def lev(a: str, b: str) -> int:
    d = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        p, d[0] = d[0], i
        for j, cb in enumerate(b, 1):
            p, d[j] = d[j], min(d[j] + 1, d[j - 1] + 1, p + (ca != cb))
    return d[-1]


def cer(ref: str, hyp: str) -> float:
    r, h = norm_text(ref), norm_text(hyp)
    if not r:
        return 0.0 if not h else 1.0
    return lev(r, h) / len(r)


# ───────────────────────── 音频读写 ─────────────────────────


def read_wav(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as w:
        sr, ch = w.getframerate(), w.getnchannels()
        x = np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) / 32768.0
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    return x, sr


def to16k(x: np.ndarray, sr: int) -> np.ndarray:
    if sr == SR:
        return x
    if sr % SR == 0:  # 整数倍：简单均值抽取（仅诊断用）
        k = sr // SR
        n = len(x) // k * k
        return x[:n].reshape(-1, k).mean(axis=1).astype(np.float32)
    return np.interp(np.linspace(0, len(x) - 1, int(len(x) * SR / sr)), np.arange(len(x)), x).astype(np.float32)


def load_variant(stem: str, variant: str) -> np.ndarray:
    """按变体取 16k float32。stem 可含 `|` 连接多段（一句被拆成多段时拼接，段间补 0.3 秒零）。"""
    parts = []
    for one in stem.split("|"):
        base = "raw" if variant.startswith("raw") else "proc"
        p = POOL / f"{one}_{base}.wav"
        if not p.exists():
            p = Path(one + f"_{base}.wav") if Path(one).is_absolute() else p
        x, sr = read_wav(p)
        x = to16k(x, sr)
        if variant.startswith(("raw_norm", "raw_pad")):
            pk = float(np.percentile(np.abs(x), 99.5)) if len(x) else 0.0
            x = x * (0.5 / pk) if pk > 1e-6 else x
        parts.append(x)
        parts.append(np.zeros(int(SR * 0.3), np.float32))
    x = np.concatenate(parts[:-1]) if len(parts) > 1 else parts[0]
    m = re.search(r"_pad(\d*)$", variant)
    if m:  # proc_pad / proc_pad800：前后各补若干毫秒零（缺省 400）
        z = np.zeros(int(SR * (int(m.group(1) or 400) / 1000)), np.float32)
        x = np.concatenate([z, x, z])
    return np.ascontiguousarray(x, dtype=np.float32)


# ───────────────────────── 池与记录 ─────────────────────────

_SEG_RE = re.compile(r"(\d{8})-(\d{6})-(\d{3})_(.*)_proc\.wav$")


def label_of(text: str) -> str:
    """与 voice.py `_dump_segment` 的文件名标签一致。"""
    return re.sub(r'[\\/:*?"<>|\s]', "", text)[:10] or "空"


def list_pool() -> list[dict]:
    out = []
    for p in sorted(POOL.glob("*_proc.wav")):
        m = _SEG_RE.match(p.name)
        if not m:
            continue
        t = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S").timestamp() + int(m.group(3)) / 1000
        out.append({"stem": p.name[: -len("_proc.wav")], "t": t, "label": m.group(4)})
    return out


def load_utts() -> list[dict]:
    rows: dict[str, dict] = {}
    for f in (UTT_ALL, UTT_LIVE):
        if f.exists():
            for line in f.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    r = json.loads(line)
                    rows[r["id"]] = r  # 后者（live）覆盖前者
    return sorted((r for r in rows.values() if r.get("source") == "mic"), key=lambda r: r["t0"])


def attach_segments(utts: list[dict], segs: list[dict]) -> None:
    """utterance ↔ 池里的段：文件名时间在 t0 前 0~3 秒，且标签一致。"""
    used: set[str] = set()
    for r in utts:
        lab = label_of(r["text"])
        best = None
        for s in segs:
            d = r["t0"] - s["t"]
            if -0.05 <= d <= 3.0 and s["label"] == lab and s["stem"] not in used and (best is None or d < best[0]):
                best = (d, s)
        r["stem"] = best[1]["stem"] if best else None
        if best:
            used.add(best[1]["stem"])
        tm = r.get("timings") or {}
        r["detect"] = r["t0"] - (tm.get("asr_ms", 0) + tm.get("speaker_ms", 0)) / 1000
        m = re.match(r"([\d.]+)", (r.get("stages", {}).get("vad", {}) or {}).get("note", "") or "")
        r["dur"] = float(m.group(1)) if m else None


def cmd_sync(_: argparse.Namespace) -> None:
    POOL.mkdir(parents=True, exist_ok=True)
    n = 0
    for f in SEG_SRC.glob("*.wav"):
        d = POOL / f.name
        if not d.exists():
            shutil.copy2(f, d)
            n += 1
    old = {}
    if UTT_ALL.exists():
        for line in UTT_ALL.read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = json.loads(line)
                old[r["id"]] = line
    k = 0
    for line in UTT_LIVE.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rid = json.loads(line)["id"]
            if rid not in old or old[rid] != line:
                k += rid not in old
            old[rid] = line
    UTT_ALL.write_text("\n".join(old.values()) + "\n", encoding="utf-8")
    print(f"pool +{n} 个文件（共 {len(list(POOL.glob('*.wav')))}），utterances_all +{k} 条（共 {len(old)}）")


def cmd_pool(a: argparse.Namespace) -> None:
    segs = list_pool()
    utts = load_utts()
    attach_segments(utts, segs)
    by_stem = {r["stem"]: r for r in utts if r["stem"]}
    out = Path(a.out)
    with out.open("w", encoding="utf-8") as f:
        for s in segs:
            r = by_stem.get(s["stem"])
            f.write(json.dumps({"stem": s["stem"], "ref": None, "live_final": r["text"] if r else "",
                                "live_stream": (r or {}).get("stream_text", ""), "t": s["t"]}, ensure_ascii=False) + "\n")
    print(f"清单 {len(segs)} 段 -> {out}")


# ───────────────────────── 标答集定位 ─────────────────────────


def _sim(a: str, b: str) -> float:
    import difflib

    a, b = norm_text(a), norm_text(b)
    return difflib.SequenceMatcher(None, a, b).ratio() if a and b else 0.0


def cmd_gold(a: argparse.Namespace) -> None:
    """DP 对齐：段按时间序，一句 = 连续 1~3 段（相邻间隔 <3 秒）的拼接，可跳过噪声段、可跳过漏掉的句子。"""
    t_start = datetime.strptime(a.start, "%Y-%m-%d %H:%M:%S").timestamp()
    utts = load_utts()
    attach_segments(utts, list_pool())
    rows = [r for r in utts if r["t0"] >= t_start and r["stem"]]
    if a.end:
        t_end = datetime.strptime(a.end, "%Y-%m-%d %H:%M:%S").timestamp()
        rows = [r for r in rows if r["t0"] <= t_end]
    n, m = len(rows), len(GOLD_SENTENCES)
    # 每段取"最终"和"流式"里更像的那条做相似度，避免某一边认错就对不上
    def txt(rs: list[dict]) -> list[str]:
        return ["".join(r["text"] for r in rs), "".join((r.get("stream_text") or r["text"]) for r in rs)]

    NEG = -1e9
    dp = [[NEG] * (m + 1) for _ in range(n + 1)]
    bp: dict[tuple[int, int], tuple] = {}
    dp[0][0] = 0.0
    for i in range(n + 1):
        for j in range(m + 1):
            if dp[i][j] <= NEG / 2:
                continue
            cur = dp[i][j]
            if i < n and cur > dp[i + 1][j]:  # 跳过这一段（噪声/别的话）
                dp[i + 1][j] = cur
                bp[(i + 1, j)] = ("skipseg", i, j)
            if j < m and cur > dp[i][j + 1]:  # 跳过这一句（没被收到）
                dp[i][j + 1] = cur
                bp[(i, j + 1)] = ("skipsent", i, j)
            if i < n and j < m:
                for k in range(1, 4):
                    if i + k > n:
                        break
                    grp = rows[i : i + k]
                    if k > 1 and any(grp[q + 1]["detect"] - grp[q]["detect"] - (grp[q + 1]["dur"] or 0) > 3.0 for q in range(k - 1)):
                        break
                    sc = max(_sim(t, GOLD_SENTENCES[j]) for t in txt(grp))
                    if sc >= 0.4 and cur + sc > dp[i + k][j + 1]:
                        dp[i + k][j + 1] = cur + sc
                        bp[(i + k, j + 1)] = ("assign", i, j, k, sc)
    i, j = n, m
    assign: dict[int, tuple[list[dict], float]] = {}
    while (i, j) != (0, 0):
        step = bp[(i, j)]
        if step[0] == "assign":
            _, pi, pj, k, sc = step
            assign[pj] = (rows[pi : pi + k], sc)
            i, j = pi, pj
        else:
            i, j = step[1], step[2]
    out = Path(a.out)
    lines, report = [], []
    for jj, ref in enumerate(GOLD_SENTENCES):
        if jj in assign:
            grp, sc = assign[jj]
            stems = "|".join(r["stem"] for r in grp)
            lines.append(json.dumps({"stem": stems, "ref": ref, "idx": jj + 1,
                                     "live_final": "".join(r["text"] for r in grp),
                                     "live_stream": "".join(r.get("stream_text", "") for r in grp)}, ensure_ascii=False))
            report.append(f"{jj + 1:>2} 命中{len(grp)}段 相似度{sc:.2f} 实录最终「{''.join(r['text'] for r in grp)}」 流式「{''.join(r.get('stream_text', '') for r in grp)}」")
        else:
            report.append(f"{jj + 1:>2} 未找到  标答「{ref}」")
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(report))
    print(f"带标答清单 {len(lines)}/{m} 句 -> {out}")


# ───────────────────────── 候选识别器 ─────────────────────────


def _lower_priority() -> None:
    """识别在后台降到低优先级，别抢守护进程的 CPU。"""
    if sys.platform == "win32":
        import ctypes

        ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x00004000)


def _find(d: Path, *pats: str) -> str:
    for p in pats:
        hit = sorted(d.glob(p))
        if hit:
            return str(hit[0])
    raise FileNotFoundError(f"{d} 里找不到 {pats}")


class Cand:
    name = ""
    cloud = False

    def decode(self, x: np.ndarray) -> str:
        raise NotImplementedError


class SenseVoice(Cand):
    def __init__(self, d: Path, fp32: bool, threads: int):
        import sherpa_onnx

        m = d / ("model.onnx" if fp32 else "model.int8.onnx")
        self.rec = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=str(m), tokens=str(d / "tokens.txt"), num_threads=threads, provider="cpu", language="zh", use_itn=True)

    def decode(self, x):
        s = self.rec.create_stream()
        s.accept_waveform(SR, x)
        self.rec.decode_stream(s)
        return s.result.text.strip()


class OfflineGeneric(Cand):
    """Paraformer / FireRed 等 sherpa 离线模型：构造由工厂函数给，解码流程相同。"""

    def __init__(self, rec):
        self.rec = rec

    def decode(self, x):
        s = self.rec.create_stream()
        s.accept_waveform(SR, x)
        self.rec.decode_stream(s)
        return s.result.text.strip()


class StreamZip(Cand):
    """现在的流式 zipformer；hot=True 时改 modified_beam_search + 热词（该模型是汉字建模，只能给中文热词）。"""

    def __init__(self, threads: int, hot: bool):
        import sherpa_onnx

        d = MODELS / "sherpa-onnx-streaming-zipformer-zh-int8-2025-06-30"
        kw = dict(tokens=str(d / "tokens.txt"), encoder=str(d / "encoder.int8.onnx"), decoder=str(d / "decoder.onnx"),
                  joiner=str(d / "joiner.int8.onnx"), num_threads=threads, provider="cpu", sample_rate=16000, feature_dim=80,
                  enable_endpoint_detection=False)
        if hot:
            hw = EVAL / "hotwords_zh.txt"
            hw.write_text("\n".join(" ".join(w) + " :2.0" for w in HOTWORDS if re.fullmatch(r"[\u4e00-\u9fff]+", w)) + "\n", encoding="utf-8")
            kw.update(decoding_method="modified_beam_search", hotwords_file=str(hw), hotwords_score=2.0, max_active_paths=4)
        else:
            kw.update(decoding_method="greedy_search")
        self.rec = sherpa_onnx.OnlineRecognizer.from_transducer(**kw)

    def decode(self, x):
        s = self.rec.create_stream()
        s.accept_waveform(SR, x)
        # 守护进程里流式识别器会一直收到音频直到 VAD 翻 false（段后还有 ~0.8s 真实尾静音），再补 0.3s 零；
        # 离线只有 seg.samples，所以补 1.0s 零来近似，否则流式会无故丢尾字（不代表线上行为）
        s.accept_waveform(SR, np.zeros(int(SR * 1.0), np.float32))
        s.input_finished()
        while self.rec.is_ready(s):
            self.rec.decode_stream(s)
        return self.rec.get_result(s).strip()


class FireRedGPU(Cand):
    """WSL 里的 FireRedASR2-AED GPU 服务（scripts/asr_server.py）：HTTP 往返，耗时含传输。fr2gpu:proc_pad = 与线上一致（补 0.4s）。"""

    def __init__(self, url: str = "http://127.0.0.1:18798") -> None:
        import httpx

        self.url, self.c, self.last = url, httpx.Client(timeout=10, trust_env=False), {}

    def decode(self, x):
        r = self.c.post(self.url + "/asr", content=np.ascontiguousarray(x, dtype="<f4").tobytes())
        r.raise_for_status()
        self.last = r.json()
        return self.last.get("text", "")


class SiliconFlow(Cand):
    cloud = True
    URL = "https://api.siliconflow.cn/v1/audio/transcriptions"

    def __init__(self, model: str):
        self.model = model
        self.cache_f = EVAL / "sf_cache.json"
        self.cache = json.loads(self.cache_f.read_text(encoding="utf-8")) if self.cache_f.exists() else {}
        self.key = self._load_key()
        self.audio_sec = 0.0

    @staticmethod
    def _load_key() -> str:
        """key 来源：环境变量 → ~/.claude/settings.json（与 front.py 一致）→ ~/.openclaw/openclaw.json。只在内存里用。"""
        for e in ("SILICONFLOW_API_KEY", "GUANJIA_SF_KEY"):
            if os.environ.get(e):
                return os.environ[e]
        home = Path.home()
        try:
            return json.loads((home / ".claude" / "settings.json").read_text(encoding="utf-8-sig"))["env"]["ANTHROPIC_AUTH_TOKEN"]
        except Exception:  # noqa: BLE001
            pass
        cfg = json.loads((home / ".openclaw" / "openclaw.json").read_text(encoding="utf-8-sig"))

        def walk(o, path=""):
            if isinstance(o, dict):
                for k, v in o.items():
                    if "siliconflow" in (path + "/" + k).lower() and isinstance(v, str) and k.lower() in ("apikey", "api_key", "key", "token"):
                        return v
                    r = walk(v, path + "/" + k)
                    if r:
                        return r
            elif isinstance(o, list):
                for i, v in enumerate(o):
                    r = walk(v, path + f"/{i}")
                    if r:
                        return r

        k = walk(cfg)
        if not k:
            raise RuntimeError("找不到硅基 key")
        return k

    def decode(self, x):
        import io

        import httpx

        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SR)
            w.writeframes((np.clip(x, -1, 1) * 32767).astype(np.int16).tobytes())
        data = buf.getvalue()
        ck = f"{self.model}:{hashlib.sha1(data).hexdigest()}"
        if ck in self.cache:
            return self.cache[ck]
        self.audio_sec += len(x) / SR
        for attempt in range(5):  # 掉线每分钟重试，最多 5 次
            try:
                r = httpx.post(self.URL, headers={"Authorization": f"Bearer {self.key}"},
                               files={"file": ("a.wav", data, "audio/wav")}, data={"model": self.model}, timeout=60)
                if r.status_code == 200:
                    text = (r.json().get("text") or "").strip()
                    self.cache[ck] = text
                    self.cache_f.write_text(json.dumps(self.cache, ensure_ascii=False), encoding="utf-8")
                    return text
                if r.status_code in (400, 401, 403, 404):
                    return f"<HTTP {r.status_code}>"
            except httpx.HTTPError:
                pass
            time.sleep(60)
        return "<云端失败>"


def build(name: str, threads: int) -> Cand:
    import sherpa_onnx

    def d(prefix: str) -> Path:
        hit = sorted(EVAL_MODELS.glob(prefix + "*"))
        hit = [h for h in hit if h.is_dir()]
        if not hit:
            raise FileNotFoundError(f"models/asr_eval 下没有 {prefix}*（还没下载完？）")
        return hit[0]

    if name == "sv_int8":  # 基准：与 asr_final.py 完全相同的配置
        return SenseVoice(MODELS / "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2025-09-09", False, threads)
    if name == "fr2gpu":
        return FireRedGPU()
    if name == "sv_int8_2024":  # 旧版 SenseVoice（硅基云端的 SenseVoiceSmall 与它同代），验证"2025-09 新版是否更差"
        return SenseVoice(d("sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024"), False, threads)
    if name == "sv_fp32":
        return SenseVoice(d("sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2025-09-09"), True, threads)
    if name == "zip_stream":
        return StreamZip(threads, False)
    if name == "zip_stream_hot":
        return StreamZip(threads, True)
    if name == "para_int8":  # sherpa 的 Paraformer 不支持热词（热词只给 transducer），所以这里只是无热词基线
        p = d("sherpa-onnx-paraformer-zh")
        return OfflineGeneric(sherpa_onnx.OfflineRecognizer.from_paraformer(
            paraformer=_find(p, "model.int8.onnx", "model.onnx"), tokens=str(p / "tokens.txt"), num_threads=threads, provider="cpu"))
    if name == "fire2":
        p = d("sherpa-onnx-fire-red-asr2")
        return OfflineGeneric(sherpa_onnx.OfflineRecognizer.from_fire_red_asr(
            encoder=_find(p, "encoder*.onnx"), decoder=_find(p, "decoder*.onnx"), tokens=str(p / "tokens.txt"),
            num_threads=threads, provider="cpu"))
    if name in ("qwen3", "qwen3_hot"):
        p = d("sherpa-onnx-qwen3-asr")
        return OfflineGeneric(sherpa_onnx.OfflineRecognizer.from_qwen3_asr(
            conv_frontend=_find(p, "conv_frontend*.onnx"), encoder=_find(p, "encoder*.onnx"), decoder=_find(p, "decoder*.onnx"),
            tokenizer=str(next(p.glob("tokenizer*"))), num_threads=threads, provider="cpu",
            hotwords=",".join(HOTWORDS) if name == "qwen3_hot" else ""))
    if name.startswith("sf_"):  # sf_sensevoice / sf_qwen3 / sf_xingchen / sf_xingchen_ultra
        m = {"sf_sensevoice": "FunAudioLLM/SenseVoiceSmall", "sf_qwen3": "Qwen/Qwen3-ASR-1.7B",
             "sf_telespeech": "TeleAI/TeleSpeechASR", "sf_xingchen": "XingChenAGI/XingChenASR-V3.2", "sf_xingchen_ultra": "XingChenAGI/XingChenASR-V3.2-Ultra"}[name]
        return SiliconFlow(m)
    raise SystemExit(f"未知候选 {name}")


# ───────────────────────── 运行与对比表 ─────────────────────────


def cmd_run(a: argparse.Namespace) -> None:
    _lower_priority()
    from guanjia.ort_preload import preload_onnxruntime

    preload_onnxruntime()
    items = [json.loads(l) for l in Path(a.manifest).read_text(encoding="utf-8").splitlines() if l.strip()]
    if a.limit:
        items = items[: a.limit]
    has_ref = any(it.get("ref") for it in items)
    specs = [c.strip() for c in a.cands.split(",") if c.strip()]
    results: dict[str, list[dict]] = {}  # spec -> 每段 {text, ms}
    summary: dict[str, dict] = {}
    cache_cands: dict[str, Cand] = {}
    for spec in specs:
        name, _, variant = spec.partition(":")
        variant = variant or "proc"
        try:
            t_load = time.perf_counter()
            if name not in cache_cands:
                cache_cands[name] = build(name, a.threads)
            cand = cache_cands[name]
            load_s = time.perf_counter() - t_load
        except Exception as e:  # noqa: BLE001
            print(f"[{spec}] 加载失败：{type(e).__name__}: {str(e)[:150]}", flush=True)
            summary[spec] = {"error": f"{type(e).__name__}: {str(e)[:150]}"}
            continue
        if not cand.cloud:  # 预热：第一次解码含图编译，不计入耗时
            cand.decode(load_variant(items[0]["stem"], variant))
        outs = []
        for it in items:
            x = load_variant(it["stem"], variant)
            t0 = time.perf_counter()
            try:
                txt = cand.decode(x)
            except Exception as e:  # noqa: BLE001
                txt = f"<ERR {type(e).__name__}>"
            ms = (time.perf_counter() - t0) * 1000
            outs.append({"text": txt, "ms": ms, "sec": len(x) / SR, "cer": cer(it["ref"], txt) if it.get("ref") else None})
        results[spec] = outs
        sm = {"load_s": round(load_s, 1), "mean_ms": round(float(np.mean([o["ms"] for o in outs])), 0),
              "rtf": round(sum(o["ms"] for o in outs) / 1000 / max(1e-9, sum(o["sec"] for o in outs)), 3)}
        if has_ref:
            refd = [o for o, it in zip(outs, items) if it.get("ref")]
            # 总字错率（按字数加权）+ 平均 CER
            tot_err = sum(lev(norm_text(it["ref"]), norm_text(o["text"])) for o, it in zip(outs, items) if it.get("ref"))
            tot_len = sum(len(norm_text(it["ref"])) for it in items if it.get("ref"))
            sm["CER"] = round(tot_err / tot_len, 4)
            sm["mean_CER"] = round(float(np.mean([o["cer"] for o in refd])), 4)
            sm["n"] = len(refd)
            ex = {int(v) for v in a.exclude_idx.split(",") if v.strip()}
            if ex:
                keep = [(o, it) for o, it in zip(outs, items) if it.get("ref") and it.get("idx") not in ex]
                sm["CER_clean"] = round(sum(lev(norm_text(it["ref"]), norm_text(o["text"])) for o, it in keep)
                                        / sum(len(norm_text(it["ref"])) for _, it in keep), 4)
        summary[spec] = sm
        print(f"[{spec}] {sm}", flush=True)
        if isinstance(cand, SiliconFlow):
            sm["cloud_audio_sec"] = round(cand.audio_sec, 1)
    out = Path(a.out)
    out.with_suffix(".json").write_text(json.dumps({"items": items, "results": results, "summary": summary}, ensure_ascii=False, indent=1), encoding="utf-8")
    cols = list(results)
    md = ["# ASR 并排对比（每段一行）", ""]
    md.append("## 汇总")
    md.append("| 候选 | " + " | ".join(["CER", "CER(剔除偏离句)", "平均耗时ms", "RTF", "加载s"] if has_ref else ["平均耗时ms", "RTF", "加载s"]) + " |")
    md.append("|---|" + "---|" * (5 if has_ref else 3))
    for spec, sm in summary.items():
        if "error" in sm:
            md.append(f"| {spec} | 加载失败：{sm['error']} |")
        else:
            cells = ([f"{sm['CER']:.1%}", f"{sm['CER_clean']:.1%}" if "CER_clean" in sm else "-"] if has_ref else []) + [f"{sm['mean_ms']:.0f}", f"{sm['rtf']}", f"{sm['load_s']}"]
            md.append(f"| {spec} | " + " | ".join(cells) + " |")
    md += ["", "## 逐段", "| # | 秒 | 标答/实录最终 | 实录流式 | " + " | ".join(cols) + " |", "|---|---|---|---|" + "---|" * len(cols)]
    for i, it in enumerate(items):
        ref = it.get("ref")
        head = f"**{ref}**" if ref else (it.get("live_final") or "（空）")
        cells = []
        for c in cols:
            o = results[c][i]
            t = o["text"].replace("|", "/")
            cells.append(f"{t}" + (f" ({o['cer']:.0%})" if o["cer"] is not None else ""))
        sec = next(iter(results.values()))[i]["sec"] if results else 0
        md.append(f"| {it.get('idx', i + 1)} | {sec:.1f} | {head} | {it.get('live_stream') or ''} | " + " | ".join(cells) + " |")
    out.write_text("\n".join(md) + "\n", encoding="utf-8")
    print(f"对比表 -> {out}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("sync")
    p = sub.add_parser("pool")
    p.add_argument("--out", default=str(EVAL / "manifest_pool.jsonl"))
    g = sub.add_parser("gold")
    g.add_argument("--start", required=True, help="开始念第 1 句之前的时间，如 '2026-10-11 13:50:00'")
    g.add_argument("--end", default="")
    g.add_argument("--out", default=str(EVAL / "manifest_gold.jsonl"))
    r = sub.add_parser("run")
    r.add_argument("--manifest", required=True)
    r.add_argument("--cands", required=True)
    r.add_argument("--out", default=str(EVAL / "results" / f"bench_{datetime.now():%Y%m%d-%H%M%S}.md"))
    r.add_argument("--threads", type=int, default=2)
    r.add_argument("--limit", type=int, default=0)
    r.add_argument("--exclude-idx", default="", help="CER_clean 里排除的句号（主人实际说的和标答不一致的句子），如 1,7,15")
    a = ap.parse_args()
    if a.cmd == "run":
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    {"sync": cmd_sync, "pool": cmd_pool, "gold": cmd_gold, "run": cmd_run}[a.cmd](a)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
