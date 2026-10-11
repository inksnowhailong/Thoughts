"""声纹注册（命令行版）：录 N 秒你的声音（自然说话即可），切出合格的语音段，每段存一个向量。

用法：uv run python -m guanjia.enroll --name 我 --seconds 12 --in 20
默认「替换」这个人原有的全部样本；加 --append 则在原有样本后面追加（近/远、平静/带情绪各录一次，追加最稳）。
保存前会备份旧库（pre-reenroll 只建一次、永不覆盖，另有 .prev）。更省事的做法是用面板「声纹」卡片引导注册。
"""

from __future__ import annotations

import argparse

import numpy as np
import sounddevice as sd

from guanjia.enroll_session import quality
from guanjia.speaker import SpeakerGate
from guanjia.vad import VAD, Downsampler
from guanjia.voice import AEC_ENABLED, MIC_GAIN, aec_cancel, apply_mic_gain, make_aec, pick_stream_rate


def to_16k_like_daemon(pcm: np.ndarray, sr: int, gain: float = MIC_GAIN, aec_on: bool = AEC_ENABLED) -> np.ndarray:
    """按守护进程同一条链处理录音：逐 10ms 帧过 AEC（参考为零，只起预处理/降噪作用）→ FIR 降采样到 16k → 软件增益。"""
    aec, down, frame = make_aec(sr), Downsampler(sr), sr // 100
    zeros = np.zeros(frame, dtype=np.float32)
    n = len(pcm) // frame * frame
    return apply_mic_gain(
        np.concatenate([down.process(aec_cancel(aec, pcm[i:i + frame], zeros) if aec_on else pcm[i:i + frame])
                        for i in range(0, n, frame)]), gain)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True)
    ap.add_argument("--seconds", type=float, default=12.0)
    ap.add_argument("--in", dest="in_dev", type=int, default=None)
    ap.add_argument("--mic-gain", type=float, default=MIC_GAIN, help="与守护进程一致的软件增益")
    ap.add_argument("--out", dest="out_dev", type=int, default=None, help="与守护进程相同的输出设备，用来取同一采样率")
    ap.add_argument("--append", action="store_true", help="在这个名字原有的样本后追加（默认替换）")
    a = ap.parse_args()

    # 采样率与守护进程一致：取输出设备的默认采样率
    sr = pick_stream_rate(a.in_dev, a.out_dev)  # 与守护进程同一选法（优先 16k）
    dev = sd.query_devices(a.in_dev if a.in_dev is not None else sd.default.device[0])
    print(f"请说话 {a.seconds:.0f} 秒（{dev['name']} @ {sr}）...")
    rec = sd.rec(int(sr * a.seconds), samplerate=sr, channels=1, dtype="float32", device=a.in_dev)
    sd.wait()
    pcm = to_16k_like_daemon(rec[:, 0], sr, a.mic_gain)

    vad = VAD()
    for i in range(0, len(pcm), 1600):
        vad.push(pcm[i:i + 1600])
    segs = []
    for s in vad.flush():
        if s.duration < 0.8:
            continue
        ok, reason, _ = quality(s.samples)
        if ok:
            segs.append(s)
        else:
            print(f"跳过一段（{s.duration:.1f}s）：{reason}")
    if not segs:
        print("没有检出合格的语音段，请离麦近一点、说长一点再试")
        return
    gate = SpeakerGate()
    embs = np.stack([gate.embed(s.samples) for s in segs])
    if a.append and gate.refs(a.name) is not None:
        embs = np.concatenate([gate.refs(a.name), embs])
    try:
        backup = gate.save_person(a.name, embs)  # 锁内：备份旧库 + 整批一次写盘
    except ValueError as e:  # 现有库读不出来，不去覆盖它
        print(f"没有保存：{e}")
        return
    print(f"声纹模型 {gate.model.name}，库 {gate.store.name}（备份：{backup.name if backup else '无'}）")
    print(f"「{a.name}」现有 {len(embs)} 段样本（本次 {len(segs)} 段，共 {sum(s.duration for s in segs):.1f}s）；当前库：{gate.counts()}")
    print("自检：", gate.identify(np.concatenate([s.samples for s in segs])))
    print("提示：想再补近/远、大声/带情绪的样本，加 --append 再录一次（打分取最像的 3 段平均，覆盖场景越多越稳）。")


if __name__ == "__main__":
    main()
