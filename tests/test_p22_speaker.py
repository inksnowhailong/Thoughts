"""P2.2 B1：声纹库 v2、旧库兼容、top-k 打分、备份回滚、注册会话。
全程用临时目录里的假库，不碰线上 data\\speakers.npz。
运行：cd /d F:\\coding\\guanjia && .venv\\Scripts\\python.exe tests\\test_p22_speaker.py
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from guanjia.enroll_session import MIN_TOTAL, EnrollSession, quality  # noqa: E402
from guanjia.settings import Settings  # noqa: E402
from guanjia.speaker import STORE, SpeakerGate, backup_path, load_store, save_store, score_against  # noqa: E402

failed = 0


def check(cond: bool, name: str) -> None:
    global failed
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        failed += 1


def unit(v):
    v = np.asarray(v, dtype=np.float32)
    return v / np.linalg.norm(v)


rng = np.random.default_rng(0)
tmp = Path(tempfile.mkdtemp(prefix="gj-spk-"))
try:
    # 1. top-k 平均
    refs = np.stack([unit([1, 0, 0]), unit([0.9, 0.1, 0]), unit([0, 1, 0]), unit([0, 0, 1])])
    v = unit([1, 0, 0])
    sims = sorted((refs @ v).tolist())[-3:]
    check(abs(score_against(v, refs) - sum(sims) / 3) < 1e-6, "score = mean of top-3 cosines")
    check(abs(score_against(v, refs[:1]) - 1.0) < 1e-6, "single ref = plain cosine (legacy unchanged)")

    # 2. v2 存取
    store = tmp / "speakers.npz"
    db = {"我": np.stack([unit(rng.standard_normal(192)) for _ in range(5)]), "妈": unit(rng.standard_normal(192))[None]}
    save_store(store, db)
    got, legacy = load_store(store, 192)
    check(not legacy and set(got) == {"我", "妈"} and got["我"].shape == (5, 192), "v2 round trip keeps names and all samples")
    check(not list(tmp.glob("*.tmp")), "no temp file left after save")

    # 3. 旧库：每人一个 (192,) 向量
    old = tmp / "old.npz"
    np.savez(old, **{"我": unit(rng.standard_normal(192))})
    got, legacy = load_store(old, 192)
    check(legacy and got["我"].shape == (1, 192), "legacy store read as [1,192]")
    try:
        load_store(old, 512)
        check(False, "dim mismatch raises")
    except ValueError:
        check(True, "dim mismatch raises")

    # 4. 备份文件名
    check(backup_path(Path("data/speakers.npz")).name == "speakers.campp.pre-reenroll.npz", "campplus backup name")
    check(backup_path(Path("data/speakers.eres2netv2.npz")).name == "speakers.eres2netv2.pre-reenroll.npz", "other model backup name")

    # 5. 质量检查
    sr = 16000
    voice = (0.3 * np.sin(np.arange(sr * 3) / 7)).astype(np.float32)
    check(quality(voice)[0], "3s voiced segment passes")
    short = voice[: sr]
    check(not quality(short)[0], "1s segment rejected (effective < 1.5s)")
    gated = voice.copy()
    gated[: int(len(gated) * 0.6)] = 0
    check(not quality(gated)[0], "60% digital zeros rejected")
    check(not quality(voice * 0.01)[0], "too quiet rejected")
    check(not quality(np.tile(voice, 4))[0], "12s segment rejected")

    # 6. 注册会话（假向量函数）
    calls = []

    def fake_embed(x):
        calls.append(len(x))
        return unit(rng.standard_normal(192))

    s = EnrollSession("我", fake_embed, old_refs=None)
    check(s.snapshot()["group"] == "near", "starts with near group")
    r = s.feed(short)
    check(not r["ok"] and len(calls) == 0, "bad segment not embedded")
    r = s.feed(voice, playing=True)
    check(not r["ok"] and len(calls) == 0, "segment during playback ignored")
    for _ in range(MIN_TOTAL - 1):
        s.feed(voice)
    check(not s.to_testing(), "cannot test before MIN_TOTAL")
    s.feed(voice)
    s.discard_last()
    check(s.snapshot()["total"] == MIN_TOTAL - 1, "discard_last removes one")
    s.feed(voice)
    check(s.to_testing() and s.state == "testing", "enough samples -> testing")
    s.feed(voice)
    snap = s.snapshot()
    check(s.state == "ready" and snap["total"] == MIN_TOTAL and snap["test_score"] is not None, "test segment scored, not added")
    check(snap["test_old_score"] is None, "no old refs -> no old score")

    # 7. 真模型：旧库读取 → set_person 写 v2 → 备份 → 回滚（临时目录里的假库）
    live = tmp / "speakers.npz"
    live.unlink()
    check(live != STORE, "test never points at the live store")
    g0 = SpeakerGate(store=live)
    dim = g0.dim
    check(g0.names() == [] and not g0.legacy, "missing store -> empty gate")
    np.savez(live, **{"我": unit(rng.standard_normal(dim))})
    g = SpeakerGate(store=live)
    check(g.legacy and g.counts() == {"我": 1}, "gate loads legacy store")
    check(g.threshold == Settings().spk_threshold, "default threshold comes from Settings")
    name, score = g.identify(voice)
    check(isinstance(score, float), "identify works on legacy store")
    b = g.backup()
    check(b is not None and b.exists(), "backup created")
    g.set_person("我", np.stack([g.embed(voice), g.embed(voice[::-1].copy())]))
    check(not g.legacy and g.counts() == {"我": 2}, "set_person replaces samples and writes v2")
    g.backup()
    check(load_store(b, dim)[1], "pre-reenroll backup is never overwritten (still legacy)")
    check(g.rollback() and g.legacy and g.counts() == {"我": 1}, "rollback restores pre-reenroll store")
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print("ALL PASS test_p22_speaker" if failed == 0 else f"{failed} FAILED test_p22_speaker")
sys.exit(1 if failed else 0)
