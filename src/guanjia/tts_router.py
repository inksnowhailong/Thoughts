"""TTS 路由层：智谱 GLM-TTS 为主（云端流式，首包 300-500ms），本地 IndexTTS 兜底。

为什么这么设计：
- IndexTTS 首包 1200-2900ms，对话里"问完到出声"的空窗太长；GLM-TTS 流式 PCM 首包快得多。
- 云端不能是唯一依赖：断网 / 欠费 / 限流时对话必须还能出声 → 本地 IndexTTS 兜底。
- 兜底只发生在"一个字都还没出声"时：GLM 出过声后断流绝不重头再念一遍
  （半句重复比静默更糟），直接抛异常交上层记 unspoken。
- 采样率统一成 16kHz float32 逐块吐出，消费方（播放队列）不需要知道走的哪条路。

接口语义对齐 tts_client.TTSClient（synth/alive/abort/control 同名同义），
voice.py 里把 TTSClient 换成 TTSRouter 即可；老的 `pcm, sr = synth(...)` 用法走 synth_full。
"""

from __future__ import annotations

import base64
import io
import json
import logging
import os
import time
import wave
from collections.abc import Iterator
from pathlib import Path

import httpx
import numpy as np

log = logging.getLogger('guanjia.tts_router')

# ---- 主路：GLM-TTS（智谱云端，OpenAI /audio/speech 兼容风格）----
GLM_URL = os.environ.get('GUANJIA_GLM_TTS_URL', 'https://open.bigmodel.cn/api/paas/v4/audio/speech')
GLM_MODEL = 'glm-tts'
GLM_VOICE = os.environ.get('GUANJIA_GLM_TTS_VOICE', 'tongtong')
GLM_SR = 24000        # GLM-TTS pcm 裸流采样率
GLM_DTYPE = '<i2'     # GLM-TTS pcm 字节序：16bit 小端
TARGET_SR = 16000     # 语音进程吃 16kHz
FIRST_BYTE_SEC = 3.0  # 首包预算：3 秒内没出声就切本地（httpx 读超时兜底）
KEY_PATH = Path.home() / '.openclaw' / 'secrets' / 'zhipu_key_file' / 'value'

# ---- 兜底：IndexTTS（WSL 里的本地服务，Windows 18790 / WSL2 18791）----
DEFAULT_URL = os.environ.get('GUANJIA_TTS_URL', 'http://127.0.0.1:18791/tts')


def _resample_stream(chunks: Iterator[np.ndarray], sr_in: int, sr_out: int) -> Iterator[np.ndarray]:
    """流式线性插值重采样：逐块喂入、逐块产出，跨块无缝。

    朴素做法（每块独立 linspace 插值）会在块边界丢掉左邻样本，产生台阶爆音；
    这里保留上一块最后一个样本做插值锚点，输出点 k 的输入坐标用绝对值 k*sr_in/sr_out 计算，
    与块的切分方式完全无关，所以块边界上没有缝。
    """
    if sr_in == sr_out:
        yield from chunks
        return
    step = sr_in / sr_out               # 每产出一个样本，输入坐标前进的步长
    last = None                         # 上一块末样本（跨块插值的左邻）
    end = -1                            # 已见输入样本的绝对末下标
    k = 0                               # 已产出的输出样本总数
    for chunk in chunks:
        if not len(chunk):
            continue
        lo = end if last is not None else 0                # 本窗口在输入流上的绝对起点
        win = np.concatenate(([last], chunk)) if last is not None else chunk
        hi = lo + len(win) - 1                             # 本窗口绝对末端
        # 本窗口能产出的输出点：p = k*step < hi（左右邻样本都已在窗口内）
        n_out = max(0, int(np.ceil(hi / step - 1e-9)) - k)
        if n_out:
            pos = (k + np.arange(n_out, dtype=np.float64)) * step
            i0 = pos.astype(np.int64)
            frac = pos - i0
            idx = i0 - lo
            yield (win[idx] * (1.0 - frac) + win[idx + 1] * frac).astype(np.float32)
            k += n_out
        last = float(chunk[-1])
        end = hi
    # 尾部：输出点落在最后一个样本之后的，用末样本补齐（不足 1ms，不影响听感）
    if last is not None:
        n_tail = max(0, int(np.ceil((end + 1) / step - 1e-9)) - k)
        if n_tail:
            yield np.full(n_tail, last, dtype=np.float32)


def _sse_to_pcm(byte_chunks: Iterator[bytes]) -> Iterator[bytes]:
    """GLM-TTS SSE 流 → 裸 PCM 字节流。

    响应是 text/event-stream，每行 data: {"choices":[{"delta":{"content":"<base64 PCM>"}}]}——
    content 是 base64 编码的 PCM 块。之前直接把 SSE JSON 文本当 int16 PCM 解码导致爆炸噪音。
    """
    buf = b''
    for b in byte_chunks:
        buf += b
        while b'\n' in buf:
            line, buf = buf.split(b'\n', 1)
            line = line.strip()
            if not line.startswith(b'data:'):
                continue
            payload = line[5:].strip()
            if payload == b'[DONE]':
                return
            try:
                obj = json.loads(payload.decode('utf-8'))
                content = obj['choices'][0].get('delta', {}).get('content', '')
                if content:
                    yield base64.b64decode(content)
            except (json.JSONDecodeError, KeyError, IndexError, ValueError):
                continue  # 非音频事件行跳过


def _pcm_to_float32(byte_chunks: Iterator[bytes]) -> Iterator[np.ndarray]:
    """裸 PCM 字节流 → float32 样本流。

    int16 帧可能被网络包从中间截断，留 1 字节尾巴拼到下一块再切，保证帧对齐。
    """
    carry = b''
    for b in byte_chunks:
        b = carry + b
        n = len(b) & ~1                 # 截成偶数字节 = 整数个 int16 帧
        carry = b[n:]
        if n:
            yield np.frombuffer(b, dtype=GLM_DTYPE, count=n // 2).astype(np.float32) / 32768.0


class TTSRouter:
    """GLM-TTS 主路 + IndexTTS 兜底的路由器，接口与 tts_client.TTSClient 对齐。

    合成请求可能来自 TurnRunner 线程和 ack 线程，_resp 单槽位与旧实现同样只保证
    "abort 能关掉当前正在跑的那一个"；主流程一句一句串行，实际够用。
    """

    def __init__(self, url: str = DEFAULT_URL, timeout: float = 30.0) -> None:
        self.url = url
        # 主路超时即首包预算：读超时 3 秒没等到数据就抛异常 → 兜底；
        # 流起来之后同一超时兼做断流看门狗（正常流块间不会空 3 秒）
        self._glm = httpx.Client(timeout=httpx.Timeout(FIRST_BYTE_SEC))
        self._index = httpx.Client(timeout=timeout, trust_env=False)  # 本机服务，不走系统代理
        self._key = self._load_key()
        self._resp: httpx.Response | None = None  # 进行中的请求（主路流或兜底），供 abort() 从别的线程关闭
        self._aborted = False                     # 本句被 abort：synth 里据此不再发兜底请求

    def _load_key(self) -> str:
        """读智谱 API key；缺失只在这里警告一次，主路自动退化为纯本地。

        key 存在 ~/.openclaw/openclaw.json 的 models.providers.zhipu.apiKey
        （与 camera-tool.py 同源），SecretRef 的 secrets 目录远程机器上不存在。
        """
        try:
            import json as _json
            oc = _json.loads(
                (Path.home() / '.openclaw' / 'openclaw.json').read_text(encoding='utf-8-sig'))
            key = oc['models']['providers']['zhipu'].get('apiKey', '')
            if isinstance(key, dict):
                # SecretRef 尝试 secrets 文件（可能不存在）
                kf = KEY_PATH
                key = kf.read_text(encoding='utf-8').strip() if kf.exists() else ''
            return str(key).strip()
        except Exception as e:
            log.warning('tts glm key 读取失败（%s），主路关闭，只用本地 IndexTTS', e)
            return ''

    # ---- 对外接口（与 TTSClient 同名同义）----

    def synth(self, text: str, **gen_kwargs) -> Iterator[np.ndarray]:
        """合成一句话：逐块 yield 16kHz float32 单声道 PCM。

        主路 GLM-TTS 流式；首包超时 / 请求失败且未出过声 → 自动切本地 IndexTTS。
        gen_kwargs（num_beams / duration_factor 等）只透传给 IndexTTS，GLM 不认这些参数。
        """
        self._aborted = False
        t0 = time.perf_counter()
        head = text[:30]
        if self._key:
            spoken = 0      # 已出声样本数：决定失败后能不能兜底
            first_ms = None
            try:
                for chunk in self._glm_stream(text):
                    if first_ms is None:
                        first_ms = (time.perf_counter() - t0) * 1000
                        log.info('tts glm first=%dms text=%r', int(first_ms), head)
                    spoken += len(chunk)
                    yield chunk
            except Exception as e:  # noqa: BLE001
                if self._aborted:
                    log.info('tts glm aborted text=%r', head)
                    raise
                if spoken:
                    log.warning('tts glm 断流（已出声 %.2fs，不兜底防半句重复）: %s text=%r',
                                spoken / TARGET_SR, e, head)
                    raise
                log.warning('tts glm 失败（%.0fms，切本地）: %s text=%r',
                            (time.perf_counter() - t0) * 1000, e, head)
            else:
                if spoken:
                    log.info('tts glm ok: first=%dms total=%.2fs audio=%.2fs text=%r',
                             int(first_ms or 0), time.perf_counter() - t0, spoken / TARGET_SR, head)
                    return
                log.warning('tts glm 200 但零样本，切本地 text=%r', head)
        yield from self._index_stream(text, t0, **gen_kwargs)

    def synth_full(self, text: str, **gen_kwargs) -> tuple[np.ndarray, int]:
        """兼容桥：把流式块拼成整段，返回 (pcm, 16000)。

        voice.py 里老语义的两处 `pcm, sr = synth(text)` 改成调用这个即可，行为与
        旧 TTSClient 完全一致（只是拿到了主路提速）；真正的流式消费走 synth()。
        """
        parts = list(self.synth(text, **gen_kwargs))
        pcm = np.concatenate(parts) if parts else np.empty(0, dtype=np.float32)
        return pcm, TARGET_SR

    def alive(self) -> bool:
        """健康检查：主路有 key 就算活（云端没有免费探活端点，不值得每次轮询都打一枪）；
        没 key 时健康与否取决于本地 IndexTTS。"""
        if self._key:
            return True
        try:
            return self._index.get(self.url.rsplit('/', 1)[0] + '/health', timeout=2.0).status_code == 200
        except httpx.HTTPError:
            return False

    def abort(self) -> None:
        """从其他线程中断当前合成：直接关掉进行中的请求（主路流或兜底都适用）。

        关闭后 synth 的读循环立刻抛异常；_aborted 标记让 synth 不再发兜底请求。
        """
        self._aborted = True
        r = self._resp
        if r is not None:
            r.close()

    def control(self, action: str, timeout: float = 120.0) -> bool:
        """游戏态显存管理：/unload /load 原样转发给本地 IndexTTS（云端 GLM 无此概念）。

        可选能力，失败只返回 False 不抛异常——与旧 TTSClient.control 一致。
        """
        try:
            return self._index.post(self.url.rsplit('/', 1)[0] + '/' + action, timeout=timeout).status_code == 200
        except httpx.HTTPError:
            return False

    # ---- 两条路 ----

    def _glm_stream(self, text: str) -> Iterator[np.ndarray]:
        """主路：POST GLM-TTS，流式读 24kHz 16bit PCM，重采样成 16kHz float32 逐块 yield。

        读超时（= 首包预算 3 秒）由 self._glm 客户端统一兜底，超时抛异常由 synth 切本地。
        不带 chunk_size 的 iter_bytes 按网络包原样切，首包不被攒包推迟。
        """
        body = {'model': GLM_MODEL, 'input': text, 'voice': GLM_VOICE,
                'response_format': 'pcm', 'stream': True}
        headers = {'Authorization': 'Bearer ' + self._key, 'Content-Type': 'application/json'}
        with self._glm.stream('POST', GLM_URL, json=body, headers=headers) as r:
            self._resp = r
            try:
                if r.status_code != 200:
                    detail = r.read()[:200]
                    raise RuntimeError('glm http %d: %s' % (r.status_code, detail.decode('utf-8', 'replace')))
                yield from _resample_stream(_pcm_to_float32(_sse_to_pcm(r.iter_bytes())), GLM_SR, TARGET_SR)
            finally:
                self._resp = None

    def _index_stream(self, text: str, t0: float, **gen_kwargs) -> Iterator[np.ndarray]:
        """兜底路：本地 IndexTTS 整句 wav → 抽 PCM → 统一到 16kHz 逐块 yield。

        t0 是本次 synth 的起点（含主路尝试耗时），日志里的耗时因此是端到端的。
        """
        t = time.perf_counter()
        with self._index.stream('POST', self.url, json={'text': text, **gen_kwargs}) as r:
            self._resp = r
            try:
                r.raise_for_status()
                data = r.read()
            finally:
                self._resp = None
        with wave.open(io.BytesIO(data), 'rb') as w:
            sr, n_ch = w.getframerate(), w.getnchannels()
            pcm = np.frombuffer(w.readframes(w.getnframes()), dtype='<i2').astype(np.float32) / 32768.0
        if n_ch > 1:
            pcm = pcm.reshape(-1, n_ch).mean(axis=1)
        log.info('tts index ok: total=%.2fs（本地 %.2fs） sr=%d audio=%.2fs text=%r',
                 time.perf_counter() - t0, time.perf_counter() - t, sr, len(pcm) / sr, text[:30])
        yield from _resample_stream(iter([pcm]), sr, TARGET_SR)


if __name__ == '__main__':
    # 冒烟：python -m guanjia.tts_router [要合成的文本]——打印首包 / 总耗时 / 时长
    import logging as _logging
    import sys

    _logging.basicConfig(level=_logging.INFO, format='%(levelname)s %(name)s %(message)s')
    c = TTSRouter()
    print('alive:', c.alive())
    text = ' '.join(sys.argv[1:]) or '先生你回来啦？饭在桌上焖着，别忘了趁热吃。'
    t0 = time.perf_counter()
    total, first = 0, None
    for part in c.synth(text):
        if first is None:
            first = (time.perf_counter() - t0) * 1000
        total += len(part)
    print(f'{total / TARGET_SR:.2f}s audio @ {TARGET_SR}Hz, first chunk {first or -1:.0f}ms, '
          f'total {time.perf_counter() - t0:.2f}s')
